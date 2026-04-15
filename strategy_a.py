"""
strategies/strategy_a.py — Safe strategy (Raydium / Bonded).
Strictest filters. Best for conservative entries.
"""

from strategies.base import BaseStrategy
from data.dexscreener import extract_metrics


class StrategyA(BaseStrategy):
    key   = "strategy_a"
    label = "🛡️ SAFE"

    def filter(self, pair: dict, rc: dict) -> tuple:
        m = extract_metrics(pair)
        failures = []
        warnings = []

        checks = [
            self._age_filter(pair),
            self._mc_filter(m["mc"]),
            self._liq_filter(m["liq_usd"], m["mc"]),
            self._volume_filter(m),
            self._txn_filter(m),
            self._pressure_filter(m),
            self._security_filter(rc),
            self._holder_filter(rc),
        ]

        for passed, info in checks:
            if not passed:
                failures.extend(info)
                return False, failures, warnings
            elif info:
                warnings.extend(info)

        # LP lock required for strategy A
        liq_cfg = self.cfg.get("liquidity", {})
        if liq_cfg.get("require_lp_locked"):
            locked = float(rc.get("lp_locked_pct") or 0)
            iliq   = float(rc.get("iliq_pct") or 0)
            min_locked = float(liq_cfg.get("min_lp_locked_pct", 0))
            if locked < min_locked and iliq < 95:
                failures.append(f"LP not locked ({locked:.0f}%)")
                return False, failures, warnings

        # Drawdown check
        dd_cfg = self.cfg.get("drawdown", {})
        max_dd = float(dd_cfg.get("max_from_peak_pct", 100))
        pc24   = float(m.get("pc_24h") or 0)
        if pc24 < -(max_dd):
            failures.append(f"Drawdown too deep ({pc24:.1f}%)")
            return False, failures, warnings

        return True, failures, warnings
