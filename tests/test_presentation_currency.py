"""Presentation currency: nothing is reported without one, and each conversion names its rate."""

import unittest
from decimal import Decimal

from portfolio_research.fx import FxTable
from tests.support.normalization import CADUSD, FX_DAY, observation


class PresentationGuardTests(unittest.TestCase):
    """Without a base currency nothing is treated as already presented."""

    def test_an_amount_is_never_reported_when_there_is_no_presentation_currency(self):
        from portfolio_research.presentation import _convert

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


class PositionPresentationTests(unittest.TestCase):
    """One holding at a time: what its value is taken to be, and what is presented."""

    def context(self, fx=None):
        return {
            "fx": FxTable() if fx is None else fx,
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
    def rates():
        return FxTable([observation("CAD", "USD", "0.7211", FX_DAY, "yahoo")], max_age_days=7)

    @staticmethod
    def position(symbol, quantity, price, market_value, **fields):
        return {
            "account_id": "a1",
            "source_id": 1,
            "snapshot_id": "s1",
            "row_number": 1,
            "raw_symbol": symbol,
            "quantity": quantity,
            "price": price,
            "market_value": market_value,
            "currency": None,
            "valuation_date": None,
            **fields,
        }

    @staticmethod
    def security(quote_currency, factor=1):
        return {"quote_currency": quote_currency, "quote_unit_factor": factor}

    def normalize(self, position, security, account=None, fx=None):
        from portfolio_research.normalize import _normalize_position

        context = self.context(fx)
        row = _normalize_position(position, account or {}, security, context)
        return row, context["issues"]

    def codes(self, issues):
        return [issue["code"] for issue in issues]

    def test_a_cash_balance_does_not_re_denominate_the_holdings_beside_it(self):
        """A CAD cash row says what the cash is, never what a US holding's value is."""
        account = {"account_id": "a1", "cash": "50", "captured_cash_currency": "CAD"}
        row, issues = self.normalize(
            self.position("AAPL", "2", "300", "600"),
            self.security("USD"),
            account=account,
            fx=self.rates(),
        )
        self.assertEqual(row["reported_currency"], "USD")
        self.assertEqual(row["value_currency_basis"], "presentation")
        self.assertEqual(row["market_value_usd"], "600")
        self.assertEqual(row["price_usd"], "300")
        self.assertEqual(issues, [])

    def test_an_attested_account_currency_is_still_honoured(self):
        """Removing the cash fallback must not cost the account its own stated currency."""
        account = {"account_id": "a1", "currency": "CAD", "captured_cash_currency": "USD"}
        row, _ = self.normalize(
            self.position("RY.TO", "10", "285.20", "2852.00"),
            self.security("CAD"),
            account=account,
            fx=self.rates(),
        )
        self.assertEqual(row["reported_currency"], "CAD")
        self.assertEqual(row["value_currency_basis"], "account")
        self.assertEqual(row["market_value_usd"], format(Decimal("2852.00") * CADUSD, "f"))

    def test_a_row_without_a_value_still_presents_its_quoted_price(self):
        """Nothing to imply a price from, so the quoted price converts on its own."""
        row, issues = self.normalize(
            self.position("VFV.TO", "100", "128.00", None),
            self.security("CAD"),
            fx=self.rates(),
        )
        self.assertEqual(row["price_usd"], format(Decimal("128.00") * CADUSD, "f"))
        self.assertIsNone(row["market_value_usd"])
        self.assertEqual(issues, [])

    def test_a_pence_row_without_a_value_presents_the_major_unit_price(self):
        """The subunit is divided out first, or the price lands 100x too high."""
        fx = FxTable([observation("GBP", "USD", "1.3160", FX_DAY, "yahoo")], max_age_days=7)
        row, _ = self.normalize(
            self.position("VOD.L", "100", "128.75", None),
            self.security("GBp", factor=100),
            fx=fx,
        )
        self.assertEqual(row["price_major"], "1.2875")
        self.assertEqual(row["price_usd"], format(Decimal("1.2875") * Decimal("1.3160"), "f"))

    def test_a_currency_that_cannot_be_checked_is_never_counted_at_one_to_one(self):
        """No CAD/USD rate, so an assumed USD value is withheld rather than presented."""
        row, issues = self.normalize(
            self.position("VFV.TO", "100", "128.00", "12800.00"),
            self.security("CAD"),
        )
        self.assertEqual(row["reported_currency"], "USD")
        self.assertEqual(row["reported_market_value"], "12800.00")
        self.assertIsNone(row["market_value_usd"])
        self.assertIsNone(row["price_usd"])
        self.assertIsNone(row["fx_rate"])
        self.assertEqual(self.codes(issues), ["VALUE_CURRENCY_UNCHECKED"])
        self.assertIn("VFV.TO", issues[0]["message"])
        self.assertIn("CAD", issues[0]["message"])

    def test_a_mismatch_that_can_be_checked_warns_but_is_still_presented(self):
        """A rate makes the check possible; what it finds is a data-quality warning."""
        row, issues = self.normalize(
            self.position("VFV.TO", "100", "128.00", "12800.00"),
            self.security("CAD"),
            fx=self.rates(),
        )
        self.assertEqual(self.codes(issues), ["VALUE_ARITHMETIC_MISMATCH"])
        self.assertEqual(row["market_value_usd"], "12800.00")

    def test_a_us_listing_needs_no_rate_and_stays_silent(self):
        """The owner's common path: quoted and valued in USD, checked at parity."""
        row, issues = self.normalize(
            self.position("VOO", "10", "500", "5000"), self.security("USD")
        )
        self.assertEqual(row["quote_currency"], "USD")
        self.assertEqual(row["market_value_usd"], "5000")
        self.assertEqual(issues, [])

    def test_a_holding_with_no_listing_at_all_is_left_alone(self):
        """An unresolvable symbol has no quote currency, so there is nothing to check."""
        row, issues = self.normalize(self.position("ABC.DE", "4", "25", "100"), {})
        self.assertIsNone(row["quote_currency"])
        self.assertEqual(row["reported_currency"], "USD")
        self.assertEqual(row["market_value_usd"], "100")
        self.assertEqual(issues, [])


if __name__ == "__main__":
    unittest.main()
