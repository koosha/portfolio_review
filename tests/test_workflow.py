"""One monthly operation: collect, resolve, fetch, analyze and publish exactly once.

A cancellation once seen stays seen, so a store that cannot answer a later probe never
resumes a fetch the owner stopped, and the prior run is read only for the one part the
next prioritisation compares against.
"""

import tempfile
import threading
import time
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from portfolio.companion import ChromeCompanion
from portfolio.storage import Store
from portfolio_research.calendar import decision_context
from portfolio_research.repository import ResearchRepository
from portfolio_research.service import ResearchService, default_config
from portfolio_research.workflow import PROSPECTIVE_STATUS
from tests.test_batches import table
from tests.test_current import cached_providers

AS_OF = "2026-09-18"
TERMINAL = {"complete", "failed", "cancelled"}
STAGES = ("collecting", "resolving", "fetching", "analyzing", "publishing")


class FakeChrome(threading.Thread):
    """A deterministic stand-in for the paired extension.

    It polls ``take()`` on a fixed interval and completes every claimed job with the
    fixture holdings table. ``delay`` holds a claimed job open so a test can act while
    the collection is running; ``failures`` names the sources that report a failure.
    """

    def __init__(self, companion, *, delay=0.0, failures=(), symbol="DEMO"):
        super().__init__(daemon=True)
        self.companion = companion
        self.delay = float(delay)
        self.failures = set(failures)
        self.symbol = symbol
        self.stopped = threading.Event()
        self.completed = []

    def run(self):
        while not self.stopped.wait(0.05):
            job = self.companion.take("1.2.0")
            if job is None:
                continue
            if self.delay and self.stopped.wait(self.delay):
                return
            ok = job["source_id"] not in self.failures
            try:
                self.companion.complete(
                    {
                        "id": job["id"],
                        "url": job["url"],
                        "ok": ok,
                        "table": table(self.symbol),
                        "error": "Synthetic capture failed.",
                    }
                )
            except ValueError:  # An expired job never stops the helper.
                continue
            self.completed.append(job["source_id"])

    def stop(self):
        self.stopped.set()
        self.join(timeout=10)


class WorkflowTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.ids = [
            self.store.add_source(name, url=f"https://finance.yahoo.com/portfolio/p_fixture_{name}")
            for name in ("A", "B")
        ]
        self.companion = ChromeCompanion(self.store)
        self.config = default_config(self.temp.name)
        self.config["risk"]["bootstrap_samples"] = 50
        self.service = ResearchService(self.config, companion=self.companion)
        self.addCleanup(self.service.close)

    def seed_collection(self, symbol="SEED"):
        batch = self.store.begin_batch(self.ids)
        for source_id in self.ids:
            self.store.ingest_table(source_id, table(symbol), batch_id=batch)
        return batch

    def start_chrome(self, **fields):
        chrome = FakeChrome(self.companion, **fields)
        self.addCleanup(chrome.stop)
        chrome.start()
        deadline = time.monotonic() + 10
        while not self.companion.status()["browser_open"]:
            if time.monotonic() > deadline:
                self.fail("The fake Chrome helper never polled for work.")
            time.sleep(0.02)
        return chrome

    def finish(self, workflow_id, timeout=180):
        deadline = time.monotonic() + timeout
        while True:
            workflow = self.service.workflow(workflow_id)
            if workflow["status"] in TERMINAL:
                return workflow
            if time.monotonic() > deadline:
                self.fail(f"Workflow never finished: {workflow}")
            time.sleep(0.05)

    def saved_run(self, as_of="2026-09-11"):
        """A previously published review, saved without running the analysis."""
        bundle = {
            "as_of": as_of,
            "mode": "offline",
            "timeline": decision_context(as_of),
        }
        return self.service.store.save_run({"metadata": {}, "summary": {}}, self.config, bundle)


