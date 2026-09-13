"""Shared frozen-input application calculations for monthly jobs and previews."""

from copy import deepcopy

from portfolio_lab.analytics import analyze, json_safe
from portfolio_lab.metrics import _frame, _observed
from portfolio_lab.providers import FRAME_COLUMNS

from .calendar import decision_context


def validate_workspace(payload):
    if not isinstance(payload, dict) or set(payload) - {"assessments", "valuations"}:
        raise ValueError("Research workspace accepts assessments and valuations only.")
    for key in ("assessments", "valuations"):
        if not isinstance(payload.get(key, {}), dict):
            raise ValueError(f"workspace.{key} must map security IDs to objects.")
        if len(payload.get(key, {})) > 2000:
            raise ValueError("Too many company research records.")
        for sid, value in payload.get(key, {}).items():
            if not isinstance(sid, str) or not sid.strip() or not isinstance(value, dict):
                raise ValueError("Company records need nonempty security IDs and object values.")
            if key == "assessments" and value.get("security_id") != sid:
                raise ValueError("The assessment security ID must match its workspace identity.")
    return deepcopy(payload)


def validate_workspace_revision(workspace, previous):
    for sid, current in workspace.get("assessments", {}).items():
        prior = previous.get("assessments", {}).get(sid)
        if not prior or current == prior:
            continue
        if type(current.get("version")) is not int or current["version"] <= prior.get("version", 0):
            raise ValueError("An edited assessment needs a new version and a new explicit review.")
        if current.get("review_status") == "reviewed" and current.get("reviewed_at") == prior.get(
            "reviewed_at"
        ):
            raise ValueError("An edited assessment cannot reuse its previous review timestamp.")


def analyze_review(bundle, config):
    from .evidence import validate_assessment
    from .valuation import calculate_dcf, calculate_eps, dcf_sensitivity, eps_sensitivity

    workspace = validate_workspace(bundle.get("workspace", {}))
    result = analyze(bundle, config)
    result["observations"] = {}
    for name, date_column in (("fundamentals", "period_end"), ("fund_holdings", "holdings_date")):
        original = _frame(bundle, name)
        eligible = _observed(original, bundle, date_column, config=config)
        result["observations"][name] = {
            "rows": eligible[[column for column in FRAME_COLUMNS[name] if column in eligible]],
            "retained_count": len(original),
            "eligible_count": len(eligible),
            "scope": "Retained records eligible at the saved decision cutoff; units/currencies and source IDs accompany each observation.",
        }
    result["timeline"] = deepcopy(bundle.get("timeline") or decision_context(bundle["as_of"]))
    result["company_research"] = {}
    for security_id in sorted(
        set(workspace.get("assessments", {})) | set(workspace.get("valuations", {}))
    ):
        company = {"security_id": security_id, "basis": "subjective", "calibrated": False}
        if security_id in workspace.get("assessments", {}):
            company["assessment"] = validate_assessment(
                workspace["assessments"][security_id],
                decision_cutoff=result["timeline"]["decision_cutoff"],
                generated_at=result["timeline"]["generated_at"],
                strict_receipt=True,
            )
        values = workspace.get("valuations", {}).get(security_id, {})
        if not isinstance(values, dict) or set(values) - {
            "eps",
            "dcf",
            "origin",
            "source",
            "version",
            "horizon_months",
        }:
            raise ValueError(
                "Company valuations accept EPS, DCF and explicit source/version/horizon fields."
            )
        if "eps" in values:
            company["eps"] = calculate_eps(values["eps"])
            scenarios = company["eps"].get("scenarios", [])
            central = next(
                (row for row in scenarios if str(row.get("label", "")).lower() == "central"), {}
            )
            if (
                central.get("status") == "ready"
                and central.get("eps", 0) > 0
                and central.get("pe", 0) > 0
            ):
                company["eps_sensitivity"] = eps_sensitivity(
                    values["eps"],
                    [central["eps"] * f for f in (0.8, 0.9, 1, 1.1, 1.2)],
                    [central["pe"] * f for f in (0.8, 0.9, 1, 1.1, 1.2)],
                )
        if "dcf" in values:
            company["dcf"] = calculate_dcf(values["dcf"])
            discount = values["dcf"].get("discount_rate")
            growth = values["dcf"].get("terminal_growth_rate")
            if company["dcf"]["status"] == "ready" and discount is not None and growth is not None:
                company["dcf_sensitivity"] = dcf_sensitivity(
                    values["dcf"],
                    [discount + f for f in (-0.02, -0.01, 0, 0.01, 0.02) if discount + f > 0],
                    [growth + f for f in (-0.01, -0.005, 0, 0.005, 0.01)],
                )
        result["company_research"][security_id] = company
    result["metadata"].update(product="Portfolio Review", method_version="portfolio-review-1")
    if "collector" in bundle:
        result["input_status"] = deepcopy(bundle["collector"])
    return json_safe(result)
