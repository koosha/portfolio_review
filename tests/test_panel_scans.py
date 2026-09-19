"""Per-security work reads the rows it is handed, never the whole price panel again.

Scoring, covariance and pricing each walk a panel that grows with the number of
securities, so a filter written per security would turn one pass into a quadratic one.
These cases count the full-panel boolean masks a call makes and require that count to
stay flat as the panel grows. No network: the panel is generated locally.
"""

import unittest
from unittest.mock import patch

import pandas as pd


class ScanCostTests(unittest.TestCase):
    """Per-security work must not re-scan the whole panel once per security.

    At the review's default scope the comparison carries a thousand screened
    candidates with three years of daily bars. A filter written per security turns that
    into a quadratic pass, so these cases count the full-panel boolean masks each call
    makes and require the count to stay flat as the number of securities grows.
    """

    MASK_FLOOR = 200

    def panel(self, count, sessions=60):
        days = pd.bdate_range(end="2026-09-18", periods=sessions).strftime("%Y-%m-%d")
        prices = pd.DataFrame(
            [
                {
                    "security_id": f"S{index:04d}",
                    "date": day,
                    "close": 100.0 + step,
                    "adjusted_close": 100.0 + step,
                    "volume": 5_000_000,
                    "currency": "USD",
                    "available_at": f"{day}T20:00:00Z",
                    "received_at": f"{day}T20:00:00Z",
                }
                for index in range(count)
                for step, day in enumerate(days)
            ]
        )
        securities_frame = pd.DataFrame(
            [
                {
                    "security_id": f"S{index:04d}",
                    "ticker": f"S{index:04d}",
                    "issuer_id": f"issuer_{index:04d}",
                    "name": f"Issuer {index}",
                    "sector": "Technology",
                    "instrument_type": "equity",
                    "currency": "USD",
                    "domicile": "US",
                    "equity_type": "ordinary_common",
                    "eligible": True,
                    "market_cap": 1e11 - index,
                    "market_cap_as_of": "2026-09-18",
                    "market_cap_available_at": "2026-09-18T20:00:00Z",
                    "market_cap_received_at": "2026-09-18T20:00:00Z",
                }
                for index in range(count)
            ]
        )
        return {
            "as_of": "2026-09-18",
            "securities": securities_frame,
            "prices": prices,
            "fundamentals": pd.DataFrame(),
            "issues": [],
        }

    def config(self):
        from copy import deepcopy

        from portfolio_lab.config import DEFAULTS

        return deepcopy(DEFAULTS)

    def masks(self, run):
        """How many whole-panel boolean masks one call takes."""
        original = pd.DataFrame.__getitem__
        seen = []

        def counting(frame, key):
            if isinstance(key, pd.Series) and key.dtype == bool and len(key) >= self.MASK_FLOOR:
                seen.append(len(key))
            return original(frame, key)

        with patch.object(pd.DataFrame, "__getitem__", counting):
            run()
        return len(seen)

    def test_scoring_does_not_rescan_the_price_panel_per_security(self):
        from portfolio_lab.metrics import score_securities

        config = self.config()
        small, large = self.panel(5), self.panel(40)
        few = self.masks(lambda: score_securities(small, config))
        many = self.masks(lambda: score_securities(large, config))
        self.assertLessEqual(many, few + 4, f"{few} masks at n=5, {many} at n=40")

    def test_covariance_does_not_rescan_the_price_panel_per_security(self):
        from portfolio_lab.metrics import portfolio_covariance

        config = self.config()
        small, large = self.panel(5, sessions=200), self.panel(40, sessions=200)
        few = self.masks(
            lambda: portfolio_covariance(small, config, sorted(small["securities"].security_id))
        )
        many = self.masks(
            lambda: portfolio_covariance(large, config, sorted(large["securities"].security_id))
        )
        self.assertLessEqual(many, few + 4, f"{few} masks at n=5, {many} at n=40")

    def test_known_price_reads_only_the_rows_it_is_handed(self):
        from portfolio_lab import allocation

        bundle = self.panel(40)
        prices = bundle["prices"]
        grouped = dict(tuple(prices.groupby("security_id", sort=False)))
        cutoff = pd.Timestamp("2026-09-18T20:00:00Z")
        config = self.config()
        scans = self.masks(
            lambda: allocation._known_price(
                bundle, config, prices, "S0000", cutoff, grouped["S0000"]
            )
        )
        self.assertEqual(scans, 0)


if __name__ == "__main__":
    unittest.main()
