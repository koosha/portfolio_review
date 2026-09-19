"""Deterministic company briefs assembled from structured facts, with a source per bullet.

Every statement is generated from numbers by template and is labelled proposed: a
brief is draft evidence for review, never a validated view. External text (news
titles) is data, never instruction, and is quarantined to the changes section with
`classification: "third_party_opinion"`; it never reaches the thesis, counter
thesis or invalidation conditions. A bullet without a retained source is dropped
rather than published uncited.

Input shapes (plain dicts produced by the market-data adapter):

security: {"security_id", "name", "instrument_type", "sector", "currency"}
prices: [{"security_id", "date": "YYYY-MM-DD", "close", "adjusted_close", "volume",
    "currency", "available_at" (aware ISO), "received_at", "source_id"}]
estimates: {"currency", "eps"/"revenue": {"0y"|"+1y": {"avg", "low", "high",
    "year_ago", "analysts", "growth"}}, "price_targets": {"current", "low", "high",
    "mean", "median"}, "recommendations": {"period", "strong_buy", "buy", "hold",
    "sell", "strong_sell"}, "earnings_dates": ["YYYY-MM-DD"], "ex_dividend_date",
    "received_at", "source_id", "label"}
ttm: trailing statement totals — {"period_end", "currency", "revenue",
    "operating_income", "net_income", "operating_cash_flow", "capex" (positive
    spending), "assets", "debt", "cash", "available_at", "received_at", "source_id",
    "prior": {"period_end", "revenue", "operating_income", "net_income"}}
events: [{"security_id", "event_date", "kind": "news"|"filing"|"dividend"|"split"|
    "earnings_date", "title", "url", "provider", "summary", "value", "currency",
    "classification", "received_at", "source_id"}]
previous: the previous review's stored stub — {"as_of", "close", "currency",
    "source_id", "received_at", "estimates": estimates-shaped} or None
fx_note: a sentence naming the verified conversion used for a foreign quote, or None
"""

from __future__ import annotations

import hashlib
import math
import re
from calendar import monthrange
from collections.abc import Mapping
from datetime import date, datetime, timezone

METHOD_VERSION = "brief-template-1"
BASIS = "proposed_from_structured_facts"
DEFAULT_ADAPTER_VERSION = "yfinance-adapter-1"
NEWS_BULLET_LIMIT = 5
MAX_TITLE_LENGTH = 200
OPERATING_SHORTFALL = 0.10
LEVERAGE_INCREASE = 0.10
PROPOSED = "proposed, not reviewed"
EXTERNAL = "an external estimate, not an established outcome"
ADAPTER_LIMITATION = "Provider prices, statements and disclosures can be revised after receipt."
TEMPLATE_LIMITATION = (
    "Every statement is generated from structured facts by template and is proposed; "
    "none has been reviewed."
)


def _adapter_version():
    try:
        from portfolio_research import market_data

        version = getattr(market_data, "ADAPTER_VERSION", None)
        return version if isinstance(version, str) and version else DEFAULT_ADAPTER_VERSION
    except ImportError:
        return DEFAULT_ADAPTER_VERSION


def _number(value):
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _text(value, limit=MAX_TITLE_LENGTH):
    if not isinstance(value, str):
        return None
    cleaned = re.sub(r"\s+", " ", "".join(c for c in value if ord(c) >= 32)).strip()
    if not cleaned:
        return None
    return cleaned if len(cleaned) <= limit else cleaned[: limit - 1].rstrip() + "…"


def _day(value):
    try:
        return date.fromisoformat(value) if isinstance(value, str) and len(value) == 10 else None
    except ValueError:
        return None


def _money(value, currency):
    return f"{value:,.0f} {currency}" if currency else f"{value:,.0f}"


def _price(value, currency):
    return f"{value:,.2f} {currency}" if currency else f"{value:,.2f}"


def _percent(value):
    return f"{value * 100:.1f}%"


def _signed(value):
    return f"{value * 100:+.1f}%"


def _count(number, noun):
    return f"{number} {noun}" if number == 1 else f"{number} {noun}s"


def _rows(value):
    return [row for row in value or [] if isinstance(row, Mapping)]


def _estimate(estimates, family, period, field):
    if not isinstance(estimates, Mapping):
        return None
    block = estimates.get(family)
    period_row = block.get(period) if isinstance(block, Mapping) else None
    return _number(period_row.get(field)) if isinstance(period_row, Mapping) else None


