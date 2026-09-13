import json
import math
import unittest
from copy import deepcopy

from portfolio_research.valuation import (
    calculate_dcf,
    calculate_eps,
    dcf_sensitivity,
    eps_sensitivity,
)


def eps_input():
    return {
        "starting_price": 100,
        "currency": "USD",
        "horizon_months": 12,
        "eps_convention": "forward",
        "pe_convention": "forward",
        "scenarios": [
            {"label": "adverse", "eps": 4, "pe": 15, "distributions_per_starting_share": 1},
            {"label": "central", "eps": 5.5, "pe": 20, "distributions_per_starting_share": 1},
            {"label": "favorable", "eps": 6, "pe": 25, "distributions_per_starting_share": 1},
        ],
    }


def dcf_input():
    # NOPAT=100, reinvestment=20 and FCFF=80 in both years. Terminal FCFF
    # is 100*1.02*(1-.02/.10)=81.6; EV=(80/1.1)+(80+1020)/1.21=10800/11.
    return {
        "currency": "USD",
        "monetary_unit": "units",
        "company_type": "nonfinancial",
        "discount_rate": 0.10,
        "terminal_growth_rate": 0.02,
        "terminal_roic": 0.10,
        "sbc_treatment": "expensed_in_ebit",
        "debt": 100,
        "preferred": 20,
        "nci": 10,
        "excess_cash": 40,
        "nonoperating_assets": 5,
        "diluted_shares": 10,
        "projections": [
            {
                "year": year,
                "revenue": 500,
                "ebit": 125,
                "tax_rate": 0.20,
                "depreciation": 10,
                "capex": 30,
                "change_working_capital": 0,
                "invested_capital": capital,
                "stock_compensation": 5,
            }
            for year, capital in ((1, 500), (2, 520))
        ],
    }


class EPSValuationTests(unittest.TestCase):
    def test_independent_price_and_cash_distribution_bridge(self):
        result = calculate_eps(eps_input())
        self.assertEqual(result["status"], "ready")
        self.assertEqual([s["horizon_price"] for s in result["scenarios"]], [60, 110, 150])
        self.assertAlmostEqual(result["scenarios"][1]["total_return"], 0.11)
        self.assertFalse(result["calibrated"])

    def test_six_month_return_is_not_silently_annualized(self):
        data = eps_input()
        data["horizon_months"] = 6
        self.assertAlmostEqual(calculate_eps(data)["scenarios"][1]["total_return"], 0.11)

    def test_missing_distribution_is_not_zero(self):
        data = eps_input()
        del data["scenarios"][1]["distributions_per_starting_share"]
        result = calculate_eps(data)
        self.assertEqual(result["status"], "partial")
        self.assertIsNone(result["scenarios"][1]["total_return"])
        data["scenarios"][1]["distributions_per_starting_share"] = 0
        self.assertAlmostEqual(calculate_eps(data)["scenarios"][1]["total_return"], 0.1)

    def test_buybacks_cannot_be_added_twice(self):
        for field in ("buyback_yield", "buybacks_per_share", "buyback_return"):
            data = eps_input()
            data["scenarios"][1][field] = 0.03
            with self.assertRaisesRegex(ValueError, "Buybacks"):
                calculate_eps(data)

    def test_mismatched_multiple_convention_is_rejected(self):
        data = eps_input()
        data["pe_convention"] = "trailing"
        with self.assertRaisesRegex(ValueError, "matching"):
            calculate_eps(data)

    def test_negative_earnings_are_unavailable_not_negative_share_prices(self):
        data = eps_input()
        data["scenarios"][0]["eps"] = -3
        result = calculate_eps(data)["scenarios"][0]
        self.assertEqual(result["eps"], -3)
        self.assertIsNone(result["horizon_price"])

    def test_invalid_numeric_values_and_duplicate_labels(self):
        for bad in (0, -1, True, math.nan, math.inf, "not a number"):
            data = eps_input()
            data["starting_price"] = bad
            with self.assertRaises(ValueError):
                calculate_eps(data)
        data = eps_input()
        data["scenarios"].append(data["scenarios"][0])
        with self.assertRaises(ValueError):
            calculate_eps(data)

    def test_sensitivity_uses_same_distribution_and_preserves_inputs(self):
        data = eps_input()
        original = deepcopy(data)
        grid = eps_sensitivity(data, [4, 5.5], [15, 20])
        self.assertEqual(grid["cells"][0][0]["horizon_price"], 60)
        self.assertAlmostEqual(grid["cells"][1][1]["total_return"], 0.11)
        self.assertEqual(data, original)
        with self.assertRaises(ValueError):
            eps_sensitivity(data, [None], [20])

    def test_sensitivity_does_not_erase_currency_mismatch(self):
        data = eps_input()
        data["scenarios"][1]["currency"] = "CAD"
        grid = eps_sensitivity(data, [5.5], [20])
        self.assertIsNone(grid["cells"][0][0]["horizon_price"])


