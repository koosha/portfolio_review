"""What this month's evidence says to look at first, and why.

Nothing here is a recommendation and nothing is recomputed: every reason quotes an
input the run already produced, and a rule that lacks its inputs stays silent rather
than guessing. A holding is listed because a measurement moved, an expectation was
revised, a limit is exceeded or a fact is missing; an unowned company is listed because
its own complete evidence, or the owner's watchlist, asked for the comparison.

The list is ordered by how many independent reasons a security collected and then by
issuer size. It is not a composite ranking: a single ``priority_score`` is reported only
where every signal family is present, because a composite built over a missing family
would be a different measurement wearing the same name.
"""

from __future__ import annotations

import math
from copy import deepcopy
from typing import Any

METHOD_VERSION = "priorities-1"

# Rule thresholds. Each is a stated convention, not a tuned parameter.
TOP_DECILE_SCORE = 0.9
ESTIMATE_REVISION_THRESHOLD = 0.10
ESTIMATE_GROWTH_THRESHOLD = 0.15
DRAWDOWN_CONTRIBUTION_RANK = 3
SIGNAL_FAMILIES = ("quality", "value", "momentum")
INCOMPLETE_COVERAGE = {"missing", "stale"}
COVERAGE_CAPABILITIES = ("prices", "statements")
SCENARIO_LABELS = {"adverse": "Adverse", "central": "Central", "favorable": "Favorable"}
NEXT_YEAR_ESTIMATE = "eps_next_year_avg"
CURRENT_YEAR_ESTIMATE = "eps_0y_avg"
RANK_BASIS = (
    "Count of independent reasons, then issuer market capitalization; the composite "
    "signal score is reported only where every family is present and never orders this list. "
    "Any percentile below is a rank against the issuers this run actually scored — the "
    "holdings alone when discovery is off, the screened universe when it ran — and "
    "evidence.scored_issuers states that denominator for each item."
)

RULES = (
    {
        "rule": "below_retention_rank",
        "applies_to": "holding",
        "inputs": ["signals.score", "allocation.retention_quantile"],
        "detail": "The issuer's composite signal percentile sits below the retention quantile.",
    },
    {
        "rule": "concentration_over_cap",
        "applies_to": "holding",
        "inputs": ["issuer_exposure.weight", "mandate.issuer_cap"],
        "detail": "Household issuer weight exceeds the confirmed issuer cap.",
    },
    {
        "rule": "stale_or_missing_data",
        "applies_to": "holding",
        "inputs": ["coverage.by_security"],
        "detail": "Prices or statements were missing or stale for this security this run.",
    },
    {
        "rule": "large_estimate_revision",
        "applies_to": "holding",
        "inputs": ["research.brief.facts.eps_next_year_avg", "previous run's same fact"],
        "detail": "Consensus EPS for the next year moved beyond the revision threshold.",
    },
    {
        "rule": "proposed_central_return_negative",
        "applies_to": "holding",
        "inputs": ["research.proposals.eps.proposal_meta.verification.central_total_return"],
        "detail": "The proposed EPS model's central scenario returns less than zero.",
    },
    {
        "rule": "drawdown_warning",
        "applies_to": "holding",
        "inputs": ["risk.risk_contributions", "risk.stresses", "mandate.max_stress_loss"],
        "detail": "A top risk contributor while a mechanical stress exceeds the stated loss limit.",
    },
    {
        "rule": "unresolved_identity_or_currency",
        "applies_to": "holding",
        "inputs": ["coverage.capabilities.fx", "coverage.capabilities.listings"],
        "detail": "The listing or the currency conversion for this security is unresolved.",
    },
    {
        "rule": "top_decile_composite",
        "applies_to": "candidate",
        "inputs": ["signals.score", "signals.data_status"],
        "detail": "A complete composite score in the top decile of the scored universe.",
    },
    {
        "rule": "watchlist",
        "applies_to": "candidate",
        "inputs": ["signals.watchlist"],
        "detail": "The owner put this symbol on the watchlist.",
    },
    {
        "rule": "estimate_growth_high",
        "applies_to": "candidate",
        "inputs": [
            "signals.data_status",
            "research.brief.facts.eps_0y_avg",
            "research.brief.facts.eps_next_year_avg",
        ],
        "detail": "Consensus expects next-year EPS well above the current year, with a complete score.",
    },
)