def _endpoint(start, horizon_months):
    index = start.year * 12 + start.month - 1 + horizon_months
    year, month = divmod(index, 12)
    return date(year, month + 1, min(start.day, monthrange(year, month + 1)[1]))


def _content_hash(source_id):
    _, _, digest = source_id.partition(":")
    if re.fullmatch(r"[0-9a-f]{64}", digest or ""):
        return digest
    return hashlib.sha256(source_id.encode("utf-8")).hexdigest()


def _locator(locator, source_id):
    """A publishable locator for a source, else the provider that answered.

    A provider-supplied URL is data, not a vetted address: it goes through the same
    allowlist the published source list uses, so a ``javascript:`` or ``data:`` href
    can never travel into a rendered brief as a citation.
    """
    from .public import safe_locator

    return safe_locator(_text(locator, limit=300)) or source_id.partition(":")[0]


def _register(registry, source_id, *, locator=None, published_at=None, received_at=None):
    """Retain a source only when its receipt is known; bullets cite retained sources."""
    if not isinstance(source_id, str) or not source_id or not isinstance(received_at, str):
        return
    entry = registry.setdefault(
        source_id,
        {
            "id": source_id,
            "locator": None,
            "published_at": None,
            "received_at": received_at,
            "version": _adapter_version(),
            "content_hash": _content_hash(source_id),
        },
    )
    entry["locator"] = entry["locator"] or _locator(locator, source_id)
    if isinstance(published_at, str) and (entry["published_at"] or "") < published_at:
        entry["published_at"] = published_at
    if (entry["received_at"] or "") < received_at:
        entry["received_at"] = received_at


def _register_all(registry, prices, estimates, ttm, events, previous):
    for row in prices:
        _register(
            registry,
            row.get("source_id"),
            published_at=row.get("available_at"),
            received_at=row.get("received_at"),
        )
    for row in events:
        _register(
            registry,
            row.get("source_id"),
            locator=row.get("url"),
            published_at=row.get("event_date"),
            received_at=row.get("received_at"),
        )
    for record in (estimates, ttm, previous):
        if isinstance(record, Mapping):
            _register(
                registry,
                record.get("source_id"),
                published_at=record.get("available_at"),
                received_at=record.get("received_at"),
            )


def _bullet(registry, text, *source_ids, **extra):
    cited = list(dict.fromkeys(item for item in source_ids if item in registry))
    statement = _text(text, limit=600)
    if not cited or not statement:
        return None
    return {"text": statement, "source_ids": cited, **extra}


def _kept(items):
    return [item for item in items if item is not None]


def _changes(latest, estimates, events, previous, fx_note, registry, news_limit):
    previous = previous if isinstance(previous, Mapping) else {}
    since, as_of = _day(previous.get("as_of")), previous.get("as_of")
    currency = latest.get("currency") if latest else None
    items = [
        _price_change(latest, previous, currency, fx_note, registry, events),
        _estimate_change(estimates, previous, currency, registry),
    ]
    recent = [row for row in events if not since or (_day(row.get("event_date")) or since) >= since]
    filings = [row for row in recent if row.get("kind") == "filing"]
    news = [row for row in recent if row.get("kind") == "news"]
    if filings or news:
        window = f"since {as_of}" if as_of else "in the retained data window"
        items.append(
            _bullet(
                registry,
                f"{_count(len(filings), 'new filing')} and {_count(len(news), 'news item')} "
                f"recorded {window}.",
                *[row.get("source_id") for row in filings + news],
            )
        )
    for row in sorted(
        (row for row in recent if row.get("kind") in ("dividend", "split")),
        key=lambda row: str(row.get("event_date")),
    ):
        items.append(_action_bullet(row, currency, registry))
    for row in sorted(news, key=lambda row: str(row.get("event_date")), reverse=True)[:news_limit]:
        title, provider = _text(row.get("title")), _text(row.get("provider"))
        items.append(
            _bullet(
                registry,
                f"{title} — {provider or 'provider unattributed'}, {row.get('event_date')}.",
                row.get("source_id"),
                classification="third_party_opinion",
            )
            if title
            else None
        )
    return _kept(items)


def _split_factor(events, since, until):
    """The cumulative split ratio applied between two dates, in current share terms."""
    factor = 1.0
    if since is None or until is None:
        return factor
    for row in events:
        day = _day(row.get("event_date"))
        ratio = _number(row.get("value"))
        if row.get("kind") == "split" and day and since < day <= until and ratio and ratio > 0:
            factor *= ratio
    return factor


