"""Opening an archive written before the monthly workflow, and copying a whole project.

Two promises are pinned here. A research database written by the code that shipped before
the monthly workflow still opens: the new operation tables appear, the runs and records it
already held stay readable through the service, and the recorded schema version is not
bumped behind the owner's back. And a project can be copied whole — databases, the new
tables, the provider cache beside them — into a directory that does not yet exist.

The third migration promise, that a capture from the older extension (a 1.1.6 header whose
listing column is dropped) is still accepted, is pinned by
``tests.test_holdings.HoldingsTests.test_v2_capture_from_older_extension_drops_quote_symbols_with_warning``
and is deliberately cross-referenced rather than repeated.
"""

import io
import json
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

from portfolio_lab.config import load_config
from portfolio_lab.demo import create_demo
from portfolio_research.operations import backup_project, restore_backup
from portfolio_research.repository import ResearchRepository
from portfolio_research.service import ResearchService

PROJECT = Path(__file__).resolve().parent.parent

# Copied verbatim from the schema this project shipped before the monthly workflow:
# `git show e76440b:portfolio_lab/ingestion.py` (ResearchStore) followed by
# `git show e76440b:portfolio_research/repository.py` (ResearchRepository). It is a
# fixture of the past, so it must not be edited when the live schema grows — that
# divergence is exactly what these tests are for.
BASELINE_SCHEMA = """
    CREATE TABLE IF NOT EXISTS research_store_metadata (schema_version INTEGER NOT NULL);
    INSERT INTO research_store_metadata SELECT 1
        WHERE NOT EXISTS (SELECT 1 FROM research_store_metadata);
    CREATE TABLE IF NOT EXISTS research_runs (
        run_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, as_of TEXT,
        config_hash TEXT NOT NULL, bundle_hash TEXT NOT NULL,
        config_json TEXT NOT NULL, result_json TEXT NOT NULL,
        bundle_json TEXT NOT NULL, manifest_json TEXT NOT NULL
    );
    CREATE TRIGGER IF NOT EXISTS immutable_runs_update BEFORE UPDATE ON research_runs
    BEGIN SELECT RAISE(ABORT, 'research runs are immutable'); END;
    CREATE TRIGGER IF NOT EXISTS immutable_runs_delete BEFORE DELETE ON research_runs
    BEGIN SELECT RAISE(ABORT, 'research runs are immutable'); END;
    CREATE TRIGGER IF NOT EXISTS immutable_runs_replace BEFORE INSERT ON research_runs
    WHEN EXISTS (SELECT 1 FROM research_runs WHERE run_id=NEW.run_id)
    BEGIN SELECT RAISE(ABORT, 'research runs are immutable'); END;
    CREATE TABLE IF NOT EXISTS research_payloads (
        source_id TEXT PRIMARY KEY, provider TEXT NOT NULL, cache_key TEXT NOT NULL,
        received_at TEXT NOT NULL, sha256 TEXT NOT NULL, payload_json TEXT NOT NULL
    );
    CREATE TRIGGER IF NOT EXISTS immutable_payloads_update BEFORE UPDATE ON research_payloads
    BEGIN SELECT RAISE(ABORT, 'research payloads are immutable'); END;
    CREATE TRIGGER IF NOT EXISTS immutable_payloads_delete BEFORE DELETE ON research_payloads
    BEGIN SELECT RAISE(ABORT, 'research payloads are immutable'); END;
    CREATE TRIGGER IF NOT EXISTS immutable_payloads_replace BEFORE INSERT ON research_payloads
    WHEN EXISTS (SELECT 1 FROM research_payloads WHERE source_id=NEW.source_id)
    BEGIN SELECT RAISE(ABORT, 'research payloads are immutable'); END;
    CREATE TABLE IF NOT EXISTS review_metadata (schema_version INTEGER NOT NULL);
    INSERT INTO review_metadata SELECT 1 WHERE NOT EXISTS (SELECT 1 FROM review_metadata);
    CREATE TABLE IF NOT EXISTS review_records (
        record_id TEXT PRIMARY KEY, kind TEXT NOT NULL, run_id TEXT,
        parent_id TEXT, created_at TEXT NOT NULL, payload_json TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS review_record_kind ON review_records(kind, created_at);
    CREATE TRIGGER IF NOT EXISTS immutable_review_update BEFORE UPDATE ON review_records
    BEGIN SELECT RAISE(ABORT, 'review records are immutable'); END;
    CREATE TRIGGER IF NOT EXISTS immutable_review_delete BEFORE DELETE ON review_records
    BEGIN SELECT RAISE(ABORT, 'review records are immutable'); END;
    CREATE TRIGGER IF NOT EXISTS immutable_review_replace BEFORE INSERT ON review_records
    WHEN EXISTS (SELECT 1 FROM review_records WHERE record_id=NEW.record_id)
    BEGIN SELECT RAISE(ABORT, 'review records are immutable'); END;
    CREATE TABLE IF NOT EXISTS review_jobs (
        job_id TEXT PRIMARY KEY, request_key TEXT UNIQUE NOT NULL,
        payload_hash TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        payload_json TEXT NOT NULL, output_json TEXT, error TEXT
    );
"""

