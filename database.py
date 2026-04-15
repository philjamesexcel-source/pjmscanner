"""
core/database.py — PostgreSQL layer.
Complete schema: tokens, entry_signals, wallets, wallet_trades,
performance_tracking, config_versions.
"""

import os
import logging
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Optional

import psycopg2
from psycopg2.extras import RealDictCursor, execute_values

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# CONNECTION
# ─────────────────────────────────────────────

def _dsn() -> str:
    return (
        f"host={os.environ.get('POSTGRES_HOST', 'postgres.screener.svc.cluster.local')} "
        f"port={os.environ.get('POSTGRES_PORT', '5432')} "
        f"dbname={os.environ.get('POSTGRES_DB', 'screener')} "
        f"user={os.environ.get('POSTGRES_USER', 'screener')} "
        f"password={os.environ.get('POSTGRES_PASSWORD', '')}"
    )


@contextmanager
def conn():
    c = psycopg2.connect(_dsn())
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def wait_for_db(retries: int = 20, delay: int = 5):
    for i in range(retries):
        try:
            with conn():
                pass
            logger.info("DB: connected")
            return
        except Exception as e:
            logger.warning(f"DB: waiting ({i+1}/{retries}): {e}")
            time.sleep(delay)
    raise RuntimeError("DB: failed to connect after retries")


# ─────────────────────────────────────────────
# SCHEMA
# ─────────────────────────────────────────────

