"""Turning an archived provider payload into dated records.

Pure mapping: price and action rows, fundamentals rows in the engine's column shape,
fund holdings and sector rows, and classified events. Every record carries its receipt
timestamp, source id and adapter version, and says on what basis it is available.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from .market_values import (
    ADAPTER_VERSION,
    BALANCE_LINES,
    CAPEX_LINE,
    CASHFLOW_LINES,
    COMMON_LINE,
    DISCLOSURE_BASIS,
    FUND_SECTORS,
    INCOME_LINES,
    NEW_YORK,
    PERIOD_FORMS,
    PRICE_ADJUSTMENT,
    WORKING_CAPITAL_LINE,
    _issue,
    _major,
    _number,
    _text,
    _units,
    available_at,
)
from .statements import ttm_from_quarters


def _price_rows(sid, payload, received_at, source_id, issues=None):
    metadata = payload.get("metadata") or {}
    quote_currency, major, factor = _units(metadata.get("currency"))
    prices, actions = [], []
    for row in payload.get("rows", []):
        day = _text(row.get("date"), 20)
        if day is None:
            continue
        prices.append(
            {
                "security_id": sid,
                "date": day,
                "close": _major(row.get("close"), quote_currency, factor),
                "adjusted_close": _major(row.get("adjusted_close"), quote_currency, factor),
                "volume": _number(row.get("volume")),
                "currency": major,
                "quote_currency": quote_currency,
                "quote_unit_factor": factor,
                "available_at": available_at(day),
                "received_at": received_at,
                "source_id": source_id,
                "adjustment": PRICE_ADJUSTMENT,
                "adapter_version": ADAPTER_VERSION,
            }
        )
        dividend = _number(row.get("dividend"))
        if dividend:
            actions.append(
                {
                    "security_id": sid,
                    "date": day,
                    "kind": "dividend",
                    "value": _major(dividend, quote_currency, factor),
                    "currency": major,
                    "received_at": received_at,
                    "source_id": source_id,
                }
            )
        split = _number(row.get("split"))
        if split is not None and split < 0:
            # Zero is the provider's "no action" value; a negative ratio is not a split.
            # Storing one would raise inside the pure split-adjustment helper and take
            # the whole run down with it.
            _issue(
                issues,
                "PROVIDER_VALUE_REJECTED",
                sid,
                f"Stock split ratio {split:g} on {day} is not positive; the action was dropped.",
            )
            split = None
        if split:
            actions.append(
                {
                    "security_id": sid,
                    "date": day,
                    "kind": "split",
                    "value": split,
                    "currency": None,
                    "received_at": received_at,
                    "source_id": source_id,
                }
            )
    return prices, actions, quote_currency, major, factor


def _capex(value):
    """Yahoo reports capital expenditure as a negative cash flow; store the outflow."""
    number = _number(value)
    if number is None:
        return None, None
    if number > 0:
        return None, "Capital Expenditure is reported as a positive amount; not converted."
    return -number, None


def _working_capital(value):
    """Yahoo signs the change in working capital as a cash effect; store the investment.

    A negative provider value means working capital consumed cash, which is positive
    reinvestment to the valuation engines (``capex - depreciation + change_working_capital``).
    The line is flipped here for the same reason ``_capex`` is.
    """
    number = _number(value)
    if number is None:
        return None, "Change In Working Capital is absent; reinvestment excludes it."
    return -number, None


def _publication(filings, period_end, lag_days, *, period_type):
    """``(available_at, basis)``: the filing that reports this period, else the assumed lag.

    A filing counts only when its form reports this period type and it arrives no later
    than the assumed publication lag allows. The provider returns recent filings only,
    so without both bounds an older period would inherit a much later filing date and be
    treated as unknowable long after it was in fact published.
    """
    assumed = (date.fromisoformat(period_end) + timedelta(days=int(lag_days))).isoformat()
    forms = PERIOD_FORMS.get(str(period_type), frozenset())
    candidates = [
        row["date"]
        for row in filings
        if row.get("date")
        and row.get("type")
        and str(row["type"]).upper().split("/")[0] in forms
        and period_end <= row["date"] <= assumed
    ]
    if candidates:
        return min(candidates), "filing_date"
    return assumed, "assumed_publication_lag"


def _assets_begin(balance):
    """Total assets at the start of each period: the previous period's instant."""
    periods = sorted(balance, reverse=True)
    result = {}
    for current, previous in zip(periods, periods[1:]):
        result[current] = _number(balance[previous].get("Total Assets"))
    return result


