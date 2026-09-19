"""Per-output requirements for one run.

Each row states what an output needs, whether this run has it, and what is missing.
Nothing is recomputed here: readiness reads the results and inputs the run already
produced, so a requirement is never satisfied by an assumption. An unavailable input
limits the outputs that depend on it and leaves the others usable.
"""

from __future__ import annotations

import math
from typing import Any

METHOD_VERSION = "readiness-1"
OUTPUTS = (
    "captured_holdings",
    "usd_value",
    "exposure",
    "risk",
    "stress",
    "company_valuation",
    "candidates",
    "baskets",
)
FUND_TYPES = {"etf", "mutual_fund", "plan_fund", "fund"}
NON_COMPANY = FUND_TYPES | {"currency", "cash"}
COVERED_STATUSES = {"ok", "stale", "not_applicable"}
NAMED_LIMIT = 5


def _number(value: Any) -> float | None:
    try:
        if isinstance(value, bool):
            return None
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _amount(value: float | None, currency: str) -> str:
    return "an unknown amount" if value is None else f"{value:,.2f} {currency}"


def _named(ids: list[str]) -> str:
    if not ids:
        return ""
    shown = sorted(ids)[:NAMED_LIMIT]
    suffix = "" if len(ids) <= NAMED_LIMIT else f" and {len(ids) - NAMED_LIMIT} more"
    return f"; missing: {', '.join(shown)}{suffix}"


def _requirement(name: str, met: Any, detail: str) -> dict:
    return {"name": name, "met": bool(met), "detail": detail}


def _row(output: str, requirements: list[dict], *, scope: str, blocked: bool = False) -> dict:
    missing = [r["name"] for r in requirements if not r["met"]]
    return {
        "output": output,
        "status": "ready" if not missing else ("blocked" if blocked else "partial"),
        "requirements": requirements,
        "missing": missing,
        "scope": scope,
        "method_version": METHOD_VERSION,
    }


def _ids(frame: Any, column: str = "security_id") -> set[str]:
    columns = getattr(frame, "columns", None)
    if columns is None or column not in columns:
        return set()
    return {str(value) for value in frame[column].dropna().unique()}


def _covered_value(summary: dict, holdings: list[dict], base: str) -> float:
    """The covered holdings subtotal the risk denominator is actually built from.

    ``metrics._covered_weights`` admits an amount only when it is stated in the base
    currency and is not negative; this figure describes that denominator, so it applies
    the same two filters. A foreign-currency account is named, never converted.
    """
    accounts = summary.get("accounts") or []
    if accounts:
        total = 0.0
        for account in accounts:
            if account.get("currency") != base:
                continue
            for field in ("known_position_value", "cash"):
                value = _number(account.get(field))
                if value is not None and value >= 0:
                    total += value
        return total
    return sum(
        value
        for row in holdings
        if row.get("currency") == base
        and (value := _number(row.get("market_value"))) is not None
        and value >= 0
    )


def _excluded_accounts(summary: dict, base: str) -> list[str]:
    """Accounts left out of the covered subtotal because they are in another currency."""
    return sorted(
        str(account.get("account_id"))
        for account in (summary.get("accounts") or [])
        if account.get("currency") != base
    )


def _denominator_scope(result: dict, summary: dict, base: str, covered: float) -> str:
    denominator = (result.get("risk") or {}).get("denominator")
    if isinstance(denominator, dict):
        value = _number(denominator.get("value"))
        return f"Covered holdings only ({_amount(value, denominator.get('currency') or base)})"
    if summary.get("complete"):
        return f"Reconciled NAV ({_amount(_number(summary.get('total_value')), base)})"
    return f"Covered holdings only ({_amount(covered, base)}); account NAV is not reconciled"


def _captured_holdings(summary: dict, holdings: list[dict]) -> dict:
    accounts = summary.get("accounts") or []
    dated = [row for row in holdings if _text(row.get("valuation_date"))]
    return _row(
        "captured_holdings",
        [
            _requirement(
                "accounts_captured",
                accounts,
                f"{len(accounts)} accounts captured" if accounts else "No accounts captured",
            ),
            _requirement(
                "positions_captured",
                holdings,
                f"{len(holdings)} positions captured" if holdings else "No positions captured",
            ),
            _requirement(
                "position_valuation_dates",
                holdings and len(dated) == len(holdings),
                f"{len(dated)} of {len(holdings)} positions carry a valuation date",
            ),
        ],
        scope=f"{len(holdings)} captured positions in {len(accounts)} accounts, as reported",
        blocked=not holdings,
    )


