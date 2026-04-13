#!/usr/bin/env python3
"""
tracker.py — Background monitoring thread.
Runs every 5 minutes. Does three things:
  1. Updates live prices and peak/lowest tracking
  2. Checks for pullback entry conditions
  3. Fires milestone alerts (2x, 5x, 10x)
  4. Processes 72h outcome checks
  5. Sends interim reports at 23:00 EAT
"""

import logging
import time
import threading
from datetime import datetime, timezone, timedelta
from typing import Optional

import requests
import db
from alerts import (
    send_telegram,
    build_pullback_alert,
    build_milestone_alert,
    build_outcome_alert,
)

HEADERS        = {"User-Agent": "MemecoinScreener/1.0"}
CHECK_INTERVAL = 300   # 5 minutes


# ─────────────────────────────────────────────
# DATA FETCHER
# ─────────────────────────────────────────────

def fetch_pair_data(pair_addr: str) -> Optional[dict]:
    """Fetch current pair data from DexScreener."""
    try:
        url = f"https://api.dexscreener.com/latest/dex/pairs/solana/{pair_addr}"
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        pairs = r.json().get("pairs") or []
        return pairs[0] if pairs else None
    except Exception as e:
        logging.warning(f"Tracker: fetch failed for {pair_addr[:8]}: {e}")
        return None


def _classify(multiplier: float) -> str:
    if multiplier >= 5:   return "moon"
    if multiplier >= 2:   return "up"
    if multiplier >= 0.8: return "flat"
    if multiplier > 0:    return "down"
    return "dead"


# ─────────────────────────────────────────────
# PULLBACK DETECTION
# ─────────────────────────────────────────────

def _check_pullback(alert: dict, current_pair: dict,
                    strategy_cfg: dict,
                    tg_token: str, tg_chat: str):
    """
    Check if a coin has pulled back to a good entry zone.
    Conditions:
      - Price has dropped pullback_min_pct to pullback_max_pct from peak
      - 5m volume above threshold (shows activity at the dip)
      - Vol at pullback >= vol_recovery_ratio * (some baseline)
    """
    alert_id   = alert["id"]
    peak_price = float(alert.get("peak_price") or alert.get("price_at_alert") or 0)
    if peak_price <= 0:
        return

    current_price = float(current_pair.get("priceUsd") or 0)
    if current_price <= 0:
        return

    # Has already had a pullback alert sent? Don't spam
    existing = db.get_latest_pullback(alert_id)
    if existing:
        # Don't resend within 2 hours
        sent_at = existing.get("sent_at")
        if sent_at and (datetime.now(timezone.utc) - sent_at).total_seconds() < 7200:
            return

    pullback_pct = ((peak_price - current_price) / peak_price) * 100
    pb_cfg       = strategy_cfg.get("pullback", {})
    min_pb       = pb_cfg.get("min_pullback_pct", 15)
    max_pb       = pb_cfg.get("max_pullback_pct", 60)

    if pullback_pct < min_pb or pullback_pct > max_pb:
        return

    # Check volume recovery
    vol_5m         = float((current_pair.get("volume") or {}).get("m5") or 0)
    min_vol        = pb_cfg.get("min_5m_vol_after_pullback", 0)
    if vol_5m < min_vol:
        return

    # All conditions met — fire pullback alert
    strategy_label = strategy_cfg.get("label", "")
    current_mc = current_pair.get("marketCap") or 0

    msg, buttons = build_pullback_alert(
        alert          = alert,
        current_pair   = current_pair,
        pullback_pct   = pullback_pct,
        strategy_label = strategy_label,
    )
    sent = send_telegram(tg_token, tg_chat, msg, buttons)

    if sent:
        db.insert_pullback_alert(
            alert_id          = alert_id,
            mint              = alert["mint"],
            symbol            = alert["symbol"],
            strategy          = alert["strategy"],
            mc_at_pullback    = float(current_mc),
            price_at_pullback = current_price,
            pullback_pct      = pullback_pct,
            vol_5m            = vol_5m,
        )
        logging.info(
            f"Tracker: pullback alert sent {alert['symbol']} "
            f"({pullback_pct:.1f}% from peak)"
        )


# ─────────────────────────────────────────────
# MILESTONE CHECKS
# ─────────────────────────────────────────────

