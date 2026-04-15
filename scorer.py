"""
scoring/scorer.py — Composite token scorer. Returns 0–100.

Components:
  liquidity_score      (default 0.20)
  volume_score         (default 0.20)
  momentum_score       (default 0.25)
  holder_distribution  (default 0.15)
  wallet_activity      (default 0.10)
  risk_score           (default 0.10)

Weights are overridable per strategy.
"""

import math
import logging
from typing import Optional

logger = logging.getLogger(__name__)


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


def _f(v) -> float:
    try:
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


# ─────────────────────────────────────────────
# SUB-SCORES (each returns 0.0–1.0)
# ─────────────────────────────────────────────

def liquidity_score(liq_usd: float, mc: float) -> float:
    """Higher liquidity and healthier liq/MC ratio = higher score."""
    if liq_usd <= 0:
        return 0.0
    # Absolute liquidity component
    liq_component = _clamp(math.log10(max(liq_usd, 1)) / math.log10(500_000), 0, 1)
    # Liq/MC ratio component
    ratio = (liq_usd / mc * 100) if mc > 0 else 0
    ratio_component = _clamp(ratio / 25, 0, 1)
    return round(liq_component * 0.6 + ratio_component * 0.4, 4)


def volume_score(vol_5m: float, vol_1h: float, vol_24h: float,
                  liq_usd: float) -> float:
    """Volume velocity and vol/liq ratio."""
    if liq_usd <= 0:
        return 0.0
    vol_liq_ratio = vol_1h / liq_usd
    # Vol/Liq component — sweet spot 1.5–5x
    if vol_liq_ratio >= 5:
        vl_comp = 0.9   # potential wash trading, slight penalty
    elif vol_liq_ratio >= 3:
        vl_comp = 1.0
    elif vol_liq_ratio >= 1.5:
        vl_comp = 0.8
    elif vol_liq_ratio >= 0.5:
        vl_comp = 0.5
    else:
        vl_comp = vol_liq_ratio / 1.5 * 0.5

    # 5m acceleration — is it building?
    accel = (vol_5m * 12) / vol_1h if vol_1h > 0 else 0  # annualized 5m vs 1h
    accel_comp = _clamp(accel / 2.0, 0, 1)

    return round(vl_comp * 0.7 + accel_comp * 0.3, 4)


def momentum_score(pc_5m: Optional[float], pc_1h: Optional[float],
                    pc_24h: Optional[float],
                    buy_sell_ratio_1h: float,
                    total_txns_1h: float) -> float:
    """Price velocity + buy pressure."""
    p1h  = _f(pc_1h)
    p24h = _f(pc_24h)
    p5m  = _f(pc_5m)

    # 1h price change component
    if p1h >= 100:
        pc_comp = 1.0
    elif p1h >= 50:
        pc_comp = 0.85
    elif p1h >= 20:
        pc_comp = 0.70
    elif p1h >= 5:
        pc_comp = 0.50
    elif p1h > 0:
        pc_comp = 0.30
    elif p1h >= -10:
        pc_comp = 0.20   # small pullback may be entry
    else:
        pc_comp = max(0, 0.20 + p1h / 100)

    # Buy/sell pressure
    bs_comp = _clamp((buy_sell_ratio_1h - 1.0) / 1.0, 0, 1)

    # Transaction velocity
    tx_comp = _clamp(total_txns_1h / 800, 0, 1)

    return round(pc_comp * 0.50 + bs_comp * 0.30 + tx_comp * 0.20, 4)


def holder_distribution_score(holders: int, top10_pct: Optional[float],
                                block0_pct: Optional[float],
                                max_single_pct: Optional[float]) -> float:
    """More holders, more distributed = better score."""
    if holders <= 0:
        return 0.0

    # Holder count
    h_comp = _clamp(math.log10(max(holders, 1)) / math.log10(5000), 0, 1)

    # Top10 concentration (lower is better, 40% is sweet spot)
    t10 = _f(top10_pct) if top10_pct is not None else 50
    t10_comp = _clamp(1.0 - (t10 / 100), 0, 1)

    # Block0 sniper penalty
    b0  = _f(block0_pct) if block0_pct is not None else 0
    b0_comp = _clamp(1.0 - (b0 / 50), 0, 1)

    return round(h_comp * 0.4 + t10_comp * 0.4 + b0_comp * 0.2, 4)