SCHEMA = """
-- ── tokens ────────────────────────────────────────────────────
-- One row per detected token per strategy.
CREATE TABLE IF NOT EXISTS tokens (
    id                  SERIAL PRIMARY KEY,
    mint                TEXT NOT NULL,
    symbol              TEXT NOT NULL,
    name                TEXT NOT NULL,
    pair_addr           TEXT NOT NULL,
    chain               TEXT NOT NULL DEFAULT 'solana',
    dex                 TEXT,
    strategy            TEXT NOT NULL,          -- A | B | C
    score               NUMERIC NOT NULL,        -- 0–100

    -- Market data at detection
    mc_at_detection     NUMERIC NOT NULL,
    price_at_detection  NUMERIC NOT NULL,
    liq_at_detection    NUMERIC NOT NULL,
    vol_1h_at_detection NUMERIC,
    vol_24h_at_detection NUMERIC,
    holders_at_detection INT,
    buy_sell_ratio      NUMERIC,
    lp_locked_pct       NUMERIC,

    -- Security
    rugcheck_score      NUMERIC,
    mint_renounced      BOOLEAN,
    freeze_renounced    BOOLEAN,
    deployer            TEXT,

    -- State
    detected_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    alerted_at          TIMESTAMPTZ,
    alert_sent          BOOLEAN DEFAULT FALSE,
    watching_pullback   BOOLEAN DEFAULT TRUE,
    outcome_checked     BOOLEAN DEFAULT FALSE,
    check_due_at        TIMESTAMPTZ,

    -- Peak tracking (for pullback detection and milestone tracking)
    peak_mc             NUMERIC,
    peak_price          NUMERIC,
    lowest_price_since_peak NUMERIC,

    UNIQUE(mint, strategy)
);

CREATE INDEX IF NOT EXISTS idx_tokens_strategy    ON tokens(strategy);
CREATE INDEX IF NOT EXISTS idx_tokens_mint        ON tokens(mint);
CREATE INDEX IF NOT EXISTS idx_tokens_watching    ON tokens(watching_pullback);
CREATE INDEX IF NOT EXISTS idx_tokens_score       ON tokens(score DESC);

-- ── entry_signals ──────────────────────────────────────────────
-- Fires when a token pulls back into a confirmed entry zone.
CREATE TABLE IF NOT EXISTS entry_signals (
    id                  SERIAL PRIMARY KEY,
    token_id            INT REFERENCES tokens(id),
    mint                TEXT NOT NULL,
    symbol              TEXT NOT NULL,
    strategy            TEXT NOT NULL,
    signal_type         TEXT NOT NULL,          -- pullback | reaccumulation
    mc_at_signal        NUMERIC NOT NULL,
    price_at_signal     NUMERIC NOT NULL,
    pullback_pct        NUMERIC,                -- % drop from peak
    vol_5m_at_signal    NUMERIC,
    sent_at             TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── performance_tracking ───────────────────────────────────────
-- Live price snapshots for all detected tokens.
CREATE TABLE IF NOT EXISTS performance_tracking (
    token_id            INT PRIMARY KEY REFERENCES tokens(id),
    mint                TEXT NOT NULL,
    strategy            TEXT NOT NULL,

    -- Current market state
    current_mc          NUMERIC,
    current_price       NUMERIC,
    current_liq         NUMERIC,
    vol_5m              NUMERIC,
    vol_1h              NUMERIC,
    vol_24h             NUMERIC,
    price_change_1h     NUMERIC,
    price_change_24h    NUMERIC,
    buy_sell_ratio_1h   NUMERIC,

    -- Performance vs detection
    multiple_vs_detection NUMERIC,

    -- Performance vs entry signal (if fired)
    entry_price         NUMERIC,
    multiple_vs_entry   NUMERIC,

    -- 72h outcome
    mc_at_72h           NUMERIC,
    outcome             TEXT,                   -- moon|up|flat|down|dead

    trend               TEXT DEFAULT 'flat',    -- up|down|flat
    last_updated        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_perf_mult ON performance_tracking(multiple_vs_detection DESC NULLS LAST);

-- ── milestones ─────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS milestones (
    id              SERIAL PRIMARY KEY,
    token_id        INT REFERENCES tokens(id),
    mint            TEXT NOT NULL,
    strategy        TEXT NOT NULL,
    milestone_type  TEXT NOT NULL,    -- vs_detection | vs_entry
    multiplier      NUMERIC NOT NULL, -- 2, 5, 10, 20
    mc_at_milestone NUMERIC,
    sent_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(token_id, milestone_type, multiplier)
);

-- ── wallets ────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS wallets (
    id              SERIAL PRIMARY KEY,
    address         TEXT UNIQUE NOT NULL,
    label           TEXT,                       -- optional human label
    score           NUMERIC DEFAULT 0,          -- 0–1 wallet quality score
    win_rate        NUMERIC,
    avg_roi         NUMERIC,
    early_accuracy  NUMERIC,
    total_trades    INT DEFAULT 0,
    winning_trades  INT DEFAULT 0,
    tracked_since   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_active     TIMESTAMPTZ,
    last_scored_at  TIMESTAMPTZ,
    active          BOOLEAN DEFAULT TRUE
);

CREATE INDEX IF NOT EXISTS idx_wallets_score ON wallets(score DESC);

-- ── wallet_trades ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS wallet_trades (
    id              SERIAL PRIMARY KEY,
    wallet_id       INT REFERENCES wallets(id),
    wallet_address  TEXT NOT NULL,
    mint            TEXT NOT NULL,
    symbol          TEXT,
    action          TEXT NOT NULL,              -- buy | sell
    amount_usd      NUMERIC,
    mc_at_trade     NUMERIC,
    price_at_trade  NUMERIC,
    tx_signature    TEXT,
    traded_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    notified        BOOLEAN DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS idx_wallet_trades_wallet ON wallet_trades(wallet_id);
CREATE INDEX IF NOT EXISTS idx_wallet_trades_mint   ON wallet_trades(mint);
CREATE INDEX IF NOT EXISTS idx_wallet_trades_notified ON wallet_trades(notified);

-- ── config_versions ────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS config_versions (
    id          SERIAL PRIMARY KEY,
    version     TEXT NOT NULL,
    config_json JSONB NOT NULL,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    stable      BOOLEAN DEFAULT FALSE,
    notes       TEXT
);
"""


def init_schema():
    with conn() as c:
        with c.cursor() as cur:
            cur.execute(SCHEMA)
    logger.info("DB: schema ready")


# ─────────────────────────────────────────────
# TOKENS
# ─────────────────────────────────────────────

def insert_token(mint, symbol, name, pair_addr, chain, dex, strategy,
                 score, mc, price, liq, vol_1h, vol_24h, holders,
                 buy_sell_ratio, lp_locked_pct, rugcheck_score,
                 mint_renounced, freeze_renounced, deployer,
                 check_due_at) -> Optional[int]:
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("""
                INSERT INTO tokens
                    (mint, symbol, name, pair_addr, chain, dex, strategy,
                     score, mc_at_detection, price_at_detection,
                     liq_at_detection, vol_1h_at_detection, vol_24h_at_detection,
                     holders_at_detection, buy_sell_ratio, lp_locked_pct,
                     rugcheck_score, mint_renounced, freeze_renounced,
                     deployer, check_due_at,
                     peak_mc, peak_price, lowest_price_since_peak)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (mint, strategy) DO NOTHING
                RETURNING id
            """, (mint, symbol, name, pair_addr, chain, dex, strategy,
                  float(score), float(mc), float(price), float(liq),
                  float(vol_1h or 0), float(vol_24h or 0),
                  int(holders or 0), float(buy_sell_ratio or 0),
                  float(lp_locked_pct or 0), float(rugcheck_score or 0),
                  bool(mint_renounced), bool(freeze_renounced),
                  deployer, check_due_at,
                  float(mc), float(price), float(price)))
            row = cur.fetchone()
            return row[0] if row else None