def _check_milestones(alert: dict, current_pair: dict,
                       strategy_cfg: dict,
                       tg_token: str, tg_chat: str):
    """Check and fire 2x, 5x, 10x milestones."""
    alert_id    = alert["id"]
    mc_at_alert = float(alert.get("mc_at_alert") or 0)
    current_mc  = float(current_pair.get("marketCap") or 0)
    current_price = float(current_pair.get("priceUsd") or 0)
    strategy_label = strategy_cfg.get("label", "")

    if mc_at_alert <= 0 or current_mc <= 0:
        return

    # vs alert MC
    mult_vs_alert = current_mc / mc_at_alert
    for m in strategy_cfg.get("milestones", {}).get("vs_alert_mc", [2, 5, 10]):
        if mult_vs_alert >= m and not db.milestone_sent(alert_id, "vs_alert", m):
            msg, buttons = build_milestone_alert(
                alert          = alert,
                milestone_type = "vs_alert",
                multiplier     = m,
                current_mc     = current_mc,
                current_price  = current_price,
                strategy_label = strategy_label,
            )
            if send_telegram(tg_token, tg_chat, msg, buttons):
                db.insert_milestone(
                    alert_id           = alert_id,
                    mint               = alert["mint"],
                    strategy           = alert["strategy"],
                    milestone_type     = "vs_alert",
                    multiplier         = m,
                    mc_at_milestone    = current_mc,
                    price_at_milestone = current_price,
                )
                logging.info(
                    f"Tracker: milestone {m}x vs alert — {alert['symbol']}"
                )

    # vs entry price (pullback entry)
    pullback = db.get_latest_pullback(alert_id)
    if pullback:
        entry_mc = float(pullback.get("mc_at_pullback") or 0)
        if entry_mc > 0:
            mult_vs_entry = current_mc / entry_mc
            for m in strategy_cfg.get("milestones", {}).get("vs_entry", [2, 3, 5]):
                if mult_vs_entry >= m and not db.milestone_sent(
                    alert_id, "vs_entry", m
                ):
                    msg, buttons = build_milestone_alert(
                        alert          = alert,
                        milestone_type = "vs_entry",
                        multiplier     = m,
                        current_mc     = current_mc,
                        current_price  = current_price,
                        strategy_label = strategy_label,
                    )
                    if send_telegram(tg_token, tg_chat, msg, buttons):
                        db.insert_milestone(
                            alert_id           = alert_id,
                            mint               = alert["mint"],
                            strategy           = alert["strategy"],
                            milestone_type     = "vs_entry",
                            multiplier         = m,
                            mc_at_milestone    = current_mc,
                            price_at_milestone = current_price,
                        )
                        logging.info(
                            f"Tracker: milestone {m}x vs entry — {alert['symbol']}"
                        )


# ─────────────────────────────────────────────
# 72H OUTCOME CHECKS
# ─────────────────────────────────────────────

def _process_72h_outcomes(tg_token: str, tg_chat: str, cfg: dict):
    """Process all alerts whose 72h window has elapsed."""
    due = db.get_pending_72h_checks()
    if not due:
        return

    logging.info(f"Tracker: {len(due)} entries due for 72h check")

    # Get strategy configs
    strategy_a_cfg = cfg.get("strategy_a", {})
    strategy_b_cfg = cfg.get("strategy_b", {})

    for alert in due:
        pair = fetch_pair_data(alert["pair_addr"])
        time.sleep(1)

        mc_now = float((pair.get("marketCap") or 0)) if pair else 0
        mc_at_alert = float(alert.get("mc_at_alert") or 0)

        mult_vs_alert = (mc_now / mc_at_alert) if mc_at_alert > 0 and mc_now > 0 else 0
        outcome = _classify(mult_vs_alert) if mult_vs_alert > 0 else "dead"

        # vs entry
        pullback = db.get_latest_pullback(alert["id"])
        mult_vs_entry = None
        if pullback:
            entry_mc = float(pullback.get("mc_at_pullback") or 0)
            if entry_mc > 0 and mc_now > 0:
                mult_vs_entry = mc_now / entry_mc

        db.insert_outcome(
            alert_id      = alert["id"],
            mint          = alert["mint"],
            strategy      = alert["strategy"],
            mc_at_72h     = mc_now if mc_now > 0 else None,
            mult_vs_alert = round(mult_vs_alert, 3) if mult_vs_alert else None,
            mult_vs_entry = round(mult_vs_entry, 3) if mult_vs_entry else None,
            outcome       = outcome,
        )
        db.stop_watching_pullback(alert["id"])

        # Get strategy label
        s = alert.get("strategy", "A")
        s_cfg = strategy_a_cfg if s == "A" else strategy_b_cfg
        strategy_label = s_cfg.get("label", f"Strategy {s}")

        msg, buttons = build_outcome_alert(
            alert          = alert,
            mc_at_72h      = mc_now,
            mult_vs_alert  = mult_vs_alert,
            mult_vs_entry  = mult_vs_entry,
            outcome        = outcome,
            strategy_label = strategy_label,
        )
        send_telegram(tg_token, tg_chat, msg, buttons)
        logging.info(
            f"Tracker: 72h result {alert['symbol']} "
            f"→ {mult_vs_alert:.2f}x ({outcome})"
        )


# ─────────────────────────────────────────────
# INTERIM REPORT
# ─────────────────────────────────────────────

