"""Source-backed defaults, edited decisions, and deterministic scenario replay."""

import tempfile
import unittest
from copy import deepcopy
from unittest.mock import patch

import pandas as pd

from portfolio_lab.config import validate_config
from portfolio_lab.demo import create_demo
from portfolio_lab.pipeline import load_inputs, replay_analysis, save_analysis
from portfolio_research.application import analyze_review
from portfolio_research.calendar import decision_context
from portfolio_research.enrichment import _apply_shared_state
from portfolio_research.holding_analysis import automatic_eps, build_holding_analysis
from portfolio_research.scenarios import SOURCE_LABEL
from portfolio_research.valuation import calculate_eps


def fixture(horizon=12):
    config = validate_config(
        {
            "data": {"sec_enabled": False, "fred_enabled": False},
            "mandate": {"benchmark_id": "BENCH"},
            "allocation": {"horizon_months": horizon},
        }
    )
    security = {
        "security_id": "EQUITY",
        "ticker": "EQ",
        "name": "Example Company",
        "instrument_type": "equity",
        "equity_type": "ordinary_common",
        "issuer_id": "issuer:1",
        "currency": "USD",
        "sector": "Industrials",
    }
    quarters = [
        {
            "security_id": "EQUITY",
            "period_end": date,
            "available_at": "2026-08-01",
            "received_at": "2026-08-02",
            "diluted_eps": 2.5,
            "source_id": "statement:original",
            "currency": "USD",
        }
        for date in ("2026-06-30", "2026-03-31", "2025-12-31", "2025-09-30")
    ]
    trailing = {
        **quarters[0],
        "period_type": "ttm",
        "ttm_quarters": ",".join(row["period_end"] for row in quarters),
    }
    prices = {
        "currency": "USD",
        "prices": [
            {
                "security_id": "EQUITY",
                "date": "2026-08-31",
                "available_at": "2026-08-31",
                "received_at": "2026-08-31",
                "currency": "USD",
                "close": 100.0,
                "source_id": "price:original",
            }
        ],
        "actions": [{"kind": "dividend", "date": "2026-06-01", "value": 2.0}],
    }
    estimates = {
        "currency": "USD",
        "received_at": "2026-08-30",
        "source_id": "consensus:original",
        "eps": {"0y": {"avg": 10.0}, "+1y": {"avg": 11.0}},
    }
    bundle = {
        "as_of": "2026-08-31",
        "timeline": decision_context("2026-08-31"),
        "securities": pd.DataFrame([security]),
        "research_inputs": {
            "EQUITY": {
                "estimates": estimates,
                "valuation_facts": {
                    "prices": prices,
                    "statements": {"currency": "USD", "quarterly": quarters, "ttm": trailing},
                    "profile": {"shares_outstanding": 100_000_000},
                },
            }
        },
    }
    benchmark = [
        {"security_id": "BENCH", "scenario": label, "return_value": value}
        for label, value in config["allocation"]["shared_state"]["market_returns"].items()
    ]
    result = {
        "holdings": [{**security, "account_id": "account:1", "market_value": 1000}],
        "forecast_inputs": benchmark,
        "macro": [{"series_id": "DGS10"}],
        "summary": {"complete": False},
        "allocation": {"issues": []},
    }
    return security, bundle, config, result


