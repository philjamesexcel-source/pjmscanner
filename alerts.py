#!/usr/bin/env python3
"""
alerts.py — Telegram message builders.
Rich format matching the original bot style with full checklist,
deployer info, liquidity lock, buy/sell signal, and trading buttons.
"""

import re
import json
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


def _safe(val, default=0.0):
    try:
        return float(val) if val is not None else default
    except (TypeError, ValueError):
        return default


def _esc(s: str) -> str:
    """Escape HTML special chars."""
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _ck(ok: bool, label: str) -> str:
    return ("✅" if ok else "❌") + " " + label


def _short(addr: str) -> str:
    if addr and len(addr) > 8:
        return f"{addr[:4]}…{addr[-4:]}"
    return addr or "?"


def _trading_buttons(pair_addr: str, mint: str, chain: str = "solana") -> dict:
    dex_url  = f"https://dexscreener.com/{chain}/{pair_addr}"
    rc_url   = f"https://rugcheck.xyz/tokens/{mint}"
    birdeye  = f"https://birdeye.so/token/{mint}?chain={chain}"
    photon   = f"https://photon-sol.tinyastro.io/en/r/{mint}"
    axiom    = f"http://axiom.trade/t/{mint}"
    dextools = f"https://www.dextools.io/app/en/solana/pair-explorer/{pair_addr}"
    gecko    = f"https://www.geckoterminal.com/{chain}/pools/{pair_addr}"

    return {"inline_keyboard": [
        [
            {"text": "📈 DexScreener", "url": dex_url},
            {"text": "🔒 RugCheck",    "url": rc_url},
        ],
        [
            {"text": "🐦 Birdeye",  "url": birdeye},
            {"text": "⚡ Photon",   "url": photon},
            {"text": "🪐 Axiom",    "url": axiom},
        ],
        [
            {"text": "🛠 DexTools", "url": dextools},
            {"text": "🦎 Gecko",    "url": gecko},
        ],
    ]}


# ─────────────────────────────────────────────
# 1. INITIAL ALERT — rich format
# ─────────────────────────────────────────────

