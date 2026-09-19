"""Pure statement normalization: TTM windows, vintages, share units, split-adjusted dividends."""

import unittest

from portfolio_research.statements import (
    METHOD_VERSION,
    latest_vintage,
    per_share,
    share_units,
    split_adjust_dividends,
    ttm_from_quarters,
)

QUARTER_ENDS = ["2025-06-30", "2025-09-30", "2025-12-31", "2026-03-31", "2026-06-30"]


def quarter(period_end, index):
    """One quarterly statement row with distinct, easily summed values."""
    return {
        "security_id": "AAPL",
        "period_end": period_end,
        "received_at": "2026-09-14T12:00:00+00:00",
        "revenue": 100.0 + index,
        "gross_profit": 40.0 + index,
        "operating_income": 30.0 + index,
        "net_income": 20.0 + index,
        "income_common": 19.0 + index,
        "operating_cash_flow": 25.0 + index,
        "capex": 5.0 + index,
        "assets": 1000.0 + 10 * index,
        "debt": 300.0 + index,
        "cash": 90.0 + index,
    }


QUARTERS = [quarter(end, i) for i, end in enumerate(QUARTER_ENDS)]


class TtmTests(unittest.TestCase):
    def test_ttm_sums_the_four_most_recent_quarters(self):
        ttm = ttm_from_quarters(QUARTERS)
        self.assertEqual(ttm["period_end"], "2026-06-30")
        self.assertEqual(
            ttm["quarters_used"], ["2026-06-30", "2026-03-31", "2025-12-31", "2025-09-30"]
        )
        self.assertAlmostEqual(ttm["revenue"], 100.0 * 4 + (1 + 2 + 3 + 4))
        self.assertAlmostEqual(ttm["operating_income"], 30.0 * 4 + (1 + 2 + 3 + 4))
        self.assertAlmostEqual(ttm["income_common"], 19.0 * 4 + (1 + 2 + 3 + 4))
        self.assertAlmostEqual(ttm["capex"], 5.0 * 4 + (1 + 2 + 3 + 4))
        self.assertEqual(ttm["basis"], "ttm_from_quarters")
        self.assertEqual(ttm["method_version"], METHOD_VERSION)
        self.assertEqual(ttm["missing"], [])

    def test_balance_items_come_from_the_latest_quarter_and_assets_begin_from_four_earlier(self):
        ttm = ttm_from_quarters(QUARTERS)
        self.assertAlmostEqual(ttm["assets"], 1040.0)
        self.assertAlmostEqual(ttm["debt"], 304.0)
        self.assertAlmostEqual(ttm["cash"], 94.0)
        self.assertAlmostEqual(ttm["assets_begin"], 1000.0)
        self.assertEqual(ttm["assets_begin_period_end"], "2025-06-30")

    def test_assets_begin_is_missing_without_a_fifth_quarter(self):
        ttm = ttm_from_quarters(QUARTERS[1:])
        self.assertIsNone(ttm["assets_begin"])
        self.assertIsNone(ttm["assets_begin_period_end"])
        self.assertIn("assets_begin", ttm["missing"])

    def test_a_field_missing_from_one_quarter_is_missing_from_the_ttm(self):
        quarters = [dict(row) for row in QUARTERS]
        quarters[3]["gross_profit"] = None
        ttm = ttm_from_quarters(quarters)
        self.assertIsNone(ttm["gross_profit"])
        self.assertIn("gross_profit", ttm["missing"])
        self.assertIsNotNone(ttm["revenue"])

    def test_fewer_than_four_quarters_has_no_ttm_and_records_a_reason(self):
        issues = []
        self.assertIsNone(ttm_from_quarters(QUARTERS[-3:], issues=issues, security_id="AAPL"))
        self.assertEqual([issue["code"] for issue in issues], ["TTM_INSUFFICIENT_QUARTERS"])
        self.assertEqual(issues[0]["security_id"], "AAPL")
        self.assertIn("4", issues[0]["detail"])

    def test_a_gap_longer_than_one_quarter_has_no_ttm_and_records_a_reason(self):
        gapped = [
            quarter("2024-06-30", 0),
            quarter("2025-09-30", 1),
            quarter("2025-12-31", 2),
            quarter("2026-03-31", 3),
            quarter("2026-06-30", 4),
        ]
        issues = []
        self.assertIsNone(ttm_from_quarters(gapped[:4], issues=issues, security_id="AAPL"))
        self.assertEqual([issue["code"] for issue in issues], ["TTM_QUARTER_GAP"])
        self.assertIn("2024-06-30", issues[0]["detail"])

    def test_a_restated_quarter_is_used_once_with_its_newest_vintage(self):
        restated = dict(quarter("2026-06-30", 4), revenue=999.0)
        restated["received_at"] = "2026-09-15T12:00:00+00:00"
        ttm = ttm_from_quarters([*QUARTERS, restated])
        self.assertEqual(
            ttm["quarters_used"], ["2026-06-30", "2026-03-31", "2025-12-31", "2025-09-30"]
        )
        self.assertAlmostEqual(ttm["revenue"], 999.0 + 103.0 + 102.0 + 101.0)

    def test_an_unparseable_period_end_is_an_invalid_supplied_value(self):
        with self.assertRaises(ValueError):
            ttm_from_quarters([*QUARTERS[:3], dict(QUARTERS[4], period_end="last quarter")])
        with self.assertRaises(ValueError):
            ttm_from_quarters("quarters")


