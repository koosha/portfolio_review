import copy
import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from portfolio.storage import Store
from portfolio_lab.ingestion import ResearchStore
from portfolio_research.calendar import decision_context
from portfolio_research.operations import restore_backup
from portfolio_research.public import public_result, public_sources, public_workspace
from portfolio_research.repository import ResearchRepository
from portfolio_research.service import ResearchService, default_config


class PublicBoundaryTests(unittest.TestCase):
    def test_nested_private_paths_spaces_keys_sql_and_mixed_case_secrets_removed(self):
        private_path = "/Users/synthetic/My Drive (fixture@example.invalid)/code/secret.sqlite3"
        result = {
            "metadata": {
                "note": "Read " + private_path,
                private_path: "private-key-value",
                "API_KEY": "fixture-api-secret",
                "Authorization": "Bearer fixture-bearer",
                "sql": "SELECT private_table FROM private_source",
                "saved_config": {"source": {"path": private_path}},
            },
            "company_research": {
                "DEMO": {
                    "nested": {
                        "PASSWORD": "fixture-password",
                        "raw_path": private_path,
                        "legitimate": "Keep this valuation label",
                    }
                }
            },
        }
        output = json.dumps(public_result(result))
        for private in (
            "fixture@example.invalid",
            "secret.sqlite3",
            "fixture-api-secret",
            "private-key-value",
            "private_table",
            "fixture-password",
            "fixture-bearer",
        ):
            with self.subTest(private=private):
                self.assertNotIn(private, output)
        self.assertIn("Keep this valuation label", output)
        workspace = {
            "assessments": {"DEMO": {"API_KEY": "fixture-api-secret", "note": private_path}},
            "valuations": {},
        }
        self.assertNotIn("fixture-api-secret", json.dumps(public_workspace(workspace)))
        self.assertNotIn("fixture@example.invalid", json.dumps(public_workspace(workspace)))

    def test_approved_source_domains_still_reject_userinfo_credentials(self):
        for locator in (
            "https://user:fixture-password@www.sec.gov/Archives/report",
            "https://:fixture-password@www.sec.gov/Archives/report",
            "https://www.sec.gov/Archives/report?api_key=fixture-api-secret",
        ):
            with self.subTest(locator=locator):
                output = public_sources([{"provider": "SEC", "url": locator}])
                self.assertNotIn("locator", output[0])
                self.assertNotIn("fixture-password", json.dumps(output))
        accepted = public_sources(
            [{"provider": "SEC", "url": "https://www.sec.gov/Archives/report"}]
        )
        self.assertEqual(accepted[0]["locator"], "https://www.sec.gov/Archives/report")


class StoreBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def test_future_archive_version_rejected_before_any_schema_writes(self):
        path = self.directory / "future.sqlite3"
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE research_store_metadata(schema_version INTEGER NOT NULL)")
            db.execute("INSERT INTO research_store_metadata VALUES(99)")
        before = path.read_bytes()
        with self.assertRaises(ValueError):
            ResearchStore(path)
        self.assertEqual(before, path.read_bytes())

    def test_future_application_version_rejected_before_base_or_app_migration(self):
        path = self.directory / "future-app.sqlite3"
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE research_store_metadata(schema_version INTEGER NOT NULL)")
            db.execute("INSERT INTO research_store_metadata VALUES(1)")
            db.execute("CREATE TABLE review_metadata(schema_version INTEGER NOT NULL)")
            db.execute("INSERT INTO review_metadata VALUES(99)")
        before = path.read_bytes()
        with self.assertRaises(ValueError):
            ResearchRepository(path)
        self.assertEqual(before, path.read_bytes())

    def test_source_alias_rejected_before_writable_repository_initialization(self):
        source = self.directory / "portfolio.sqlite3"
        with sqlite3.connect(source) as db:
            db.execute("CREATE TABLE original_holdings(id INTEGER PRIMARY KEY)")
        before = source.read_bytes()
        alias = self.directory / "research.sqlite3"
        config = default_config(self.directory)
        for link_type in ("symbolic", "hard"):
            if alias.exists():
                alias.unlink()
            if link_type == "symbolic":
                alias.symlink_to(source)
            else:
                alias.hardlink_to(source)
            with self.subTest(link_type=link_type), self.assertRaises(ValueError):
                ResearchService(copy.deepcopy(config))
            self.assertEqual(before, source.read_bytes())

    def test_durable_duplicate_request_and_immutable_review_record(self):
        store = ResearchRepository(self.directory / "research.sqlite3")
        first, created = store.create_job(
            "preview", "synthetic-request", {"base_run_id": "synthetic"}
        )
        second, repeated = store.create_job(
            "preview", "synthetic-request", {"base_run_id": "synthetic"}
        )
        self.assertTrue(created)
        self.assertFalse(repeated)
        self.assertEqual(first, second)
        with self.assertRaises(ValueError):
            store.create_job("preview", "synthetic-request", {"base_run_id": "different"})
        record_id = store.append_record("configuration", {"mandate": {"confirmed": False}})
        for sql in (
            "UPDATE review_records SET payload_json='{}' WHERE record_id=?",
            "DELETE FROM review_records WHERE record_id=?",
        ):
            with store.connect() as connection, self.assertRaises(sqlite3.IntegrityError):
                connection.execute(sql, (record_id,))
        self.assertFalse(store.latest("configuration")["mandate"]["confirmed"])

    def test_service_evaluation_uses_effective_saved_overrides_not_raw_provider_vintage(self):
        source = Store(self.directory)
        before = source.path.read_bytes()
        service = ResearchService(default_config(self.directory))
        self.addCleanup(service.close)
        forecasts = [
            {
                "security_id": "DEMO",
                "forecast_date": "2026-09-03T19:00:00Z",
                "horizon_months": 12,
                "scenario": label,
                "return_value": 0.1,
                "probability": probability,
                "basis": "subjective",
            }
            for label, probability in [("adverse", 0.25), ("central", 0.5), ("favorable", 0.25)]
        ]
        effective = [
            {**row, "return_value": 0.8, "joint_validation_status": "valid"} for row in forecasts
        ]
        bundle = {
            "as_of": "2026-09-03",
            "mode": "offline",
            "forecasts": pd.DataFrame(forecasts),
            "timeline": decision_context("2026-09-03", generated_at="2026-09-03T21:00:00Z"),
        }
        run_id = service.store.save_run(
            {"forecast_inputs": effective, "metadata": {}}, service.config, bundle
        )
        prices = "security_id,date,adjusted_close,available_at,received_at\nDEMO,2026-09-04,100,2026-09-04T20:01:00Z,2026-09-04T20:02:00Z\nDEMO,2027-09-07,120,2027-09-07T20:01:00Z,2027-09-07T20:02:00Z\n"
        queued = service.submit(
            {
                "kind": "evaluate",
                "request_key": "synthetic-effective-override",
                "run_id": run_id,
                "prices_csv": prices,
                "evaluation_date": "2027-09-08",
            }
        )
        finished = service.wait(queued["job_id"])
        self.assertEqual(finished["status"], "complete")
        row = finished["output"]["evaluation"]["results"][0]
        self.assertEqual(row["status"], "scored")
        self.assertAlmostEqual(row["predicted_return"], 0.8)
        self.assertEqual(before, source.path.read_bytes())

    def test_restore_rejects_parent_traversal_before_creating_destination(self):
        backup = self.directory / "snapshot"
        backup.mkdir()
        payload = backup / "payload"
        payload.write_text("synthetic backup payload")
        (backup / "manifest.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "source_filename": "portfolio.sqlite3",
                    "files": {
                        "../snapshot/payload": hashlib.sha256(payload.read_bytes()).hexdigest()
                    },
                }
            )
        )
        destination = self.directory / "another-parent" / "restored"
        with self.assertRaises(ValueError):
            restore_backup(backup, destination)
        self.assertFalse(destination.exists())
        self.assertFalse((destination.parent / "snapshot" / "payload").exists())

    def test_restore_source_filename_cannot_overwrite_research_archive(self):
        backup = self.directory / "snapshot"
        backup.mkdir()
        files = {}
        for name in ("holdings.sqlite3", "research.sqlite3"):
            path = backup / name
            with sqlite3.connect(path) as db:
                db.execute("CREATE TABLE synthetic_records(id INTEGER PRIMARY KEY)")
            files[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        (backup / "manifest.json").write_text(
            json.dumps({"version": 1, "source_filename": "research.sqlite3", "files": files})
        )
        destination = self.directory / "restored"
        with self.assertRaises(ValueError):
            restore_backup(backup, destination)
        self.assertFalse(destination.exists())