def build_initial_alert(pair: dict, rc_data: dict, scores: dict,
                         warnings: list, strategy_label: str) -> tuple:
    bt        = pair.get("baseToken") or {}
    mint      = bt.get("address", "")
    symbol    = _esc(bt.get("symbol", "?"))
    name      = _esc(bt.get("name", "Unknown"))
    pair_addr = pair.get("pairAddress", "")
    chain     = pair.get("chainId", "solana")
    dex       = _esc((pair.get("dexId") or "").upper())

    mc       = _safe(pair.get("marketCap"))
    price    = pair.get("priceUsd") or "?"
    liq      = _safe((pair.get("liquidity") or {}).get("usd"))
    vol      = pair.get("volume") or {}
    vol_24h  = _safe(vol.get("h24"))
    vol_1h   = _safe(vol.get("h1"))
    vol_5m   = _safe(vol.get("m5"))
    pc       = pair.get("priceChange") or {}
    h1_pc    = pc.get("h1")
    h24_pc   = pc.get("h24")
    h5m_pc   = pc.get("m5")
    txns     = pair.get("txns") or {}
    txn1h    = txns.get("h1") or {}
    txn24    = txns.get("h24") or {}
    buys_1h  = _safe(txn1h.get("buys"))
    sells_1h = _safe(txn1h.get("sells"))
    buys_24h = _safe(txn24.get("buys"))
    sells_24h= _safe(txn24.get("sells"))
    total_1h = buys_1h + sells_1h

    # Age
    from filters import _pair_age_minutes
    age_m = _pair_age_minutes(pair)
    if age_m is not None:
        age_str = f"{int(age_m//60)}h {int(age_m%60)}m" if age_m >= 60 else f"{int(age_m)}m"
    else:
        age_str = "?"

    # Security
    mint_ok   = rc_data.get("mint_renounced", False)
    freeze_ok = rc_data.get("freeze_renounced", False)
    meta_ok   = not rc_data.get("mutable_metadata", True)
    rc_score  = rc_data.get("score", "N/A")
    locked    = _safe(rc_data.get("lp_locked_pct"))
    iliq      = _safe(rc_data.get("iliq_pct"))
    holders   = rc_data.get("total_holders") or "?"
    top10     = rc_data.get("top10_pct")
    b0_pct    = rc_data.get("block0_snipe_pct")
    deployer  = rc_data.get("deployer") or ""
    risks     = rc_data.get("risks") or []

    top10_str = f"{top10:.2f}%"  if top10  is not None else "?"
    b0_str    = f"{b0_pct:.1f}%" if b0_pct is not None else "?"

    # Liq lock display
    if locked >= 80:
        liq_lock_str = f"🔒 {locked:.0f}%"
    elif iliq >= 99:
        liq_lock_str = "🔥 (bonding curve)"
    elif locked > 0:
        liq_lock_str = f"⚠️ {locked:.0f}% locked"
    else:
        liq_lock_str = "⚠️ 0% locked"

    # Liq/MC pct
    liq_mc_pct = (liq / mc * 100) if mc > 0 else 0

    # Vol/Liq ratio
    vol_liq = (vol_1h / liq) if liq > 0 else 0
    vol_mc  = (vol_24h / mc) if mc > 0 else 0

    # Vol/MC signal
    if vol_mc >= 10:
        vol_mc_sig = f"⚡ Extreme ({vol_mc:.1f}x) — check for wash trading"
    elif vol_mc >= 3:
        vol_mc_sig = f"🔥 Strong ({vol_mc:.1f}x)"
    elif vol_mc >= 1:
        vol_mc_sig = f"✅ Active ({vol_mc:.1f}x)"
    else:
        vol_mc_sig = f"😐 Weak ({vol_mc:.2f}x)"

    # Buy/sell signal
    buy_ratio = (buys_1h / total_1h) if total_1h > 0 else 0
    if total_1h > 0:
        if buy_ratio >= 0.65:
            bs_signal = f"🟢 Accumulation ({int(buys_1h)}B / {int(sells_1h)}S)"
        elif buy_ratio >= 0.52:
            bs_signal = f"🟡 Balanced ({int(buys_1h)}B / {int(sells_1h)}S)"
        else:
            bs_signal = f"🔴 Distribution ({int(buys_1h)}B / {int(sells_1h)}S)"
    else:
        bs_signal = "? No tx data"

    # Risk summary line
    if not rc_data or rc_data.get("score", 9999) == 9999:
        risk_line = "⚠️ No RugCheck data"
    elif risks:
        risk_line = f"⚠️ {_esc(risks[0])}" if len(risks) == 1 else f"⚠️ {len(risks)} risks flagged"
    else:
        risk_line = "✅ Looks clean"

    # Warning block
    warn_block = ("\n⚠️ " + " | ".join(warnings[:3])) if warnings else ""

    # Price change strings
    p1h_str  = f"{h1_pc:+.2f}%"  if h1_pc  is not None else "?"
    p24h_str = f"{h24_pc:+.2f}%" if h24_pc is not None else "?"
    p5m_str  = f"{h5m_pc:+.2f}%" if h5m_pc is not None else "?"

    # URLs
    solscan_t = f"https://solscan.io/token/{mint}"
    solscan_p = f"https://solscan.io/token/{pair_addr}"
    dep_link  = ""
    if deployer and len(deployer) > 8:
        solscan_d = f"https://solscan.io/address/{deployer}"
        dep_link  = f'👨‍💻 Deployer: <a href="{solscan_d}">{_short(deployer)}</a>\n'

    # Owner display
    owner_str = "RENOUNCED" if mint_ok else deployer[:20] if deployer else "Unknown"

    # Checklist
    holders_num = _safe(rc_data.get("total_holders"), 0)
    b0_num      = _safe(b0_pct, 0)
    top10_num   = _safe(top10, 999)
    rc_score_num = _safe(rc_score, 9999)

    checklist = "\n".join([
        _ck(mint_ok,   "Mint renounced")      + "  " + _ck(freeze_ok, "Freeze renounced"),
        _ck(meta_ok,   "Metadata immutable")  + "  " + _ck(rc_score_num < 500, f"RugCheck &lt; 500"),
        _ck(top10_num < 40, f"Top10 &lt; 40% ({top10_str})") + "  " +
        _ck(b0_num < 30,    f"Block0 snipe &lt; 30% ({b0_str})"),
        _ck(liq >= 50_000,  f"Liq &gt; $50K (${liq:,.0f})") + "  " +
        _ck(liq_mc_pct >= 5, f"Liq &ge; 5% of MC ({liq_mc_pct:.0f}%)"),
        _ck(vol_mc >= 0.5,  f"Vol/MC &ge; 0.5x ({vol_mc:.1f}x)") + "  " +
        _ck(holders_num >= 500, f"Holders &ge; 500 ({holders})"),
        _ck((h1_pc or 0) > 0, "1h price positive") + "  " +
        _ck((h24_pc or 0) >= 30, "24h gain &gt; 30%"),
    ])

    msg = (
        f"{strategy_label}\n"
        f'<a href="{solscan_t}"><b>{name} ({symbol})</b></a>\n'
        f"{risk_line}{warn_block}\n"
        f'📌 Pair: <a href="{solscan_p}">{_short(pair_addr)}</a>\n'
        f"{dep_link}"
        f"👤 Owner: {owner_str}\n"
        f"🔸 Chain: {chain.upper()} | ⚖️ Age: {age_str} | DEX: {dex}\n"
        f"🌿 Mint: {'No ✅' if mint_ok else 'Active ❌'} | Liq: {liq_lock_str}\n"
        f"🔐 Freeze: {'✅ Renounced' if freeze_ok else '❌ Active'} | "
        f"Metadata: {'✅ Immutable' if meta_ok else '❌ Mutable'}\n\n"
        f"💰 MC: ${mc:,.0f} | Liq: ${liq:,.0f} ({liq_mc_pct:.0f}% of MC)\n"
        f"📈 24h: {p24h_str} | 1h: {p1h_str} | 5m: {p5m_str}\n"
        f"📊 Vol 24h: ${vol_24h:,.0f} | {bs_signal}\n"
        f"⚡ Vol/MC: {vol_mc_sig}\n"
        f"💵 Price: ${price}\n\n"
        f"🔒 RugCheck Score: {rc_score}\n"
        f"👥 Holders: {holders} | Top10: {top10_str}\n"
        f"🥡 Block 0 Snipes: {b0_str}\n\n"
        f"<b>📋 CHECKLIST</b>\n"
        f"{checklist}\n\n"
        f"📋 <code>{mint}</code>"
    )

    return msg, _trading_buttons(pair_addr, mint, chain)


