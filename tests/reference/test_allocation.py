"""Account funding and explicit evidence gates, using deterministic risk inputs.

Covariance estimation has separate tests. These tests isolate portfolio decisions
and recompute economic identities instead of matching a particular solver vector.
"""

import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

from portfolio_lab.allocation import build_proposals
from portfolio_lab.config import validate_config


def fixture():
    securities = pd.DataFrame(
        [
            dict(
                security_id=sid,
                ticker=sid,
                issuer_id="issuer_" + sid,
                name=sid,
                sector=sector,
                instrument_type="equity",
                currency="USD",
                eligible=True,
            )
            for sid, sector in [("A", "Industrials"), ("B", "Healthcare"), ("C", "Technology")]
        ]
    )
    positions = pd.DataFrame(
        [
            dict(
                account_id="r1",
                security_id=sid,
                quantity=450.0,
                price=100.0,
                market_value=45000.0,
                currency="USD",
                valuation_date="2026-08-31",
            )
            for sid in ["A", "B"]
        ]
    )
    accounts = pd.DataFrame(
        [
            dict(
                account_id="r1",
                account_type="retirement",
                currency="USD",
                total_value=100000.0,
                cash=10000.0,
                complete=True,
            )
        ]
    )
    bundle = dict(
        as_of="2026-08-31",
        mode="offline",
        positions=positions,
        accounts=accounts,
        securities=securities,
        issues=[],
        sources=[],
        prices=pd.DataFrame(
            [
                dict(
                    security_id=sid,
                    date="2026-08-31",
                    close=100.0,
                    available_at="2026-08-31T20:00:00Z",
                    received_at="2026-08-31T20:00:00Z",
                )
                for sid in ["A", "B", "C"]
            ]
        ),
        forecasts=pd.DataFrame(
            [
                dict(
                    security_id=sid,
                    scenario=scenario,
                    horizon_months=12,
                    return_value=returns[sid],
                    probability=probability,
                    basis="subjective",
                    source="test",
                    forecast_date="2026-08-31",
                )
                for scenario, probability, returns in [
                    ("adverse", 0.3, {"A": -0.1, "B": -0.15, "C": -0.2}),
                    ("central", 0.7, {"A": 0.03, "B": 0.12, "C": 0.5}),
                ]
                for sid in ["A", "B", "C"]
            ]
        ),
        fund_holdings=pd.DataFrame(),
        tax_lots=pd.DataFrame(),
    )
    config = validate_config(
        {
            "mandate": dict(
                confirmed=True,
                base_currency="USD",
                benchmark_id="B",
                issuer_cap=0.65,
                sector_cap=0.8,
                min_cash_weight=0.02,
                max_turnover=1.0,
                max_volatility=0.8,
                max_stress_loss=0.5,
                account_permissions={"r1": ["A", "B", "C"]},
            ),
            "allocation": dict(
                optimize=True,
                cash_return=0.0,
                active_sleeve_weight=0.20,
                sleeve_budget_basis="account_nav",
                sleeve_membership={"r1": {"A": 0.10, "B": 0.10}},
                incumbent_gap_weight=0.0,
                incumbent_gap_fraction=0.0,
                transaction_cost_bps=25.0,
                min_trade_value=0.0,
                min_trade_weight=0.0,
                return_hurdle=0.0,
            ),
        }
    )
    return bundle, config


