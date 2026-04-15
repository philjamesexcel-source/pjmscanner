"""
alerts/telegram.py — Telegram alert builder and sender.
Rich HTML format with full checklist, deployer info, and trading buttons.
"""

import json
import logging
import re
from typing import Optional

import requests

from core.rate_limiter import wait as rl_wait

logger = logging.getLogger(__name__)


def _f(v, d=0.0) -> float:
    try:
        return float(v) if v is not None else d
    except (TypeError, ValueError):
        return d


def _esc(s) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _short(addr: str) -> str:
    return f"{addr[:4]}…{addr[-4:]}" if addr and len(addr) > 8 else (addr or "?")


def _ck(ok: bool, label: str) -> str:
    return ("✅" if ok else "❌") + " " + label


def send(token: str, chat_id: str, text: str,
         reply_markup: dict = None) -> bool:
    rl_wait("telegram")
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
        logger.error(f"Telegram send failed: {e}")
        try:
            plain = re.sub(r"<[^>]+>", "", text)
            requests.post(url, json={"chat_id": chat_id, "text": plain[:4000]}, timeout=10)
        except Exception:
            pass
        return False


def _buttons(pair_addr: str, mint: str, chain: str = "solana") -> dict:
    dex      = f"https://dexscreener.com/{chain}/{pair_addr}"
    rc_url   = f"https://rugcheck.xyz/tokens/{mint}"
    birdeye  = f"https://birdeye.so/token/{mint}?chain={chain}"
    photon   = f"https://photon-sol.tinyastro.io/en/r/{mint}"
    axiom    = f"http://axiom.trade/t/{mint}"
    dextools = f"https://www.dextools.io/app/en/solana/pair-explorer/{pair_addr}"
    gecko    = f"https://www.geckoterminal.com/{chain}/pools/{pair_addr}"
    return {"inline_keyboard": [
        [{"text": "📈 DexScreener", "url": dex},
         {"text": "🔒 RugCheck",    "url": rc_url}],
        [{"text": "🐦 Birdeye",     "url": birdeye},
         {"text": "⚡ Photon",      "url": photon},
         {"text": "🪐 Axiom",       "url": axiom}],
        [{"text": "🛠 DexTools",    "url": dextools},
         {"text": "🦎 Gecko",       "url": gecko}],
    ]}


# ─────────────────────────────────────────────
# 1. DETECTION ALERT
# ─────────────────────────────────────────────

