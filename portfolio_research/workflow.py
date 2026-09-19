"""One monthly operation: collect, resolve, fetch, analyze and publish exactly once.

The coordinator owns the order and the waiting; every stage records what it did, what
it could not do and what the owner must answer. A cancelled or failed operation never
writes a run, so the last usable review is always the one that was published last.
"""

import time
import uuid
from copy import deepcopy
from time import monotonic

from .public import _safe_text
from .repository import now

STAGES = ("collecting", "resolving", "fetching", "analyzing", "publishing")
POLL_SECONDS = 1.0
# How often a long provider fetch may look up from its work: once a second to see
# whether the owner asked it to stop, once every few seconds to say how far it is.
PROBE_SECONDS = 1.0
PROGRESS_SECONDS = 3.0
BUSY_WAIT_SECONDS = 600  # Wait out a pull someone else started.
COLLECT_TIMEOUT_SECONDS = 900  # Give up on this pull and use the last published one.
FETCH_INTERRUPTED = "PROVIDER_FETCH_INTERRUPTED"
# A collected snapshot values its positions at its own receipt, not at the session close.
COLLECTION_VALUATION_TOLERANCE = 0.02

CANCELLED_MESSAGE = "Cancelled before publishing; the last usable review is unchanged."
NOT_CONNECTED = (
    "Chrome extension not connected. Pair it in Data → Yahoo Finance to collect fresh "
    "holdings; the last complete collection is used."
)
NO_SELECTION = (
    "Select at least one Yahoo portfolio in Holdings to collect fresh holdings; the last "
    "complete collection is used."
)
RETRY_ACCOUNTS = (
    "Retry the failed accounts in Holdings; the last complete collection is used for this review."
)
EXCEPTIONS_ACTION = (
    "{count} account/currency/identity exceptions need your answer in Overview → Exceptions; "
    "unaffected analysis continues."
)
PROVIDER_ACTION = (
    "These data sources did not answer: {names}. The review continues with the data that was "
    "available; retry later for fuller coverage."
)
COLLECT_TIMED_OUT = (
    "Chrome did not finish this collection in time. The last complete collection is used."
)
UNEXPECTED_FAILURE = (
    "The review could not be completed. Previous saved reviews remain available; correct the "
    "inputs and start a new review."
)
PROSPECTIVE_STATUS = "Baseline records need review; saved analysis remains available."
HANDOFF_FAILED = (
    "The review could not be started. Previous saved reviews remain available; start a new review."
)


class _Cancelled(Exception):
    """Raised inside a polling loop once the owner asked the operation to stop."""


def _codes(issues, *prefixes):
    """Distinct issue codes raised by one capability, in a stable order."""
    found = {
        str(issue.get("code") or "")
        for issue in issues
        if isinstance(issue, dict) and str(issue.get("code") or "").startswith(prefixes)
    }
    return sorted(found)


def _distinct(bundle, name, column):
    frame = bundle.get(name)
    if frame is None or column not in getattr(frame, "columns", []):
        return 0
    return int(frame[column].dropna().nunique())


def _frame_rows(bundle, name):
    frame = bundle.get(name)
    return 0 if frame is None else int(len(frame))


def _equities(bundle):
    frame = bundle.get("securities")
    if frame is None or "instrument_type" not in getattr(frame, "columns", []):
        return _frame_rows(bundle, "securities")
    return int((frame["instrument_type"] == "equity").sum())


def _capability(*, enabled, refresh, covered, requested, issues, failed=False):
    """One capability's state: what was asked for, what answered and what broke.

    ``skipped`` means nothing was asked of this source or nothing answered without a
    failure; ``failed`` means it was asked and answered nothing while reporting a
    problem; ``cached`` is a complete answer read from the archive rather than fetched.
    """
    if not enabled or (requested == 0 and not failed):
        status = "skipped"
    elif failed or (covered == 0 and issues):
        status = "failed"
    elif covered == 0:
        status = "skipped"
    elif covered < requested:
        status = "partial"
    else:
        status = "ok" if refresh else "cached"
    return {"status": status, "covered": covered, "requested": requested, "issues": issues}