class AllocationTests(unittest.TestCase):
    def setUp(self):
        self.bundle, self.config = fixture()
        self.scores = pd.DataFrame({"security_id": ["A", "B", "C"], "composite": [0.3, 0.7, 0.95]})
        self.covariance = patch(
            "portfolio_lab.metrics.portfolio_covariance",
            side_effect=lambda b, c, ids: {
                "ids": ids,
                "matrix": np.eye(len(ids)) * 0.04,
                "complete": True,
                "observations": 156,
                "issues": [],
            },
        )
        self.stress = patch(
            "portfolio_lab.metrics.stress_returns",
            side_effect=lambda b, c, ids: {"recession": {sid: -0.3 for sid in ids}},
        )
        self.covariance.start()
        self.stress.start()
        self.addCleanup(self.covariance.stop)
        self.addCleanup(self.stress.stop)

    def run_proposals(self):
        return build_proposals(self.bundle, self.config, self.scores)

    def assert_blocked(self, code):
        result = self.run_proposals()
        self.assertEqual(result["status"], "blocked", result)
        self.assertEqual(result["proposals"], [])
        self.assertIn(code, {i["code"] for i in result["issues"]})

    def test_unconfirmed_mandate_blocks(self):
        self.config["mandate"]["confirmed"] = False
        self.assert_blocked("mandate_unconfirmed")

    def test_missing_cash_is_not_filled_with_portfolio_residual(self):
        self.bundle["accounts"].loc[0, "cash"] = np.nan
        self.assert_blocked("unknown_account_cash")

    def test_unreconciled_total_is_not_available_capital(self):
        self.bundle["accounts"].loc[0, "total_value"] = 125000.0
        self.assert_blocked("account_not_reconciled")

    def test_unknown_permissions_block(self):
        self.config["mandate"]["account_permissions"] = {}
        self.assert_blocked("missing_account_permissions")

    def test_missing_forecasts_and_stale_dates_block(self):
        original = self.bundle["forecasts"].copy()
        self.bundle["forecasts"] = original[original.security_id != "C"]
        self.assert_blocked("missing_forecasts")
        self.bundle["forecasts"] = original
        self.bundle["forecasts"].loc[
            self.bundle["forecasts"].security_id == "A", "forecast_date"
        ] = "2025-01-01"
        self.assert_blocked("stale_forecast")

    def test_new_partial_forecast_does_not_borrow_old_scenarios(self):
        self.bundle["forecasts"].loc[0, "forecast_date"] = "2026-08-30"
        self.assert_blocked("incomplete_joint_scenarios")

    def test_after_close_forecast_cannot_replace_known_joint_set(self):
        baseline = self.run_proposals()
        late = self.bundle["forecasts"].iloc[0].copy()
        late["forecast_date"] = "2026-08-31T20:00:01Z"  # 16:00:01 New York
        late["return_value"] = 5.0
        self.bundle["forecasts"] = pd.concat(
            [self.bundle["forecasts"], pd.DataFrame([late])], ignore_index=True
        )
        result = self.run_proposals()
        self.assertIn(result["status"], {"draft", "review_ready"})
        self.assertEqual(result["comparison"], baseline["comparison"])

    def test_after_close_price_publication_is_not_trade_price(self):
        self.bundle["prices"].loc[0, "available_at"] = "2026-08-31T20:00:01Z"
        self.assert_blocked("missing_trade_prices")

    def test_strict_receipt_policy_excludes_later_received_price(self):
        self.config["data"]["require_received_by_cutoff"] = True
        self.bundle["forecasts"]["received_at"] = "2026-08-31T19:00:00Z"
        self.bundle["prices"].loc[0, "received_at"] = "2026-08-31T20:00:01Z"
        self.assert_blocked("missing_trade_prices")

    def test_upstream_stage_failure_blocks_allocation(self):
        self.bundle["issues"] = [
            {"severity": "error", "code": "signals_failed", "message": "Arithmetic failure"}
        ]
        self.assert_blocked("upstream_analysis_failed")

    def test_editing_calibrated_forecasts_marks_subjective_whatif(self):
        self.bundle["forecasts"]["basis"] = "calibrated"
        self.bundle["forecasts"]["calibration_id"] = "frozen-training-v1"
        self.config["allocation"]["mode"] = "scenario"
        self.config["allocation"]["return_overrides"] = {"C": {"central": 0.6}}
        result = self.run_proposals()
        self.assertIn(result["status"], {"draft", "review_ready"})
        self.assertEqual(result["solver"]["forecast_basis"], "subjective")
        self.assertIn("calibration_not_validated", {i["code"] for i in result["issues"]})

    def test_simple_candidate_uses_explicit_residual_when_scores_missing(self):
        self.scores["composite"] = np.nan
        self.config["allocation"]["residual_security_id"] = "B"
        result = self.run_proposals()
        simple = next(
            c for c in result["comparison"] if c["candidate"] == "simple_equal_issuer_sleeve"
        )
        self.assertTrue(simple["feasible"])
        self.assertGreater(simple["execution_cost"], 0)

    def test_future_forecasts_and_missing_quantities_block(self):
        self.bundle["forecasts"].loc[0, "forecast_date"] = "2026-09-01"
        self.assert_blocked("incomplete_joint_scenarios")
        self.bundle["forecasts"].loc[0, "forecast_date"] = "2026-08-31"
        self.bundle["positions"].loc[0, "quantity"] = np.nan
        self.assert_blocked("missing_quantity")

    def test_joint_probabilities_are_not_silently_normalized(self):
        self.bundle["forecasts"].loc[
            self.bundle["forecasts"].scenario == "central", "probability"
        ] = 0.6
        self.assert_blocked("probability_sum")

    def test_calibrated_mode_requires_calibration_evidence(self):
        self.config["allocation"]["mode"] = "calibrated"
        self.assert_blocked("uncalibrated_forecast")

    def test_positive_costs_reduce_available_account_capital_once(self):
        result = self.run_proposals()
        self.assertIn(result["status"], {"draft", "review_ready"})
        candidate = next(c for c in result["comparison"] if c["candidate"] == "conditional_optimum")
        self.assertTrue(candidate["feasible"])
        self.assertGreater(candidate["execution_cost"], 0)
        self.assertFalse(result["solver"]["executable"])
        self.assertTrue(all(p["executable"] is False for p in result["proposals"]))
        fees = sum(p["estimated_cost"] for p in result["proposals"])
        self.assertAlmostEqual(fees, candidate["execution_cost"], places=5)
        account = candidate["accounts"][0]
        trades = sum(p["trade_value"] for p in result["proposals"])
        self.assertAlmostEqual(10000.0 - trades - fees, account["ending_cash"], places=4)
        self.assertAlmostEqual(account["budget_total"], 100000.0, places=4)
        self.assertGreaterEqual(account["ending_cash"], 2000.0 - 0.01)

    def test_capital_does_not_cross_account_boundaries(self):
        self.bundle["positions"] = pd.DataFrame(
            [
                dict(
                    account_id="r1",
                    security_id="A",
                    quantity=1000.0,
                    price=100.0,
                    market_value=100000.0,
                    currency="USD",
                    valuation_date="2026-08-31",
                ),
                dict(
                    account_id="r2",
                    security_id="B",
                    quantity=500.0,
                    price=100.0,
                    market_value=50000.0,
                    currency="USD",
                    valuation_date="2026-08-31",
                ),
            ]
        )
        self.bundle["accounts"] = pd.DataFrame(
            [
                dict(
                    account_id="r1",
                    account_type="retirement",
                    currency="USD",
                    total_value=100000.0,
                    cash=0.0,
                    complete=True,
                ),
                dict(
                    account_id="r2",
                    account_type="retirement",
                    currency="USD",
                    total_value=100000.0,
                    cash=50000.0,
                    complete=True,
                ),
            ]
        )
        self.config["mandate"].update(
            account_permissions={"r1": ["A"], "r2": ["B", "C"]},
            locked_security_ids=["A"],
            min_cash_weight=0.0,
        )
        result = self.run_proposals()
        self.assertIn(result["status"], {"draft", "review_ready"})
        self.assertFalse(any(p["account_id"] == "r1" for p in result["proposals"]))
        candidate = next(c for c in result["comparison"] if c["candidate"] == "conditional_optimum")
        for account in candidate["accounts"]:
            aid = account["account_id"]
            initial_cash = 0.0 if aid == "r1" else 50000.0
            trades = sum(p["trade_value"] for p in result["proposals"] if p["account_id"] == aid)
            fees = sum(p["estimated_cost"] for p in result["proposals"] if p["account_id"] == aid)
            self.assertAlmostEqual(initial_cash - trades - fees, account["ending_cash"], places=4)
            self.assertAlmostEqual(account["budget_total"], 100000.0, places=4)

    def test_multiple_classes_share_one_household_issuer_cap(self):
        self.bundle["securities"].loc[
            self.bundle["securities"].security_id == "C", ["issuer_id", "sector"]
        ] = ["issuer_B", "Healthcare"]
        result = self.run_proposals()
        self.assertIn(result["status"], {"draft", "review_ready"})
        current = {"A": 0.45, "B": 0.45, "C": 0.0}
        for proposal in result["proposals"]:
            current[proposal["security_id"]] = proposal["target_weight"]
        self.assertLessEqual(current["B"] + current["C"], 0.65 + 1e-6)

    def test_impossible_locked_position_does_not_relax_limits(self):
        self.config["mandate"].update(issuer_cap=0.3, locked_security_ids=["A"])
        result = self.run_proposals()
        self.assertEqual(result["status"], "infeasible", result)
        self.assertEqual(result["proposals"], [])
        self.assertIn("allocation_infeasible", {i["code"] for i in result["issues"]})

    def test_partial_fund_disclosures_block_certified_exposure_limits(self):
        self.bundle["securities"].loc[
            self.bundle["securities"].security_id == "C", "instrument_type"
        ] = "etf"
        self.bundle["fund_holdings"] = pd.DataFrame(
            [
                dict(
                    fund_id="C",
                    issuer_id="issuer_A",
                    weight=0.5,
                    holdings_date="2026-08-30",
                    available_at="2026-08-31",
                    source_id="test",
                )
            ]
        )
        self.assert_blocked("partial_fund_lookthrough")

    def test_complete_fund_holdings_share_caps_with_direct_positions(self):
        self.bundle["securities"].loc[
            self.bundle["securities"].security_id == "C", "instrument_type"
        ] = "etf"
        self.bundle["fund_holdings"] = pd.DataFrame(
            [
                dict(
                    fund_id="C",
                    issuer_id=issuer,
                    weight=weight,
                    holdings_date="2026-08-30",
                    available_at="2026-08-31",
                    source_id="test",
                )
                for issuer, weight in [("issuer_A", 0.9), ("issuer_B", 0.1)]
            ]
        )
        self.config["mandate"]["issuer_cap"] = 0.5
        result = self.run_proposals()
        self.assertIn(result["status"], {"draft", "review_ready"})
        weights = {"A": 0.45, "B": 0.45, "C": 0.0}
        for proposal in result["proposals"]:
            weights[proposal["security_id"]] = proposal["target_weight"]
        self.assertGreater(weights["C"], 0.0)
        self.assertLessEqual(weights["A"] + 0.9 * weights["C"], 0.5 + 1e-6)
        self.assertLessEqual(weights["B"] + 0.1 * weights["C"], 0.5 + 1e-6)

    def test_incomplete_covariance_prevents_certified_volatility_limit(self):
        with patch(
            "portfolio_lab.metrics.portfolio_covariance",
            return_value={"matrix": None, "complete": False},
        ):
            self.assert_blocked("incomplete_covariance")

    def test_binding_volatility_limit_uses_quadratic_solver_and_preserves_funding(self):
        self.config["mandate"].update(
            max_volatility=0.1,
            issuer_cap=1.0,
            sector_cap=1.0,
            min_cash_weight=0.0,
            max_turnover=2.0,
        )
        result = self.run_proposals()
        self.assertIn(result["status"], {"draft", "review_ready"})
        self.assertIn("SLSQP", result["solver"]["method"])
        candidate = next(c for c in result["comparison"] if c["candidate"] == "conditional_optimum")
        weights = {"A": 0.45, "B": 0.45, "C": 0.0}
        for proposal in result["proposals"]:
            weights[proposal["security_id"]] = proposal["target_weight"]
        measured_volatility = np.sqrt(0.04 * sum(weight**2 for weight in weights.values()))
        self.assertLessEqual(measured_volatility, 0.1 + 1e-6)
        self.assertAlmostEqual(measured_volatility, candidate["annualized_volatility"], places=7)
        self.assertAlmostEqual(candidate["accounts"][0]["budget_total"], 100000.0, places=4)
        self.assertTrue(candidate["feasible"])

    def test_stress_limit_includes_immediate_trading_costs(self):
        self.config["mandate"].update(
            max_stress_loss=0.12,
            issuer_cap=1.0,
            sector_cap=1.0,
            min_cash_weight=0.0,
            max_turnover=2.0,
        )
        self.config["allocation"]["transaction_cost_bps"] = 100.0
        result = self.run_proposals()
        self.assertIn(result["status"], {"draft", "review_ready"})
        candidate = next(c for c in result["comparison"] if c["candidate"] == "conditional_optimum")
        weights = {"A": 0.45, "B": 0.45, "C": 0.0}
        for proposal in result["proposals"]:
            weights[proposal["security_id"]] = proposal["target_weight"]
        fees = sum(p["estimated_cost"] for p in result["proposals"])
        immediate_stress_loss = 0.3 * sum(weights.values()) + fees / 100000.0
        self.assertGreater(fees, 0.0)
        self.assertLessEqual(immediate_stress_loss, 0.12 + 1e-6)
        self.assertAlmostEqual(
            candidate["max_modeled_stress_loss"], immediate_stress_loss, places=6
        )
        self.assertAlmostEqual(candidate["accounts"][0]["budget_total"], 100000.0, places=4)

    def test_taxable_account_disabled_remains_fixed(self):
        self.bundle["accounts"].loc[0, "account_type"] = "taxable"
        result = self.run_proposals()
        self.assertIn(result["status"], {"draft", "review_ready"})
        self.assertEqual(result["proposals"], [])
        self.assertIn("taxable_account_locked", {i["code"] for i in result["issues"]})

    def test_taxable_permission_without_verified_tax_inputs_cannot_trade(self):
        self.bundle["accounts"].loc[0, "account_type"] = "taxable"
        self.config["mandate"]["allow_taxable_proposals"] = True
        self.config["tax"].update(enabled=True, short_term_rate=0.35, long_term_rate=0.2)
        result = self.run_proposals()
        self.assertFalse(any(p["account_id"] == "r1" for p in result["proposals"]), result)
        self.assertFalse(result["solver"]["executable"])

    def enable_tax_lots(self, bases=(20.0, 40.0)):
        self.bundle["accounts"].loc[0, "account_type"] = "taxable"
        self.config["mandate"]["allow_taxable_proposals"] = True
        self.config["tax"].update(enabled=True, short_term_rate=0.35, long_term_rate=0.2)
        self.bundle["tax_lots"] = pd.DataFrame(
            [
                dict(
                    account_id="r1",
                    security_id=sid,
                    lot_id="lot_" + sid,
                    acquired_date="2020-01-01",
                    quantity=450.0,
                    basis_per_share=basis,
                    currency="USD",
                )
                for sid, basis in zip(["A", "B"], bases)
            ]
        )

    def test_appreciated_lot_tax_reserve_changes_sales_and_reconciles_funding(self):
        self.enable_tax_lots()
        result = self.run_proposals()
        self.assertIn(result["status"], {"draft", "review_ready"})
        candidate = next(c for c in result["comparison"] if c["candidate"] == "conditional_optimum")
        tax = sum(p["estimated_tax"] for p in result["proposals"])
        fees = sum(p["estimated_cost"] for p in result["proposals"])
        trades = sum(p["trade_value"] for p in result["proposals"])
        self.assertGreater(tax, 0.0)
        self.assertAlmostEqual(tax, candidate["conditional_tax_reserve"], places=4)
        self.assertAlmostEqual(
            10000.0 - trades - fees - tax, candidate["accounts"][0]["ending_cash"], places=4
        )
        self.assertAlmostEqual(candidate["accounts"][0]["budget_total"], 100000.0, places=4)
        self.assertAlmostEqual(
            candidate["estimated_terminal_wealth_before_tax"] - tax,
            candidate["estimated_terminal_wealth_after_cost_and_tax_reserve"],
            places=4,
        )
        for proposal in result["proposals"]:
            if proposal["trade_value"] < 0:
                expected_tax = (
                    -proposal["fractional_quantity_change"]
                    * (100.0 - {"A": 20.0, "B": 40.0}[proposal["security_id"]])
                    * 0.2
                )
                self.assertAlmostEqual(proposal["estimated_tax"], expected_tax, places=4)
        self.config["tax"].update(short_term_rate=0.0, long_term_rate=0.0)
        low_tax = self.run_proposals()
        self.assertIn(low_tax["status"], {"draft", "review_ready"})
        self.config["tax"].update(short_term_rate=0.8, long_term_rate=0.8)
        high_tax = self.run_proposals()
        self.assertIn(high_tax["status"], {"draft", "review_ready"})

        def sold(result):
            return sum(-p["trade_value"] for p in result["proposals"] if p["trade_value"] < 0)

        self.assertLess(sold(high_tax), sold(low_tax))

    def test_zero_gain_and_loss_lots_only_record_actual_sales(self):
        self.enable_tax_lots(bases=(100.0, 150.0))
        result = self.run_proposals()
        self.assertIn(result["status"], {"draft", "review_ready"})
        candidate = next(c for c in result["comparison"] if c["candidate"] == "conditional_optimum")
        self.assertAlmostEqual(candidate["conditional_tax_reserve"], 0.0, places=6)
        for proposal in result["proposals"]:
            self.assertAlmostEqual(proposal["estimated_tax"], 0.0, places=6)
            if proposal["trade_value"] < 0:
                self.assertAlmostEqual(
                    sum(lot["quantity"] for lot in proposal["lot_plan"]),
                    -proposal["fractional_quantity_change"],
                    places=5,
                )
                self.assertTrue(all(lot["loss_credit"] == 0 for lot in proposal["lot_plan"]))
            else:
                self.assertEqual(proposal["lot_plan"], [])

    def test_tax_lots_must_reconcile_to_entire_holding_quantity(self):
        self.enable_tax_lots()
        for incorrect_quantity in [400.0, 500.0]:
            with self.subTest(quantity=incorrect_quantity):
                self.bundle["tax_lots"].loc[0, "quantity"] = incorrect_quantity
                result = self.run_proposals()
                self.assertFalse(any(p["account_id"] == "r1" for p in result["proposals"]), result)
                self.assertIn("tax_analysis_unavailable", {i["code"] for i in result["issues"]})


if __name__ == "__main__":
    unittest.main()
