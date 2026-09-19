"""Pure accounting normalization for provider statements.

Trailing-twelve-month windows, restatement vintages, per-share arithmetic, statement
share-unit reconciliation and split-adjusted dividends. Nothing here reads the network
or the cache; every function takes supplied records and returns new records. Missing
inputs stay missing: no field is ever zero-filled to complete a window.
"""

from __future__ import annotations

import math
from datetime import date

METHOD_VERSION = "statement-normalization-1"
# Flows accumulate across the four quarters; balances are instants.
FLOW_FIELDS = (
    "revenue",
    "gross_profit",
    "operating_income",
    "net_income",
    "income_common",
    "operating_cash_flow",
    "capex",
)
BALANCE_FIELDS = ("assets", "debt", "cash")
TTM_QUARTERS = 4
# Calendar quarters end 89-92 days apart; a longer step means a period is absent.
MAX_QUARTER_GAP_DAYS = 100
# Statement share counts are often printed in thousands or millions.
SHARE_UNIT_FACTORS = (
    (1.0, "same_units"),
    (1e3, "statement_in_thousands"),
    (1e6, "statement_in_millions"),
)
SHARE_UNIT_TOLERANCE = 0.2


def _issue(issues, code, security_id, detail):
    if issues is not None:
        issues.append(
            {
                "code": code,
                "security_id": security_id,
                "detail": detail,
                "severity": "warning",
                "message": f"{security_id}: {detail}" if security_id else detail,
            }
        )