def wallet_activity_score(wallet_alert_count: int = 0,
                            top_wallet_score: float = 0.0) -> float:
    """Score based on tracked smart wallet activity on this token."""
    if wallet_alert_count == 0:
        return 0.3   # neutral baseline
    count_comp  = _clamp(wallet_alert_count / 5, 0, 1)
    quality_comp = _clamp(top_wallet_score, 0, 1)
    return round(count_comp * 0.5 + quality_comp * 0.5, 4)


def risk_score(rugcheck_score: float, mint_renounced: bool,
               freeze_renounced: bool, mutable_metadata: bool,
               lp_locked_pct: float) -> float:
    """Security posture. Returns 0–1 (higher = less risky)."""
    base = 1.0

    # RugCheck score penalty
    if rugcheck_score > 1000:
        base -= 0.4
    elif rugcheck_score > 500:
        base -= 0.2
    elif rugcheck_score > 200:
        base -= 0.1

    if not mint_renounced:
        base -= 0.2
    if not freeze_renounced:
        base -= 0.2
    if mutable_metadata:
        base -= 0.1

    # LP lock bonus
    if lp_locked_pct >= 80:
        pass   # no penalty
    elif lp_locked_pct >= 50:
        base -= 0.05
    else:
        base -= 0.15

    return round(_clamp(base, 0, 1), 4)


# ─────────────────────────────────────────────
# COMPOSITE
# ─────────────────────────────────────────────

def compute(metrics: dict, rc: dict, strategy_cfg: dict,
             wallet_alert_count: int = 0,
             top_wallet_score: float = 0.0) -> dict:
    """
    Compute composite score 0–100.
    Returns dict with all sub-scores and composite.
    """
    s_cfg = strategy_cfg.get("scoring", {})

    weights = {
        "liquidity":    _f(s_cfg.get("liquidity_weight",             0.20)),
        "volume":       _f(s_cfg.get("volume_weight",                0.20)),
        "momentum":     _f(s_cfg.get("momentum_weight",              0.25)),
        "holder":       _f(s_cfg.get("holder_distribution_weight",   0.15)),
        "wallet":       _f(s_cfg.get("wallet_activity_weight",       0.10)),
        "risk":         _f(s_cfg.get("risk_weight",                  0.10)),
    }

    s_liq  = liquidity_score(metrics["liq_usd"], metrics["mc"])
    s_vol  = volume_score(metrics["vol_5m"], metrics["vol_1h"],
                           metrics["vol_24h"], metrics["liq_usd"])
    s_mom  = momentum_score(metrics.get("pc_5m"), metrics.get("pc_1h"),
                             metrics.get("pc_24h"),
                             metrics["buy_sell_ratio_1h"],
                             metrics["total_1h"])
    s_hold = holder_distribution_score(
        rc.get("total_holders", 0),
        rc.get("top10_pct"),
        rc.get("block0_snipe_pct"),
        rc.get("max_single_wallet_pct"),
    )
    s_wall = wallet_activity_score(wallet_alert_count, top_wallet_score)
    s_risk = risk_score(
        rc.get("score", 9999),
        rc.get("mint_renounced", False),
        rc.get("freeze_renounced", False),
        rc.get("mutable_metadata", True),
        rc.get("lp_locked_pct", 0),
    )

    composite_01 = (
        s_liq  * weights["liquidity"] +
        s_vol  * weights["volume"]    +
        s_mom  * weights["momentum"]  +
        s_hold * weights["holder"]    +
        s_wall * weights["wallet"]    +
        s_risk * weights["risk"]
    )

    # Normalize to 0–100
    composite = round(_clamp(composite_01) * 100, 1)

    return {
        "liquidity":          round(s_liq  * 100, 1),
        "volume":             round(s_vol  * 100, 1),
        "momentum":           round(s_mom  * 100, 1),
        "holder_distribution": round(s_hold * 100, 1),
        "wallet_activity":    round(s_wall * 100, 1),
        "risk":               round(s_risk * 100, 1),
        "composite":          composite,
    }


def tier(score: float) -> str:
    if score >= 80:
        return "🔥 STRONG"
    if score >= 65:
        return "✅ GOOD"
    if score >= 50:
        return "⚠️ MODERATE"
    return "❌ WEAK"
