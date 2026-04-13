#!/usr/bin/env python3
"""
filters.py — Filter layers and composite scoring.
Each function takes (pair, rc_data, strategy_cfg) and returns
(passed: bool, failures: list, warnings: list).
"""

import math
import logging
from datetime import datetime, timezone
from typing import Optional


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def _safe(val, default=0):
    try:
        return float(val) if val is not None else default
    except (TypeError, ValueError):
        return default


def _pair_age_minutes(pair: dict) -> Optional[float]:
    created = pair.get("pairCreatedAt")
    if not created:
        return None
    now_ms = datetime.now(timezone.utc).timestamp() * 1000
    return (now_ms - created) / 60_000


# ─────────────────────────────────────────────
# INDIVIDUAL FILTERS
# ─────────────────────────────────────────────

def filter_age(pair: dict, cfg: dict) -> tuple:
    age_m = _pair_age_minutes(pair)
    if age_m is None:
        return True, [], ["Age unknown — no pairCreatedAt"]

    age_cfg = cfg.get("age", {})
    min_m   = age_cfg.get("min_minutes", 0)
    max_h   = age_cfg.get("max_hours", 9999)
    min_h   = age_cfg.get("min_hours", 0)

    # Strategy A uses minutes, Strategy B uses hours
    effective_min_m = max(min_m, min_h * 60)
    max_m = max_h * 60

    if age_m < effective_min_m:
        return False, [f"Too new ({age_m:.0f}m < {effective_min_m:.0f}m min)"], []
    if age_m > max_m:
        return False, [f"Too old ({age_m/60:.1f}h > {max_h}h max)"], []
    return True, [], []


def filter_market_cap(pair: dict, cfg: dict) -> tuple:
    mc  = _safe(pair.get("marketCap"))
    cfg = cfg.get("market_cap", {})
    if mc <= 0:
        return False, ["No market cap data"], []
    if mc < cfg.get("min", 0):
        return False, [f"MC too low (${mc:,.0f} < ${cfg['min']:,})"], []
    if mc > cfg.get("max", 999_999_999):
        return False, [f"MC too high (${mc:,.0f} > ${cfg['max']:,})"], []
    return True, [], []


def filter_liquidity(pair: dict, cfg: dict) -> tuple:
    liq     = _safe((pair.get("liquidity") or {}).get("usd"))
    mc      = _safe(pair.get("marketCap"))
    liq_cfg = cfg.get("liquidity", {})

    if liq <= 0:
        return False, ["No liquidity"], []
    if liq < liq_cfg.get("min", 0):
        return False, [f"Liq too low (${liq:,.0f})"], []
    if liq > liq_cfg.get("max", 999_999_999):
        return False, [f"Liq too high (${liq:,.0f})"], []

    # Liq/MC ratio
    if mc > 0:
        ratio = (liq / mc) * 100
        min_r = liq_cfg.get("liq_to_mc_min_pct", 0)
        max_r = liq_cfg.get("liq_to_mc_max_pct", 100)
        if ratio < min_r:
            return False, [f"Liq/MC too low ({ratio:.1f}% < {min_r}%)"], []
        if ratio > max_r:
            return False, [f"Liq/MC too high ({ratio:.1f}% > {max_r}%)"], []

    return True, [], []


def filter_volume(pair: dict, cfg: dict) -> tuple:
    vol_cfg   = cfg.get("volume", {})
    txns      = pair.get("txns") or {}
    vol       = pair.get("volume") or {}
    liq       = _safe((pair.get("liquidity") or {}).get("usd"))

    vol_24h = _safe(vol.get("h24"))
    vol_1h  = _safe(vol.get("h1"))
    vol_5m  = _safe(vol.get("m5"))

    if vol_24h < vol_cfg.get("min_24h", 0):
        return False, [f"Vol 24h too low (${vol_24h:,.0f})"], []
    if vol_1h < vol_cfg.get("min_1h", 0):
        return False, [f"Vol 1h too low (${vol_1h:,.0f})"], []
    if vol_5m < vol_cfg.get("min_5m", 0):
        return False, [f"Vol 5m too low (${vol_5m:,.0f})"], []

    # Vol/Liq ratio
    if liq > 0:
        ratio = vol_1h / liq
        if ratio < vol_cfg.get("vol_liq_min_ratio", 0):
            return False, [f"Vol/Liq too low ({ratio:.2f})"], []
        hard = vol_cfg.get("vol_liq_hard_reject", 999)
        if ratio > hard:
            return False, [f"Vol/Liq suspiciously high ({ratio:.1f}) — wash trading?"], []

    return True, [], []