def _statement_row(sid, period_end, statements_set, *, context):
    income = statements_set["income"].get(period_end, {})
    balance = statements_set["balance"].get(period_end, {})
    cashflow = statements_set["cashflow"].get(period_end, {})
    capex, capex_note = _capex(cashflow.get(CAPEX_LINE))
    working_capital, working_note = _working_capital(cashflow.get(WORKING_CAPITAL_LINE))
    stamp, basis = _publication(
        context["filings"], period_end, context["lag_days"], period_type=context["period_type"]
    )
    row = {
        "security_id": sid,
        "period_end": period_end,
        "period_type": context["period_type"],
        "available_at": stamp,
        "availability_basis": basis,
        "received_at": context["received_at"],
        "currency": context["currency"],
        "source_id": context["source_id"],
        "adapter_version": ADAPTER_VERSION,
        # Only an explicit common-shareholders line establishes that definition.
        "earnings_definition": "common_shareholders" if COMMON_LINE in income else None,
        "capex": capex,
        "assets_begin": context["assets_begin"].get(period_end),
        "data_note": "; ".join(note for note in (capex_note, working_note) if note) or None,
    }
    for field, line in INCOME_LINES.items():
        row[field] = _number(income.get(line))
    for field, line in BALANCE_LINES.items():
        row[field] = _number(balance.get(line))
    for field, line in CASHFLOW_LINES.items():
        row[field] = _number(cashflow.get(line))
    row["change_working_capital"] = working_capital
    return row


def _statement_rows(sid, payload, period_type, *, received_at, source_id, lag_days):
    statements_set = payload.get(period_type) or {}
    if not statements_set.get("income"):
        return []
    context = {
        "period_type": period_type,
        "filings": payload.get("filings") or [],
        "lag_days": lag_days,
        "received_at": received_at,
        "source_id": source_id,
        "currency": payload.get("currency"),
        "assets_begin": _assets_begin(statements_set.get("balance") or {}),
    }
    periods = sorted(statements_set["income"], reverse=True)
    return [_statement_row(sid, period, statements_set, context=context) for period in periods]


def _ttm_row(sid, quarterly, *, issues):
    """One TTM row in the fundamentals shape, built from the quarterly rows."""
    if not quarterly:
        return None
    ttm = ttm_from_quarters(quarterly, issues=issues, security_id=sid)
    if ttm is None:
        return None
    # The window is knowable when its latest quarter was published.
    return {
        **quarterly[0],
        **{key: value for key, value in ttm.items() if key not in {"quarters_used", "missing"}},
        "ttm_quarters": ",".join(ttm["quarters_used"]),
        "missing_fields": ",".join(ttm["missing"]),
    }


def _holding_issuer(symbol, issuer_lookup):
    if not symbol:
        return None
    if issuer_lookup is None:
        return "listing:" + symbol
    try:
        found = issuer_lookup({"symbol": symbol})
    except Exception:  # A broken lookup leaves the holding listing-scoped.
        found = None
    return found.strip() if isinstance(found, str) and found.strip() else "listing:" + symbol