class OneOperationTests(WorkflowTestCase):
    def test_one_operation_collects_and_publishes_a_usable_review(self):
        chrome = self.start_chrome()
        started = self.service.start_workflow(
            {"operation_key": "operation-fresh-review-1", "review_kind": "current"}
        )
        workflow = self.finish(started["workflow_id"])
        self.assertEqual(workflow["status"], "complete", workflow)
        for stage in STAGES:
            with self.subTest(stage=stage):
                self.assertIn(workflow["stages"][stage]["status"], {"complete", "skipped"})
        self.assertEqual(workflow["stages"]["collecting"]["status"], "complete")
        self.assertEqual(sorted(chrome.completed), sorted(self.ids))
        self.assertTrue(workflow["batch_id"])
        self.assertTrue(workflow["run_id"])
        run = self.service.run(workflow["run_id"])
        self.assertEqual(run["result"]["metadata"]["workflow_id"], workflow["workflow_id"])
        self.assertEqual(run["result"]["metadata"]["operation_key"], "operation-fresh-review-1")
        self.assertEqual(run["result"]["metadata"]["review_kind"], "current")
        self.assertEqual(self.service.current()["latest_run"]["run_id"], workflow["run_id"])
        status = self.service.status()
        self.assertIsNone(status["active_workflow"])
        self.assertEqual(status["last_workflow"]["workflow_id"], workflow["workflow_id"])

    def test_duplicate_clicks_reuse_the_active_operation(self):
        self.start_chrome(delay=1.5)
        first = self.service.start_workflow({"operation_key": "operation-duplicate-1"})
        second = self.service.start_workflow({"operation_key": "operation-duplicate-2"})
        self.assertEqual(second["workflow_id"], first["workflow_id"])
        self.assertTrue(second["reused"])
        self.assertEqual(self.finish(first["workflow_id"])["status"], "complete")
        third = self.service.start_workflow(
            {"operation_key": "operation-duplicate-3", "collect": False}
        )
        self.assertNotEqual(third["workflow_id"], first["workflow_id"])
        self.assertEqual(self.finish(third["workflow_id"])["status"], "complete")
        self.assertEqual(len(self.service.workflows()), 2)

    def test_cancelling_during_collection_leaves_the_last_review_unchanged(self):
        self.seed_collection()
        before = len(self.service.store.list_runs())
        self.start_chrome(delay=2.0)
        started = self.service.start_workflow({"operation_key": "operation-cancel-1"})
        cancelled = self.service.cancel_workflow(started["workflow_id"])
        self.assertTrue(cancelled["cancel_requested"])
        workflow = self.finish(started["workflow_id"])
        self.assertEqual(workflow["status"], "cancelled", workflow)
        self.assertIsNone(workflow["run_id"])
        self.assertEqual(len(self.service.store.list_runs()), before)
        self.assertEqual(workflow["stages"]["collecting"]["status"], "cancelled")
        self.assertIn(
            "last usable review is unchanged", workflow["stages"]["collecting"]["message"]
        )
        self.assertIsNone(self.service.status()["active_workflow"])

    def test_failed_accounts_do_not_block_the_review_or_blend_collections(self):
        published = self.seed_collection()
        self.start_chrome(failures=[self.ids[1]])
        started = self.service.start_workflow({"operation_key": "operation-partial-collection"})
        workflow = self.finish(started["workflow_id"])
        self.assertEqual(workflow["status"], "complete", workflow)
        collecting = workflow["stages"]["collecting"]
        self.assertEqual(collecting["status"], "failed")
        self.assertIn("Retry the failed accounts", collecting["action_needed"])
        self.assertFalse(collecting["detail"]["batch_published"])
        self.assertTrue(workflow["run_id"])
        self.assertEqual(workflow["stages"]["resolving"]["detail"]["batch"]["id"], published)

    def test_an_unpaired_extension_skips_collection_and_still_publishes(self):
        self.seed_collection()
        started = self.service.start_workflow({"operation_key": "operation-no-chrome-1"})
        workflow = self.finish(started["workflow_id"])
        self.assertEqual(workflow["status"], "complete", workflow)
        collecting = workflow["stages"]["collecting"]
        self.assertEqual(collecting["status"], "skipped")
        self.assertIn("Chrome extension not connected", collecting["action_needed"])
        self.assertTrue(workflow["run_id"])
        self.assertEqual(self.service.current()["latest_run"]["run_id"], workflow["run_id"])

    def test_one_failed_provider_never_stops_the_saved_review(self):
        self.seed_collection()
        config = deepcopy(self.config)
        config["data"].update(mode="live", price_provider="yahoo")
        service = ResearchService(config, companion=self.companion)
        self.addCleanup(service.close)
        with (
            cached_providers(),
            patch(
                "portfolio_lab.providers._enrich_yahoo",
                side_effect=RuntimeError("synthetic Yahoo outage"),
            ),
        ):
            started = service.start_workflow({"operation_key": "operation-provider-failure"})
            deadline = time.monotonic() + 180
            while service.workflow(started["workflow_id"])["status"] not in TERMINAL:
                if time.monotonic() > deadline:
                    self.fail("Workflow never finished.")
                time.sleep(0.05)
            workflow = service.workflow(started["workflow_id"])
        self.assertEqual(workflow["status"], "complete", workflow)
        self.assertEqual(workflow["providers"]["yahoo_prices"]["status"], "failed")
        self.assertTrue(workflow["run_id"])
        self.assertTrue(service.run(workflow["run_id"])["result"]["run_id"])

    def test_a_failed_analysis_saves_nothing_and_keeps_the_previous_review(self):
        self.seed_collection()
        previous = self.saved_run()
        failure = ValueError(
            "synthetic analysis failure reading /Users/fixture/private/holdings.sqlite3"
        )
        with patch("portfolio_research.application.analyze_review", side_effect=failure):
            started = self.service.start_workflow(
                {"operation_key": "operation-analysis-failure", "collect": False}
            )
            workflow = self.finish(started["workflow_id"])
        self.assertEqual(workflow["status"], "failed", workflow)
        self.assertIsNone(workflow["run_id"])
        self.assertEqual(workflow["stages"]["analyzing"]["status"], "failed")
        self.assertIn("synthetic analysis failure", workflow["error"])
        self.assertNotIn("/Users/", workflow["error"])
        self.assertEqual([row["run_id"] for row in self.service.store.list_runs()], [previous])
        self.assertEqual(self.service.run(previous)["result"]["run_id"], previous)