class DCFValuationTests(unittest.TestCase):
    def test_independent_fcff_discounting_and_every_equity_bridge_claim(self):
        result = calculate_dcf(dcf_input())
        self.assertEqual(result["status"], "ready")
        self.assertEqual([row["fcff"] for row in result["projections"]], [80, 80])
        self.assertAlmostEqual(result["terminal_reinvestment_rate"], 0.2)
        self.assertAlmostEqual(result["terminal_fcff"], 81.6)
        self.assertAlmostEqual(result["terminal_value"], 1020)
        self.assertAlmostEqual(result["enterprise_value"], 10800 / 11)
        self.assertAlmostEqual(result["common_equity_value"], 9865 / 11)
        self.assertAlmostEqual(result["value_per_share"], 9865 / 110)
        self.assertIsNone(result["horizon_total_return"])

    def test_units_are_scaled_and_share_count_is_explicit(self):
        data = dcf_input()
        data.update(monetary_unit="millions", diluted_shares=10_000_000)
        self.assertAlmostEqual(calculate_dcf(data)["value_per_share"], 9865 / 110)

    def test_growth_requires_reinvestment_and_capital_reconciliation(self):
        data = dcf_input()
        data["projections"][1]["invested_capital"] = 500
        with self.assertRaisesRegex(ValueError, "reconcile"):
            calculate_dcf(data)
        data = dcf_input()
        data["projections"][0]["roic"] = 0.5
        with self.assertRaisesRegex(ValueError, "ROIC differs"):
            calculate_dcf(data)

    def test_stock_compensation_deducted_once_with_equivalent_ebit_inputs(self):
        before_sbc = dcf_input()
        before_sbc["sbc_treatment"] = "deduct_from_ebit"
        after_sbc = dcf_input()
        for row in after_sbc["projections"]:
            row["ebit"] -= row["stock_compensation"]
        deducted = calculate_dcf(before_sbc)
        expensed = calculate_dcf(after_sbc)
        self.assertEqual(deducted["enterprise_value"], expensed["enterprise_value"])
        self.assertEqual(deducted["projections"][0]["fcff"], 76)
        self.assertLess(
            deducted["enterprise_value"], calculate_dcf(dcf_input())["enterprise_value"]
        )

    def test_unknown_bridge_values_currency_or_compensation_block_outputs(self):
        for field in (
            "debt",
            "preferred",
            "nci",
            "excess_cash",
            "nonoperating_assets",
            "diluted_shares",
            "currency",
            "monetary_unit",
            "sbc_treatment",
        ):
            data = dcf_input()
            del data[field]
            result = calculate_dcf(data)
            self.assertEqual(result["status"], "blocked", field)
            self.assertIsNone(result["value_per_share"], field)
        data = dcf_input()
        data["projections"][0]["stock_compensation"] = None
        self.assertIsNone(calculate_dcf(data)["enterprise_value"])

    def test_mixed_currency_is_not_silently_combined(self):
        for field in ("bridge_currency", "discount_currency"):
            data = dcf_input()
            data[field] = "CAD"
            self.assertIsNone(calculate_dcf(data)["value_per_share"])
        data = dcf_input()
        data["projections"][1]["currency"] = "EUR"
        self.assertIsNone(calculate_dcf(data)["enterprise_value"])
        data = dcf_input()
        data["projections"][1]["monetary_unit"] = "millions"
        self.assertIsNone(calculate_dcf(data)["enterprise_value"])
        data = dcf_input()
        data["share_unit"] = "millions"
        with self.assertRaises(ValueError):
            calculate_dcf(data)

    def test_invalid_terminal_tax_share_and_numeric_assumptions(self):
        for field, bad in (
            ("terminal_growth_rate", 0.1),
            ("terminal_growth_rate", -1),
            ("terminal_roic", 0),
            ("terminal_roic", 0.01),
            ("discount_rate", 0),
            ("diluted_shares", -1),
            ("debt", math.nan),
            ("preferred", math.inf),
        ):
            data = dcf_input()
            data[field] = bad
            with self.assertRaises(ValueError, msg=field):
                calculate_dcf(data)
        data = dcf_input()
        data["projections"][0]["tax_rate"] = 1.01
        with self.assertRaises(ValueError):
            calculate_dcf(data)

    def test_inappropriate_company_or_nonpositive_terminal_earnings_stays_blocked(self):
        data = dcf_input()
        data["company_type"] = "bank"
        self.assertIsNone(calculate_dcf(data)["value_per_share"])
        data = dcf_input()
        data["projections"][-1]["ebit"] = -10
        self.assertIsNone(calculate_dcf(data)["terminal_value"])

    def test_overlevered_bridge_does_not_display_negative_share_price(self):
        data = dcf_input()
        data["debt"] = 2000
        result = calculate_dcf(data)
        self.assertLess(result["common_equity_value"], 0)
        self.assertIsNone(result["value_per_share"])

    def test_sensitivity_retains_invalid_cells_and_higher_discount_reduces_value(self):
        data = dcf_input()
        original = deepcopy(data)
        grid = dcf_sensitivity(data, [0.08, 0.10, 0.12], [0.02, 0.10])
        self.assertEqual(grid["cells"][0][1]["status"], "invalid")
        self.assertIsNone(grid["cells"][1][1]["value_per_share"])
        self.assertGreater(
            grid["cells"][0][0]["value_per_share"], grid["cells"][2][0]["value_per_share"]
        )
        self.assertEqual(data, original)

    def test_empty_inputs_and_results_are_json_finite(self):
        self.assertEqual(calculate_eps({})["status"], "blocked")
        self.assertEqual(calculate_dcf({})["status"], "blocked")
        json.dumps(calculate_dcf(dcf_input()), allow_nan=False)


if __name__ == "__main__":
    unittest.main()