def _price_change(latest, previous, currency, fx_note, registry, events):
    """The change since the previous review, in one unit and in current share terms.

    Closes are the raw quotes the adapter stored, so a split between the two reviews
    would read as a collapse; the earlier close is restated first. Two closes recorded
    in different currencies are not a change at all, and the bullet says so instead of
    dividing across units.
    """
    close = _number(latest.get("close")) if latest else None
    before, as_of = _number(previous.get("close")), previous.get("as_of")
    if close is None or not before or not as_of:
        return None
    unit = previous.get("currency") or currency
    note = _text(fx_note, limit=300)
    if unit and currency and unit != currency:
        text = (
            f"Price {_price(close, currency)} on {latest.get('date')}; the change since the "
            f"previous review on {as_of} is unavailable because that close was recorded in "
            f"{unit} ({_price(before, unit)})."
        )
    else:
        factor = _split_factor(events, _day(as_of), _day(latest.get("date")))
        adjusted = "" if factor == 1.0 else f", adjusted for a {factor:g}-for-1 split"
        text = (
            f"Price {_price(close, currency)} on {latest.get('date')}, "
            f"{_signed(close / (before / factor) - 1)} versus "
            f"{_price(before, unit)} at the previous review on {as_of}{adjusted}."
        )
    return _bullet(
        registry,
        f"{text} {note}" if note else text,
        latest.get("source_id"),
        previous.get("source_id"),
    )


def _estimate_change(estimates, previous, currency, registry):
    current = _estimate(estimates, "eps", "+1y", "avg")
    before = _estimate(previous.get("estimates"), "eps", "+1y", "avg")
    as_of = previous.get("as_of")
    if current is None or not before or not as_of:
        return None
    unit = (estimates.get("currency") if isinstance(estimates, Mapping) else None) or currency
    return _bullet(
        registry,
        f"Consensus EPS for +1y is {_price(current, unit)} versus {_price(before, unit)} at the "
        f"previous review on {as_of} ({_signed(current / before - 1)}); {EXTERNAL}.",
        estimates.get("source_id") if isinstance(estimates, Mapping) else None,
        previous.get("estimates", {}).get("source_id")
        if isinstance(previous.get("estimates"), Mapping)
        else None,
    )


def _action_bullet(row, currency, registry):
    value = _number(row.get("value"))
    event_date, kind = row.get("event_date"), row.get("kind")
    unit = row.get("currency") or currency
    if kind == "dividend":
        text = (
            f"Dividend of {_price(value, unit)} per share with an event date of {event_date}."
            if value is not None
            else f"A dividend event was recorded with an event date of {event_date}."
        )
    else:
        text = (
            f"Share split of {value:g} for 1 with an event date of {event_date}."
            if value is not None
            else f"A share split was recorded with an event date of {event_date}."
        )
    return _bullet(registry, text, row.get("source_id"))


def _financials(ttm, registry):
    if not isinstance(ttm, Mapping):
        return []
    currency, source, period = ttm.get("currency"), ttm.get("source_id"), ttm.get("period_end")
    prior = ttm.get("prior") if isinstance(ttm.get("prior"), Mapping) else {}
    items = []
    for field, label in (
        ("revenue", "revenue"),
        ("operating_income", "operating income"),
        ("net_income", "net income"),
    ):
        value = _number(ttm.get(field))
        if value is None:
            continue
        text = f"Trailing twelve-month {label} of {_money(value, currency)} to {period}"
        before = _number(prior.get(field))
        if before:
            text += (
                f", {_signed(value / before - 1)} versus {_money(before, currency)} a year earlier"
            )
        items.append(_bullet(registry, text + ".", source))
    cash_flow, capex = _number(ttm.get("operating_cash_flow")), _number(ttm.get("capex"))
    if cash_flow is not None and capex is not None:
        items.append(
            _bullet(
                registry,
                f"Trailing free cash flow of {_money(cash_flow - capex, currency)} to {period} "
                f"(operating cash flow {_money(cash_flow, currency)} less capex "
                f"{_money(capex, currency)}).",
                source,
            )
        )
    debt, assets = _number(ttm.get("debt")), _number(ttm.get("assets"))
    if debt is not None and assets:
        items.append(
            _bullet(
                registry,
                f"Total debt of {_money(debt, currency)} is {_percent(debt / assets)} of assets "
                f"({_money(assets, currency)}) at {period}.",
                source,
            )
        )
    return _kept(items)