def build_detection_alert(metrics: dict, rc: dict, scores: dict,
                           warnings: list, strategy_label: str) -> tuple:
    mint      = metrics["mint"]
    symbol    = _esc(metrics["symbol"])
    name      = _esc(metrics["name"])
    pair_addr = metrics["pair_addr"]
    chain     = metrics["chain"]
    dex       = _esc(metrics["dex"])

    mc       = _f(metrics["mc"])
    price    = metrics.get("price", 0)
    liq      = _f(metrics["liq_usd"])
    vol_5m   = _f(metrics["vol_5m"])
    vol_1h   = _f(metrics["vol_1h"])
    vol_24h  = _f(metrics["vol_24h"])
    pc_1h    = metrics.get("pc_1h")
    pc_24h   = metrics.get("pc_24h")
    pc_5m    = metrics.get("pc_5m")
    buys_1h  = _f(metrics["buys_1h"])
    sells_1h = _f(metrics["sells_1h"])
    bs_ratio = _f(metrics["buy_sell_ratio_1h"])
    age_m    = _f(metrics.get("age_minutes"))

    # Security
    mint_ok   = rc.get("mint_renounced", False)
    freeze_ok = rc.get("freeze_renounced", False)
    meta_ok   = not rc.get("mutable_metadata", True)
    rc_score  = rc.get("score", "N/A")
    locked    = _f(rc.get("lp_locked_pct"))
    iliq      = _f(rc.get("iliq_pct"))
    holders   = rc.get("total_holders") or "?"
    top10     = rc.get("top10_pct")
    b0_pct    = rc.get("block0_snipe_pct")
    deployer  = rc.get("deployer") or ""
    risk_line = _esc(rc.get("risk_summary") or "⚠️ No RugCheck data")

    top10_str = f"{_f(top10):.2f}%"  if top10  is not None else "?"
    b0_str    = f"{_f(b0_pct):.1f}%" if b0_pct is not None else "?"
    liq_mc_pct = (liq / mc * 100) if mc > 0 else 0

    # LP lock display
    if locked >= 80:
        liq_lock_str = f"🔒 {locked:.0f}%"
    elif iliq >= 99:
        liq_lock_str = "🔥 (bonding curve)"
    elif locked > 0:
        liq_lock_str = f"⚠️ {locked:.0f}% locked"
    else:
        liq_lock_str = "⚠️ 0% locked"

    # Vol/MC signal
    vol_mc = (vol_24h / mc) if mc > 0 else 0
    if vol_mc >= 10:
        vol_mc_sig = f"⚡ Extreme ({vol_mc:.1f}x) — check wash trading"
    elif vol_mc >= 3:
        vol_mc_sig = f"🔥 Strong ({vol_mc:.1f}x)"
    elif vol_mc >= 1:
        vol_mc_sig = f"✅ Active ({vol_mc:.1f}x)"
    else:
        vol_mc_sig = f"😐 Weak ({vol_mc:.2f}x)"

    # Buy/sell signal
    total_1h = buys_1h + sells_1h
    if total_1h > 0:
        if bs_ratio >= 1.5:
            bs_sig = f"🟢 Accumulation ({int(buys_1h)}B / {int(sells_1h)}S)"
        elif bs_ratio >= 1.2:
            bs_sig = f"🟡 Balanced ({int(buys_1h)}B / {int(sells_1h)}S)"
        else:
            bs_sig = f"🔴 Distribution ({int(buys_1h)}B / {int(sells_1h)}S)"
    else:
        bs_sig = "? No tx data"

    # Age
    if age_m is not None:
        age_str = f"{int(age_m//60)}h {int(age_m%60)}m" if age_m >= 60 else f"{int(age_m)}m"
    else:
        age_str = "?"

    # Composite score
    score_val = _f(scores.get("composite"))
    score_tier = ""
    if score_val >= 80:
        score_tier = "🔥 STRONG"
    elif score_val >= 65:
        score_tier = "✅ GOOD"
    else:
        score_tier = "⚠️ MODERATE"

    # Price strings
    p1h_str  = f"{_f(pc_1h):+.2f}%"  if pc_1h  is not None else "?"
    p24h_str = f"{_f(pc_24h):+.2f}%" if pc_24h is not None else "?"
    p5m_str  = f"{_f(pc_5m):+.2f}%"  if pc_5m  is not None else "?"

    # URLs
    solscan_t = f"https://solscan.io/token/{mint}"
    solscan_p = f"https://solscan.io/token/{pair_addr}"
    dep_link  = ""
    if deployer and len(deployer) > 8:
        solscan_d = f"https://solscan.io/address/{deployer}"
        dep_link  = f'👨‍💻 Deployer: <a href="{solscan_d}">{_short(deployer)}</a>\n'
    owner_str = "RENOUNCED" if mint_ok else "Active ⚠️"

    # Checklist
    holders_n = _f(rc.get("total_holders"), 0)
    b0_n      = _f(b0_pct, 0)
    top10_n   = _f(top10, 999)
    rc_n      = _f(rc_score, 9999)

    checklist = "\n".join([
        _ck(mint_ok,   "Mint renounced")     + "  " + _ck(freeze_ok, "Freeze renounced"),
        _ck(meta_ok,   "Metadata immutable") + "  " + _ck(rc_n < 500, f"RugCheck &lt; 500"),
        _ck(top10_n < 40, f"Top10 &lt; 40% ({top10_str})") + "  " +
        _ck(b0_n < 30,    f"Block0 &lt; 30% ({b0_str})"),
        _ck(liq >= 50000,   f"Liq &gt; $50K (${liq:,.0f})") + "  " +
        _ck(liq_mc_pct >= 5, f"Liq &ge; 5% of MC ({liq_mc_pct:.0f}%)"),
        _ck(vol_mc >= 0.5,   f"Vol/MC &ge; 0.5x ({vol_mc:.1f}x)") + "  " +
        _ck(holders_n >= 500, f"Holders &ge; 500 ({holders})"),
        _ck(_f(pc_1h) > 0, "1h price positive") + "  " +
        _ck(_f(pc_24h) >= 30, "24h gain &gt; 30%"),
    ])

    warn_block = ("\n⚠️ " + " | ".join(warnings[:3])) if warnings else ""

    msg = (
        f"{strategy_label} — {score_tier}\n"
        f'<a href="{solscan_t}"><b>{name} ({symbol})</b></a>\n'
        f"{risk_line}{warn_block}\n\n"
        f'📌 Pair: <a href="{solscan_p}">{_short(pair_addr)}</a>\n'
        f"{dep_link}"
        f"👤 Owner: {owner_str}\n"
        f"🔸 Chain: {chain.upper()} | ⚖️ Age: {age_str} | DEX: {dex}\n"
        f"🌿 Mint: {'No ✅' if mint_ok else 'Active ❌'} | Liq: {liq_lock_str}\n"
        f"🔐 Freeze: {'✅ Renounced' if freeze_ok else '❌ Active'} | "
        f"Metadata: {'✅ Immutable' if meta_ok else '❌ Mutable'}\n\n"
        f"💰 MC: ${mc:,.0f} | Liq: ${liq:,.0f} ({liq_mc_pct:.0f}% of MC)\n"
        f"📈 24h: {p24h_str} | 1h: {p1h_str} | 5m: {p5m_str}\n"
        f"📊 Vol 24h: ${vol_24h:,.0f} | {bs_sig}\n"
        f"⚡ Vol/MC: {vol_mc_sig}\n"
        f"💵 Price: ${price}\n\n"
        f"🎯 Score: <b>{score_val:.0f}/100</b>  "
        f"[Liq:{scores.get('liquidity',0):.0f} "
        f"Vol:{scores.get('volume',0):.0f} "
        f"Mom:{scores.get('momentum',0):.0f} "
        f"Risk:{scores.get('risk',0):.0f}]\n\n"
        f"🔒 RugCheck Score: {rc_score}\n"
        f"👥 Holders: {holders} | Top10: {top10_str}\n"
        f"🥡 Block 0 Snipes: {b0_str}\n\n"
        f"<b>📋 CHECKLIST</b>\n{checklist}\n\n"
        f"📋 <code>{mint}</code>"
    )
    return msg, _buttons(pair_addr, mint, chain)


