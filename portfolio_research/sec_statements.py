"""Quarterly, filing-date-aware extraction from SEC US-GAAP company facts.

Only explicitly reported quarterly durations or compatible cumulative-flow
differences establish a quarter. Per-share earnings and weighted share counts
are not additive and are never obtained by subtracting year-to-date values.
"""

from __future__ import annotations

import math
from datetime import date, timedelta

from portfolio_research.statements import quarterly_window

_PER_SHARE = {
    "diluted_eps": ("EarningsPerShareDiluted", "USD/shares"),
    "diluted_shares": ("WeightedAverageNumberOfDilutedSharesOutstanding", "shares"),
}


def _day(value):
    return date.fromisoformat(str(value)[:10])


def _read(companyfacts, tag, unit, cutoff, allowed_forms):
    rows = []
    items = (
        companyfacts.get("facts", {}).get("us-gaap", {}).get(tag, {}).get("units", {}).get(unit, [])
    )
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            if isinstance(item.get("val"), bool):
                continue
            value = float(item["val"])
            end, filed = _day(item["end"]), _day(item["filed"])
            start = _day(item["start"]) if item.get("start") else None
            available = filed + timedelta(days=1)
            if (
                not math.isfinite(value)
                or item.get("form") not in allowed_forms
                or not item.get("accn")
                or available > cutoff
                or end > cutoff
                or (start is not None and start > end)
            ):
                continue
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        rows.append(
            {
                **item,
                "val": value,
                "_end": end,
                "_start": start,
                "_filed": filed,
                "_available": available,
                "_tag": tag,
                "_unit": unit,
            }
        )
    return rows


def _pick(rows, *, end, start, accession=None, filed_by=None):
    matching = [
        row
        for row in rows
        if row["_end"] == end
        and row["_start"] == start
        and (accession is None or row["accn"] == accession)
        and (filed_by is None or row["_filed"] <= filed_by)
    ]
    if not matching:
        return None
    best = max(matching, key=lambda row: (row["_filed"], row["accn"]))
    same_context = [
        row for row in matching if row["_filed"] == best["_filed"] and row["accn"] == best["accn"]
    ]
    return best if len({row["val"] for row in same_context}) == 1 else None


def _duration(row):
    return (row["_end"] - row["_start"]).days + 1 if row["_start"] else None


def _previous(rows, current, previous_end):
    same_filing = [
        row
        for row in rows
        if row["accn"] == current["accn"]
        and row["_start"] == current["_start"]
        and row["_end"] == previous_end
        and row["_filed"] <= current["_filed"]
    ]
    return _pick(
        same_filing or rows,
        end=previous_end,
        start=current["_start"],
        accession=current["accn"] if same_filing else None,
        filed_by=current["_filed"],
    )


def _cumulative_pair(current, previous):
    """Compatible consecutive YTD scopes, including annual minus nine months."""
    if (
        previous is None
        or current["_start"] != previous["_start"]
        or current["_tag"] != previous["_tag"]
        or current["_unit"] != previous["_unit"]
        or not 60 <= (current["_end"] - previous["_end"]).days <= 100
    ):
        return False
    current_days, previous_days = _duration(current), _duration(previous)
    return (
        (120 <= current_days <= 220 and 60 <= previous_days <= 100)
        or (220 <= current_days <= 310 and 120 <= previous_days <= 220)
        or (330 <= current_days <= 400 and 220 <= previous_days <= 310)
    )


def _parts(row, received_at, source_id):
    return {
        "tag": row["_tag"],
        "start": row.get("start"),
        "end": row["end"],
        "filed": row["filed"],
        "accession": row["accn"],
        "value": row["val"],
        "unit": row["_unit"],
        "available_at": row["_available"].isoformat(),
        "received_at": received_at,
        "source_id": source_id,
    }


def _flow(rows, *, start, end, anchor):
    direct = _pick(rows, start=start, end=end, accession=anchor["accn"])
    if direct is not None:
        return direct["val"], "reported_quarter", [direct]
    current_rows = [
        row
        for row in rows
        if row["_end"] == end
        and row["accn"] == anchor["accn"]
        and row["_start"] is not None
        and row["_start"] < start
    ]
    derived = []
    for fiscal_start in {row["_start"] for row in current_rows}:
        current = _pick(current_rows, start=fiscal_start, end=end, accession=anchor["accn"])
        if current is None:
            continue
        previous = _previous(rows, current, start - timedelta(days=1))
        if _cumulative_pair(current, previous):
            derived.append((current["val"] - previous["val"], current, previous))
    # Conflicting cumulative contexts cannot be resolved by their order in the payload.
    if len(derived) != 1:
        return None
    value, current, previous = derived[0]
    method = (
        "annual_minus_nine_months"
        if _duration(current) >= 330
        else "current_ytd_minus_previous_ytd"
    )
    return value, method, [current, previous]