def filter_price_change(pair: dict, cfg: dict) -> tuple:
    pc_cfg = cfg.get("price_change", {})
    pc     = pair.get("priceChange") or {}

    h1  = _safe(pc.get("h1"))
    h24 = _safe(pc.get("h24"))

    if h1 < pc_cfg.get("min_1h_pct", -999):
        return False, [f"1h price change too low ({h1:.1f}%)"], []
    if h24 < pc_cfg.get("min_24h_pct", -999):
        return False, [f"24h price change too low ({h24:.1f}%)"], []
    if h24 > pc_cfg.get("max_24h_pct", 999999):
        return False, [f"24h price change too high ({h24:.1f}%) — likely post-dump"], []

    warnings = []
    pb_min = pc_cfg.get("pullback_from_high_min_pct")
    pb_max = pc_cfg.get("pullback_from_high_max_pct")
    if pb_min and h24 > 0:
        # Approximate pullback from high using h24 and h6
        h6 = _safe(pc.get("h6"))
        if h6 > h24 and pb_min:
            pct_off_high = ((h6 - h24) / (1 + h6 / 100)) * 100 if h6 > 0 else 0
            if pb_max and pct_off_high > pb_max:
                return False, [f"Pullback too deep ({pct_off_high:.0f}% from high)"], []

    return True, [], warnings


def filter_buy_sell(pair: dict, cfg: dict) -> tuple:
    bs_cfg = cfg.get("buy_sell", {})
    txns   = pair.get("txns") or {}
    h1     = txns.get("h1") or {}
    h24    = txns.get("h24") or {}

    buys_1h  = _safe(h1.get("buys"))
    sells_1h = _safe(h1.get("sells"))
    total_1h = buys_1h + sells_1h
    buys_24h = _safe(h24.get("buys"))
    sells_24h = _safe(h24.get("sells"))
    total_24h = buys_24h + sells_24h

    min_tx_1h  = bs_cfg.get("min_tx_count_1h", 0)
    min_tx_24h = bs_cfg.get("min_tx_count_24h", 0)

    if total_1h < min_tx_1h:
        return False, [f"Too few 1h txns ({total_1h:.0f} < {min_tx_1h})"], []
    if min_tx_24h and total_24h < min_tx_24h:
        return False, [f"Too few 24h txns ({total_24h:.0f} < {min_tx_24h})"], []

    if total_1h > 0:
        buy_ratio = buys_1h / total_1h
        min_ratio = bs_cfg.get("min_buy_ratio_1h", 0)
        if buy_ratio < min_ratio:
            return False, [f"Buy ratio too low ({buy_ratio:.2f} < {min_ratio})"], []

    return True, [], []


def filter_security(rc_data: dict, cfg: dict) -> tuple:
    sec_cfg = cfg.get("security", {})
    if not rc_data:
        return False, ["RugCheck data unavailable"], []

    score  = _safe(rc_data.get("score"))
    mint   = rc_data.get("mint_renounced", False)
    freeze = rc_data.get("freeze_renounced", False)
    mutable = rc_data.get("mutable_metadata", False)

    max_score = sec_cfg.get("max_rugcheck_score", 9999)
    if score > max_score:
        return False, [f"RugCheck score too high ({score:.0f} > {max_score})"], []

    if sec_cfg.get("require_mint_renounced") and not mint:
        return False, ["Mint authority not renounced"], []

    if sec_cfg.get("require_freeze_renounced") and not freeze:
        return False, ["Freeze authority not renounced"], []

    if not sec_cfg.get("allow_mutable_metadata") and mutable:
        return False, ["Mutable metadata"], []

    return True, [], []


def filter_holders(rc_data: dict, cfg: dict) -> tuple:
    h_cfg = cfg.get("holders", {})
    if not rc_data:
        return True, [], ["No holder data"]

    holders      = _safe(rc_data.get("total_holders"))
    top10_pct    = _safe(rc_data.get("top10_pct"))
    single_max   = _safe(rc_data.get("max_single_wallet_pct"))
    block0_pct   = _safe(rc_data.get("block0_snipe_pct"))

    warnings = []
    if holders < h_cfg.get("min_holders", 0):
        return False, [f"Too few holders ({holders:.0f})"], []
    if top10_pct > h_cfg.get("max_top10_pct", 100):
        return False, [f"Top 10 too concentrated ({top10_pct:.1f}%)"], []
    if single_max > h_cfg.get("max_single_wallet_pct", 100):
        return False, [f"Single wallet too large ({single_max:.1f}%)"], []
    if block0_pct > h_cfg.get("max_block0_snipe_pct", 100):
        if h_cfg.get("block0_hard_reject"):
            return False, [f"Block0 snipers too high ({block0_pct:.1f}%)"], []
        warnings.append(f"Block0 snipers: {block0_pct:.1f}%")

    return True, [], warnings


