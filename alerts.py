#!/usr/bin/env python3
"""
alerts.py — Telegram message builders.
Three alert types:
  1. Initial alert   — token passed filters
  2. Pullback entry  — token pulled back to entry zone with volume
  3. Milestone       — 2x, 5x, 10x vs alert MC or entry price
"""

import re
import logging
import requests


HEADERS = {"User-Agent": "MemecoinScreener/1.0"}


# ─────────────────────────────────────────────
# TELEGRAM SENDER
# ─────────────────────────────────────────────

def send_telegram(token: str, chat_id: str, text: str,
                  reply_markup: dict = None) -> bool:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id":    chat_id,
        "text":       text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        import json
        payload["reply_markup"] = json.dumps(reply_markup)
    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
        return True
    except Exception as e:
        logging.error(f"Telegram send failed: {e}")
        try:
            plain = re.sub(r"<[^>]+>", "", text)
            requests.post(url, json={
                "chat_id": chat_id, "text": plain[:4000]
            }, timeout=10)
        except Exception:
            pass
        return False


def _buttons(pair_addr: str, mint: str, chain: str = "solana") -> dict:
    return {"inline_keyboard": [[
        {"text": "📈 DexScreener",
         "url": f"https://dexscreener.com/{chain}/{pair_addr}"},
        {"text": "🔒 RugCheck",
         "url": f"https://rugcheck.xyz/tokens/{mint}"},
    ]]}


# ─────────────────────────────────────────────
# 1. INITIAL ALERT
# ─────────────────────────────────────────────

def build_initial_alert(pair: dict, rc_data: dict, scores: dict,
                         warnings: list, strategy_label: str) -> tuple:
    bt      = pair.get("baseToken") or {}
    mint    = bt.get("address", "")
    symbol  = bt.get("symbol", "?")
    name    = bt.get("name", "Unknown")
    pair_addr = pair.get("pairAddress", "")
    chain   = pair.get("chainId", "solana")

    mc       = pair.get("marketCap") or 0
    price    = float(pair.get("priceUsd") or 0)
    liq      = (pair.get("liquidity") or {}).get("usd") or 0
    vol      = pair.get("volume") or {}
    vol_24h  = vol.get("h24") or 0
    vol_1h   = vol.get("h1") or 0
    vol_5m   = vol.get("m5") or 0
    pc       = pair.get("priceChange") or {}
    h1_pc    = pc.get("h1") or 0
    h24_pc   = pc.get("h24") or 0
    txns     = pair.get("txns") or {}
    h1_txns  = txns.get("h1") or {}
    buys_1h  = h1_txns.get("buys") or 0
    sells_1h = h1_txns.get("sells") or 0
    total_1h = buys_1h + sells_1h
    buy_ratio = (buys_1h / total_1h * 100) if total_1h > 0 else 0

    score = scores.get("composite", 0)
    tier  = "🔥 <b>Strong Setup</b>" if score >= 0.78 else "✅ <b>Good Setup</b>"

    # Age
    from filters import _pair_age_minutes
    age_m = _pair_age_minutes(pair)
    if age_m is not None:
        age_str = f"{age_m/60:.1f}h" if age_m >= 60 else f"{age_m:.0f}m"
    else:
        age_str = "unknown"

    # RugCheck
    rc_score = int((rc_data or {}).get("score", 0))
    rc_emoji = "✅" if rc_score < 200 else ("⚠️" if rc_score < 500 else "🔴")

    # Score bar
    def score_bar(s, width=10):
        filled = round(s * width)
        return "█" * filled + "░" * (width - filled)

    warn_text = ""
    if warnings:
        warn_text = "\n⚠️ " + " | ".join(warnings[:3])

    msg = (
        f"{strategy_label}\n"
        f"🪙 <b>{name} ({symbol})</b>\n\n"
        f"💰 MC:       <b>${mc:,.0f}</b>\n"
        f"💧 Liq:      <b>${liq:,.0f}</b>\n"
        f"📊 Vol 5m:   <b>${vol_5m:,.0f}</b>\n"
        f"📊 Vol 1h:   <b>${vol_1h:,.0f}</b>\n"
        f"📊 Vol 24h:  <b>${vol_24h:,.0f}</b>\n"
        f"📈 Chg 1h:   <b>{h1_pc:+.1f}%</b>  |  24h: <b>{h24_pc:+.1f}%</b>\n"
        f"🔄 Txns 1h:  <b>{total_1h:.0f}</b>  |  Buy ratio: <b>{buy_ratio:.0f}%</b>\n"
        f"⏱ Age:      <b>{age_str}</b>\n"
        f"{rc_emoji} RugCheck:  <b>{rc_score}</b>\n\n"
        f"🎯 Score: <b>{score:.3f}</b>  {score_bar(score)}\n"
        f"{tier}\n"
        f"{warn_text}\n"
        f"<code>{mint}</code>"
    )

    return msg, _buttons(pair_addr, mint, chain)


