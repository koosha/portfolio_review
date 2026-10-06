"""Pure accounting normalization for provider statements.

Trailing-twelve-month windows, restatement vintages, per-share arithmetic, statement
share-unit reconciliation and split-adjusted dividends. Nothing here reads the network
or the cache; every function takes supplied records and returns new records. Missing
inputs stay missing: no field is ever zero-filled to complete a window.
"""

from __future__ import annotations

import math
from copy import deepcopy
from datetime import date

import pandas as pd

METHOD_VERSION = "statement-normalization-2"
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
MIN_QUARTER_GAP_DAYS = 60
OPTIONAL_FLOWS = (
    "depreciation",
    "change_working_capital",
    "tax_provision",
    "pretax_income",
    "stock_compensation",
)
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


def _optional(value):
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, (list, tuple, dict)):
        return value
    try:
        return None if pd.isna(value) else value
    except (TypeError, ValueError):
        return None


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


def quarter_window_start(as_of, years=3) -> str:
    """Exclusive calendar boundary for at most three years of quarterly evidence."""
    if type(years) is not int or not 1 <= years <= 3:
        raise ValueError("financial history years must be an integer between 1 and 3")
    day = pd.Timestamp(_date(as_of, "as_of"))
    return (day - pd.DateOffset(years=years)).date().isoformat()


def quarterly_window(
    rows,
    *,
    as_of,
    years=3,
    security_id=None,
    cutoff=None,
    require_received_by_cutoff=False,
    allow_untyped=False,
) -> list[dict]:
    """Latest eligible quarterly vintages, at most four quarters per selected year.

    The raw history is untouched. Publication/receipt filtering precedes vintage
    selection, so a future restatement cannot hide an eligible original record.
    ``allow_untyped`` applies only to an already established quarterly container.
    """
    start, end = _date(quarter_window_start(as_of, years), "start"), _date(as_of, "as_of")
    boundary = pd.Timestamp(cutoff) if cutoff is not None else None
    if boundary is not None and boundary.tzinfo is None:
        raise ValueError("quarterly cutoff requires an explicit timezone")
    selected = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        kind = _optional(row.get("period_type"))
        kind = kind.lower() if isinstance(kind, str) else kind
        if kind not in ("quarter", "quarterly") and not (allow_untyped and kind is None):
            continue
        if security_id is not None and str(row.get("security_id")) != str(security_id):
            continue
        try:
            day = _date(row.get("period_end"), "period_end")
            if not start < day <= end:
                continue
            if _optional(row.get("period_start")) is not None:
                first = _date(row["period_start"], "period_start")
                if not MIN_QUARTER_GAP_DAYS <= (day - first).days + 1 <= MAX_QUARTER_GAP_DAYS:
                    continue
            if boundary is not None:
                for field in ("available_at", "received_at"):
                    value = _optional(row.get(field))
                    if field == "received_at" and not require_received_by_cutoff:
                        continue
                    stamp = pd.Timestamp(value) if value else pd.NaT
                    if pd.isna(stamp):
                        break
                    if stamp.tzinfo is None:
                        stamp = stamp.tz_localize("America/New_York")
                    if stamp > boundary:
                        break
                else:
                    selected.append(deepcopy(row))
                continue
        except (ValueError, TypeError):
            continue
        selected.append(deepcopy(row))
    selected = latest_vintage(selected)
    grouped = {}
    for row in selected:
        grouped.setdefault(str(row.get("security_id") or ""), []).append(row)
    result = []
    for group in grouped.values():
        result.extend(
            sorted(group, key=lambda row: str(row["period_end"]), reverse=True)[: 4 * years]
        )
    return sorted(
        result,
        key=lambda row: (str(row.get("security_id") or ""), str(row["period_end"])),
        reverse=True,
    )


