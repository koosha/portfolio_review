"""Integration-method changes with explicit synthetic decisions and accounting checks."""

import unittest
from copy import deepcopy
from unittest.mock import patch

import numpy as np
import pandas as pd

from portfolio_lab.allocation import build_proposals
from portfolio_lab.config import validate_config
from portfolio_lab.metrics import risk_analysis, scenario_analysis, score_securities
from tests.reference.test_allocation import fixture
from tests.reference.test_metrics import sample
from tests.reference.test_providers import company, extract


class MethodologyTests(unittest.TestCase):
    def setUp(self):
        self.bundle, self.config = fixture()
        self.config["allocation"]["optimize"] = False
        self.config["mandate"]["dealing_rules"] = {
            "r1": {
                sid: {"fractional_shares": False, "quantity_increment": 1, "dealing_allowed": True}
                for sid in ["A", "B", "C"]
            }
        }
        self.scores = pd.DataFrame({"security_id": ["A", "B", "C"], "score": [0.3, 0.7, 0.95]})
        self.enterContext(
            patch(
                "portfolio_lab.metrics.portfolio_covariance",
                side_effect=lambda b, c, ids: {
                    "ids": ids,
                    "matrix": np.eye(len(ids)) * 0.04,
                    "complete": True,
                    "observations": 260,
                    "issues": [],
                },
            )
        )
        self.enterContext(
            patch(
                "portfolio_lab.metrics.stress_returns",
                side_effect=lambda b, c, ids: {"recession": {sid: -0.3 for sid in ids}},
            )
        )

    def candidates(self):
        result = build_proposals(self.bundle, self.config, self.scores)
        return result, {row["candidate"]: row for row in result.get("candidates", [])}

    def test_usd_reporting_default_keeps_investment_mandate_unconfirmed(self):
        config = validate_config({})
        self.assertFalse(config["mandate"]["confirmed"])
        self.assertFalse(config["allocation"]["optimize"])
        self.assertTrue(config["allocation"]["use_probabilities"])
        self.assertEqual(config["mandate"]["base_currency"], "USD")
        self.assertIsNone(config["mandate"]["benchmark_id"])
        self.assertIsNone(config["allocation"]["active_sleeve_weight"])
        self.assertIsNone(config["allocation"]["sleeve_budget_basis"])
        self.assertIsNone(config["allocation"]["cash_return"])

    def test_unknown_cash_return_keeps_asset_outcomes_but_not_portfolio_mean(self):
        self.config["allocation"]["cash_return"] = None
        scenarios = scenario_analysis(self.bundle, self.config)
        self.assertEqual(scenarios["status"], "partial")
        self.assertIsNone(scenarios["weighted_return"])
        for row in scenarios["scenarios"]:
            self.assertIsNone(row["return"])
            self.assertTrue(any(value["return"] is not None for value in row["contributions"]))
        _, candidates = self.candidates()
        simple = candidates["simple_equal_issuer_sleeve"]
        self.assertEqual(simple["status"], "draft")
        self.assertIsNone(simple["scenario_weighted_return_after_cost_and_tax_reserve"])
        self.assertIsNone(simple["improvement_per_redeployed_dollar"])
        self.assertTrue(all(value is None for value in simple["scenarios"].values()))
        self.config["allocation"]["optimize"] = True
        result, _ = self.candidates()
        self.assertEqual(result["status"], "blocked")
        self.assertIn("missing_cash_return", {row["code"] for row in result["issues"]})

    def test_explicit_cash_return_enters_scenarios_and_candidate_objective_once(self):
        baseline = scenario_analysis(self.bundle, self.config)
        self.config["allocation"]["cash_return"] = 0.04
        scenarios = scenario_analysis(self.bundle, self.config)
        self.assertAlmostEqual(
            scenarios["weighted_return"] - baseline["weighted_return"], 0.1 * 0.04
        )
        _, candidates = self.candidates()
        simple = candidates["simple_equal_issuer_sleeve"]
        no_change = candidates["no_change"]
        self.assertAlmostEqual(
            no_change["scenario_weighted_return_after_cost_and_tax_reserve"],
            scenarios["weighted_return"],
        )
        # Recompute cash sensitivity from the retained cash and per-side trading cost.
        for sensitivity in simple["cost_sensitivities"]:
            delta = sensitivity["execution_cost"] - simple["execution_cost"]
            self.assertAlmostEqual(
                sensitivity["scenario_weighted_return_after_cost_and_tax_reserve"],
                simple["scenario_weighted_return_after_cost_and_tax_reserve"]
                - delta * 1.04 / 100000.0,
            )

    def test_sleeve_ties_use_turnover_then_stable_issuer_identity(self):
        self.config["allocation"].update(
            max_names=1, sleeve_membership={"r1": {"A": 0.25, "B": 0.30}}
        )
        self.scores["score"] = [0.9, 0.9, 0.1]
        _, candidates = self.candidates()
        # Both exceed the .20 target and have equal incremental turnover benefit.
        self.assertEqual(candidates["simple_equal_issuer_sleeve"]["selections"], {"r1": ["A"]})

    def test_unweighted_scenarios_have_outcomes_without_invented_mean(self):
        self.bundle["forecasts"]["probability"] = None
        out = scenario_analysis(self.bundle, self.config)
        self.assertEqual(out["status"], "complete")
        self.assertEqual(out["probability_status"], "unweighted")
        self.assertEqual(len(out["scenarios"]), 2)
        self.assertIsNone(out["weighted_return"])
        self.assertIsNone(out["objective_weighted_return"])
        self.assertTrue(all(row["probability"] is None for row in out["scenarios"]))
        result, candidates = self.candidates()
        simple = candidates["simple_equal_issuer_sleeve"]
        self.assertTrue(simple["feasible"])
        self.assertIsNone(simple["scenario_weighted_return_after_cost_and_tax_reserve"])
        self.assertGreater(len(simple["proposals"]), 0)
        self.config["allocation"]["optimize"] = True
        blocked, _ = self.candidates()
        self.assertEqual(blocked["status"], "blocked")
        self.assertIn("missing_probabilities", {x["code"] for x in blocked["issues"]})

    def test_unweighted_toggle_ignores_existing_distribution_without_changing_outcomes(self):
        weighted = scenario_analysis(self.bundle, self.config)
        self.assertIsNotNone(weighted["weighted_return"])
        source = self.bundle["forecasts"].copy(deep=True)
        self.config["allocation"]["use_probabilities"] = False
        self.config = validate_config(self.config)
        unweighted = scenario_analysis(self.bundle, self.config)
        self.assertEqual(unweighted["probability_status"], "unweighted")
        for field in [
            "weighted_return",
            "objective_weighted_return",
            "net_weighted_return",
            "net_objective_weighted_return",
            "benchmark_weighted_return",
            "benchmark_objective_weighted_return",
        ]:
            self.assertIsNone(unweighted[field])
        for before, after in zip(weighted["scenarios"], unweighted["scenarios"]):
            self.assertIsNone(after["probability"])
            for field in ["return", "terminal_value", "benchmark_return", "active_return"]:
                self.assertEqual(before[field], after[field])
        pd.testing.assert_frame_equal(source, self.bundle["forecasts"])
        _, candidates = self.candidates()
        simple = candidates["simple_equal_issuer_sleeve"]
        self.assertTrue(simple["feasible"])
        self.assertIsNone(simple["scenario_weighted_return_after_cost_and_tax_reserve"])
        self.config["allocation"]["optimize"] = True
        blocked, _ = self.candidates()
        self.assertEqual(blocked["status"], "blocked")
        self.assertIn("missing_probabilities", {x["code"] for x in blocked["issues"]})
        with self.assertRaisesRegex(ValueError, "use_probabilities must be true or false"):
            validate_config({"allocation": {"use_probabilities": "false"}})

    def test_unweighted_toggle_ignores_partial_source_and_saved_override(self):
        self.config["allocation"].update(
            use_probabilities=False, probability_overrides={"old_label": 1.0}
        )
        self.bundle["forecasts"].loc[0, "probability"] = None
        result = scenario_analysis(self.bundle, self.config)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["probability_status"], "unweighted")
        self.assertEqual(len(result["scenarios"]), 2)

    def test_mixed_joint_vintages_and_partial_distribution_fail(self):
        self.bundle["forecasts"].loc[
            self.bundle["forecasts"].security_id == "A", "forecast_date"
        ] = "2026-08-30"
        result = scenario_analysis(self.bundle, self.config)
        self.assertEqual(result["status"], "invalid")
        self.assertIn("mixed_joint_vintage", {x["code"] for x in result["issues"]})
        self.bundle["forecasts"]["forecast_date"] = "2026-08-31"
        self.bundle["forecasts"].loc[
            self.bundle["forecasts"].scenario == "central", "probability"
        ] = None
        result = scenario_analysis(self.bundle, self.config)
        self.assertIsNone(result["weighted_return"])
        self.assertIn("partial_probabilities", {x["code"] for x in result["issues"]})

    def test_arbitrary_calibration_string_cannot_unlock_calibrated_result(self):
        self.bundle["forecasts"]["basis"] = "calibrated"
        self.bundle["forecasts"]["calibration_id"] = "not-validated"
        self.assertEqual(scenario_analysis(self.bundle, self.config)["basis"], "subjective")
        self.config["allocation"]["mode"] = "calibrated"
        result, _ = self.candidates()
        self.assertEqual(result["status"], "blocked")
        self.assertIn("uncalibrated_forecast", {x["code"] for x in result["issues"]})

    def test_simple_basket_exists_with_optimizer_off_and_preserves_legacy(self):
        result, candidates = self.candidates()
        self.assertEqual(result["selected_candidate"], "simple_equal_issuer_sleeve")
        self.assertNotIn("conditional_optimum", candidates)
        simple = candidates["simple_equal_issuer_sleeve"]
        self.assertEqual(simple["status"], "review_ready")
        self.assertEqual(simple["budget_basis"], "account_nav")
        self.assertEqual(simple["target_sleeve_membership"], {"r1": {"B": 0.1, "C": 0.1}})
        for row in simple["proposals"]:
            legacy = 0.35 if row["security_id"] in {"A", "B"} else 0.0
            self.assertGreaterEqual(row["target_weight"], legacy - 1e-9)
        for account in simple["accounts"]:
            self.assertAlmostEqual(account["cash_identity_residual"], 0, places=6)
            self.assertAlmostEqual(account["budget_total"], 100000, places=6)
        self.assertEqual(result["proposals"], simple["proposals"])

    def test_legacy_ownership_is_not_sleeve_membership(self):
        self.config["allocation"]["sleeve_membership"] = {}
        _, candidates = self.candidates()
        self.assertEqual(candidates["simple_equal_issuer_sleeve"]["status"], "blocked")
        self.assertIn("legacy", candidates["simple_equal_issuer_sleeve"]["reason"])

    def test_unfunded_sleeve_fails_without_selling_legacy(self):
        self.config["allocation"]["active_sleeve_weight"] = 0.9
        _, candidates = self.candidates()
        self.assertEqual(candidates["simple_equal_issuer_sleeve"]["status"], "infeasible")
        self.assertEqual(candidates["simple_equal_issuer_sleeve"]["proposals"], [])

    def test_cost_and_hurdle_sensitivities_reconcile_same_basket(self):
        _, candidates = self.candidates()
        simple = candidates["simple_equal_issuer_sleeve"]
        gross = sum(abs(row["trade_value"]) for row in simple["proposals"])
        self.assertEqual(
            [row["transaction_cost_bps"] for row in simple["cost_sensitivities"]], [5, 10, 25]
        )
        for row in simple["cost_sensitivities"]:
            self.assertAlmostEqual(
                row["execution_cost"], gross * row["transaction_cost_bps"] / 10000
            )
            self.assertLess(row["validation"]["account_budget_residual"], 1e-6)
        buys = sum(max(row["trade_value"], 0) for row in simple["proposals"])
        sells = sum(max(-row["trade_value"], 0) for row in simple["proposals"])
        self.assertAlmostEqual(simple["redeployed_principal"], max(buys, sells))
        self.assertAlmostEqual(
            simple["improvement_per_redeployed_dollar"],
            simple["expected_improvement_dollars_after_cost_and_tax_reserve"] / max(buys, sells),
        )
        self.assertEqual(
            [row["return_hurdle"] for row in simple["hurdle_sensitivities"]], [0.01, 0.02, 0.04]
        )

    def test_missing_dealing_rules_leave_draft_and_whole_share_rounding_is_real(self):
        del self.config["mandate"]["dealing_rules"]["r1"]["C"]
        _, candidates = self.candidates()
        self.assertEqual(candidates["simple_equal_issuer_sleeve"]["status"], "draft")
        self.config["mandate"]["dealing_rules"]["r1"]["C"] = {
            "fractional_shares": False,
            "quantity_increment": 7,
            "dealing_allowed": True,
        }
        _, candidates = self.candidates()
        row = next(
            r
            for r in candidates["simple_equal_issuer_sleeve"]["proposals"]
            if r["security_id"] == "C"
        )
        self.assertAlmostEqual(row["fractional_quantity_change"] % 7, 0, places=6)
        self.assertTrue(candidates["simple_equal_issuer_sleeve"]["rounding_applied"])

    def test_rounding_cannot_relax_an_issuer_cap(self):
        self.config["mandate"]["issuer_cap"] = 0.4005
        for rule in self.config["mandate"]["dealing_rules"]["r1"].values():
            rule["quantity_increment"] = 1000
        _, candidates = self.candidates()
        repair = candidates["minimal_constraint_repair"]
        self.assertFalse(repair["feasible"])
        self.assertEqual(repair["status"], "infeasible")
        self.assertGreater(repair["validation"]["max_linear_violation"], 0.01)

    def test_hard_constraints_override_bands_as_complete_basket(self):
        self.config["mandate"]["issuer_cap"] = 0.4
        self.config["allocation"]["min_trade_value"] = 50000
        _, candidates = self.candidates()
        repair = candidates["minimal_constraint_repair"]
        self.assertTrue(repair["feasible"])
        self.assertGreater(len(repair["proposals"]), 0)
        self.assertTrue(
            any(
                "hard_constraint_overrides_trade_band" in row["review_flags"]
                for row in repair["proposals"]
            )
        )

    def test_flow_cash_is_not_added_to_nav_twice(self):
        self.config["allocation"].update(
            flow_policy="approved_benchmark",
            new_flows={
                "r1": {
                    "amount": 5000,
                    "source_id": "synthetic-deposit",
                    "included_in_snapshot": True,
                    "valuation_date": "2026-08-31",
                }
            },
        )
        result, candidates = self.candidates()
        candidate = candidates["approved_benchmark_new_flows"]
        self.assertTrue(candidate["feasible"])
        self.assertEqual(result["selected_candidate"], "approved_benchmark_new_flows")
        self.assertAlmostEqual(candidate["accounts"][0]["budget_total"], 100000)
        self.assertEqual(candidate["accounts"][0]["external_flow_added"], 0)
        self.config["allocation"]["new_flows"]["r1"]["valuation_date"] = "2026-07-31"
        _, candidates = self.candidates()
        self.assertEqual(candidates["approved_benchmark_new_flows"]["status"], "infeasible")
        self.config["allocation"]["new_flows"]["r1"]["valuation_date"] = "2026-08-31"
        self.config["allocation"]["new_flows"]["r1"]["amount"] = 50000
        _, candidates = self.candidates()
        self.assertEqual(candidates["approved_benchmark_new_flows"]["status"], "infeasible")

    def test_invalid_dealing_and_flow_configuration_rejected(self):
        config = deepcopy(self.config)
        config["mandate"]["dealing_rules"]["r1"]["A"]["quantity_increment"] = 0.1
        with self.assertRaises(ValueError):
            validate_config(config)

        config = deepcopy(self.config)
        config["allocation"]["new_flows"] = {
            "r1": {"amount": 100, "source_id": "", "included_in_snapshot": False}
        }
        with self.assertRaises(ValueError):
            validate_config(config)

    def test_explicit_all_cash_can_fund_a_sleeve_without_invented_positions(self):
        self.bundle["positions"] = self.bundle["positions"].iloc[:0].copy()
        self.bundle["accounts"]["cash"] = 100000.0
        self.config["allocation"]["sleeve_membership"] = {"r1": {}}
        _, candidates = self.candidates()
        self.assertTrue(candidates["no_change"]["feasible"])
        simple = candidates["simple_equal_issuer_sleeve"]
        self.assertTrue(simple["feasible"])
        self.assertTrue(all(row["trade_value"] > 0 for row in simple["proposals"]))
        self.assertAlmostEqual(simple["accounts"][0]["budget_total"], 100000.0)

    def test_substitution_hurdle_is_not_transferred_to_another_horizon(self):
        self.bundle["forecasts"]["horizon_months"] = 6
        self.config["allocation"]["horizon_months"] = 6
        _, candidates = self.candidates()
        simple = candidates["simple_equal_issuer_sleeve"]
        self.assertIsNone(simple["return_hurdle_met"])
        self.assertTrue(all(row["met"] is None for row in simple["hurdle_sensitivities"]))

    def test_dealing_lock_cannot_be_ignored_by_sleeve_or_repair(self):
        self.config["mandate"]["dealing_rules"]["r1"]["A"]["dealing_allowed"] = False
        self.config["mandate"]["issuer_cap"] = 0.4
        _, candidates = self.candidates()
        self.assertFalse(candidates["minimal_constraint_repair"]["feasible"])
        self.assertFalse(candidates["simple_equal_issuer_sleeve"]["feasible"])


