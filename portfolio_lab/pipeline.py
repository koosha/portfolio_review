"""Shared monthly/read, frozen replay and safe report export services."""

import html
import json
import os
from copy import deepcopy
from pathlib import Path
from time import monotonic

import pandas as pd

from .ingestion import ResearchStore, _open_source, _source_path, load_portfolio
from .providers import enrich_bundle


def is_collector(path):
    connection = _open_source(_source_path(path))
    try:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_schema WHERE type='table'")
        }
        return {"sources", "snapshots", "positions"} <= tables
    finally:
        connection.close()


def distinct_databases(config):
    source = Path(config["source"]["path"]).expanduser().resolve()
    research = Path(config["research"]["path"]).expanduser().resolve()
    if (
        source == research
        or source.exists()
        and research.exists()
        and os.path.samefile(source, research)
    ):
        raise ValueError("Source holdings and research must use different database files.")


def load_inputs(
    config,
    as_of=None,
    refresh=False,
    *,
    supplemental=None,
    account_ids=None,
    review_kind="historical",
    generated_at=None,
):
    """Load dated inputs for a historical month-end review or a current review.

    A current review takes the newest completed collection received by the generation
    instant and observes the last completed session; a historical review keeps the
    month-end contract and only accepts receipts through its decision date. The calendar
    owns the kind/date contract and its error messages.
    """
    from portfolio_research.calendar import review_context

    timeline = review_context(review_kind, as_of=as_of, generated_at=generated_at)
    distinct_databases(config)
    current = review_kind == "current"
    as_of = timeline["decision_date"]
    c = deepcopy(config)
    c["data"]["refresh_network"] = bool(refresh)
    if is_collector(c["source"]["path"]):
        from portfolio_research.adapter import load_collector

        bundle = load_collector(
            c["source"]["path"],
            as_of,
            supplemental,
            account_ids,
            receipt_through=timeline["requested_date"] if current else None,
            receipt_before=timeline["generated_at"] if current else None,
        )
    else:
        bundle = load_portfolio(c, as_of)
    received = bundle.get("collector", {}).get("collection_received_at")
    if received:
        timeline["collection_received_at"] = received
    bundle["timeline"] = timeline
    return enrich_bundle(bundle, c, as_of)


def save_analysis(result, config, bundle):
    distinct_databases(config)
    store = ResearchStore(config["research"]["path"])
    run_id = store.save_run(result, config, bundle)
    archived = store.load_run(run_id)
    try:
        export_report(archived, config["research"]["output_dir"])
    except (OSError, ValueError, TypeError):
        # The run is already committed. A retryable report failure must never
        # present it as a failed calculation or encourage another archive write.
        archived["metadata"]["export_status"] = "failed; retry Export from the saved run"
    return archived


def _settle_execution(result, bundle, started):
    """Move a current review's execution past the moment its result exists.

    Completion is measured on the generation clock (generation time plus monotonic
    elapsed time), so an explicitly supplied generation time stays self-consistent.
    """
    from portfolio_research.calendar import settle_execution

    timeline = bundle["timeline"]
    elapsed = pd.Timedelta(seconds=max(monotonic() - started, 0.0))
    completed = pd.Timestamp(timeline["generated_at"]) + elapsed
    settled = settle_execution(timeline, completed.isoformat())
    if settled == timeline:
        return
    bundle["timeline"] = settled
    result["timeline"] = deepcopy(settled)


def run_analysis(
    config,
    as_of=None,
    refresh=False,
    save=True,
    *,
    supplemental=None,
    workspace=None,
    review_kind="historical",
    generated_at=None,
):
    from portfolio_research.application import analyze_review

    started = monotonic()
    bundle = load_inputs(
        config,
        as_of,
        refresh,
        supplemental=supplemental,
        review_kind=review_kind,
        generated_at=generated_at,
    )
    bundle["workspace"] = deepcopy(workspace or {})
    result = analyze_review(bundle, config)
    _settle_execution(result, bundle, started)
    if save:
        result = save_analysis(result, config, bundle)
    return result, bundle


