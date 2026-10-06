"""Quarterly analytical views are bounded without mutating retained histories."""

from copy import deepcopy
from unittest import TestCase
from unittest.mock import patch

import pandas as pd

from portfolio_lab import providers
from portfolio_research.statements import (
    quarter_window_start,
    quarterly_window,
    ttm_from_quarters,
    ttm_in_window,
)
from tests.test_sec_quarter_history import history


def records():
    return [
        {
            "security_id": "TEST",
            "period_type": "quarterly",
            "period_end": end.date().isoformat(),
            "period_start": (end - pd.offsets.QuarterEnd(1) + pd.Timedelta(days=1))
            .date()
            .isoformat(),
            "available_at": (end + pd.Timedelta(days=30)).date().isoformat(),
            "received_at": (end + pd.Timedelta(days=31)).isoformat() + "+00:00",
            "currency": "USD",
            "source_id": "source:" + end.date().isoformat(),
            "revenue": index,
        }
        for index, end in enumerate(pd.date_range("2022-03-31", "2027-03-31", freq="QE"))
    ]


class QuarterlyWindowTests(TestCase):
    def test_three_calendar_years_and_twelve_distinct_quarter_ends(self):
        source = records()
        original = deepcopy(source)
        selected = quarterly_window(source, as_of="2026-10-06")
        self.assertEqual(len(selected), 12)
        self.assertEqual(selected[0]["period_end"], "2026-09-30")
        self.assertEqual(selected[-1]["period_end"], "2023-12-31")
        selected[0]["revenue"] = -1
        self.assertEqual(source, original)

    def test_years_are_bounded_integers_and_leap_days_use_calendar_offset(self):
        self.assertEqual(quarter_window_start("2024-02-29", 3), "2021-02-28")
        for invalid in [0, 4, 3.0, True, "3"]:
            with self.assertRaises(ValueError):
                quarter_window_start("2026-10-06", invalid)

    def test_latest_vintage_is_selected_after_publication_and_receipt_gates(self):
        original = {
            **records()[-4],
            "period_end": "2026-06-30",
            "period_start": "2026-04-01",
            "available_at": "2026-08-01",
            "received_at": "2026-08-02T12:00:00+00:00",
            "revenue": 1,
        }
        later = {
            **original,
            "available_at": "2026-10-07",
            "received_at": "2026-10-08T12:00:00+00:00",
            "revenue": 2,
        }
        after_receipt = {**original, "received_at": "2026-10-08T12:00:00+00:00", "revenue": 3}
        selected = quarterly_window(
            [original, later, after_receipt],
            as_of="2026-10-06",
            cutoff=pd.Timestamp("2026-10-06T20:00:00+00:00"),
            require_received_by_cutoff=True,
        )
        self.assertEqual([row["revenue"] for row in selected], [1])

    def test_missing_optional_dataframe_fields_do_not_remove_valid_quarters(self):
        source = records()[-5]
        source["period_start"] = float("nan")
        source["period_type"] = "QUARTER"
        selected = quarterly_window([source], as_of="2026-10-06")
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["period_end"], source["period_end"])

    def test_annual_future_and_subquarter_records_are_excluded(self):
        selected = quarterly_window(
            [
                {**records()[-5], "period_type": "annual"},
                {**records()[-5], "period_start": "2026-06-28"},
                records()[-1],
            ],
            as_of="2026-10-06",
        )
        self.assertEqual(selected, [])

    def test_each_security_has_its_own_quarter_cap(self):
        source = records()
        source += [{**row, "security_id": "OTHER"} for row in source]
        selected = quarterly_window(source, as_of="2026-10-06", years=1)
        self.assertEqual(len(selected), 8)
        self.assertEqual(
            len(quarterly_window(source, as_of="2026-10-06", years=1, security_id="TEST")), 4
        )

    def test_untyped_container_requires_an_explicit_quarterly_contract(self):
        row = records()[-5]
        del row["period_type"]
        self.assertEqual(quarterly_window([row], as_of="2026-10-06"), [])
        self.assertEqual(len(quarterly_window([row], as_of="2026-10-06", allow_untyped=True)), 1)


