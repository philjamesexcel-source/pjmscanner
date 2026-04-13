#!/usr/bin/env python3
"""
db.py — Database layer
All PostgreSQL queries. Used by screener.py, tracker.py, dashboard.py.
Schema supports two strategies, pullback tracking, and milestone alerts.
"""

import os
import logging
import time
from contextlib import contextmanager

import psycopg2
from psycopg2.extras import RealDictCursor


# ─────────────────────────────────────────────
# CONNECTION
# ─────────────────────────────────────────────

def get_dsn() -> str:
    return (
        f"host={os.environ.get('POSTGRES_HOST', 'postgres.screener.svc.cluster.local')} "
        f"port={os.environ.get('POSTGRES_PORT', '5432')} "
        f"dbname={os.environ.get('POSTGRES_DB', 'screener')} "
        f"user={os.environ.get('POSTGRES_USER', 'screener')} "
        f"password={os.environ.get('POSTGRES_PASSWORD', '')}"
    )


@contextmanager
def get_conn():
    conn = psycopg2.connect(get_dsn())
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def wait_for_db(retries: int = 15, delay: int = 5):
    for attempt in range(retries):
        try:
            with get_conn():
                pass
            logging.info("DB: connected to PostgreSQL")
            return
        except Exception as e:
            logging.warning(f"DB: waiting ({attempt+1}/{retries}): {e}")
            time.sleep(delay)
    raise RuntimeError("DB: could not connect to PostgreSQL after retries")


# ─────────────────────────────────────────────
# SCHEMA
# ─────────────────────────────────────────────

SCHEMA = """
-- Core alerts table
-- One row per token per strategy. strategy column separates A vs B.
CREATE TABLE IF NOT EXISTS alerts (
    id              SERIAL PRIMARY KEY,
    mint            TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    name            TEXT NOT NULL,
    pair_addr       TEXT NOT NULL,
    strategy        TEXT NOT NULL,          -- 'A' or 'B'
    mc_at_alert     NUMERIC NOT NULL,       -- MC when initial alert fired
    price_at_alert  NUMERIC NOT NULL,       -- price when initial alert fired
    peak_mc         NUMERIC,                -- highest MC seen since alert
    peak_price      NUMERIC,               -- highest price seen since alert
    lowest_price    NUMERIC,               -- lowest price since peak (for pullback calc)
    alerted_at      TIMESTAMPTZ NOT NULL,
    check_due_at    TIMESTAMPTZ NOT NULL,   -- 72h after alerted_at
    cycle_start     TIMESTAMPTZ NOT NULL,
    pullback_watching BOOLEAN DEFAULT TRUE, -- still watching for pullback entry
    UNIQUE(mint, strategy)                  -- one alert per token per strategy
);

-- Pullback entry alerts
-- Fires when token pulls back the right amount with volume confirmation
CREATE TABLE IF NOT EXISTS pullback_alerts (
    id              SERIAL PRIMARY KEY,
    alert_id        INT REFERENCES alerts(id),
    mint            TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    strategy        TEXT NOT NULL,
    mc_at_pullback  NUMERIC NOT NULL,
    price_at_pullback NUMERIC NOT NULL,
    pullback_pct    NUMERIC NOT NULL,       -- % drop from peak
    vol_5m          NUMERIC,               -- 5m volume at time of pullback alert
    sent_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Performance milestones
-- Fires when coin hits 2x, 5x, 10x vs alert MC or entry price
CREATE TABLE IF NOT EXISTS milestones (
    id              SERIAL PRIMARY KEY,
    alert_id        INT REFERENCES alerts(id),
    mint            TEXT NOT NULL,
    strategy        TEXT NOT NULL,
    milestone_type  TEXT NOT NULL,          -- 'vs_alert' or 'vs_entry'
    multiplier      NUMERIC NOT NULL,       -- 2, 5, 10 etc
    mc_at_milestone NUMERIC,
    price_at_milestone NUMERIC,
    sent_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(alert_id, milestone_type, multiplier)
);

-- 72h outcomes
CREATE TABLE IF NOT EXISTS outcomes (
    id              SERIAL PRIMARY KEY,
    alert_id        INT REFERENCES alerts(id),
    mint            TEXT NOT NULL,
    strategy        TEXT NOT NULL,
    mc_at_72h       NUMERIC,
    multiplier_vs_alert NUMERIC,            -- mc_at_72h / mc_at_alert
    multiplier_vs_entry NUMERIC,           -- mc_at_72h / mc_at_pullback (if entry fired)
    outcome         TEXT,                   -- moon | up | flat | down | dead
    checked_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 3-day cycles
CREATE TABLE IF NOT EXISTS cycles (
    id              SERIAL PRIMARY KEY,
    cycle_start     TIMESTAMPTZ NOT NULL UNIQUE,
    cycle_end       TIMESTAMPTZ NOT NULL,
    report_sent_at  TIMESTAMPTZ,
    total_alerts    INT DEFAULT 0,
    total_checked   INT DEFAULT 0,
    avg_multiplier  NUMERIC,
    moon_count      INT DEFAULT 0,
    up_count        INT DEFAULT 0,
    flat_count      INT DEFAULT 0,
    down_count      INT DEFAULT 0,
    dead_count      INT DEFAULT 0
);

-- Live price snapshots (updated every 5 min by background thread)
CREATE TABLE IF NOT EXISTS live_metrics (
    alert_id        INT PRIMARY KEY REFERENCES alerts(id),
    mint            TEXT NOT NULL,
    strategy        TEXT NOT NULL,
    current_mc      NUMERIC,
    current_price   NUMERIC,
    current_liq     NUMERIC,
    vol_24h         NUMERIC,
    vol_5m          NUMERIC,
    price_change_1h NUMERIC,
    price_change_24h NUMERIC,
    multiplier_vs_alert NUMERIC,
    multiplier_vs_entry NUMERIC,
    trend           TEXT DEFAULT 'flat',
    last_updated    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_alerts_strategy ON alerts(strategy);
CREATE INDEX IF NOT EXISTS idx_alerts_mint ON alerts(mint);
CREATE INDEX IF NOT EXISTS idx_live_metrics_mult ON live_metrics(multiplier_vs_alert DESC NULLS LAST);
"""


