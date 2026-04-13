#!/usr/bin/env python3
"""
screener.py — Main entry point.
Scans every N minutes, runs both strategies, fires Telegram alerts.
"""

import os
import sys
import time
import logging
from datetime import datetime, timezone, timedelta

import requests
import yaml

import db
import tracker
from filters import run_filters
from alerts import (
    send_telegram,
    build_initial_alert,
    build_startup_message,
    build_scan_summary,
    build_empty_scan_summary,
)

HEADERS = {"User-Agent": "MemecoinScreener/1.0"}

GECKO_NEW   = "https://api.geckoterminal.com/api/v2/networks/{net}/new_pools"
GECKO_TREND = "https://api.geckoterminal.com/api/v2/networks/{net}/trending_pools"
DEX_TOKEN   = "https://api.dexscreener.com/latest/dex/tokens/{mint}"
RUGCHECK    = "https://api.rugcheck.xyz/v1/tokens/{mint}/report"

CHAIN_TO_GECKO = {
    "solana": "solana", "ethereum": "eth", "bsc": "bsc",
    "base": "base", "arbitrum": "arbitrum",
}


# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

def load_config(path: str = "config.yaml") -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def setup_logging(cfg: dict):
    log_cfg  = cfg.get("global", {}).get("logging", {})
    level    = getattr(logging, log_cfg.get("level", "INFO").upper(), logging.INFO)
    handlers = [logging.StreamHandler(sys.stdout)]
    if log_cfg.get("log_to_file"):
        log_file = log_cfg.get("log_file", "logs/screener.log")
        os.makedirs(os.path.dirname(log_file), exist_ok=True)
        handlers.append(logging.FileHandler(log_file))
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=handlers,
    )


# ─────────────────────────────────────────────
# DATA FETCHERS
# ─────────────────────────────────────────────

def _gecko_mints(url: str) -> list:
    try:
        r = requests.get(url, headers=HEADERS, timeout=15, params={"page": 1})
        r.raise_for_status()
        mints = []
        for pool in r.json().get("data", []):
            rels = pool.get("relationships", {})
            tid  = (rels.get("base_token", {}).get("data", {}) or {}).get("id", "")
            if "_" in tid:
                mints.append(tid.split("_", 1)[1])
        return mints
    except Exception as e:
        logging.warning(f"Gecko fetch failed: {e}")
        return []


def _dex_pairs(mints: list, seen: set, chain: str) -> list:
    pairs = []
    for i in range(0, len(mints), 30):
        batch = mints[i:i+30]
        try:
            r = requests.get(
                DEX_TOKEN.format(mint=",".join(batch)),
                headers=HEADERS, timeout=15
            )
            r.raise_for_status()
            for p in r.json().get("pairs") or []:
                addr = p.get("pairAddress", "")
                if addr and addr not in seen:
                    if (p.get("chainId") or "").lower() == chain.lower():
                        liq = (p.get("liquidity") or {}).get("usd") or 0
                        if liq > 0:
                            seen.add(addr)
                            pairs.append(p)
            time.sleep(0.3)
        except Exception as e:
            logging.warning(f"DexScreener batch failed: {e}")
    return pairs


def fetch_pairs(chains: list) -> list:
    all_pairs  = []
    seen_pairs = set()
    seen_mints = set()

    for chain in chains:
        network = CHAIN_TO_GECKO.get(chain, chain)
        for url in [
            GECKO_NEW.format(net=network),
            GECKO_TREND.format(net=network),
        ]:
            mints = _gecko_mints(url)
            new_mints = [m for m in mints if m not in seen_mints]
            seen_mints.update(new_mints)
            if new_mints:
                pairs = _dex_pairs(new_mints, seen_pairs, chain)
                all_pairs.extend(pairs)
            time.sleep(0.3)

    logging.info(f"Discovered {len(all_pairs)} candidate pairs")
    return all_pairs


def fetch_rugcheck(mint: str) -> dict:
    try:
        r = requests.get(
            RUGCHECK.format(mint=mint),
            headers=HEADERS, timeout=15
        )
        r.raise_for_status()
        return r.json()
    except Exception:
        return {}


