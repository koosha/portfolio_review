"""Integration tests for the workflow's consequential boundaries."""

import hashlib
import json
import tempfile
import threading
import unittest
from copy import deepcopy
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from portfolio_lab.analytics import analyze
from portfolio_lab.config import dashboard_patch, load_config
from portfolio_lab.demo import create_demo
from portfolio_lab.ingestion import ResearchStore
from portfolio_lab.pipeline import replay_analysis, run_analysis
from portfolio_lab.web import make_server


class WorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.config = load_config(create_demo(cls.temp.name))
        cls.config["risk"]["bootstrap_samples"] = 50
        source = Path(cls.config["source"]["path"])
        cls.source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        cls.result, cls.bundle = run_analysis(cls.config, "2026-08-31")

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_demo_reconciles_and_separately_funds_accounts(self):
        r = self.result
        self.assertTrue(r["summary"]["complete"])
        self.assertEqual(r["summary"]["total_value"], 160000)
        self.assertEqual(r["quality_summary"]["error"], 0)
        self.assertEqual(r["allocation"]["status"], "review_ready")
        self.assertGreater(len(r["proposals"]), 0)
        current = {a["account_id"]: a["total_value"] for a in r["summary"]["accounts"]}
        optimum = next(
            x
            for x in r["allocation"]["comparison"]
            if x["candidate"] == r["allocation"]["selected_candidate"]
        )
        for account in optimum["accounts"]:
            self.assertAlmostEqual(
                account["budget_total"], current[account["account_id"]], places=4
            )
        self.assertEqual(
            hashlib.sha256(Path(self.config["source"]["path"]).read_bytes()).hexdigest(),
            self.source_hash,
        )
        self.assertTrue(all(not p["executable"] for p in r["proposals"]))

    def test_frozen_replay_preserves_numerical_results(self):
        replay, _, _ = replay_analysis(self.config, self.result["run_id"])
        self.assertEqual(replay["summary"], self.result["summary"])
        self.assertEqual(replay["risk"], self.result["risk"])
        self.assertEqual(replay["proposals"], self.result["proposals"])
        self.assertEqual(replay["metadata"]["parent_run_id"], self.result["run_id"])

    def test_changed_scenarios_change_results_without_mutating_archive(self):
        p = {
            "allocation": {
                "probability_overrides": {"Adverse": 0.6, "Central": 0.3, "Favorable": 0.1}
            }
        }
        replay, _, _ = replay_analysis(self.config, self.result["run_id"], p)
        self.assertLess(
            replay["scenarios"]["weighted_return"], self.result["scenarios"]["weighted_return"]
        )
        archived = ResearchStore(self.config["research"]["path"]).load_run(self.result["run_id"])
        self.assertEqual(archived["scenarios"], self.result["scenarios"])

    def test_upstream_error_and_missing_coverage_block_basket(self):
        b = deepcopy(self.bundle)
        b["issues"].append(
            {"severity": "error", "code": "INCOMPLETE_SNAPSHOT", "message": "Missing account input"}
        )
        r = analyze(b, self.config)
        self.assertEqual(r["allocation"]["status"], "blocked")
        self.assertEqual(r["proposals"], [])
        b = deepcopy(self.bundle)
        b["accounts"].loc[0, "total_value"] += 1000
        r = analyze(b, self.config)
        self.assertFalse(r["summary"]["complete"])
        self.assertEqual(r["proposals"], [])

    def test_configuration_clears_overrides_and_rejects_paths(self):
        c = dashboard_patch(
            self.config, {"allocation": {"return_overrides": {"SIM01": {"Central": 0.8}}}}
        )
        c = dashboard_patch(c, {"allocation": {"return_overrides": {}}})
        self.assertEqual(c["allocation"]["return_overrides"], {})
        c = dashboard_patch(c, {"allocation": {"return_overrides": {"SIM01": {"Central": 0.8}}}})
        c = dashboard_patch(c, {"allocation": {"horizon_months": 6}})
        self.assertEqual(c["allocation"]["return_overrides"], {})
        with self.assertRaises(ValueError):
            dashboard_patch(c, {"source": {"path": "different.sqlite"}})
        with self.assertRaises(ValueError):
            dashboard_patch(c, {"allocation": {"probability_overrides": {"Adverse": 0.8}}})

    def test_local_api_token_preview_and_path_boundary(self):
        server = make_server(self.config, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"

        def request(path, data=None, token=None, host=None):
            headers = {"Content-Type": "application/json"}
            if token:
                headers["X-Local-Token"] = token
            if host:
                headers["Host"] = host
            raw = json.dumps(data).encode() if data is not None else None
            return urlopen(Request(base + path, data=raw, headers=headers), timeout=20)

        try:
            with request("/api/state") as response:
                session = json.load(response)
            count = len(ResearchStore(self.config["research"]["path"]).list_runs())
            with self.assertRaises(HTTPError) as caught:
                request("/api/research/jobs", {"kind": "preview"})
            self.assertEqual(caught.exception.code, 403)
            with self.assertRaises(HTTPError) as caught:
                request("/api/research", host="attacker.example")
            self.assertEqual(caught.exception.code, 403)
            with self.assertRaises(HTTPError) as caught:
                request(
                    "/api/research/jobs",
                    {
                        "kind": "preview",
                        "request_key": "reject-path-1",
                        "base_run_id": self.result["run_id"],
                        "patch": {"source": {"path": "bad"}},
                    },
                    session["token"],
                )
            self.assertEqual(caught.exception.code, 400)
            with request(
                "/api/research/jobs",
                {
                    "kind": "preview",
                    "request_key": "workflow-preview-1",
                    "base_run_id": self.result["run_id"],
                    "patch": {},
                },
                session["token"],
            ) as response:
                job = json.load(response)
            preview = server.research.wait(job["job_id"])
            self.assertEqual(preview["status"], "complete", preview.get("error"))
            self.assertTrue(preview["output"]["result"]["metadata"]["preview"])
            self.assertNotIn("saved_config", preview["output"]["result"])
            self.assertEqual(len(ResearchStore(self.config["research"]["path"]).list_runs()), count)
            with request("/") as response:
                self.assertIn(b"Portfolio", response.read())
            with request("/app.js") as response:
                self.assertIn(b"renderSources", response.read())
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
