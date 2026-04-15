#!/usr/bin/env python3
"""
main.py — PJM Scanner orchestration loop.

Flow:
  1. Load config (with version tracking)
  2. Init DB
  3. Start background threads (tracker, wallet tracker, health server)
  4. Run discovery + filter + score + alert loop every N seconds
"""

import os
import sys
import time
import logging
import threading
from datetime import datetime, timezone, timedelta

import core.config   as config_loader
import core.database as db
from core.circuit_breaker import status_all as cb_status

from data.dexscreener import discover_candidates, extract_metrics
from data import rugcheck

from strategies.strategy_a import StrategyA
from strategies.strategy_b import StrategyB
from strategies.strategy_c import StrategyC

from scoring.scorer import compute as compute_score, tier as score_tier

from alerts.telegram import (
    send, build_detection_alert, build_startup, build_empty_summary,
    build_summary,
)

import tracker_loop
import wallet_tracker.tracker as wallet_tracker

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# LOGGING SETUP
# ─────────────────────────────────────────────

def setup_logging(cfg: dict):
    log_cfg  = cfg.get("global", {}).get("logging", {})
    level    = getattr(logging, log_cfg.get("level", "INFO").upper(), logging.INFO)
    handlers = [logging.StreamHandler(sys.stdout)]
    if log_cfg.get("log_to_file"):
        import pathlib
        log_file = log_cfg.get("log_file", "logs/pjmscanner.log")
        pathlib.Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file))
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        handlers=handlers,
    )


# ─────────────────────────────────────────────
# HEALTH SERVER
# ─────────────────────────────────────────────

