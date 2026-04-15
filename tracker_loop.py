#!/usr/bin/env python3
"""
tracker_loop.py — Background monitoring thread.

Runs every tracker_interval_seconds. For every token in the DB:
  1. Fetches current price from DexScreener
  2. Updates peak/lowest tracking
  3. Checks pullback entry conditions — fires entry signal alert
  4. Checks milestone multiples — fires milestone alert
  5. Processes 72h outcome checks
  6. Sends interim snapshots at 23:00 EAT
"""

import logging
import threading
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

from data.dexscreener import fetch_single_pair, extract_metrics, safe_float
from core import database as db
from alerts.telegram import (
    send,
    build_entry_alert,
    build_milestone_alert,
    build_outcome_alert,
)

logger = logging.getLogger(__name__)

CHECK_INTERVAL = 60   # seconds between tracker runs
INTERIM_HOUR   = 20   # UTC (= 23:00 EAT)


def _classify(mult: float) -> str:
    if mult >= 5:   return "moon"
    if mult >= 2:   return "up"
    if mult >= 0.8: return "flat"
    if mult > 0:    return "down"
    return "dead"


def _s(v, d=0.0) -> float:
    try:
        return float(v) if v is not None else d
    except (TypeError, ValueError):
        return d


# ─────────────────────────────────────────────
# PULLBACK DETECTION
# ─────────────────────────────────────────────

def _check_pullback(token: dict, metrics: dict,
                     s_cfg: dict,
                     tg_token: str, tg_chat: str):
    token_id    = token["id"]
    peak_price  = _s(token.get("peak_price") or token.get("price_at_detection"))
    if peak_price <= 0:
        return

    cur_price   = _s(metrics.get("price"))
    cur_mc      = _s(metrics.get("mc"))
    vol_5m      = _s(metrics.get("vol_5m"))
    if cur_price <= 0:
        return

    pullback_pct = ((peak_price - cur_price) / peak_price) * 100
    pb_cfg       = s_cfg.get("pullback", {})
    min_pb       = _s(pb_cfg.get("min_pct", 20))
    max_pb       = _s(pb_cfg.get("max_pct", 60))

    if pullback_pct < min_pb or pullback_pct > max_pb:
        return

    # Volume confirmation
    min_vol = _s(pb_cfg.get("min_5m_vol_usd", 0))
    if vol_5m < min_vol:
        return

    # Volume recovery ratio
    vol_recovery = _s(pb_cfg.get("min_vol_recovery_ratio", 0))
    if vol_recovery > 0:
        vol_1h_det = _s(token.get("vol_1h_at_detection"))
        vol_1h_now = _s(metrics.get("vol_1h"))
        if vol_1h_det > 0 and vol_1h_now < vol_1h_det * vol_recovery:
            return

    # Check we haven't sent a pullback alert too recently (2h cooldown)
    existing = db.get_latest_entry_signal(token_id)
    if existing:
        sent_at = existing.get("sent_at")
        if sent_at and (datetime.now(timezone.utc) - sent_at).total_seconds() < 7200:
            return

    # Fire entry signal
    strategy_label = s_cfg.get("label", "")
    msg, buttons = build_entry_alert(
        token          = token,
        current_metrics= metrics,
        pullback_pct   = pullback_pct,
        signal_type    = "pullback",
        strategy_label = strategy_label,
    )
    if send(tg_token, tg_chat, msg, buttons):
        db.insert_entry_signal(
            token_id    = token_id,
            mint        = token["mint"],
            symbol      = token["symbol"],
            strategy    = token["strategy"],
            signal_type = "pullback",
            mc          = cur_mc,
            price       = cur_price,
            pullback_pct= pullback_pct,
            vol_5m      = vol_5m,
        )
        # Set entry price in performance tracking
        db.upsert_performance(
            token_id         = token_id,
            mint             = token["mint"],
            strategy         = token["strategy"],
            current_mc       = cur_mc,
            current_price    = cur_price,
            current_liq      = metrics.get("liq_usd"),
            vol_5m           = vol_5m,
            vol_1h           = metrics.get("vol_1h"),
            vol_24h          = metrics.get("vol_24h"),
            price_change_1h  = metrics.get("pc_1h"),
            price_change_24h = metrics.get("pc_24h"),
            buy_sell_ratio_1h= metrics.get("buy_sell_ratio_1h"),
            mc_at_detection  = _s(token.get("mc_at_detection")),
            entry_price      = cur_mc,
        )
        logger.info(
            f"Entry signal [{token['strategy']}] {token['symbol']} "
            f"pullback={pullback_pct:.1f}%"
        )


# ─────────────────────────────────────────────
# MILESTONE CHECKS
# ─────────────────────────────────────────────

