"""Monthly orchestration. Missing evidence is reported and never filled with demo data."""

from __future__ import annotations

import hashlib
import math
import platform
from datetime import date, datetime, timezone
from importlib.metadata import version
from pathlib import Path

import numpy as np
import pandas as pd

from . import __version__, metrics
from .allocation import build_proposals


def json_safe(value):
    if isinstance(value, pd.DataFrame):
        return json_safe(value.to_dict("records"))
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, np.ndarray)):
        return [json_safe(v) for v in value]
    if isinstance(value, (datetime, date, pd.Timestamp)):
        return value.isoformat()
    if isinstance(value, np.generic):
        return json_safe(value.item())
    if value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def code_hash():
    digest = hashlib.sha256()
    root = Path(__file__).parent.parent
    for directory in (root / "portfolio_lab", root / "portfolio_research"):
        for path in sorted(directory.rglob("*.py")):
            digest.update(str(path.relative_to(root)).encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def macro_panel(bundle, config):
    frame = bundle.get("macro", pd.DataFrame())
    result = []
    if frame.empty:
        return result
    from portfolio_research.calendar import bundle_cutoff, new_york_dates

    cutoff = bundle_cutoff(bundle)
    f = frame.copy()
    f["_date"] = pd.to_datetime(f["date"], utc=True, errors="coerce")
    f["_vintage"] = pd.to_datetime(f["vintage_date"], utc=True, errors="coerce")
    f["value"] = pd.to_numeric(f["value"], errors="coerce")
    # Bare dates start their New York day for the cutoff; ordering keeps the parsed values.
    observed = new_york_dates(f["date"], f["_date"]) <= cutoff
    vintaged = new_york_dates(f["vintage_date"], f["_vintage"]) <= cutoff
    f = f[observed & vintaged].dropna(subset=["value"])
    if config["data"]["require_received_by_cutoff"]:
        if "received_at" not in f:
            raise ValueError("Strict receipt policy requires macro received_at timestamps")
        received = pd.to_datetime(f.received_at, utc=True, errors="coerce")
        f = f[new_york_dates(f.received_at, received) <= cutoff]
    for sid, group in f.groupby("series_id"):
        group = group.sort_values(["_date", "_vintage"]).drop_duplicates("date", keep="last")
        latest = group.iloc[-1]
        points = group.tail(180)
        result.append(
            {
                "series_id": sid,
                "latest_value": latest.value,
                "observation_date": str(latest["date"])[:10],
                "vintage_date": str(latest["vintage_date"])[:10],
                "units": latest.get("units"),
                "change_from_previous_observation": float(
                    group.iloc[-1].value - group.iloc[-2].value
                )
                if len(group) > 1
                else None,
                "history": [
                    {"date": str(r["date"])[:10], "value": r["value"]} for _, r in points.iterrows()
                ],
                "interpretation": "Context indicator; no causal return forecast or automatic timing rule is inferred.",
            }
        )
    return json_safe(result)


def analyze(bundle: dict, config: dict) -> dict:
    issues = list(bundle.get("issues", []))
    result = {
        "metadata": {
            "as_of": bundle["as_of"],
            "valuation_date": bundle.get("valuation_date"),
            "mode": bundle.get("mode", config["data"]["mode"]),
            "scope": bundle.get("scope", "configured_universe"),
            "version": __version__,
            "code_hash": code_hash(),
            "python_version": platform.python_version(),
            "numerical_versions": {
                name: version(name) for name in ["numpy", "pandas", "scipy", "scikit-learn"]
            },
            "computed_at": datetime.now(timezone.utc).isoformat(),
            "point_in_time_policy": "available-by-cutoff and received-by-cutoff"
            if config["data"]["require_received_by_cutoff"]
            else "available-by-cutoff reconstruction; retrieval may be later",
            "purpose": "Conditional portfolio research; no trade execution",
        },
        "sources": bundle.get("sources", []),
        "issues": issues,
    }

    def stage(name, function, fallback):
        try:
            answer = function()
            if isinstance(answer, dict):
                issues.extend(answer.get("issues", []))
            return answer
        except Exception as exc:
            # A failed stage remains visible and blocks an allocation basket.
            issues.append(
                {
                    "severity": "error",
                    "code": f"{name}_failed",
                    "message": f"{name} failed: {type(exc).__name__}: {exc}",
                }
            )
            return fallback

    result["macro"] = stage("macro", lambda: macro_panel(bundle, config), [])
    reconciled = stage(
        "reconciliation",
        lambda: metrics.reconcile(bundle, config),
        {"summary": {"complete": False}, "holdings": []},
    )
    result.update(summary=reconciled["summary"], holdings=reconciled["holdings"])
    exposure = stage("exposures", lambda: metrics.exposures(bundle, config), {})
    result.update(
        issuer_exposure=exposure.get("issuer_exposure", []),
        sector_exposure=exposure.get("sector_exposure", []),
        exposure_status=exposure.get("status", "incomplete"),
    )
    scores = stage("signals", lambda: metrics.score_securities(bundle, config), pd.DataFrame())
    result["signals"] = json_safe(scores)
    result["risk"] = stage(
        "risk", lambda: metrics.risk_analysis(bundle, config), {"status": "incomplete"}
    )
    result["scenarios"] = stage(
        "scenarios", lambda: metrics.scenario_analysis(bundle, config), {"status": "incomplete"}
    )
    # Pass stage failures through to allocation; source ingestion issues are already present.
    allocator_bundle = dict(bundle)
    allocator_bundle["issues"] = list(issues)
    allocation = stage(
        "allocation",
        lambda: build_proposals(allocator_bundle, config, scores),
        {"status": "blocked", "proposals": [], "decisions": [], "comparison": [], "solver": {}},
    )
    result.update(
        allocation=allocation,
        proposals=allocation.get("proposals", []),
        decisions=allocation.get("decisions", []),
    )
    forecasts = bundle.get("forecasts", pd.DataFrame()).copy()
    if not forecasts.empty:
        from portfolio_research.calendar import new_york_dates

        parsed = pd.to_datetime(forecasts.forecast_date, utc=True, errors="coerce")
        dates = new_york_dates(forecasts.forecast_date, parsed)
        horizon = pd.to_numeric(forecasts.horizon_months, errors="coerce")
        forecasts = forecasts[
            (dates <= metrics._cutoff(bundle)) & (horizon == config["allocation"]["horizon_months"])
        ]
        if not forecasts.empty:
            latest = forecasts.groupby("security_id")["forecast_date"].transform("max")
            forecasts = forecasts[forecasts.forecast_date == latest]
    result["forecast_inputs"] = json_safe(forecasts)
    for forecast in result["forecast_inputs"]:
        sid, label = forecast["security_id"], forecast["scenario"]
        returns = config["allocation"].get("return_overrides", {}).get(sid, {})
        probabilities = config["allocation"].get("probability_overrides", {})
        if label in returns:
            forecast["return_value"] = returns[label]
        if label in probabilities:
            forecast["probability"] = probabilities[label]
        if not config["allocation"].get("use_probabilities", True):
            forecast["probability"] = None
        forecast["joint_validation_status"] = (
            "valid" if result["scenarios"].get("status") in {"complete", "partial"} else "invalid"
        )
        if returns or probabilities:
            forecast["basis"] = "subjective"
            forecast["calibration_id"] = None
            forecast["source"] = "Reviewed user scenario override; frozen with this run"
    seen = set()
    result["issues"] = []
    for issue in issues:
        key = (issue.get("severity"), issue.get("code"), issue.get("message"))
        if key not in seen:
            result["issues"].append(issue)
            seen.add(key)
    result["quality_summary"] = {
        severity: sum(i.get("severity") == severity for i in result["issues"])
        for severity in ("error", "warning", "info")
    }
    return json_safe(result)
