"""Versioned, read-only compatibility views over immutable saved review inputs."""

from copy import deepcopy

from portfolio_lab.analytics import json_safe
from portfolio_lab.config import validate_config

from .application import validate_workspace
from .benchmark import benchmark_view, prepare_benchmark
from .holding_analysis import METHOD_VERSION, build_holding_analysis

VIEW_VERSION = 2
BACKEND_VERSION = "portfolio-review-2"
CAPABILITIES = {
    "holding_analysis": 1,
    "legacy_holding_analysis_view": VIEW_VERSION,
    "active_valuation_model": 1,
    "benchmark_reference": 1,
}


def saved_holding_view(saved, bundle, config):
    """Add a missing holding view without changing the saved run or its evidence.

    This is a presentation adapter, not a new recommendation run. Existing outputs,
    timestamps, performance ledgers, workspace records and configuration remain intact.
    All calculations use the original bundle and never enter the provider pipeline.
    """
    result = deepcopy(saved)
    if (
        "holding_analysis" in result
        and (result.get("benchmark_reference") or {}).get("status") == "resolved"
    ):
        return result
    if (
        "holding_analysis" in result
        and (config.get("mandate") or {}).get("benchmark_id") is not None
    ):
        frozen = deepcopy(bundle)
        if isinstance(frozen, dict):
            _, reference = prepare_benchmark(frozen, config)
            ready = any(
                row.get("security_id") == reference.get("security_id")
                for row in result.get("forecast_inputs", [])
            )
            if reference["status"] != "resolved" or ready:
                result["benchmark_reference"] = benchmark_view(frozen, result)
                return json_safe(result)
    metadata = result.setdefault("metadata", {})
    view = {
        "version": VIEW_VERSION,
        "method_version": METHOD_VERSION,
        "status": "computed",
        "source": "frozen_saved_inputs",
        "base_run_id": saved.get("run_id"),
        "archive_unchanged": True,
        "comparison_basis": "Frozen evidence with the configured/default benchmark's editable shared-state assumptions.",
    }
    metadata.setdefault("computed_views", {})["holding_analysis"] = view
    try:
        if not isinstance(bundle, dict):
            raise ValueError("This review has no retained inputs.")
        frozen = deepcopy(bundle)
        used = validate_config(deepcopy(config))
        from .enrichment import _apply_shared_state

        _apply_shared_state(frozen, used, frozen["as_of"])
        used, _ = prepare_benchmark(frozen, used)
        sid = frozen["benchmark_reference"].get("security_id")
        benchmark_rows = frozen.get("forecasts")
        if hasattr(benchmark_rows, "empty") and not benchmark_rows.empty:
            selected = benchmark_rows[benchmark_rows.security_id == sid].to_dict("records")
            result["forecast_inputs"] = [
                row for row in result.get("forecast_inputs", []) if row.get("security_id") != sid
            ] + selected
        workspace = validate_workspace(frozen.get("workspace", {}))
        result["holding_analysis"] = build_holding_analysis(result, frozen, used, workspace)
        result["benchmark_reference"] = benchmark_view(frozen, result)
        view["benchmark_default_applied"] = result["benchmark_reference"].get(
            "default_applied", False
        )
    except (ValueError, TypeError, KeyError):
        # A legacy record that cannot be interpreted must not break every saved tab,
        # and its absence is not evidence of a particular holding's missing data.
        view.update(
            status="unavailable",
            reason="Update analysis to create a holding view from compatible inputs.",
        )
        result["holding_analysis"] = []
    return json_safe(result)