def start_health_server(cfg: dict):
    port = cfg.get("global", {}).get("health", {}).get("port", 9090)
    from http.server import BaseHTTPRequestHandler, HTTPServer
    import json

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({
                "status": "ok",
                "time":   datetime.now(timezone.utc).isoformat(),
                "circuits": cb_status(),
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *_): pass  # silence access logs

    def _run():
        HTTPServer(("0.0.0.0", port), Handler).serve_forever()

    t = threading.Thread(target=_run, daemon=True, name="health-server")
    t.start()
    logger.info(f"Health server on :{port}")


# ─────────────────────────────────────────────
# CYCLE HELPER
# ─────────────────────────────────────────────

def cycle_start_for(dt: datetime) -> datetime:
    epoch     = datetime(2000, 1, 1, tzinfo=timezone.utc)
    days      = (dt - epoch).days
    cycle_day = (days // 3) * 3
    return epoch + timedelta(days=cycle_day)


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def main():
    cfg = config_loader.load_all()
    setup_logging(cfg)

    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    tg_chat  = os.environ.get("TELEGRAM_CHANNEL_ID")

    if not tg_token or not tg_chat:
        logger.error("TELEGRAM_BOT_TOKEN and TELEGRAM_CHANNEL_ID must be set.")
        sys.exit(1)

    db.wait_for_db()
    db.init_schema()

    g_cfg          = cfg.get("global", {})
    scan_interval  = int(g_cfg.get("scan_interval_seconds", 120))
    chains         = [n for n, v in g_cfg.get("networks", {}).items()
                      if v.get("enabled")]
    max_per_scan   = int(g_cfg.get("alerts", {}).get("max_per_scan", 10))
    min_score      = float(g_cfg.get("scoring", {}).get("min_score_to_alert", 60))
    show_breakdown = g_cfg.get("alerts", {}).get("show_filter_failure_breakdown", True)
    SUMMARY_HOURS  = set(g_cfg.get("alerts", {}).get("summary_hours_utc", [10, 20]))
    MILESTONES_DET = g_cfg.get("milestones", {}).get("vs_detection", [2, 5, 10, 20])
    MILESTONES_ENT = g_cfg.get("milestones", {}).get("vs_entry", [2, 3, 5, 10])

    # Build strategy instances
    strategies = []
    for StratClass, key in [
        (StrategyA, "strategy_a"),
        (StrategyB, "strategy_b"),
        (StrategyC, "strategy_c"),
    ]:
        s_cfg = config_loader.get_strategy(cfg, key)
        if s_cfg.get("enabled", True):
            strategies.append(StratClass(s_cfg))
            logger.info(f"Strategy loaded: {StratClass.key} [{s_cfg.get('label')}]")

    if not strategies:
        logger.error("No strategies enabled. Exiting.")
        sys.exit(1)

    strategies_on = [s.label for s in strategies]

    # State
    seen_pairs     = set()
    failure_tally  = {}
    alerts_sent    = 0
    by_strategy    = {}
    last_summary   = None

    logger.info(
        f"PJM Scanner started | Strategies: {strategies_on} | "
        f"Interval: {scan_interval}s | Chains: {chains}"
    )

    send(tg_token, tg_chat, build_startup(strategies_on, scan_interval))

    # Start background threads
    start_health_server(cfg)
    tracker_loop.start(tg_token, tg_chat, cfg)
    wallet_tracker.start(tg_token, tg_chat, cfg)

    crash_count = 0

    while True:
        try:
            logger.info("── Scan starting ──")
            scan_start = time.time()

            # Discover candidates
            all_pairs = discover_candidates(chains)
            candidates = []  # (score, metrics, rc, scores_dict, warnings, strategy)

            for pair in all_pairs:
                pair_addr = pair.get("pairAddress", "")
                if not pair_addr or pair_addr in seen_pairs:
                    continue

                m    = extract_metrics(pair)
                mint = m["mint"]
                if not mint:
                    continue

                rc_raw = rugcheck.fetch(mint)
                rc     = rugcheck.parse(rc_raw)

                for strategy in strategies:
                    # Skip if already detected for this strategy
                    if db.mint_strategy_exists(mint, strategy.key[-1].upper()):
                        continue

                    passed, failures, warnings = strategy.filter(pair, rc)

                    if not passed:
                        if show_breakdown:
                            for f in failures:
                                key = f.split("(")[0].strip()
                                failure_tally[key] = failure_tally.get(key, 0) + 1
                        continue

                    # Score
                    s_key = strategy.key[-1].upper()
                    scores = compute_score(m, rc, strategy.cfg)

                    if scores["composite"] < min_score:
                        key = f"Score < {min_score:.0f}"
                        if show_breakdown:
                            failure_tally[key] = failure_tally.get(key, 0) + 1
                        continue

                    candidates.append(
                        (scores["composite"], m, rc, scores, warnings, strategy)
                    )
                    logger.info(
                        f"PASS [{strategy.key[-1].upper()}] {mint[:8]} "
                        f"score={scores['composite']:.1f} sym={m['symbol']}"
                    )

            # Sort by score, cap
            candidates.sort(key=lambda x: x[0], reverse=True)
            if max_per_scan > 0:
                candidates = candidates[:max_per_scan]

            for composite, m, rc, scores, warnings, strategy in candidates:
                pair_addr   = m["pair_addr"]
                mint        = m["mint"]
                symbol      = m["symbol"]
                name        = m["name"]
                s_key       = strategy.key[-1].upper()
                s_label     = strategy.label

                seen_pairs.add(pair_addr)
                alerts_sent += 1
                by_strategy[s_label] = by_strategy.get(s_label, 0) + 1

                msg, buttons = build_detection_alert(m, rc, scores, warnings, s_label)
                ok = send(tg_token, tg_chat, msg, buttons)

                if ok:
                    now     = datetime.now(timezone.utc)
                    token_id = db.insert_token(
                        mint            = mint,
                        symbol          = symbol,
                        name            = name,
                        pair_addr       = pair_addr,
                        chain           = m["chain"],
                        dex             = m["dex"],
                        strategy        = s_key,
                        score           = composite,
                        mc              = m["mc"],
                        price           = m["price"],
                        liq             = m["liq_usd"],
                        vol_1h          = m["vol_1h"],
                        vol_24h         = m["vol_24h"],
                        holders         = rc.get("total_holders"),
                        buy_sell_ratio  = m["buy_sell_ratio_1h"],
                        lp_locked_pct   = rc.get("lp_locked_pct"),
                        rugcheck_score  = rc.get("score"),
                        mint_renounced  = rc.get("mint_renounced"),
                        freeze_renounced= rc.get("freeze_renounced"),
                        deployer        = rc.get("deployer"),
                        check_due_at    = now + timedelta(hours=72),
                    )
                    if token_id:
                        db.mark_alerted(token_id)
                        logger.info(f"✅ [{s_key}] {symbol} score={composite:.1f} id={token_id}")
                else:
                    logger.error(f"❌ Alert failed: {symbol}")

                time.sleep(1)

            elapsed = time.time() - scan_start
            logger.info(
                f"── Scan done in {elapsed:.1f}s | "
                f"Pairs: {len(all_pairs)} | Alerts: {len(candidates)} ──"
            )

            # Summaries at 13:00 and 23:00 EAT
            now_utc  = datetime.now(timezone.utc)
            d_h_key  = (now_utc.date(), now_utc.hour)
            if now_utc.hour in SUMMARY_HOURS and d_h_key != last_summary:
                last_summary = d_h_key
                eat_h = (now_utc.hour + 3) % 24
                if alerts_sent == 0:
                    send(tg_token, tg_chat,
                         build_empty_summary(failure_tally, eat_h))
                else:
                    send(tg_token, tg_chat,
                         build_summary(alerts_sent, eat_h, scan_interval, by_strategy))
                alerts_sent   = 0
                by_strategy   = {}
                failure_tally = {}

            crash_count = 0  # reset on successful scan

        except KeyboardInterrupt:
            logger.info("Stopped.")
            break
        except Exception as e:
            crash_count += 1
            logger.error(f"Scan error (crash #{crash_count}): {e}", exc_info=True)
            config_loader.record_crash(cfg)

        sleep_for = max(0, scan_interval - (time.time() - scan_start
                        if 'scan_start' in dir() else scan_interval))
        logger.info(f"Sleeping {sleep_for:.0f}s…")
        time.sleep(sleep_for)


if __name__ == "__main__":
    main()
