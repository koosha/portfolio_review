"""Adapter fundamentals: statement periods, availability, restatements and consensus estimates."""

import unittest

from portfolio_research.market_data import ADAPTER_VERSION, estimates, statements
from tests.support.market_data_adapter import AdapterCase, FakeTicker


class StatementTests(AdapterCase):
    def test_ttm_row_sums_four_quarters_with_positive_capex_and_earlier_assets(self):
        result = self.call(statements, "AAPL")
        ttm = result["ttm"]
        self.assertEqual(ttm["period_end"], "2026-06-30")
        self.assertEqual(ttm["period_type"], "ttm")
        self.assertAlmostEqual(ttm["revenue"], 104 + 103 + 102 + 101)
        self.assertAlmostEqual(ttm["operating_income"], 34 + 33 + 32 + 31)
        self.assertAlmostEqual(ttm["income_common"], 23 + 22 + 21 + 20)
        self.assertAlmostEqual(ttm["capex"], 9 + 8 + 7 + 6)
        self.assertAlmostEqual(ttm["operating_cash_flow"], 29 + 28 + 27 + 26)
        self.assertAlmostEqual(ttm["assets"], 1040.0)
        self.assertAlmostEqual(ttm["assets_begin"], 1000.0)
        self.assertEqual(ttm["currency"], "USD")
        self.assertEqual(ttm["security_id"], "AAPL")
        self.assertIn(ttm, result["rows"])

    def test_annual_and_quarterly_rows_use_the_fundamentals_column_shape(self):
        from portfolio_lab.providers import FRAME_COLUMNS

        result = self.call(statements, "AAPL")
        annual = [row for row in result["annual"] if row["period_end"] == "2025-12-31"][0]
        self.assertEqual(annual["period_type"], "annual")
        self.assertAlmostEqual(annual["revenue"], 400.0)
        self.assertAlmostEqual(annual["capex"], 25.0)
        self.assertAlmostEqual(annual["assets_begin"], 960.0)
        self.assertEqual(len(result["quarterly"]), 5)
        for row in result["rows"]:
            self.assertTrue(set(FRAME_COLUMNS["fundamentals"]).issubset(row))
            self.assertEqual(row["adapter_version"], ADAPTER_VERSION)

    def test_available_at_uses_the_filing_date_when_one_covers_the_period(self):
        result = self.call(statements, "AAPL")
        quarter = [row for row in result["quarterly"] if row["period_end"] == "2026-06-30"][0]
        self.assertEqual(quarter["available_at"], "2026-08-01")
        self.assertEqual(quarter["availability_basis"], "filing_date")
        self.assertEqual(quarter["earnings_definition"], "common_shareholders")

    def test_available_at_falls_back_to_the_assumed_publication_lag(self):
        result = self.call(statements, "VOD.L")
        quarter = [row for row in result["quarterly"] if row["period_end"] == "2026-06-30"][0]
        self.assertEqual(quarter["available_at"], "2026-09-28")
        self.assertEqual(quarter["availability_basis"], "assumed_publication_lag")
        self.assertIsNone(quarter["earnings_definition"])
        self.assertIsNone(quarter["assets"])
        self.assertEqual(quarter["currency"], "GBP")

    def test_working_capital_is_stored_in_the_investment_sign(self):
        """Yahoo signs the change in working capital as a cash effect; the engines do not."""
        result = self.call(statements, "AAPL")
        quarter = [row for row in result["quarterly"] if row["period_end"] == "2026-06-30"][0]
        self.assertAlmostEqual(quarter["change_working_capital"], 2.0)
        self.assertAlmostEqual(quarter["capex"], 9.0)

    def test_a_period_whose_only_filing_is_much_later_falls_back_to_the_assumed_lag(self):
        result = self.call(statements, "AAPL")
        annual = {row["period_end"]: row for row in result["annual"]}
        self.assertEqual(annual["2025-12-31"]["available_at"], "2026-02-02")
        self.assertEqual(annual["2025-12-31"]["availability_basis"], "filing_date")
        self.assertEqual(annual["2024-12-31"]["available_at"], "2025-03-31")
        self.assertEqual(annual["2024-12-31"]["availability_basis"], "assumed_publication_lag")
        quarter = [row for row in result["quarterly"] if row["period_end"] == "2025-09-30"][0]
        self.assertEqual(quarter["available_at"], "2025-12-29")
        self.assertEqual(quarter["availability_basis"], "assumed_publication_lag")

    def test_a_filing_date_is_matched_to_the_period_it_reports(self):
        from portfolio_research.market_records import _publication

        filings = [{"date": "2026-04-20", "type": "10-K"}]
        self.assertEqual(
            _publication(filings, "2026-03-31", 90, period_type="quarterly"),
            ("2026-06-29", "assumed_publication_lag"),
        )
        self.assertEqual(
            _publication(filings, "2026-01-31", 90, period_type="annual"),
            ("2026-04-20", "filing_date"),
        )

    def test_a_restated_quarter_keeps_one_row_per_period_with_the_newer_receipt(self):
        from portfolio_research.statements import latest_vintage

        first = self.call(statements, "AAPL")
        FakeTicker.restated = True
        second = self.call(statements, "AAPL", force=True)
        original = [row for row in first["quarterly"] if row["period_end"] == "2026-06-30"][0]
        restated = [row for row in second["quarterly"] if row["period_end"] == "2026-06-30"][0]
        self.assertAlmostEqual(original["revenue"], 104.0)
        self.assertAlmostEqual(restated["revenue"], 204.0)
        self.assertGreater(restated["received_at"], original["received_at"])
        kept = [
            row
            for row in latest_vintage([*first["quarterly"], *second["quarterly"]])
            if row["period_end"] == "2026-06-30"
        ]
        self.assertEqual(len(kept), 1)
        self.assertAlmostEqual(kept[0]["revenue"], 204.0)


class EstimateTests(AdapterCase):
    def test_consensus_estimates_targets_and_recommendations_are_mapped_and_labelled(self):
        result = self.call(estimates, "AAPL")
        self.assertEqual(result["currency"], "USD")
        self.assertEqual(set(result["eps"]), {"0q", "+1q", "0y", "+1y"})
        forward = result["eps"]["+1y"]
        self.assertAlmostEqual(forward["avg"], 7.50)
        self.assertAlmostEqual(forward["low"], 6.90)
        self.assertAlmostEqual(forward["high"], 8.20)
        self.assertAlmostEqual(forward["year_ago"], 6.80)
        self.assertEqual(forward["analysts"], 32)
        self.assertAlmostEqual(result["revenue"]["0y"]["avg"], 430.0)
        self.assertEqual(result["price_targets"]["median"], 325.0)
        self.assertEqual(result["recommendations"]["period"], "0m")
        self.assertEqual(result["recommendations"]["strong_buy"], 12)
        self.assertEqual(result["recommendations"]["sell"], 1)
        self.assertEqual(result["earnings_dates"], ["2026-10-29", "2026-11-02"])
        self.assertEqual(result["ex_dividend_date"], "2026-11-07")
        self.assertEqual(result["label"], "External consensus estimate; not an established outcome")
        self.assertTrue(result["source_id"].startswith("yahoo_estimates:"))


if __name__ == "__main__":
    unittest.main()