def replay_analysis(config, run_id, patch=None, save=False, *, workspace=None):
    from portfolio_research.application import (
        analyze_review,
        validate_workspace,
        validate_workspace_revision,
    )
    from portfolio_research.calendar import decision_context

    from .config import dashboard_patch

    distinct_databases(config)
    store = ResearchStore(config["research"]["path"])
    archived = store.load_run(run_id)
    frozen = store.load_bundle(run_id)
    used = dashboard_patch(archived["saved_config"], patch or {})
    used["research"] = deepcopy(config["research"])
    used["source"] = deepcopy(config["source"])
    used["data"]["refresh_network"] = False
    if workspace is not None:
        workspace = validate_workspace(workspace)
        validate_workspace_revision(workspace, frozen.get("workspace", {}))
        frozen["workspace"] = deepcopy(workspace)
    timeline = deepcopy(frozen.get("timeline") or decision_context(frozen["as_of"]))
    timeline.setdefault("original_generated_at", timeline["generated_at"])
    timeline["generated_at"] = decision_context(frozen["as_of"])["generated_at"]
    timeline["frozen_input_replay"] = True
    frozen["timeline"] = timeline
    result = analyze_review(frozen, used)
    result["metadata"].update(parent_run_id=run_id, preview=not save)
    if save:
        result = save_analysis(result, used, frozen)
    return result, frozen, used


def report_html(result):
    from portfolio_research.public import public_result

    safe = public_result(result)

    def esc(value):
        return html.escape("Unavailable" if value is None else str(value))

    def table(rows):
        if not rows:
            return "<p>Unavailable: no observations in this run.</p>"
        columns = list(dict.fromkeys(key for row in rows for key in row))
        head = "".join(f"<th>{esc(key.replace('_', ' '))}</th>" for key in columns)
        body = "".join(
            "<tr>"
            + "".join(
                f"<td>{esc(json.dumps(row.get(key), ensure_ascii=False) if isinstance(row.get(key), (dict, list)) else row.get(key))}</td>"
                for key in columns
            )
            + "</tr>"
            for row in rows
        )
        return f"<div class='scroll'><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>"

    metadata = safe.get("metadata", {})
    parts = [
        "<!doctype html><html lang='en'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Portfolio Review · saved report</title><style>body{font:15px system-ui;color:#18312e;background:#f7f8f5;max-width:1280px;margin:40px auto;padding:0 24px}h1{font-size:32px}h2{margin-top:40px}table{border-collapse:collapse;width:100%;background:white;font-size:13px}th,td{padding:10px;text-align:left;border-bottom:1px solid #d9e1dc;vertical-align:top}th{white-space:nowrap}.scroll{overflow:auto}pre{white-space:pre-wrap;overflow-wrap:anywhere}.muted{color:#536960}</style><h1>Portfolio Review</h1>"
    ]
    parts.append(
        f"<p>{esc(metadata.get('mode', 'offline')).upper()} · Decision {esc(metadata.get('as_of'))} · Run {esc(safe.get('run_id'))}</p>"
    )
    parts.append(
        "<p class='muted'>Read-only saved research. Recalculation and editing are available in the local application. Historical illustrations and subjective scenarios do not establish actual portfolio performance.</p>"
    )
    for title, rows in [
        ("Account reconciliation", safe.get("summary", {}).get("accounts", [])),
        ("Holdings", safe.get("holdings", [])),
        ("Issuer exposure", safe.get("issuer_exposure", [])),
        ("Sector exposure", safe.get("sector_exposure", [])),
        ("Company screen", safe.get("signals", [])),
        ("Joint scenarios", safe.get("scenarios", {}).get("scenarios", [])),
        ("Candidate comparison", safe.get("allocation", {}).get("comparison", [])),
        ("Selected basket", safe.get("proposals", [])),
        ("Decisions and no-action reasons", safe.get("decisions", [])),
        ("Data issues", safe.get("issues", [])),
    ]:
        parts.append(f"<h2>{esc(title)}</h2>{table(rows)}")
    for title, value in [
        ("Company evidence and valuation", safe.get("company_research", {})),
        ("Risk and assumptions", safe.get("risk", {})),
        ("Candidate ledgers", safe.get("allocation", {}).get("candidates", [])),
    ]:
        parts.append(
            f"<h2>{esc(title)}</h2><pre>{esc(json.dumps(value, indent=2, ensure_ascii=False))}</pre>"
        )
    parts.append("</html>")
    return "".join(parts)


def export_report(result, directory):
    from portfolio_research.public import public_result

    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    run_id = str(result["run_id"])
    if not run_id.isalnum() or len(run_id) > 64:
        raise ValueError("Invalid report run identifier.")
    (target / f"{run_id}.json").write_text(
        json.dumps(public_result(result), indent=2, allow_nan=False) + "\n"
    )
    page = report_html(result)
    (target / f"{run_id}.html").write_text(page)
    (target / f"{run_id}_dashboard.html").write_text(page)
    return target / f"{run_id}.html"


def export_dashboard_snapshot(result, path):
    Path(path).write_text(report_html(result))