class TtmScopeTests(TestCase):
    def test_declared_quarter_starts_reject_overlap_and_missing_flow_days(self):
        source = quarterly_window(records(), as_of="2026-10-06")[:4]
        self.assertIsNotNone(ttm_from_quarters(source))
        for start, code in [
            ("2026-03-24", "TTM_QUARTER_OVERLAP"),
            ("2026-04-02", "TTM_QUARTER_FLOW_GAP"),
        ]:
            changed = deepcopy(source)
            next(row for row in changed if row["period_end"] == "2026-06-30")["period_start"] = (
                start
            )
            issues = []
            eligible = quarterly_window(changed, as_of="2026-10-06")
            self.assertEqual(len(eligible), 4)
            self.assertIsNone(ttm_from_quarters(eligible, issues=issues, security_id="TEST"))
            self.assertEqual(issues[0]["code"], code)
            self.assertIn(start, issues[0]["detail"])

    def test_unknown_period_starts_keep_established_quarterly_provider_contract(self):
        source = quarterly_window(records(), as_of="2026-10-06")[:4]
        for row in source:
            row["period_start"] = None
        self.assertIsNotNone(ttm_from_quarters(source))

    def test_retained_ttm_with_known_overlapping_quarterly_components_is_rejected(self):
        source = quarterly_window(records(), as_of="2026-10-06")[:4]
        ttm = ttm_from_quarters(source)
        self.assertTrue(ttm_in_window(ttm, as_of="2026-10-06"))
        components = ttm["provenance"]["revenue"]["components"]
        next(part for part in components if part["period_end"] == "2026-06-30")["period_start"] = (
            "2026-03-24"
        )
        self.assertFalse(ttm_in_window(ttm, as_of="2026-10-06"))

    def test_valid_four_quarter_scope_and_null_optional_references(self):
        row = {
            "period_type": "ttm",
            "period_end": "2026-06-30",
            "quarters_used": float("nan"),
            "ttm_quarters": "2026-06-30,2026-03-31,2025-12-31,2025-09-30",
        }
        self.assertTrue(ttm_in_window(row, as_of="2026-08-31", years=1))
        row["quarters_used"] = pd.NA
        self.assertTrue(ttm_in_window(row, as_of="2026-08-31", years=1))

    def test_explicit_ttm_scope_has_equivalent_quarter_end_boundaries(self):
        row = {"period_type": "ttm", "period_start": "2025-07-01", "period_end": "2026-06-30"}
        self.assertTrue(ttm_in_window(row, as_of="2026-08-31", years=1))
        row.update(period_start="2025-06-01", period_end="2026-05-31")
        self.assertFalse(ttm_in_window(row, as_of="2026-08-31", years=1))

    def test_duplicate_short_gap_and_outside_window_references_are_rejected(self):
        for refs in [
            ["2026-06-30"] * 4,
            ["2026-06-30", "2026-06-29", "2026-06-28", "2026-06-27"],
            ["2026-06-30", "2026-03-31", "2025-12-31", "2022-09-30"],
        ]:
            row = {"period_type": "ttm", "period_end": "2026-06-30", "quarters_used": refs}
            self.assertFalse(ttm_in_window(row, as_of="2026-10-06"))
        self.assertFalse(
            ttm_in_window(
                {"period_type": "annual", "period_start": "2025-07-01", "period_end": "2026-06-30"},
                as_of="2026-10-06",
            )
        )

    def test_lineage_timestamps_and_weighted_share_counts_follow_used_quarters(self):
        source = quarterly_window(records(), as_of="2026-10-06")[:4]
        for index, row in enumerate(source):
            row.update(
                diluted_shares=10 + index, share_count_basis="ordinary_share", depreciation=1
            )
        ttm = ttm_from_quarters(source)
        self.assertEqual(ttm["depreciation"], 4)
        self.assertEqual(len(ttm["source_ids"]), 4)
        self.assertEqual(ttm["available_at"], max(row["available_at"] for row in source))
        self.assertEqual(len(ttm["provenance"]["revenue"]["components"]), 4)
        self.assertGreater(ttm["diluted_shares"], 10)
        self.assertLess(ttm["diluted_shares"], 13)


class SecIntegrationTests(TestCase):
    def test_sec_quarters_and_ttm_with_same_end_survive_frame_filter(self):
        source = history()
        original = deepcopy(source)
        bundle = {
            "as_of": "2025-03-01",
            "issues": [],
            "sources": [],
            "securities": pd.DataFrame(
                [{"security_id": "TEST", "instrument_type": "equity", "cik": "123"}]
            ),
        }
        config = {"data": {"refresh_network": False, "financial_history_years": 3}}
        with patch.object(
            providers,
            "_cached",
            return_value=(source, "2025-03-01T12:00:00+00:00", {"source_id": "source:sec"}),
        ):
            providers._enrich_sec(bundle, config, bundle["as_of"])
        providers._filter_frames(bundle, bundle["as_of"])
        rows = bundle["fundamentals"]
        quarters = rows[rows.period_type.eq("quarter")]
        trailing = rows[rows.period_type.eq("ttm")]
        self.assertEqual(len(quarters), 4)
        self.assertEqual(len(trailing), 1)
        self.assertEqual(trailing.iloc[0]["revenue"], 600)
        self.assertEqual(trailing.iloc[0]["period_end"], quarters.iloc[-1]["period_end"])
        self.assertEqual(len(bundle["sec_quarter_history"]["TEST"]), 4)
        self.assertEqual(source, original)
        self.assertFalse(any(issue["code"] == "SEC_FETCH_FAILED" for issue in bundle["issues"]))
