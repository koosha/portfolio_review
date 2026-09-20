"""Presentation currency: nothing is reported without one, and each conversion names its rate."""

import unittest
from decimal import Decimal

from portfolio_research.fx import FxTable
from tests.support.normalization import CADUSD, FX_DAY, observation


class PresentationGuardTests(unittest.TestCase):
    """Without a base currency nothing is treated as already presented."""

    def test_an_amount_is_never_reported_when_there_is_no_presentation_currency(self):
        from portfolio_research.normalize import _convert

        context = {"presentation": None, "fx": FxTable(), "used": set(), "fx_exceptions": {}}
        for currency in (None, "CAD", "USD"):
            amount, fields, status = _convert("50", currency, FX_DAY, context)
            self.assertIsNone(amount, currency)
            self.assertEqual(status, "unknown_currency", currency)
            self.assertEqual(fields, {})
        self.assertEqual(context["fx_exceptions"], {})


class AccountFxEvidenceTests(unittest.TestCase):
    """A cash conversion and a NAV conversion are evidenced by their own rates."""

    def context(self):
        return {
            "fx": FxTable(
                [
                    observation("EUR", "USD", "1.1000", FX_DAY, "yahoo"),
                    observation("CAD", "USD", "0.7211", FX_DAY, "yahoo"),
                ],
                max_age_days=7,
            ),
            "presentation": "USD",
            "current": True,
            "receipt_day": FX_DAY,
            "as_of": FX_DAY,
            "used": set(),
            "fx_exceptions": {},
            "exceptions": [],
            "issues": [],
        }

    @staticmethod
    def account():
        return {
            "account_id": "a1",
            "source_id": 1,
            "snapshot_id": "s1",
            "name": "Euro broker",
            "currency": "EUR",
            "cash": "50",
            "cash_basis": "captured",
            "captured_cash_currency": "CAD",
            "total_value": "1000",
            "valuation_date": FX_DAY,
        }

    def test_the_nav_rate_is_recorded_beside_the_nav_and_the_cash_rate_beside_the_cash(self):
        from portfolio_research.normalize import _normalize_account

        row, cash, unconverted = _normalize_account(self.account(), [], self.context())
        self.assertFalse(unconverted)
        self.assertEqual(row["reported_currency"], "EUR")
        self.assertEqual(row["cash_currency"], "CAD")
        self.assertEqual(row["total_value_usd"], format(Decimal("1000") * Decimal("1.1000"), "f"))
        self.assertEqual(row["fx_pair"], "EURUSD")
        self.assertEqual(row["fx_rate"], "1.1000")
        self.assertEqual(row["fx_observation_date"], FX_DAY)
        self.assertIn("EURUSD", row["fx_source_id"])
        self.assertEqual(cash, format(Decimal("50") * CADUSD, "f"))
        self.assertEqual(row["cash_fx_pair"], "CADUSD")
        self.assertEqual(row["cash_fx_rate"], "0.7211")
        self.assertEqual(row["cash_fx_observation_date"], FX_DAY)
        self.assertIn("CADUSD", row["cash_fx_source_id"])

    def test_one_currency_records_the_same_rate_in_both_places(self):
        from portfolio_research.normalize import _normalize_account

        account = {**self.account(), "captured_cash_currency": "EUR"}
        row, _, _ = _normalize_account(account, [], self.context())
        self.assertEqual(row["fx_pair"], "EURUSD")
        self.assertEqual(row["cash_fx_pair"], "EURUSD")

    def test_a_presented_account_currency_leaves_the_cash_fields_empty(self):
        from portfolio_research.normalize import _normalize_account

        account = {**self.account(), "currency": "USD", "captured_cash_currency": "USD"}
        row, _, _ = _normalize_account(account, [], self.context())
        for field in ("fx_pair", "fx_rate", "cash_fx_pair", "cash_fx_rate"):
            self.assertIsNone(row[field], field)


if __name__ == "__main__":
    unittest.main()