def _number(value: Any) -> float | None:
    try:
        if isinstance(value, bool):
            return None
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _rows(frame: Any) -> list[dict]:
    """Records from a DataFrame, a list of dicts, or nothing at all."""
    if frame is None:
        return []
    if hasattr(frame, "to_dict") and hasattr(frame, "columns"):
        return [dict(row) for row in frame.to_dict("records")]
    if isinstance(frame, dict):
        return [dict(value) for value in frame.values() if isinstance(value, dict)]
    return [dict(row) for row in frame if isinstance(row, dict)]


def _percentiles(values: dict[str, float]) -> dict[str, float]:
    """Average-rank percentiles, matching the ranking the sleeve selection uses."""
    total = len(values)
    if not total:
        return {}
    ordered = list(values.values())
    ranks = {}
    for key, value in values.items():
        below = sum(1 for other in ordered if other < value)
        equal = sum(1 for other in ordered if other == value)
        ranks[key] = (below + (equal + 1) / 2) / total
    return ranks


def _reason(rule: str, detail: str, source_ids: list[str] | None = None) -> dict:
    seen: list[str] = []
    for source in source_ids or []:
        if source and source not in seen:
            seen.append(str(source))
    return {"rule": rule, "detail": detail, "source_ids": seen}


def _facts(research: dict, security_id: str) -> dict:
    """The brief's extracted numeric facts, keyed by field."""
    brief = (research.get(security_id) or {}).get("brief") or {}
    facts = {}
    for fact in brief.get("facts") or []:
        if isinstance(fact, dict) and fact.get("field") is not None:
            facts[str(fact["field"])] = fact
    return facts


def _fact_value(facts: dict, field: str) -> tuple[float | None, str | None]:
    fact = facts.get(field) or {}
    return _number(fact.get("value")), fact.get("source_id")


def _eps_proposal(research: dict, security_id: str) -> dict | None:
    proposals = (research.get(security_id) or {}).get("proposals") or {}
    eps = proposals.get("eps") if isinstance(proposals, dict) else None
    return eps if isinstance(eps, dict) else None


def _scenario_range(forecasts: dict[str, dict], research: dict, security_id: str) -> dict:
    """Three labelled returns from this run's forecast set, else from the EPS proposal."""
    stated = forecasts.get(security_id)
    if stated:
        return dict(stated)
    payload = _eps_proposal(research, security_id)
    if not payload:
        return {}
    price = _number(payload.get("starting_price"))
    if not price or price <= 0:
        return {}
    computed = {}
    for scenario in payload.get("scenarios") or []:
        if not isinstance(scenario, dict):
            continue
        label = SCENARIO_LABELS.get(str(scenario.get("label", "")).lower())
        eps = _number(scenario.get("eps"))
        multiple = _number(scenario.get("pe"))
        distributions = _number(scenario.get("distributions_per_starting_share")) or 0.0
        if label is None or eps is None or multiple is None:
            continue
        computed[label] = (eps * multiple + distributions) / price - 1
    return computed


def _forecast_ranges(result: dict) -> dict[str, dict]:
    ranges: dict[str, dict] = {}
    for row in _rows(result.get("forecast_inputs")):
        sid = row.get("security_id")
        value = _number(row.get("return_value"))
        label = str(row.get("scenario") or "")
        if sid is None or value is None or not label:
            continue
        ranges.setdefault(str(sid), {})[SCENARIO_LABELS.get(label.lower(), label)] = value
    return ranges


def _selected_candidate(result: dict) -> dict:
    allocation = result.get("allocation") or {}
    name = allocation.get("selected_candidate")
    for candidate in allocation.get("candidates") or []:
        if isinstance(candidate, dict) and candidate.get("candidate") == name:
            return candidate
    return {}


def _issue_codes(issues: list, security_id: str) -> list[str]:
    """Issue codes that name this security explicitly or open their message with it."""
    codes: list[str] = []
    for issue in issues or []:
        if not isinstance(issue, dict):
            continue
        message = str(issue.get("message") or "")
        named = issue.get("security_id") == security_id or message.startswith(f"{security_id}:")
        code = issue.get("code")
        if named and code and code not in codes:
            codes.append(str(code))
    return codes


def _families(signal: dict) -> list[str]:
    return sorted(family for family in SIGNAL_FAMILIES if _number(signal.get(family)) is not None)


