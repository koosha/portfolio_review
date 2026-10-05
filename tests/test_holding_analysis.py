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