class PublishedReviewTests(WorkflowTestCase):
    """Once the archive write lands the run is published, whatever happens afterwards."""

    def test_a_failure_after_the_archive_write_still_reports_a_published_review(self):
        self.seed_collection()
        previous = self.saved_run()
        with patch.object(
            ResearchService,
            "_evaluate_prior",
            side_effect=AttributeError("'NoneType' object has no attribute 'columns'"),
        ):
            started = self.service.start_workflow(
                {"operation_key": "operation-publish-half-fail", "collect": False}
            )
            workflow = self.finish(started["workflow_id"])
        self.assertEqual(workflow["status"], "complete", workflow)
        self.assertTrue(workflow["run_id"])
        self.assertNotEqual(workflow["run_id"], previous)
        self.assertIsNone(workflow["error"])
        self.assertEqual(workflow["stages"]["publishing"]["status"], "complete")
        self.assertEqual(self.service.current()["latest_run"]["run_id"], workflow["run_id"])
        self.assertEqual(self.service.status()["latest_run_id"], workflow["run_id"])
        self.assertTrue(self.service.run(workflow["run_id"])["result"]["run_id"])

    def test_a_degraded_baseline_is_named_on_the_publishing_stage(self):
        self.seed_collection()
        with patch.object(
            ResearchService, "_record_baselines", side_effect=ValueError("baselines unusable")
        ):
            started = self.service.start_workflow(
                {"operation_key": "operation-prospective-status", "collect": False}
            )
            workflow = self.finish(started["workflow_id"])
        self.assertEqual(workflow["status"], "complete", workflow)
        publishing = workflow["stages"]["publishing"]
        self.assertEqual(publishing["status"], "complete")
        self.assertEqual(publishing["action_needed"], PROSPECTIVE_STATUS)
        self.assertEqual(publishing["detail"]["prospective_status"], PROSPECTIVE_STATUS)
        self.assertEqual(publishing["detail"]["run_id"], workflow["run_id"])
        self.assertTrue(workflow["run_id"])

    def test_an_undegraded_publication_names_no_outstanding_baseline_action(self):
        self.seed_collection()
        started = self.service.start_workflow(
            {"operation_key": "operation-clean-publication", "collect": False}
        )
        workflow = self.finish(started["workflow_id"])
        self.assertEqual(workflow["status"], "complete", workflow)
        publishing = workflow["stages"]["publishing"]
        self.assertIsNone(publishing["action_needed"])
        self.assertIsNone(publishing["detail"]["prospective_status"])