def init_schema():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(SCHEMA)
    logging.info("DB: schema ready")


# ─────────────────────────────────────────────
# ALERTS
# ─────────────────────────────────────────────

def insert_alert(mint, symbol, name, pair_addr, strategy,
                 mc_at_alert, price_at_alert, alerted_at,
                 check_due_at, cycle_start) -> int:
    """Insert initial alert. Returns alert id. Ignores duplicate mint+strategy."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO alerts
                    (mint, symbol, name, pair_addr, strategy,
                     mc_at_alert, price_at_alert, peak_mc, peak_price,
                     lowest_price, alerted_at, check_due_at, cycle_start)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (mint, strategy) DO NOTHING
                RETURNING id
            """, (mint, symbol, name, pair_addr, strategy,
                  float(mc_at_alert), float(price_at_alert),
                  float(mc_at_alert), float(price_at_alert),
                  float(price_at_alert),
                  alerted_at, check_due_at, cycle_start))
            row = cur.fetchone()
            return row[0] if row else None


def get_alerts_for_monitoring(strategy: str = None) -> list:
    """All alerts still being watched for pullback entry."""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            if strategy:
                cur.execute("""
                    SELECT * FROM alerts
                    WHERE pullback_watching = TRUE
                    AND strategy = %s
                    ORDER BY alerted_at DESC
                """, (strategy,))
            else:
                cur.execute("""
                    SELECT * FROM alerts
                    WHERE pullback_watching = TRUE
                    ORDER BY alerted_at DESC
                """)
            return cur.fetchall()


def get_pending_72h_checks() -> list:
    """Alerts whose 72h check is due but not yet completed."""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT a.* FROM alerts a
                LEFT JOIN outcomes o ON a.id = o.alert_id
                WHERE o.id IS NULL
                AND a.check_due_at <= NOW()
            """)
            return cur.fetchall()


def get_all_pending_alerts() -> list:
    """All alerts without completed outcomes (for interim report)."""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT a.* FROM alerts a
                LEFT JOIN outcomes o ON a.id = o.alert_id
                WHERE o.id IS NULL
                ORDER BY a.alerted_at DESC
            """)
            return cur.fetchall()


def update_peak_and_lowest(alert_id: int, current_price: float,
                            current_mc: float):
    """Update peak and lowest price tracking for pullback detection."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE alerts SET
                    peak_mc    = GREATEST(COALESCE(peak_mc, 0), %s),
                    peak_price = GREATEST(COALESCE(peak_price, 0), %s),
                    lowest_price = CASE
                        WHEN %s > COALESCE(peak_price, 0) THEN %s
                        ELSE LEAST(COALESCE(lowest_price, %s), %s)
                    END
                WHERE id = %s
            """, (float(current_mc), float(current_price),
                  float(current_price), float(current_price),
                  float(current_price), float(current_price),
                  alert_id))


def stop_watching_pullback(alert_id: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE alerts SET pullback_watching = FALSE WHERE id = %s",
                (alert_id,)
            )