def provider_status(bundle, config, *, refresh, interrupted=None):
    """Per-capability results derived from the bundle's own issues and receipts."""
    data = config.get("data", {})
    live = data.get("mode") == "live"
    issues = bundle.get("issues") or []
    identity = bundle.get("identity") or {}
    normalization = bundle.get("normalization") or {}
    ledger = bundle.get("ledger") or {}
    identified = sum(int(value) for value in identity.values())
    undecided = int(identity.get("ambiguous", 0)) + int(identity.get("unresolved", 0))
    rows = len(ledger.get("positions") or []) + len(ledger.get("accounts") or [])
    unconverted = int(normalization.get("unconverted_positions", 0)) + int(
        normalization.get("unconverted_accounts", 0)
    )
    prices = _distinct(bundle, "prices", "security_id")
    fundamentals = _distinct(bundle, "fundamentals", "security_id")
    macro = _distinct(bundle, "macro", "series_id")
    stopped = [FETCH_INTERRUPTED] if interrupted else []
    return {
        "listings": _capability(
            enabled=data.get("listing_provider", "yahoo") == "yahoo",
            refresh=refresh,
            covered=max(identified - undecided, 0),
            requested=identified,
            issues=_codes(issues, "LISTING_", "INVALID_LISTING_"),
        ),
        "fx": _capability(
            enabled=config.get("mandate", {}).get("base_currency") is not None,
            refresh=refresh,
            covered=max(rows - unconverted, 0),
            requested=rows,
            issues=_codes(issues, "FX_", "NO_PRESENTATION_CURRENCY"),
        ),
        "yahoo_prices": _capability(
            enabled=live and data.get("price_provider", "csv") == "yahoo",
            refresh=refresh,
            covered=prices,
            requested=_frame_rows(bundle, "securities"),
            issues=_codes(issues, "YAHOO_", "PRICES_") + (stopped if not prices else []),
            failed=bool(interrupted) and not prices,
        ),
        "sec": _capability(
            enabled=live and bool(data.get("sec_enabled")),
            refresh=refresh,
            covered=fundamentals,
            requested=_equities(bundle),
            issues=_codes(issues, "SEC_") + (stopped if not fundamentals else []),
            failed=bool(interrupted) and not fundamentals,
        ),
        "fred": _capability(
            enabled=live and bool(data.get("fred_enabled")),
            refresh=refresh,
            covered=macro,
            requested=len(data.get("fred_series") or []),
            issues=_codes(issues, "FRED_") + (stopped if not macro else []),
            failed=bool(interrupted) and not macro,
        ),
        **_research_status(bundle, refresh=refresh, interrupted=interrupted),
    }


def _research_status(bundle, *, refresh, interrupted=None):
    """Per-capability results the research adapter reported for this fetch.

    The coverage report already counted what each capability was asked for and what
    answered, per security; this states it in the same shape as every other provider so
    one stage record names every source that did not answer.
    """
    coverage = bundle.get("coverage") or {}
    capabilities = coverage.get("capabilities") or {}
    issues = bundle.get("issues") or []
    stopped = [FETCH_INTERRUPTED] if interrupted else []
    status = {}
    for name in ("prices", "statements", "estimates", "fund_disclosures", "events"):
        record = capabilities.get(name)
        if not isinstance(record, dict):
            continue
        covered = int(record.get("available") or 0)
        status["research_" + name] = _capability(
            enabled=True,
            refresh=refresh,
            covered=covered,
            requested=int(record.get("requested") or 0),
            issues=_codes(issues, "PROVIDER_", "INVALID_LISTING_")
            + (stopped if not covered else []),
            failed=bool(interrupted) and not covered,
        )
    return status


def _previous_review(store):
    """What prioritisation compares this review against, or ``None`` when there is none.

    Prioritisation explains what changed since the last run — a revised estimate, say —
    and reads the previous run's ``research`` and nothing else, so only that field is
    extracted. A saved run carries a brief per researched security; parsing all of it to
    reach one subtree would cost every review the size of the one before it.

    A first review, a missing archive, a damaged one, or a stage running without an
    archive at all costs that comparison and nothing else.
    """
    try:
        runs = store.list_runs()
        if not runs:
            return None
        research = store.load_run_part(runs[0]["run_id"], "research")
        return {"research": research} if isinstance(research, dict) else {"research": {}}
    except Exception:
        return None


