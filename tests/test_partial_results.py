"""Usable partial results: covered-holdings weights never change a reconciled run.

The reconciled expectations in ``support/partial_results_baseline.json`` were produced
by the engine before the ``basis`` keyword existed, from the synthetic demo inputs at
2026-08-31 with ``risk.bootstrap_samples = 50``.
"""

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

import numpy as np
import pandas as pd

from portfolio_lab import metrics
from portfolio_lab.allocation import build_proposals
from portfolio_lab.analytics import analyze, json_safe
from portfolio_lab.config import DEFAULTS, load_config, validate_config
from portfolio_lab.demo import create_demo
from portfolio_lab.pipeline import load_inputs

BASELINE = Path(__file__).parent / "support" / "partial_results_baseline.json"
COVERED_VALUE = 160_000.0


class PartialResultsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.config = load_config(create_demo(cls.temp.name))
        cls.config["risk"]["bootstrap_samples"] = 50
        cls.bundle = load_inputs(cls.config, "2026-08-31")
        cls.baseline = json.loads(BASELINE.read_text())

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def unreconciled(self):
        """The demo with one account NAV missing; every position stays valued."""
        bundle = deepcopy(self.bundle)
        accounts = bundle["accounts"]
        accounts.loc[accounts.account_id == "Retirement_B", "total_value"] = np.nan
        return bundle

    def assert_same(self, actual, expected, path="result"):
        if isinstance(expected, dict):
            self.assertIsInstance(actual, dict, path)
            self.assertEqual(sorted(actual), sorted(expected), f"{path}: field names differ")
            for key in expected:
                self.assert_same(actual[key], expected[key], f"{path}.{key}")
        elif isinstance(expected, list):
            self.assertIsInstance(actual, list, path)
            self.assertEqual(len(actual), len(expected), f"{path}: length differs")
            for index, item in enumerate(expected):
                self.assert_same(actual[index], item, f"{path}[{index}]")
        elif isinstance(expected, float):
            self.assertIsInstance(actual, float, path)
            self.assertAlmostEqual(actual, expected, places=12, msg=path)
        else:
            self.assertEqual(actual, expected, path)

    # A reconciled run keeps every number it had before the basis keyword existed.

    def test_default_basis_matches_explicit_reconciled_nav(self):
        risk = json_safe(metrics.risk_analysis(self.bundle, self.config))
        explicit = json_safe(
            metrics.risk_analysis(self.bundle, self.config, basis="reconciled_nav")
        )
        self.assert_same(explicit, risk, "risk")
        scenarios = json_safe(metrics.scenario_analysis(self.bundle, self.config))
        explicit_scenarios = json_safe(
            metrics.scenario_analysis(self.bundle, self.config, basis="reconciled_nav")
        )
        self.assert_same(explicit_scenarios, scenarios, "scenarios")

    def test_reconciled_results_match_the_pre_change_engine(self):
        self.assert_same(
            json_safe(metrics.risk_analysis(self.bundle, self.config)),
            self.baseline["risk"],
            "risk",
        )
        self.assert_same(
            json_safe(metrics.scenario_analysis(self.bundle, self.config)),
            self.baseline["scenarios"],
            "scenarios",
        )

    def test_complete_run_carries_no_covered_scope_labels(self):
        result = analyze(self.bundle, self.config)
        self.assertTrue(result["summary"]["complete"])
        for output in ("risk", "scenarios"):
            self.assertNotIn("scope", result[output])
            self.assertNotIn("denominator", result[output])
        self.assertNotIn("covered holdings only", result["risk"]["history_label"])
        self.assert_same(result["risk"], self.baseline["risk"], "analyze.risk")
        self.assert_same(result["scenarios"], self.baseline["scenarios"], "analyze.scenarios")

    # An unreconciled run still measures the holdings it can see.

    def test_covered_basis_weights_state_their_own_denominator(self):
        bundle = self.unreconciled()
        weights, issues = metrics._global_weights(bundle, self.config, basis="covered_value")
        self.assertAlmostEqual(sum(weights.values()), 1.0, places=12)
        self.assertAlmostEqual(weights["CASH"], 16_000 / COVERED_VALUE, places=12)
        self.assertAlmostEqual(weights["SIM01"], 12_000 / COVERED_VALUE, places=12)
        info = [i for i in issues if i["code"] == "holdings_only_denominator"]
        self.assertEqual(len(info), 1)
        self.assertEqual(info[0]["severity"], "info")
        self.assertIn("160,000.00 USD", info[0]["message"])
        self.assertIn("not reconciled", info[0]["message"])

    def test_default_basis_still_refuses_an_unreconciled_portfolio(self):
        bundle = self.unreconciled()
        weights, issues = metrics._global_weights(bundle, self.config)
        self.assertEqual(weights, {})
        self.assertIn("incomplete_portfolio", {i["code"] for i in issues})
        self.assertEqual(metrics.risk_analysis(bundle, self.config)["status"], "incomplete")

    def test_covered_risk_and_scenarios_report_numbers_and_scope(self):
        bundle = self.unreconciled()
        result = analyze(bundle, self.config)
        self.assertFalse(result["summary"]["complete"])
        risk = result["risk"]
        self.assertEqual(risk["scope"], "covered_holdings")
        self.assertEqual(risk["reconciled_result"], "incomplete")
        self.assertEqual(risk["status"], "complete")
        self.assertAlmostEqual(
            risk["annualized_volatility"],
            self.baseline["risk"]["annualized_volatility"],
            places=12,
        )
        self.assertEqual(
            risk["denominator"],
            {"basis": "covered_value", "value": COVERED_VALUE, "currency": "USD"},
        )
        self.assertIn("· covered holdings only", risk["history_label"])
        scenarios = result["scenarios"]
        self.assertEqual(scenarios["scope"], "covered_holdings")
        self.assertEqual(scenarios["denominator"]["value"], COVERED_VALUE)
        self.assertAlmostEqual(
            scenarios["weighted_return"],
            self.baseline["scenarios"]["weighted_return"],
            places=12,
        )
        first = scenarios["scenarios"][0]
        self.assertAlmostEqual(
            first["terminal_value"], COVERED_VALUE * (1 + first["return"]), places=6
        )
        codes = {i["code"] for i in result["issues"] if i["severity"] == "info"}
        self.assertIn("holdings_only_denominator", codes)
        self.assertEqual(result["allocation"]["status"], "blocked")

    def test_positions_without_a_known_value_leave_the_denominator(self):
        bundle = self.unreconciled()
        positions = bundle["positions"]
        target = positions.index[
            (positions.account_id == "Retirement_A") & (positions.security_id == "SIM01")
        ][0]
        positions.loc[target, "market_value"] = np.nan
        weights, issues = metrics._global_weights(bundle, self.config, basis="covered_value")
        self.assertNotIn("SIM01", weights)
        self.assertAlmostEqual(sum(weights.values()), 1.0, places=12)
        self.assertAlmostEqual(weights["SIM06"], 14_000 / 148_000, places=12)
        message = next(i for i in issues if i["code"] == "holdings_only_denominator")["message"]
        self.assertIn("148,000.00 USD", message)

    def test_covered_basis_does_not_accept_supplied_weights(self):
        supplied = pd.DataFrame(
            [{"account_id": "Retirement_A", "security_id": "SIM01", "weight": 1.0}]
        )
        weights, issues = metrics._global_weights(
            self.unreconciled(), self.config, supplied, basis="covered_value"
        )
        self.assertEqual(weights, {})
        self.assertIn("unsupported_basis", {i["code"] for i in issues})

    def test_unknown_basis_is_rejected(self):
        with self.assertRaises(ValueError):
            metrics._global_weights(self.bundle, self.config, basis="nav")

    # The allocation ledger check reads its tolerance from the configuration.

    def test_valuation_tolerance_governs_the_quantity_times_close_check(self):
        bundle = deepcopy(self.bundle)
        prices = bundle["prices"]
        rows = prices.index[prices.security_id == "SIM06"]
        prices.loc[rows[-1], "close"] = float(prices.loc[rows[-1], "close"]) * 1.005
        scores = metrics.score_securities(bundle, self.config)
        strict = build_proposals(bundle, self.config, scores)
        self.assertIn("valuation_price_mismatch", {i["code"] for i in strict["issues"]})
        tolerant = deepcopy(self.config)
        tolerant["allocation"]["valuation_tolerance"] = 0.02
        relaxed = build_proposals(bundle, tolerant, scores)
        self.assertNotIn("valuation_price_mismatch", {i["code"] for i in relaxed["issues"]})


class ValuationToleranceConfigTests(unittest.TestCase):
    def test_default_and_validated_range(self):
        self.assertEqual(DEFAULTS["allocation"]["valuation_tolerance"], 0.001)
        config = deepcopy(DEFAULTS)
        config["allocation"]["valuation_tolerance"] = 0.02
        self.assertEqual(validate_config(config)["allocation"]["valuation_tolerance"], 0.02)
        for invalid in (-0.001, 0.2, None, "0.01"):
            config = deepcopy(DEFAULTS)
            config["allocation"]["valuation_tolerance"] = invalid
            with self.assertRaises(ValueError):
                validate_config(config)


if __name__ == "__main__":
    unittest.main()
