"""
strategies/strategy_b.py — Momentum strategy (1h–24h).
Focus on velocity and volume acceleration.
"""

from strategies.base import BaseStrategy
from data.dexscreener import extract_metrics


class StrategyB(BaseStrategy):
    key   = "strategy_b"
    label = "⚡ MOMENTUM"

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

        # Momentum-specific: min 1h price change
        mom_cfg = self.cfg.get("momentum", {})
        pc_1h   = float(m.get("pc_1h") or 0)
        min_pc  = float(mom_cfg.get("min_price_change_1h_pct", 0))
        max_pc  = float(mom_cfg.get("max_price_change_1h_pct", 10000))

        if pc_1h < min_pc:
            failures.append(f"1h price change too low ({pc_1h:.1f}%)")
            return False, failures, warnings
        if pc_1h > max_pc:
            warnings.append(f"1h price change very high ({pc_1h:.1f}%) — possible pump")

        # New wallet velocity check
        min_new_w = float(self.cfg.get("pressure", {}).get("min_new_wallets_5m", 0))
        if min_new_w > 0:
            # Approximate with buy tx count 5m as proxy
            if float(m.get("buys_5m", 0)) < min_new_w:
                failures.append(f"Low wallet velocity (buys_5m: {m.get('buys_5m', 0):.0f})")
                return False, failures, warnings

        return True, failures, warnings