def _holding_reasons(context: dict, item: dict) -> list[dict]:
    signal, sid = item["signal"], item["security_id"]
    config, result = context["config"], context["result"]
    reasons = []
    percentile = context["percentiles"].get(str(signal.get("issuer_id")))
    retention = _number(config.get("allocation", {}).get("retention_quantile"))
    if percentile is not None and retention is not None and percentile < retention:
        # The denominator belongs in the sentence: the same holding moves from the top
        # of a handful of issuers to the bottom of a thousand purely because discovery
        # ran, and a reader cannot check the reason without knowing which happened.
        scored = context["scored_issuers"]
        reasons.append(
            _reason(
                "below_retention_rank",
                f"issuer percentile {percentile:.2f} of {scored} scored issuer"
                f"{'' if scored == 1 else 's'} is below the retention quantile "
                f"{retention:.2f}",
            )
        )
    cap = _number(config.get("mandate", {}).get("issuer_cap"))
    weight = item["issuer_weight"]
    if cap is not None and weight is not None and weight > cap:
        reasons.append(
            _reason(
                "concentration_over_cap",
                f"issuer weight {weight:.2%} exceeds the {cap:.2%} issuer cap",
            )
        )
    incomplete = [
        name
        for name in COVERAGE_CAPABILITIES
        if str(item["coverage"].get(name) or "") in INCOMPLETE_COVERAGE
    ]
    if incomplete:
        reasons.append(
            _reason(
                "stale_or_missing_data",
                f"{', '.join(incomplete)} were missing or stale for this security",
            )
        )
    current, source = _fact_value(item["facts"], NEXT_YEAR_ESTIMATE)
    prior, prior_source = _fact_value(context["previous_facts"].get(sid, {}), NEXT_YEAR_ESTIMATE)
    if current is not None and prior:
        change = current / prior - 1
        if abs(change) > ESTIMATE_REVISION_THRESHOLD:
            reasons.append(
                _reason(
                    "large_estimate_revision",
                    f"consensus EPS for the next year moved {change:+.1%} since the previous review",
                    [source, prior_source],
                )
            )
    payload = _eps_proposal(context["research"], sid)
    verification = ((payload or {}).get("proposal_meta") or {}).get("verification") or {}
    central = _number(verification.get("central_total_return"))
    if central is not None and central < 0:
        evidence = ((payload or {}).get("proposal_meta") or {}).get("evidence") or {}
        reasons.append(
            _reason(
                "proposed_central_return_negative",
                f"the proposed EPS model's central scenario returns {central:+.1%}",
                list(evidence.values()),
            )
        )
    limit = _number(config.get("mandate", {}).get("max_stress_loss"))
    worst = context["worst_stress"]
    if limit is not None and worst is not None and sid in context["top_contributors"]:
        loss, scenario = worst
        if loss > limit:
            reasons.append(
                _reason(
                    "drawdown_warning",
                    f"a top-{DRAWDOWN_CONTRIBUTION_RANK} risk contributor while the "
                    f"{scenario} stress loses {loss:.1%} against a {limit:.1%} limit",
                )
            )
    unresolved = [
        name for name in ("fx", "listings") if sid in context["ledger_gaps"].get(name, ())
    ]
    if unresolved:
        reasons.append(
            _reason(
                "unresolved_identity_or_currency",
                f"{', '.join(unresolved)} remained unresolved for this security",
            )
        )
    del result
    return reasons


def _candidate_reasons(context: dict, item: dict) -> list[dict]:
    signal, sid = item["signal"], item["security_id"]
    complete = str(signal.get("data_status") or "") == "complete"
    score = _number(signal.get("score"))
    reasons = []
    if complete and score is not None and score >= TOP_DECILE_SCORE:
        reasons.append(
            _reason(
                "top_decile_composite",
                f"complete composite score of {score:.2f} at or above {TOP_DECILE_SCORE:.2f}",
            )
        )
    watchlist = {str(entry) for entry in context["config"].get("signals", {}).get("watchlist", [])}
    if sid in watchlist or str(signal.get("ticker") or "") in watchlist:
        reasons.append(_reason("watchlist", "the owner placed this symbol on the watchlist"))
    current, current_source = _fact_value(item["facts"], CURRENT_YEAR_ESTIMATE)
    following, following_source = _fact_value(item["facts"], NEXT_YEAR_ESTIMATE)
    if complete and current and current > 0 and following is not None:
        growth = following / current - 1
        if growth >= ESTIMATE_GROWTH_THRESHOLD:
            reasons.append(
                _reason(
                    "estimate_growth_high",
                    f"consensus expects {growth:+.1%} EPS growth into the next year",
                    [current_source, following_source],
                )
            )
    return reasons