def mint_strategy_exists(mint: str, strategy: str) -> bool:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM alerts WHERE mint = %s AND strategy = %s",
                (mint, strategy)
            )
            return cur.fetchone() is not None


# ─────────────────────────────────────────────
# PULLBACK ALERTS
# ─────────────────────────────────────────────

def insert_pullback_alert(alert_id, mint, symbol, strategy,
                           mc_at_pullback, price_at_pullback,
                           pullback_pct, vol_5m):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO pullback_alerts
                    (alert_id, mint, symbol, strategy,
                     mc_at_pullback, price_at_pullback, pullback_pct, vol_5m)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            """, (alert_id, mint, symbol, strategy,
                  float(mc_at_pullback), float(price_at_pullback),
                  float(pullback_pct), float(vol_5m or 0)))


def get_latest_pullback(alert_id: int):
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT * FROM pullback_alerts
                WHERE alert_id = %s
                ORDER BY sent_at DESC LIMIT 1
            """, (alert_id,))
            return cur.fetchone()


# ─────────────────────────────────────────────
# MILESTONES
# ─────────────────────────────────────────────

def milestone_sent(alert_id: int, milestone_type: str, multiplier: float) -> bool:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT 1 FROM milestones
                WHERE alert_id = %s
                AND milestone_type = %s
                AND multiplier = %s
            """, (alert_id, milestone_type, float(multiplier)))
            return cur.fetchone() is not None


def insert_milestone(alert_id, mint, strategy, milestone_type,
                      multiplier, mc_at_milestone, price_at_milestone):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO milestones
                    (alert_id, mint, strategy, milestone_type,
                     multiplier, mc_at_milestone, price_at_milestone)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (alert_id, milestone_type, multiplier) DO NOTHING
            """, (alert_id, mint, strategy, milestone_type,
                  float(multiplier),
                  float(mc_at_milestone) if mc_at_milestone else None,
                  float(price_at_milestone) if price_at_milestone else None))


# ─────────────────────────────────────────────
# OUTCOMES
# ─────────────────────────────────────────────