def get_tokens_watching() -> list:
    with conn() as c:
        with c.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT * FROM tokens
                WHERE watching_pullback = TRUE
                AND outcome_checked = FALSE
                ORDER BY detected_at DESC
            """)
            return cur.fetchall()


def get_pending_72h() -> list:
    with conn() as c:
        with c.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT t.*, pt.current_mc
                FROM tokens t
                LEFT JOIN performance_tracking pt ON pt.token_id = t.id
                WHERE t.outcome_checked = FALSE
                AND t.check_due_at <= NOW()
            """)
            return cur.fetchall()


def update_peak(token_id: int, price: float, mc: float):
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("""
                UPDATE tokens SET
                    peak_mc    = GREATEST(COALESCE(peak_mc, 0), %s),
                    peak_price = GREATEST(COALESCE(peak_price, 0), %s),
                    lowest_price_since_peak = CASE
                        WHEN %s >= COALESCE(peak_price, 0) THEN %s
                        ELSE LEAST(COALESCE(lowest_price_since_peak, %s), %s)
                    END
                WHERE id = %s
            """, (float(mc), float(price),
                  float(price), float(price),
                  float(price), float(price),
                  token_id))


def mark_alerted(token_id: int):
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("""
                UPDATE tokens SET alert_sent = TRUE, alerted_at = NOW()
                WHERE id = %s
            """, (token_id,))


def mark_outcome_checked(token_id: int):
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("""
                UPDATE tokens SET outcome_checked = TRUE, watching_pullback = FALSE
                WHERE id = %s
            """, (token_id,))


def mint_strategy_exists(mint: str, strategy: str) -> bool:
    with conn() as c:
        with c.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM tokens WHERE mint = %s AND strategy = %s",
                (mint, strategy)
            )
            return cur.fetchone() is not None


# ─────────────────────────────────────────────
# ENTRY SIGNALS
# ─────────────────────────────────────────────

def insert_entry_signal(token_id, mint, symbol, strategy,
                         signal_type, mc, price, pullback_pct, vol_5m):
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("""
                INSERT INTO entry_signals
                    (token_id, mint, symbol, strategy, signal_type,
                     mc_at_signal, price_at_signal, pullback_pct, vol_5m_at_signal)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (token_id, mint, symbol, strategy, signal_type,
                  float(mc), float(price),
                  float(pullback_pct) if pullback_pct else None,
                  float(vol_5m) if vol_5m else None))


def get_latest_entry_signal(token_id: int):
    with conn() as c:
        with c.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT * FROM entry_signals
                WHERE token_id = %s
                ORDER BY sent_at DESC LIMIT 1
            """, (token_id,))
            return cur.fetchone()


# ─────────────────────────────────────────────
# PERFORMANCE TRACKING
# ─────────────────────────────────────────────

def upsert_performance(token_id, mint, strategy, current_mc,
                        current_price, current_liq, vol_5m, vol_1h,
                        vol_24h, price_change_1h, price_change_24h,
                        buy_sell_ratio_1h, mc_at_detection,
                        entry_price=None):
    mult_det = (current_mc / mc_at_detection) if mc_at_detection and mc_at_detection > 0 else None
    mult_ent = (current_mc / entry_price) if entry_price and entry_price > 0 else None

    with conn() as c:
        with c.cursor() as cur:
            cur.execute("SELECT current_mc FROM performance_tracking WHERE token_id = %s", (token_id,))
            prev = cur.fetchone()
            if prev and prev[0] and current_mc:
                trend = 'up' if current_mc > float(prev[0]) else ('down' if current_mc < float(prev[0]) else 'flat')
            else:
                trend = 'flat'

            cur.execute("""
                INSERT INTO performance_tracking
                    (token_id, mint, strategy, current_mc, current_price,
                     current_liq, vol_5m, vol_1h, vol_24h,
                     price_change_1h, price_change_24h, buy_sell_ratio_1h,
                     multiple_vs_detection, entry_price, multiple_vs_entry,
                     trend, last_updated)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
                ON CONFLICT (token_id) DO UPDATE SET
                    current_mc          = EXCLUDED.current_mc,
                    current_price       = EXCLUDED.current_price,
                    current_liq         = EXCLUDED.current_liq,
                    vol_5m              = EXCLUDED.vol_5m,
                    vol_1h              = EXCLUDED.vol_1h,
                    vol_24h             = EXCLUDED.vol_24h,
                    price_change_1h     = EXCLUDED.price_change_1h,
                    price_change_24h    = EXCLUDED.price_change_24h,
                    buy_sell_ratio_1h   = EXCLUDED.buy_sell_ratio_1h,
                    multiple_vs_detection = EXCLUDED.multiple_vs_detection,
                    entry_price         = COALESCE(performance_tracking.entry_price, EXCLUDED.entry_price),
                    multiple_vs_entry   = CASE
                        WHEN performance_tracking.entry_price IS NOT NULL
                        THEN %s
                        ELSE EXCLUDED.multiple_vs_entry
                    END,
                    trend               = EXCLUDED.trend,
                    last_updated        = NOW()
            """, (token_id, mint, strategy,
                  _f(current_mc), _f(current_price), _f(current_liq),
                  _f(vol_5m), _f(vol_1h), _f(vol_24h),
                  _f(price_change_1h), _f(price_change_24h),
                  _f(buy_sell_ratio_1h),
                  _f(mult_det), _f(entry_price), _f(mult_ent),
                  trend,
                  _f(mult_ent)))