def _priced_config(config, bundle, review_kind):
    """``(config for this review, accepted quantity×price difference)``.

    A current review is valued at the moment the collection was captured, while the
    price series ends at the last completed session, so quantity×close legitimately
    differs from the captured market value by more than a rounding step. The accepted
    difference is explicit, applies only to collected snapshots, and is recorded in the
    run's metadata rather than hidden in the check.
    """
    configured = float(config["allocation"]["valuation_tolerance"])
    positions = (bundle.get("ledger") or {}).get("positions") or []
    collected = any(
        isinstance(row, dict) and row.get("valuation_basis") == "collection_receipt"
        for row in positions
    )
    if review_kind != "current" or not collected:
        return config, configured
    tolerance = max(configured, COLLECTION_VALUATION_TOLERANCE)
    priced = deepcopy(config)
    priced["allocation"]["valuation_tolerance"] = tolerance
    return priced, tolerance


def _batch_record(companion, batch_id):
    """The collector's own publication receipt for this batch, or ``None``."""
    if not batch_id:
        return None
    try:
        with companion.store.connect() as connection:
            row = connection.execute(
                "SELECT status,completeness FROM import_batches WHERE id=?", (batch_id,)
            ).fetchone()
    except Exception:  # A receipt that cannot be read is reported as no receipt.
        return None
    return dict(row) if row else None