# ─────────────────────────────────────────────
# 2. PULLBACK ENTRY ALERT
# ─────────────────────────────────────────────

def build_pullback_alert(alert: dict, current_pair: dict,
                          pullback_pct: float,
                          strategy_label: str) -> tuple:
    symbol    = alert.get("symbol", "?")
    name      = _esc(alert.get("name", "Unknown"))
    mint      = alert.get("mint", "")
    pair_addr = alert.get("pair_addr", "")

    mc_at_alert = float(alert.get("mc_at_alert") or 0)
    peak_mc     = float(alert.get("peak_mc") or mc_at_alert)
    peak_price  = float(alert.get("peak_price") or 0)
    current_mc  = float(current_pair.get("marketCap") or 0)
    current_price = float(current_pair.get("priceUsd") or 0)
    vol_5m      = float((current_pair.get("volume") or {}).get("m5") or 0)
    peak_mult   = (peak_mc / mc_at_alert) if mc_at_alert > 0 else 1

    msg = (
        f"{strategy_label} 🎯 <b>PULLBACK ENTRY — {name} ({symbol})</b>\n\n"
        f"📉 Pulled back <b>{pullback_pct:.1f}%</b> from peak\n\n"
        f"📌 MC at initial alert:  <b>${mc_at_alert:,.0f}</b>\n"
        f"🏔 Peak MC reached:      <b>${peak_mc:,.0f}</b> (<b>{peak_mult:.2f}x</b>)\n"
        f"📍 Current MC:           <b>${current_mc:,.0f}</b>\n\n"
        f"💵 Entry price:          <b>${current_price:.8f}</b>\n"
        f"📊 Vol 5m (at entry):    <b>${vol_5m:,.0f}</b>\n\n"
        f"⚡ <b>Potential entry zone — watch for reversal</b>\n\n"
        f"<code>{mint}</code>"
    )
    return msg, _trading_buttons(pair_addr, mint)