def _send_interim_report(tg_token: str, tg_chat: str):
    pending = db.get_all_pending_alerts()
    if not pending:
        return

    now_utc = datetime.now(timezone.utc)
    lines = [f"🔭 <b>Interim Snapshot — {len(pending)} coin(s) tracked</b>\n"]

    for alert in pending:
        pair = fetch_pair_data(alert["pair_addr"])
        time.sleep(0.5)

        mc_alert  = float(alert.get("mc_at_alert") or 0)
        check_due = alert.get("check_due_at")
        hours_left = max(0, (check_due - now_utc).total_seconds() / 3600) if check_due else 0
        strategy = alert.get("strategy", "?")

        if pair:
            mc_now = float(pair.get("marketCap") or 0)
            mult   = (mc_now / mc_alert) if mc_alert > 0 and mc_now > 0 else 0
            trend  = "🚀" if mult >= 5 else ("📈" if mult >= 2 else ("➡️" if mult >= 0.8 else "📉"))
            mc_str = f"${mc_now:,.0f}"
        else:
            mult   = 0
            trend  = "💀"
            mc_str = "N/A"

        dex_url = f"https://dexscreener.com/solana/{alert['pair_addr']}"
        lines.append(
            f"{trend} <a href=\"{dex_url}\"><b>{alert['name']} ({alert['symbol']})</b></a>"
            f" [{strategy}]\n"
            f"   MC alert: ${mc_alert:,.0f} → {mc_str}"
            + (f" <b>({mult:.2f}x)</b>" if mult > 0 else "")
            + f" | 72h in: {hours_left:.0f}h"
        )

    send_telegram(tg_token, tg_chat, "\n\n".join(lines))
    logging.info(f"Tracker: interim report sent — {len(pending)} coins")


# ─────────────────────────────────────────────
# BACKGROUND THREAD
# ─────────────────────────────────────────────

def start_background_checker(tg_token: str, tg_chat: str, cfg: dict):
    strategy_a_cfg = cfg.get("strategy_a", {})
    strategy_b_cfg = cfg.get("strategy_b", {})
    interim_freq   = cfg.get("global", {}).get("interim_report_frequency", "daily")
    INTERIM_HOUR   = 20   # 23:00 EAT = 20:00 UTC

    last_interim_key = None

    def _loop():
        nonlocal last_interim_key
        logging.info("Tracker: background checker started")

        while True:
            try:
                now = datetime.now(timezone.utc)

                # Process all monitored alerts
                all_alerts = db.get_alerts_for_monitoring()

                for alert in all_alerts:
                    pair = fetch_pair_data(alert["pair_addr"])
                    if not pair:
                        time.sleep(0.5)
                        continue

                    current_price = float(pair.get("priceUsd") or 0)
                    current_mc    = float(pair.get("marketCap") or 0)

                    if current_price > 0 and current_mc > 0:
                        # Update peak/lowest
                        db.update_peak_and_lowest(
                            alert["id"], current_price, current_mc
                        )

                        # Reload updated alert for accurate peak values
                        fresh_alerts = db.get_alerts_for_monitoring()
                        fresh = next(
                            (a for a in fresh_alerts if a["id"] == alert["id"]),
                            alert
                        )

                        # Get strategy config
                        s_cfg = (strategy_a_cfg if fresh["strategy"] == "A"
                                 else strategy_b_cfg)

                        # Check pullback entry
                        max_hours = s_cfg.get("pullback", {}).get(
                            "max_hours_to_watch", 48
                        )
                        hours_since = (
                            (now - fresh["alerted_at"]).total_seconds() / 3600
                        )
                        if hours_since <= max_hours:
                            _check_pullback(
                                fresh, pair, s_cfg, tg_token, tg_chat
                            )

                        # Check milestones
                        _check_milestones(
                            fresh, pair, s_cfg, tg_token, tg_chat
                        )

                        # Update live metrics
                        pullback = db.get_latest_pullback(fresh["id"])
                        entry_mc = float(
                            pullback.get("mc_at_pullback") or 0
                        ) if pullback else None
                        vol = pair.get("volume") or {}
                        pc  = pair.get("priceChange") or {}
                        db.upsert_live_metrics(
                            alert_id       = fresh["id"],
                            mint           = fresh["mint"],
                            strategy       = fresh["strategy"],
                            current_mc     = current_mc,
                            current_price  = current_price,
                            current_liq    = (pair.get("liquidity") or {}).get("usd"),
                            vol_24h        = vol.get("h24"),
                            vol_5m         = vol.get("m5"),
                            price_change_1h  = pc.get("h1"),
                            price_change_24h = pc.get("h24"),
                            mc_at_alert    = float(fresh.get("mc_at_alert") or 0),
                            entry_price    = entry_mc,
                        )

                    time.sleep(0.5)

                # 72h outcome checks
                _process_72h_outcomes(tg_token, tg_chat, cfg)

                # Interim report at 23:00 EAT
                if interim_freq == "daily":
                    key = (now.date(), now.hour)
                    if now.hour == INTERIM_HOUR and key != last_interim_key:
                        last_interim_key = key
                        _send_interim_report(tg_token, tg_chat)

            except Exception as e:
                logging.error(f"Tracker error: {e}", exc_info=True)

            time.sleep(CHECK_INTERVAL)

    t = threading.Thread(target=_loop, daemon=True, name="tracker-checker")
    t.start()
    logging.info(f"Tracker: running every {CHECK_INTERVAL//60} min")