def ttm_in_window(row, *, as_of, years=3) -> bool:
    """A declared twelve-month scope fully inside the quarterly evidence window."""
    if not isinstance(row, dict) or str(_optional(row.get("period_type"))).lower() != "ttm":
        return False
    start, end = _date(quarter_window_start(as_of, years), "start"), _date(as_of, "as_of")
    try:
        period_end = _date(row.get("period_end"), "period_end")
        if not start < period_end <= end:
            return False
        references = _optional(row.get("quarters_used"))
        if not isinstance(references, (list, tuple, str)) or not references:
            references = _optional(row.get("ttm_quarters"))
        if isinstance(references, (list, tuple, str)) and references:
            references = references.split(",") if isinstance(references, str) else references
            dates = sorted({_date(value, "quarter") for value in references}, reverse=True)
            return (
                len(references) == len(dates) == 4
                and dates[0] == period_end
                and all(start < day <= end for day in dates)
                and all(
                    MIN_QUARTER_GAP_DAYS <= (newer - older).days <= MAX_QUARTER_GAP_DAYS
                    for newer, older in zip(dates, dates[1:])
                )
                and _known_ttm_scopes_valid(row, dates)
            )
        first = _date(row.get("period_start"), "period_start")
        # The cap applies to quarterly reporting dates. With normal filing lag,
        # the oldest quarter's flow may start before the calendar-year boundary.
        oldest_end = (pd.Timestamp(period_end) - pd.DateOffset(months=9)).date()
        return (
            first <= period_end
            and 330 <= (period_end - first).days + 1 <= 400
            and start < oldest_end
        )
    except (ValueError, TypeError):
        return False


