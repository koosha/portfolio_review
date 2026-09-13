import unittest

from portfolio.storage import holding_summary

CAPTURE = {"method": "yahoo-holdings-table-v1", "completeness": "end-observed"}


def row(value=None, symbol="DEMO", quantity="1", price=None, currency=None):
    return dict(
        symbol=symbol, quantity=quantity, market_value=value, price=price, currency=currency
    )


class TotalTests(unittest.TestCase):
    def test_total_includes_the_observed_yahoo_cash_row(self):
        result = holding_summary(
            [row("100.01"), row(symbol="Total Cash", quantity=None, price="7.25")], CAPTURE
        )
        self.assertEqual(result["total_market_value"], "107.26")
        self.assertEqual(result["groups"][0]["cash"], "7.25")
        self.assertEqual(result["missing_market_values"], 0)
        self.assertFalse(result["completeness_verified"])

    def test_cash_only_account_is_not_zero_or_missing(self):
        result = holding_summary([row(symbol="Total Cash", quantity=None, price="0.03")], CAPTURE)
        self.assertEqual(result["total_market_value"], "0.03")

    def test_missing_market_values_remain_explicitly_partial(self):
        result = holding_summary([row("10"), row()], CAPTURE)
        self.assertEqual(result["total_market_value"], "10")
        self.assertEqual(result["missing_market_values"], 1)
        self.assertIsNone(holding_summary([row()], CAPTURE)["total_market_value"])

    def test_different_explicit_currencies_are_not_combined(self):
        result = holding_summary([row("10", currency="USD"), row("20", currency="CAD")], CAPTURE)
        self.assertIsNone(result["total_market_value"])
        self.assertEqual(len(result["groups"]), 2)

    def test_values_are_summed_exactly_without_binary_or_decimal_context_rounding(self):
        result = holding_summary(
            [row("100000000000000000000000000000.01"), row("0.02"), row("-0.01")], CAPTURE
        )
        self.assertEqual(result["total_market_value"], "100000000000000000000000000000.02")

    def test_cash_with_market_value_is_not_double_counted(self):
        result = holding_summary(
            [row("7.25", symbol="Total Cash", quantity=None, price="7.25")], CAPTURE
        )
        self.assertEqual(result["total_market_value"], "7.25")