# ─────────────────────────────────────────────
# 3. MILESTONE ALERT
# ─────────────────────────────────────────────

def build_milestone_alert(alert: dict, milestone_type: str,
                           multiplier: float, current_mc: float,
                           current_price: float,
                           strategy_label: str) -> tuple:
    symbol    = alert.get("symbol", "?")
    name      = _esc(alert.get("name", "Unknown"))
    mint      = alert.get("mint", "")
    pair_addr = alert.get("pair_addr", "")

    mc_at_alert = float(alert.get("mc_at_alert") or 0)

    emoji_map = {2: "📈", 3: "🔥", 5: "🚀", 10: "🌙"}
    emoji = emoji_map.get(int(multiplier), "💰")

    if milestone_type == "vs_alert":
        base_label = "initial alert MC"
        base_val   = f"${mc_at_alert:,.0f}"
    else:
        pullback = None
        try:
            import db as _db
            pullback = _db.get_latest_pullback(alert["id"])
        except Exception:
            pass
        entry_mc = float((pullback or {}).get("mc_at_pullback") or 0)
        base_label = "pullback entry"
        base_val   = f"${entry_mc:,.0f}" if entry_mc > 0 else "entry"

    msg = (
        f"{strategy_label} {emoji} <b>{multiplier:.0f}x MILESTONE — {name} ({symbol})</b>\n\n"
        f"<b>{multiplier:.0f}x</b> vs {base_label} ({base_val})\n\n"
        f"📍 Current MC:    <b>${current_mc:,.0f}</b>\n"
        f"💵 Current price: <b>${current_price:.8f}</b>\n\n"
        f"<code>{mint}</code>"
    )
    return msg, _trading_buttons(pair_addr, mint)


# ─────────────────────────────────────────────
# 4. 72H OUTCOME ALERT
# ─────────────────────────────────────────────

def build_outcome_alert(alert: dict, mc_at_72h: float,
                         mult_vs_alert: float,
                         mult_vs_entry: float,
                         outcome: str,
                         strategy_label: str) -> tuple:
    symbol    = alert.get("symbol", "?")
    name      = _esc(alert.get("name", "Unknown"))
    mint      = alert.get("mint", "")
    pair_addr = alert.get("pair_addr", "")

    mc_at_alert = float(alert.get("mc_at_alert") or 0)
    alerted_at  = alert.get("alerted_at")
    alerted_str = alerted_at.strftime("%Y-%m-%d %H:%M UTC") if alerted_at else "?"

    emoji_map = {"moon": "🚀", "up": "📈", "flat": "➡️", "down": "📉", "dead": "💀"}
    emoji = emoji_map.get(outcome, "❓")
    msg_map = {
        "moon": "Moonshot! Strong signal confirmed.",
        "up":   "Good gain. Signal validated.",
        "flat": "Flat — held value but no breakout.",
        "down": "Lost value. Review filters.",
        "dead": "Dead or rugged.",
    }

    mc_str    = f"${mc_at_72h:,.0f}" if mc_at_72h and mc_at_72h > 0 else "N/A"
    entry_str = f"{mult_vs_entry:.2f}x" if mult_vs_entry else "N/A"

    msg = (
        f"{strategy_label} {emoji} <b>72h RESULT — {name} ({symbol})</b>\n\n"
        f"📅 Alerted: {alerted_str}\n\n"
        f"💰 MC at alert:      <b>${mc_at_alert:,.0f}</b>\n"
        f"💰 MC at 72h:        <b>{mc_str}</b>\n"
        f"📊 vs Alert MC:      <b>{mult_vs_alert:.2f}x</b>\n"
        f"📊 vs Entry price:   <b>{entry_str}</b>\n\n"
        f"{msg_map.get(outcome, '')}\n\n"
        f"<code>{mint}</code>"
    )
    return msg, _trading_buttons(pair_addr, mint)


# ─────────────────────────────────────────────
# 5. SUMMARY MESSAGES
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
        "<b>Why tokens are failing filters:</b>"
    ]
    if failure_tally:
        for reason, count in sorted(
            failure_tally.items(), key=lambda x: -x[1]
        )[:10]:
            lines.append(f"  • {reason}: {count}")
    else:
        lines.append("  No data yet.")
    return "\n".join(lines)