def _expectations(estimates, registry):
    if not isinstance(estimates, Mapping):
        return []
    currency, source = estimates.get("currency"), estimates.get("source_id")
    items = []
    for period in ("0y", "+1y"):
        average = _estimate(estimates, "eps", period, "avg")
        low, high = (_estimate(estimates, "eps", period, key) for key in ("low", "high"))
        analysts = _estimate(estimates, "eps", period, "analysts")
        if average is None:
            continue
        span = (
            f" (low {_price(low, currency)}, high {_price(high, currency)})"
            if low is not None and high is not None
            else ""
        )
        cover = f" across {int(analysts)} analysts" if analysts else ""
        items.append(
            _bullet(
                registry,
                f"Consensus EPS for {period} averages {_price(average, currency)}{span}"
                f"{cover}; {EXTERNAL}.",
                source,
            )
        )
    targets = estimates.get("price_targets")
    if isinstance(targets, Mapping):
        low, high = _number(targets.get("low")), _number(targets.get("high"))
        mean, median = _number(targets.get("mean")), _number(targets.get("median"))
        if None not in (low, high, mean):
            median_text = f" and a median of {_price(median, currency)}" if median else ""
            items.append(
                _bullet(
                    registry,
                    f"Analyst price targets range {_price(low, currency)} to "
                    f"{_price(high, currency)} with a mean of {_price(mean, currency)}"
                    f"{median_text}; {EXTERNAL}.",
                    source,
                )
            )
    items.append(_recommendation_bullet(estimates.get("recommendations"), source, registry))
    return _kept(items)


def _recommendation_bullet(mix, source, registry):
    if not isinstance(mix, Mapping):
        return None
    counts = {
        key: _number(mix.get(key)) or 0
        for key in ("strong_buy", "buy", "hold", "sell", "strong_sell")
    }
    total = sum(counts.values())
    if not total:
        return None
    positive = counts["strong_buy"] + counts["buy"]
    negative = counts["sell"] + counts["strong_sell"]
    period = _text(mix.get("period")) or "the latest period"
    return _bullet(
        registry,
        f"Analyst recommendations for period {period}: {positive:.0f} of {total:.0f} ratings buy "
        f"or strong buy, {counts['hold']:.0f} hold, {negative:.0f} sell or strong sell; "
        f"{EXTERNAL}.",
        source,
    )


def _thesis(estimates, ttm, latest, registry):
    trailing = ttm if isinstance(ttm, Mapping) else {}
    currency, source = trailing.get("currency"), trailing.get("source_id")
    estimate_source = estimates.get("source_id") if isinstance(estimates, Mapping) else None
    unit = (estimates.get("currency") if isinstance(estimates, Mapping) else None) or currency
    thesis, counter = [], []
    current, forward = (_estimate(estimates, "eps", period, "avg") for period in ("0y", "+1y"))
    low = _estimate(estimates, "eps", "+1y", "low")
    if current and forward is not None:
        thesis.append(
            _bullet(
                registry,
                f"Consensus expects EPS of {_price(forward, unit)} for +1y versus "
                f"{_price(current, unit)} for 0y, growth of {_signed(forward / current - 1)}; "
                f"{PROPOSED}.",
                estimate_source,
            )
        )
    revenue, operating = _number(trailing.get("revenue")), _number(trailing.get("operating_income"))
    if revenue and operating is not None:
        thesis.append(
            _bullet(
                registry,
                f"Trailing operating margin is {_percent(operating / revenue)} on revenue of "
                f"{_money(revenue, currency)} to {trailing.get('period_end')}; {PROPOSED}.",
                source,
            )
        )
    cash_flow, capex = _number(trailing.get("operating_cash_flow")), _number(trailing.get("capex"))
    if revenue and cash_flow is not None and capex is not None:
        free_cash_flow = cash_flow - capex
        thesis.append(
            _bullet(
                registry,
                f"Trailing free cash flow is {_money(free_cash_flow, currency)}, "
                f"{_percent(free_cash_flow / revenue)} of revenue; {PROPOSED}.",
                source,
            )
        )
    if low is not None and forward:
        counter.append(
            _bullet(
                registry,
                f"The low consensus estimate for +1y is {_price(low, unit)}, "
                f"{_signed(low / forward - 1)} versus the average of {_price(forward, unit)}; "
                f"{PROPOSED}.",
                estimate_source,
            )
        )
    debt = _number(trailing.get("debt"))
    if debt is not None and operating:
        counter.append(
            _bullet(
                registry,
                f"Debt of {_money(debt, currency)} is {debt / operating:,.2f}x trailing operating "
                f"income of {_money(operating, currency)}; {PROPOSED}.",
                source,
            )
        )
    counter.append(_target_downside(estimates, latest, registry))
    return _kept(thesis), _kept(counter)