class HoldingAnalysisTests(unittest.TestCase):
    def test_native_collection_passes_the_existing_identity_lookup_to_the_profile(self):
        from contextlib import ExitStack

        from portfolio_research.enrichment import _collect

        security, bundle, config, _ = fixture()

        def lookup(metadata):
            return "issuer:1"

        with ExitStack() as stack:
            for name in ("price_history", "statements", "estimates", "news_and_filings"):
                stack.enter_context(
                    patch("portfolio_research.market_data." + name, return_value=None)
                )
            profile = stack.enter_context(
                patch(
                    "portfolio_research.market_data.security_profile",
                    return_value={"issuer_id": "issuer:1"},
                )
            )
            record = _collect(
                security,
                config,
                bundle["as_of"],
                refresh=False,
                issues=[],
                issuer_lookup=lookup,
                timeline=bundle["timeline"],
            )
        self.assertIs(profile.call_args.kwargs["issuer_lookup"], lookup)
        self.assertEqual(record["profile"]["issuer_id"], security["issuer_id"])

    def test_explicit_depositary_receipt_supports_a_listing_consensus_company_model(self):
        security, bundle, config, _ = fixture()
        security["equity_type"] = "depositary_receipt"
        payload, reason = automatic_eps("EQUITY", security, bundle, config)
        self.assertIsNone(reason)
        self.assertIsNotNone(payload)
        self.assertEqual(payload["eps_convention"], "forward")

    def test_statement_eps_subunits_are_normalized_before_the_price_multiple(self):
        security, bundle, config, _ = fixture()
        facts = bundle["research_inputs"]["EQUITY"]["valuation_facts"]
        facts["prices"]["currency"] = "GBP"
        facts["prices"]["prices"][0]["currency"] = "GBP"
        for row in facts["statements"]["quarterly"]:
            row.update(currency="GBp", diluted_eps=250, per_share_basis="listed_share")
        payload, reason = automatic_eps("EQUITY", security, bundle, config)
        self.assertIsNone(reason)
        self.assertEqual(payload["proposal_meta"]["inputs"]["starting_eps"], 10)

    def test_foreign_statements_use_verified_listing_consensus_when_statement_units_missing(self):
        security, bundle, config, _ = fixture()
        statements = bundle["research_inputs"]["EQUITY"]["valuation_facts"]["statements"]
        statements["currency"] = "CNY"
        for row in statements["quarterly"]:
            row["currency"] = "CNY"
        before = deepcopy(bundle["research_inputs"])
        payload, reason = automatic_eps("EQUITY", security, bundle, config)
        self.assertIsNone(reason)
        self.assertEqual(payload["eps_convention"], "forward")
        meta = payload["proposal_meta"]
        self.assertFalse(meta["inputs"]["statements_used"])
        self.assertEqual(meta["inputs"]["earnings_basis"], "current_fiscal_consensus_eps")
        self.assertEqual(meta["inputs"]["starting_eps"], 10)
        self.assertTrue(any("no verified" in note for note in meta["notes"]))
        self.assertEqual(bundle["research_inputs"], before)

    def test_documented_ordinary_earnings_convert_to_quote_per_ads_once(self):
        from tests.test_valuation_basis import fx_record, ratio_record

        security, bundle, config, _ = fixture()
        facts = bundle["research_inputs"]["EQUITY"]["valuation_facts"]
        facts["statements"]["currency"] = "CNY"
        for row in facts["statements"]["quarterly"]:
            row.update(currency="CNY", diluted_eps=1.375, per_share_basis="ordinary_share")
        facts["listing_basis"] = ratio_record()
        bundle["fx_observations"] = [fx_record()]
        payload, reason = automatic_eps("EQUITY", security, bundle, config)
        self.assertIsNone(reason)
        self.assertEqual(payload["eps_convention"], "trailing")
        self.assertAlmostEqual(payload["proposal_meta"]["inputs"]["starting_eps"], 6.6)
        self.assertEqual(
            payload["proposal_meta"]["assumptions"]["reporting_to_quote_fx"],
            "constant_retained_spot_rate",
        )

    def test_a_known_ads_requires_statement_share_basis_even_in_same_currency(self):
        from tests.test_valuation_basis import ratio_record

        security, bundle, config, _ = fixture()
        facts = bundle["research_inputs"]["EQUITY"]["valuation_facts"]
        facts["listing_basis"] = ratio_record()
        bundle["research_inputs"]["EQUITY"]["estimates"] = None
        payload, reason = automatic_eps("EQUITY", security, bundle, config)
        self.assertIsNone(payload)
        self.assertIn("share basis", reason)

    def test_a_foreign_usd_listing_needs_verified_statement_units_or_listing_consensus(self):
        security, bundle, config, _ = fixture()
        security["domicile"] = "CN"
        payload, reason = automatic_eps("EQUITY", security, bundle, config)
        self.assertIsNone(reason)
        self.assertEqual(payload["eps_convention"], "forward")
        bundle["research_inputs"]["EQUITY"]["estimates"] = None
        payload, reason = automatic_eps("EQUITY", security, bundle, config)
        self.assertIsNone(payload)
        self.assertIn("share basis", reason)
        for row in bundle["research_inputs"]["EQUITY"]["valuation_facts"]["statements"][
            "quarterly"
        ]:
            row["per_share_basis"] = "listed_share"
        payload, reason = automatic_eps("EQUITY", security, bundle, config)
        self.assertIsNone(reason)
        self.assertEqual(payload["eps_convention"], "trailing")

    def test_currency_of_actual_eps_observations_wins_over_an_inconsistent_wrapper(self):
        security, bundle, config, _ = fixture()
        facts = bundle["research_inputs"]["EQUITY"]["valuation_facts"]
        for row in facts["statements"]["quarterly"]:
            row["currency"] = "CAD"
        bundle["research_inputs"]["EQUITY"]["estimates"] = None
        payload, _ = automatic_eps("EQUITY", security, bundle, config)
        self.assertIsNone(payload)

    def test_contradictory_price_currency_cannot_create_a_signal(self):
        security, bundle, config, _ = fixture()
        bundle["research_inputs"]["EQUITY"]["valuation_facts"]["prices"]["prices"][0][
            "currency"
        ] = "GBP"
        payload, reason = automatic_eps("EQUITY", security, bundle, config)
        self.assertIsNone(payload)
        self.assertIn("conflicting price currencies", reason)

    def test_duplicate_or_overlapping_eps_quarters_are_not_an_annual_earnings_value(self):
        for dates in (
            ("2026-06-30", "2026-06-30", "2025-12-31", "2025-09-30"),
            ("2026-06-30", "2026-06-29", "2026-06-28", "2026-06-27"),
        ):
            security, bundle, config, _ = fixture()
            statements = bundle["research_inputs"]["EQUITY"]["valuation_facts"]["statements"]
            for row, day in zip(statements["quarterly"], dates):
                row["period_end"] = day
            statements["ttm"]["ttm_quarters"] = ",".join(dates)
            bundle["research_inputs"]["EQUITY"]["estimates"] = None
            self.assertIsNone(automatic_eps("EQUITY", security, bundle, config)[0])

    def test_consensus_growth_requires_the_same_currency_in_both_periods(self):
        security, bundle, config, _ = fixture()
        estimates = bundle["research_inputs"]["EQUITY"]["estimates"]
        estimates["eps"]["0y"]["currency"] = "USD"
        estimates["eps"]["+1y"]["currency"] = "CNY"
        payload, _ = automatic_eps("EQUITY", security, bundle, config)
        self.assertEqual(
            payload["proposal_meta"]["inputs"]["growth_basis"], "flat_earnings_assumption"
        )

    def test_consensus_growth_on_statement_eps_is_an_explicit_accounting_proxy(self):
        security, bundle, config, _ = fixture()
        payload, _ = automatic_eps("EQUITY", security, bundle, config)
        self.assertEqual(
            payload["proposal_meta"]["inputs"]["growth_convention"],
            "consensus_growth_applied_to_statement_eps_draft_proxy",
        )
        self.assertTrue(
            any(
                "accounting definitions may differ" in note
                for note in payload["proposal_meta"]["notes"]
            )
        )

    def test_partial_wrong_horizon_or_wrong_date_benchmark_is_not_a_buy_comparison(self):
        for defect in ("partial", "horizon", "date"):
            _, bundle, config, result = fixture()
            config["allocation"]["use_probabilities"] = False
            if defect == "partial":
                result["forecast_inputs"] = [result["forecast_inputs"][1]]
            elif defect == "horizon":
                result["forecast_inputs"][1]["horizon_months"] = 6
            else:
                result["forecast_inputs"][1]["forecast_date"] = "2026-07-31"
            row = build_holding_analysis(result, bundle, config, {})[0]
            self.assertEqual(row["action"], "Review")
            self.assertIsNone(row["benchmark_return"])

    def test_frozen_current_inputs_do_not_admit_receipts_after_the_final_cutoff(self):
        security, bundle, config, _ = fixture()
        bundle["timeline"].update(
            review_kind="current", input_acquisition_started_at="2026-08-31T19:00:00Z"
        )
        item = bundle["research_inputs"]["EQUITY"]
        item["valuation_facts"]["statements"] = {}
        item["estimates"]["received_at"] = "2026-08-31T20:30:00Z"
        self.assertIsNone(automatic_eps("EQUITY", security, bundle, config)[0])

    def test_sec_covered_company_keeps_native_valuation_statements_without_overwriting_sec(self):
        from contextlib import ExitStack

        from portfolio_research.enrichment import _collect, _frames

        security, bundle, config, _ = fixture()
        security["cik"] = "0000000001"
        config["data"]["sec_enabled"] = True
        native = bundle["research_inputs"]["EQUITY"]["valuation_facts"]
        with ExitStack() as stack:
            calls = {}
            for name, returned in {
                "price_history": native["prices"],
                "statements": native["statements"],
                "estimates": bundle["research_inputs"]["EQUITY"]["estimates"],
                "security_profile": native["profile"],
                "news_and_filings": None,
            }.items():
                calls[name] = stack.enter_context(
                    patch("portfolio_research.market_data." + name, return_value=returned)
                )
            record = _collect(security, config, bundle["as_of"], refresh=False, issues=[])
        calls["statements"].assert_called_once()
        self.assertTrue(record["sec_covered"])
        self.assertEqual(record["statements"]["ttm"]["period_end"], "2026-06-30")
        # SEC observations were obtained by the existing provider path; the vendor
        # copy is confined to the separate valuation facts rather than merged twice.
        frames = _frames({"EQUITY": record}, {})
        self.assertEqual(frames["fundamentals"], [])

    def test_initial_holding_has_three_populated_statement_based_cases_and_action(self):
        _, bundle, config, result = fixture()
        before = deepcopy(bundle["research_inputs"])
        rows = build_holding_analysis(result, bundle, config, {})
        row = rows[0]
        self.assertEqual(row["action"], "Buy")
        self.assertEqual(row["model"], "eps_multiple")
        self.assertEqual(row["source_type"], "automatic_draft")
        self.assertEqual(len(row["scenarios"]), 3)
        self.assertAlmostEqual(row["scenarios"][1]["horizon_price"], 110)
        self.assertAlmostEqual(row["scenarios"][1]["return_value"], 0.12)
        self.assertTrue(row["evidence"]["statements"]["used"])
        self.assertTrue(row["evidence"]["estimates"]["used"])
        self.assertFalse(row["evidence"]["macro"]["used"])
        self.assertEqual(row["evidence"]["macro"]["status"], "context_only")
        self.assertFalse(row["executable"])
        self.assertIn("Confirm the investment mandate.", row["trade_readiness"]["reasons"])
        self.assertEqual(bundle["research_inputs"], before)
        self.assertEqual(result["holdings"][0]["research_action"], "Buy")
        self.assertEqual(result["company_research"]["EQUITY"]["eps"]["status"], "ready")

    def test_manual_edit_recalculates_action_and_preserves_other_defaults(self):
        security, bundle, config, result = fixture()
        original, _ = automatic_eps("EQUITY", security, bundle, config)
        for eps, action in ((6, "Sell"), (10.5, "Hold"), (13, "Buy")):
            payload = deepcopy(original)
            payload["scenarios"][1]["eps"] = eps
            workspace = {"valuations": {"EQUITY": {"eps": payload, "origin": "manual"}}}
            row = build_holding_analysis(deepcopy(result), bundle, config, workspace)[0]
            self.assertEqual(row["action"], action)
            self.assertEqual(row["source_type"], "user_assumption")
            self.assertEqual(workspace["valuations"]["EQUITY"]["eps"], payload)
            self.assertEqual(row["valuation_input"]["scenarios"][0], original["scenarios"][0])

    def test_distributions_and_earnings_use_selected_horizon(self):
        for horizon in (6, 12, 18):
            security, bundle, config, _ = fixture(horizon)
            payload, _ = automatic_eps("EQUITY", security, bundle, config)
            central = calculate_eps(payload)["scenarios"][1]
            self.assertAlmostEqual(central["distributions_per_starting_share"], 2 * horizon / 12)
            self.assertAlmostEqual(central["eps"], 10 * 1.1 ** (horizon / 12))

    def test_missing_distributions_are_an_explicit_draft_assumption(self):
        security, bundle, config, _ = fixture()
        bundle["research_inputs"]["EQUITY"]["valuation_facts"]["prices"]["actions"] = []
        payload, _ = automatic_eps("EQUITY", security, bundle, config)
        self.assertEqual(
            payload["proposal_meta"]["assumptions"]["dividend_basis"], "explicit_zero_draft"
        )
        self.assertEqual(payload["scenarios"][1]["distributions_per_starting_share"], 0)

    def test_currency_mismatch_negative_earnings_and_stale_prices_are_gated(self):
        for defect in ("currency", "earnings", "stale"):
            security, bundle, config, _ = fixture()
            facts = bundle["research_inputs"]["EQUITY"]["valuation_facts"]
            if defect == "currency":
                facts["statements"]["currency"] = "CAD"
                for row in facts["statements"]["quarterly"]:
                    row["currency"] = "CAD"
                bundle["research_inputs"]["EQUITY"]["estimates"] = None
            elif defect == "earnings":
                for row in facts["statements"]["quarterly"]:
                    row["diluted_eps"] = -2
            else:
                facts["prices"]["prices"][0]["date"] = "2025-01-01"
            payload, reason = automatic_eps("EQUITY", security, bundle, config)
            self.assertIsNone(payload, defect)
            self.assertTrue(reason)

    def test_a_retained_proposal_cannot_bypass_a_failed_native_data_gate(self):
        security, bundle, config, result = fixture()
        proposed, _ = automatic_eps("EQUITY", security, bundle, config)
        bundle["research"] = {"EQUITY": {"proposals": {"eps": proposed}}}
        bundle["research_inputs"]["EQUITY"]["valuation_facts"]["prices"]["prices"][0]["date"] = (
            "2025-01-01"
        )
        row = build_holding_analysis(result, bundle, config, {})[0]
        self.assertEqual(row["action"], "Review")
        self.assertNotEqual(row["status"], "ready")
        self.assertIn("stale", row["reason"])

    def test_historical_yahoo_statements_require_a_pre_cutoff_receipt(self):
        security, bundle, config, result = fixture()
        item = bundle["research_inputs"]["EQUITY"]
        item["estimates"] = None
        statements = item["valuation_facts"]["statements"]
        statements["point_in_time"] = "current_retrieval; not a historical vintage"
        for row in [statements["ttm"], *statements["quarterly"]]:
            row["received_at"] = "2026-09-15"
        payload, _ = automatic_eps("EQUITY", security, bundle, config)
        self.assertIsNone(payload)
        self.assertEqual(build_holding_analysis(result, bundle, config, {})[0]["action"], "Review")
        for row in [statements["ttm"], *statements["quarterly"]]:
            row["source_policy"] = "original_filing"
            row["accession"] = "0000000001-26-000001"
        payload, _ = automatic_eps("EQUITY", security, bundle, config)
        self.assertIsNotNone(payload)

    def test_dcf_selection_updates_intrinsic_values_without_inventing_horizon_returns(self):
        from portfolio_research.demo import demo_workspace

        security, bundle, config, result = fixture()
        eps, _ = automatic_eps("EQUITY", security, bundle, config)
        dcf = demo_workspace()["valuations"]["SIM01"]["dcf"]
        workspace = {
            "valuations": {"EQUITY": {"eps": eps, "dcf": deepcopy(dcf), "active_model": "dcf"}}
        }
        first = build_holding_analysis(deepcopy(result), bundle, config, workspace)[0]
        self.assertEqual(first["model"], "fcff_dcf")
        self.assertEqual(first["action"], "Review")
        self.assertTrue(all(case["return_value"] is None for case in first["scenarios"]))
        self.assertTrue(all(case["value_per_share"] is not None for case in first["scenarios"]))
        workspace["valuations"]["EQUITY"]["dcf"]["discount_rate"] = 0.14
        second = build_holding_analysis(deepcopy(result), bundle, config, workspace)[0]
        self.assertLess(
            second["scenarios"][1]["value_per_share"], first["scenarios"][1]["value_per_share"]
        )
        workspace["valuations"]["EQUITY"]["active_model"] = "eps"
        selected = build_holding_analysis(deepcopy(result), bundle, config, workspace)[0]
        self.assertEqual(selected["model"], "eps_multiple")
        self.assertEqual(selected["action"], "Buy")

    def test_current_estimates_cannot_enter_a_historical_forecast(self):
        security, bundle, config, _ = fixture()
        item = bundle["research_inputs"]["EQUITY"]
        item["valuation_facts"]["statements"] = {}
        item["estimates"]["received_at"] = "2026-09-01"
        payload, reason = automatic_eps("EQUITY", security, bundle, config)
        self.assertIsNone(payload)
        self.assertTrue(reason)

    def test_far_future_consensus_is_rejected_even_in_current_review(self):
        security, bundle, config, _ = fixture()
        bundle["timeline"]["review_kind"] = "current"
        item = bundle["research_inputs"]["EQUITY"]
        item["valuation_facts"]["statements"] = {}
        item["estimates"]["received_at"] = "2030-01-01"
        payload, reason = automatic_eps("EQUITY", security, bundle, config)
        self.assertIsNone(payload)
        self.assertTrue(reason)

    def test_future_current_fact_receipts_are_rejected(self):
        security, bundle, config, _ = fixture()
        bundle["timeline"]["review_kind"] = "current"
        item = bundle["research_inputs"]["EQUITY"]
        item["valuation_facts"]["prices"]["prices"][0]["received_at"] = "2030-01-01"
        payload, _ = automatic_eps("EQUITY", security, bundle, config)
        self.assertIsNone(payload)

    def test_future_splits_and_dividends_do_not_change_starting_share_basis(self):
        security, bundle, config, _ = fixture()
        first, _ = automatic_eps("EQUITY", security, bundle, config)
        actions = bundle["research_inputs"]["EQUITY"]["valuation_facts"]["prices"]["actions"]
        actions.extend(
            [
                {"kind": "split", "date": "2026-09-01", "value": 10},
                {"kind": "dividend", "date": "2026-09-01", "value": 100},
            ]
        )
        second, _ = automatic_eps("EQUITY", security, bundle, config)
        self.assertEqual(first, second)

    def test_reported_eps_is_not_double_adjusted_for_a_split(self):
        security, bundle, config, _ = fixture()
        inputs = bundle["research_inputs"]["EQUITY"]
        inputs["estimates"] = None
        facts = inputs["valuation_facts"]
        facts["prices"]["actions"].append({"kind": "split", "date": "2026-05-01", "value": 2})
        blocked, _ = automatic_eps("EQUITY", security, bundle, config)
        self.assertIsNone(blocked)
        facts["statements"]["ttm"]["eps_share_basis"] = "current_share_terms"
        current, _ = automatic_eps("EQUITY", security, bundle, config)
        self.assertEqual(current["proposal_meta"]["inputs"]["starting_eps"], 10)
        facts["statements"]["ttm"]["eps_share_basis"] = "original_share_terms"
        original, _ = automatic_eps("EQUITY", security, bundle, config)
        self.assertEqual(original["proposal_meta"]["inputs"]["starting_eps"], 6.25)

    def test_probabilities_produce_conditional_mean_only_when_complete(self):
        _, bundle, config, result = fixture()
        self.assertEqual(
            build_holding_analysis(deepcopy(result), bundle, config, {})[0]["return_metric"],
            "central_scenario",
        )
        config["allocation"]["shared_state"]["probabilities"] = {
            "Adverse": 0.25,
            "Central": 0.5,
            "Favorable": 0.25,
        }
        row = build_holding_analysis(deepcopy(result), bundle, config, {})[0]
        self.assertEqual(row["return_metric"], "weighted_scenario")
        self.assertAlmostEqual(
            row["scenario_return"],
            sum(case["return_value"] * case["probability"] for case in row["scenarios"]),
        )
        self.assertFalse(row["calibrated"])
        config["allocation"]["use_probabilities"] = False
        row = build_holding_analysis(deepcopy(result), bundle, config, {})[0]
        self.assertEqual(row["return_metric"], "central_scenario")

    def test_a_manual_horizon_mismatch_is_not_compared_to_the_benchmark(self):
        security, bundle, config, result = fixture()
        payload, _ = automatic_eps("EQUITY", security, bundle, config)
        payload["horizon_months"] = 6
        row = build_holding_analysis(
            result, bundle, config, {"valuations": {"EQUITY": {"eps": payload}}}
        )[0]
        self.assertEqual(row["action"], "Review")
        self.assertIsNone(row["scenario_return"])
        self.assertIn("horizon", row["reason"])

    def test_shared_state_edits_change_company_assumptions_without_network(self):
        security, bundle, config, _ = fixture()
        first, _ = automatic_eps("EQUITY", security, bundle, config)
        config["allocation"]["shared_state"]["market_returns"]["Central"] = -0.1
        with patch(
            "portfolio_research.market_data.estimates", side_effect=AssertionError("network")
        ):
            second, _ = automatic_eps("EQUITY", security, bundle, config)
        self.assertNotEqual(first["scenarios"][1]["eps"], second["scenarios"][1]["eps"])
        self.assertNotEqual(first["scenarios"][1]["pe"], second["scenarios"][1]["pe"])


