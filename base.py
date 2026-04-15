"""
strategies/base.py — Base strategy class.
All strategies inherit from this. Implements the filter pipeline.
"""

import logging
from typing import Optional
from data.dexscreener import extract_metrics, pair_age_minutes

logger = logging.getLogger(__name__)


def _f(v, d=0.0) -> float:
    try:
        return float(v) if v is not None else d
    except (TypeError, ValueError):
        return d


class BaseStrategy:
    key: str   = "base"
    label: str = "BASE"

    def __init__(self, cfg: dict):
        self.cfg = cfg

    @property
    def enabled(self) -> bool:
        return self.cfg.get("enabled", True)

    def filter(self, pair: dict, rc: dict) -> tuple[bool, list, list]:
        """
        Run all filters.
        Returns (passed, failures, warnings).
        """
        raise NotImplementedError

    def _age_filter(self, pair: dict) -> tuple[bool, list]:
        age_m   = _f(pair_age_minutes(pair))
        age_cfg = self.cfg.get("age", {})
        min_m   = _f(age_cfg.get("min_minutes", 0))
        max_h   = _f(age_cfg.get("max_hours", age_cfg.get("max_days", 9999) * 24))
        min_h   = _f(age_cfg.get("min_hours", 0))
        min_m   = max(min_m, min_h * 60)
        max_m   = max_h * 60

        if age_m < min_m:
            return False, [f"Too new ({age_m:.0f}m < {min_m:.0f}m)"]
        if age_m > max_m:
            return False, [f"Too old ({age_m/60:.1f}h > {max_h:.0f}h)"]
        return True, []

    def _mc_filter(self, mc: float) -> tuple[bool, list]:
        cfg = self.cfg.get("market_cap", {})
        mn  = _f(cfg.get("min_usd", 0))
        mx  = _f(cfg.get("max_usd", 999_999_999))
        if mc < mn:
            return False, [f"MC too low (${mc:,.0f})"]
        if mc > mx:
            return False, [f"MC too high (${mc:,.0f})"]
        return True, []

    def _liq_filter(self, liq: float, mc: float) -> tuple[bool, list]:
        cfg = self.cfg.get("liquidity", {})
        mn  = _f(cfg.get("min_usd", 0))
        if liq < mn:
            return False, [f"Liq too low (${liq:,.0f})"]
        min_ratio = _f(cfg.get("liq_to_mc_min_pct", 0))
        if mc > 0 and min_ratio > 0:
            ratio = liq / mc * 100
            if ratio < min_ratio:
                return False, [f"Liq/MC too low ({ratio:.1f}% < {min_ratio}%)"]
        return True, []

    def _volume_filter(self, m: dict) -> tuple[bool, list]:
        cfg = self.cfg.get("volume", {})
        for field, key in [
            ("min_5m_usd",  "vol_5m"),
            ("min_15m_usd", "vol_15m"),
            ("min_1h_usd",  "vol_1h"),
            ("min_24h_usd", "vol_24h"),
        ]:
            threshold = _f(cfg.get(field, 0))
            if threshold > 0 and _f(m.get(key)) < threshold:
                return False, [f"Vol {key} too low (${_f(m.get(key)):,.0f})"]
        return True, []

    def _txn_filter(self, m: dict) -> tuple[bool, list]:
        cfg = self.cfg.get("transactions", {})
        for field, key in [
            ("min_5m",  "total_5m"),
            ("min_15m", "total_15m"),
            ("min_1h",  "total_1h"),
        ]:
            threshold = _f(cfg.get(field, 0))
            if threshold > 0 and _f(m.get(key)) < threshold:
                return False, [f"Txns {key} too low ({_f(m.get(key)):.0f})"]
        return True, []

    def _pressure_filter(self, m: dict) -> tuple[bool, list]:
        cfg   = self.cfg.get("pressure", {})
        ratio = _f(m.get("buy_sell_ratio_1h", 1.0))
        mn    = _f(cfg.get("min_buy_sell_ratio", 0))
        if mn > 0 and ratio < mn:
            return False, [f"Buy/sell ratio too low ({ratio:.2f} < {mn})"]
        return True, []

    def _security_filter(self, rc: dict) -> tuple[bool, list]:
        cfg   = self.cfg.get("security", {})
        warns = []

        score = _f(rc.get("score", 9999))
        mx    = _f(cfg.get("max_rugcheck_score", 800))
        if score > mx:
            return False, [f"RugCheck score too high ({score:.0f} > {mx:.0f})"]

        if cfg.get("require_mint_renounced") and not rc.get("mint_renounced"):
            return False, ["Mint authority not renounced"]
        if cfg.get("require_freeze_renounced") and not rc.get("freeze_renounced"):
            return False, ["Freeze authority not renounced"]
        if not cfg.get("allow_mutable_metadata") and rc.get("mutable_metadata"):
            return False, ["Mutable metadata"]

        return True, warns

    def _holder_filter(self, rc: dict) -> tuple[bool, list]:
        cfg     = self.cfg.get("holders", {})
        holders = _f(rc.get("total_holders", 0))
        top10   = _f(rc.get("top10_pct", 0))
        b0      = _f(rc.get("block0_snipe_pct", 0))

        mn = _f(cfg.get("min_count", 0))
        if mn > 0 and holders < mn:
            return False, [f"Too few holders ({holders:.0f} < {mn:.0f})"]

        mx_top10 = _f(cfg.get("max_top_holder_pct", 100))
        if top10 > mx_top10:
            return False, [f"Top10 too concentrated ({top10:.1f}% > {mx_top10}%)"]

        mx_b0 = _f(cfg.get("max_block0_snipe_pct", 100))
        if b0 > mx_b0:
            return False, [f"Block0 snipers too high ({b0:.1f}% > {mx_b0}%)"]

        return True, []