def _target_downside(estimates, latest, registry):
    targets = estimates.get("price_targets") if isinstance(estimates, Mapping) else None
    low = _number(targets.get("low")) if isinstance(targets, Mapping) else None
    close = _number(latest.get("close")) if latest else None
    if low is None or not close:
        return None
    currency = latest.get("currency")
    return _bullet(
        registry,
        f"The low analyst price target of {_price(low, currency)} is {_signed(low / close - 1)} "
        f"versus the close of {_price(close, currency)} on {latest.get('date')}; {EXTERNAL}.",
        estimates.get("source_id"),
    )


def _invalidation(estimates, ttm, registry):
    trailing = ttm if isinstance(ttm, Mapping) else {}
    currency, source, period = (
        trailing.get("currency"),
        trailing.get("source_id"),
        trailing.get("period_end"),
    )
    items = []
    operating = _number(trailing.get("operating_income"))
    if operating is not None:
        items.append(
            _bullet(
                registry,
                f"Trailing operating income falls below "
                f"{_money(operating * (1 - OPERATING_SHORTFALL), currency)}, "
                f"{_percent(OPERATING_SHORTFALL)} below the {_money(operating, currency)} "
                f"reported to {period}.",
                source,
            )
        )
    debt, assets = _number(trailing.get("debt")), _number(trailing.get("assets"))
    if debt is not None and assets:
        ratio = debt / assets
        items.append(
            _bullet(
                registry,
                f"Debt rises above {_percent(ratio + LEVERAGE_INCREASE)} of assets from "
                f"{_percent(ratio)} at {period}.",
                source,
            )
        )
    low = _estimate(estimates, "eps", "+1y", "low")
    if low is not None:
        unit = estimates.get("currency") or currency
        items.append(
            _bullet(
                registry,
                f"Reported EPS for +1y falls below the low consensus estimate of "
                f"{_price(low, unit)}.",
                estimates.get("source_id"),
            )
        )
    return _kept(items)


def _catalysts(estimates, events, as_of, endpoint, registry):
    found, items = {}, []
    scheduled = []
    if isinstance(estimates, Mapping):
        source = estimates.get("source_id")
        for value in estimates.get("earnings_dates") or []:
            scheduled.append(("earnings", "Next scheduled earnings date", _day(value), source))
        scheduled.append(
            (
                "ex_dividend",
                "Ex-dividend date",
                _day(estimates.get("ex_dividend_date")),
                source,
            )
        )
    for row in events:
        if row.get("kind") == "earnings_date":
            scheduled.append(
                (
                    "earnings",
                    "Next scheduled earnings date",
                    _day(row.get("event_date")),
                    row.get("source_id"),
                )
            )
    for kind, description, target, source in scheduled:
        if target is None or not as_of <= target <= endpoint or source not in registry:
            continue
        existing = found.get((kind, target))
        if existing:
            if source not in existing["source_ids"]:
                existing["source_ids"].append(source)
            continue
        entry = {
            "description": description,
            "target_date": target.isoformat(),
            "source_ids": [source],
        }
        found[(kind, target)] = entry
        items.append(entry)
    return sorted(items, key=lambda item: (item["target_date"], item["description"]))


