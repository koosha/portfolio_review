"""This month's answer, per security: buy a share of available cash, sell part, or hold.

Every held security gets one row, and so do the unowned candidates worth initiating.

- **Buy shares** split whatever cash is available, from new money or from the sales
  recommended below. They sum to exactly 1 across all buys, whatever the amount.
- **Sell fractions** are the share of that position to sell. A sale is recommended only
  when the expected return over the review horizon is below the owner's floor; it grows
  linearly from the floor to a full exit at ``sell_full_return``.

The expected return comes from the strongest valuation the run holds: an adopted EPS or
DCF valuation first, then the automatic proposal. A row with no valuation evidence holds
and says so. Pure: nothing here reads a provider, the disk or the clock.
"""

from __future__ import annotations

import math

from .priorities import _number, _rows

METHOD_VERSION = "recommendations-1"
SELL_STEP = 0.05
SHARE_DECIMALS = 4
DEFAULT_POSITION_CAP = 0.10
INCOMPLETE_COVERAGE = {"missing", "stale"}
CONFIDENCE = ("low", "medium", "high")
BASIS_CONFIDENCE = {
    "adopted_eps_central": "high",
    "adopted_dcf": "high",
    "proposed_eps_central": "medium",
    "proposed_dcf": "medium",
    "none": "low",
}
RULES = (
    {
        "rule": "expected_return_below_floor",
        "detail": "Expected return is below the sell floor; the fraction sold grows to 100% at "
        "the full-sale return.",
    },
    {
        "rule": "expected_return_above_threshold",
        "detail": "Expected return clears the buy threshold and the composite score gate.",
    },
    {
        "rule": "within_hold_band",
        "detail": "Expected return sits between the sell floor and the buy threshold.",
    },
    {"rule": "at_position_cap", "detail": "Attractive, but already at the position cap."},
    {"rule": "below_buy_thresholds", "detail": "Not attractive enough to initiate."},
    {
        "rule": "no_expected_return_evidence",
        "detail": "No valuation evidence; hold until one is adopted.",
    },
    {"rule": "locked", "detail": "The mandate locks this security."},
    {
        "rule": "beyond_new_position_limit",
        "detail": "Qualifies, but more new positions qualified than allowed.",
    },
    {
        "rule": "stale_or_missing_data",
        "detail": "Prices or statements are missing or stale; confidence is lowered.",
    },
    {"rule": "watchlist", "detail": "On the owner's watchlist, so always listed."},
)


def _reason(rule: str, detail: str, source_ids: list[str] | None = None) -> dict:
    return {"rule": rule, "detail": detail, "source_ids": [s for s in source_ids or [] if s]}


def _pct(value: float) -> str:
    return f"{value * 100:+.1f}%"


def _price(sid: str, research: dict, holding: dict, signal: dict) -> float | None:
    dcf = ((research.get(sid) or {}).get("proposals") or {}).get("dcf") or {}
    for candidate in (dcf.get("price_major"), holding.get("price"), signal.get("price")):
        value = _number(candidate)
        if value is not None and value > 0:
            return value
    return None


def expected_return(sid: str, result: dict, holding: dict, signal: dict) -> tuple:
    """``(return, basis, source_ids)`` from the strongest valuation the run holds."""
    company = (result.get("company_research") or {}).get(sid) or {}
    research = result.get("research") or {}
    eps = company.get("eps") or {}
    central = next(
        (
            row
            for row in eps.get("scenarios") or []
            if isinstance(row, dict) and str(row.get("label", "")).lower() == "central"
        ),
        {},
    )
    value = _number(central.get("total_return"))
    if central.get("status") == "ready" and value is not None:
        return value, "adopted_eps_central", [f"workspace:eps:{sid}"]
    dcf = company.get("dcf") or {}
    per_share = _number(dcf.get("value_per_share"))
    price = _price(sid, research, holding, signal)
    if dcf.get("status") == "ready" and per_share is not None and price:
        return per_share / price - 1, "adopted_dcf", [f"workspace:dcf:{sid}"]
    proposals = (research.get(sid) or {}).get("proposals") or {}
    meta = (proposals.get("eps") or {}).get("proposal_meta") or {}
    verification = meta.get("verification") or {}
    value = _number(verification.get("central_total_return"))
    if verification.get("status") == "ready" and value is not None:
        return value, "proposed_eps_central", list((meta.get("evidence") or {}).values())[:3]
    dcf_meta = (proposals.get("dcf") or {}).get("proposal_meta") or {}
    value = _number(dcf_meta.get("implied_change_vs_price"))
    if value is not None:
        return value, "proposed_dcf", list((dcf_meta.get("evidence") or {}).values())[:3]
    return None, "none", []