# ─────────────────────────────────────────────
# 2. ENTRY SIGNAL (PULLBACK)
# ─────────────────────────────────────────────

def build_entry_alert(token: dict, current_metrics: dict,
                       pullback_pct: float, signal_type: str,
                       strategy_label: str) -> tuple:
    symbol    = token.get("symbol", "?")
    name      = _esc(token.get("name", "Unknown"))
    mint      = token.get("mint", "")
    pair_addr = token.get("pair_addr", "")
    chain     = token.get("chain", "solana")

    mc_det  = _f(token.get("mc_at_detection"))
    peak_mc = _f(token.get("peak_mc") or mc_det)
    cur_mc  = _f(current_metrics.get("mc"))
    cur_p   = _f(current_metrics.get("price"))
    vol_5m  = _f(current_metrics.get("vol_5m"))
    bs_sig  = _f(current_metrics.get("buy_sell_ratio_1h"))

    peak_mult = (peak_mc / mc_det) if mc_det > 0 else 1

    sig_emoji = "🎯" if signal_type == "pullback" else "🔄"
    sig_label = "PULLBACK ENTRY" if signal_type == "pullback" else "REACCUMULATION"

    msg = (
        f"{strategy_label} {sig_emoji} <b>{sig_label} — {name} ({symbol})</b>\n\n"
        f"📉 Pulled back <b>{pullback_pct:.1f}%</b> from peak\n\n"
        f"📌 MC at detection: <b>${mc_det:,.0f}</b>\n"
        f"🏔 Peak MC reached: <b>${peak_mc:,.0f}</b> (<b>{peak_mult:.2f}x</b>)\n"
        f"📍 Current MC:      <b>${cur_mc:,.0f}</b>\n\n"
        f"💵 Entry price: <b>${cur_p:.8f}</b>\n"
        f"📊 Vol 5m:      <b>${vol_5m:,.0f}</b>\n"
        f"📊 B/S ratio:   <b>{bs_sig:.2f}</b>\n\n"
        f"⚡ <b>Entry zone confirmed — watch for reversal</b>\n\n"
        f"<code>{mint}</code>"
    )
    return msg, _buttons(pair_addr, mint, chain)


# ─────────────────────────────────────────────
# 3. MILESTONE
# ─────────────────────────────────────────────

def build_milestone_alert(token: dict, milestone_type: str,
                           multiplier: float, current_mc: float,
                           current_price: float,
                           strategy_label: str) -> tuple:
    symbol    = token.get("symbol", "?")
    name      = _esc(token.get("name", "Unknown"))
    mint      = token.get("mint", "")
    pair_addr = token.get("pair_addr", "")
    chain     = token.get("chain", "solana")
    mc_det    = _f(token.get("mc_at_detection"))

    emoji = {2:"📈", 3:"🔥", 5:"🚀", 10:"🌙", 20:"👑"}.get(int(multiplier), "💰")
    base  = "detection MC" if milestone_type == "vs_detection" else "entry price"

    msg = (
        f"{strategy_label} {emoji} <b>{multiplier:.0f}x — {name} ({symbol})</b>\n\n"
        f"<b>{multiplier:.0f}x</b> vs {base}\n"
        f"Base: ${mc_det:,.0f}\n\n"
        f"📍 Current MC:    <b>${current_mc:,.0f}</b>\n"
        f"💵 Current price: <b>${current_price:.8f}</b>\n\n"
        f"<code>{mint}</code>"
    )
    return msg, _buttons(pair_addr, mint, chain)


