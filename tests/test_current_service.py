import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from portfolio.storage import Store
from portfolio_lab.config import dashboard_patch, load_config
from portfolio_lab.demo import create_demo
from portfolio_research.service import ResearchService, default_config

SUNDAY_RECEIPT = "2026-09-13T20:00:00+00:00"
FRIDAY = "2026-09-11"
FRIDAY_CUTOFF = "2026-09-11T20:00:00+00:00"
SUNDAY_GENERATED = datetime(2026, 9, 13, 21, 0, tzinfo=UTC)


class SundayClock(datetime):
    """Pins the review calendar's wall clock to Sunday evening after the Sunday capture."""

    @classmethod
    def now(cls, tz=None):
        return SUNDAY_GENERATED if tz is None else SUNDAY_GENERATED.astimezone(tz)


def table(symbol="DEMO", value="10", currency="USD"):
    headers = ["Symbol", "Shares", "Last Price", "Market Value ($)"]
    rows = [[symbol, "1", value, value], ["Total Cash", "", "2.03", ""]]
    if currency:
        headers = [*headers, "Currency"]
        rows = [[*row, currency] for row in rows]
    return {
        "method": "yahoo-holdings-table-v1",
        "headers": headers,
        "rows": rows,
        "page_count": 1,
        "expected_count": len(rows),
        "completeness": "count-verified",
    }


class CurrentServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.a = self.store.add_source(
            "Labelled account", url="https://finance.yahoo.com/portfolio/p_fixture_service_a"
        )
        self.b = self.store.add_source(
            "Unlabelled account", url="https://finance.yahoo.com/portfolio/p_fixture_service_b"
        )
        with patch("portfolio.storage.now", return_value=SUNDAY_RECEIPT):
            self.batch = self.store.begin_batch([self.a, self.b])
            self.store.ingest_table(self.a, table("DEMO"), batch_id=self.batch)
            self.store.ingest_table(self.b, table("OTHER", currency=None), batch_id=self.batch)
        self.config = default_config(self.temp.name)
        self.assertEqual(Path(self.config["source"]["path"]), self.store.path.resolve())
        self.service = ResearchService(self.config)
        self.addCleanup(self.service.close)

    def monthly(self, request_key, **fields):
        job = self.service.submit({"kind": "monthly", "request_key": request_key, **fields})
        finished = self.service.wait(job["job_id"])
        self.assertIn(finished["status"], {"complete", "failed"}, finished)
        return finished

    def test_current_holdings_do_not_need_a_saved_review(self):
        response = self.service.current()
        self.assertTrue(response["supported"])
        self.assertIsNone(response["latest_run"])
        self.assertIn("dated separately", response["note"])
        current = response["current"]
        self.assertEqual(current["review_kind"], "current")
        self.assertEqual(current["collection"]["status"], "published")
        self.assertEqual(current["collection"]["batch_id"], self.batch)
        self.assertEqual(current["dates"]["collection_received_at"], SUNDAY_RECEIPT)
        self.assertIsNone(current["dates"]["source_valuation_time"])
        self.assertEqual(len(current["accounts"]), 2)
        self.assertEqual(len(current["positions"]), 2)
        self.assertFalse(current["totals"]["reconciled_nav"])
        serialized = json.dumps(response, allow_nan=False)
        self.assertNotIn("finance.yahoo.com", serialized)
        self.assertNotIn(str(self.store.path), serialized)
        self.assertNotIn(self.temp.name, serialized)

    def test_status_reports_latest_collection_and_review_kinds(self):
        status = self.service.status()
        self.assertEqual(status["schema_version"], 1)
        self.assertEqual(status["latest_collection"]["status"], "published")
        self.assertEqual(status["latest_collection"]["collection_received_at"], SUNDAY_RECEIPT)
        self.assertEqual(status["latest_collection"]["account_count"], 2)
        self.assertEqual(status["review_kinds"], ["current", "historical"])
        self.assertEqual(status["default_review_kind"], "current")
        for key in (
            "product",
            "mode",
            "dataset",
            "config",
            "runs",
            "jobs",
            "latest_run_id",
            "default_as_of",
            "providers",
            "collector_busy",
        ):
            self.assertIn(key, status)
        self.assertEqual(status["dataset"], "Yahoo holdings")
        self.assertIsNone(status["latest_run_id"])

    def test_status_survives_an_unreadable_collection(self):
        failure = ValueError("synthetic collector problem")
        with (
            patch("portfolio_research.current.current_snapshot", side_effect=failure),
            patch("portfolio_research.service.current_snapshot", side_effect=failure, create=True),
        ):
            status = self.service.status()
        self.assertEqual(status["latest_collection"]["status"], "unavailable")
        self.assertIn("synthetic collector problem", status["latest_collection"]["error"])
        self.assertEqual(status["schema_version"], 1)

    @patch("portfolio_research.calendar.datetime", SundayClock)
    def test_saved_historical_review_is_dated_separately_and_unchanged(self):
        finished = self.monthly(
            "current-service-historical-review", as_of=FRIDAY, review_kind="historical"
        )
        runs = self.service.store.list_runs()
        if finished["status"] != "complete":
            self.assertEqual(runs, [])
            self.assertIsNone(self.service.current()["latest_run"])
            return
        run_id = finished["output"]["result"]["run_id"]
        self.assertEqual(finished["output"]["result"]["metadata"]["review_kind"], "historical")
        before = self.service.run(run_id)
        response = self.service.current()
        self.assertTrue(response["supported"])
        latest = response["latest_run"]
        self.assertEqual(latest["run_id"], run_id)
        self.assertEqual(latest["as_of"], FRIDAY)
        self.assertEqual(latest["created_at"], runs[0]["created_at"])
        self.assertEqual(latest["review_kind"], "historical")
        self.assertIsNone(latest["valuation_date"])
        self.assertIsNone(latest["collection_received_at"])
        self.assertEqual(latest["information_cutoff"], FRIDAY_CUTOFF)
        self.assertEqual(response["current"]["dates"]["collection_received_at"], SUNDAY_RECEIPT)
        self.assertEqual(response["current"]["dates"]["market_observation_date"], FRIDAY)
        self.assertEqual(self.service.run(run_id), before)
        self.assertEqual(self.service.status()["latest_run_id"], run_id)

    def test_default_review_kind_for_submitted_jobs_stays_historical(self):
        finished = self.monthly("current-service-default-kind", as_of=FRIDAY)
        if finished["status"] != "complete":
            self.assertIsNone(self.service.current()["latest_run"])
            return
        metadata = finished["output"]["result"]["metadata"]
        self.assertEqual(metadata["review_kind"], "historical")
        self.assertEqual(metadata["as_of"], FRIDAY)
        self.assertEqual(metadata["information_cutoff"], FRIDAY_CUTOFF)
        self.assertEqual(self.service.current()["latest_run"]["review_kind"], "historical")

    def test_current_review_job_uses_the_sunday_collection(self):
        finished = self.monthly("current-service-current-review", review_kind="current")
        if finished["status"] != "complete":
            self.assertIsNone(self.service.current()["latest_run"])
            return
        result = finished["output"]["result"]
        metadata = result["metadata"]
        self.assertEqual(metadata["review_kind"], "current")
        self.assertEqual(metadata["market_observation_date"], metadata["as_of"])
        self.assertEqual(metadata["information_cutoff"], result["timeline"]["generated_at"])
        self.assertEqual(metadata["collection_received_at"], SUNDAY_RECEIPT)
        self.assertEqual(result["timeline"]["review_kind"], "current")
        latest = self.service.current()["latest_run"]
        self.assertEqual(latest["run_id"], result["run_id"])
        self.assertEqual(latest["review_kind"], "current")
        self.assertEqual(latest["collection_received_at"], SUNDAY_RECEIPT)

    def test_current_review_requests_reject_explicit_dates_and_unknown_kinds(self):
        with self.assertRaises(ValueError):
            self.service.submit(
                {
                    "kind": "monthly",
                    "review_kind": "current",
                    "as_of": FRIDAY,
                    "request_key": "current-service-current-with-date",
                }
            )
        with self.assertRaises(ValueError):
            self.service.submit(
                {
                    "kind": "monthly",
                    "review_kind": "weekly",
                    "request_key": "current-service-weekly-kind",
                }
            )
        with self.assertRaises(ValueError):
            self.service.submit(
                {
                    "kind": "monthly",
                    "review_kind": None,
                    "request_key": "current-service-null-kind",
                }
            )
        self.assertEqual(self.service.status()["jobs"], [])
        self.assertTrue(self.service.current()["supported"])

    def test_monthly_request_key_created_before_review_kinds_is_returned_on_retry(self):
        """A stable scheduler key (e.g. monthly-YYYY-MM-DD) must survive the upgrade."""
        key = "monthly-2026-09-11"
        legacy_request = {
            "as_of": FRIDAY,
            "resolved_config": dashboard_patch(self.service.resolved_config(), {}),
            "supplemental": self.service.store.latest("supplemental"),
        }
        job_id, created = self.service.store.create_job("monthly", key, legacy_request)
        self.assertTrue(created)
        retried = self.service.submit({"kind": "monthly", "as_of": FRIDAY, "request_key": key})
        self.assertEqual(retried["job_id"], job_id)
        explicit = self.service.submit(
            {"kind": "monthly", "as_of": FRIDAY, "review_kind": "historical", "request_key": key}
        )
        self.assertEqual(explicit["job_id"], job_id)
        self.assertNotIn("review_kind", self.service.store.job(job_id)["payload"])
        with self.assertRaisesRegex(ValueError, "different inputs"):
            self.service.submit({"kind": "monthly", "review_kind": "current", "request_key": key})

    def test_current_review_kind_is_stored_with_the_request(self):
        with patch.object(self.service.executor, "submit"):
            job = self.service.submit(
                {"kind": "monthly", "review_kind": "current", "request_key": "current-kind-stored"}
            )
        self.assertEqual(self.service.store.job(job["job_id"])["payload"]["review_kind"], "current")


class DemoSourceCurrentTests(unittest.TestCase):
    def test_configured_non_collector_source_is_not_supported(self):
        with tempfile.TemporaryDirectory() as directory:
            config = load_config(create_demo(Path(directory) / "demo"))
            config["risk"]["bootstrap_samples"] = 50
            service = ResearchService(config)
            try:
                response = service.current()
                self.assertFalse(response["supported"])
                self.assertIn("saved reviews", response["reason"])
                self.assertNotIn("current", response)
                status = service.status()
                self.assertEqual(status["schema_version"], 1)
                self.assertEqual(status["review_kinds"], ["current", "historical"])
                self.assertEqual(status["default_review_kind"], "current")
                self.assertNotIn(str(Path(config["source"]["path"])), json.dumps(status))
            finally:
                service.close()