def parse_rugcheck(data: dict) -> dict:
    if not data:
        return {}
    risks  = data.get("risks") or []
    score  = data.get("score") or 0
    fields = data.get("tokenMeta") or {}
    top10  = 0
    max_w  = 0
    blk0   = 0
    holders = len(data.get("topHolders") or [])

    for h in (data.get("topHolders") or [])[:10]:
        top10 += float(h.get("pct") or 0)
    for h in (data.get("topHolders") or []):
        pct = float(h.get("pct") or 0)
        max_w = max(max_w, pct)
    for h in (data.get("topHolders") or []):
        if h.get("isBlock0"):
            blk0 += float(h.get("pct") or 0)

    return {
        "score":              float(score),
        "mint_renounced":     data.get("mintAuthority") is None,
        "freeze_renounced":   data.get("freezeAuthority") is None,
        "mutable_metadata":   fields.get("mutable", False),
        "total_holders":      holders,
        "top10_pct":          round(top10, 2),
        "max_single_wallet_pct": round(max_w, 2),
        "block0_snipe_pct":   round(blk0, 2),
        "risks":              [r.get("name", "") for r in risks],
    }


# ─────────────────────────────────────────────
# CYCLE HELPER
# ─────────────────────────────────────────────

def cycle_start_for(dt: datetime) -> datetime:
    from datetime import timezone
    epoch     = datetime(2000, 1, 1, tzinfo=timezone.utc)
    days      = (dt - epoch).days
    cycle_day = (days // 3) * 3
    return epoch + timedelta(days=cycle_day)


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def main():
    cfg = load_config()
    setup_logging(cfg)

    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    tg_chat  = os.environ.get("TELEGRAM_CHANNEL_ID")

    if not tg_token or not tg_chat:
        logging.error("TELEGRAM_BOT_TOKEN and TELEGRAM_CHANNEL_ID must be set.")
        sys.exit(1)

    db.wait_for_db()
    db.init_schema()

    g_cfg          = cfg.get("global", {})
    scan_interval  = g_cfg.get("scan_interval_minutes", 15) * 60
    chains         = [c.lower() for c in g_cfg.get("chains", ["solana"])]
    max_per_scan   = g_cfg.get("max_alerts_per_scan", 5)
    show_breakdown = g_cfg.get("show_filter_failure_breakdown", True)
    SUMMARY_HOURS  = set(g_cfg.get("summary_hours_utc", [10, 20]))

    cfg_a = cfg.get("strategy_a", {})
    cfg_b = cfg.get("strategy_b", {})
    a_on  = cfg_a.get("enabled", True)
    b_on  = cfg_b.get("enabled", True)

    strategies_on = []
    if a_on: strategies_on.append(cfg_a.get("label", "⚡ FAST"))
    if b_on: strategies_on.append(cfg_b.get("label", "🎯 SWING"))

    seen_pairs     = set()
    failure_tally  = {}
    alerts_sent    = 0
    by_strategy    = {}
    last_summary   = None

    logging.info(
        f"Screener started | Strategies: {strategies_on} | "
        f"Interval: {scan_interval//60}min"
    )
    send_telegram(
        tg_token, tg_chat,
        build_startup_message(strategies_on, scan_interval // 60)
    )

    tracker.start_background_checker(tg_token, tg_chat, cfg)

    while True:
        try:
            logging.info("── Starting scan ──")
            all_pairs  = fetch_pairs(chains)
            candidates = []   # (score, pair, rc_data, scores, strategy, s_cfg)

            for pair in all_pairs:
                pair_addr = pair.get("pairAddress", "")
                if not pair_addr or pair_addr in seen_pairs:
                    continue

                mint    = (pair.get("baseToken") or {}).get("address", "")
                if not mint:
                    continue

                rc_raw  = fetch_rugcheck(mint)
                rc_data = parse_rugcheck(rc_raw)

                # Run both strategies
                for strategy_key, s_cfg, s_label in [
                    ("A", cfg_a, cfg_a.get("label", "⚡ FAST")),
                    ("B", cfg_b, cfg_b.get("label", "🎯 SWING")),
                ]:
                    if not s_cfg.get("enabled", True):
                        continue

                    # Skip if already alerted for this mint+strategy
                    if db.mint_strategy_exists(mint, strategy_key):
                        continue

                    passed, failures, warnings, scores = run_filters(
                        pair, rc_data, s_cfg
                    )

                    if not passed:
                        if show_breakdown:
                            for f in failures:
                                key = f.split("(")[0].strip()
                                failure_tally[key] = failure_tally.get(key, 0) + 1
                        continue

                    composite = scores.get("composite", 1.0)
                    candidates.append(
                        (composite, pair, rc_data, scores, warnings,
                         strategy_key, s_cfg)
                    )
                    logging.info(
                        f"PASS [{strategy_key}] {mint[:8]} "
                        f"score={composite:.3f} "
                        f"sym={(pair.get('baseToken') or {}).get('symbol')}"
                    )

            # Sort by score, cap per scan
            candidates.sort(key=lambda x: x[0], reverse=True)
            if max_per_scan > 0:
                candidates = candidates[:max_per_scan]

            for composite, pair, rc_data, scores, warnings, strategy_key, s_cfg in candidates:
                pair_addr = pair.get("pairAddress", "")
                mint      = (pair.get("baseToken") or {}).get("address", "")
                symbol    = (pair.get("baseToken") or {}).get("symbol", "???")
                name      = (pair.get("baseToken") or {}).get("name", "Unknown")
                mc_now    = float(pair.get("marketCap") or 0)
                price_now = float(pair.get("priceUsd") or 0)
                s_label   = s_cfg.get("label", f"Strategy {strategy_key}")

                seen_pairs.add(pair_addr)

                msg, buttons = build_initial_alert(
                    pair           = pair,
                    rc_data        = rc_data,
                    scores         = scores,
                    warnings       = warnings,
                    strategy_label = s_label,
                )
                ok = send_telegram(tg_token, tg_chat, msg, buttons)

                if ok:
                    alerts_sent += 1
                    by_strategy[s_label] = by_strategy.get(s_label, 0) + 1
                    logging.info(
                        f"✅ ALERT [{strategy_key}] {symbol} "
                        f"score={composite:.3f}"
                    )

                    if mc_now > 0 and price_now > 0:
                        now     = datetime.now(timezone.utc)
                        cycle_s = cycle_start_for(now)
                        cycle_e = cycle_s + timedelta(days=3)
                        db.get_or_create_cycle(cycle_s, cycle_e)
                        db.insert_alert(
                            mint           = mint,
                            symbol         = symbol,
                            name           = name,
                            pair_addr      = pair_addr,
                            strategy       = strategy_key,
                            mc_at_alert    = mc_now,
                            price_at_alert = price_now,
                            alerted_at     = now,
                            check_due_at   = now + timedelta(hours=72),
                            cycle_start    = cycle_s,
                        )
                else:
                    logging.error(f"❌ Alert failed: {symbol}")

                time.sleep(1)

            logging.info(f"── Scan done. Alerts: {len(candidates)} ──")

            # Scheduled summaries
            now_utc  = datetime.now(timezone.utc)
            hour_utc = now_utc.hour
            d_h_key  = (now_utc.date(), hour_utc)

            if hour_utc in SUMMARY_HOURS and d_h_key != last_summary:
                last_summary = d_h_key
                eat_h = (hour_utc + 3) % 24
                if alerts_sent == 0:
                    send_telegram(
                        tg_token, tg_chat,
                        build_empty_scan_summary(failure_tally, eat_h)
                    )
                else:
                    send_telegram(
                        tg_token, tg_chat,
                        build_scan_summary(
                            alerts_sent, eat_h,
                            scan_interval // 60, by_strategy
                        )
                    )
                alerts_sent   = 0
                by_strategy   = {}
                failure_tally = {}

        except KeyboardInterrupt:
            logging.info("Screener stopped.")
            break
        except Exception as e:
            logging.error(f"Scan error: {e}", exc_info=True)

        logging.info(f"Sleeping {scan_interval//60}min…")
        time.sleep(scan_interval)


if __name__ == "__main__":
    main()
