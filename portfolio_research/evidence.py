"""Versioned, source-backed company research with explicit availability gates.

This module validates retained evidence, not the truth of an external document.
Manual review is explicit. Extracted numbers, self-declared calibration IDs and
model output never become validated forecasts through a flag in imported JSON.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from calendar import monthrange
from collections.abc import Mapping
from copy import deepcopy
from datetime import date, datetime, timezone
from typing import Any

METHOD_VERSION = "company-evidence-v1"
REQUIRED_SECTIONS = (
    "market_expectations",
    "thesis",
    "counter_thesis",
    "catalysts",
    "balance_sheet_risks",
    "invalidation_conditions",
)


def _json_value(value: Any, path: str = "assessment"):
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{path} contains a nonfinite number")
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{path} must have string object keys")
            _json_value(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _json_value(item, f"{path}[{index}]")
    elif value is not None and not isinstance(value, (str, int, float, bool)):
        raise ValueError(f"{path} must contain JSON-compatible values")


def _timestamp(value: Any, field: str, issues: list[str]):
    if value is None or value == "":
        issues.append(f"{field} is missing")
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO timestamp with timezone")
    if len(value) == 10:
        try:
            date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"{field} is not a valid ISO date") from exc
        issues.append(f"{field} has date-only precision; intraday availability is unknown")
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} is not a valid ISO timestamp") from exc
    if parsed.tzinfo is None:
        issues.append(f"{field} has no timezone; availability is unknown")
        return None
    return parsed.astimezone(timezone.utc)


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _records(data: Mapping, name: str) -> list:
    values = data.get(name, [])
    if not isinstance(values, list) or any(not isinstance(item, dict) for item in values):
        raise ValueError(f"{name} must be a list of objects")
    return values


def _find_source(sources: dict, source_id: Any):
    return sources.get(source_id) if isinstance(source_id, str) else None


def _catalyst_reasons(value: Any, cutoff: datetime | None, horizon: int | None) -> list[str]:
    # A narrative may explicitly record that no catalyst has been identified.
    # Identified events need dates so they cannot silently exceed the horizon.
    if _text(value):
        return []
    if not isinstance(value, list) or not value:
        return [
            "catalysts are missing; record dated events or an explicit no-known-items rationale"
        ]
    reasons = []
    endpoint = None
    if cutoff is not None and horizon is not None:
        month_index = cutoff.year * 12 + cutoff.month - 1 + horizon
        year, month = divmod(month_index, 12)
        endpoint = date(year, month + 1, min(cutoff.day, monthrange(year, month + 1)[1]))
    for catalyst in value:
        if not isinstance(catalyst, dict) or not _text(catalyst.get("description")):
            reasons.append("Each catalyst needs a description and target_date")
            continue
        try:
            target = date.fromisoformat(catalyst.get("target_date", ""))
        except (TypeError, ValueError) as exc:
            raise ValueError("catalyst.target_date must be an ISO calendar date") from exc
        if endpoint is None:
            reasons.append(
                "Catalyst horizon cannot be validated without decision cutoff and horizon"
            )
        elif not cutoff.date() <= target <= endpoint:
            reasons.append("Catalyst target_date falls outside the assessment horizon")
    return reasons


def _review_reasons(record: Mapping, name: str, generated: datetime | None) -> list[str]:
    issues = []
    if record.get("review_status") != "reviewed":
        issues.append(f"{name} has not been reviewed")
    if not _text(record.get("reviewed_by")):
        issues.append(f"{name}.reviewed_by is missing")
    reviewed = _timestamp(record.get("reviewed_at"), f"{name}.reviewed_at", issues)
    if reviewed is not None and generated is not None and reviewed > generated:
        issues.append(f"{name} was reviewed after the generation cutoff")
    return issues


def validate_assessment(
    payload: Mapping,
    *,
    decision_cutoff: str | None = None,
    generated_at: str | None = None,
    strict_receipt: bool = True,
) -> dict:
    """Validate a manually entered/imported assessment without changing its inputs.

    sources: id, locator, published_at, received_at, content_hash (SHA-256), version.
    facts: id, field, value, units, source_id, origin, review_status/by/at.
    operating_assumptions: field, value, units, horizon_months, basis, author, version,
    origin (manual/import/extracted/llm), and source_id for extracted/LLM assertions.
    """
    if not isinstance(payload, Mapping):
        raise ValueError("assessment must be an object")
    data = deepcopy(dict(payload))
    _json_value(data)
    if not isinstance(strict_receipt, bool):
        raise ValueError("strict_receipt must be boolean")
    issues: list[str] = []
    version = data.get("version")
    if version is None:
        issues.append("version is missing")
    elif isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ValueError("version must be a positive integer")
    if not _text(data.get("security_id")):
        issues.append("security_id is missing")
    if not _text(data.get("author")):
        issues.append("author is missing")
    horizon = data.get("horizon_months")
    if horizon is None:
        issues.append("horizon_months is missing")
    elif isinstance(horizon, bool) or horizon not in (6, 12, 18):
        raise ValueError("horizon_months must be 6, 12, or 18")
    else:
        horizon = int(horizon)
    cutoff_text = decision_cutoff if decision_cutoff is not None else data.get("decision_cutoff")
    generated_text = generated_at if generated_at is not None else data.get("generated_at")
    cutoff = _timestamp(cutoff_text, "decision_cutoff", issues)
    generated = _timestamp(generated_text, "generated_at", issues)
    if cutoff is not None and generated is not None and generated < cutoff:
        raise ValueError("generated_at cannot precede decision_cutoff")
    data["decision_cutoff"] = cutoff_text
    data["generated_at"] = generated_text
    data["strict_receipt"] = strict_receipt
    review_status = data.get("review_status", "draft")
    if review_status not in ("draft", "reviewed", "rejected", "needs_review"):
        raise ValueError("Invalid assessment review_status")
    source_by_id = {}
    source_results = []
    for source in _records(data, "sources"):
        source_id = source.get("id")
        if not _text(source_id) or source_id in source_by_id:
            raise ValueError("Each source must have a unique nonempty id")
        reasons = []
        if not _text(source.get("locator")):
            reasons.append(f"source {source_id}: locator is missing")
        if not _text(source.get("version")):
            reasons.append(f"source {source_id}: original source version is missing")
        digest = source.get("content_hash")
        if digest is None:
            reasons.append(f"source {source_id}: content_hash is missing")
        elif not isinstance(digest, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", digest):
            raise ValueError(f"source {source_id}: content_hash must be a SHA-256 hex digest")
        published = _timestamp(
            source.get("published_at"), f"source {source_id}.published_at", reasons
        )
        receipt_reasons = []
        received = _timestamp(
            source.get("received_at"), f"source {source_id}.received_at", receipt_reasons
        )
        if strict_receipt:
            reasons.extend(receipt_reasons)
        if cutoff is None:
            reasons.append(f"source {source_id}: decision cutoff is unavailable")
        elif published is not None and published > cutoff:
            reasons.append(f"source {source_id}: published after the decision cutoff")
        if strict_receipt and generated is None:
            reasons.append(f"source {source_id}: generation cutoff is unavailable")
        elif strict_receipt and received is not None and received > generated:
            reasons.append(f"source {source_id}: received after the generation cutoff")
        normalized = dict(source, eligible=not reasons, issues=reasons)
        source_by_id[source_id] = normalized
        source_results.append(normalized)
    fact_results = []
    fact_ids = set()
    for fact in _records(data, "facts"):
        fact_id = fact.get("id")
        if not _text(fact_id) or fact_id in fact_ids:
            raise ValueError("Each fact must have a unique nonempty id")
        fact_ids.add(fact_id)
        reasons = []
        for field in ("field", "units"):
            if not _text(fact.get(field)):
                reasons.append(f"fact {fact_id}: {field} is missing")
        if fact.get("value") is None:
            reasons.append(f"fact {fact_id}: value is missing")
        elif not isinstance(fact["value"], (str, int, float, bool)):
            raise ValueError(f"fact {fact_id}: value must be a scalar")
        source = _find_source(source_by_id, fact.get("source_id"))
        if source is None:
            reasons.append(f"fact {fact_id}: source_id does not identify a retained source")
        elif not source["eligible"]:
            reasons.extend(source["issues"])
        if fact.get("origin") not in ("manual", "import", "extracted", "llm"):
            reasons.append(f"fact {fact_id}: a supported origin is required")
        reasons.extend(_review_reasons(fact, f"fact {fact_id}", generated))
        fact_results.append(dict(fact, eligible=not reasons, issues=reasons))
    assumption_results = []
    for index, assumption in enumerate(_records(data, "operating_assumptions")):
        name = f"operating_assumption[{index}]"
        reasons = []
        for field in ("field", "units", "basis", "author"):
            if not _text(assumption.get(field)):
                reasons.append(f"{name}.{field} is missing")
        if assumption.get("value") is None:
            reasons.append(f"{name}.value is missing")
        if assumption.get("horizon_months") != horizon or horizon is None:
            reasons.append(f"{name}: horizon differs from this assessment")
        assumption_version = assumption.get("version")
        if (
            isinstance(assumption_version, bool)
            or not isinstance(assumption_version, int)
            or assumption_version < 1
        ):
            reasons.append(f"{name}.version must be a positive integer")
        origin = assumption.get("origin")
        if origin not in ("manual", "import", "extracted", "llm"):
            reasons.append(f"{name}: origin is missing or unsupported")
        if origin in ("extracted", "llm"):
            source = _find_source(source_by_id, assumption.get("source_id"))
            if source is None or not source["eligible"]:
                reasons.append(
                    f"{name}: extracted numbers require retained eligible source evidence"
                )
            reasons.extend(_review_reasons(assumption, name, generated))
        assumption_results.append(
            dict(
                assumption,
                eligible=not reasons,
                issues=reasons,
                forecast_type="subjective_assumption",
                calibrated=False,
            )
        )
    for section in REQUIRED_SECTIONS:
        value = data.get(section)
        if section == "catalysts":
            issues.extend(_catalyst_reasons(value, cutoff, horizon))
            continue
        if not (
            _text(value) or isinstance(value, list) and value and all(_text(item) for item in value)
        ):
            issues.append(
                f"{section} is missing; record the case or an explicit no-known-items rationale"
            )
    if not fact_results:
        issues.append("At least one retained, reviewed source-backed fact is required")
    if not assumption_results:
        issues.append("Explicit operating assumptions are required")
    issues.extend(reason for fact in fact_results for reason in fact["issues"])
    issues.extend(reason for assumption in assumption_results for reason in assumption["issues"])
    review_issues = _review_reasons(data, "assessment", generated)
    calibration_claim = bool(data.get("calibration_id") or data.get("calibrated"))
    # A document ID or imported 'calibrated=true' is never a calibration process.
    calibration_issues = (
        ["Calibration claims are unsupported here; a source-backed assessment remains subjective"]
        if calibration_claim
        else []
    )
    data["review_status"] = review_status
    canonical = json.dumps(
        data, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    ready = not issues and not review_issues
    return {
        "method_version": METHOD_VERSION,
        "assessment_id": f"assessment:{digest[:24]}",
        "content_hash": digest,
        "version": version,
        "status": "ready"
        if ready
        else "blocked"
        if issues or review_status == "rejected"
        else "draft",
        "assessment": data,
        "sources": source_results,
        "facts": fact_results,
        "eligible_facts": [fact for fact in fact_results if fact["eligible"]],
        "operating_assumptions": assumption_results,
        "can_supply_scenarios": ready,
        "issues": list(dict.fromkeys(issues + review_issues)),
        "calibrated": False,
        "forecast_type": "subjective_assessment",
        "calibration_issues": calibration_issues,
        "timing_policy": "public_and_received" if strict_receipt else "public_availability_only",
    }


def revise_assessment(previous: Mapping, changes: Mapping) -> dict:
    """Return a new subjective draft; the caller persists both versions immutably."""
    if not isinstance(changes, Mapping):
        raise ValueError("changes must be an object")
    prior = validate_assessment(previous)
    if prior["version"] is None:
        raise ValueError("Cannot revise an assessment without a version")
    draft = deepcopy(dict(previous))
    draft.update(deepcopy(dict(changes)))
    draft.update(
        version=prior["version"] + 1,
        parent_assessment_id=prior["assessment_id"],
        review_status="draft",
        calibrated=False,
    )
    for field in ("reviewed_by", "reviewed_at", "calibration_id", "calibration_record"):
        draft.pop(field, None)
    _json_value(draft)
    return draft