def _confidence(basis: str, coverage: dict) -> tuple[str, bool]:
    level = CONFIDENCE.index(BASIS_CONFIDENCE[basis])
    gap = any(
        str(coverage.get(name) or "") in INCOMPLETE_COVERAGE for name in ("prices", "statements")
    )
    return CONFIDENCE[max(level - 1, 0) if gap else level], gap


def _sell_fraction(value: float, floor: float, full: float) -> float:
    if value <= full or floor == full:
        return 1.0
    raw = (floor - value) / (floor - full)
    stepped = math.ceil(round(raw / SELL_STEP, 9)) * SELL_STEP
    return round(min(max(stepped, SELL_STEP), 1.0), 4)


def _shares(weights: dict[str, float]) -> dict[str, float]:
    """Shares rounded to four places that sum to exactly one."""
    total = sum(weights.values())
    if not weights or total <= 0:
        return {}
    shares = {sid: round(w / total, SHARE_DECIMALS) for sid, w in weights.items()}
    largest = min(shares, key=lambda sid: (-shares[sid], sid))
    shares[largest] = round(shares[largest] + 1 - sum(shares.values()), SHARE_DECIMALS)
    return shares


def review_recommendations(result: dict, bundle: dict, config: dict) -> dict:
    """Recommend buy / sell / hold for every held security and the best new candidates."""
    review = dict(config.get("review") or {})
    mandate = config.get("mandate") or {}
    floor = float(review.get("sell_return_floor", 0.0))
    full = float(review.get("sell_full_return", -0.2))
    buy_return = float(review.get("buy_min_return", 0.08))
    buy_score = float(review.get("buy_min_score", 0.5))
    weighting = review.get("buy_weighting", "excess_return")
    cap = _number(review.get("max_position_weight"))
    cap = cap if cap is not None else _number(mandate.get("issuer_cap")) or DEFAULT_POSITION_CAP
    locked = {str(sid) for sid in mandate.get("locked_security_ids") or []}
    watchlist = {str(sid) for sid in (config.get("signals") or {}).get("watchlist") or []}
    summary = result.get("summary") or {}
    base = summary.get("currency") or mandate.get("base_currency") or "USD"
    research = result.get("research") or {}
    coverage = (result.get("coverage") or {}).get("by_security") or {}
    issues: list[dict] = []

    signals = {
        str(row["security_id"]): row
        for row in _rows(result.get("signals"))
        if row.get("security_id") is not None
    }
    securities = {
        str(row["security_id"]): row
        for row in _rows(bundle.get("securities"))
        if row.get("security_id") is not None
    }
    held: dict[str, dict] = {}
    unconverted = set()
    for row in _rows(result.get("holdings")):
        sid = row.get("security_id")
        if sid is None:
            continue
        sid = str(sid)
        entry = held.setdefault(sid, {"value": 0.0, "accounts": set(), "row": row})
        entry["accounts"].add(str(row.get("account_id")))
        value = _number(row.get("market_value"))
        currency = row.get("currency")
        if value is None:
            continue
        if currency not in (None, "", base):
            unconverted.add(sid)
            continue
        entry["value"] += value
    if unconverted:
        issues.append(
            {
                "code": "unconverted_positions",
                "message": f"{len(unconverted)} positions are not in {base} and are left out of "
                f"the weights: {', '.join(sorted(unconverted))}.",
            }
        )
    position_total = sum(entry["value"] for entry in held.values())
    total = _number(summary.get("total_value"))
    if summary.get("complete") and total:
        nav, nav_basis = total, "reconciled_nav"
    else:
        nav, nav_basis = position_total, "known_position_value"
        if held:
            issues.append(
                {
                    "code": "nav_not_reconciled",
                    "message": "Weights use the known position value because account NAV and "
                    "cash are not reconciled.",
                }
            )

    def name_of(sid: str, holding: dict, signal: dict) -> str | None:
        brief = (research.get(sid) or {}).get("brief") or {}
        return (
            holding.get("name")
            or brief.get("name")
            or signal.get("name")
            or (securities.get(sid) or {}).get("name")
        )

    def symbol_of(sid: str, holding: dict, signal: dict) -> str | None:
        return (
            holding.get("ticker")
            or signal.get("ticker")
            or (securities.get(sid) or {}).get("ticker")
        )

    def base_row(sid: str, holding: dict, signal: dict, value: float, accounts: list[str]):
        er, basis, sources = expected_return(sid, result, holding, signal)
        confidence, gap = _confidence(basis, coverage.get(sid) or {})
        score = _number(signal.get("score"))
        row = {
            "security_id": sid,
            "symbol": symbol_of(sid, holding, signal),
            "name": name_of(sid, holding, signal),
            "held": bool(accounts),
            "account_ids": accounts,
            "current_value": value if accounts else 0.0,
            "current_weight": (value / nav)
            if nav and accounts
            else (0.0 if not accounts else None),
            "action": "hold",
            "buy_share": None,
            "sell_fraction": None,
            "sell_amount": None,
            "expected_return": er,
            "expected_return_basis": basis,
            "score": score,
            "confidence": confidence,
            "reasons": [],
            "_sources": sources,
        }
        if gap:
            row["reasons"].append(
                _reason("stale_or_missing_data", "prices or statements are missing or stale")
            )
        return row

    def qualifies(row: dict) -> bool:
        er, score = row["expected_return"], row["score"]
        return er is not None and er >= buy_return and (score is None or score >= buy_score)

    def buy_reason(row: dict) -> dict:
        score = row["score"]
        detail = (
            f"expected return {_pct(row['expected_return'])} ({row['expected_return_basis']}) "
            f"clears the {_pct(buy_return)} buy threshold"
        )
        if score is not None:
            detail += f"; composite score {score:.2f}"
        return _reason("expected_return_above_threshold", detail, row["_sources"])

    rows: list[dict] = []
    for sid in sorted(held):
        entry = held[sid]
        row = base_row(
            sid, entry["row"], signals.get(sid, {}), entry["value"], sorted(entry["accounts"])
        )
        er = row["expected_return"]
        weight = row["current_weight"]
        if sid in locked:
            row["reasons"].insert(0, _reason("locked", "the mandate locks this security"))
        elif er is None:
            row["reasons"].insert(
                0,
                _reason(
                    "no_expected_return_evidence",
                    "no EPS or DCF valuation is available; adopt or import one to size this",
                ),
            )
        elif er < floor:
            fraction = _sell_fraction(er, floor, full)
            row.update(
                action="sell",
                sell_fraction=fraction,
                sell_amount=round(fraction * row["current_value"], 2),
            )
            row["reasons"].insert(
                0,
                _reason(
                    "expected_return_below_floor",
                    f"expected return {_pct(er)} ({row['expected_return_basis']}) is below the "
                    f"{_pct(floor)} floor; sell {fraction:.0%}",
                    row["_sources"],
                ),
            )
        elif qualifies(row) and weight is not None and weight >= cap:
            row["reasons"].insert(
                0,
                _reason(
                    "at_position_cap",
                    f"weight {weight:.1%} is at or above the {cap:.1%} position cap",
                ),
            )
        elif qualifies(row):
            row["action"] = "buy"
            row["reasons"].insert(0, buy_reason(row))
        else:
            detail = f"expected return {_pct(er)} is within the hold band"
            if er >= buy_return and row["score"] is not None:
                detail = f"composite score {row['score']:.2f} is below the {buy_score:.2f} gate"
            row["reasons"].insert(0, _reason("within_hold_band", detail))
        rows.append(row)

    considered = qualified = 0
    if review.get("include_new_candidates", True):
        pool = set()
        for item in _rows((result.get("priorities") or {}).get("new_candidates")):
            if item.get("security_id") is not None:
                pool.add(str(item["security_id"]))
        for sid, signal in signals.items():
            if signal.get("eligible") and signal.get("data_status") == "complete":
                pool.add(sid)
        for sid, row in securities.items():
            if sid in watchlist or str(row.get("ticker") or "") in watchlist:
                pool.add(sid)
        pool -= set(held)
        candidates = [base_row(sid, {}, signals.get(sid, {}), 0.0, []) for sid in sorted(pool)]
        considered = len(candidates)
        ranked = sorted(
            (row for row in candidates if qualifies(row) and row["security_id"] not in locked),
            key=lambda row: (-row["expected_return"], -(row["score"] or 0.0), row["security_id"]),
        )
        qualified = len(ranked)
        limit = int(review.get("max_new_positions", 3))
        chosen = {row["security_id"] for row in ranked[:limit]}
        for row in candidates:
            sid = row["security_id"]
            watched = sid in watchlist or str(row.get("symbol") or "") in watchlist
            if sid in chosen:
                row["action"] = "buy"
                row["reasons"].insert(0, buy_reason(row))
            elif not watched:
                continue
            elif sid in locked:
                row["reasons"].insert(0, _reason("locked", "the mandate locks this security"))
            elif qualifies(row):
                row["reasons"].insert(
                    0,
                    _reason(
                        "beyond_new_position_limit",
                        f"qualifies, but {qualified} candidates qualified for {limit} new positions",
                    ),
                )
            elif row["expected_return"] is None:
                row["reasons"].insert(
                    0,
                    _reason("no_expected_return_evidence", "no EPS or DCF valuation is available"),
                )
            else:
                row["reasons"].insert(
                    0,
                    _reason(
                        "below_buy_thresholds",
                        f"expected return {_pct(row['expected_return'])} does not clear the "
                        f"{_pct(buy_return)} buy threshold"
                        + (f" with score {row['score']:.2f}" if row["score"] is not None else ""),
                    ),
                )
            if watched:
                row["reasons"].append(_reason("watchlist", "on the owner's watchlist"))
            rows.append(row)

    buys = {row["security_id"]: row for row in rows if row["action"] == "buy"}
    weights = {
        sid: (row["expected_return"] - buy_return if weighting == "excess_return" else 1.0)
        for sid, row in buys.items()
    }
    if weighting == "excess_return" and buys and sum(weights.values()) <= 0:
        weights = dict.fromkeys(buys, 1.0)
    for sid, share in _shares(weights).items():
        buys[sid]["buy_share"] = share
    if not buys:
        issues.append(
            {
                "code": "no_buy_candidates",
                "message": "Nothing clears the buy threshold; hold any available cash.",
            }
        )
    for row in rows:
        row.pop("_sources", None)
    order = {"sell": 0, "buy": 1, "hold": 2}
    rows.sort(
        key=lambda row: (
            order[row["action"]],
            -(row["sell_amount"] or 0.0) if row["action"] == "sell" else 0.0,
            -(row["buy_share"] or 0.0),
            -(row["current_value"] or 0.0),
            row["security_id"],
        )
    )
    share_sum = round(sum(row["buy_share"] or 0.0 for row in rows), SHARE_DECIMALS)
    return {
        "method_version": METHOD_VERSION,
        "horizon_months": (config.get("allocation") or {}).get("horizon_months"),
        "currency": base,
        "nav": {"value": nav, "basis": nav_basis},
        "parameters": {**review, "max_position_weight": cap},
        "sale_proceeds": round(sum(row["sell_amount"] or 0.0 for row in rows), 2),
        "buy_share_sum": share_sum,
        "counts": {
            "buy": len(buys),
            "sell": sum(row["action"] == "sell" for row in rows),
            "hold": sum(row["action"] == "hold" for row in rows),
            "candidates_considered": considered,
            "candidates_qualified": qualified,
        },
        "issues": issues,
        "rules": [dict(rule) for rule in RULES],
        "rows": rows,
    }
