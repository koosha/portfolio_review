"""Q/V/M cross-section integrity and transparent normalization coverage."""

import unittest
from unittest.mock import patch

import pandas as pd

from portfolio_lab.metrics import score_securities
from portfolio_research import market_data
from portfolio_research.identity import resolve_identities
from tests.reference.test_metrics import sample
from tests.test_identity import ledger, listing, position


class ScoreIntegrityTests(unittest.TestCase):
    def score(self, bundle, config):
        return score_securities(bundle, config).set_index("security_id")

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
        self.assertEqual(context["method_version"], "quality-value-momentum-2")


if __name__ == "__main__":
    unittest.main()