def _item(context: dict, security_id: str, owned: bool) -> dict:
    signal = context["signals"].get(security_id, {})
    research = context["research"]
    coverage = (context["coverage"].get("by_security") or {}).get(security_id) or {}
    issuer_id = signal.get("issuer_id") or context["issuers"].get(security_id)
    issuer_weight = context["issuer_weights"].get(str(issuer_id))
    item = {
        "security_id": security_id,
        "signal": signal,
        "facts": _facts(research, security_id),
        "coverage": coverage,
        "issuer_weight": issuer_weight,
    }
    reasons = _holding_reasons(context, item) if owned else _candidate_reasons(context, item)
    if not reasons:
        return {}
    families = _families(signal)
    complete = str(signal.get("data_status") or "") == "complete"
    score = _number(signal.get("score"))
    current_value = context["values"].get(security_id, 0.0) if owned else 0.0
    denominator = context["denominator"]
    trades = context["trades"].get(security_id, [])
    proposed = None
    if denominator and trades:
        proposed = (current_value + sum(row["trade_value"] for row in trades)) / denominator
    cap = _number(context["config"].get("mandate", {}).get("issuer_cap"))
    name = ((research.get(security_id) or {}).get("brief") or {}).get("name") or context[
        "names"
    ].get(security_id)
    return {
        "security_id": security_id,
        "name": name,
        "kind": "holding" if owned else "candidate",
        "reasons": reasons,
        "evidence": {
            "issuer_id": str(issuer_id) if issuer_id is not None else None,
            "sector": signal.get("sector"),
            "market_cap": _number(signal.get("market_cap")),
            "score": score,
            "scored_issuers": context["scored_issuers"],
            "data_status": signal.get("data_status"),
            "families_present": families,
            "current_value": current_value if owned else None,
            "current_weight_basis": "reconciled_nav" if denominator else "unreconciled",
            "issuer_weight": issuer_weight,
            "coverage": dict(coverage),
        },
        "scenario_range": _scenario_range(context["forecasts"], research, security_id),
        "current_weight": (current_value / denominator) if denominator and owned else None,
        "proposed_weight": proposed,
        "risk_effect": {
            "contribution": context["contributions"].get(security_id),
            "concentration_vs_cap": (issuer_weight / cap)
            if issuer_weight is not None and cap
            else None,
        },
        "account_eligibility": context["eligibility"](security_id, signal),
        "funding_source": context["funding"](trades),
        "costs": {
            "execution_cost": sum(row["estimated_cost"] for row in trades),
            "tax_reserve": sum(row["estimated_tax"] for row in trades),
        }
        if trades
        else None,
        "unresolved_facts": _issue_codes(context["result"].get("issues"), security_id),
        "invalidation": deepcopy(
            ((research.get(security_id) or {}).get("brief") or {}).get("invalidation_conditions")
            or []
        ),
        "priority_score": score if complete and len(families) == len(SIGNAL_FAMILIES) else None,
        "rank_basis": RANK_BASIS,
    }