BASELINE_RUN_ID = "0f1e2d3c4b5a69788796a5b4c3d2e1f0"
BASELINE_CREATED_AT = "2026-06-30T21:15:04.000000+00:00"
BASELINE_AS_OF = "2026-06-30"
BASELINE_DECISION = {"note": "synthetic baseline decision", "chosen": "hold"}


def _dumps(value):
    """The archive's canonical JSON, as the baseline code wrote it."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def _baseline_result(run_id):
    """A saved result shaped like the baseline pipeline's, with no workflow fields."""
    return {
        "run_id": run_id,
        "summary": {"complete": True, "as_of": BASELINE_AS_OF},
        "holdings": [
            {"security_id": "SIM01", "symbol": "SIM01", "weight": 0.6, "market_value": 60000.0},
            {"security_id": "SIM02", "symbol": "SIM02", "weight": 0.4, "market_value": 40000.0},
        ],
        "issues": [],
        "sources": [{"name": "synthetic prices", "url": "https://example.invalid/prices"}],
        "metadata": {
            "run_id": run_id,
            "created_at": BASELINE_CREATED_AT,
            "as_of": BASELINE_AS_OF,
            "config_hash": "0" * 64,
            "bundle_hash": "1" * 64,
        },
    }


def write_baseline_database(path, config):
    """Create a research database with the pre-workflow schema and one saved run in it."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    result = _baseline_result(BASELINE_RUN_ID)
    bundle = {"as_of": BASELINE_AS_OF, "workspace": {"assessments": {}, "valuations": {}}}
    manifest = {"sources": [], "frames": {}, "bundle_hash": "1" * 64, "config_hash": "0" * 64}
    with sqlite3.connect(path) as connection:
        connection.executescript(BASELINE_SCHEMA)
        connection.execute(
            "INSERT INTO research_runs VALUES (?,?,?,?,?,?,?,?,?)",
            (
                BASELINE_RUN_ID,
                BASELINE_CREATED_AT,
                BASELINE_AS_OF,
                "0" * 64,
                "1" * 64,
                _dumps(config),
                _dumps(result),
                _dumps(bundle),
                _dumps(manifest),
            ),
        )
        connection.execute(
            "INSERT INTO review_records VALUES (?,?,?,?,?,?)",
            (
                "baselinerecord0000000000000000aa",
                "decision",
                BASELINE_RUN_ID,
                None,
                BASELINE_CREATED_AT,
                _dumps(BASELINE_DECISION),
            ),
        )
        connection.execute(
            "INSERT INTO review_jobs "
            "(job_id,request_key,payload_hash,kind,status,created_at,updated_at,payload_json,"
            "output_json,error) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                "baselinejob00000000000000000000a",
                "baseline-monthly-request",
                "2" * 64,
                "monthly",
                "complete",
                BASELINE_CREATED_AT,
                BASELINE_CREATED_AT,
                _dumps({"kind": "monthly", "as_of": BASELINE_AS_OF}),
                _dumps({"run_id": BASELINE_RUN_ID}),
                None,
            ),
        )
    return path


def table_names(path):
    with sqlite3.connect(path) as connection:
        return {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }


def column_names(path, table):
    with sqlite3.connect(path) as connection:
        return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}


def schema_versions(path, table):
    with sqlite3.connect(path) as connection:
        return [row[0] for row in connection.execute(f"SELECT schema_version FROM {table}")]


class BaselineDatabaseTests(unittest.TestCase):
    """A database written before the monthly workflow opens under today's code."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.config = load_config(create_demo(root / "demo"))
        self.research_path = root / "baseline" / "research.sqlite3"
        write_baseline_database(self.research_path, self.config)
        self.config["research"] = {
            "path": str(self.research_path),
            "output_dir": str(root / "baseline" / "reports"),
        }

    def open_service(self, **kwargs):
        service = ResearchService(self.config, **kwargs)
        self.addCleanup(service.close)
        return service

    def test_baseline_database_starts_without_the_workflow_table(self):
        tables = table_names(self.research_path)
        self.assertIn("research_runs", tables)
        self.assertIn("review_records", tables)
        self.assertNotIn("review_workflows", tables)
        self.assertNotIn("owner_pid", column_names(self.research_path, "review_jobs"))

    def test_opening_adds_workflow_tables_without_touching_the_schema_version(self):
        service = self.open_service()
        self.assertIn("review_workflows", table_names(self.research_path))
        self.assertIn("owner_pid", column_names(self.research_path, "review_jobs"))
        self.assertEqual(schema_versions(self.research_path, "review_metadata"), [1])
        self.assertEqual(schema_versions(self.research_path, "research_store_metadata"), [1])
        self.assertIsNone(service.status()["active_workflow"])

    def test_the_saved_run_still_loads_through_the_service(self):
        service = self.open_service()
        status = service.status()
        self.assertEqual(status["latest_run_id"], BASELINE_RUN_ID)
        self.assertEqual([row["run_id"] for row in status["runs"]], [BASELINE_RUN_ID])
        loaded = service.run(BASELINE_RUN_ID)
        self.assertEqual(loaded["result"]["run_id"], BASELINE_RUN_ID)
        self.assertEqual(loaded["result"]["metadata"]["as_of"], BASELINE_AS_OF)
        self.assertEqual(len(loaded["result"]["holdings"]), 2)
        self.assertIsNone(loaded["previous_run_id"])

    def test_records_and_jobs_written_before_the_workflow_are_still_readable(self):
        service = self.open_service()
        self.assertEqual(service.store.latest("decision"), BASELINE_DECISION)
        self.assertEqual(
            [job["job_id"] for job in service.store.jobs()], ["baselinejob00000000000000000000a"]
        )

    def test_recovery_on_a_baseline_database_finds_nothing_to_recover(self):
        service = self.open_service(recover=True)
        self.assertIsNone(service.status()["active_workflow"])
        self.assertIsNone(service.status()["last_workflow"])
        self.assertEqual(
            service.store.job("baselinejob00000000000000000000a")["status"], "complete"
        )

    def test_a_backup_taken_before_the_upgrade_restores_and_then_migrates(self):
        root = Path(self.temp.name)
        target = backup_project(self.config, root / "backup", root / "demo")
        restored = restore_backup(target, root / "restored")
        research_path = Path(load_config(restored / "research-config.json")["research"]["path"])
        self.assertNotIn("review_workflows", table_names(research_path))
        store = ResearchRepository(research_path)
        self.assertIn("review_workflows", table_names(research_path))
        self.assertEqual(schema_versions(research_path, "review_metadata"), [1])
        self.assertEqual(store.load_run(BASELINE_RUN_ID)["run_id"], BASELINE_RUN_ID)
        self.assertEqual(store.latest("decision"), BASELINE_DECISION)

    def test_a_new_workflow_can_be_recorded_in_the_migrated_database(self):
        service = self.open_service()
        workflow_id, created = service.store.create_workflow(
            "migrated-monthly-operation", "current", {"as_of": "2026-07-31"}
        )
        self.assertTrue(created)
        self.assertEqual(service.store.workflow(workflow_id)["status"], "queued")
        self.assertEqual(service.status()["active_workflow"]["workflow_id"], workflow_id)


