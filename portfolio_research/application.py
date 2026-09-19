"""Shared frozen-input application calculations for monthly jobs and previews."""

from copy import deepcopy

from portfolio_lab.analytics import analyze, json_safe
from portfolio_lab.metrics import _frame, _observed
from portfolio_lab.providers import FRAME_COLUMNS

from .calendar import decision_context

# Where a saved valuation came from: the owner typed it, it was prefilled from a
# proposal and left for review, or it arrived with an import. A proposal is never a
# reviewed assumption until the owner keeps it.
VALUATION_ORIGINS = {"manual", "prefill", "import"}
VALUATION_KEYS = {
    "eps",
    "dcf",
    "origin",
    "proposal_meta",
    "source",
    "version",
    "horizon_months",
}


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
            if key == "valuations" and value.get("origin") not in VALUATION_ORIGINS | {None}:
                raise ValueError(
                    "A company valuation origin must be " + ", ".join(sorted(VALUATION_ORIGINS))
                )
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


def analyze_review(bundle, config, *, previous=None):
    """The published review: engine outputs, the owner's workspace and what to look at.

    ``previous`` is the last published run's result when there is one. It is read only
    to explain what changed since then; nothing in this run depends on it existing.
    """
    from .evidence import validate_assessment
    from .readiness import readiness
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
    timeline = result["timeline"]
    result["metadata"]["review_kind"] = timeline.get("review_kind", "historical")
    result["metadata"]["information_cutoff"] = timeline.get(
        "information_cutoff", timeline["decision_cutoff"]
    )
    result["metadata"]["collection_received_at"] = timeline.get("collection_received_at")
    result["metadata"]["market_observation_date"] = timeline.get(
        "market_observation_date", timeline["decision_date"]
    )
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
        if not isinstance(values, dict) or set(values) - VALUATION_KEYS:
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
        for field in ("origin", "proposal_meta"):
            # A prefilled valuation keeps saying where it came from.
            if values.get(field) is not None:
                company[field] = deepcopy(values[field])
        result["company_research"][security_id] = company
    result["metadata"].update(product="Portfolio Review", method_version="portfolio-review-1")
    if "collector" in bundle:
        result["input_status"] = deepcopy(bundle["collector"])
    # Automatic research is reported beside the owner's reviewed workspace, never inside
    # it: briefs and proposals are proposed, and the requirements table says what each
    # output of this run actually has.
    if bundle.get("research"):
        result["research"] = deepcopy(bundle["research"])
    # What the review discovered, what it refused and why, stated beside the analysis:
    # the scope it acquired, the rows it excluded by name, and the holdings and
    # candidates whose evidence says look here first.
    if bundle.get("universe"):
        result["universe"] = deepcopy(bundle["universe"])
    if bundle.get("exclusions") is not None:
        result["exclusions"] = deepcopy(bundle["exclusions"])
    if bundle.get("research"):
        from .priorities import review_priorities

        result["priorities"] = review_priorities(result, bundle, config, previous=previous)
    result["readiness"] = readiness(result, bundle, config)
    return json_safe(result)