def _context(result: dict, bundle: dict, config: dict, previous: dict | None) -> dict:
    signals = {}
    issuer_scores: dict[str, float] = {}
    for row in _rows(result.get("signals")):
        sid = row.get("security_id")
        if sid is None:
            continue
        signals[str(sid)] = row
        score = _number(row.get("score"))
        if score is not None and row.get("issuer_id") is not None:
            issuer_scores[str(row["issuer_id"])] = score
    securities = {
        str(row["security_id"]): row
        for row in _rows(bundle.get("securities"))
        if row.get("security_id") is not None
    }
    values: dict[str, float] = {}
    for row in result.get("holdings") or []:
        value = _number(row.get("market_value"))
        if row.get("security_id") is not None and value is not None:
            values[str(row["security_id"])] = values.get(str(row["security_id"]), 0.0) + value
    summary = result.get("summary") or {}
    total = _number(summary.get("total_value"))
    denominator = total if summary.get("complete") and total else None
    risk = result.get("risk") or {}
    contributions = {
        str(row["security_id"]): _number(row.get("contribution"))
        for row in risk.get("risk_contributions") or []
        if isinstance(row, dict) and row.get("security_id") is not None
    }
    ordered = sorted(
        ((value, sid) for sid, value in contributions.items() if value is not None),
        key=lambda pair: (-pair[0], pair[1]),
    )
    losses = [
        (-_number(row.get("return")), str(row.get("scenario")))
        for row in risk.get("stresses") or []
        if isinstance(row, dict)
        and row.get("status") == "complete"
        and _number(row.get("return")) is not None
    ]
    coverage = result.get("coverage") or {}
    capabilities = coverage.get("capabilities") or {}
    selected = _selected_candidate(result)
    trades: dict[str, list[dict]] = {}
    for proposal in selected.get("proposals") or []:
        value = _number(proposal.get("trade_value"))
        sid = proposal.get("security_id")
        if sid is None or value is None:
            continue
        trades.setdefault(str(sid), []).append(
            {
                "account_id": proposal.get("account_id"),
                "trade_value": value,
                "estimated_cost": _number(proposal.get("estimated_cost")) or 0.0,
                "estimated_tax": _number(proposal.get("estimated_tax")) or 0.0,
            }
        )
    accounts = {
        str(row["account_id"]): row
        for row in selected.get("accounts") or []
        if isinstance(row, dict) and row.get("account_id") is not None
    }
    mandate = config.get("mandate", {})
    permissions = mandate.get("account_permissions") or {}
    policy = mandate.get("account_candidate_policy") or {}

    def eligibility(security_id: str, signal: dict) -> list[str]:
        allowed = {
            str(aid)
            for aid, ids in permissions.items()
            if security_id in {str(value) for value in ids or []}
        }
        if bool(signal.get("eligible")):
            allowed |= {str(aid) for aid, rule in policy.items() if rule == "eligible_universe"}
        return sorted(allowed)

    def funding(rows: list[dict]) -> dict | None:
        for row in sorted(rows, key=lambda entry: str(entry["account_id"])):
            account = accounts.get(str(row["account_id"]))
            if account is not None:
                return deepcopy(account)
        return None

    previous_research = (previous or {}).get("research") or {}
    return {
        "result": result,
        "config": config,
        "signals": signals,
        "percentiles": _percentiles(issuer_scores),
        "scored_issuers": len(issuer_scores),
        "research": result.get("research") or {},
        "previous_facts": {sid: _facts(previous_research, sid) for sid in previous_research},
        "coverage": coverage,
        "ledger_gaps": {
            name: {str(sid) for sid in (capabilities.get(name) or {}).get("missing") or []}
            for name in ("fx", "listings")
        },
        "issuer_weights": {
            str(row["issuer_id"]): _number(row.get("weight"))
            for row in result.get("issuer_exposure") or []
            if isinstance(row, dict) and row.get("issuer_id") is not None
        },
        "issuers": {sid: row.get("issuer_id") for sid, row in securities.items()},
        "names": {sid: row.get("name") for sid, row in securities.items()},
        "values": values,
        "denominator": denominator,
        "contributions": contributions,
        "top_contributors": {sid for _, sid in ordered[:DRAWDOWN_CONTRIBUTION_RANK]},
        "worst_stress": max(losses, key=lambda pair: pair[0]) if losses else None,
        "forecasts": _forecast_ranges(result),
        "trades": trades,
        "eligibility": eligibility,
        "funding": funding,
    }


def _ordered(items: list[dict]) -> list[dict]:
    return sorted(
        items,
        key=lambda item: (
            -len(item["reasons"]),
            -(item["evidence"]["market_cap"] or 0.0),
            item["security_id"],
        ),
    )


def review_priorities(
    result: dict, bundle: dict, config: dict, *, previous: dict | None = None
) -> dict:
    """Rank this run's holdings and unowned candidates by the reasons it can evidence.

    ``result`` and ``previous`` are saved run results, ``bundle`` the run's inputs.
    Pure: no input is modified and nothing is read from disk, a provider or the clock.
    """
    context = _context(result, bundle, config, previous)
    owned = set(context["values"]) | {
        sid for sid, row in context["signals"].items() if bool(row.get("owned"))
    }
    owned |= {
        str(row["security_id"])
        for row in _rows(bundle.get("securities"))
        if row.get("security_id") is not None and bool(row.get("owned"))
    }
    holdings, candidates = [], []
    for security_id in sorted(set(context["signals"]) | owned):
        is_owned = security_id in owned
        item = _item(context, security_id, is_owned)
        if item:
            (holdings if is_owned else candidates).append(item)
    return {
        "holdings_to_review": _ordered(holdings),
        "new_candidates": _ordered(candidates),
        "method_version": METHOD_VERSION,
        "rules": [dict(rule) for rule in RULES],
    }