def set_outcome(token_id, mc_at_72h, outcome):
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("""
                UPDATE performance_tracking
                SET mc_at_72h = %s, outcome = %s
                WHERE token_id = %s
            """, (_f(mc_at_72h), outcome, token_id))


def _f(v) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


# ─────────────────────────────────────────────
# MILESTONES
# ─────────────────────────────────────────────

def milestone_sent(token_id: int, milestone_type: str, mult: float) -> bool:
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("""
                SELECT 1 FROM milestones
                WHERE token_id = %s AND milestone_type = %s AND multiplier = %s
            """, (token_id, milestone_type, float(mult)))
            return cur.fetchone() is not None


def insert_milestone(token_id, mint, strategy, milestone_type, mult, mc):
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("""
                INSERT INTO milestones
                    (token_id, mint, strategy, milestone_type, multiplier, mc_at_milestone)
                VALUES (%s,%s,%s,%s,%s,%s)
                ON CONFLICT (token_id, milestone_type, multiplier) DO NOTHING
            """, (token_id, mint, strategy, milestone_type, float(mult), _f(mc)))


# ─────────────────────────────────────────────
# WALLETS
# ─────────────────────────────────────────────

def upsert_wallet(address, score=0, win_rate=None, avg_roi=None,
                   early_accuracy=None, total_trades=0, winning_trades=0):
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("""
                INSERT INTO wallets
                    (address, score, win_rate, avg_roi, early_accuracy,
                     total_trades, winning_trades, last_scored_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,NOW())
                ON CONFLICT (address) DO UPDATE SET
                    score          = EXCLUDED.score,
                    win_rate       = EXCLUDED.win_rate,
                    avg_roi        = EXCLUDED.avg_roi,
                    early_accuracy = EXCLUDED.early_accuracy,
                    total_trades   = EXCLUDED.total_trades,
                    winning_trades = EXCLUDED.winning_trades,
                    last_scored_at = NOW()
            """, (address, float(score),
                  _f(win_rate), _f(avg_roi), _f(early_accuracy),
                  int(total_trades), int(winning_trades)))


def get_active_wallets(min_score: float = 0.60) -> list:
    with conn() as c:
        with c.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT * FROM wallets
                WHERE active = TRUE AND score >= %s
                ORDER BY score DESC
            """, (float(min_score),))
            return cur.fetchall()


def insert_wallet_trade(wallet_address, mint, symbol, action,
                         amount_usd, mc_at_trade, price_at_trade,
                         tx_signature=None):
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("""
                INSERT INTO wallet_trades
                    (wallet_address, mint, symbol, action,
                     amount_usd, mc_at_trade, price_at_trade, tx_signature)
                SELECT %s,%s,%s,%s,%s,%s,%s,%s
                FROM wallets w WHERE w.address = %s
                LIMIT 1
            """, (wallet_address, mint, symbol, action,
                  _f(amount_usd), _f(mc_at_trade), _f(price_at_trade),
                  tx_signature, wallet_address))


def get_unnotified_wallet_trades() -> list:
    with conn() as c:
        with c.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT wt.*, w.score, w.win_rate, w.avg_roi
                FROM wallet_trades wt
                JOIN wallets w ON w.address = wt.wallet_address
                WHERE wt.notified = FALSE
                ORDER BY wt.traded_at DESC
            """)
            return cur.fetchall()


