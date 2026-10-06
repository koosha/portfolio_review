"""Q/V/M cross-section integrity and transparent normalization coverage."""

import unittest
from unittest.mock import patch

import pandas as pd

from portfolio_lab.metrics import score_securities
from portfolio_research import market_data
from portfolio_research.identity import resolve_identities
from portfolio_research.statements import ttm_from_quarters
from tests.reference.test_metrics import sample
from tests.test_identity import ledger, listing, position


class ScoreIntegrityTests(unittest.TestCase):
    def score(self, bundle, config):
        return score_securities(bundle, config).set_index("security_id")

    def quarterly_bundle(self):
        bundle, config = sample()
        rows = []
        for original in bundle["fundamentals"].to_dict("records"):
            for index, end in enumerate(
                ("2026-06-30", "2026-03-31", "2025-12-31", "2025-09-30", "2025-06-30")
            ):
                row = dict(original)
                day = pd.Timestamp(end)
                row.update(
                    period_type="quarterly",
                    period_start=(day - pd.offsets.QuarterEnd(1) + pd.Timedelta(days=1))
                    .date()
                    .isoformat(),
                    period_end=end,
                    available_at=(day + pd.Timedelta(days=30)).date().isoformat(),
                    received_at=(day + pd.Timedelta(days=30)).date().isoformat(),
                    source_id=f"quarter:{original['security_id']}:{end}",
                    assets=10e9 - index * 0.25e9,
                    assets_begin=None,
                )
                for field in (
                    "gross_profit",
                    "operating_income",
                    "net_income",
                    "income_common",
                    "operating_cash_flow",
                    "capex",
                ):
                    row[field] = original[field] / 4
                rows.append(row)
        bundle["fundamentals"] = pd.DataFrame(rows)
        return bundle, config

    def native_holding(self):
        bundle, config = sample()
        resolved = resolve_identities(
            ledger(position(1, "B", quote_symbol="B")),
            None,
            listings={"B": listing("B", "USD", exchange="NMS")},
            search=None,
            issuer_lookup=lambda _row: "cik:0000000001",
        )["securities"][0]
        self.assertIs(resolved["eligible"], False)
        self.assertEqual(resolved["resolution_basis"], "quote_symbol")
        mask = bundle["securities"].security_id.eq("B")
        for field in ("eligible", "issuer_id", "resolution_status", "resolution_basis"):
            bundle["securities"].loc[mask, field] = resolved[field]
        with patch.object(
            market_data,
            "_obtain",
            return_value=(
                {
                    "country": "United States",
                    "sector": "Technology",
                    "market_cap_currency": "USD",
                    "metadata": {"instrument_type": "EQUITY", "currency": "USD"},
                },
                "2026-08-31T19:00:00Z",
                {"source_id": "yahoo_profile:fixture"},
            ),
        ):
            profile = market_data.security_profile(
                config,
                resolved,
                refresh=False,
                issues=[],
                as_of=bundle["as_of"],
                issuer_lookup=lambda _row: "cik:0000000001",
            )
        # Exactly the retained packet shape written by enrich_market: the profile
        # is inside valuation_facts, not at the top of a security's research inputs.
        bundle["research_inputs"] = {"B": {"valuation_facts": {"profile": profile}}}
        return bundle, config

    def test_verified_native_holding_can_be_ranked_without_enabling_purchases(self):
        bundle, config = self.native_holding()
        scores = self.score(bundle, config)
        self.assertTrue(scores.loc["B", "research_eligible"])
        self.assertEqual(scores.loc["B", "research_eligibility_basis"], "verified_native_profile")
        self.assertTrue(pd.notna(scores.loc["B", "score"]))
        self.assertFalse(bundle["securities"].set_index("security_id").loc["B", "eligible"])

    def test_native_research_requires_observed_profile_and_verified_issuer(self):
        for invalid in (
            "missing_profile",
            "future_profile",
            "fallback_issuer",
            "unresolved",
            "foreign_profile",
        ):
            with self.subTest(invalid=invalid):
                bundle, config = self.native_holding()
                if invalid == "missing_profile":
                    bundle["research_inputs"] = {}
                elif invalid == "future_profile":
                    bundle["research_inputs"]["B"]["valuation_facts"]["profile"]["received_at"] = (
                        "2026-09-01T20:00:00Z"
                    )
                elif invalid == "fallback_issuer":
                    bundle["securities"].loc[
                        bundle["securities"].security_id.eq("B"), "issuer_id"
                    ] = "listing:B"
                elif invalid == "unresolved":
                    bundle["securities"].loc[
                        bundle["securities"].security_id.eq("B"), "resolution_status"
                    ] = "unresolved"
                else:
                    bundle["research_inputs"]["B"]["valuation_facts"]["profile"]["domicile"] = "CN"
                scores = self.score(bundle, config)
                self.assertFalse(scores.loc["B", "research_eligible"])
                self.assertTrue(pd.isna(scores.loc["B", "score"]))

    def test_explicit_mapping_exclusion_remains_until_research_is_affirmed(self):
        bundle, config = self.native_holding()
        mask = bundle["securities"].security_id.eq("B")
        bundle["securities"].loc[mask, "resolution_basis"] = "supplemental"
        scores = self.score(bundle, config)
        self.assertFalse(scores.loc["B", "research_eligible"])
        bundle["securities"]["research_eligible"] = None
        bundle["securities"].loc[mask, "research_eligible"] = True
        scores = self.score(bundle, config)
        self.assertTrue(pd.notna(scores.loc["B", "score"]))
        self.assertFalse(bundle["securities"].set_index("security_id").loc["B", "eligible"])

    def test_explicit_research_exclusion_overrides_source_eligibility(self):
        bundle, config = sample()
        bundle["securities"]["research_eligible"] = False
        scores = self.score(bundle, config)
        self.assertFalse(scores.eligible.any())
        self.assertTrue(scores.score.isna().all())

    def test_unresolved_identity_and_unknown_source_eligibility_do_not_enter_ranks(self):
        for field, value, reason in (
            ("issuer_id", None, "unresolved_issuer"),
            ("resolution_status", "conflict", "unresolved_security_identity"),
            ("eligible", None, "source_ineligible"),
        ):
            with self.subTest(field=field):
                bundle, config = sample()
                bundle["securities"][field] = (
                    bundle["securities"]
                    .get(field, pd.Series(None, index=bundle["securities"].index))
                    .astype(object)
                )
                bundle["securities"].loc[bundle["securities"].security_id.eq("B"), field] = value
                scores = self.score(bundle, config)
                self.assertFalse(scores.loc["B", "eligible"])
                self.assertTrue(pd.isna(scores.loc["B", "score"]))
                self.assertIn(reason, scores.loc["B", "reasons"])

    def test_unknown_sectors_cannot_become_an_accounting_peer_group(self):
        bundle, config = sample()
        bundle["securities"]["sector"] = "Unknown"
        scores = self.score(bundle, config)
        self.assertTrue(scores.quality.isna().all())
        self.assertTrue(scores.value.isna().all())
        self.assertTrue(pd.notna(scores.loc["B", "momentum"]))
        self.assertIn("unknown_sector_for_quality_value", scores.loc["B", "reasons"])

    def test_yahoo_financial_sector_alias_requires_separate_model(self):
        bundle, config = sample()
        bundle["securities"]["sector"] = "Financial Services"
        config["signals"]["excluded_sectors"] = []
        scores = self.score(bundle, config)
        self.assertFalse(scores.eligible.any())
        self.assertTrue(scores.score.isna().all())
        self.assertEqual(scores.loc["B", "sector"], "Financials")
        self.assertIn("separate_sector_model_required", scores.loc["B", "reasons"])

    def test_standalone_quarter_is_not_read_as_trailing_year(self):
        bundle, config = sample()
        bundle["fundamentals"]["period_type"] = "quarterly"
        scores = self.score(bundle, config)
        self.assertTrue(scores.gross_profitability.isna().all())
        self.assertTrue(scores.earnings_yield.isna().all())
        self.assertTrue(scores.score.isna().all())
        self.assertTrue(pd.notna(scores.loc["B", "momentum"]))

    def test_annual_and_unlabelled_facts_cannot_substitute_for_quarterly_ttm(self):
        for kind in ("annual", None):
            with self.subTest(kind=kind):
                bundle, config = sample()
                bundle["fundamentals"]["period_type"] = kind
                scores = self.score(bundle, config)
                self.assertTrue(scores.gross_profitability.isna().all())
                self.assertTrue(scores.value.isna().all())
                self.assertTrue(scores.score.isna().all())
                self.assertIn("quarterly_ttm_required", scores.loc["B", "reasons"])

    def test_declared_imported_ttm_needs_a_valid_twelve_month_scope(self):
        bundle, config = sample()
        self.assertTrue(pd.notna(self.score(bundle, config).loc["B", "score"]))
        for start in (None, "2026-04-01", "2022-07-01"):
            with self.subTest(start=start):
                invalid = dict(bundle)
                invalid["fundamentals"] = bundle["fundamentals"].copy()
                invalid["fundamentals"]["period_start"] = start
                scores = self.score(invalid, config)
                self.assertTrue(scores.gross_profitability.isna().all())
                self.assertIn(
                    "ttm_outside_financial_window_or_unverified_period_scope",
                    scores.loc["B", "reasons"],
                )

    def test_native_ttm_needs_four_verified_component_periods(self):
        bundle, config = sample()
        bundle["fundamentals"]["source_id"] = "yahoo_statements:retained"
        self.assertTrue(self.score(bundle, config).score.isna().all())
        bundle["fundamentals"]["ttm_quarters"] = "2026-06-30,2026-03-31,2025-12-31,2025-09-30"
        self.assertTrue(pd.notna(self.score(bundle, config).loc["B", "score"]))

    def test_known_old_balance_cannot_supply_beginning_assets_denominator(self):
        bundle, config = sample()
        bundle["fundamentals"]["assets_begin_period_end"] = "2022-06-30"
        scores = self.score(bundle, config)
        self.assertTrue(scores.gross_profitability.isna().all())
        self.assertTrue(scores.operating_profitability.isna().all())
        self.assertTrue(scores.negative_accruals.isna().all())
        self.assertTrue(pd.notna(scores.loc["B", "value"]))
        self.assertIn("beginning_assets_outside_financial_window", scores.loc["B", "reasons"])

    def test_imported_quarters_build_exact_ttm_without_changing_raw_rows(self):
        bundle, config = self.quarterly_bundle()
        original = bundle["fundamentals"].copy(deep=True)
        scores = self.score(bundle, config)
        self.assertTrue(pd.notna(scores.loc["B", "score"]))
        self.assertAlmostEqual(scores.loc["B", "metric_inputs"]["operating_income"], 1e9)
        self.assertAlmostEqual(scores.loc["B", "metric_inputs"]["assets_begin"], 9e9)
        self.assertAlmostEqual(scores.loc["B", "metric_inputs"]["average_assets"], 9.5e9)
        self.assertEqual(scores.loc["B", "fundamental_period_type"], "ttm")
        self.assertEqual(
            scores.loc["B", "fundamental_quarters"],
            "2026-06-30,2026-03-31,2025-12-31,2025-09-30",
        )
        self.assertEqual(len(scores.loc["B", "fundamental_component_source_ids"]), 5)
        pd.testing.assert_frame_equal(bundle["fundamentals"], original)

    def test_native_mixed_quarter_ttm_frame_handles_missing_provenance_cells(self):
        bundle, config = self.quarterly_bundle()
        retained = ttm_from_quarters(
            bundle["fundamentals"]
            .loc[bundle["fundamentals"].security_id.eq("B")]
            .to_dict("records")
        )
        bundle["fundamentals"] = pd.DataFrame(
            [*bundle["fundamentals"].to_dict("records"), retained]
        )
        # Native frames mix scalar quarterly fields with TTM provenance maps.
        # Pandas fills the absent quarterly map cells with float NaN.
        self.assertTrue(
            bundle["fundamentals"]
            .loc[bundle["fundamentals"].period_type.eq("quarterly"), "provenance"]
            .isna()
            .all()
        )
        original = bundle["fundamentals"].copy(deep=True)
        scores = self.score(bundle, config)
        self.assertTrue(pd.notna(scores.loc["B", "score"]))
        self.assertAlmostEqual(scores.loc["B", "metric_inputs"]["operating_income"], 1e9)
        pd.testing.assert_frame_equal(bundle["fundamentals"], original)

    def test_ttm_component_evidence_cannot_contradict_its_declared_quarter_dates(self):
        bundle, config = sample()
        bundle["fundamentals"]["ttm_quarters"] = "2026-06-30,2026-03-31,2025-12-31,2025-09-30"
        bundle["fundamentals"]["provenance"] = [
            {
                "gross_profit": {
                    "components": [
                        {"period_end": day}
                        for day in ("2025-06-30", "2025-03-31", "2024-12-31", "2024-09-30")
                    ]
                }
            }
            for _ in range(len(bundle["fundamentals"]))
        ]
        self.assertTrue(self.score(bundle, config).score.isna().all())

    def test_verified_quarters_are_preferred_to_period_only_import_for_same_end(self):
        bundle, config = self.quarterly_bundle()
        imported, _ = sample()
        imported["fundamentals"]["operating_income"] = 1e20
        bundle["fundamentals"] = pd.DataFrame(
            [
                *bundle["fundamentals"].to_dict("records"),
                *imported["fundamentals"].to_dict("records"),
            ]
        )
        scores = self.score(bundle, config)
        self.assertAlmostEqual(scores.loc["B", "metric_inputs"]["operating_income"], 1e9)
        self.assertEqual(scores.loc["B", "fundamental_period_type"], "ttm")

    def test_unqualified_quarter_income_never_becomes_qualified_common_earnings(self):
        bundle, config = self.quarterly_bundle()
        bundle["fundamentals"].loc[
            bundle["fundamentals"].period_end.eq("2026-03-31"), "earnings_definition"
        ] = "consolidated_unqualified"
        scores = self.score(bundle, config)
        self.assertTrue(scores.earnings_yield.isna().all())
        self.assertTrue(scores.score.isna().all())
        self.assertTrue(pd.notna(scores.loc["B", "quality"]))

    def test_incomplete_or_mixed_currency_quarters_do_not_sum_to_ttm(self):
        for invalid in ("missing_quarter", "mixed_currency"):
            with self.subTest(invalid=invalid):
                bundle, config = self.quarterly_bundle()
                if invalid == "missing_quarter":
                    bundle["fundamentals"] = bundle["fundamentals"].loc[
                        bundle["fundamentals"].period_end.ne("2026-03-31")
                    ]
                else:
                    bundle["fundamentals"].loc[
                        bundle["fundamentals"].period_end.eq("2026-03-31"), "currency"
                    ] = "GBP"
                scores = self.score(bundle, config)
                self.assertTrue(scores.score.isna().all())
                self.assertTrue(scores.earnings_yield.isna().all())

    def test_future_restatement_does_not_replace_an_eligible_quarter(self):
        bundle, config = self.quarterly_bundle()
        original = self.score(bundle, config)
        revised = (
            bundle["fundamentals"].loc[bundle["fundamentals"].period_end.eq("2026-03-31")].copy()
        )
        revised["available_at"] = "2026-09-01"
        revised["received_at"] = "2026-09-01"
        revised["gross_profit"] = 1e20
        bundle["fundamentals"] = pd.concat([bundle["fundamentals"], revised], ignore_index=True)
        after = self.score(bundle, config)
        pd.testing.assert_series_equal(original.gross_profitability, after.gross_profitability)

    def test_cached_ttm_component_dates_and_imported_scope_honor_financial_years(self):
        bundle, config = sample()
        config["data"]["financial_history_years"] = 1
        self.assertTrue(pd.notna(self.score(bundle, config).loc["B", "score"]))
        bundle["fundamentals"]["period_start"] = "2025-06-01"
        bundle["fundamentals"]["period_end"] = "2026-05-31"
        self.assertTrue(self.score(bundle, config).score.isna().all())
        bundle, config = sample()
        bundle["fundamentals"]["ttm_quarters"] = "2026-06-30,2026-03-31,2025-12-31,2023-06-30"
        scores = self.score(bundle, config)
        self.assertTrue(scores.gross_profitability.isna().all())
        self.assertTrue(scores.score.isna().all())

    def test_old_ttm_period_is_excluded_even_when_staleness_is_relaxed(self):
        bundle, config = sample()
        config["data"]["max_fundamental_age_days"] = 3000
        bundle["fundamentals"]["period_end"] = "2023-06-30"
        bundle["fundamentals"]["period_start"] = "2022-07-01"
        scores = self.score(bundle, config)
        self.assertTrue(scores.gross_profitability.isna().all())
        self.assertTrue(scores.score.isna().all())

    def test_mixed_date_only_and_timestamped_observations_keep_valid_scores(self):
        bundle, config = sample()
        before = self.score(bundle, config)
        mask = bundle["fundamentals"].security_id.eq("B")
        bundle["fundamentals"].loc[mask, "available_at"] = "2026-08-01T20:00:00Z"
        bundle["fundamentals"].loc[mask, "period_end"] = "2026-06-30T20:00:00Z"
        mask = bundle["prices"].security_id.eq("B") & bundle["prices"].date.eq("2026-08-31")
        bundle["prices"].loc[mask, "date"] = "2026-08-31T20:00:00Z"
        after = self.score(bundle, config)
        self.assertAlmostEqual(after.loc["B", "score"], before.loc["B", "score"])

    def test_current_vendor_statement_snapshot_is_not_backdated_to_historical_review(self):
        bundle, config = sample()
        bundle["fundamentals"]["source_id"] = "yahoo_statements:current"
        bundle["fundamentals"]["received_at"] = "2026-09-05T20:00:00Z"
        config["data"]["require_received_by_cutoff"] = False
        scores = self.score(bundle, config)
        self.assertTrue(scores.gross_profitability.isna().all())
        self.assertTrue(scores.value.isna().all())
        self.assertTrue(scores.score.isna().all())

    def test_duplicate_old_momentum_endpoint_never_silently_selects_another_price(self):
        bundle, config = sample()
        duplicate = (
            bundle["prices"]
            .loc[bundle["prices"].security_id.eq("B") & bundle["prices"].date.eq("2025-08-29")]
            .copy()
        )
        duplicate["adjusted_close"] = 1
        bundle["prices"] = pd.concat([bundle["prices"], duplicate], ignore_index=True)
        scores = self.score(bundle, config)
        self.assertIn("duplicate_price_sessions", scores.loc["B", "reasons"])
        self.assertFalse(scores.loc["B", "eligible"])
        self.assertTrue(pd.isna(scores.loc["B", "momentum"]))
        self.assertTrue(pd.isna(scores.loc["B", "raw_momentum"]))

    def test_market_cap_quote_currency_cannot_supply_an_unconverted_value_denominator(self):
        bundle, config = sample()
        bundle["securities"]["market_cap_currency"] = "GBP"
        scores = self.score(bundle, config)
        self.assertTrue(scores.earnings_yield.isna().all())
        self.assertTrue(scores.cashflow_yield.isna().all())
        self.assertTrue(scores.score.isna().all())
        self.assertTrue(pd.notna(scores.loc["B", "quality"]))
        self.assertIn("market_cap_fundamental_currency_mismatch", scores.loc["B", "reasons"])

    def test_percentile_ties_and_missing_quality_input_keep_exact_coverage(self):
        bundle, config = sample()
        bundle["fundamentals"].loc[bundle["fundamentals"].security_id.eq("B"), "gross_profit"] = (
            None
        )
        scores = self.score(bundle, config)
        # Operating profitability and negative accruals are equal across the three
        # unique issuers: their average-tie percentile is rank 2 / 3, not 100%.
        self.assertAlmostEqual(scores.loc["B", "quality"], 2 / 3)
        self.assertEqual(scores.loc["B", "quality_metric_count"], 2)
        self.assertEqual(scores.loc["B", "quality_peer_counts"]["gross_profitability"], 2)
        self.assertEqual(
            scores.loc["A2", "quality_peer_counts"], scores.loc["A", "quality_peer_counts"]
        )
        self.assertEqual(scores.loc["B", "rank_universe_issuer_count"], 3)
        self.assertEqual(scores.loc["B", "sector_peer_count"], 3)
        self.assertEqual(
            scores.loc["A2", "fundamental_source_id"], scores.loc["A", "fundamental_source_id"]
        )
        self.assertEqual(scores.loc["A2", "metric_inputs"], scores.loc["A", "metric_inputs"])
        self.assertEqual(scores.loc["B", "fundamental_period_end"], "2026-06-30")
        self.assertEqual(scores.loc["B", "momentum_start_source_id"], "fixture")
        self.assertAlmostEqual(
            scores.loc["B", "raw_momentum"],
            scores.loc["B", "momentum_end_adjusted_close"]
            / scores.loc["B", "momentum_start_adjusted_close"]
            - 1,
        )
        self.assertAlmostEqual(
            scores.loc["B", "score"],
            (scores.loc["B", "quality"] + scores.loc["B", "value"] + scores.loc["B", "momentum"])
            / 3,
        )
        context = scores.attrs["scoring_context"]
        self.assertEqual(context["supplied_securities"], 5)
        self.assertEqual(context["ranked_issuers"], 3)
        self.assertFalse(context["market_wide"])
        self.assertEqual(context["method_version"], "quality-value-momentum-3")


if __name__ == "__main__":
    unittest.main()