def fund_rows(sid, payload, received_at, source_id, issuer_lookup=None):
    """Fund holding rows, sector rows and the sector keys this adapter cannot map.

    The provider publishes an undated snapshot of the largest holdings only. The rows
    say so: ``disclosure_basis`` records that the holdings date is the receipt date and
    that the weights do not sum to the whole fund.
    """
    holdings_date = datetime.fromisoformat(received_at).astimezone(NEW_YORK).date().isoformat()
    base = {
        "fund_id": sid,
        "holdings_date": holdings_date,
        # The snapshot is knowable from its New York day, not from the fetch instant: a
        # receipt stamp always falls after a current review's information cutoff, which
        # would silently drop every freshly fetched disclosure.
        "available_at": holdings_date,
        "received_at": received_at,
        "source_id": source_id,
        "disclosure_basis": DISCLOSURE_BASIS,
        "adapter_version": ADAPTER_VERSION,
    }
    holdings = [
        {
            **base,
            "issuer_id": _holding_issuer(row.get("symbol"), issuer_lookup),
            "holding_symbol": row.get("symbol"),
            "name": row.get("name"),
            "weight": _number(row.get("weight")),
        }
        for row in payload.get("top_holdings", [])
        if _number(row.get("weight")) is not None and row.get("symbol")
    ]
    cash = _number((payload.get("asset_classes") or {}).get("cashPosition"))
    if cash is not None:
        holdings.append(
            {**base, "issuer_id": "CASH", "holding_symbol": None, "name": "Cash", "weight": cash}
        )
    weights = payload.get("sector_weightings") or {}
    sectors = [
        {**base, "sector": FUND_SECTORS[key], "weight": value}
        for key, value in weights.items()
        if key in FUND_SECTORS and value is not None
    ]
    return holdings, sectors, sorted(key for key in weights if key not in FUND_SECTORS)


def _event(sid, *, kind, event_date, title, url, provider, summary, classification, shared):
    return {
        "security_id": sid,
        "event_date": event_date,
        "kind": kind,
        "title": title,
        "url": url,
        "provider": provider,
        "summary": summary,
        "classification": classification,
        **shared,
    }


def _news_events(sid, payload, shared, limit):
    events, seen = [], set()
    for row in payload.get("news", []):
        key = row.get("url") or f"{row.get('title')}|{row.get('published_at')}"
        if key in seen or not row.get("published_at"):
            continue
        seen.add(key)
        events.append(
            _event(
                sid,
                kind="news",
                event_date=row["published_at"],
                title=row.get("title"),
                url=row.get("url"),
                provider=row.get("provider"),
                summary=row.get("summary"),
                classification="third_party_opinion",
                shared=shared,
            )
        )
        if len(events) >= limit:
            break
    return events


def _filing_events(sid, payload, shared):
    """Filing events, newest first: the caller bounds them before merging with news."""
    events, seen = [], set()
    filings = sorted(
        payload.get("filings", []), key=lambda row: str(row.get("date") or ""), reverse=True
    )
    for row in filings:
        if not row.get("date"):
            continue
        key = row.get("url") or f"{row.get('title')}|{row['date']}"
        if key in seen:
            continue
        seen.add(key)
        events.append(
            _event(
                sid,
                kind="filing",
                event_date=row["date"],
                title=row.get("title") or row.get("type"),
                url=row.get("url"),
                provider=_text(row.get("type"), 20),
                summary=None,
                classification="issuer_fact",
                shared=shared,
            )
        )
    return events


def _schedule_events(sid, payload, shared):
    return [
        _event(
            sid,
            kind="earnings_date",
            event_date=day,
            title="Scheduled earnings date",
            url=None,
            provider="yahoo",
            summary=None,
            classification="issuer_fact",
            shared=shared,
        )
        for day in payload.get("earnings_dates", [])
    ]


def _action_events(sid, actions, shared):
    events, seen = [], set()
    for row in actions or []:
        kind, day = row.get("kind"), _text(row.get("date"), 20)
        if kind not in {"dividend", "split"} or day is None or (kind, day) in seen:
            continue
        seen.add((kind, day))
        value = _number(row.get("value"))
        events.append(
            _event(
                sid,
                kind=kind,
                event_date=day,
                title=f"{kind.capitalize()} {value:g}" if value is not None else kind.capitalize(),
                url=None,
                provider="yahoo",
                summary=None,
                classification="issuer_fact",
                shared=shared,
            )
        )
    return events


def _cik(issuer_id):
    if isinstance(issuer_id, str) and issuer_id.startswith("cik:"):
        digits = issuer_id.split(":", 1)[1].strip()
        return digits if digits.isdigit() else None
    return None