class DocumentedRecoveryTests(unittest.TestCase):
    """The backup the README documents, taken against a data directory the app made.

    The documented setup is ``uv run app.py`` and nothing else, and every recovery
    subparser defaults ``--config`` to ``data/research-config.json``. A server that only
    ever reads that file leaves the documented backup — which the README also calls the
    only copy of the holdings and the pairing key — impossible to take, and the owner
    discovers that at the moment they need it.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / "data"

    def start_application(self):
        """Run the real entry point against a fresh data directory, then stop it."""
        source = "import sys\nfrom portfolio.server import main\nmain()\n"
        with socket.socket() as probe:  # The app refuses port 0, so one is claimed here.
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        process = subprocess.Popen(
            [sys.executable, "-u", "-c", source, "--data-dir", str(self.data), "--port", str(port)],
            cwd=str(PROJECT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(process.stderr.close)
        self.addCleanup(process.stdout.close)
        self.addCleanup(process.wait)
        self.addCleanup(process.terminate)
        # research.sqlite3 appears once the research service is constructed, which is the
        # step after the configuration is resolved: past it, the directory is everything a
        # first run leaves behind.
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if process.poll() is not None:
                self.fail(f"The application did not start: {process.stderr.read()}")
            if (self.data / "research.sqlite3").exists():
                return
            time.sleep(0.05)
        self.fail("The application never opened its research database.")

    def left_behind(self):
        return sorted(item.name for item in self.data.iterdir())

    def test_a_data_directory_the_application_made_holds_the_configuration_recovery_reads(self):
        self.start_application()
        self.assertIn("research-config.json", self.left_behind())
        config = load_config(self.data / "research-config.json")
        self.assertEqual(Path(config["research"]["path"]).parent, self.data.resolve())

    def run_cli(self, arguments):
        """Run one documented command, keeping its report out of the suite's output.

        ``cli.main`` reports to stdout and stderr the way an operator reads it, and the
        refusal below is a success here; printed into a test run it reads as a failure.
        """
        from portfolio_research import cli

        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return cli.main(arguments)

    def test_the_documented_backup_and_restore_run_with_no_prior_init(self):
        self.start_application()
        backup = self.root / "backups" / "2026-09"
        self.assertEqual(
            self.run_cli(
                [
                    "backup",
                    "--config",
                    str(self.data / "research-config.json"),
                    "--out",
                    str(backup),
                ]
            ),
            0,
        )
        self.assertTrue((backup / "manifest.json").is_file())
        restored = self.root / "restored"
        self.assertEqual(
            self.run_cli(["restore", "--from", str(backup), "--into", str(restored)]), 0
        )
        self.assertTrue((restored / "research-config.json").is_file())
        # And the second restore refuses, exactly as the README says neither command
        # overwrites anything.
        self.assertNotEqual(
            self.run_cli(["restore", "--from", str(backup), "--into", str(restored)]), 0
        )


class ProjectBackupTests(unittest.TestCase):
    """A whole project round-trips, new tables and provider cache included."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = load_config(create_demo(self.root / "demo"))
        self.store = ResearchRepository(self.config["research"]["path"])
        self.workflow_id, _ = self.store.create_workflow(
            "monthly-operation-backup", "current", {"as_of": BASELINE_AS_OF}
        )
        self.store.update_workflow(self.workflow_id, status="running", stage="collecting")
        self.store.append_record("decision", BASELINE_DECISION)
        self.run_id = self.store.save_run(
            _baseline_result("a1b2c3d4e5f60718293a4b5c6d7e8f90"),
            self.config,
            {"as_of": BASELINE_AS_OF, "workspace": {"assessments": {}, "valuations": {}}},
        )
        self.sidecar = Path(self.config["research"]["path"]).parent / "cache" / "yahoo"
        self.sidecar.mkdir(parents=True, exist_ok=True)
        (self.sidecar / "SIM01.source.json").write_text(
            json.dumps(
                {
                    "key": "SIM01/history",
                    "provider": "yahoo",
                    "received_at": datetime(2026, 6, 30, tzinfo=timezone.utc).isoformat(),
                    "sha256": "3" * 64,
                }
            )
        )
        (self.sidecar / "SIM01.json").write_text(json.dumps({"close": [10.0, 11.0]}))

    def restored_project(self):
        target = backup_project(self.config, self.root / "backup", self.root / "demo")
        restored = restore_backup(target, self.root / "restored")
        return target, restored

    def test_backup_manifest_lists_the_provider_cache_entries(self):
        target, _ = self.restored_project()
        manifest = json.loads((target / "manifest.json").read_text())
        self.assertIn("cache/yahoo/SIM01.source.json", manifest["files"])
        self.assertIn("cache/yahoo/SIM01.json", manifest["files"])
        self.assertIn("research.sqlite3", manifest["files"])

    def test_restore_keeps_the_workflow_tables_and_their_rows(self):
        _, restored = self.restored_project()
        restored_config = load_config(restored / "research-config.json")
        research_path = Path(restored_config["research"]["path"])
        self.assertIn("review_workflows", table_names(research_path))
        store = ResearchRepository(research_path)
        workflow = store.workflow(self.workflow_id)
        self.assertEqual(workflow["status"], "running")
        self.assertEqual(workflow["stage"], "collecting")
        self.assertEqual(workflow["request"]["as_of"], BASELINE_AS_OF)
        self.assertEqual(store.latest("decision"), BASELINE_DECISION)
        self.assertEqual(store.load_run(self.run_id)["run_id"], self.run_id)

    def test_restore_copies_the_provider_cache_beside_the_restored_database(self):
        _, restored = self.restored_project()
        restored_config = load_config(restored / "research-config.json")
        cache = Path(restored_config["research"]["path"]).parent / "cache" / "yahoo"
        self.assertEqual(
            json.loads((cache / "SIM01.source.json").read_text())["key"], "SIM01/history"
        )
        self.assertEqual(json.loads((cache / "SIM01.json").read_text())["close"], [10.0, 11.0])

    def test_restore_refuses_to_overwrite_an_existing_project(self):
        target, restored = self.restored_project()
        before = sorted(path.name for path in restored.iterdir())
        with self.assertRaises(FileExistsError):
            restore_backup(target, restored)
        self.assertEqual(sorted(path.name for path in restored.iterdir()), before)
        store = ResearchRepository(Path(restored) / "research.sqlite3")
        self.assertEqual(store.workflow(self.workflow_id)["status"], "running")


if __name__ == "__main__":
    unittest.main()