def _known_ttm_scopes_valid(row, dates):
    """Validate retained quarterly flow scopes when their starts were recorded."""
    provenance = row.get("provenance")
    if not isinstance(provenance, dict):
        return True
    for field in (*FLOW_FIELDS, *OPTIONAL_FLOWS):
        entry = provenance.get(field)
        components = entry.get("components") if isinstance(entry, dict) else None
        if not isinstance(components, (list, tuple)) or len(components) != 4:
            continue
        if not all(isinstance(part, dict) for part in components):
            continue
        ends = [
            _optional(part.get("period_end")) or _optional(part.get("end")) for part in components
        ]
        if not all(ends):
            continue
        try:
            ordered = sorted(
                zip((_date(end, "quarter") for end in ends), components),
                reverse=True,
                key=lambda pair: pair[0],
            )
            if [end for end, _ in ordered] != dates:
                return False
            for end, part in ordered:
                value = _optional(part.get("period_start")) or _optional(part.get("start"))
                if (
                    value is not None
                    and not MIN_QUARTER_GAP_DAYS
                    <= (end - _date(value, "period_start")).days + 1
                    <= MAX_QUARTER_GAP_DAYS
                ):
                    return False
            for (_, newer), (older_end, _) in zip(ordered, ordered[1:]):
                value = _optional(newer.get("period_start")) or _optional(newer.get("start"))
                if value is not None and (_date(value, "period_start") - older_end).days != 1:
                    return False
        except (ValueError, TypeError):
            return False
    return True


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
    for row in window:
        value = _optional(row.get("period_start"))
        if value is not None:
            first = _date(value, "period_start")
            duration = (row["_period_end"] - first).days + 1
            if not MIN_QUARTER_GAP_DAYS <= duration <= MAX_QUARTER_GAP_DAYS:
                _issue(
                    issues,
                    "TTM_INVALID_QUARTER_SCOPE",
                    security_id,
                    f"{row['period_end']}: declared quarterly flow lasts {duration} days.",
                )
                return None
    for newer, older in zip(window, window[1:]):
        gap = (newer["_period_end"] - older["_period_end"]).days
        if not MIN_QUARTER_GAP_DAYS <= gap <= MAX_QUARTER_GAP_DAYS:
            _issue(
                issues,
                "TTM_QUARTER_GAP",
                security_id,
                f"{gap} days between {older['period_end']} and {newer['period_end']}; "
                "a quarter is absent from the window.",
            )
            return None
        value = _optional(newer.get("period_start"))
        if value is not None:
            first = _date(value, "period_start")
            join_gap = (first - older["_period_end"]).days
            if join_gap != 1:
                overlap = join_gap <= 0
                _issue(
                    issues,
                    "TTM_QUARTER_OVERLAP" if overlap else "TTM_QUARTER_FLOW_GAP",
                    security_id,
                    f"Quarter starting {first.isoformat()} {'overlaps' if overlap else 'leaves a gap after'} the previous quarter ending {older['period_end']}; a TTM needs contiguous, nonoverlapping flows.",
                )
                return None
    latest = window[0]
    missing: list[str] = []
    result: dict = {
        **{
            key: deepcopy(value)
            for key, value in latest.items()
            if key
            not in {
                "_period_end",
                *FLOW_FIELDS,
                *BALANCE_FIELDS,
                *OPTIONAL_FLOWS,
                "diluted_eps",
                "diluted_shares",
            }
        },
        "period_end": latest["_period_end"].isoformat(),
        "period_type": "ttm",
        "basis": "ttm_from_quarters",
        "method_version": METHOD_VERSION,
        "quarters_used": [row["_period_end"].isoformat() for row in window],
        "ttm_quarters": ",".join(row["_period_end"].isoformat() for row in window),
    }
    currencies = {_optional(row.get("currency")) for row in window}
    if _optional(latest.get("currency")) is not None and len(currencies) != 1:
        _issue(
            issues,
            "TTM_CURRENCY_MISMATCH",
            security_id,
            "Quarterly statement currencies differ; no comparable TTM.",
        )
        return None
    for field in FLOW_FIELDS:
        values = [_number(row.get(field)) for row in window]
        if any(value is None for value in values):
            result[field] = None
            missing.append(field)
        else:
            result[field] = sum(values)
    for field in OPTIONAL_FLOWS:
        values = [_number(row.get(field)) for row in window]
        result[field] = sum(values) if all(value is not None for value in values) else None
    result["provenance"] = {
        field: {
            "method": "sum_nonoverlapping_quarters",
            "components": [
                {
                    key: deepcopy(row.get(key))
                    for key in (
                        "period_start",
                        "period_end",
                        "source_id",
                        "available_at",
                        "received_at",
                    )
                }
                | {
                    "value": row.get(field),
                    "quarter_provenance": deepcopy(
                        row["provenance"].get(field)
                        if isinstance(row.get("provenance"), dict)
                        else None
                    ),
                }
                for row in window
            ],
        }
        for field in (*FLOW_FIELDS, *OPTIONAL_FLOWS)
        if result.get(field) is not None
    }
    for field in ("available_at", "received_at"):
        stamps = [row.get(field) for row in window]
        if all(isinstance(value, str) for value in stamps):
            result[field] = max(stamps)
    result["source_ids"] = sorted(
        {
            row["source_id"]
            for row in window
            if isinstance(row.get("source_id"), str) and row["source_id"]
        }
    )
    result["period_start"] = window[-1].get("period_start")
    shares = [_number(row.get("diluted_shares")) for row in window]
    result["diluted_shares"] = None
    if (
        all(value is not None and value > 0 for value in shares)
        and all(_optional(row.get("period_start")) is not None for row in window)
        and len({str(_optional(row.get("share_count_basis"))) for row in window}) == 1
    ):
        durations = [
            (row["_period_end"] - _date(row["period_start"], "period_start")).days + 1
            for row in window
        ]
        if all(MIN_QUARTER_GAP_DAYS <= days <= MAX_QUARTER_GAP_DAYS for days in durations):
            result["diluted_shares"] = sum(
                value * days for value, days in zip(shares, durations)
            ) / sum(durations)
            result["diluted_share_basis"] = "duration_weighted_quarterly_average"
    for field in BALANCE_FIELDS:
        result[field] = _number(latest.get(field))
        if result[field] is None:
            missing.append(field)
    begin = rows[TTM_QUARTERS] if len(rows) > TTM_QUARTERS else None
    if begin is not None:
        gap = (window[-1]["_period_end"] - begin["_period_end"]).days
        begin = begin if MIN_QUARTER_GAP_DAYS <= gap <= MAX_QUARTER_GAP_DAYS else None
    result["assets_begin"] = _number(begin.get("assets")) if begin else None
    result["assets_begin_period_end"] = begin["_period_end"].isoformat() if begin else None
    if begin is not None:
        for field in ("available_at", "received_at"):
            value = begin.get(field)
            if isinstance(value, str) and isinstance(result.get(field), str):
                result[field] = max(result[field], value)
    elif _optional(window[-1].get("assets_begin_period_end")) is not None:
        # A verified opening balance on the oldest included quarter is context,
        # not an extra quarter of analytical flows.
        balance_day = _date(window[-1]["assets_begin_period_end"], "assets_begin_period_end")
        first = _optional(window[-1].get("period_start"))
        if first is not None and (_date(first, "period_start") - balance_day).days == 1:
            result["assets_begin"] = _number(window[-1].get("assets_begin"))
            result["assets_begin_period_end"] = balance_day.isoformat()
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
