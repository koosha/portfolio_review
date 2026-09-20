"""Local application boundary: explicit run context, durable jobs and versioned inputs."""

import hashlib
import io
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path

import pandas as pd

from portfolio_lab.config import DEFAULTS, dashboard_patch, merge_config, validate_config
from portfolio_lab.ingestion import _pack_bundle, _unpack_bundle
from portfolio_lab.pipeline import (
    distinct_databases,
    is_collector,
    replay_analysis,
    run_analysis,
    save_analysis,
)

from .application import validate_workspace, validate_workspace_revision
from .calendar import REVIEW_KINDS, decision_context, review_context
from .decisions import decision_fields, last_decision
from .public import _clean, _safe_text, public_config, public_result, public_workspace
from .repository import ResearchRepository

_LATEST = object()  # "the stored supplemental", distinct from an explicit None.
INPUT_KINDS = {"prices", "fundamentals", "macro", "fund_holdings", "forecasts", "universe"}
DEFAULT_REVIEW_KIND = "current"
SUBMITTED_REVIEW_KIND = "historical"
UNSUPPORTED_CURRENT = "Current holdings come from the Yahoo collector; this configured source is analyzed only through saved reviews."
CURRENT_NOTE = "Current holdings and the last completed analysis are dated separately; they do not share a denominator."
UNREADABLE_COLLECTION = "Collector holdings could not be read. Saved reviews remain available."
LISTING_CACHE = "yahoo_listing"
FX_CACHE = "bank_of_canada_fx"
WORKFLOW_FIELDS = (
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
)
# The states an operation can still be worked on in; everything else is final.
LIVE_WORKFLOW = ("queued", "running", "waiting")
WORKFLOW_SWEEP = 50  # Recent operations examined when the application stops.
EXTENSION_ACTIONS = {
    "connected": None,
    "offline": "Open Chrome with the extension enabled; Yahoo stays signed in there.",
    "unpaired": "Install the Chrome extension and pair it with the key shown in Data → Yahoo Finance.",
}


def _public_workflow(record):
    """The browser's view of one operation: progress and next steps, nothing local."""
    projection = {key: record.get(key) for key in WORKFLOW_FIELDS}
    projection["cancel_requested"] = bool(record.get("cancel_requested"))
    return _clean(projection)


def _cache_health(config, provider):
    """How much this provider has already answered, from its own archived receipts."""
    from .provider_cache import cache_root, newest_sources

    root = cache_root(config, provider)
    try:
        responses = sum(1 for _ in root.glob("*.source.json")) if root.is_dir() else 0
        received = [
            source.get("received_at") for source in newest_sources(config, provider).values()
        ]
    except OSError:
        responses, received = 0, []
    return {
        "cached_responses": responses,
        "last_received_at": max(received) if received else None,
    }


def _installed(package):
    from importlib.util import find_spec

    try:
        return find_spec(package) is not None
    except (ImportError, ValueError):
        return False


def _mapping_date(snapshot, record):
    """The date a listing mapping must apply from to answer this snapshot.

    ``load_collector`` chooses a mapping by the account's attested valuation date when it
    has one, so a mapping dated on the review day would never reach that snapshot.
    """
    requested = snapshot["timeline"]["requested_date"]
    accounts = snapshot.get("accounts") or []
    valuations = [
        row.get("valuation_date")
        for row in accounts
        if row.get("account_id") == record.get("account_id") and row.get("valuation_date")
    ]
    return min([requested, *valuations])


def _companion_busy(companion):
    """Whether the paired browser is collecting, when one was supplied."""
    if companion is None:
        return lambda: False
    return lambda: bool(companion.status().get("busy"))


def default_config(directory):
    directory = Path(directory).resolve()
    config = deepcopy(DEFAULTS)
    config["source"]["path"] = str(directory / "portfolio.sqlite3")
    config["research"] = {
        "path": str(directory / "research.sqlite3"),
        "output_dir": str(directory / "reports"),
    }
    config["data"].update(mode="offline", sec_enabled=False, fred_enabled=False)
    return validate_config(config)