class ReviewWorkflow:
    """The five stages of one durable review operation, run on the service executor."""

    def __init__(self, service, workflow_id):
        record = service.store.workflow(workflow_id)
        self.service = service
        self.store = service.store
        self.workflow_id = workflow_id
        self.operation_key = record["operation_key"]
        self.review_kind = record["review_kind"]
        self.request = record["request"]
        self.stages = deepcopy(record["stages"]) or {
            stage: {"status": "pending"} for stage in STAGES
        }
        self.providers = deepcopy(record["providers"])
        self.config = None
        self.bundle = None
        self.result = None
        self.run_id = None
        self.refresh = False
        self.started = monotonic()
        self.probed = 0.0
        self.stop_seen = False
        self.reported = 0.0
        self.progress = None

    def run(self):
        """Run the stages once. Cancellation and failure both leave the last review."""
        stage = STAGES[0]
        try:
            self.store.update_workflow(self.workflow_id, status="running")
            for stage in STAGES:
                self._check_cancel(stage)
                getattr(self, "_" + stage)()
            self.store.update_workflow(
                self.workflow_id,
                status="complete",
                stage="publishing",
                stages=self.stages,
                providers=self.providers,
                run_id=self.run_id,
            )
        except _Cancelled:
            return
        except (ValueError, KeyError, TypeError, FileNotFoundError) as exc:
            self._failed(stage, _safe_text(str(exc)))
        except Exception:
            self._failed(stage, UNEXPECTED_FAILURE)

    # Durable stage records -------------------------------------------------

    def _save(self, **fields):
        self.store.update_workflow(
            self.workflow_id, stages=self.stages, providers=self.providers, **fields
        )

    def _begin(self, stage, message=None):
        self.stages[stage] = {
            "status": "running",
            "started_at": now(),
            "finished_at": None,
            "message": message,
            "action_needed": None,
            "detail": {},
        }
        self._save(status="running", stage=stage)

    def _end(self, stage, state, *, message=None, action_needed=None, detail=None, **fields):
        record = self.stages.get(stage) or {}
        self.stages[stage] = {
            "status": state,
            "started_at": record.get("started_at") or now(),
            "finished_at": now(),
            "message": message,
            "action_needed": action_needed,
            "detail": record.get("detail") or {} if detail is None else detail,
        }
        self._save(stage=stage, **fields)

    def _failed(self, stage, message):
        """Record the failure durably; a race with a cancellation never escapes the run."""
        try:
            if self.run_id:
                # The run is already in the archive. Reporting a failure here would tell
                # the owner the review did not happen while the page they load next opens
                # it, so the operation reports the degradation and keeps the review.
                self._end(
                    "publishing",
                    "complete",
                    message="Review saved.",
                    action_needed=PROSPECTIVE_STATUS,
                    detail={"run_id": self.run_id, "prospective_status": PROSPECTIVE_STATUS},
                    status="complete",
                    run_id=self.run_id,
                )
                return
            self._end(stage, "failed", message=message, status="failed", error=message)
        except ValueError:
            pass  # Already finished; the last usable review is unchanged either way.

    def _check_cancel(self, stage):
        """Stop before publishing when the owner asked to; nothing is written."""
        if not self._stopping() and not self.store.workflow(self.workflow_id)["cancel_requested"]:
            return
        self._end(stage, "cancelled", message=CANCELLED_MESSAGE, status="cancelled")
        raise _Cancelled

    def _stopping(self):
        """An application that is shutting down is a cancellation: nothing may publish."""
        stopping = getattr(self.service, "stopping", None)
        return bool(stopping is not None and stopping.is_set())

    def _pause(self):
        """One poll interval, cut short the moment the application asks to stop."""
        stopping = getattr(self.service, "stopping", None)
        if stopping is None:
            time.sleep(POLL_SECONDS)
            return
        stopping.wait(POLL_SECONDS)

    # Stages ----------------------------------------------------------------

    def _collecting(self):
        """Collect fresh holdings; a collection that cannot finish never stops the review."""
        try:
            self._collect()
        except _Cancelled:
            raise
        except Exception as exc:
            self._end(
                "collecting",
                "failed",
                message=_safe_text(str(exc)),
                action_needed=RETRY_ACCOUNTS,
            )

    def _collect(self):
        if self.request.get("collect") is False:
            self._end(
                "collecting",
                "skipped",
                message="Fresh collection was not requested; the last complete collection is used.",
            )
            return
        self._begin("collecting")
        companion = getattr(self.service, "companion", None)
        state = companion.status() if companion else None
        if not state or not state.get("browser_open"):
            self._end("collecting", "skipped", message=NOT_CONNECTED, action_needed=NOT_CONNECTED)
            return
        sources = [
            source
            for source in companion.store.sources()
            if source.get("selected") and source.get("url")
        ]
        if not sources:
            self._end("collecting", "skipped", message=NO_SELECTION, action_needed=NO_SELECTION)
            return
        if self._submit(companion, [source["id"] for source in sources]):
            self._await_collection(companion, len(sources))

    def _submit(self, companion, source_ids):
        """Start the pull, waiting out a pull that is already running."""
        deadline = monotonic() + BUSY_WAIT_SECONDS
        while True:
            self._check_cancel("collecting")
            try:
                companion.submit("refresh", source_ids)
                return True
            except ValueError as exc:
                if not companion.status().get("busy") or monotonic() > deadline:
                    self._end(
                        "collecting",
                        "failed",
                        message=_safe_text(str(exc)),
                        action_needed=RETRY_ACCOUNTS,
                    )
                    return False
            self._save(status="waiting", stage="collecting")
            self._pause()

    def _await_collection(self, companion, requested):
        """Poll the collection to its own end; its receipt decides what was published."""
        batch_id = companion.jobs[0].get("batch_id") if companion.jobs else None
        self._save(status="running", stage="collecting", batch_id=batch_id)
        deadline = monotonic() + COLLECT_TIMEOUT_SECONDS
        state = companion.status()
        while state.get("busy"):
            self._check_cancel("collecting")
            if monotonic() > deadline:
                detail = self._collection_detail(companion, batch_id, requested, state)
                self._end(
                    "collecting",
                    "failed",
                    message=COLLECT_TIMED_OUT,
                    action_needed=RETRY_ACCOUNTS,
                    detail=detail,
                )
                return
            self._pause()
            state = companion.status()
        detail = self._collection_detail(companion, batch_id, requested, state)
        if detail["batch_published"]:
            collected = sum(1 for row in detail["results"] if row["ok"])
            self._end(
                "collecting",
                "complete",
                message=f"Collected {collected} of {requested} portfolios.",
                detail=detail,
            )
            return
        names = ", ".join(row["name"] or "an account" for row in detail["results"] if not row["ok"])
        self._end(
            "collecting",
            "failed",
            message=f"These accounts did not collect: {names or 'no account answered'}.",
            action_needed=RETRY_ACCOUNTS,
            detail=detail,
        )

    def _collection_detail(self, companion, batch_id, requested, state):
        results = [
            {key: row.get(key) for key in ("source_id", "name", "ok", "unchanged", "error")}
            for row in state.get("results") or []
        ]
        receipt = _batch_record(companion, batch_id)
        published = (
            receipt["status"] == "published"
            if receipt
            else bool(results) and len(results) == requested and all(row["ok"] for row in results)
        )
        return {
            "batch_id": batch_id,
            "requested": requested,
            "results": results,
            "batch_published": bool(published),
            "completeness": (receipt or {}).get("completeness"),
        }

    def _resolving(self):
        """Read the collection and resolve identity and presentation, nothing more."""
        from portfolio_lab.pipeline import load_source_bundle

        self._begin("resolving")
        self.config = self.service.resolved_config()
        self.refresh = self._refresh()
        self.started = monotonic()
        self.bundle = load_source_bundle(
            self.config,
            self._as_of(),
            self.refresh,
            supplemental=self.store.latest("supplemental"),
            review_kind=self.review_kind,
            generated_at=now(),
        )
        collector = self.bundle.get("collector") or {}
        batch = collector.get("batch") or None
        exceptions = self.bundle.get("exceptions") or []
        detail = {
            "batch": batch,
            "collection_received_at": collector.get("collection_received_at"),
            "exceptions": len(exceptions),
            "identity": self.bundle.get("identity") or {},
        }
        self._end(
            "resolving",
            "complete",
            message="Holdings resolved.",
            action_needed=EXCEPTIONS_ACTION.format(count=len(exceptions)) if exceptions else None,
            detail=detail,
            batch_id=batch.get("id") if batch else None,
        )

    def _fetching(self):
        """Ask every configured provider once; a provider that fails is recorded, not fatal."""
        from portfolio_lab.pipeline import PRESENTATION_FX, enrich_inputs

        from .enrichment import fetch_controls

        self._begin("fetching")
        as_of = self.bundle["timeline"]["decision_date"]
        interrupted = None
        # Discovery and enrichment are one bounded fetch: the candidate screen is
        # acquired and qualified first, so the securities the review compares against
        # are researched by the same pass that researches the holdings.
        with fetch_controls(should_stop=self._fetch_stopping, progress=self._fetch_progress):
            self._acquire(as_of)
            try:
                self.bundle = enrich_inputs(self.bundle, self.config, as_of, self.refresh)
            except Exception as exc:  # One provider must never end the review.
                interrupted = _safe_text(str(exc))
                self.bundle = self._cached_only(as_of, PRESENTATION_FX)
        self.providers = provider_status(
            self.bundle, self.config, refresh=self.refresh, interrupted=interrupted
        )
        failed = sorted(
            name for name, record in self.providers.items() if record["status"] == "failed"
        )
        detail = {name: record["status"] for name, record in self.providers.items()}
        if self.progress is not None:
            detail["progress"] = dict(self.progress)
        self._end(
            "fetching",
            "complete",
            message="Provider data fetched."
            if not interrupted
            else "Some providers were unavailable; cached data was used.",
            action_needed=PROVIDER_ACTION.format(names=", ".join(failed)) if failed else None,
            detail=detail,
        )

    def _acquire(self, as_of):
        """Acquire and qualify the dated candidate screen; discovery is never fatal."""
        from .enrichment import acquire_candidates
        from .market_values import _issue

        try:
            acquire_candidates(self.bundle, self.config, as_of, refresh=self.refresh)
        except Exception as exc:
            _issue(
                self.bundle.setdefault("issues", []),
                "UNIVERSE_UNAVAILABLE",
                None,
                f"Candidate discovery failed ({_safe_text(str(exc))}); "
                "this review compares the securities it already knows.",
                "error",
            )

    def _fetch_stopping(self):
        """Whether the owner has asked this operation to stop, probed once a second.

        The probe answers rather than raises: a provider loop unwound by an exception
        would throw away the answers it already has. The fetch finishes with what it
        collected and the run's own cancellation check ends the operation before
        anything is published.

        A cancellation once seen is latched. A store that cannot answer — a lock, a row
        being rewritten — has told the review nothing, and "nothing" may not overwrite
        "the owner asked us to stop".
        """
        if self.stop_seen:
            return True
        now_seconds = monotonic()
        if now_seconds < self.probed + PROBE_SECONDS:
            return False
        self.probed = now_seconds
        try:
            self.stop_seen = bool(
                self._stopping() or self.store.workflow(self.workflow_id)["cancel_requested"]
            )
        except Exception:  # A store that cannot answer is not new information.
            pass
        return self.stop_seen

    def _fetch_progress(self, progress):
        """Publish how far the fetch has got, at most once every few seconds."""
        if not isinstance(progress, dict):
            return
        self.progress = {"done": progress.get("done"), "total": progress.get("total")}
        record = self.stages.get("fetching") or {}
        record["detail"] = {**(record.get("detail") or {}), "progress": dict(self.progress)}
        self.stages["fetching"] = record
        now_seconds = monotonic()
        finished = progress.get("done") == progress.get("total")
        if not finished and now_seconds < self.reported + PROGRESS_SECONDS:
            return
        self.reported = now_seconds
        try:
            self._save(stage="fetching")
        except Exception:  # A durable progress note is never worth losing the fetch.
            pass

    def _cached_only(self, as_of, presentation_fx):
        """Re-read the same inputs with connectors off after a provider raised."""
        from portfolio_lab.pipeline import enrich_inputs

        config = deepcopy(self.config)
        config["data"].update(price_provider="csv", sec_enabled=False, fred_enabled=False)
        try:
            return enrich_inputs(self.bundle, config, as_of, False)
        except Exception:  # Keep the resolved holdings; the analysis reports the gaps.
            return {key: value for key, value in self.bundle.items() if key != presentation_fx}

    def _analyzing(self):
        from portfolio_lab.pipeline import _settle_execution

        from .application import analyze_review

        self._begin("analyzing")
        config, tolerance = _priced_config(self.config, self.bundle, self.review_kind)
        result = analyze_review(
            self.bundle, config, previous=_previous_review(getattr(self, "store", None))
        )
        result["metadata"]["valuation_tolerance"] = tolerance
        _settle_execution(result, self.bundle, self.started)
        result["run_id"] = uuid.uuid4().hex
        result["metadata"]["workflow_id"] = self.workflow_id
        result["metadata"]["operation_key"] = self.operation_key
        self.result = result
        self._end(
            "analyzing",
            "complete",
            message="Analysis complete.",
            detail={"issues": result.get("quality_summary") or {}},
        )

    def _publishing(self):
        """One archive write; the baseline and outcome records never undo it."""
        from portfolio_lab.pipeline import save_analysis

        self._begin("publishing")
        saved = save_analysis(self.result, self.config, self.bundle)
        # The archive write is the commit point. Binding the run to the operation here
        # means everything after it is a degradation of a published review, never a
        # failure of one: research runs are immutable and this one now exists.
        self.run_id = saved["run_id"]
        self._save(run_id=self.run_id)
        degraded = False
        try:
            self.service._record_baselines(saved, self.config, self.bundle)
            self.service._evaluate_prior(saved, self.bundle)
        except Exception:  # Comparator records are secondary to the saved review.
            degraded = True
        self._end(
            "publishing",
            "complete",
            message="Review saved.",
            action_needed=PROSPECTIVE_STATUS if degraded else None,
            detail={
                "run_id": self.run_id,
                "prospective_status": PROSPECTIVE_STATUS if degraded else None,
            },
            run_id=self.run_id,
        )

    # Request contract ------------------------------------------------------

    def _refresh(self):
        """Live sources refresh by default; every other mode reads the cache only."""
        requested = self.request.get("refresh")
        if isinstance(requested, bool):
            return requested
        return self.config["data"]["mode"] == "live"

    def _as_of(self):
        """A historical review observes its month end; a current review has no date."""
        month = self.request.get("month")
        if self.review_kind != "historical" or not month:
            return None
        from .calendar import month_end_session

        return month_end_session(month)
