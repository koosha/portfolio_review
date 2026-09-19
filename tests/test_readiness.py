"""Per-output requirements: what each answer needs, and what is missing without it."""

import tempfile
import unittest
from copy import deepcopy

import numpy as np

from portfolio_lab import metrics
from portfolio_lab.analytics import analyze, json_safe
from portfolio_lab.config import DEFAULTS, load_config
from portfolio_lab.demo import create_demo
from portfolio_lab.pipeline import load_inputs
from portfolio_research.public import RESULT_FIELDS, public_result
from portfolio_research.readiness import _amount, readiness

EXPECTED_OUTPUTS = [
    "captured_holdings",
    "usd_value",
    "exposure",
    "risk",
    "stress",
    "company_valuation",
    "candidates",
    "baskets",
]


class ReadinessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.config = load_config(create_demo(cls.temp.name))
        cls.config["risk"]["bootstrap_samples"] = 50
        cls.bundle = load_inputs(cls.config, "2026-08-31")
        cls.result = analyze(cls.bundle, cls.config)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def rows(self, result=None, bundle=None, config=None):
        report = readiness(
            result if result is not None else self.result,
            bundle if bundle is not None else self.bundle,
            config if config is not None else self.config,
        )
        return {row["output"]: row for row in report}

    def test_every_documented_output_appears_once_in_order(self):
        report = readiness(self.result, self.bundle, self.config)
        self.assertEqual([row["output"] for row in report], EXPECTED_OUTPUTS)
        for row in report:
            self.assertEqual(
                sorted(row),
                ["method_version", "missing", "output", "requirements", "scope", "status"],
            )
            self.assertEqual(row["method_version"], "readiness-1")
            self.assertIn(row["status"], {"ready", "partial", "blocked"})
            self.assertTrue(row["scope"], row["output"])
            self.assertTrue(row["requirements"], row["output"])
            unmet = set()
            for requirement in row["requirements"]:
                self.assertEqual(sorted(requirement), ["detail", "met", "name"], row["output"])
                self.assertIsInstance(requirement["met"], bool)
                self.assertTrue(requirement["detail"])
                if not requirement["met"]:
                    unmet.add(requirement["name"])
            self.assertEqual(set(row["missing"]), unmet, row["output"])
            self.assertEqual(row["status"] == "ready", not unmet, row["output"])
        self.assertEqual(json_safe(report), report)

    def test_the_covered_subtotal_counts_only_the_base_currency(self):
        """The reported subtotal names the denominator the risk result actually used."""
        from portfolio_research.readiness import _covered_value

        summary = {
            "currency": "USD",
            "complete": False,
            "accounts": [
                {
                    "account_id": "a",
                    "currency": "USD",
                    "known_position_value": 100_000.0,
                    "cash": 5_000.0,
                    "complete": False,
                },
                {
                    "account_id": "b",
                    "currency": "CAD",
                    "known_position_value": 50_000.0,
                    "cash": 1_000.0,
                    "complete": False,
                },
                {
                    "account_id": "c",
                    "currency": "USD",
                    "known_position_value": 20_000.0,
                    "cash": -20_000.0,
                    "complete": False,
                },
            ],
        }
        self.assertAlmostEqual(_covered_value(summary, [], "USD"), 125_000.0)
        denominator = {"basis": "covered_value", "value": 125_000.0, "currency": "USD"}
        result = {
            "summary": summary,
            "holdings": [],
            "risk": {"denominator": denominator},
            "scenarios": {},
        }
        rows = self.rows(result=result, bundle={}, config=self.config)
        scope = rows["usd_value"]["scope"]
        self.assertIn("125,000.00 USD", scope)
        self.assertIn("b", scope)
        # The subtotal readiness reports is the denominator the risk result used.
        self.assertIn(_amount(denominator["value"], "USD"), rows["risk"]["scope"])

    def test_reconciled_demo_is_ready_for_holdings_value_exposure_risk_and_stress(self):
        rows = self.rows()
        for output in ("captured_holdings", "usd_value", "exposure", "risk", "stress"):
            self.assertEqual(rows[output]["status"], "ready", output)
        self.assertIn("NAV", rows["usd_value"]["scope"])
        self.assertIn("160,000.00 USD", rows["usd_value"]["scope"])
        observations = next(
            r for r in rows["risk"]["requirements"] if r["name"] == "common_weekly_observations"
        )
        self.assertIn("156", observations["detail"])
        self.assertIn(str(self.config["risk"]["min_weekly_observations"]), observations["detail"])
        self.assertEqual(rows["baskets"]["status"], "ready")

    def test_company_valuation_is_partial_and_names_the_missing_driver(self):
        row = self.rows()["company_valuation"]
        self.assertEqual(row["status"], "partial")
        self.assertEqual(row["missing"], ["estimates"])
        requirements = {r["name"]: r for r in row["requirements"]}
        self.assertTrue(requirements["statements"]["met"])
        self.assertTrue(requirements["price"]["met"])
        self.assertFalse(requirements["estimates"]["met"])
        self.assertIn("8 of 8", requirements["statements"]["detail"])
        # The held fund is not a company valuation subject.
        self.assertNotIn("SIMETF", requirements["statements"]["detail"])

    def test_missing_statements_are_confined_to_the_affected_security(self):
        bundle = deepcopy(self.bundle)
        fundamentals = bundle["fundamentals"]
        bundle["fundamentals"] = fundamentals[fundamentals.security_id != "SIM01"]
        row = self.rows(bundle=bundle)["company_valuation"]
        self.assertEqual(row["status"], "partial")
        self.assertIn("statements", row["missing"])
        statements = next(r for r in row["requirements"] if r["name"] == "statements")
        self.assertFalse(statements["met"])
        self.assertIn("SIM01", statements["detail"])
        self.assertNotIn("SIM03", statements["detail"])
        self.assertIn("7 of 8", statements["detail"])

    def test_coverage_report_answers_the_company_requirements_when_present(self):
        bundle = deepcopy(self.bundle)
        held = sorted({h["security_id"] for h in self.result["holdings"]})
        bundle["coverage"] = {
            "adapter_version": "yfinance-adapter-1",
            "by_security": {
                sid: {
                    "prices": "ok",
                    "statements": "missing" if sid == "SIM03" else "ok",
                    "estimates": "ok",
                }
                for sid in held
            },
        }
        row = self.rows(bundle=bundle)["company_valuation"]
        requirements = {r["name"]: r for r in row["requirements"]}
        self.assertTrue(requirements["estimates"]["met"])
        self.assertFalse(requirements["statements"]["met"])
        self.assertIn("SIM03", requirements["statements"]["detail"])
        self.assertEqual(row["status"], "partial")

    def test_unreconciled_accounts_move_value_and_risk_to_covered_scope(self):
        bundle = deepcopy(self.bundle)
        accounts = bundle["accounts"]
        accounts.loc[accounts.account_id == "Retirement_B", "total_value"] = np.nan
        result = analyze(bundle, self.config)
        rows = self.rows(result=result, bundle=bundle)
        self.assertEqual(rows["captured_holdings"]["status"], "ready")
        self.assertEqual(rows["usd_value"]["status"], "partial")
        self.assertIn("not total NAV", rows["usd_value"]["scope"])
        self.assertIn("account_nav_reconciled", rows["usd_value"]["missing"])
        self.assertEqual(rows["risk"]["status"], "partial")
        self.assertIn("covered holdings", rows["risk"]["scope"].lower())
        self.assertEqual(rows["baskets"]["status"], "blocked")
        self.assertIn("reconciled_account_funding", rows["baskets"]["missing"])

    def test_risk_requires_the_configured_minimum_common_weeks(self):
        config = deepcopy(self.config)
        config["risk"]["min_weekly_observations"] = 400
        result = dict(self.result, risk=json_safe(metrics.risk_analysis(self.bundle, config)))
        rows = self.rows(result=result, config=config)
        self.assertEqual(rows["risk"]["status"], "blocked")
        observations = next(
            r for r in rows["risk"]["requirements"] if r["name"] == "common_weekly_observations"
        )
        self.assertFalse(observations["met"])
        self.assertIn("400", observations["detail"])
        self.assertEqual(rows["stress"]["status"], "ready")

    def test_candidates_state_their_universe_scope(self):
        row = self.rows()["candidates"]
        self.assertIn("configured_universe", row["scope"])
        requirements = {r["name"]: r for r in row["requirements"]}
        self.assertTrue(requirements["scored_universe"]["met"])
        self.assertTrue(requirements["unowned_candidates"]["met"])
        self.assertIn("17", requirements["unowned_candidates"]["detail"])

    def test_an_empty_result_blocks_every_output_without_raising(self):
        report = readiness({}, {}, deepcopy(DEFAULTS))
        self.assertEqual([row["output"] for row in report], EXPECTED_OUTPUTS)
        self.assertEqual({row["status"] for row in report}, {"blocked"})
        for row in report:
            self.assertTrue(row["missing"])


class PublicResultFieldTests(unittest.TestCase):
    def test_new_fields_reach_the_browser_schema(self):
        for field in ("readiness", "research", "coverage", "fund_sectors"):
            self.assertIn(field, RESULT_FIELDS)
        result = {
            "readiness": [{"output": "risk", "status": "partial", "missing": ["benchmark"]}],
            "research": {"SIM01": {"brief": {"security_id": "SIM01"}}},
            "coverage": {"capabilities": {"prices": {"requested": 1, "available": 1}}},
            "fund_sectors": [{"fund_id": "SIMETF", "sector": "Technology", "weight": 0.4}],
        }
        published = public_result(result)
        for field, value in result.items():
            self.assertEqual(published[field], value)


if __name__ == "__main__":
    unittest.main()