# ─────────────────────────────────────────────
# COMPOSITE SCORING
# ─────────────────────────────────────────────

def compute_score(pair: dict, rc_data: dict, cfg: dict) -> dict:
    """
    Returns dict with individual sub-scores and composite.
    All sub-scores are 0.0–1.0.
    """
    weights = cfg.get("scoring", {}).get("weights", {})
    vol     = pair.get("volume") or {}
    txns    = pair.get("txns") or {}
    liq_usd = _safe((pair.get("liquidity") or {}).get("usd"))
    mc      = _safe(pair.get("marketCap"))
    pc      = pair.get("priceChange") or {}

    # Momentum: vol/liq ratio, 5m vol, 1h price change
    vol_1h = _safe(vol.get("h1"))
    vol_5m = _safe(vol.get("m5"))
    vol_24h = _safe(vol.get("h24"))
    h1_pc  = _safe(pc.get("h1"))
    vl_ratio = (vol_1h / liq_usd) if liq_usd > 0 else 0
    momentum_score = min(1.0, (
        min(vl_ratio / 5.0, 0.4) +
        min(vol_5m / 10000, 0.3) +
        min(max(h1_pc, 0) / 50, 0.3)
    ))

    # Liquidity: size and ratio
    liq_score = min(1.0, (
        min(liq_usd / 200000, 0.5) +
        min((liq_usd / mc * 10) if mc > 0 else 0, 0.5)
    ))

    # Buy pressure: buy ratio and tx count
    h1_txns = txns.get("h1") or {}
    buys  = _safe(h1_txns.get("buys"))
    sells = _safe(h1_txns.get("sells"))
    total = buys + sells
    buy_ratio = (buys / total) if total > 0 else 0.5
    buy_score = min(1.0, (
        min((buy_ratio - 0.5) / 0.3, 0.6) +
        min(total / 100, 0.4)
    )) if buy_ratio >= 0.5 else 0.0

    # Safety: rugcheck score inverted
    rc_score_raw = _safe((rc_data or {}).get("score"))
    safety_score = max(0.0, 1.0 - (rc_score_raw / 1000))

    # Holder growth (proxy: total holders)
    holders = _safe((rc_data or {}).get("total_holders"))
    holder_score = min(1.0, holders / 500) if holders else 0.3

    scores = {
        "momentum":     round(momentum_score, 3),
        "liquidity":    round(liq_score, 3),
        "buy_pressure": round(buy_score, 3),
        "safety":       round(safety_score, 3),
        "holder_growth": round(holder_score, 3),
    }

    composite = sum(
        scores.get(k, 0) * v
        for k, v in weights.items()
    )
    scores["composite"] = round(composite, 3)
    return scores


# ─────────────────────────────────────────────
# MAIN FILTER RUNNER
# ─────────────────────────────────────────────

def run_filters(pair: dict, rc_data: dict, strategy_cfg: dict) -> tuple:
    """
    Run all filters for a given strategy config.
    Returns (passed: bool, failures: list, warnings: list, scores: dict)
    """
    failures = []
    warnings = []

    checks = [
        filter_age(pair, strategy_cfg),
        filter_market_cap(pair, strategy_cfg),
        filter_liquidity(pair, strategy_cfg),
        filter_volume(pair, strategy_cfg),
        filter_price_change(pair, strategy_cfg),
        filter_buy_sell(pair, strategy_cfg),
        filter_security(rc_data, strategy_cfg),
        filter_holders(rc_data, strategy_cfg),
    ]

    for passed, fails, warns in checks:
        failures.extend(fails)
        warnings.extend(warns)
        if not passed:
            return False, failures, warnings, {}

    # Scoring
    scoring_cfg = strategy_cfg.get("scoring", {})
    if scoring_cfg.get("enabled", True):
        scores = compute_score(pair, rc_data, strategy_cfg)
        min_score = scoring_cfg.get("min_score", 0.0)
        if scores["composite"] < min_score:
            failures.append(
                f"Score too low ({scores['composite']:.3f} < {min_score})"
            )
            return False, failures, warnings, scores
    else:
        scores = {}

    return True, failures, warnings, scores