def _check_milestones(token: dict, metrics: dict,
                       milestones_det: list, milestones_ent: list,
                       s_cfg: dict,
                       tg_token: str, tg_chat: str):
    token_id    = token["id"]
    mc_det      = _s(token.get("mc_at_detection"))
    cur_mc      = _s(metrics.get("mc"))
    cur_price   = _s(metrics.get("price"))
    s_label     = s_cfg.get("label", "")

    if mc_det <= 0 or cur_mc <= 0:
        return

    mult_det = cur_mc / mc_det

    # vs detection
    for m in milestones_det:
        if mult_det >= m and not db.milestone_sent(token_id, "vs_detection", m):
            msg, buttons = build_milestone_alert(
                token          = token,
                milestone_type = "vs_detection",
                multiplier     = m,
                current_mc     = cur_mc,
                current_price  = cur_price,
                strategy_label = s_label,
            )
            if send(tg_token, tg_chat, msg, buttons):
                db.insert_milestone(
                    token_id = token_id,
                    mint     = token["mint"],
                    strategy = token["strategy"],
                    milestone_type = "vs_detection",
                    mult     = m,
                    mc       = cur_mc,
                )
                logger.info(f"Milestone {m}x vs detection — {token['symbol']}")

    # vs entry
    entry = db.get_latest_entry_signal(token_id)
    if entry:
        entry_mc = _s(entry.get("mc_at_signal"))
        if entry_mc > 0:
            mult_ent = cur_mc / entry_mc
            for m in milestones_ent:
                if mult_ent >= m and not db.milestone_sent(token_id, "vs_entry", m):
                    msg, buttons = build_milestone_alert(
                        token          = token,
                        milestone_type = "vs_entry",
                        multiplier     = m,
                        current_mc     = cur_mc,
                        current_price  = cur_price,
                        strategy_label = s_label,
                    )
                    if send(tg_token, tg_chat, msg, buttons):
                        db.insert_milestone(
                            token_id = token_id,
                            mint     = token["mint"],
                            strategy = token["strategy"],
                            milestone_type = "vs_entry",
                            mult     = m,
                            mc       = cur_mc,
                        )
                        logger.info(f"Milestone {m}x vs entry — {token['symbol']}")


# ─────────────────────────────────────────────
# 72H OUTCOME PROCESSING
# ─────────────────────────────────────────────

def _process_outcomes(tg_token: str, tg_chat: str,
                       cfg: dict, strategy_cfgs: dict):
    due = db.get_pending_72h()
    if not due:
        return

    logger.info(f"Processing {len(due)} 72h outcomes")
    for token in due:
        pair = fetch_single_pair(
            token.get("chain", "solana"),
            token["pair_addr"]
        )
        time.sleep(0.5)

        mc_now  = _s((pair or {}).get("marketCap")) if pair else 0
        mc_det  = _s(token.get("mc_at_detection"))
        mult_det = (mc_now / mc_det) if mc_det > 0 and mc_now > 0 else 0
        outcome  = _classify(mult_det) if mult_det > 0 else "dead"

        entry   = db.get_latest_entry_signal(token["id"])
        entry_mc  = _s((entry or {}).get("mc_at_signal"))
        mult_ent  = (mc_now / entry_mc) if entry_mc > 0 and mc_now > 0 else None

        db.set_outcome(token["id"], mc_now if mc_now > 0 else None, outcome)
        db.mark_outcome_checked(token["id"])

        s_key   = token.get("strategy", "A")
        s_cfg   = strategy_cfgs.get(s_key, {})
        s_label = s_cfg.get("label", f"Strategy {s_key}")

        msg, buttons = build_outcome_alert(
            token      = token,
            mc_72h     = mc_now,
            mult_det   = mult_det,
            mult_entry = mult_ent,
            outcome    = outcome,
            strategy_label = s_label,
        )
        send(tg_token, tg_chat, msg, buttons)
        logger.info(
            f"72h [{s_key}] {token['symbol']} → "
            f"{mult_det:.2f}x ({outcome})"
        )


# ─────────────────────────────────────────────
# INTERIM SNAPSHOT
# ─────────────────────────────────────────────

def _send_interim(tg_token: str, tg_chat: str):
    pending = db.get_tokens_watching()
    if not pending:
        return

    now   = datetime.now(timezone.utc)
    lines = [f"🔭 <b>Interim — {len(pending)} token(s) tracked</b>\n"]

    for t in pending:
        pair = fetch_single_pair(t.get("chain", "solana"), t["pair_addr"])
        time.sleep(0.3)

        mc_det = _s(t.get("mc_at_detection"))
        if pair:
            mc_now = _s(pair.get("marketCap"))
            mult   = (mc_now / mc_det) if mc_det > 0 and mc_now > 0 else 0
            trend  = ("🚀" if mult >= 5 else "📈" if mult >= 2
                      else "➡️" if mult >= 0.8 else "📉")
            mc_str = f"${mc_now:,.0f}"
        else:
            mult   = 0
            trend  = "💀"
            mc_str = "N/A"

        due     = t.get("check_due_at")
        due_str = f"{max(0, (due - now).total_seconds() / 3600):.0f}h" if due else "?"
        dex_url = f"https://dexscreener.com/{t.get('chain','solana')}/{t['pair_addr']}"

        lines.append(
            f"{trend} <a href=\"{dex_url}\"><b>{t['name']} ({t['symbol']})</b></a>"
            f" [{t['strategy']}]\n"
            f"   ${mc_det:,.0f} → {mc_str}"
            + (f" <b>({mult:.2f}x)</b>" if mult > 0 else "")
            + f" | 72h in: {due_str}"
        )

    send(tg_token, tg_chat, "\n\n".join(lines))
    logger.info(f"Interim snapshot sent — {len(pending)} tokens")


