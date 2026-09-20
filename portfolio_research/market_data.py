"""Versioned Yahoo market-data adapter for automatic company research.

Every function takes one security and returns records that carry their own receipt
timestamps, source ids and adapter version. A provider failure is confined to the
security it happened to: the failure becomes an issue and the other securities
proceed. Live calls are bounded by a per-call timeout, a bounded retry and a polite
minimum interval, and an archived payload younger than ``data.provider_refresh_hours``
is reused instead of re-fetched unless ``force`` is set.

Provider text (news titles, summaries, filing titles) is data, never instruction: it
is length-bounded, stripped of control characters and carried with an explicit
classification. Consensus figures are external estimates, not established outcomes.

Reading a live response lives in ``market_payloads``, mapping an archived response to
records in ``market_records``, and the shared vocabulary in ``market_values``.

Adapter contract checked against yfinance 1.7.0: https://github.com/ranaroussi/yfinance
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import pandas as pd

from .market_payloads import (
    _estimates_payload,
    _funds_payload,
    _news_payload,
    _price_payload,
    _profile_payload,
    _statement_payload,
)
from .market_records import (
    _action_events,
    _cik,
    _filing_events,
    _news_events,
    _price_rows,
    _schedule_events,
    _statement_rows,
    _ttm_row,
    fund_rows,
)
from .market_values import (
    ADAPTER_VERSION,
    ESTIMATE_LABEL,
    _issue,
    _number,
    available_at,
    domicile_code,
    equity_type_label,
    listing_symbol,
    sector_label,
    security_id,
)

__all__ = [
    "ADAPTER_VERSION",
    "available_at",
    "domicile_code",
    "equity_type_label",
    "estimates",
    "fund_disclosure",
    "listing_symbol",
    "news_and_filings",
    "price_history",
    "sector_label",
    "security_id",
    "security_profile",
    "statements",
]

PRICES_PROVIDER = "yahoo_prices"
STATEMENTS_PROVIDER = "yahoo_statements"
ESTIMATES_PROVIDER = "yahoo_estimates"
FUNDS_PROVIDER = "yahoo_funds"
NEWS_PROVIDER = "yahoo_news"
PROFILE_PROVIDER = "yahoo_profile"
QUOTE_URL = "https://finance.yahoo.com/quote/"
MAX_ATTEMPTS = 2
MAX_EVENTS = 200
# Filings are unbounded at the provider; without their own cap they would crowd every
# news item and every recent filing out of the merged event window.
MAX_FILINGS = 100
_LAST_CALL: list[float] = []


def _now() -> str:
    from portfolio_lab.providers import _now as provider_now

    return provider_now()


def _pace(config):
    """Wait out the configured minimum interval between live provider calls."""
    interval = _number(config["data"].get("provider_min_interval_seconds")) or 0.0
    if interval <= 0:
        return
    if _LAST_CALL:
        remaining = interval - (time.monotonic() - _LAST_CALL[0])
        if remaining > 0:
            time.sleep(remaining)
    _LAST_CALL[:] = [time.monotonic()]


def _live(config, provider, security, fetch, *, issues):
    """One archived-ready payload from the provider, or ``None`` with an issue recorded."""
    from portfolio_lab.providers import ProviderShapeError, _error_label

    sid, symbol = security_id(security), listing_symbol(security)
    try:
        import yfinance as yf
    except ImportError:
        _issue(
            issues,
            "PROVIDER_DEPENDENCY_MISSING",
            sid,
            "Install optional yfinance to refresh market data; cached payloads remain usable.",
            "error",
        )
        return None
    timeout = _number(config["data"].get("provider_timeout_seconds")) or 30.0
    for attempt in range(MAX_ATTEMPTS):
        _pace(config)
        answered, outcome = threading.Event(), {}

        def call(box=outcome, done=answered):
            try:
                box["value"] = fetch(yf.Ticker(symbol))
            except Exception as exc:  # Reported on the calling thread, never here.
                box["error"] = exc
            finally:
                done.set()

        # A daemon worker, not a pooled one: an abandoned provider call must not be
        # joined at interpreter exit, so a slow symbol stalls neither the run nor the
        # process it runs in.
        threading.Thread(target=call, name=f"{provider}-fetch", daemon=True).start()
        if not answered.wait(timeout):
            _issue(
                issues,
                "PROVIDER_TIMEOUT",
                sid,
                f"{provider} did not answer within {timeout:g} s",
            )
            return None
        if "error" not in outcome:
            return outcome["value"]
        error = outcome["error"]
        if isinstance(error, ProviderShapeError):
            # The response arrived and no longer carries what this adapter reads. Asking
            # again cannot change that, so it is reported once, in the reader's own words.
            _issue(issues, "PROVIDER_SHAPE_UNRECOGNIZED", sid, _error_label(error))
            return None
        if attempt + 1 == MAX_ATTEMPTS:  # Provider objects fail lazily and in many ways.
            _issue(issues, "PROVIDER_FETCH_FAILED", sid, _error_label(error))
    return None


def _fresh(received_at, config) -> bool:
    hours = _number(config["data"].get("provider_refresh_hours"))
    if hours is None:
        return False
    try:
        received = datetime.fromisoformat(str(received_at))
    except (TypeError, ValueError):
        return False
    if received.tzinfo is None:
        return False
    return datetime.fromisoformat(_now()) - received < timedelta(hours=hours)


def _cache(config, provider, key):
    from portfolio_lab.providers import _cached

    try:
        return _cached(config, provider, key)
    except Exception:  # A missing or damaged cache is simply no cached answer.
        return None


def _obtain(config, provider, security, fetch, *, refresh, issues, force=False):
    """``(payload, received_at, source)`` from the cache or the provider, or ``None``.

    Without ``refresh`` only the cache answers. With ``refresh`` a cached payload younger
    than ``data.provider_refresh_hours`` is reused unless ``force`` is set, and a failed
    refresh falls back to whatever is cached rather than losing the security.
    """
    from portfolio_lab.providers import _archive, _error_label

    sid, symbol = security_id(security), listing_symbol(security)
    if sid is None or symbol is None:
        _issue(issues, "INVALID_LISTING_SYMBOL", sid, "No exact provider symbol for this security")
        return None
    cached = _cache(config, provider, sid)
    if cached is not None and not force and (not refresh or _fresh(cached[1], config)):
        return cached
    if not refresh:
        _issue(
            issues,
            "PROVIDER_CACHE_MISSING",
            sid,
            f"No cached {provider} payload; run with refresh to fetch.",
        )
        return None
    payload = _live(config, provider, security, fetch, issues=issues)
    if payload is None:
        return cached
    received_at = _now()
    try:
        source = _archive(
            config,
            provider,
            sid,
            payload,
            QUOTE_URL + quote(symbol),
            received_at,
            adapter_version=ADAPTER_VERSION,
            symbol=symbol,
            point_in_time="current_retrieval; not a historical vintage",
        )
    except Exception as exc:  # Serialization and disk failures end one capability, not the run.
        _issue(issues, "PROVIDER_ARCHIVE_FAILED", sid, _error_label(exc))
        return cached
    return payload, received_at, source


def price_history(config, security, *, refresh, issues, as_of, force=False) -> dict | None:
    """Daily closes, adjusted closes and corporate actions for one listing."""
    years = int(_number(config["data"].get("lookback_years")) or 5)
    start = (pd.Timestamp(as_of) - pd.DateOffset(years=years)).date().isoformat()
    end = (pd.Timestamp(as_of) + pd.Timedelta(days=1)).date().isoformat()
    obtained = _obtain(
        config,
        PRICES_PROVIDER,
        security,
        lambda ticker: _price_payload(ticker, start, end),
        refresh=refresh,
        issues=issues,
        force=force,
    )
    if obtained is None:
        return None
    payload, received_at, source = obtained
    sid = security_id(security)
    prices, actions, quote_currency, major, factor = _price_rows(
        sid, payload, received_at, source["source_id"], issues
    )
    metadata = payload.get("metadata") or {}
    return {
        "security_id": sid,
        "symbol": listing_symbol(security),
        "currency": major,
        "quote_currency": quote_currency,
        "quote_unit_factor": factor,
        "exchange": metadata.get("exchange"),
        "instrument_type": metadata.get("instrument_type"),
        "timezone": metadata.get("timezone"),
        "prices": prices,
        "actions": actions,
        "received_at": received_at,
        "source_id": source["source_id"],
        "adapter_version": ADAPTER_VERSION,
    }


def statements(config, security, *, refresh, issues, as_of, force=False) -> dict | None:
    """Annual, quarterly and trailing-twelve-month fundamentals for one security."""
    obtained = _obtain(
        config,
        STATEMENTS_PROVIDER,
        security,
        _statement_payload,
        refresh=refresh,
        issues=issues,
        force=force,
    )
    if obtained is None:
        return None
    payload, received_at, source = obtained
    sid = security_id(security)
    lag_days = _number(config["data"].get("assumed_publication_lag_days")) or 90
    shared = {"received_at": received_at, "source_id": source["source_id"], "lag_days": lag_days}
    annual = _statement_rows(sid, payload, "annual", **shared)
    quarterly = _statement_rows(sid, payload, "quarterly", **shared)
    ttm = _ttm_row(sid, quarterly, issues=issues)
    return {
        "security_id": sid,
        "currency": payload.get("currency"),
        "annual": annual,
        "quarterly": quarterly,
        "ttm": ttm,
        "rows": [*annual, *quarterly, *([ttm] if ttm else [])],
        "filings": payload.get("filings") or [],
        "received_at": received_at,
        "source_id": source["source_id"],
        "adapter_version": ADAPTER_VERSION,
    }


def estimates(config, security, *, refresh, issues, as_of, force=False) -> dict | None:
    """Consensus EPS and revenue estimates, price targets and recommendation counts.

    These are other people's expectations. They are labelled as external estimates and
    are never treated as an established outcome.
    """
    obtained = _obtain(
        config,
        ESTIMATES_PROVIDER,
        security,
        _estimates_payload,
        refresh=refresh,
        issues=issues,
        force=force,
    )
    if obtained is None:
        return None
    payload, received_at, source = obtained
    currency = next(
        (
            period.get("currency")
            for period in payload.get("eps", {}).values()
            if period.get("currency")
        ),
        None,
    )
    return {
        "security_id": security_id(security),
        "currency": currency,
        "eps": payload.get("eps", {}),
        "revenue": payload.get("revenue", {}),
        "price_targets": payload.get("price_targets", {}),
        "recommendations": payload.get("recommendations"),
        "earnings_dates": payload.get("earnings_dates", []),
        "ex_dividend_date": payload.get("ex_dividend_date"),
        "dividend_date": payload.get("dividend_date"),
        "received_at": received_at,
        "source_id": source["source_id"],
        "adapter_version": ADAPTER_VERSION,
        "label": ESTIMATE_LABEL,
    }


def fund_disclosure(
    config, security, *, refresh, issues, as_of, issuer_lookup=None, force=False
) -> dict | None:
    """Top holdings, sector weights and the fund overview for an ETF or mutual fund.

    The provider publishes an undated snapshot of the largest holdings only; the rows
    carry that basis and never pretend to describe the whole fund.
    """
    obtained = _obtain(
        config,
        FUNDS_PROVIDER,
        security,
        _funds_payload,
        refresh=refresh,
        issues=issues,
        force=force,
    )
    if obtained is None:
        return None
    payload, received_at, source = obtained
    sid = security_id(security)
    holdings, sectors, unmapped = fund_rows(
        sid, payload, received_at, source["source_id"], issuer_lookup
    )
    return {
        "security_id": sid,
        "holdings": holdings,
        "sectors": sectors,
        "unmapped_sectors": unmapped,
        "fund_overview": payload.get("fund_overview") or {},
        "asset_classes": payload.get("asset_classes") or {},
        "received_at": received_at,
        "source_id": source["source_id"],
        "adapter_version": ADAPTER_VERSION,
    }


def news_and_filings(
    config, security, *, refresh, issues, as_of, actions=None, force=False
) -> dict | None:
    """Dated events for one security: filings and corporate actions, and news as opinion.

    News text is third-party opinion and is classified as such. Filing, dividend, split
    and scheduled-earnings rows are issuer facts.
    """
    obtained = _obtain(
        config,
        NEWS_PROVIDER,
        security,
        _news_payload,
        refresh=refresh,
        issues=issues,
        force=force,
    )
    if obtained is None:
        return None
    payload, received_at, source = obtained
    sid = security_id(security)
    shared = {
        "received_at": received_at,
        "source_id": source["source_id"],
        "adapter_version": ADAPTER_VERSION,
    }
    limit = int(_number(config["data"].get("news_limit")) or 20)
    filings = _filing_events(sid, payload, shared)
    dropped = max(0, len(filings) - MAX_FILINGS)
    events = [
        *_news_events(sid, payload, shared, limit),
        *filings[:MAX_FILINGS],
        *_schedule_events(sid, payload, shared),
        *_action_events(sid, actions, shared),
    ]
    events.sort(key=lambda event: (event["event_date"], event["kind"], event["title"] or ""))
    # Truncate at the old end: the window is about what changed, so the most recent
    # rows are the ones a review needs.
    dropped += max(0, len(events) - MAX_EVENTS)
    if dropped:
        _issue(
            issues,
            "EVENTS_TRUNCATED",
            sid,
            f"{dropped} older events were dropped; the window keeps the most recent.",
            "info",
        )
    return {
        "security_id": sid,
        "events": events[-MAX_EVENTS:],
        "received_at": received_at,
        "source_id": source["source_id"],
        "adapter_version": ADAPTER_VERSION,
    }


def security_profile(
    config, security, *, refresh, issues, as_of, issuer_lookup=None, force=False
) -> dict | None:
    """Sector, industry, size, domicile and equity type for one security.

    Market capitalisation is a current snapshot: it is attached only when the review's
    ``as_of`` is today or later, never backdated into a historical review.
    """
    from portfolio_lab.providers import _day

    obtained = _obtain(
        config,
        PROFILE_PROVIDER,
        security,
        _profile_payload,
        refresh=refresh,
        issues=issues,
        force=force,
    )
    if obtained is None:
        return None
    payload, received_at, source = obtained
    metadata = payload.get("metadata") or {}
    symbol = listing_symbol(security)
    kind, reason = equity_type_label(metadata.get("instrument_type"), symbol)
    current = _day(as_of) >= _day(datetime.now(timezone.utc))
    issuer_id = None
    if issuer_lookup is not None:
        try:
            issuer_id = issuer_lookup({"symbol": symbol, "exchange": metadata.get("exchange")})
        except Exception:  # A broken lookup leaves the issuer unidentified.
            issuer_id = None
    return {
        "security_id": security_id(security),
        "name": payload.get("name"),
        "sector": sector_label(payload.get("sector")),
        "industry": payload.get("industry"),
        "market_cap": payload.get("market_cap") if current else None,
        "market_cap_as_of": as_of if current and payload.get("market_cap") else None,
        "market_cap_received_at": received_at if current else None,
        "market_cap_source_id": source["source_id"] if current else None,
        "shares_outstanding": payload.get("shares_outstanding"),
        "financial_currency": payload.get("financial_currency"),
        "issuer_id": issuer_id,
        "cik": _cik(issuer_id),
        "domicile": domicile_code(payload.get("country")),
        "equity_type": kind,
        "equity_type_reason": reason,
        "exchange": metadata.get("exchange"),
        "instrument_type": metadata.get("instrument_type"),
        "received_at": received_at,
        "source_id": source["source_id"],
        "adapter_version": ADAPTER_VERSION,
    }