# ─────────────────────────────────────────────
# 2. PULLBACK ENTRY ALERT
# ─────────────────────────────────────────────

def build_pullback_alert(alert: dict, current_pair: dict,
                          pullback_pct: float,
                          strategy_label: str) -> tuple:
    symbol    = alert.get("symbol", "?")
    name      = alert.get("name", "Unknown")
    mint      = alert.get("mint", "")
    pair_addr = alert.get("pair_addr", "")
    chain     = "solana"

    mc_at_alert    = float(alert.get("mc_at_alert") or 0)
    price_at_alert = float(alert.get("price_at_alert") or 0)
    peak_mc        = float(alert.get("peak_mc") or mc_at_alert)
    peak_price     = float(alert.get("peak_price") or price_at_alert)

    vol   = (current_pair.get("volume") or {})
    vol_5m = vol.get("m5") or 0
    current_mc    = current_pair.get("marketCap") or 0
    current_price = float(current_pair.get("priceUsd") or 0)

    peak_mult = (peak_mc / mc_at_alert) if mc_at_alert > 0 else 1

    msg = (
        f"{strategy_label} 🎯 <b>PULLBACK ENTRY — {name} ({symbol})</b>\n\n"
        f"📉 Pulled back <b>{pullback_pct:.1f}%</b> from peak\n\n"
        f"📌 MC at initial alert:  <b>${mc_at_alert:,.0f}</b>\n"
        f"🏔 Peak MC reached:     <b>${peak_mc:,.0f}</b>  "
        f"(<b>{peak_mult:.2f}x</b>)\n"
        f"📍 Current MC:          <b>${current_mc:,.0f}</b>\n\n"
        f"💵 Entry price:         <b>${current_price:.8f}</b>\n"
        f"📊 Vol 5m (at entry):   <b>${vol_5m:,.0f}</b>\n\n"
        f"⚡ <b>Potential entry zone — watch for reversal</b>\n"
        f"<code>{mint}</code>"
    )

    return msg, _buttons(pair_addr, mint, chain)


# ─────────────────────────────────────────────
# 3. MILESTONE ALERT
# ─────────────────────────────────────────────

def build_milestone_alert(alert: dict, milestone_type: str,
                           multiplier: float, current_mc: float,
                           current_price: float,
                           strategy_label: str) -> tuple:
    symbol    = alert.get("symbol", "?")
    name      = alert.get("name", "Unknown")
    mint      = alert.get("mint", "")
    pair_addr = alert.get("pair_addr", "")
    chain     = "solana"

    mc_at_alert    = float(alert.get("mc_at_alert") or 0)
    price_at_alert = float(alert.get("price_at_alert") or 0)

    if milestone_type == "vs_alert":
        base_label  = "initial alert MC"
        base_value  = f"${mc_at_alert:,.0f}"
        emoji_map   = {2: "📈", 5: "🚀", 10: "🌙"}
    else:
        base_label  = "pullback entry"
        base_value  = f"${price_at_alert:.8f}"
        emoji_map   = {2: "📈", 3: "🔥", 5: "🚀"}

    emoji = emoji_map.get(int(multiplier), "💰")

    msg = (
        f"{strategy_label} {emoji} <b>{multiplier:.0f}x MILESTONE — {name} ({symbol})</b>\n\n"
        f"<b>{multiplier:.0f}x</b> vs {base_label}\n"
        f"Base: {base_value}\n\n"
        f"📍 Current MC:    <b>${current_mc:,.0f}</b>\n"
        f"💵 Current price: <b>${current_price:.8f}</b>\n\n"
        f"<code>{mint}</code>"
    )

    return msg, _buttons(pair_addr, mint, chain)