# ─────────────────────────────────────────────
# 4. WALLET ALERT
# ─────────────────────────────────────────────

def build_wallet_alert(trades: list, token_mc: float,
                        strategy_label: str = "") -> tuple:
    if not trades:
        return "", {}

    first   = trades[0]
    mint    = first.get("mint", "")
    symbol  = _esc(first.get("symbol") or mint[:8])
    wallets = [t["wallet_address"] for t in trades]
    count   = len(trades)
    total   = sum(_f(t.get("amount_usd")) for t in trades)
    avg_sc  = sum(_f(t.get("score")) for t in trades) / count if count > 0 else 0

    wallet_lines = "\n".join([
        f"  👛 {_short(t['wallet_address'])} | "
        f"${_f(t.get('amount_usd')):,.0f} | "
        f"Score: {_f(t.get('score')):.2f}"
        for t in trades[:5]
    ])

    msg = (
        f"🧠 <b>SMART WALLET SIGNAL — {symbol}</b>\n\n"
        f"<b>{count}</b> tracked wallet(s) buying\n"
        f"Total size: <b>${total:,.0f}</b>\n"
        f"Avg wallet score: <b>{avg_sc:.2f}</b>\n\n"
        f"{wallet_lines}\n\n"
        f"📍 Token MC: ${token_mc:,.0f}\n\n"
        f"<code>{mint}</code>"
    )
    return msg, _buttons(first.get("pair_addr", mint), mint)


# ─────────────────────────────────────────────
# 5. OUTCOME (72H)
# ─────────────────────────────────────────────

def build_outcome_alert(token: dict, mc_72h: float,
                         mult_det: float, mult_entry: float,
                         outcome: str, strategy_label: str) -> tuple:
    symbol    = token.get("symbol", "?")
    name      = _esc(token.get("name", "Unknown"))
    mint      = token.get("mint", "")
    pair_addr = token.get("pair_addr", "")
    chain     = token.get("chain", "solana")
    mc_det    = _f(token.get("mc_at_detection"))
    det_at    = token.get("detected_at")
    det_str   = det_at.strftime("%Y-%m-%d %H:%M UTC") if det_at else "?"

    emoji = {"moon":"🚀","up":"📈","flat":"➡️","down":"📉","dead":"💀"}.get(outcome,"❓")
    msg_m = {"moon":"Moonshot! Signal confirmed.","up":"Good gain.","flat":"Flat.",
             "down":"Loss. Review filters.","dead":"Dead or rugged."}.get(outcome,"")

    mc_str  = f"${mc_72h:,.0f}" if mc_72h and mc_72h > 0 else "N/A"
    ent_str = f"{mult_entry:.2f}x" if mult_entry else "N/A"

    msg = (
        f"{strategy_label} {emoji} <b>72h RESULT — {name} ({symbol})</b>\n\n"
        f"📅 Detected: {det_str}\n\n"
        f"💰 MC at detection: <b>${mc_det:,.0f}</b>\n"
        f"💰 MC at 72h:       <b>{mc_str}</b>\n"
        f"📊 vs Detection:    <b>{mult_det:.2f}x</b>\n"
        f"📊 vs Entry:        <b>{ent_str}</b>\n\n"
        f"{msg_m}\n\n<code>{mint}</code>"
    )
    return msg, _buttons(pair_addr, mint, chain)


# ─────────────────────────────────────────────
# 6. SUMMARIES
# ─────────────────────────────────────────────

def build_startup(strategies_on: list, interval_s: int) -> str:
    return (
        f"🚀 <b>PJM Scanner Started</b>\n\n"
        f"Strategies: {' | '.join(strategies_on)}\n"
        f"Scan: every <b>{interval_s//60}min</b>\n"
        f"📅 Summaries at <b>13:00 and 23:00 EAT</b>"
    )


def build_empty_summary(failure_tally: dict, eat_hour: int) -> str:
    lines = [
        f"📭 <b>Summary — {eat_hour:02d}:00 EAT — No alerts this window</b>\n",
        "<b>Why tokens are failing:</b>"
    ]
    for reason, count in sorted(failure_tally.items(), key=lambda x: -x[1])[:10]:
        lines.append(f"  • {reason}: {count}")
    return "\n".join(lines)


def build_summary(alerts_sent: int, eat_hour: int,
                   interval_s: int, by_strategy: dict) -> str:
    lines = [f"📊 <b>Summary — {eat_hour:02d}:00 EAT</b>\n"]
    lines.append(f"✅ <b>{alerts_sent}</b> alert(s) this window")
    for label, count in by_strategy.items():
        lines.append(f"  {label}: {count}")
    lines.append(f"\nHealthy | every {interval_s//60}min")
    return "\n".join(lines)
