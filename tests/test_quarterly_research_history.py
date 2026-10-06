"""Quarterly financial history limits apply to drafts without rewriting reviewed models."""

import unittest
from copy import deepcopy
from unittest.mock import patch

import pandas as pd

from portfolio_research.holding_analysis import (
    _frozen_proposal,
    _model_facts,
    automatic_eps,
    build_holding_analysis,
)
from portfolio_research.proposals import propose_dcf_inputs
from portfolio_research.research_build import _quarterly_facts
from tests.test_holding_analysis import fixture
from tests.test_proposals import (
    annual_rows,
    defaults,
    equity_security,
    estimates_payload,
    ttm_payload,
)


def quarterly_fixture(count=8):
    security, bundle, config, result = fixture()
    dates = (
        "2026-06-30",
        "2026-03-31",
        "2025-12-31",
        "2025-09-30",
        "2025-06-30",
        "2025-03-31",
        "2024-12-31",
        "2024-09-30",
        "2024-06-30",
        "2024-03-31",
        "2023-12-31",
        "2023-09-30",
    )
    item = bundle["research_inputs"]["EQUITY"]
    item["estimates"] = None
    statements = item["valuation_facts"]["statements"]
    template = statements["quarterly"][0]
    statements["quarterly"] = [
        {
            **template,
            "period_end": day,
            "period_type": "quarterly",
            "source_id": f"quarter:{day}",
            "income_common": 30.0 if index < 4 else 25.0,
            "earnings_definition": "common_shareholders",
        }
        for index, day in enumerate(dates[:count])
    ]
    return security, bundle, config, result