# ─────────────────────────────────────────────
# 4. 72H OUTCOME ALERT
# ─────────────────────────────────────────────

def build_outcome_alert(alert: dict, mc_at_72h: float,
                         mult_vs_alert: float,
                         mult_vs_entry: float,
                         outcome: str,
                         strategy_label: str) -> tuple:
    symbol    = alert.get("symbol", "?")
    name      = alert.get("name", "Unknown")
    mint      = alert.get("mint", "")
    pair_addr = alert.get("pair_addr", "")
    chain     = "solana"

    mc_at_alert = float(alert.get("mc_at_alert") or 0)
    alerted_at  = alert.get("alerted_at")
    alerted_str = alerted_at.strftime("%Y-%m-%d %H:%M UTC") if alerted_at else "?"

    emoji_map = {
        "moon": "🚀", "up": "📈", "flat": "➡️", "down": "📉", "dead": "💀"
    }
    emoji = emoji_map.get(outcome, "❓")
    msg_map = {
        "moon": "Moonshot! Strong signal.",
        "up":   "Good gain. Signal confirmed.",
        "flat": "Flat — held value but no breakout.",
        "down": "Lost value. Review filters.",
        "dead": "Dead or rugged.",
    }

    mc_str = f"${mc_at_72h:,.0f}" if mc_at_72h and mc_at_72h > 0 else "N/A"
    entry_str = f"{mult_vs_entry:.2f}x" if mult_vs_entry else "N/A"

    msg = (
        f"{strategy_label} {emoji} <b>72h RESULT — {name} ({symbol})</b>\n\n"
        f"📅 Alerted: {alerted_str}\n\n"
        f"💰 MC at alert:     <b>${mc_at_alert:,.0f}</b>\n"
        f"💰 MC at 72h:       <b>{mc_str}</b>\n"
        f"📊 vs Alert MC:     <b>{mult_vs_alert:.2f}x</b>\n"
        f"📊 vs Entry price:  <b>{entry_str}</b>\n\n"
        f"{msg_map.get(outcome, '')}\n\n"
        f"<code>{mint}</code>"
    )

    return msg, _buttons(pair_addr, mint, chain)


# ─────────────────────────────────────────────
# 5. SUMMARY / INTERIM MESSAGES
# ─────────────────────────────────────────────

def build_startup_message(strategies_on: list, scan_interval_min: int) -> str:
    return (
        f"🚀 <b>Memecoin Screener Started</b>\n\n"
        f"Active strategies: {' | '.join(strategies_on)}\n"
        f"Scan interval: every <b>{scan_interval_min} min</b>\n"
        f"📅 Summaries at <b>13:00 and 23:00 EAT</b>"
    )


def build_scan_summary(alerts_sent: int, eat_hour: int,
                        interval_min: int, by_strategy: dict) -> str:
    lines = [f"📊 <b>Summary — {eat_hour:02d}:00 EAT</b>\n"]
    lines.append(f"✅ <b>{alerts_sent}</b> alert(s) sent this window")
    for strategy, count in by_strategy.items():
        lines.append(f"  {strategy}: {count}")
    lines.append(f"\nScreener healthy | every {interval_min} min")
    return "\n".join(lines)


def build_empty_scan_summary(failure_tally: dict, eat_hour: int) -> str:
    lines = [
        f"📭 <b>Summary — {eat_hour:02d}:00 EAT — No alerts this window</b>\n",
        "<b>Why tokens are failing:</b>"
    ]
    if failure_tally:
        for reason, count in sorted(
            failure_tally.items(), key=lambda x: -x[1]
        )[:10]:
            lines.append(f"  • {reason}: {count}")
    else:
        lines.append("  No data yet.")
    return "\n".join(lines)