class GeneratedForecastReplayTests(unittest.TestCase):
    def test_legacy_monthly_journey_proposals_open_as_populated_drafts(self):
        from portfolio_lab.config import load_config
        from tests.support.monthly_journey import _inject

        with tempfile.TemporaryDirectory() as folder:
            config = load_config(create_demo(folder))
            config["risk"]["bootstrap_samples"] = 50
            bundle = load_inputs(config, "2026-08-31")
            _inject(bundle, config, "2026-08-31")
            self.assertNotIn("valuation_facts", bundle["research_inputs"]["SIM01"])
            result = analyze_review(bundle, config)
            row = next(row for row in result["holding_analysis"] if row["security_id"] == "SIM01")
            self.assertEqual(row["source_type"], "retained_proposal_draft")
            self.assertEqual(row["model"], "eps_multiple")
            self.assertTrue(all(row["return_value"] is not None for row in row["scenarios"]))
            self.assertIn(row["action"], {"Buy", "Sell", "Hold"})
            self.assertEqual(result["company_research"]["SIM01"]["eps"]["status"], "ready")

    def test_generated_forecasts_refresh_and_imported_forecasts_are_unchanged(self):
        _, bundle, config, _ = fixture()
        bundle["securities"] = pd.DataFrame(
            [
                *bundle["securities"].to_dict("records"),
                {
                    "security_id": "BENCH",
                    "instrument_type": "etf",
                    "currency": "USD",
                    "sector": "Unknown",
                },
            ]
        )
        _apply_shared_state(bundle, config, bundle["as_of"])
        first = bundle["forecasts"].copy(deep=True)
        imported = first[first.security_id.eq("BENCH")].copy()
        imported["source"] = "imported:reviewed"
        bundle["forecasts"] = pd.concat([first[first.security_id.ne("BENCH")], imported])
        config["allocation"]["shared_state"]["market_returns"]["Central"] = -0.1
        _apply_shared_state(bundle, config, bundle["as_of"])
        generated = bundle["forecasts"][bundle["forecasts"].security_id.eq("EQUITY")]
        self.assertEqual(len(generated), 3)
        self.assertEqual(set(generated.source), {SOURCE_LABEL})
        self.assertAlmostEqual(
            float(generated[generated.scenario.eq("Central")].return_value.iloc[0]), -0.1
        )
        actual = bundle["forecasts"][bundle["forecasts"].security_id.eq("BENCH")]
        pd.testing.assert_frame_equal(
            actual.reset_index(drop=True), imported.reset_index(drop=True)
        )

    def test_saved_replay_recalculates_generated_forecasts_and_preserves_archive(self):
        from portfolio_lab.config import load_config

        with tempfile.TemporaryDirectory() as folder:
            config = load_config(create_demo(folder))
            config["risk"]["bootstrap_samples"] = 50
            bundle = load_inputs(config, "2026-08-31")
            bundle["forecasts"] = bundle["forecasts"].iloc[0:0]
            result = analyze_review(bundle, config)
            saved = save_analysis(result, config, bundle)
            original = deepcopy(saved["forecast_inputs"])
            state = deepcopy(config["allocation"]["shared_state"])
            state["market_returns"]["Central"] = -0.1
            with patch(
                "portfolio_lab.pipeline.enrich_bundle", side_effect=AssertionError("network")
            ):
                preview, frozen, _ = replay_analysis(
                    config, saved["run_id"], {"allocation": {"shared_state": state}}
                )
            self.assertNotEqual(preview["forecast_inputs"], original)
            self.assertEqual(saved["forecast_inputs"], original)
            regenerated = frozen["forecasts"]
            self.assertFalse(regenerated.duplicated(["security_id", "scenario"]).any())
