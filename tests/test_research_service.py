import hashlib
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from portfolio_lab.config import load_config
from portfolio_lab.demo import create_demo
from portfolio_lab.pipeline import save_analysis
from portfolio_research.demo import demo_workspace
from portfolio_research.service import ResearchService


class ResearchServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.config = load_config(create_demo(Path(cls.temp.name) / "demo"))
        cls.config["risk"]["bootstrap_samples"] = 50
        cls.service = ResearchService(cls.config)
        cls.source = Path(cls.config["source"]["path"])
        cls.source_hash = hashlib.sha256(cls.source.read_bytes()).hexdigest()
        job = cls.service.submit(
            {
                "kind": "monthly",
                "as_of": "2026-08-31",
                "request_key": "initial-synthetic-monthly",
                "workspace": demo_workspace(),
            }
        )
        cls.initial = cls.service.wait(job["job_id"])
        if cls.initial["status"] != "complete":
            raise AssertionError(cls.initial["error"])
        cls.run_id = cls.initial["output"]["result"]["run_id"]

    @classmethod
    def tearDownClass(cls):
        cls.service.close()
        cls.temp.cleanup()

    def preview(self, probabilities=None):
        request = {
            "kind": "preview",
            "base_run_id": self.run_id,
            "request_key": uuid.uuid4().hex,
            "patch": {},
        }
        if probabilities:
            request["patch"] = {"allocation": {"probability_overrides": probabilities}}
        job = self.service.submit(request)
        result = self.service.wait(job["job_id"])
        self.assertEqual(result["status"], "complete", result["error"])
        return request, result

    def test_monthly_integrates_valuation_and_preserves_source(self):
        result = self.initial["output"]["result"]
        self.assertTrue(result["summary"]["complete"])
        company = result["company_research"]["SIM01"]
        self.assertAlmostEqual(company["eps"]["scenarios"][1]["total_return"], 0.11)
        self.assertEqual(company["dcf"]["status"], "ready")
        self.assertEqual(len(company["dcf_sensitivity"]["cells"]), 5)
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), self.source_hash)

    def test_duplicate_submission_is_one_job_and_different_payload_rejected(self):
        request, result = self.preview()
        repeated = self.service.submit(request)
        self.assertEqual(repeated["job_id"], result["job_id"])
        request["patch"] = {"allocation": {"transaction_cost_bps": 25}}
        with self.assertRaisesRegex(ValueError, "different inputs"):
            self.service.submit(request)

    def test_tabs_keep_explicit_bases_and_preview_does_not_write_run(self):
        before = len(self.service.store.list_runs())
        _, first = self.preview({"Adverse": 0.6, "Central": 0.3, "Favorable": 0.1})
        _, second = self.preview({"Adverse": 0.1, "Central": 0.3, "Favorable": 0.6})
        a, b = first["output"]["result"], second["output"]["result"]
        self.assertEqual(a["metadata"]["parent_run_id"], self.run_id)
        self.assertLess(a["scenarios"]["weighted_return"], b["scenarios"]["weighted_return"])
        self.assertEqual(len(self.service.store.list_runs()), before)
        self.assertEqual(
            self.service.run(self.run_id)["result"]["scenarios"],
            self.initial["output"]["result"]["scenarios"],
        )

    def test_save_commits_exact_preview_and_keeps_original_immutable(self):
        _, preview = self.preview({"Adverse": 0.6, "Central": 0.3, "Favorable": 0.1})
        job = self.service.submit(
            {"kind": "save", "preview_job_id": preview["job_id"], "request_key": uuid.uuid4().hex}
        )
        saved = self.service.wait(job["job_id"])
        self.assertEqual(saved["status"], "complete", saved["error"])
        child = saved["output"]["result"]
        self.assertEqual(child["scenarios"], preview["output"]["result"]["scenarios"])
        self.assertNotEqual(child["run_id"], self.run_id)
        self.assertFalse(child["metadata"]["preview"])
        self.assertEqual(
            self.service.run(self.run_id)["result"]["scenarios"],
            self.initial["output"]["result"]["scenarios"],
        )
        self.assertTrue(self.service.compare(self.run_id, child["run_id"])["assumption_changes"])

    def test_export_failure_preserves_successful_archive(self):
        _, preview = self.preview()
        internal = self.service.store.job(preview["job_id"])["output"]
        from portfolio_lab.ingestion import _unpack_bundle

        with patch(
            "portfolio_lab.pipeline.export_report",
            side_effect=OSError("synthetic unwritable report"),
        ):
            saved = save_analysis(
                internal["result"], internal["config"], _unpack_bundle(internal["bundle"])
            )
        self.assertIn("failed", saved["metadata"]["export_status"])
        self.assertEqual(
            self.service.store.load_run(saved["run_id"])["summary"], internal["result"]["summary"]
        )

    def test_replay_never_calls_live_providers(self):
        with patch(
            "portfolio_lab.pipeline.enrich_bundle",
            side_effect=AssertionError("network refresh during preview"),
        ):
            self.preview()

    def test_public_run_has_no_private_configuration_or_source_paths(self):
        response = self.service.run(self.run_id)
        serialized = json.dumps(response)
        self.assertNotIn("saved_config", serialized)
        self.assertNotIn("positions_query", serialized)
        self.assertNotIn(str(self.source), serialized)
        self.assertNotIn("sec_user_agent", serialized)

    def test_missing_secret_or_source_overrides_cannot_be_saved(self):
        with self.assertRaises(ValueError):
            self.service.save_settings({"source": {"path": "elsewhere.sqlite"}})
        with self.assertRaises(ValueError):
            self.service.save_providers({"api_key": "synthetic-secret"})
        with self.assertRaises(ValueError):
            self.service.save_providers({"mode": "live"})

    def test_effective_overrides_are_frozen_for_future_evaluation(self):
        _, preview = self.preview({"Adverse": 0.6, "Central": 0.3, "Favorable": 0.1})
        forecasts = preview["output"]["result"]["forecast_inputs"]
        self.assertTrue(forecasts)
        self.assertEqual(
            {row["probability"] for row in forecasts if row["scenario"] == "Adverse"}, {0.6}
        )
        self.assertTrue(all(row["basis"] == "subjective" for row in forecasts))

    def test_monthly_checks_prior_forecasts_without_rewriting_them(self):
        original = self.service.store.load_run(self.run_id)
        request = {"kind": "monthly", "as_of": "2026-09-01", "request_key": "next-monthly-test"}
        job = self.service.submit(request)
        finished = self.service.wait(job["job_id"])
        self.assertEqual(finished["status"], "complete", finished["error"])
        checks = [
            row
            for row in self.service.store.records("evaluation", self.run_id)
            if row["payload"].get("observation_run_id") == job["job_id"]
        ]
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0]["payload"]["summary"]["scored_count"], 0)
        self.assertEqual(self.service.store.load_run(self.run_id), original)
        self.assertEqual(self.service.submit(request)["job_id"], job["job_id"])