def _number(value):
    """A finite float, or ``None`` for anything that is not a usable number."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _date(value, name):
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError as exc:
            raise ValueError(f"{name} must be an ISO date: {value!r}") from exc
    if isinstance(value, date):
        return value
    raise ValueError(f"{name} must be an ISO date: {value!r}")


def _received(row):
    value = row.get("received_at")
    return value if isinstance(value, str) else ""


def latest_vintage(rows) -> list[dict]:
    """One row per ``(security_id, period_end)``: the newest ``received_at`` wins.

    Restatements arrive as a second row for a period already reported. The earlier
    vintage is not discarded by the caller — it belongs in ``fundamentals_history`` —
    but engines read one figure per period, and that figure is the latest one.
    """
    if not isinstance(rows, list):
        raise ValueError("Statement rows must be supplied as a list")
    chosen: dict[tuple, dict] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Each statement row must be an object")
        key = (row.get("security_id"), str(row.get("period_end")))
        current = chosen.get(key)
        if current is None or _received(row) >= _received(current):
            chosen[key] = dict(row)
    return list(chosen.values())


def ttm_from_quarters(quarters, *, issues=None, security_id=None) -> dict | None:
    """Sum the four most recent distinct quarters into one trailing-twelve-month record.

    Balance items come from the latest quarter and ``assets_begin`` from the quarter four
    periods earlier (the start of the same window), when that quarter is supplied. A field
    absent from any of the four quarters is missing from the window rather than summed as
    a partial year. Fewer than four quarters, or a gap longer than one quarter, produces
    ``None`` and a reason on ``issues``.
    """
    if not isinstance(quarters, list):
        raise ValueError("Quarterly statements must be supplied as a list")
    rows = latest_vintage(quarters)
    for row in rows:
        row["_period_end"] = _date(row.get("period_end"), "period_end")
    rows.sort(key=lambda row: row["_period_end"], reverse=True)
    if len(rows) < TTM_QUARTERS:
        _issue(
            issues,
            "TTM_INSUFFICIENT_QUARTERS",
            security_id,
            f"{len(rows)} distinct quarters supplied; a TTM window needs 4.",
        )
        return None
    window = rows[:TTM_QUARTERS]
    for newer, older in zip(window, window[1:]):
        gap = (newer["_period_end"] - older["_period_end"]).days
        if gap > MAX_QUARTER_GAP_DAYS:
            _issue(
                issues,
                "TTM_QUARTER_GAP",
                security_id,
                f"{gap} days between {older['period_end']} and {newer['period_end']}; "
                "a quarter is absent from the window.",
            )
            return None
    latest = window[0]
    missing: list[str] = []
    result: dict = {
        "period_end": latest["_period_end"].isoformat(),
        "period_type": "ttm",
        "basis": "ttm_from_quarters",
        "method_version": METHOD_VERSION,
        "quarters_used": [row["_period_end"].isoformat() for row in window],
    }
    for field in FLOW_FIELDS:
        values = [_number(row.get(field)) for row in window]
        if any(value is None for value in values):
            result[field] = None
            missing.append(field)
        else:
            result[field] = sum(values)
    for field in BALANCE_FIELDS:
        result[field] = _number(latest.get(field))
        if result[field] is None:
            missing.append(field)
    begin = rows[TTM_QUARTERS] if len(rows) > TTM_QUARTERS else None
    if begin is not None:
        gap = (window[-1]["_period_end"] - begin["_period_end"]).days
        begin = begin if gap <= MAX_QUARTER_GAP_DAYS else None
    result["assets_begin"] = _number(begin.get("assets")) if begin else None
    result["assets_begin_period_end"] = begin["_period_end"].isoformat() if begin else None
    if result["assets_begin"] is None:
        missing.append("assets_begin")
    result["missing"] = missing
    return result


def per_share(value, shares) -> float | None:
    """``value`` divided by a positive share count, or ``None`` when either is unusable."""
    amount, count = _number(value), _number(shares)
    if amount is None or count is None or count <= 0:
        return None
    return amount / count


def share_units(statement_shares, listing_shares_outstanding) -> dict:
    """Reconcile a statement share count with the listing's shares outstanding.

    Returns ``{"factor", "basis", "reason"}``. ``factor`` multiplies the statement figure
    to reach actual shares (1, 1e3 or 1e6). A ratio that no standard scaling explains is
    refused with a reason rather than guessed at.
    """
    statement, listing = _number(statement_shares), _number(listing_shares_outstanding)
    if statement is None or statement <= 0:
        return {
            "factor": None,
            "basis": None,
            "reason": "Statement share count is unavailable or not positive.",
        }
    if listing is None or listing <= 0:
        return {
            "factor": None,
            "basis": None,
            "reason": "Listing shares outstanding are unavailable; scaling cannot be checked.",
        }
    ratio = listing / statement
    for factor, basis in SHARE_UNIT_FACTORS:
        if abs(ratio / factor - 1) <= SHARE_UNIT_TOLERANCE:
            return {"factor": factor, "basis": basis, "reason": None}
    spread = max(ratio, 1 / ratio)
    return {
        "factor": None,
        "basis": None,
        "reason": (
            f"Statement shares {statement:g} and listing shares {listing:g} differ by a "
            f"factor of {spread:.1f}; no thousand or million scaling explains it."
        ),
    }


def split_adjust_dividends(dividends_rows, splits_rows) -> list[dict]:
    """Restate per-share dividends in current share terms.

    A dividend paid before an n-for-1 split is worth ``value / n`` per current share.
    Ratios compound across successive splits; rows keep their original ``raw_value``.
    """
    if not isinstance(dividends_rows, list) or not isinstance(splits_rows, list):
        raise ValueError("Dividend and split rows must be supplied as lists")
    splits = []
    for row in splits_rows:
        if not isinstance(row, dict):
            raise ValueError("Each split row must be an object")
        ratio = _number(row.get("value", row.get("ratio")))
        if ratio is None or ratio <= 0:
            raise ValueError("A split ratio must be a positive number")
        splits.append((_date(row.get("date"), "split date"), ratio))
    adjusted = []
    for row in dividends_rows:
        if not isinstance(row, dict):
            raise ValueError("Each dividend row must be an object")
        day = _date(row.get("date"), "dividend date")
        factor = 1.0
        for split_day, ratio in splits:
            if split_day > day:
                factor *= ratio
        value = _number(row.get("value"))
        adjusted.append(
            {
                **row,
                "date": day.isoformat(),
                "value": None if value is None else value / factor,
                "raw_value": value,
                "split_factor": factor,
                "basis": "current_share_terms",
                "method_version": METHOD_VERSION,
            }
        )
    return sorted(adjusted, key=lambda row: row["date"])