class QuarterlyResearchHistoryTests(unittest.TestCase):
    def test_invalid_retained_component_scope_uses_valid_fresh_quarters(self):
        for retained_in in ("native", "structured"):
            with self.subTest(retained_in=retained_in):
                security, bundle, config, _ = quarterly_fixture()
                statements = bundle["research_inputs"]["EQUITY"]["valuation_facts"]["statements"]
                quarters = statements["quarterly"]
                for row, start in zip(
                    quarters[:4], ("2026-04-01", "2026-01-01", "2025-10-01", "2025-07-01")
                ):
                    row["period_start"] = start
                retained = {
                    **statements["ttm"],
                    "source_id": "retained:invalid",
                    "income_common": 1e12,
                    "provenance": {
                        "income_common": {
                            "components": [
                                {
                                    "period_end": row["period_end"],
                                    "period_start": "2026-03-24"
                                    if index == 0
                                    else row["period_start"],
                                }
                                for index, row in enumerate(quarters[:4])
                            ]
                        }
                    },
                }
                statements["ttm"] = retained if retained_in == "native" else None
                if retained_in == "structured":
                    bundle["fundamentals"] = pd.DataFrame([retained])
                source_copy = deepcopy(statements)
                facts = _model_facts("EQUITY", security, bundle, config)
                self.assertEqual(facts["trailing"]["income_common"], 120)
                self.assertNotEqual(facts["trailing"]["source_id"], "retained:invalid")
                self.assertEqual(statements, source_copy)

    def test_direct_enrichment_date_is_used_without_mutating_bundle_timeline(self):
        security, bundle, config, _ = quarterly_fixture()
        as_of = bundle.pop("as_of")
        timeline = deepcopy(bundle["timeline"])
        record = {
            "security": security,
            "statements": bundle["research_inputs"]["EQUITY"]["valuation_facts"]["statements"],
        }
        ttm, history = _quarterly_facts(record, config, as_of, bundle)
        self.assertEqual(ttm["period_end"], "2026-06-30")
        self.assertEqual(history["available_quarters"], 8)
        self.assertNotIn("as_of", bundle)
        self.assertEqual(bundle["timeline"], timeline)

    def test_frozen_trailing_draft_cannot_bypass_a_smaller_or_expired_quarter_window(self):
        security, bundle, config, _ = quarterly_fixture()
        payload, reason = automatic_eps("EQUITY", security, bundle, config)
        self.assertIsNone(reason)
        bundle["research"] = {"EQUITY": {"proposals": {"eps": payload}}}
        bundle["research_inputs"]["EQUITY"].pop("valuation_facts")
        self.assertIsNotNone(_frozen_proposal("EQUITY", security, bundle, config))
        config["data"]["financial_history_years"] = 1
        self.assertIsNone(_frozen_proposal("EQUITY", security, bundle, config))
        config["data"]["financial_history_years"] = 3
        payload["proposal_meta"]["inputs"]["financial_history"]["current_ttm_quarters"][-1] = (
            "2022-09-30"
        )
        self.assertIsNone(_frozen_proposal("EQUITY", security, bundle, config))

    def test_structured_filing_quarters_feed_saved_holdings_and_proposals_without_native_copies(
        self,
    ):
        security, bundle, config, _ = quarterly_fixture()
        statements = bundle["research_inputs"]["EQUITY"]["valuation_facts"]["statements"]
        quarters = deepcopy(statements["quarterly"])
        for row in quarters:
            row.update(source_policy="original_filing", accession="filing:123", income=1)
        statements["quarterly"] = []
        statements["ttm"] = None
        # A mixed panel introduces pandas NaNs into absent lineage/unit columns.
        bundle["fundamentals"] = pd.DataFrame(
            quarters + [{**quarters[0], "security_id": "OTHER", "source_ids": ["other"]}]
        )
        original = bundle["fundamentals"].copy(deep=True)
        payload, reason = automatic_eps("EQUITY", security, bundle, config)
        self.assertIsNone(reason)
        self.assertAlmostEqual(
            payload["proposal_meta"]["inputs"]["base_annual_earnings_growth"], 0.2
        )
        ttm, history = _quarterly_facts(
            {"security": security, "statements": statements}, config, bundle["as_of"], bundle
        )
        self.assertEqual(ttm["income_common"], 120)
        self.assertEqual(history["available_quarters"], 8)
        self.assertEqual(
            history["current_ttm_quarters"],
            payload["proposal_meta"]["inputs"]["financial_history"]["current_ttm_quarters"],
        )
        pd.testing.assert_frame_equal(bundle["fundamentals"], original)

    def test_native_listing_eps_stays_separate_from_filing_income_growth(self):
        security, bundle, config, _ = quarterly_fixture()
        quarters = deepcopy(
            bundle["research_inputs"]["EQUITY"]["valuation_facts"]["statements"]["quarterly"]
        )
        for row in quarters:
            row.update(
                diluted_eps=999,
                source_id="filing:" + row["period_end"],
                source_policy="original_filing",
                accession="filing:123",
            )
        bundle["fundamentals"] = pd.DataFrame(quarters)
        payload, reason = automatic_eps("EQUITY", security, bundle, config)
        self.assertIsNone(reason)
        self.assertEqual(payload["proposal_meta"]["inputs"]["starting_eps"], 10)
        self.assertTrue(
            all(
                source.startswith("filing:")
                for source in payload["proposal_meta"]["evidence"]["earnings_growth"]
            )
        )
        self.assertTrue(
            all(
                source.startswith("quarter:")
                for source in payload["proposal_meta"]["evidence"]["earnings"]
            )
        )

    def test_point_in_time_filters_only_the_requested_security_panel(self):
        import portfolio_research.holding_analysis as holding

        security, bundle, config, _ = quarterly_fixture()
        quarters = bundle["research_inputs"]["EQUITY"]["valuation_facts"]["statements"]["quarterly"]
        bundle["fundamentals"] = pd.DataFrame(quarters + [{**quarters[0], "security_id": "OTHER"}])
        with patch.object(holding, "_observed", wraps=holding._observed) as observed:
            _model_facts("EQUITY", security, bundle, config)
        self.assertTrue(
            all(
                "OTHER" not in frame.get("security_id", pd.Series(dtype=str)).values
                for frame, *_ in [call.args for call in observed.call_args_list]
            )
        )

    def test_growth_uses_latest_and_prior_quarterly_ttm_with_exact_sources(self):
        security, bundle, config, _ = quarterly_fixture()
        payload, reason = automatic_eps("EQUITY", security, bundle, config)
        self.assertIsNone(reason)
        inputs = payload["proposal_meta"]["inputs"]
        self.assertAlmostEqual(inputs["base_annual_earnings_growth"], 0.20)
        self.assertEqual(inputs["growth_basis"], "reported_quarterly_ttm_common_earnings_growth")
        self.assertEqual(len(payload["proposal_meta"]["evidence"]["earnings_growth"]), 8)
        history = inputs["financial_history"]
        self.assertEqual(history["available_quarters"], 8)
        self.assertEqual(history["max_quarters"], 12)
        self.assertTrue(history["historical_growth_available"])
        self.assertEqual(history["prior_ttm_quarters"][0], "2025-06-30")

    def test_fewer_than_eight_quarters_does_not_use_two_annual_reports(self):
        security, bundle, config, _ = quarterly_fixture(7)
        statements = bundle["research_inputs"]["EQUITY"]["valuation_facts"]["statements"]
        statements["annual"] = [
            {
                **statements["quarterly"][0],
                "period_type": "annual",
                "period_end": day,
                "income_common": value,
            }
            for day, value in (("2026-06-30", 10000), ("2025-06-30", 1))
        ]
        payload, _ = automatic_eps("EQUITY", security, bundle, config)
        inputs = payload["proposal_meta"]["inputs"]
        self.assertEqual(inputs["base_annual_earnings_growth"], 0)
        self.assertEqual(inputs["growth_basis"], "flat_earnings_assumption")
        self.assertFalse(inputs["financial_history"]["historical_growth_available"])

    def test_older_than_three_years_cannot_change_cases_and_retained_reports_stay_intact(self):
        security, bundle, config, _ = quarterly_fixture(12)
        before, _ = automatic_eps("EQUITY", security, bundle, config)
        statements = bundle["research_inputs"]["EQUITY"]["valuation_facts"]["statements"]
        statements["quarterly"].append(
            {
                **statements["quarterly"][-1],
                "period_end": "2023-06-30",
                "income_common": 1e12,
                "diluted_eps": 1e12,
            }
        )
        retained = deepcopy(statements)
        after, _ = automatic_eps("EQUITY", security, bundle, config)
        self.assertEqual(before, after)
        self.assertEqual(statements, retained)
        self.assertEqual(
            after["proposal_meta"]["inputs"]["financial_history"]["available_quarters"], 12
        )

    def test_one_year_setting_has_only_four_quarters_and_no_historical_growth(self):
        security, bundle, config, _ = quarterly_fixture(12)
        config["data"]["financial_history_years"] = 1
        payload, _ = automatic_eps("EQUITY", security, bundle, config)
        history = payload["proposal_meta"]["inputs"]["financial_history"]
        self.assertEqual(history["available_quarters"], 4)
        self.assertEqual(history["max_quarters"], 4)
        self.assertFalse(history["historical_growth_available"])

    def test_missing_or_incompatible_prior_quarters_do_not_create_a_growth_rate(self):
        for defect in ("currency", "gap", "source"):
            with self.subTest(defect=defect):
                security, bundle, config, _ = quarterly_fixture()
                quarters = bundle["research_inputs"]["EQUITY"]["valuation_facts"]["statements"][
                    "quarterly"
                ]
                if defect == "currency":
                    quarters[-1]["currency"] = "CAD"
                elif defect == "gap":
                    quarters[-1]["period_end"] = "2024-03-31"
                else:
                    quarters[-1]["source_id"] = None
                payload, _ = automatic_eps("EQUITY", security, bundle, config)
                self.assertEqual(
                    payload["proposal_meta"]["inputs"]["base_annual_earnings_growth"], 0
                )
                self.assertFalse(
                    payload["proposal_meta"]["inputs"]["financial_history"][
                        "historical_growth_available"
                    ]
                )

    def test_a_standalone_annual_eps_does_not_replace_missing_quarters(self):
        security, bundle, config, _ = quarterly_fixture()
        statements = bundle["research_inputs"]["EQUITY"]["valuation_facts"]["statements"]
        statements["annual"] = [
            {**statements["quarterly"][0], "period_type": "annual", "diluted_eps": 10}
        ]
        statements["quarterly"] = []
        statements["ttm"] = None
        payload, reason = automatic_eps("EQUITY", security, bundle, config)
        self.assertIsNone(payload)
        self.assertTrue(reason)

    def test_consensus_growth_remains_first_and_reviewed_model_is_not_rewritten(self):
        security, bundle, config, result = quarterly_fixture()
        bundle["research_inputs"]["EQUITY"]["estimates"] = fixture()[1]["research_inputs"][
            "EQUITY"
        ]["estimates"]
        payload, _ = automatic_eps("EQUITY", security, bundle, config)
        self.assertEqual(
            payload["proposal_meta"]["inputs"]["growth_basis"], "consensus_year_to_year"
        )
        manual = deepcopy(payload)
        manual["scenarios"][1]["eps"] = 15
        workspace = {"valuations": {"EQUITY": {"eps": manual}}}
        before = deepcopy(workspace)
        config["data"]["financial_history_years"] = 1
        row = build_holding_analysis(result, bundle, config, workspace)[0]
        self.assertEqual(workspace, before)
        self.assertEqual(row["valuation_input"], manual)

    def test_quarterly_dcf_missing_flow_cannot_fall_back_to_annual_and_projection_horizon_stays_five(
        self,
    ):
        trailing = ttm_payload(
            period_type="ttm",
            quarters_used=["2025-06-30", "2025-03-31", "2024-12-31", "2024-09-30"],
        )
        kwargs = dict(
            ttm=trailing,
            annual_statements=annual_rows(),
            shares_outstanding=100_000_000,
            price_major=100,
            currency="USD",
            estimates=estimates_payload(),
            defaults=defaults(),
            quarterly_required=True,
        )
        payload = propose_dcf_inputs(equity_security(), **kwargs)
        self.assertEqual(len(payload["projections"]), 5)
        del trailing["depreciation"]
        issues = []
        self.assertIsNone(propose_dcf_inputs(equity_security(), **kwargs, issues=issues))
        self.assertIn("depreciation", issues[0]["detail"])


if __name__ == "__main__":
    unittest.main()
