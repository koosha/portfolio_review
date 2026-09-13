"""Local application boundary: explicit run context, durable jobs and versioned inputs."""

import hashlib
import io
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
from .calendar import decision_context
from .public import _safe_text, public_config, public_result, public_workspace
from .repository import ResearchRepository

INPUT_KINDS = {"prices", "fundamentals", "macro", "fund_holdings", "forecasts", "universe"}


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
    def __init__(self, config, *, collector_busy=None, recover=False):
        self.config = validate_config(deepcopy(config))
        distinct_databases(self.config)
        self.store = ResearchRepository(self.config["research"]["path"])
        self.collector = is_collector(self.config["source"]["path"])
        self.collector_busy = collector_busy or (lambda: False)
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="portfolio-review")
        self.lock = threading.RLock()
        self.futures = {}
        if recover:
            self.store.recover_jobs()

    def close(self):
        self.executor.shutdown(wait=True, cancel_futures=False)

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
            "schema_version": 1,
        }

    def run(self, run_id):
        saved = self.store.load_run(run_id)
        bundle = self.store.load_bundle(run_id)
        return {
            "result": public_result(saved),
            "config": public_config(saved["saved_config"]),
            "workspace": public_workspace(bundle.get("workspace", {})),
        }

    def submit(self, payload):
        if not isinstance(payload, dict):
            raise ValueError("Send an object describing the research job.")
        kind = payload.get("kind")
        allowed = {
            "monthly": {"as_of", "refresh", "patch", "workspace"},
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
            if request.get("as_of"):
                decision_context(request["as_of"])
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
        run_id = str(payload.get("run_id", ""))
        result = self.store.load_run(run_id)
        candidate_id = payload.get("candidate_id")
        candidates = result.get("allocation", {}).get("candidates", [])
        if candidate_id is not None and candidate_id not in {
            row.get("candidate") for row in candidates
        }:
            raise ValueError("Choose a candidate belonging to this saved run.")
        rationale = payload.get("rationale")
        if not isinstance(rationale, str) or not rationale.strip() or len(rationale) > 10000:
            raise ValueError("Record a decision or no-action rationale.")
        action = payload.get("action", "no_action")
        if action not in {"no_action", "review_candidate", "override"}:
            raise ValueError("Unsupported research decision action.")
        data = {
            "action": action,
            "candidate_id": candidate_id,
            "rationale": rationale.strip(),
            "execution": "none",
        }
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