def _facts(security, latest, estimates, ttm, registry):
    sid = security.get("security_id")
    trailing = ttm if isinstance(ttm, Mapping) else {}
    currency = trailing.get("currency")
    period = trailing.get("period_end")
    candidates = []
    if latest:
        candidates.append(
            (
                "price_close",
                _number(latest.get("close")),
                latest.get("currency"),
                latest.get("date"),
                latest.get("source_id"),
            )
        )
    for field in (
        "revenue",
        "operating_income",
        "net_income",
        "operating_cash_flow",
        "capex",
        "assets",
        "debt",
        "cash",
    ):
        candidates.append(
            (
                f"ttm_{field}",
                _number(trailing.get(field)),
                currency,
                period,
                trailing.get("source_id"),
            )
        )
    if isinstance(estimates, Mapping):
        estimate_source = estimates.get("source_id")
        received = estimates.get("received_at")
        unit = estimates.get("currency")
        for period_key, label in (("0y", "eps_0y"), ("+1y", "eps_next_year")):
            for field in ("avg", "low", "high"):
                candidates.append(
                    (
                        f"{label}_{field}",
                        _estimate(estimates, "eps", period_key, field),
                        unit,
                        received,
                        estimate_source,
                    )
                )
            candidates.append(
                (
                    f"{label}_analysts",
                    _estimate(estimates, "eps", period_key, "analysts"),
                    "analysts",
                    received,
                    estimate_source,
                )
            )
        targets = estimates.get("price_targets")
        if isinstance(targets, Mapping):
            candidates.append(
                ("price_target_mean", _number(targets.get("mean")), unit, received, estimate_source)
            )
    facts = []
    for field, value, units, as_of, source_id in candidates:
        if value is None or not units or not as_of or source_id not in registry:
            continue
        facts.append(
            {
                "id": f"{sid}:{field}",
                "field": field,
                "value": value,
                "units": units,
                "source_id": source_id,
                "as_of": as_of,
                "origin": "extracted",
                "review_status": "draft",
            }
        )
    return facts


def _limitations(estimates, ttm, latest, previous, registry):
    notes = [TEMPLATE_LIMITATION, ADAPTER_LIMITATION]
    if isinstance(estimates, Mapping):
        notes.append(
            "Consensus figures are external estimates of an uncertain outcome, not observations."
        )
    else:
        notes.append(
            "Consensus estimates were unavailable; market expectations are not reported here."
        )
    if not isinstance(ttm, Mapping) or not ttm:
        notes.append(
            "Trailing twelve-month statements were unavailable; financial developments are "
            "not reported here."
        )
    if latest is None:
        notes.append("No retained price was available; price changes are not reported here.")
    if not isinstance(previous, Mapping) or not previous.get("as_of"):
        notes.append(
            "No previous review was supplied; changes are reported over the retained data "
            "window only."
        )
    if not registry:
        notes.append("No source with a known receipt was retained; every bullet was withheld.")
    return notes


def build_brief(
    security: Mapping,
    *,
    prices,
    estimates,
    ttm,
    events,
    previous,
    fx_note,
    as_of: str | None = None,
    generated_at: str | None = None,
    horizon_months: int = 12,
    news_limit: int = NEWS_BULLET_LIMIT,
) -> dict:
    """Assemble a dated, fully cited brief for one security.

    `as_of` defaults to the latest retained price date, `generated_at` to now.
    Catalysts are limited to dated events between `as_of` and `as_of` plus
    `horizon_months`. Only sources whose receipt timestamp is known are retained,
    and a bullet that would otherwise be uncited is dropped.
    """
    security = security if isinstance(security, Mapping) else {}
    sid = security.get("security_id")
    price_rows = [row for row in _rows(prices) if row.get("security_id") in (sid, None)]
    event_rows = [row for row in _rows(events) if row.get("security_id") in (sid, None)]
    dated = [row for row in price_rows if _day(row.get("date")) and _number(row.get("close"))]
    latest = max(dated, key=lambda row: row["date"]) if dated else None
    stamp = generated_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    review_date = _day(as_of) or (_day(latest["date"]) if latest else None) or _day(stamp[:10])
    registry: dict[str, dict] = {}
    _register_all(registry, price_rows, estimates, ttm, event_rows, previous)
    thesis, counter_thesis = _thesis(estimates, ttm, latest, registry)
    endpoint = _endpoint(review_date, horizon_months) if review_date else None
    return {
        "security_id": sid,
        "name": _text(security.get("name")),
        "generated_at": stamp,
        "as_of": review_date.isoformat() if review_date else None,
        "horizon_months": horizon_months,
        "method_version": METHOD_VERSION,
        "adapter_version": _adapter_version(),
        "changes_since_previous_review": _changes(
            latest, estimates, event_rows, previous, fx_note, registry, news_limit
        ),
        "key_financial_developments": _financials(ttm, registry),
        "market_expectations": _expectations(estimates, registry),
        "thesis": thesis,
        "counter_thesis": counter_thesis,
        "catalysts": _catalysts(estimates, event_rows, review_date, endpoint, registry)
        if endpoint
        else [],
        "invalidation_conditions": _invalidation(estimates, ttm, registry),
        "facts": _facts(security, latest, estimates, ttm, registry),
        "sources": [registry[key] for key in sorted(registry)],
        "limitations": _limitations(estimates, ttm, latest, previous, registry),
        "basis": BASIS,
    }
