"""
strategies/strategy_c.py — Second Wave strategy (6h–5 days).
Requires evidence of previous pump + reaccumulation.
"""

from strategies.base import BaseStrategy
from data.dexscreener import extract_metrics


class StrategyC(BaseStrategy):
    key   = "strategy_c"
    label = "🌊 SECOND WAVE"

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

        # Second wave detection
        sw_cfg  = self.cfg.get("second_wave", {})
        pc_24h  = float(m.get("pc_24h") or 0)
        pc_1h   = float(m.get("pc_1h") or 0)

        # Must have a previous pump visible in 24h data
        min_pump = float(sw_cfg.get("min_previous_pump_x", 2.0))
        # Proxy: if 24h is still positive but 1h is recovering from a dip
        # (token pumped, corrected, now recovering)
        # More sophisticated detection would require OHLCV data
        if pc_24h < (min_pump - 1) * 100:
            failures.append(
                f"No evidence of previous {min_pump}x pump (24h: {pc_24h:.1f}%)"
            )
            return False, failures, warnings

        # Must not have dumped too hard
        max_dd = float(sw_cfg.get("max_drawdown_from_peak_pct", 60))
        # Use 1h vs 24h delta as drawdown proxy
        if pc_24h > 0 and pc_1h < -(max_dd * 0.5):
            failures.append(f"Drawdown too deep (1h: {pc_1h:.1f}%)")
            return False, failures, warnings

        # Reaccumulation signal: buy/sell ratio recovering
        ra_cfg      = self.cfg.get("reaccumulation", {})
        min_bs      = float(ra_cfg.get("min_buy_sell_ratio_1h", 1.1))
        bs_ratio_1h = float(m.get("buy_sell_ratio_1h", 0))
        if bs_ratio_1h < min_bs:
            failures.append(
                f"No reaccumulation signal (B/S: {bs_ratio_1h:.2f} < {min_bs})"
            )
            return False, failures, warnings

        return True, failures, warnings