def mark_wallet_trades_notified(ids: list):
    if not ids:
        return
    with conn() as c:
        with c.cursor() as cur:
            cur.execute(
                "UPDATE wallet_trades SET notified = TRUE WHERE id = ANY(%s)",
                (ids,)
            )


# ─────────────────────────────────────────────
# DASHBOARD QUERIES
# ─────────────────────────────────────────────

def get_dashboard_tokens(strategy: str = None) -> list:
    with conn() as c:
        with c.cursor(cursor_factory=RealDictCursor) as cur:
            where = "WHERE t.strategy = %s" if strategy else ""
            params = (strategy,) if strategy else ()
            cur.execute(f"""
                SELECT
                    t.id, t.mint, t.symbol, t.name, t.pair_addr,
                    t.strategy, t.score, t.chain, t.dex,
                    t.mc_at_detection, t.price_at_detection,
                    t.liq_at_detection, t.holders_at_detection,
                    t.buy_sell_ratio, t.lp_locked_pct,
                    t.rugcheck_score, t.mint_renounced, t.freeze_renounced,
                    t.deployer, t.detected_at, t.alerted_at,
                    t.peak_mc, t.watching_pullback,
                    es.signal_type, es.mc_at_signal, es.price_at_signal,
                    es.pullback_pct, es.sent_at AS entry_sent_at,
                    pt.current_mc, pt.current_price, pt.current_liq,
                    pt.vol_5m, pt.vol_1h, pt.price_change_1h,
                    pt.multiple_vs_detection, pt.multiple_vs_entry,
                    pt.entry_price, pt.outcome, pt.trend,
                    pt.mc_at_72h, pt.last_updated
                FROM tokens t
                LEFT JOIN LATERAL (
                    SELECT * FROM entry_signals
                    WHERE token_id = t.id
                    ORDER BY sent_at DESC LIMIT 1
                ) es ON TRUE
                LEFT JOIN performance_tracking pt ON pt.token_id = t.id
                {where}
                ORDER BY COALESCE(pt.multiple_vs_detection, 0) DESC,
                         t.detected_at DESC
            """, params)
            return cur.fetchall()


def get_strategy_stats(strategy: str = None) -> dict:
    with conn() as c:
        with c.cursor(cursor_factory=RealDictCursor) as cur:
            where = "WHERE t.strategy = %s" if strategy else ""
            params = (strategy,) if strategy else ()
            cur.execute(f"""
                SELECT
                    COUNT(t.id)                                         AS total_detected,
                    COUNT(pt.outcome)                                   AS total_outcomes,
                    ROUND(AVG(pt.multiple_vs_detection)::numeric, 2)   AS avg_multiple,
                    COUNT(*) FILTER (WHERE pt.outcome = 'moon')        AS moon_count,
                    COUNT(*) FILTER (WHERE pt.outcome = 'up')          AS up_count,
                    COUNT(*) FILTER (WHERE pt.outcome = 'flat')        AS flat_count,
                    COUNT(*) FILTER (WHERE pt.outcome = 'down')        AS down_count,
                    COUNT(*) FILTER (WHERE pt.outcome = 'dead')        AS dead_count,
                    COUNT(es.id)                                        AS entry_signals_sent,
                    COUNT(*) FILTER (
                        WHERE pt.multiple_vs_detection >= 2
                    )                                                   AS count_2x,
                    COUNT(*) FILTER (
                        WHERE pt.multiple_vs_detection >= 5
                    )                                                   AS count_5x,
                    COUNT(*) FILTER (
                        WHERE pt.multiple_vs_detection >= 10
                    )                                                   AS count_10x
                FROM tokens t
                LEFT JOIN performance_tracking pt ON pt.token_id = t.id
                LEFT JOIN entry_signals es ON es.token_id = t.id
                {where}
            """, params)
            return dict(cur.fetchone() or {})


def get_top_performers(limit: int = 20, min_multiple: float = 2.0) -> list:
    with conn() as c:
        with c.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT t.symbol, t.name, t.strategy, t.mint, t.pair_addr,
                       t.mc_at_detection, pt.current_mc,
                       pt.multiple_vs_detection, pt.multiple_vs_entry,
                       pt.outcome, t.detected_at
                FROM tokens t
                JOIN performance_tracking pt ON pt.token_id = t.id
                WHERE pt.multiple_vs_detection >= %s
                ORDER BY pt.multiple_vs_detection DESC
                LIMIT %s
            """, (float(min_multiple), limit))
            return cur.fetchall()