class VintageTests(unittest.TestCase):
    def test_one_period_keeps_the_newest_received_row_and_drops_earlier_vintages(self):
        rows = [
            {"security_id": "A", "period_end": "2026-03-31", "revenue": 1.0, "received_at": "b"},
            {"security_id": "A", "period_end": "2026-03-31", "revenue": 2.0, "received_at": "c"},
            {"security_id": "A", "period_end": "2025-12-31", "revenue": 3.0, "received_at": "a"},
            {"security_id": "B", "period_end": "2026-03-31", "revenue": 4.0, "received_at": "a"},
        ]
        kept = latest_vintage(rows)
        self.assertEqual([row["revenue"] for row in kept], [2.0, 3.0, 4.0])
        self.assertEqual(rows[0]["revenue"], 1.0)

    def test_rows_without_a_received_timestamp_keep_the_last_supplied_row(self):
        rows = [
            {"security_id": "A", "period_end": "2026-03-31", "revenue": 1.0},
            {"security_id": "A", "period_end": "2026-03-31", "revenue": 2.0},
        ]
        self.assertEqual([row["revenue"] for row in latest_vintage(rows)], [2.0])

    def test_empty_input_returns_no_rows(self):
        self.assertEqual(latest_vintage([]), [])


class PerShareTests(unittest.TestCase):
    def test_per_share_divides_and_refuses_impossible_share_counts(self):
        self.assertAlmostEqual(per_share(100.0, 25.0), 4.0)
        self.assertIsNone(per_share(100.0, 0))
        self.assertIsNone(per_share(100.0, -5))
        self.assertIsNone(per_share(None, 25.0))
        self.assertIsNone(per_share(100.0, None))
        self.assertIsNone(per_share(float("nan"), 25.0))


class ShareUnitTests(unittest.TestCase):
    def test_matching_counts_are_the_same_units(self):
        result = share_units(15_000_000_000, 14_900_000_000)
        self.assertEqual(result["factor"], 1.0)
        self.assertEqual(result["basis"], "same_units")
        self.assertIsNone(result["reason"])

    def test_thousands_and_millions_scaling_is_detected(self):
        thousands = share_units(15_000_000, 14_900_000_000)
        self.assertEqual(thousands["factor"], 1e3)
        self.assertEqual(thousands["basis"], "statement_in_thousands")
        millions = share_units(15_000, 14_900_000_000)
        self.assertEqual(millions["factor"], 1e6)
        self.assertEqual(millions["basis"], "statement_in_millions")

    def test_an_unexplained_ratio_is_refused_with_a_reason(self):
        result = share_units(15_000_000_000, 900_000_000)
        self.assertIsNone(result["factor"])
        self.assertIsNone(result["basis"])
        self.assertIn("16.7", result["reason"])

    def test_missing_or_impossible_counts_are_refused_with_a_reason(self):
        for statement, listing in [(None, 10.0), (10.0, None), (0, 10.0), (10.0, -1)]:
            result = share_units(statement, listing)
            self.assertIsNone(result["factor"])
            self.assertTrue(result["reason"])


class SplitAdjustedDividendTests(unittest.TestCase):
    def setUp(self):
        self.dividends = [
            {"security_id": "AAPL", "date": "2025-02-10", "kind": "dividend", "value": 1.0},
            {"security_id": "AAPL", "date": "2026-02-10", "kind": "dividend", "value": 0.6},
        ]
        self.splits = [{"security_id": "AAPL", "date": "2025-06-02", "kind": "split", "value": 2.0}]

    def test_dividends_before_a_split_are_expressed_in_current_share_terms(self):
        rows = split_adjust_dividends(self.dividends, self.splits)
        self.assertEqual([row["date"] for row in rows], ["2025-02-10", "2026-02-10"])
        self.assertAlmostEqual(rows[0]["value"], 0.5)
        self.assertAlmostEqual(rows[0]["raw_value"], 1.0)
        self.assertAlmostEqual(rows[0]["split_factor"], 2.0)
        self.assertAlmostEqual(rows[1]["value"], 0.6)
        self.assertAlmostEqual(rows[1]["split_factor"], 1.0)
        self.assertEqual(rows[0]["basis"], "current_share_terms")
        self.assertEqual(self.dividends[0]["value"], 1.0)

    def test_successive_splits_compound(self):
        splits = [
            *self.splits,
            {"security_id": "AAPL", "date": "2025-09-02", "kind": "split", "value": 5.0},
        ]
        rows = split_adjust_dividends(self.dividends, splits)
        self.assertAlmostEqual(rows[0]["value"], 0.1)
        self.assertAlmostEqual(rows[0]["split_factor"], 10.0)

    def test_no_splits_leaves_values_unchanged(self):
        rows = split_adjust_dividends(self.dividends, [])
        self.assertEqual([row["value"] for row in rows], [1.0, 0.6])

    def test_an_impossible_split_ratio_is_an_invalid_supplied_value(self):
        with self.assertRaises(ValueError):
            split_adjust_dividends(
                self.dividends, [{"date": "2025-06-02", "kind": "split", "value": 0}]
            )
        with self.assertRaises(ValueError):
            split_adjust_dividends(self.dividends, [{"date": "nope", "value": 2.0}])

    def test_a_dividend_without_a_value_is_kept_as_missing(self):
        rows = split_adjust_dividends(
            [{"security_id": "AAPL", "date": "2025-02-10", "value": None}], self.splits
        )
        self.assertIsNone(rows[0]["value"])
        self.assertAlmostEqual(rows[0]["split_factor"], 2.0)


if __name__ == "__main__":
    unittest.main()