class OperationEscapeTests(WorkflowTestCase):
    """An operation that never reaches a worker must never block the next review."""

    def test_a_failed_hand_off_terminalizes_the_operation_it_created(self):
        self.seed_collection()
        with patch(
            "portfolio_research.workflow.ReviewWorkflow",
            side_effect=RuntimeError("can't start new thread"),
        ):
            with self.assertRaises(RuntimeError):
                self.service.start_workflow({"operation_key": "operation-orphan-probe-1"})
        self.assertIsNone(self.service.store.active_workflow())
        orphan = self.service.workflows()[0]
        self.assertEqual(orphan["status"], "failed")
        self.assertTrue(orphan["error"])
        follow = self.service.start_workflow(
            {"operation_key": "operation-orphan-probe-2", "collect": False}
        )
        self.assertNotEqual(follow["workflow_id"], orphan["workflow_id"])
        self.assertFalse(follow["reused"])
        self.assertEqual(self.finish(follow["workflow_id"])["status"], "complete")

    def test_cancelling_an_operation_with_no_worker_finishes_it_immediately(self):
        workflow_id, _ = self.service.store.create_workflow(
            "operation-orphan-cancel", "current", {"collect": False}
        )
        self.service.store.update_workflow(workflow_id, status="running", stage="collecting")
        cancelled = self.service.cancel_workflow(workflow_id)
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertTrue(cancelled["cancel_requested"])
        self.assertIsNone(cancelled["run_id"])
        self.assertIsNone(self.service.store.active_workflow())
        self.assertEqual(self.service.workflow(workflow_id)["status"], "cancelled")

    def test_cancelling_a_live_operation_still_only_requests_the_stop(self):
        self.seed_collection()
        self.start_chrome(delay=2.0)
        started = self.service.start_workflow({"operation_key": "operation-live-cancel-1"})
        cancelled = self.service.cancel_workflow(started["workflow_id"])
        self.assertTrue(cancelled["cancel_requested"])
        self.assertIn(cancelled["status"], {"queued", "running", "waiting", "cancelled"})
        self.assertEqual(self.finish(started["workflow_id"])["status"], "cancelled")


class ShutdownTests(WorkflowTestCase):
    """Ctrl-C stops the waiting quickly and publishes nothing after the owner asked to stop."""

    def test_shutdown_stops_a_waiting_collection_and_publishes_nothing(self):
        self.seed_collection()
        before = len(self.service.store.list_runs())
        self.start_chrome(delay=120)
        started = self.service.start_workflow({"operation_key": "operation-shutdown-1"})
        deadline = time.monotonic() + 30
        while (self.service.workflow(started["workflow_id"])["stages"].get("collecting") or {}).get(
            "status"
        ) != "running":
            if time.monotonic() > deadline:
                self.fail("The collection stage never started.")
            time.sleep(0.02)
        began = time.monotonic()
        self.service.close()
        self.assertLess(time.monotonic() - began, 15, "close() waited out the collection poll")
        workflow = self.service.workflow(started["workflow_id"])
        self.assertEqual(workflow["status"], "cancelled", workflow)
        self.assertIsNone(workflow["run_id"])
        self.assertEqual(len(self.service.store.list_runs()), before)

    def test_a_waiting_collection_never_holds_a_worker_the_jobs_need(self):
        """Previews and saves keep both job workers while an operation waits on Chrome."""
        self.seed_collection()
        self.start_chrome(delay=60)
        started = self.service.start_workflow({"operation_key": "operation-worker-isolation"})
        deadline = time.monotonic() + 30
        while (self.service.workflow(started["workflow_id"])["stages"].get("collecting") or {}).get(
            "status"
        ) != "running":
            if time.monotonic() > deadline:
                self.fail("The collection stage never started.")
            time.sleep(0.02)
        gate = threading.Barrier(3, timeout=20)
        for _ in range(2):
            self.service.executor.submit(gate.wait)
        gate.wait()  # A held job worker would break this barrier instead of releasing it.
        self.service.cancel_workflow(started["workflow_id"])

    def test_shutdown_never_runs_an_operation_that_was_still_queued(self):
        self.seed_collection()
        before = len(self.service.store.list_runs())
        workflow_id, _ = self.service.store.create_workflow(
            "operation-queued-at-shutdown", "current", {"collect": False}
        )
        self.service.close()
        self.assertEqual(self.service.workflow(workflow_id)["status"], "cancelled")
        self.assertEqual(len(self.service.store.list_runs()), before)
        self.assertIsNone(self.service.store.active_workflow())


class WorkflowRecordTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repository = ResearchRepository(Path(self.temp.name) / "research.sqlite3")

    def test_restart_fails_dead_operations_and_keeps_live_ones(self):
        live, _ = self.repository.create_workflow("operation-live-worker", "current", {})
        dead, _ = self.repository.create_workflow("operation-dead-worker", "current", {})
        self.repository.update_workflow(live, status="running")
        self.repository.update_workflow(dead, status="running")
        with self.repository.connect() as connection:
            connection.execute(
                "UPDATE review_workflows SET owner_pid=123456 WHERE workflow_id=?", (dead,)
            )

        def probe(pid, signal):
            if pid == 123456:
                raise ProcessLookupError

        with patch("portfolio_research.repository.os.kill", side_effect=probe):
            self.repository.recover_workflows()
        self.assertEqual(self.repository.workflow(live)["status"], "running")
        recovered = self.repository.workflow(dead)
        self.assertEqual(recovered["status"], "failed")
        self.assertIn("start a new review", recovered["error"])
        with self.assertRaises(ValueError):
            self.repository.update_workflow(dead, status="complete")

    def test_an_operation_key_identifies_one_operation_across_restarts(self):
        first, created = self.repository.create_workflow(
            "operation-stable-key", "current", {"collect": True}
        )
        second, repeated = self.repository.create_workflow(
            "operation-stable-key", "current", {"collect": True}
        )
        self.assertTrue(created)
        self.assertFalse(repeated)
        self.assertEqual(first, second)
        self.assertEqual(self.repository.active_workflow()["workflow_id"], first)
        with self.assertRaises(ValueError):
            self.repository.create_workflow("short", "current", {})
        self.repository.update_workflow(first, status="complete")
        self.assertIsNone(self.repository.active_workflow())


class CancellationLatchTests(unittest.TestCase):
    """A cancellation once seen stays seen; a store that cannot answer says nothing."""

    def probe(self, *, answers):
        from portfolio_research.workflow import PROBE_SECONDS, ReviewWorkflow

        run = ReviewWorkflow.__new__(ReviewWorkflow)
        run.probed = 0.0
        run.stop_seen = False
        run.workflow_id = "w1"
        run._stopping = lambda: False
        calls = iter(answers)

        class Store:
            def workflow(self, workflow_id):
                answer = next(calls)
                if isinstance(answer, Exception):
                    raise answer
                return {"cancel_requested": answer}

        run.store = Store()
        clock = iter([PROBE_SECONDS * step for step in range(1, len(answers) + 2)])
        with patch("portfolio_research.workflow.monotonic", side_effect=lambda: next(clock)):
            return [run._fetch_stopping() for _ in answers]

    def test_a_failing_probe_after_a_cancellation_never_resumes_the_fetch(self):
        self.assertEqual(self.probe(answers=[True, KeyError("gone")]), [True, True])

    def test_a_failing_probe_before_any_cancellation_is_not_a_cancellation(self):
        self.assertEqual(self.probe(answers=[KeyError("gone"), False]), [False, False])

    def test_a_cancellation_is_still_reported_when_the_store_answers(self):
        self.assertEqual(self.probe(answers=[False, True]), [False, True])


class PreviousReviewExtractTests(unittest.TestCase):
    """The prior run is read for the one part prioritisation compares against."""

    def store(self, result):
        from portfolio_research.repository import ResearchRepository

        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        store = ResearchRepository(str(Path(temp.name) / "research.sqlite"))
        store.save_run(result, {"data": {}}, {"as_of": AS_OF})
        return store

    def result(self):
        return {
            "as_of": AS_OF,
            "research": {"OWN": {"brief": {"facts": []}, "proposals": None, "coverage": {}}},
            "signals": [{"security_id": "OWN"}],
            "holdings": [],
        }

    def test_only_the_research_the_comparison_reads_is_loaded(self):
        from portfolio_research.workflow import _previous_review

        previous = _previous_review(self.store(self.result()))
        self.assertEqual(sorted(previous), ["research"])
        self.assertIn("OWN", previous["research"])

    def test_no_archive_is_simply_no_comparison(self):
        from portfolio_research.repository import ResearchRepository
        from portfolio_research.workflow import _previous_review

        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        empty = ResearchRepository(str(Path(temp.name) / "research.sqlite"))
        self.assertIsNone(_previous_review(empty))
        self.assertIsNone(_previous_review(None))


if __name__ == "__main__":
    unittest.main()