# ─────────────────────────────────────────────
# BACKGROUND THREAD
# ─────────────────────────────────────────────

def start(tg_token: str, tg_chat: str, cfg: dict):
    import core.config as config_loader

    g_cfg      = cfg.get("global", {})
    interval   = int(g_cfg.get("tracker_interval_seconds", CHECK_INTERVAL))
    interim_on = g_cfg.get("alerts", {}).get("interim_report_frequency", "daily") == "daily"

    milestones_det = g_cfg.get("milestones", {}).get("vs_detection", [2, 5, 10, 20])
    milestones_ent = g_cfg.get("milestones", {}).get("vs_entry", [2, 3, 5, 10])

    # Build strategy config lookup {key: cfg}
    strategy_cfgs = {}
    for key in ["strategy_a", "strategy_b", "strategy_c"]:
        s_cfg = config_loader.get_strategy(cfg, key)
        strategy_cfgs[key[-1].upper()] = s_cfg

    last_interim_key = None

    def _loop():
        nonlocal last_interim_key
        logger.info(f"Tracker loop started | interval: {interval}s")

        while True:
            try:
                now     = datetime.now(timezone.utc)
                tokens  = db.get_tokens_watching()

                for token in tokens:
                    pair = fetch_single_pair(
                        token.get("chain", "solana"),
                        token["pair_addr"]
                    )
                    if not pair:
                        time.sleep(0.3)
                        continue

                    metrics = extract_metrics(pair)
                    cur_price = _s(metrics.get("price"))
                    cur_mc    = _s(metrics.get("mc"))

                    if cur_price > 0 and cur_mc > 0:
                        # Update peak tracking
                        db.update_peak(token["id"], cur_price, cur_mc)

                        # Refresh token with updated peaks
                        fresh_tokens = db.get_tokens_watching()
                        fresh = next(
                            (t for t in fresh_tokens if t["id"] == token["id"]),
                            token
                        )

                        # Get strategy config
                        s_key = token.get("strategy", "A")
                        s_cfg = strategy_cfgs.get(s_key, {})

                        # Pullback check
                        max_h = _s(s_cfg.get("pullback", {}).get("watch_window_hours", 48))
                        hours_since = (now - token["detected_at"]).total_seconds() / 3600
                        if hours_since <= max_h:
                            _check_pullback(fresh, metrics, s_cfg, tg_token, tg_chat)

                        # Milestone check
                        _check_milestones(
                            fresh, metrics,
                            milestones_det, milestones_ent,
                            s_cfg, tg_token, tg_chat
                        )

                        # Update live metrics
                        entry = db.get_latest_entry_signal(token["id"])
                        entry_mc = _s((entry or {}).get("mc_at_signal"))
                        db.upsert_performance(
                            token_id         = token["id"],
                            mint             = token["mint"],
                            strategy         = token["strategy"],
                            current_mc       = cur_mc,
                            current_price    = cur_price,
                            current_liq      = metrics.get("liq_usd"),
                            vol_5m           = metrics.get("vol_5m"),
                            vol_1h           = metrics.get("vol_1h"),
                            vol_24h          = metrics.get("vol_24h"),
                            price_change_1h  = metrics.get("pc_1h"),
                            price_change_24h = metrics.get("pc_24h"),
                            buy_sell_ratio_1h= metrics.get("buy_sell_ratio_1h"),
                            mc_at_detection  = _s(token.get("mc_at_detection")),
                            entry_price      = entry_mc if entry_mc > 0 else None,
                        )

                    time.sleep(0.5)

                # 72h outcome checks
                _process_outcomes(tg_token, tg_chat, cfg, strategy_cfgs)

                # Interim report
                if interim_on:
                    d_h_key = (now.date(), now.hour)
                    if now.hour == INTERIM_HOUR and d_h_key != last_interim_key:
                        last_interim_key = d_h_key
                        _send_interim(tg_token, tg_chat)

            except Exception as e:
                logger.error(f"Tracker loop error: {e}", exc_info=True)

            time.sleep(interval)

    t = threading.Thread(target=_loop, daemon=True, name="tracker-loop")
    t.start()
    logger.info("Tracker loop thread running")
