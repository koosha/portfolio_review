"""Durable jobs and immutable application records beside the frozen run archive."""

import hashlib
import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone

from portfolio_lab.ingestion import ResearchStore, _dumps

WORKFLOW_STATUSES = ("queued", "running", "waiting", "complete", "failed", "cancelled")
INTERRUPTED_WORKFLOW = "Interrupted by application restart; start a new review."
RECORD_KINDS = {
    "configuration",
    "supplemental",
    "assessment",
    "decision",
    "evaluation",
    "comparator",
    "override",
    "resolution",
}


def now():
    return datetime.now(timezone.utc).isoformat()


def _workflow_record(row):
    """One durable operation row with its JSON columns decoded."""
    record = dict(row)
    record["request"] = json.loads(record.pop("request_json"))
    record["stages"] = json.loads(record.pop("stages_json"))
    record["providers"] = json.loads(record.pop("providers_json"))
    record["cancel_requested"] = bool(record["cancel_requested"])
    return record


class ResearchRepository(ResearchStore):
    def __init__(self, path):
        super().__init__(path)
        with self.connect() as connection:
            connection.executescript("""
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
                CREATE TABLE IF NOT EXISTS review_workflows (
                    workflow_id TEXT PRIMARY KEY, operation_key TEXT UNIQUE NOT NULL,
                    status TEXT NOT NULL
                        CHECK(status IN ('queued','running','waiting','complete','failed','cancelled')),
                    stage TEXT, review_kind TEXT NOT NULL, request_json TEXT NOT NULL,
                    stages_json TEXT NOT NULL, providers_json TEXT NOT NULL,
                    batch_id TEXT, run_id TEXT, error TEXT,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, owner_pid INTEGER
                );
                CREATE INDEX IF NOT EXISTS review_workflows_created
                    ON review_workflows(created_at DESC);
            """)
            versions = connection.execute("SELECT schema_version FROM review_metadata").fetchall()
            if [row[0] for row in versions] != [1]:
                raise ValueError(
                    "Unsupported research application schema; restore compatible code before opening it."
                )
            columns = {row[1] for row in connection.execute("PRAGMA table_info(review_jobs)")}
            if "owner_pid" not in columns:
                connection.execute("ALTER TABLE review_jobs ADD COLUMN owner_pid INTEGER")

    def connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def append_record(self, kind, payload, *, run_id=None, parent_id=None):
        if kind not in RECORD_KINDS or not isinstance(payload, dict):
            raise ValueError("Choose a supported record type and an object payload.")
        if run_id:
            self.load_run(run_id)
        record_id = uuid.uuid4().hex
        with self.connect() as connection:
            if (
                parent_id
                and not connection.execute(
                    "SELECT 1 FROM review_records WHERE record_id=?", (parent_id,)
                ).fetchone()
            ):
                raise ValueError("Parent record does not exist.")
            connection.execute(
                "INSERT INTO review_records VALUES (?,?,?,?,?,?)",
                (record_id, kind, run_id, parent_id, now(), _dumps(payload)),
            )
        return record_id

    def records(self, kind, run_id=None):
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM review_records WHERE kind=? AND (? IS NULL OR run_id=?) ORDER BY created_at DESC, record_id DESC",
                (kind, run_id, run_id),
            ).fetchall()
        return [
            {
                **{k: row[k] for k in row.keys() if k != "payload_json"},
                "payload": json.loads(row["payload_json"]),
            }
            for row in rows
        ]

    def latest(self, kind, default=None):
        records = self.records(kind)
        return records[0]["payload"] if records else default

    def create_job(self, kind, request_key, payload):
        if not isinstance(request_key, str) or not 8 <= len(request_key) <= 128:
            raise ValueError("A request key of 8–128 characters is required.")
        serialized = _dumps(payload)
        digest = hashlib.sha256((kind + serialized).encode()).hexdigest()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT job_id,payload_hash FROM review_jobs WHERE request_key=?", (request_key,)
            ).fetchone()
            if row:
                if row["payload_hash"] != digest:
                    raise ValueError("This request key already identifies different inputs.")
                return row["job_id"], False
            job_id = uuid.uuid4().hex
            connection.execute(
                "INSERT INTO review_jobs (job_id,request_key,payload_hash,kind,status,created_at,updated_at,payload_json,output_json,error,owner_pid) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    job_id,
                    request_key,
                    digest,
                    kind,
                    "queued",
                    now(),
                    now(),
                    serialized,
                    None,
                    None,
                    os.getpid(),
                ),
            )
        return job_id, True

    def update_job(self, job_id, status, *, output=None, error=None):
        if status not in {"running", "complete", "failed"}:
            raise ValueError("Invalid job state.")
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE review_jobs SET status=?,updated_at=?,output_json=?,error=? WHERE job_id=? AND status IN ('queued','running')",
                (status, now(), _dumps(output) if output is not None else None, error, job_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("Job is already finished or does not exist.")

    def job(self, job_id):
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM review_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
        if row is None:
            raise KeyError("Research job not found.")
        result = dict(row)
        result["payload"] = json.loads(result.pop("payload_json"))
        result["output"] = json.loads(result.pop("output_json") or "null")
        return result

    def jobs(self):
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT job_id,kind,status,created_at,updated_at,error FROM review_jobs ORDER BY created_at DESC LIMIT 30"
            ).fetchall()
        return [dict(row) for row in rows]

    def recover_jobs(self):
        """Recover interrupted work without cancelling a live monthly CLI process."""
        with self.connect() as connection:
            unfinished = connection.execute(
                "SELECT job_id,owner_pid FROM review_jobs WHERE status IN ('queued','running')"
            ).fetchall()
            for job in unfinished:
                if job["owner_pid"]:
                    try:
                        os.kill(job["owner_pid"], 0)
                    except ProcessLookupError:
                        pass
                    except PermissionError:
                        continue  # Liveness cannot be disproved; do not cancel another job.
                    else:
                        continue
                connection.execute(
                    "UPDATE review_jobs SET status='failed',updated_at=?,error='Interrupted by application restart; submit a new request.' WHERE job_id=? AND status IN ('queued','running')",
                    (now(), job["job_id"]),
                )

    def create_workflow(self, operation_key, review_kind, request):
        """``(workflow_id, created)``: one durable operation per operation key."""
        if not isinstance(operation_key, str) or not 8 <= len(operation_key) <= 128:
            raise ValueError("An operation key of 8–128 characters is required.")
        if not isinstance(request, dict):
            raise ValueError("Send an object describing the review operation.")
        from .workflow import STAGES

        stages = {stage: {"status": "pending"} for stage in STAGES}
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT workflow_id FROM review_workflows WHERE operation_key=?", (operation_key,)
            ).fetchone()
            if row:
                return row["workflow_id"], False
            workflow_id = uuid.uuid4().hex
            connection.execute(
                "INSERT INTO review_workflows (workflow_id,operation_key,status,stage,review_kind,"
                "request_json,stages_json,providers_json,batch_id,run_id,error,cancel_requested,"
                "created_at,updated_at,owner_pid) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    workflow_id,
                    operation_key,
                    "queued",
                    None,
                    str(review_kind),
                    _dumps(request),
                    _dumps(stages),
                    _dumps({}),
                    None,
                    None,
                    None,
                    0,
                    now(),
                    now(),
                    os.getpid(),
                ),
            )
        return workflow_id, True

    def update_workflow(
        self,
        workflow_id,
        *,
        status=None,
        stage=None,
        stages=None,
        providers=None,
        batch_id=None,
        run_id=None,
        error=None,
    ):
        """Record progress while the operation is still live; a finished one is frozen."""
        if status is not None and status not in WORKFLOW_STATUSES:
            raise ValueError("Invalid review operation state.")
        fields, values = ["updated_at=?"], [now()]
        for column, value in (
            ("status", status),
            ("stage", stage),
            ("batch_id", batch_id),
            ("run_id", run_id),
            ("error", error),
        ):
            if value is not None:
                fields.append(f"{column}=?")
                values.append(value)
        for column, value in (("stages_json", stages), ("providers_json", providers)):
            if value is not None:
                fields.append(f"{column}=?")
                values.append(_dumps(value))
        with self.connect() as connection:
            cursor = connection.execute(
                f"UPDATE review_workflows SET {','.join(fields)} WHERE workflow_id=? "
                "AND status IN ('queued','running','waiting')",
                (*values, workflow_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("This review operation is already finished or does not exist.")

    def request_cancel(self, workflow_id):
        """Ask a live operation to stop; a finished one is never rewritten."""
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE review_workflows SET cancel_requested=1,updated_at=? WHERE workflow_id=? "
                "AND status IN ('queued','running','waiting')",
                (now(), workflow_id),
            )
        return cursor.rowcount == 1

    def workflow(self, workflow_id):
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM review_workflows WHERE workflow_id=?", (workflow_id,)
            ).fetchone()
        if row is None:
            raise KeyError("Review operation not found.")
        return _workflow_record(row)

    def workflows(self, limit=20):
        limit = min(max(int(limit), 1), 100)
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM review_workflows ORDER BY created_at DESC, workflow_id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_workflow_record(row) for row in rows]

    def active_workflow(self):
        """The newest operation that has not finished, or ``None``."""
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM review_workflows WHERE status IN ('queued','running','waiting') "
                "ORDER BY created_at DESC, workflow_id DESC LIMIT 1"
            ).fetchone()
        return _workflow_record(row) if row else None

    def recover_workflows(self):
        """Fail operations whose owning application is gone, never a live one."""
        with self.connect() as connection:
            unfinished = connection.execute(
                "SELECT workflow_id,owner_pid FROM review_workflows "
                "WHERE status IN ('queued','running','waiting')"
            ).fetchall()
            for workflow in unfinished:
                if workflow["owner_pid"]:
                    try:
                        os.kill(workflow["owner_pid"], 0)
                    except ProcessLookupError:
                        pass
                    except PermissionError:
                        continue  # Liveness cannot be disproved; leave it alone.
                    else:
                        continue
                connection.execute(
                    "UPDATE review_workflows SET status='failed',updated_at=?,error=? "
                    "WHERE workflow_id=? AND status IN ('queued','running','waiting')",
                    (now(), INTERRUPTED_WORKFLOW, workflow["workflow_id"]),
                )