def extract_sec_quarters(
    companyfacts,
    security_id,
    as_of,
    received_at,
    source_id,
    *,
    years=3,
) -> list[dict]:
    """Newest-first observed quarterly statements within at most three years.

    Filing-date-plus-one-day availability is a conservative reconstruction of
    publication, not a claim that this system received the fact on that date.
    The input payload is preserved. Missing concepts and invalid contexts stay
    null; their absence does not invent a quarter or an accounting substitute.
    """
    # Runtime import lets providers integrate this helper without an import cycle.
    from portfolio_lab.providers import ALLOWED_FORMS, FLOW_TAGS, STOCK_TAGS

    cutoff = _day(as_of)
    # Validate the history setting even for an empty response.
    quarterly_window([], as_of=as_of, years=years)
    if not isinstance(companyfacts, dict) or not companyfacts.get("facts", {}).get("us-gaap"):
        return []
    monetary_tags = {tag for tags in (*FLOW_TAGS.values(), *STOCK_TAGS.values()) for tag in tags}
    facts = {tag: _read(companyfacts, tag, "USD", cutoff, ALLOWED_FORMS) for tag in monetary_tags}
    per_share = {
        field: _read(companyfacts, tag, unit, cutoff, ALLOWED_FORMS)
        for field, (tag, unit) in _PER_SHARE.items()
    }
    anchors = []
    flow_tags = {tag for tags in FLOW_TAGS.values() for tag in tags}
    for rows in [*(facts[tag] for tag in flow_tags), *per_share.values()]:
        for current in rows:
            duration = _duration(current)
            if duration is not None and 60 <= duration <= 100:
                anchors.append((current["_start"], current))
                continue
            if current["_tag"] not in flow_tags or duration is None or duration <= 100:
                continue
            for previous_end in {row["_end"] for row in rows if row["_end"] < current["_end"]}:
                previous = _previous(rows, current, previous_end)
                if _cumulative_pair(current, previous):
                    anchors.append((previous_end + timedelta(days=1), current))
    result = []
    for end in sorted({anchor["_end"] for _, anchor in anchors}, reverse=True):
        candidates = [(start, anchor) for start, anchor in anchors if anchor["_end"] == end]
        latest = max((anchor["_filed"], anchor["accn"]) for _, anchor in candidates)
        candidates = [
            (start, anchor)
            for start, anchor in candidates
            if (anchor["_filed"], anchor["accn"]) == latest
        ]
        starts = {start for start, _ in candidates}
        if len(starts) != 1:
            continue
        start, anchor = candidates[0]
        row = {
            "security_id": security_id,
            "period_start": start.isoformat(),
            "period_end": end.isoformat(),
            "period_type": "quarter",
            "currency": "USD",
            "available_at": anchor["_available"].isoformat(),
            "received_at": received_at,
            "source_id": source_id,
            "source_policy": "original_filing",
            "accession": anchor["accn"],
            "filed": anchor["_filed"].isoformat(),
            "method_version": "sec-quarter-extraction-1",
            "provenance": {},
            "data_warnings": [],
            "extraction_basis": "original_filing_date_plus_one_day; not historical live-receipt replay",
        }
        for field, alternatives in FLOW_TAGS.items():
            row[field] = None
            for tag in alternatives:
                extracted = _flow(facts[tag], start=start, end=end, anchor=anchor)
                if extracted is None:
                    continue
                value, method, components = extracted
                row[field] = value
                row["provenance"][field] = {
                    "method": method,
                    "components": [_parts(part, received_at, source_id) for part in components],
                }
                row["available_at"] = max(
                    row["available_at"], *(part["_available"].isoformat() for part in components)
                )
                if field == "capex" and value < 0:
                    row["data_warnings"].append(
                        "Negative gross capex retained; accounting review required."
                    )
                break
        for field, rows in per_share.items():
            direct = _pick(rows, start=start, end=end, accession=anchor["accn"])
            row[field] = direct["val"] if direct is not None else None
            if direct is not None:
                row["provenance"][field] = {
                    "method": "reported_quarter",
                    "components": [_parts(direct, received_at, source_id)],
                }
        # A balance-sheet instant alone is not evidence of a quarterly duration.
        if all(row[field] is None for field in (*FLOW_TAGS, *_PER_SHARE)):
            continue
        for field, alternatives in STOCK_TAGS.items():
            row[field] = None
            for tag in alternatives:
                instant = _pick(facts[tag], start=None, end=end, accession=anchor["accn"])
                if instant is not None:
                    row[field] = instant["val"]
                    row["provenance"][field] = {
                        "method": "instant",
                        "components": [_parts(instant, received_at, source_id)],
                    }
                    break
        if row["assets"] is not None and row["assets"] < 0:
            row["assets"] = None
            row["data_warnings"].append("Negative total assets; invalid observation.")
        row["earnings_definition"] = (
            "common_shareholders"
            if row["income_common"] is not None
            else "consolidated_unqualified"
        )
        result.append(row)
    return quarterly_window(result, as_of=as_of, years=years, security_id=security_id)
