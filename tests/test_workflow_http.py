"""The local HTTP boundary of the monthly operation: token, shapes and unknown records."""

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from portfolio_research.repository import ResearchRepository
from portfolio_research.server import make_server
from portfolio_research.service import default_config

TERMINAL = {"complete", "failed", "cancelled"}


class WorkflowHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        directory = Path(self.temp.name)
        config = default_config(directory)
        config["risk"]["bootstrap_samples"] = 50
        self.server = make_server(config, port=0, collector_directory=directory)
        self.addCleanup(self.server.server_close)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        with self.request("/api/state") as response:
            self.token = json.load(response)["token"]

    def request(self, path, data=None, token=None):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-Local-Token"] = token
        body = json.dumps(data).encode() if data is not None else None
        return urlopen(Request(self.base + path, data=body, headers=headers), timeout=30)

    def finish(self, workflow_id, timeout=180):
        deadline = time.monotonic() + timeout
        while True:
            with self.request(f"/api/research/workflows/{workflow_id}") as response:
                workflow = json.load(response)
            if workflow["status"] in TERMINAL:
                return workflow
            if time.monotonic() > deadline:
                self.fail(f"Workflow never finished: {workflow}")
            time.sleep(0.05)

    def test_starting_an_operation_requires_the_local_token(self):
        with self.assertRaises(HTTPError) as caught:
            self.request("/api/research/workflows", {"operation_key": "http-operation-untrusted"})
        self.assertEqual(caught.exception.code, 403)
        with self.request("/api/research/workflows") as response:
            self.assertEqual(json.load(response), [])

    def test_one_click_starts_one_operation_and_a_second_click_reuses_it(self):
        payload = {"operation_key": "http-operation-first", "collect": False}
        with self.request("/api/research/workflows", payload, self.token) as response:
            self.assertEqual(response.status, 202)
            started = json.load(response)
        repeated = {"operation_key": "http-operation-second", "collect": False}
        with self.request("/api/research/workflows", repeated, self.token) as response:
            self.assertEqual(json.load(response)["workflow_id"], started["workflow_id"])
        with self.request("/api/research/workflows") as response:
            listed = json.load(response)
        self.assertEqual([row["workflow_id"] for row in listed], [started["workflow_id"]])
        finished = self.finish(started["workflow_id"])
        for key in (
            "workflow_id",
            "operation_key",
            "status",
            "stage",
            "review_kind",
            "stages",
            "providers",
            "batch_id",
            "run_id",
            "error",
            "created_at",
            "updated_at",
            "cancel_requested",
        ):
            with self.subTest(key=key):
                self.assertIn(key, finished)
        self.assertEqual(
            set(finished["stages"]),
            {"collecting", "resolving", "fetching", "analyzing", "publishing"},
        )

    def test_unknown_operations_are_not_found_and_cancel_needs_the_token(self):
        with self.assertRaises(HTTPError) as caught:
            self.request("/api/research/workflows/unknown-operation-id")
        self.assertEqual(caught.exception.code, 404)
        with self.assertRaises(HTTPError) as caught:
            self.request("/api/research/workflows/unknown-operation-id/cancel", {})
        self.assertEqual(caught.exception.code, 403)
        with self.assertRaises(HTTPError) as caught:
            self.request("/api/research/workflows/unknown-operation-id/cancel", {}, self.token)
        self.assertEqual(caught.exception.code, 404)

    def test_provider_health_is_readable_and_carries_no_secrets(self):
        with self.request("/api/research/providers/health") as response:
            health = json.load(response)
        names = [provider["name"] for provider in health["providers"]]
        for expected in (
            "chrome_extension",
            "yahoo_listings",
            "bank_of_canada_fx",
            "yahoo_prices",
            "sec",
            "fred",
        ):
            with self.subTest(provider=expected):
                self.assertIn(expected, names)
        self.assertEqual(health["mode"], "offline")
        serialized = json.dumps(health)
        self.assertNotIn(self.temp.name, serialized)
        self.assertNotIn("pairing_key", serialized)


class WorkflowRecoveryTests(unittest.TestCase):
    """A serving process that starts after a crash must not inherit a live operation."""

    def test_an_interrupted_operation_never_pins_the_next_review(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        directory = Path(temp.name)
        config = default_config(directory)
        config["risk"]["bootstrap_samples"] = 50
        repository = ResearchRepository(config["research"]["path"])
        workflow_id, _ = repository.create_workflow(
            "operation-from-a-crashed-run", "current", {"collect": False}
        )
        repository.update_workflow(workflow_id, status="running", stage="collecting")
        with repository.connect() as connection:
            connection.execute(
                "UPDATE review_workflows SET owner_pid=999999 WHERE workflow_id=?", (workflow_id,)
            )

        def probe(pid, signal):
            if pid == 999999:
                raise ProcessLookupError

        with patch("portfolio_research.repository.os.kill", side_effect=probe):
            server = make_server(config, port=0, collector_directory=directory)
        self.addCleanup(server.server_close)
        self.addCleanup(server.research.close)
        self.assertIsNone(server.research.status()["active_workflow"])
        recovered = server.research.workflow(workflow_id)
        self.assertEqual(recovered["status"], "failed")
        self.assertIn("start a new review", recovered["error"])
        started = server.research.start_workflow(
            {"operation_key": "operation-a-new-click-after-restart", "collect": False}
        )
        self.assertNotEqual(started["workflow_id"], workflow_id)
        self.assertFalse(started["reused"])


if __name__ == "__main__":
    unittest.main()