class EarningsDefinitionTests(unittest.TestCase):
    def test_sec_common_share_earnings_require_actual_matching_tag(self):
        data = company()
        unqualified = extract(data)
        self.assertEqual(unqualified["net_income"], 160)
        self.assertIsNone(unqualified["income_common"])
        self.assertEqual(unqualified["earnings_definition"], "consolidated_unqualified")
        tags = data["facts"]["us-gaap"]
        tags["NetIncomeLossAvailableToCommonStockholdersBasic"] = deepcopy(tags["NetIncomeLoss"])
        qualified = extract(data)
        self.assertEqual(qualified["income_common"], 160)
        self.assertEqual(qualified["earnings_definition"], "common_shareholders")

    def test_consolidated_income_is_not_common_equity_yield(self):
        bundle, config = sample()
        bundle["fundamentals"]["earnings_definition"] = "consolidated_unqualified"
        result = score_securities(bundle, config)
        self.assertTrue(result.earnings_yield.isna().all())
        self.assertTrue(result.score.isna().all())
        self.assertTrue(result.gross_profitability.notna().any())

    def test_screen_does_not_infer_domicile_from_ticker_or_currency(self):
        bundle, config = sample()
        bundle["securities"]["domicile"] = None
        result = score_securities(bundle, config)
        self.assertFalse(result.eligible.any())
        self.assertTrue(result.score.isna().all())
        self.assertTrue(all("us_domicile_unverified" in reasons for reasons in result.reasons))

    def test_liquidity_screen_needs_sixty_valid_observations(self):
        bundle, config = sample()
        # Reset this fixture's repeated indices to select one actual latest observation.
        bundle["prices"] = bundle["prices"].reset_index(drop=True)
        latest = bundle["prices"].index[bundle["prices"].security_id.eq("A")][-1]
        bundle["prices"].loc[latest, "volume"] = None
        result = score_securities(bundle, config).set_index("security_id")
        self.assertIn("liquidity_ineligible", result.loc["A", "reasons"])

    def test_momentum_uses_actual_month_end_session_not_holiday_rows(self):
        bundle, config = sample()
        bundle["as_of"] = "2024-04-30"
        prices = bundle["prices"].reset_index(drop=True)
        for date, value in [("2023-04-28", 100.0), ("2024-03-28", 110.0), ("2024-03-29", 1000.0)]:
            prices.loc[prices.date.eq(date) & prices.security_id.eq("A"), "adjusted_close"] = value
        bundle["prices"] = prices
        row = score_securities(bundle, config).set_index("security_id").loc["A"]
        self.assertEqual(row["momentum_end_session"], "2024-03-28")  # Good Friday was closed.
        self.assertAlmostEqual(row["raw_momentum"], 0.1)
        bundle["prices"] = prices[~(prices.date.eq("2024-03-28") & prices.security_id.eq("A"))]
        row = score_securities(bundle, config).set_index("security_id").loc["A"]
        self.assertTrue(pd.isna(row["raw_momentum"]))

    def test_five_year_risk_uses_same_assets_without_reweighting(self):
        bundle, config = sample()
        config["risk"]["bootstrap_samples"] = 50
        result = risk_analysis(bundle, config)
        sensitivity = result["five_year_sensitivity"]
        self.assertEqual(sensitivity["status"], "complete")
        self.assertGreater(sensitivity["observations"], result["observations"])
        self.assertGreater(sensitivity["annualized_volatility"], 0)


if __name__ == "__main__":
    unittest.main()
