"""Old saved reviews gain a computed view without rewriting their frozen record."""

import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from portfolio_lab.config import load_config
from portfolio_lab.demo import create_demo
from portfolio_lab.ingestion import _pack_bundle
from portfolio_lab.pipeline import load_inputs
from portfolio_research.application import analyze_review
from portfolio_research.holding_analysis import automatic_eps
from portfolio_research.saved_view import BACKEND_VERSION, CAPABILITIES, saved_holding_view
from portfolio_research.service import ResearchService
from tests.support.monthly_journey import _inject
from tests.test_holding_analysis import fixture


class SavedHoldingViewTests(unittest.TestCase):
    def test_native_frozen_facts_are_supported_without_changing_archive_metadata(self):
        _, bundle, config, result = fixture()
        result.update(run_id="legacy-native", metadata={"method_version": "historical-method"})
        before, frozen = deepcopy(result), _pack_bundle(bundle)
        with patch("portfolio_lab.pipeline.enrich_bundle", side_effect=AssertionError("network")):
            displayed = saved_holding_view(result, bundle, config)
        row = displayed["holding_analysis"][0]
        self.assertEqual(row["action"], "Buy")
        self.assertEqual(row["model"], "eps_multiple")
        self.assertEqual(len(row["scenarios"]), 3)
        self.assertEqual(displayed["metadata"]["method_version"], "historical-method")
        self.assertEqual(
            displayed["metadata"]["computed_views"]["holding_analysis"]["source"],
            "frozen_saved_inputs",
        )
        self.assertEqual(result, before)
        self.assertEqual(_pack_bundle(bundle), frozen)

    def test_existing_saved_holding_results_remain_the_original_results(self):
        _, bundle, config, result = fixture()
        result["holding_analysis"] = [
            {"security_id": "EQUITY", "action": "Hold", "method_version": "original"}
        ]
        displayed = saved_holding_view(result, bundle, config)
        self.assertEqual(displayed, result)
        self.assertNotIn("computed_views", displayed.get("metadata", {}))

    def test_genuine_missing_evidence_keeps_the_review_gate(self):
        _, bundle, config, result = fixture()
        bundle["research_inputs"] = {}
        displayed = saved_holding_view(result, bundle, config)
        row = displayed["holding_analysis"][0]
        self.assertEqual(row["action"], "Review")
        self.assertEqual(row["status"], "unavailable")
        self.assertIn("dated price", row["reason"])
        self.assertIsNone(row["valuation_input"])
        self.assertEqual(row["next_step"]["code"], "price_missing")
        self.assertEqual(row["next_step"]["section"], "data")

    def test_missing_manual_inputs_are_not_replaced_by_an_automatic_recommendation(self):
        security, bundle, config, result = fixture()
        payload, _ = automatic_eps("EQUITY", security, bundle, config)
        payload["starting_price"] = None
        bundle["workspace"] = {"valuations": {"EQUITY": {"eps": payload, "origin": "manual"}}}
        displayed = saved_holding_view(result, bundle, config)
        row = displayed["holding_analysis"][0]
        self.assertEqual(row["action"], "Review")
        self.assertIsNone(row["scenario_return"])
        self.assertEqual(row["source_type"], "user_assumption")
        self.assertEqual(row["valuation_input"], payload)
        self.assertEqual(row["next_step"]["code"], "model_inputs_missing")
        self.assertEqual(row["next_step"]["section"], "research")

    def test_valid_company_model_without_benchmark_has_a_specific_next_step(self):
        _, bundle, config, result = fixture()
        result["forecast_inputs"] = []
        row = saved_holding_view(result, bundle, config)["holding_analysis"][0]
        self.assertEqual(row["action"], "Review")
        self.assertIsNotNone(row["scenario_return"])
        self.assertEqual(row["next_step"]["code"], "benchmark_missing")
        self.assertEqual(row["next_step"]["section"], "settings")

    def test_incompatible_archive_is_marked_as_view_unavailable(self):
        result = {"run_id": "legacy-incompatible", "metadata": {"method_version": "old"}}
        displayed = saved_holding_view(result, None, {})
        self.assertEqual(displayed["holding_analysis"], [])
        view = displayed["metadata"]["computed_views"]["holding_analysis"]
        self.assertEqual(view["status"], "unavailable")
        self.assertTrue(view["archive_unchanged"])
        self.assertEqual(result["metadata"], {"method_version": "old"})


class LegacyServiceHoldingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.config = load_config(create_demo(cls.temp.name))
        cls.config["risk"]["bootstrap_samples"] = 50
        cls.service = ResearchService(cls.config)
        cls.bundle = load_inputs(cls.config, "2026-08-31")
        _inject(cls.bundle, cls.config, "2026-08-31")
        old_result = analyze_review(cls.bundle, cls.config)
        old_result.pop("holding_analysis")
        old_result["company_research"] = {}
        cls.run_id = cls.service.store.save_run(old_result, cls.config, cls.bundle)

    @classmethod
    def tearDownClass(cls):
        cls.service.close()
        cls.temp.cleanup()

    def test_old_archive_opens_with_ready_cases_from_retained_proposals(self):
        source_database = Path(self.config["source"]["path"])
        source_bytes = source_database.read_bytes()
        stored = self.service.store.load_run(self.run_id)
        frozen = _pack_bundle(self.service.store.load_bundle(self.run_id))
        runs = self.service.store.list_runs()
        with self.service.store.connect() as connection:
            raw = tuple(
                connection.execute(
                    "SELECT * FROM research_runs WHERE run_id=?", (self.run_id,)
                ).fetchone()
            )
        with (
            patch("portfolio_lab.pipeline.enrich_bundle", side_effect=AssertionError("network")),
            patch(
                "portfolio_research.market_data.estimates", side_effect=AssertionError("network")
            ),
        ):
            response = self.service.run(self.run_id)
        result = response["result"]
        rows = {row["security_id"]: row for row in result["holding_analysis"]}
        for sid in ("SIM01", "SIM03"):
            self.assertNotIn("valuation_facts", self.bundle["research_inputs"][sid])
            row = rows[sid]
            self.assertEqual(row["source_type"], "retained_proposal_draft")
            self.assertEqual(row["model"], "eps_multiple")
            self.assertTrue(all(case["return_value"] is not None for case in row["scenarios"]))
            self.assertIn(row["action"], {"Buy", "Sell", "Hold"})
        self.assertEqual(result["metadata"]["method_version"], stored["metadata"]["method_version"])
        self.assertEqual(
            result["metadata"]["computed_views"]["holding_analysis"]["base_run_id"], self.run_id
        )
        self.assertEqual(
            response["workspace"],
            self.bundle.get("workspace", {"assessments": {}, "valuations": {}}),
        )
        self.assertEqual(self.service.store.load_run(self.run_id), stored)
        self.assertEqual(_pack_bundle(self.service.store.load_bundle(self.run_id)), frozen)
        self.assertEqual(self.service.store.list_runs(), runs)
        self.assertEqual(source_database.read_bytes(), source_bytes)
        with self.service.store.connect() as connection:
            after = tuple(
                connection.execute(
                    "SELECT * FROM research_runs WHERE run_id=?", (self.run_id,)
                ).fetchone()
            )
        self.assertEqual(after, raw)

    def test_status_identifies_the_backend_capabilities(self):
        status = self.service.status()
        self.assertEqual(status["backend_version"], BACKEND_VERSION)
        self.assertEqual(status["capabilities"], CAPABILITIES)
        self.assertEqual(status["schema_version"], 1)
        status["capabilities"]["holding_analysis"] = 999
        self.assertEqual(self.service.status()["capabilities"]["holding_analysis"], 1)