class ResearchService:
    def __init__(self, config, *, collector_busy=None, recover=False, companion=None):
        self.config = validate_config(deepcopy(config))
        distinct_databases(self.config)
        self.store = ResearchRepository(self.config["research"]["path"])
        self.collector = is_collector(self.config["source"]["path"])
        self.companion = companion
        self.collector_busy = collector_busy or _companion_busy(companion)
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="portfolio-review")
        # The monthly operation waits on a collection it does not control, so it runs on its
        # own thread; a sleeping collection must never hold a worker a preview job needs.
        self.workflow_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="portfolio-operation"
        )
        self.lock = threading.RLock()
        self.futures = {}
        # Every polling loop watches this, so shutting down is a cancellation rather than a
        # wait for a source that may never answer.
        self.stopping = threading.Event()
        if recover:
            self.store.recover_jobs()
            self.store.recover_workflows()

    def close(self):
        """Stop the waiting first, then finish anything no worker will ever finish."""
        self.stopping.set()
        self.workflow_executor.shutdown(wait=True, cancel_futures=True)
        self.executor.shutdown(wait=True, cancel_futures=False)
        self._cancel_unfinished_workflows()

    def resolved_config(self):
        config = dashboard_patch(self.config, self.store.latest("configuration", {}))
        input_settings = deepcopy(self.store.latest("override", {}).get("data", {}))
        for key, value in list(input_settings.items()):
            if key.endswith("_csv") and value:
                relocated = self.store.path.parent / "inputs" / Path(value).name
                if relocated.is_file():
                    input_settings[key] = str(relocated)
        if input_settings:
            config = validate_config(merge_config(config, {"data": input_settings}))
        return config

    def status(self):
        config = self.resolved_config()
        runs = self.store.list_runs()
        return {
            "product": "Portfolio Review",
            "mode": config["data"]["mode"],
            "dataset": "Yahoo holdings"
            if self.collector
            else "Synthetic demo"
            if config["data"]["mode"] == "demo"
            else "Configured source",
            "config": public_config(config),
            "runs": runs,
            "jobs": self.store.jobs(),
            "latest_run_id": runs[0]["run_id"] if runs else None,
            "default_as_of": decision_context()["decision_date"],
            "providers": {
                key: config["data"][key]
                for key in ("mode", "price_provider", "sec_enabled", "fred_enabled")
            },
            "collector_busy": bool(self.collector_busy()),
            "latest_collection": self._latest_collection(),
            "review_kinds": list(REVIEW_KINDS),
            "default_review_kind": DEFAULT_REVIEW_KIND,
            "active_workflow": self._active_workflow(),
            "last_workflow": self._last_workflow(),
            "last_decision": last_decision(self.store.records("decision")),
            "schema_version": 1,
        }

    def _active_workflow(self):
        active = self.store.active_workflow()
        return _public_workflow(active) if active else None

    def _last_workflow(self):
        recent = self.store.workflows(limit=1)
        return _public_workflow(recent[0]) if recent else None

    def _latest_collection(self):
        """Receipt facts of the newest collection; a read failure never breaks status.

        Only the collection's own dates are needed here, so this never runs listing
        identity or FX presentation: status is polled on every page load.
        """
        empty = {"collection_received_at": None, "account_count": None}
        if not self.collector:
            return {**empty, "status": "unsupported"}
        from .adapter import load_collector
        from .calendar import review_context
        from .current import collection_facts

        try:
            timeline = review_context("current")
            bundle = load_collector(
                self.config["source"]["path"],
                timeline["decision_date"],
                receipt_through=timeline["requested_date"],
                receipt_before=timeline["generated_at"],
            )
            return collection_facts(bundle)
        except ValueError as exc:
            return {**empty, "status": "unavailable", "error": _safe_text(str(exc))}
        except Exception:  # A cache or database failure must never break the status page.
            return {**empty, "status": "unavailable", "error": UNREADABLE_COLLECTION}

    def current(self):
        """Newest collected holdings beside the last completed analysis, each with its dates."""
        if not self.collector:
            return {"supported": False, "reason": UNSUPPORTED_CURRENT}
        return {
            "supported": True,
            "current": self._current_snapshot(),
            "latest_run": self._latest_run(),
            "note": CURRENT_NOTE,
        }

    def _current_snapshot(self, config=None, supplemental=_LATEST):
        """Current holdings with identity and USD presentation from cached providers."""
        from .current import current_snapshot

        if supplemental is _LATEST:
            supplemental = self.store.latest("supplemental")
        return current_snapshot(
            self.config["source"]["path"],
            supplemental=supplemental,
            config=config or self.resolved_config(),
        )

    def exceptions(self):
        """Open identity and currency exceptions of the newest collection."""
        if not self.collector:
            return {
                "supported": False,
                "reason": UNSUPPORTED_CURRENT,
                "exceptions": [],
                "count": 0,
                "generated_at": None,
            }
        snapshot = self._current_snapshot()
        records = snapshot["open_exceptions"]
        return {
            "supported": True,
            "exceptions": records,
            "count": len(records),
            "generated_at": snapshot["dates"]["generated_at"],
        }

    def save_resolution(self, payload):
        """Resolve one open exception by appending the next supplemental version.

        The payload ``{key, kind, source_id, snapshot_id, values}`` must name an exception
        that is open now. Account kinds set facts on the exception's snapshot; a listing
        choice applies from the date this snapshot is evaluated on, so an account with an
        attested valuation date is answered too. An answer that would leave the exception
        open is refused before anything is written, and the resolution is recorded beside
        the supplemental version it produced.
        """
        from .adapter import validate_supplemental
        from .resolutions import (
            append_resolution,
            open_exception,
            require_closed,
            resolution_values,
            validate_payload,
        )

        validate_payload(payload)
        with self.lock:
            config = self.resolved_config()
            snapshot = self._current_snapshot(config) if self.collector else None
            record = open_exception(payload, snapshot["open_exceptions"] if snapshot else [])
            kind = record["resolution"]["kind"]
            values = resolution_values(kind, payload.get("values"))
            issues = []
            supplemental = self._resolved_supplemental(
                config, snapshot, record, kind, values, issues
            )
            validated = validate_supplemental(supplemental)
            answered = self._current_snapshot(config, supplemental=validated)
            remaining = answered["open_exceptions"]
            require_closed(record["key"], remaining)
            supplemental_id, record_id = append_resolution(
                self.store, record, kind, values, validated
            )
        return {
            "record_id": record_id,
            "supplemental_record_id": supplemental_id,
            "remaining": len(remaining),
            "issues": _clean(issues),
        }

    def _resolved_supplemental(self, config, snapshot, record, kind, values, issues):
        """The next supplemental version this answer produces, not yet written."""
        from .resolutions import listing_mapping, merged_accounts, merged_securities

        latest = self.store.latest("supplemental")
        if kind != "security_listing":
            return merged_accounts(latest, record, values)
        mapping = listing_mapping(
            config, record, values, _mapping_date(snapshot, record), issues=issues
        )
        return merged_securities(latest, mapping)

    def _latest_run(self):
        runs = self.store.list_runs()
        if not runs:
            return None
        row = runs[0]
        saved = self.store.load_run(row["run_id"])
        metadata = saved.get("metadata", {})
        timeline = saved.get("timeline") or {}
        return {
            "run_id": row["run_id"],
            "as_of": row["as_of"],
            "created_at": row["created_at"],
            "review_kind": metadata.get("review_kind", SUBMITTED_REVIEW_KIND),
            "valuation_date": metadata.get("valuation_date"),
            "collection_received_at": metadata.get("collection_received_at"),
            "information_cutoff": metadata.get("information_cutoff")
            or timeline.get("information_cutoff")
            or timeline.get("decision_cutoff"),
        }

    def run(self, run_id):
        saved = self.store.load_run(run_id)
        bundle = self.store.load_bundle(run_id)
        return {
            "result": public_result(saved),
            "config": public_config(saved["saved_config"]),
            "workspace": public_workspace(bundle.get("workspace", {})),
            "previous_run_id": self._previous_run_id(saved["run_id"], saved.get("metadata", {})),
        }

    def _previous_run_id(self, run_id, metadata):
        """The previous review of the same kind by review date, or ``None``.

        What changed since the last review compares like with like: a current review
        against the previous current review, never against a historical month-end saved
        in between. The previous review is the one dated before this one, not the one
        saved before it, so a month backfilled out of order still compares against the
        review that actually preceded it. Only each candidate's metadata is read, never
        its whole archive.
        """
        review_kind = metadata.get("review_kind", SUBMITTED_REVIEW_KIND)
        as_of = metadata.get("as_of")
        rows = {row["run_id"]: row for row in self.store.list_runs()}
        if run_id not in rows:
            return None
        this = rows[run_id]
        earlier = [
            row
            for identifier, row in rows.items()
            if identifier != run_id and self._is_earlier(row, this, as_of)
        ]
        # Same-dated reviews are separated by when they were saved; an undated review can
        # only be ordered by that.
        earlier.sort(
            key=lambda row: (row.get("as_of") or "", row.get("created_at") or ""), reverse=True
        )
        for row in earlier:
            saved = self.store.load_run_part(row["run_id"], "metadata") or {}
            if saved.get("review_kind", SUBMITTED_REVIEW_KIND) == review_kind:
                return row["run_id"]
        return None

    @staticmethod
    def _is_earlier(row, this, as_of):
        """Whether ``row`` names a review preceding ``this`` one."""
        theirs, mine = row.get("as_of"), as_of or this.get("as_of")
        if theirs and mine and theirs != mine:
            return theirs < mine
        return (row.get("created_at") or "") < (this.get("created_at") or "")

    def submit(self, payload):
        if not isinstance(payload, dict):
            raise ValueError("Send an object describing the research job.")
        kind = payload.get("kind")
        allowed = {
            "monthly": {"as_of", "refresh", "patch", "workspace", "review_kind"},
            "preview": {"base_run_id", "patch", "workspace"},
            "save": {"preview_job_id"},
            "evaluate": {"run_id", "prices_csv", "evaluation_date"},
            "evaluate_ledgers": {
                "run_id",
                "events_by_arm",
                "end_prices",
                "end_date",
                "coverage_confirmed",
                "execution_confirmed",
            },
        }
        if kind not in allowed or set(payload) - allowed[kind] - {"kind", "request_key"}:
            raise ValueError("Unknown job kind or fields.")
        request = {
            key: deepcopy(value)
            for key, value in payload.items()
            if key not in {"kind", "request_key"}
        }
        if kind == "monthly":
            if self.collector_busy():
                raise ValueError(
                    "Wait for the Yahoo holdings pull to finish before starting a review."
                )
            if type(request.get("refresh", False)) is not bool:
                raise ValueError("refresh must be true or false.")
            review_kind = request.get("review_kind", SUBMITTED_REVIEW_KIND)
            # The calendar owns the kind/date contract: it rejects unknown kinds, an explicit
            # date on a current review, and malformed historical dates.
            review_context(review_kind, as_of=request.get("as_of"))
            if review_kind == SUBMITTED_REVIEW_KIND:
                # The stored request is hashed under its request key. Omitting the default keeps
                # stable scheduler keys created before review kinds existed valid on retry.
                request.pop("review_kind", None)
            request["resolved_config"] = dashboard_patch(
                self.resolved_config(), request.pop("patch", {})
            )
            request["supplemental"] = self.store.latest("supplemental")
        elif kind == "preview":
            base = self.store.load_run(str(request.get("base_run_id", "")))
            dashboard_patch(base["saved_config"], request.get("patch", {}))
        elif kind == "save":
            previous = self.store.job(str(request.get("preview_job_id", "")))
            if previous["kind"] != "preview" or previous["status"] != "complete":
                raise ValueError("Choose a successfully calculated preview to save.")
        elif kind == "evaluate":
            self.store.load_run(str(request.get("run_id", "")))
            if not isinstance(request.get("prices_csv"), str):
                raise ValueError("Supply observed outcome prices as CSV text.")
        elif kind == "evaluate_ledgers":
            self.store.load_run(str(request.get("run_id", "")))
            if not isinstance(request.get("events_by_arm"), dict) or not isinstance(
                request.get("end_prices"), list
            ):
                raise ValueError(
                    "Comparator evaluation needs event lists per arm and explicit terminal price records."
                )
        if "workspace" in request:
            request["workspace"] = validate_workspace(request["workspace"])
            if kind == "preview":
                previous = self.store.load_bundle(request["base_run_id"]).get("workspace", {})
                validate_workspace_revision(request["workspace"], previous)
        with self.lock:
            job_id, created = self.store.create_job(kind, payload.get("request_key"), request)
            if created:
                self.futures[job_id] = self.executor.submit(self._execute, job_id)
        return self.job(job_id)

    def _execute(self, job_id):
        self.store.update_job(job_id, "running")
        job = self.store.job(job_id)
        request, kind = job["payload"], job["kind"]
        try:
            if kind == "monthly":
                if self.collector_busy():
                    raise ValueError("A holdings pull started; finish it and submit a new review.")
                config = request["resolved_config"]
                result, bundle = run_analysis(
                    config,
                    request.get("as_of"),
                    request.get("refresh", False),
                    save=False,
                    supplemental=request.get("supplemental"),
                    workspace=request.get("workspace"),
                    review_kind=request.get("review_kind", SUBMITTED_REVIEW_KIND),
                )
                result["run_id"] = job_id
                result = save_analysis(result, config, bundle)
            elif kind == "preview":
                result, bundle, config = replay_analysis(
                    self.config,
                    request["base_run_id"],
                    request.get("patch"),
                    workspace=request.get("workspace"),
                )
            elif kind == "save":
                preview = self.store.job(request["preview_job_id"])["output"]
                config = preview["config"]
                bundle = _unpack_bundle(preview["bundle"])
                result = deepcopy(preview["result"])
                result["metadata"]["preview"] = False
                result["run_id"] = job_id
                result = save_analysis(result, config, bundle)
            elif kind == "evaluate_ledgers":
                from .prospective import evaluate_comparators

                baselines = [row["payload"] for row in self.store.records("comparator")]
                evaluation = evaluate_comparators(
                    baselines,
                    request["events_by_arm"],
                    request["end_prices"],
                    end_date=request["end_date"],
                    coverage_confirmed=request.get("coverage_confirmed", False),
                    execution_confirmed=request.get("execution_confirmed"),
                )
                record_id = self.store.append_record(
                    "evaluation", {"kind": "comparator", **evaluation}, run_id=request["run_id"]
                )
                self.store.update_job(
                    job_id, "complete", output={"evaluation": evaluation, "record_id": record_id}
                )
                return
            else:
                from .prospective import evaluate_saved_forecasts

                bundle = self.store.load_bundle(request["run_id"])
                saved = self.store.load_run(request["run_id"])
                bundle["forecasts"] = pd.DataFrame(saved.get("forecast_inputs", []))
                evaluation = evaluate_saved_forecasts(
                    bundle,
                    pd.read_csv(io.StringIO(request["prices_csv"])),
                    request["evaluation_date"],
                )
                record_id = self.store.append_record(
                    "evaluation", evaluation, run_id=request["run_id"]
                )
                self.store.update_job(
                    job_id, "complete", output={"evaluation": evaluation, "record_id": record_id}
                )
                return
            if kind in {"monthly", "save"}:
                try:
                    self._record_baselines(result, config, bundle)
                    if kind == "monthly":
                        self._evaluate_prior(result, bundle)
                except (ValueError, KeyError, TypeError):
                    result["metadata"]["prospective_status"] = (
                        "Baseline records need review; saved analysis remains available."
                    )
            self.store.update_job(
                job_id,
                "complete",
                output={"result": result, "config": config, "bundle": _pack_bundle(bundle)},
            )
        except (ValueError, KeyError, FileNotFoundError) as exc:
            self.store.update_job(job_id, "failed", error=_safe_text(str(exc)))
        except Exception:
            self.store.update_job(
                job_id,
                "failed",
                error="Research calculation failed. Previous saved runs remain available; inspect local diagnostics or correct the inputs and retry.",
            )

    def job(self, job_id):
        job = self.store.job(job_id)
        result = {
            key: job[key]
            for key in (
                "job_id",
                "kind",
                "request_key",
                "status",
                "created_at",
                "updated_at",
                "error",
            )
        }
        output = job["output"]
        if output and "result" in output:
            workspace = _unpack_bundle(output["bundle"]).get("workspace", {})
            result["output"] = {
                "result": public_result(output["result"]),
                "config": public_config(output["config"]),
                "workspace": public_workspace(workspace),
            }
        elif output:
            result["output"] = output
        return result

    def wait(self, job_id, timeout=300):
        future = self.futures.get(job_id)
        if future:
            future.result(timeout=timeout)
        return self.job(job_id)

    def start_workflow(self, payload):
        """Start the one monthly operation, or hand back the one already running.

        A second click during an operation is the same operation: the active one is
        returned as reused, and its operation key keeps it identifiable across restarts.
        """
        request = self._workflow_request(payload)
        review_kind = request.pop("review_kind")
        with self.lock:
            active = self.store.active_workflow()
            if active:
                return {**_public_workflow(active), "reused": True}
            workflow_id, created = self.store.create_workflow(
                payload.get("operation_key"), review_kind, request
            )
            if created:
                from .workflow import HANDOFF_FAILED, ReviewWorkflow

                try:
                    self.futures[workflow_id] = self.workflow_executor.submit(
                        ReviewWorkflow(self, workflow_id).run
                    )
                except Exception:
                    # The durable row is already committed. A hand-off that never reached a
                    # worker would leave it live forever and block every later review.
                    self._finish_workflow(workflow_id, "failed", HANDOFF_FAILED)
                    raise
        return {**self.workflow(workflow_id), "reused": not created}

    def _workflow_request(self, payload):
        """The supplied operation request, validated before anything durable exists."""
        allowed = {"operation_key", "review_kind", "month", "collect", "refresh"}
        if not isinstance(payload, dict) or set(payload) - allowed:
            raise ValueError(
                "An operation accepts only an operation key, review kind, month, collect and refresh."
            )
        review_kind = payload.get("review_kind", DEFAULT_REVIEW_KIND)
        if review_kind not in REVIEW_KINDS:
            raise ValueError("Review kind must be current or historical.")
        month = payload.get("month")
        if month is not None:
            if review_kind != SUBMITTED_REVIEW_KIND:
                raise ValueError(
                    "Current reviews use the latest collection; choose a historical review for a month."
                )
            # The calendar owns the month contract and its error messages.
            review_context(SUBMITTED_REVIEW_KIND, month=str(month))
        collect, refresh = payload.get("collect", True), payload.get("refresh")
        if type(collect) is not bool or (refresh is not None and type(refresh) is not bool):
            raise ValueError("collect and refresh must be true or false.")
        return {
            "review_kind": review_kind,
            "month": month,
            "collect": collect,
            "refresh": refresh,
        }

    def workflow(self, workflow_id):
        return _public_workflow(self.store.workflow(str(workflow_id)))

    def workflows(self, limit=20):
        return [_public_workflow(record) for record in self.store.workflows(limit)]

    def cancel_workflow(self, workflow_id):
        """Ask a live operation to stop before it publishes; a finished one is unchanged."""
        workflow_id = str(workflow_id)
        with self.lock:
            record = self.store.workflow(workflow_id)
            self.store.request_cancel(workflow_id)
            future = self.futures.get(workflow_id)
            if record["status"] in LIVE_WORKFLOW and (future is None or future.done()):
                # Nothing is left to read the flag, so the cancellation is written here:
                # an operation without a worker must still have a way out of the way.
                self._finish_workflow(workflow_id, "cancelled")
        return self.workflow(workflow_id)

    def _cancel_unfinished_workflows(self):
        """Finish operations this application will not run; a live row blocks the next one."""
        for record in self.store.workflows(limit=WORKFLOW_SWEEP):
            if record["status"] in LIVE_WORKFLOW:
                self._finish_workflow(record["workflow_id"], "cancelled")

    def _finish_workflow(self, workflow_id, status, error=None):
        """Write a terminal state for an operation no worker will finish."""
        try:
            self.store.update_workflow(workflow_id, status=status, error=error)
        except (ValueError, KeyError):
            pass  # Already finished or gone; nothing is holding up a later review.

    def provider_health(self):
        """Each source's own state and the next step when it cannot answer."""
        config = self.resolved_config()
        data = config["data"]
        environment = data.get("fred_api_key_env", "FRED_API_KEY")
        return {
            "mode": data["mode"],
            "providers": [
                self._extension_health(),
                {
                    "name": "yahoo_listings",
                    "enabled": data["listing_provider"] == "yahoo",
                    **_cache_health(config, LISTING_CACHE),
                    "action": None
                    if data["listing_provider"] == "yahoo"
                    else "Listing identity is off; turn it on in Data to resolve held symbols.",
                },
                {
                    "name": "bank_of_canada_fx",
                    "enabled": "bank_of_canada" in data.get("fx_providers", []),
                    **_cache_health(config, FX_CACHE),
                    "action": None,
                },
                {
                    "name": "yahoo_prices",
                    "enabled": data["price_provider"] == "yahoo",
                    "dependency_installed": _installed("yfinance"),
                    **_cache_health(config, "yahoo"),
                    "action": None
                    if _installed("yfinance")
                    else "Install the optional yfinance package to fetch Yahoo price history.",
                },
                {
                    "name": "sec",
                    "enabled": bool(data["sec_enabled"]),
                    "contact_configured": bool(str(data.get("sec_user_agent", "")).strip()),
                    **_cache_health(config, "sec"),
                    "action": None
                    if str(data.get("sec_user_agent", "")).strip()
                    else "Set an application name and contact email in Data before fetching SEC filings.",
                },
                {
                    "name": "fred",
                    "enabled": bool(data["fred_enabled"]),
                    "credential_present": bool(os.environ.get(environment)),
                    "env_var": environment,
                    **_cache_health(config, "fred"),
                    "action": None
                    if os.environ.get(environment)
                    else f"Set the {environment} environment variable before starting the app; keys are never stored here.",
                },
            ],
        }

    def _extension_health(self):
        state = self.companion.status() if self.companion else {}
        version = state.get("extension_version")
        if state.get("browser_open"):
            status = "connected"
        elif version:
            status = "offline"
        else:
            status = "unpaired"
        return {
            "name": "chrome_extension",
            "status": status,
            "version": version,
            "required_version": state.get("required_extension_version"),
            "action": EXTENSION_ACTIONS.get(status),
        }

    def save_settings(self, patch):
        with self.lock:
            resolved = dashboard_patch(self.resolved_config(), patch)
            used = public_config(resolved)
            record_id = self.store.append_record("configuration", used)
        return {"record_id": record_id, "config": used}

    def supplemental(self):
        from .adapter import supplemental_template

        current = self.store.latest("supplemental")
        if not self.collector:
            return {
                "supported": False,
                "reason": "This configured source already supplies canonical accounts and identities.",
            }
        template = supplemental_template(self.config["source"]["path"])
        return {"supported": True, "template": template, "saved": current}

    def import_input(self, payload):
        kind = payload.get("kind")
        if kind == "supplemental":
            from .adapter import validate_supplemental

            validated = validate_supplemental(payload.get("data"))
            record_id = self.store.append_record("supplemental", validated)
            return {
                "record_id": record_id,
                "status": "saved",
                "message": "Supplemental facts saved. Start a new review to use them.",
            }
        if kind not in INPUT_KINDS or not isinstance(payload.get("csv"), str):
            raise ValueError("Choose supplemental JSON or a supported CSV input type.")
        text = payload["csv"]
        if len(text.encode()) > 8_000_000:
            raise ValueError("CSV inputs must be no larger than 8 MB.")
        frame = pd.read_csv(io.StringIO(text))
        from portfolio_lab.providers import REQUIRED_COLUMNS

        required = set(REQUIRED_COLUMNS.get(kind, []))
        if kind == "universe":
            required = {"security_id", "ticker", "issuer_id", "instrument_type", "currency"}
        if not required <= set(frame.columns):
            raise ValueError(
                f"Missing required columns: {', '.join(sorted(required - set(frame.columns)))}"
            )
        digest = hashlib.sha256(text.encode()).hexdigest()
        directory = self.store.path.parent / "inputs"
        directory.mkdir(mode=0o700, exist_ok=True)
        target = directory / f"{kind}-{digest}.csv"
        if not target.exists():
            with target.open("x") as file:
                file.write(text)
            target.chmod(0o600)
        with self.lock:
            settings = deepcopy(self.store.latest("override", {}).get("data", {}))
            settings[f"{kind}_csv"] = str(target)
            self.store.append_record("override", {"data": settings, "source_hash": digest})
        return {
            "status": "saved",
            "rows": len(frame),
            "sha256": digest,
            "message": "Input saved. Start a new review to use it.",
        }

    def save_decision(self, payload):
        """Record one monthly decision beside the run it was taken on.

        The record states what was chosen, what it was weighed against and which review
        it belongs to, so the next review can say what changed without re-deriving any
        of it. Recording a decision is not a calculation: no saved run is rewritten.
        """
        if not isinstance(payload, dict):
            raise ValueError("Send an object describing the research decision.")
        run_id = str(payload.get("run_id", ""))
        data = decision_fields(payload, self.store.load_run(run_id), SUBMITTED_REVIEW_KIND)
        if data["workflow_id"] is not None:
            # A named operation must exist: an unresolvable one is an unverifiable claim
            # about which review produced this decision.
            try:
                self.store.workflow(data["workflow_id"])
            except KeyError as exc:
                raise ValueError("Name the review operation this decision was taken in.") from exc
        record_id = self.store.append_record(
            "decision", data, run_id=run_id, parent_id=payload.get("parent_id")
        )
        return {"record_id": record_id, "status": "saved"}

    def _record_baselines(self, result, config, bundle):
        from .prospective import baseline_records

        with self.lock:
            if not self.store.records("comparator") and result.get("summary", {}).get("complete"):
                for payload in baseline_records(result, config, bundle):
                    self.store.append_record("comparator", payload, run_id=result["run_id"])

    def _evaluate_prior(self, current, observations):
        """Monthly outcome checks use refreshed observations, never revised forecasts."""
        from .prospective import evaluate_saved_forecasts

        prices = observations.get("prices", pd.DataFrame())
        evaluation_at = observations["timeline"]["generated_at"]
        for row in self.store.list_runs():
            if row["run_id"] == current["run_id"] or row["as_of"] >= current["metadata"]["as_of"]:
                continue
            saved = self.store.load_run(row["run_id"])
            if not saved.get("forecast_inputs"):
                continue
            frozen = self.store.load_bundle(row["run_id"])
            frozen["forecasts"] = pd.DataFrame(saved["forecast_inputs"])
            evaluation = evaluate_saved_forecasts(frozen, prices, evaluation_at)
            self.store.append_record(
                "evaluation",
                {
                    "kind": "monthly_forecast_check",
                    "observation_run_id": current["run_id"],
                    **evaluation,
                },
                run_id=row["run_id"],
            )

    def compare(self, left, right):
        first, second = self.run(left), self.run(right)
        differences = []

        def visit(a, b, path):
            if isinstance(a, dict) and isinstance(b, dict):
                for key in sorted(set(a) | set(b)):
                    visit(a.get(key), b.get(key), f"{path}.{key}" if path else key)
            elif a != b:
                differences.append({"field": path, "left": a, "right": b})

        visit(first["config"], second["config"], "assumptions")
        visit(first["workspace"], second["workspace"], "company_research")
        return {
            "left": left,
            "right": right,
            "assumption_changes": differences,
            "modeled_outcomes": [
                {
                    "field": field,
                    "left": first["result"].get(field),
                    "right": second["result"].get(field),
                }
                for field in ("summary", "scenarios", "allocation")
            ],
            "label": "Frozen-run assumptions and modeled outcomes; this is not realized performance.",
        }

    def save_providers(self, payload):
        allowed = {"mode", "price_provider", "sec_enabled", "fred_enabled", "sec_user_agent"}
        if not isinstance(payload, dict) or set(payload) - allowed:
            raise ValueError(
                "Only provider mode, selection and SEC contact can be set here. Keep API keys in server environment variables."
            )
        if self.config["data"]["mode"] == "demo" and payload.get("mode", "demo") != "demo":
            raise ValueError(
                "Synthetic source data cannot switch into live mode. Open the real app data directory."
            )
        if self.config["data"]["mode"] != "demo" and payload.get("mode") == "demo":
            raise ValueError(
                "Use the separate demo command; real holdings cannot be relabeled synthetic."
            )
        if "sec_user_agent" in payload and (
            not isinstance(payload["sec_user_agent"], str) or len(payload["sec_user_agent"]) > 256
        ):
            raise ValueError("SEC contact must be a short application/contact string.")
        with self.lock:
            settings = deepcopy(self.store.latest("override", {}).get("data", {}))
            settings.update(payload)
            validate_config(merge_config(self.resolved_config(), {"data": settings}))
            record_id = self.store.append_record("override", {"data": settings})
        return {"record_id": record_id, "providers": self.status()["providers"]}

    def valuation_link(self, payload):
        from .evidence import validate_assessment
        from .valuation import calculate_eps

        if payload.get("reviewed") is not True or not str(payload.get("rationale", "")).strip():
            raise ValueError("Review the scenario linkage and record its rationale first.")
        base = self.store.load_run(str(payload.get("base_run_id", "")))
        frozen = self.store.load_bundle(base["run_id"])
        workspace = validate_workspace(payload.get("workspace", {}))
        validate_workspace_revision(workspace, frozen.get("workspace", {}))
        sid = str(payload.get("security_id", ""))
        model = workspace.get("valuations", {}).get(sid, {}).get("eps")
        if not model:
            raise ValueError("Complete the EPS/multiple model before linking scenario returns.")
        horizon = base["saved_config"]["allocation"]["horizon_months"]
        if model.get("horizon_months") != horizon:
            raise ValueError("The model and selected run must use the same horizon.")
        if workspace.get("assessments", {}).get(sid, {}).get("horizon_months") != horizon:
            raise ValueError(
                "The assessment and linked portfolio scenarios must use the same horizon."
            )
        timeline = frozen.get("timeline") or decision_context(frozen["as_of"])
        timeline = {**timeline, "generated_at": decision_context(frozen["as_of"])["generated_at"]}
        assessment = validate_assessment(
            workspace.get("assessments", {}).get(sid, {}),
            decision_cutoff=timeline["decision_cutoff"],
            generated_at=timeline["generated_at"],
        )
        if assessment.get("status") != "ready":
            raise ValueError(
                "The company assessment needs eligible evidence and completed review before linkage."
            )
        calculated = calculate_eps(model)
        if calculated["status"] != "ready":
            raise ValueError("All three EPS/multiple scenarios need valid inputs.")
        inputs = frozen.get("forecasts", pd.DataFrame())
        if inputs.empty or sid not in set(inputs["security_id"]):
            raise ValueError(
                "Import a common joint scenario set including this security and the benchmark first."
            )
        labels = {str(value).lower(): str(value) for value in inputs["scenario"].unique()}
        if set(labels) != {"adverse", "central", "favorable"}:
            raise ValueError(
                "Explicitly map the company's adverse, central and favorable cases to the portfolio joint states before linking."
            )
        overrides = deepcopy(base["saved_config"]["allocation"]["return_overrides"])
        overrides[sid] = {
            labels[row["label"]]: row["total_return"] for row in calculated["scenarios"]
        }
        return {
            "patch": {"allocation": {"return_overrides": overrides}},
            "basis": "subjective",
            "rationale": str(payload["rationale"]).strip(),
            "requires_recalculation": True,
        }