def insert_outcome(alert_id, mint, strategy, mc_at_72h,
                   mult_vs_alert, mult_vs_entry, outcome):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO outcomes
                    (alert_id, mint, strategy, mc_at_72h,
                     multiplier_vs_alert, multiplier_vs_entry, outcome)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT DO NOTHING
            """, (alert_id, mint, strategy,
                  float(mc_at_72h) if mc_at_72h else None,
                  float(mult_vs_alert) if mult_vs_alert else None,
                  float(mult_vs_entry) if mult_vs_entry else None,
                  outcome))


# ─────────────────────────────────────────────
# LIVE METRICS
# ─────────────────────────────────────────────

def upsert_live_metrics(alert_id, mint, strategy, current_mc,
                         current_price, current_liq, vol_24h, vol_5m,
                         price_change_1h, price_change_24h,
                         mc_at_alert, entry_price=None):
    mult_vs_alert = (current_mc / mc_at_alert) if mc_at_alert and mc_at_alert > 0 else None
    mult_vs_entry = (current_mc / entry_price) if entry_price and entry_price > 0 else None

    with get_conn() as conn:
        with conn.cursor() as cur:
            # Determine trend
            cur.execute(
                "SELECT current_mc FROM live_metrics WHERE alert_id = %s",
                (alert_id,)
            )
            prev = cur.fetchone()
            if prev and prev[0] and current_mc:
                trend = 'up' if current_mc > float(prev[0]) else ('down' if current_mc < float(prev[0]) else 'flat')
            else:
                trend = 'flat'

            cur.execute("""
                INSERT INTO live_metrics
                    (alert_id, mint, strategy, current_mc, current_price,
                     current_liq, vol_24h, vol_5m, price_change_1h,
                     price_change_24h, multiplier_vs_alert,
                     multiplier_vs_entry, trend, last_updated)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
                ON CONFLICT (alert_id) DO UPDATE SET
                    current_mc          = EXCLUDED.current_mc,
                    current_price       = EXCLUDED.current_price,
                    current_liq         = EXCLUDED.current_liq,
                    vol_24h             = EXCLUDED.vol_24h,
                    vol_5m              = EXCLUDED.vol_5m,
                    price_change_1h     = EXCLUDED.price_change_1h,
                    price_change_24h    = EXCLUDED.price_change_24h,
                    multiplier_vs_alert = EXCLUDED.multiplier_vs_alert,
                    multiplier_vs_entry = EXCLUDED.multiplier_vs_entry,
                    trend               = EXCLUDED.trend,
                    last_updated        = NOW()
            """, (alert_id, mint, strategy,
                  float(current_mc) if current_mc else None,
                  float(current_price) if current_price else None,
                  float(current_liq) if current_liq else None,
                  float(vol_24h) if vol_24h else None,
                  float(vol_5m) if vol_5m else None,
                  float(price_change_1h) if price_change_1h else None,
                  float(price_change_24h) if price_change_24h else None,
                  float(mult_vs_alert) if mult_vs_alert else None,
                  float(mult_vs_entry) if mult_vs_entry else None,
                  trend))


# ─────────────────────────────────────────────
# DASHBOARD QUERIES
# ─────────────────────────────────────────────

def get_dashboard_data(strategy: str = None) -> list:
    """Full join for dashboard — all alerts with live metrics and outcomes."""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            where = "WHERE a.strategy = %s" if strategy else ""
            params = (strategy,) if strategy else ()
            cur.execute(f"""
                SELECT
                    a.id, a.mint, a.symbol, a.name, a.pair_addr,
                    a.strategy, a.mc_at_alert, a.price_at_alert,
                    a.peak_mc, a.peak_price, a.alerted_at, a.check_due_at,
                    a.pullback_watching,
                    pb.price_at_pullback, pb.pullback_pct, pb.vol_5m AS pullback_vol,
                    pb.sent_at AS pullback_sent_at,
                    o.mc_at_72h, o.multiplier_vs_alert AS mult_72h,
                    o.multiplier_vs_entry AS mult_72h_entry, o.outcome,
                    lm.current_mc, lm.current_price, lm.vol_5m AS live_vol_5m,
                    lm.price_change_1h, lm.multiplier_vs_alert AS live_mult,
                    lm.multiplier_vs_entry AS live_mult_entry,
                    lm.trend, lm.last_updated
                FROM alerts a
                LEFT JOIN LATERAL (
                    SELECT * FROM pullback_alerts
                    WHERE alert_id = a.id
                    ORDER BY sent_at DESC LIMIT 1
                ) pb ON TRUE
                LEFT JOIN outcomes o ON o.alert_id = a.id
                LEFT JOIN live_metrics lm ON lm.alert_id = a.id
                {where}
                ORDER BY COALESCE(lm.multiplier_vs_alert, 0) DESC,
                         a.alerted_at DESC
            """, params)
            return cur.fetchall()


def get_strategy_summary(strategy: str) -> dict:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT
                    COUNT(a.id)                                     AS total_alerts,
                    COUNT(o.id)                                     AS total_checked,
                    ROUND(AVG(o.multiplier_vs_alert)::numeric, 2)  AS avg_mult,
                    COUNT(*) FILTER (WHERE o.outcome = 'moon')     AS moon_count,
                    COUNT(*) FILTER (WHERE o.outcome = 'up')       AS up_count,
                    COUNT(*) FILTER (WHERE o.outcome = 'flat')     AS flat_count,
                    COUNT(*) FILTER (WHERE o.outcome = 'down')     AS down_count,
                    COUNT(*) FILTER (WHERE o.outcome = 'dead')     AS dead_count,
                    COUNT(pb.id)                                    AS pullback_alerts_sent
                FROM alerts a
                LEFT JOIN outcomes o ON o.alert_id = a.id
                LEFT JOIN pullback_alerts pb ON pb.alert_id = a.id
                WHERE a.strategy = %s
            """, (strategy,))
            return dict(cur.fetchone() or {})


# ─────────────────────────────────────────────
# CYCLES
# ─────────────────────────────────────────────

def get_or_create_cycle(cycle_start, cycle_end):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO cycles (cycle_start, cycle_end)
                VALUES (%s, %s)
                ON CONFLICT (cycle_start) DO NOTHING
            """, (cycle_start, cycle_end))


def get_all_cycles() -> list:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT c.*,
                    COUNT(a.id) AS total_alerts
                FROM cycles c
                LEFT JOIN alerts a ON a.cycle_start = c.cycle_start
                GROUP BY c.id
                ORDER BY c.cycle_start DESC
            """)
            return cur.fetchall()


def mark_cycle_reported(cycle_start, stats: dict):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE cycles SET
                    report_sent_at = NOW(),
                    total_alerts   = %s,
                    total_checked  = %s,
                    avg_multiplier = %s,
                    moon_count     = %s,
                    up_count       = %s,
                    flat_count     = %s,
                    down_count     = %s,
                    dead_count     = %s
                WHERE cycle_start = %s
            """, (
                stats.get("total_alerts", 0),
                stats.get("total_checked", 0),
                stats.get("avg_mult"),
                stats.get("moon_count", 0),
                stats.get("up_count", 0),
                stats.get("flat_count", 0),
                stats.get("down_count", 0),
                stats.get("dead_count", 0),
                cycle_start
            ))