def _usd_value(summary: dict, holdings: list[dict], base: str, covered: float) -> dict:
    with_currency = [row for row in holdings if _text(row.get("currency"))]
    in_base = [row for row in holdings if row.get("currency") == base]
    unconverted = sorted({str(row.get("security_id")) for row in holdings if row not in in_base})
    complete = summary.get("complete") is True
    accounts = summary.get("accounts") or []
    reconciled = [a for a in accounts if a.get("complete")]
    excluded = _excluded_accounts(summary, base)
    scope = (
        f"Reconciled account NAV ({_amount(_number(summary.get('total_value')), base)})"
        if complete
        else f"Covered holdings subtotal ({_amount(covered, base)}) in {base}; not total NAV"
        + (f"; excluded for currency: {', '.join(excluded[:NAMED_LIMIT])}" if excluded else "")
    )
    return _row(
        "usd_value",
        [
            _requirement(
                "position_value_currencies",
                holdings and len(with_currency) == len(holdings),
                f"{len(with_currency)} of {len(holdings)} positions report a value currency",
            ),
            _requirement(
                "values_in_presentation_currency",
                holdings and len(in_base) == len(holdings),
                f"{len(in_base)} of {len(holdings)} positions are valued in {base}"
                + _named(unconverted),
            ),
            _requirement(
                "account_nav_reconciled",
                complete,
                f"{len(reconciled)} of {len(accounts)} accounts reconcile NAV, cash and positions",
            ),
        ],
        scope=scope,
        blocked=covered <= 0,
    )


def _exposure(result: dict, bundle: dict, summary: dict, holdings: list[dict], base: str) -> dict:
    issuers = result.get("issuer_exposure") or []
    classified = [
        row for row in holdings if _text(row.get("issuer_id")) and _text(row.get("sector"))
    ]
    funds = sorted(
        {
            str(row.get("security_id"))
            for row in holdings
            if str(row.get("instrument_type") or "").lower() in FUND_TYPES
        }
    )
    disclosed = _ids(bundle.get("fund_holdings"), "fund_id")
    undisclosed = [fund for fund in funds if fund not in disclosed]
    unclassified = _number(summary.get("unclassified_value"))
    return _row(
        "exposure",
        [
            _requirement(
                "issuer_and_sector_classified",
                holdings and len(classified) == len(holdings),
                f"{len(classified)} of {len(holdings)} positions carry an issuer and a sector",
            ),
            _requirement(
                "valued_holdings",
                issuers,
                f"{len(issuers)} issuers carry a value" if issuers else "No issuer exposure",
            ),
            _requirement(
                "fund_look_through",
                not undisclosed,
                f"{len(funds) - len(undisclosed)} of {len(funds)} held funds have disclosed "
                f"holdings" + _named(undisclosed)
                if funds
                else "No funds held",
            ),
            _requirement(
                "unclassified_residual_resolved",
                result.get("exposure_status") == "complete",
                f"Unclassified value {_amount(unclassified, base)}"
                if unclassified
                else f"Exposure status: {result.get('exposure_status') or 'not computed'}",
            ),
        ],
        scope=(
            f"Reconciled NAV weights ({_amount(_number(summary.get('total_value')), base)})"
            if summary.get("complete")
            else f"Covered holdings weights ({_amount(_covered_value(summary, holdings, base), base)}); "
            "unknown residual stays explicit"
        ),
        blocked=not issuers,
    )


def _risk(result: dict, config: dict, summary: dict, base: str, covered: float) -> dict:
    risk = result.get("risk") or {}
    required = int((config.get("risk") or {}).get("min_weekly_observations", 104) or 0)
    observations = int(_number(risk.get("observations")) or 0)
    contributions = risk.get("risk_contributions") or []
    benchmark = (config.get("mandate") or {}).get("benchmark_id")
    complete = summary.get("complete") is True
    return _row(
        "risk",
        [
            _requirement(
                "common_weekly_observations",
                observations >= required,
                f"{observations} of {required} required common weekly observations",
            ),
            _requirement(
                "portfolio_weights",
                contributions,
                f"{len(contributions)} securities carry a risk weight"
                if contributions
                else "No position weights were available",
            ),
            _requirement(
                "reconciled_denominator",
                complete,
                "Weights use reconciled account NAV"
                if complete
                else f"Weights use covered holdings value ({_amount(covered, base)}); "
                "account NAV is not reconciled",
            ),
            _requirement(
                "benchmark_history",
                not benchmark or risk.get("beta") is not None,
                f"Benchmark-relative risk against {benchmark}"
                if benchmark and risk.get("beta") is not None
                else (
                    "No benchmark configured"
                    if not benchmark
                    else f"No valid common history with {benchmark}"
                ),
            ),
        ],
        scope=_denominator_scope(result, summary, base, covered),
        blocked=risk.get("annualized_volatility") is None,
    )


def _stress(result: dict, summary: dict, base: str, covered: float) -> dict:
    stresses = (result.get("risk") or {}).get("stresses") or []
    complete = [s for s in stresses if s.get("status") == "complete"]
    return _row(
        "stress",
        [
            _requirement(
                "shock_assumptions",
                stresses,
                f"{len(stresses)} explicit mechanical shocks"
                if stresses
                else "No shocks were applied",
            ),
            _requirement(
                "complete_underlying_exposure",
                stresses and len(complete) == len(stresses),
                f"{len(complete)} of {len(stresses)} shocks have complete underlying exposure",
            ),
        ],
        scope=_denominator_scope(result, summary, base, covered),
        blocked=not complete,
    )


