"""Enriched close series: quote-unit labelling and one conversion at each row's own date."""

import unittest

import pandas as pd

from portfolio_research.fx import FxTable
from tests.support.normalization import observation


class PresentPricesTests(unittest.TestCase):
    """Enriched closes are labelled with the listing's quote unit before conversion."""

    def bundle(self, currency="GBP"):
        return {
            "ledger": {
                "securities": [
                    {
                        "security_id": "VOD.L",
                        "currency": "GBP",
                        "quote_currency": "GBp",
                        "quote_unit_factor": 100,
                    }
                ]
            },
            "prices": pd.DataFrame(
                {
                    "security_id": ["VOD.L", "VOD.L"],
                    "date": ["2026-09-10", "2026-09-11"],
                    "close": [128.75, 130.00],
                    "adjusted_close": [128.75, 130.00],
                    "currency": [currency, currency],
                }
            ),
            "issues": [],
        }

    def table(self):
        return FxTable(
            [
                observation("GBP", "USD", "1.30", "2026-09-10", "yahoo"),
                observation("GBP", "USD", "1.31", "2026-09-11", "yahoo"),
            ],
            max_age_days=7,
        )

    def test_pence_closes_convert_once_at_each_row_date(self):
        from portfolio_research.normalize import present_prices

        config = {"mandate": {"base_currency": "USD"}}
        result = present_prices(self.bundle(), config, self.table())
        prices = result["prices"]
        self.assertEqual(list(prices["currency"]), ["USD", "USD"])
        self.assertEqual(list(prices["local_currency"]), ["GBp", "GBp"])
        self.assertAlmostEqual(prices["close"][0], 128.75 / 100 * 1.30, places=10)
        self.assertAlmostEqual(prices["close"][1], 130.00 / 100 * 1.31, places=10)
        self.assertEqual(list(prices["fx_observation_date"]), ["2026-09-10", "2026-09-11"])
        self.assertEqual(result["issues"], [])

    def test_no_base_currency_leaves_the_series_untouched(self):
        from portfolio_research.normalize import present_prices

        bundle = self.bundle()
        result = present_prices(bundle, {"mandate": {"base_currency": None}}, self.table())
        self.assertEqual(list(result["prices"]["currency"]), ["GBP", "GBP"])


if __name__ == "__main__":
    unittest.main()
