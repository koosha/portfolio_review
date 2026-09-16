"""Exposure totals never silently drop a holding that lacks a verified base-currency value."""

import math
import unittest
from copy import deepcopy

import pandas as pd

from portfolio_lab.config import DEFAULTS
from portfolio_lab.metrics import exposures


def bundle(positions):
    config = deepcopy(DEFAULTS)
    config["mandate"]["base_currency"] = "USD"
    securities = [
        {
            "security_id": sid,
            "ticker": sid,
            "issuer_id": sid,
            "sector": "Financials",
            "instrument_type": "equity",
            "currency": "USD",
            "eligible": True,
        }
        for sid in ("A", "B", "C")
    ]
    data = {
        "as_of": "2026-09-11",
        "mode": "offline",
        "issues": [],
        "sources": [],
        "accounts": pd.DataFrame(
            [
                {
                    "account_id": "X",
                    "account_type": "taxable",
                    "currency": "USD",
                    "total_value": 1000.0,
                    "cash": 100.0,
                    "complete": True,
                }
            ]
        ),
        "positions": pd.DataFrame(
            [
                {
                    "account_id": "X",
                    "security_id": sid,
                    "market_value": value,
                    "currency": currency,
                    "valuation_date": "2026-09-11",
                    "quantity": None,
                    "price": None,
                    "reported_weight": None,
                }
                for sid, value, currency in positions
            ]
        ),
        "securities": pd.DataFrame(securities),
        "fund_holdings": pd.DataFrame(
            columns=["fund_id", "issuer_id", "weight", "holdings_date", "available_at"]
        ),
    }
    return data, config


class ExposureCoverageTests(unittest.TestCase):
    def test_base_currency_holdings_raise_no_coverage_warning(self):
        data, config = bundle([("A", 400.0, "USD"), ("B", 500.0, "USD")])
        result = exposures(data, config)
        self.assertNotIn("unconverted_positions", [issue["code"] for issue in result["issues"]])
        self.assertEqual(result["status"], "complete")

    def test_unconverted_holdings_are_counted_in_one_warning(self):
        data, config = bundle([("A", 400.0, "USD"), ("B", math.nan, "CAD"), ("C", 250.0, None)])
        result = exposures(data, config)
        warnings = [issue for issue in result["issues"] if issue["code"] == "unconverted_positions"]
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0]["severity"], "warning")
        self.assertEqual(
            warnings[0]["message"],
            "2 positions without a verified USD value were excluded from exposure totals",
        )
        issuers = {row["issuer_id"] for row in result["issuer_exposure"]}
        self.assertEqual(issuers, {"A"})
        self.assertEqual(result["status"], "incomplete")


if __name__ == "__main__":
    unittest.main()