def _company_valuation(bundle: dict, holdings: list[dict]) -> dict:
    companies = sorted(
        {
            str(row.get("security_id"))
            for row in holdings
            if _text(row.get("security_id"))
            and str(row.get("instrument_type") or "").lower() not in NON_COMPANY
        }
    )
    coverage = (bundle.get("coverage") or {}).get("by_security") or {}
    research = bundle.get("research_inputs") or {}
    prices, statements = _ids(bundle.get("prices")), _ids(bundle.get("fundamentals"))

    def present(security_id: str, capability: str, fallback: bool) -> bool:
        reported = (coverage.get(security_id) or {}).get(capability)
        if reported is None:
            return fallback
        return reported in COVERED_STATUSES

    drivers = {
        "statements": [
            sid for sid in companies if not present(sid, "statements", sid in statements)
        ],
        "price": [sid for sid in companies if not present(sid, "prices", sid in prices)],
        "estimates": [
            sid
            for sid in companies
            if not present(sid, "estimates", bool((research.get(sid) or {}).get("estimates")))
        ],
    }
    requirements = [
        _requirement(
            name,
            companies and not missing,
            f"{len(companies) - len(missing)} of {len(companies)} held companies have {name}"
            + _named(missing)
            if companies
            else "No held company to value",
        )
        for name, missing in drivers.items()
    ]
    valuable = [
        sid for sid in companies if sid not in drivers["statements"] and sid not in drivers["price"]
    ]
    return _row(
        "company_valuation",
        requirements,
        scope=f"{len(companies)} held companies, each evaluated independently"
        if companies
        else "No held companies",
        blocked=not valuable,
    )


def _candidates(result: dict, holdings: list[dict]) -> dict:
    signals = [row for row in (result.get("signals") or []) if isinstance(row, dict)]
    held = {str(row.get("security_id")) for row in holdings}
    scored = [row for row in signals if row.get("data_status") == "complete"]
    eligible = [row for row in signals if row.get("eligible")]
    complete_eligible = [row for row in eligible if row.get("data_status") == "complete"]
    unowned = [row for row in scored if str(row.get("security_id")) not in held]
    scope_label = ((result.get("metadata") or {}).get("scope")) or "unstated universe"
    return _row(
        "candidates",
        [
            _requirement(
                "scored_universe",
                scored,
                f"{len(scored)} of {len(signals)} securities carry a complete composite score",
            ),
            _requirement(
                "unowned_candidates",
                unowned,
                f"{len(unowned)} scored securities are not currently held",
            ),
            _requirement(
                "complete_score_families",
                eligible and len(complete_eligible) == len(eligible),
                f"{len(complete_eligible)} of {len(eligible)} eligible securities have every "
                "score family",
            ),
        ],
        scope=f"Scored universe: {scope_label} ({len(signals)} securities); "
        "not a verified complete top-1,000 screen",
        blocked=not scored,
    )


def _baskets(result: dict, summary: dict) -> dict:
    allocation = result.get("allocation") or {}
    status = allocation.get("status")
    selected = next(
        (
            row
            for row in (allocation.get("comparison") or [])
            if row.get("candidate") == allocation.get("selected_candidate")
        ),
        {},
    )
    missing_rules = selected.get("missing_dealing_rules") or []
    tax_blocked = [
        issue
        for issue in (result.get("issues") or [])
        if issue.get("code") == "tax_analysis_unavailable"
    ]
    complete = summary.get("complete") is True
    return _row(
        "baskets",
        [
            _requirement(
                "reconciled_account_funding",
                complete,
                "Account funding is reconciled"
                if complete
                else "Hypothetical sizing needs reconciled account funding",
            ),
            _requirement(
                "allocation_not_blocked",
                status not in {None, "blocked"},
                f"Allocation status: {status or 'not run'}",
            ),
            _requirement(
                "dealing_rules_confirmed",
                not missing_rules,
                f"Dealing rules unconfirmed for {len(missing_rules)} instruments"
                if missing_rules
                else "Dealing rules confirmed for every proposed instrument",
            ),
            _requirement(
                "tax_inputs",
                not tax_blocked,
                "Tax inputs are available where required"
                if not tax_blocked
                else tax_blocked[0].get("message", "Tax inputs are unavailable"),
            ),
        ],
        scope="Hypothetical basket sizing only; no order is placed",
        blocked=not complete or status in {None, "blocked"},
    )


def readiness(result: dict, bundle: dict, config: dict) -> list[dict]:
    """What every output of this run needs, what it has, and what is missing."""
    result = result if isinstance(result, dict) else {}
    bundle = bundle if isinstance(bundle, dict) else {}
    config = config if isinstance(config, dict) else {}
    summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
    holdings = [row for row in (result.get("holdings") or []) if isinstance(row, dict)]
    base = summary.get("currency") or (config.get("mandate") or {}).get("base_currency") or "USD"
    covered = _covered_value(summary, holdings, base)
    return [
        _captured_holdings(summary, holdings),
        _usd_value(summary, holdings, base, covered),
        _exposure(result, bundle, summary, holdings, base),
        _risk(result, config, summary, base, covered),
        _stress(result, summary, base, covered),
        _company_valuation(bundle, holdings),
        _candidates(result, holdings),
        _baskets(result, summary),
    ]
