"""Listing metadata from Yahoo Finance behind the archived provider cache.

Only identity facts are kept: the quote currency and its unit, exchange, instrument
type, name and the time of the last quote. Prices and sizes are context for review,
never valuation evidence. Every lookup is isolated per symbol: a failure becomes an
issue and a missing listing, never a guessed one. Offline reads use the cache only.
"""

from __future__ import annotations

import math
import numbers
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import quote, urlencode

LISTING_PROVIDER = "yahoo_listing"
SEARCH_PROVIDER = "yahoo_search"
SYMBOL_PATTERN = re.compile(r"[A-Z0-9][A-Z0-9.\-=^]{0,19}")
CURRENCY_PATTERN = re.compile(r"[A-Za-z]{3}")
INSTRUMENT_TYPES = {
    "EQUITY": "equity",
    "ETF": "etf",
    "MUTUALFUND": "mutual_fund",
    "CURRENCY": "currency",
    "INDEX": "index",
}
DEFAULT_MAX_LISTING_AGE_DAYS = 30
MAX_TEXT_LENGTH = 200
MAX_SEARCH_TEXT = 100
MAX_SEARCH_LIMIT = 25
MAX_NUMBER_LENGTH = 128
MAX_NUMBER_EXPONENT = 128
HISTORY_FIELDS = (
    "currency",
    "exchangeName",
    "instrumentType",
    "longName",
    "shortName",
    "symbol",
    "regularMarketPrice",
    "regularMarketTime",
)
FAST_INFO_FIELDS = ("currency", "exchange", "quoteType", "lastPrice", "marketCap", "shares")
SEARCH_FIELDS = ("symbol", "shortname", "longname", "exchange", "exchDisp", "quoteType")


class ListingUnavailable(LookupError):
    """The provider answered without any listing identity facts."""


def listing_currency(code):
    """Return ``(quote_currency, major_currency, unit_factor)`` for a quote currency code.

    ``GBp``/``GBX``, ``ZAc`` and ``ILA`` are hundredths of their major unit; any other
    three-letter code is a major unit. Missing or malformed codes are all ``None``.
    """
    from .fx import MINOR_UNITS

    if not isinstance(code, str) or not CURRENCY_PATTERN.fullmatch(code):
        return None, None, None
    if code in MINOR_UNITS:
        major, factor = MINOR_UNITS[code]
        return code, major, factor
    return code.upper(), code.upper(), 1


def instrument_type(value):
    """Map a provider quote type onto the review's instrument vocabulary."""
    if not isinstance(value, str) or not value.strip():
        return None
    return INSTRUMENT_TYPES.get(value.strip().upper().replace("_", ""), "other")


def normalize_symbol(symbol):
    if not isinstance(symbol, str):
        return None
    text = symbol.strip().upper()
    return text if SYMBOL_PATTERN.fullmatch(text) else None


def _issue(issues, code, message, severity="warning", **fields):
    issues.append({"code": code, "severity": severity, "message": message, **fields})


def _text(value):
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > MAX_TEXT_LENGTH or any(ord(c) < 32 for c in text):
        return None
    return text


def _json_scalar(value):
    """Reduce a provider value to archive-safe JSON; non-finite numbers are missing."""
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, datetime):
        return _timestamp_text(value)
    if isinstance(value, numbers.Integral):
        return int(value)
    if isinstance(value, numbers.Real):
        number = float(value)
        return number if math.isfinite(number) else None
    return None


def _decimal_text(value):
    """A provider number as an exact string, or ``None`` when it is not bounded data.

    Provider payloads are untrusted: a value such as ``"9e9999999"`` is a finite
    Decimal whose formatted form is a billion characters long, so magnitude and
    digit count are bounded before anything is built from it.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        text = str(value)
        number = Decimal(text)
        if (
            len(text) > MAX_NUMBER_LENGTH
            or not number.is_finite()
            or number < 0
            or abs(number.as_tuple().exponent) > MAX_NUMBER_EXPONENT
            or number.adjusted() > MAX_NUMBER_EXPONENT
        ):
            return None
        return format(number.normalize(), "f")
    except (ArithmeticError, ValueError, TypeError):
        return None


def _timestamp_text(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        if isinstance(value, numbers.Real):
            moment = datetime.fromtimestamp(float(value), timezone.utc)
        elif isinstance(value, datetime):
            moment = value
        elif isinstance(value, str):
            moment = datetime.fromisoformat(value)
        else:
            return None
    except (OverflowError, OSError, ValueError):
        return None
    if moment.tzinfo is None:
        return None
    return datetime.fromtimestamp(moment.timestamp(), timezone.utc).isoformat()


def _is_stale(received_at, config):
    limit = config.get("data", {}).get("max_listing_age_days", DEFAULT_MAX_LISTING_AGE_DAYS)
    try:
        received = datetime.fromisoformat(received_at)
    except (TypeError, ValueError):
        return True
    if received.tzinfo is None:
        return True
    return datetime.now(timezone.utc) - received > timedelta(days=limit)


def _provider_fields(ticker, attribute, fields):
    """Read optional provider fields one at a time; return them with the first failure."""
    try:
        values = getattr(ticker, attribute)
    except Exception as exc:  # Provider objects fetch lazily and fail in many ways.
        return {}, exc
    result = {}
    for field in fields:
        try:
            value = _json_scalar(values[field])
        except Exception:  # A missing or broken field leaves that fact unknown.
            continue
        if value is not None:
            result[field] = value
    return result, None


def _has_identity(payload):
    if not isinstance(payload, dict):
        return False
    history = payload.get("history_metadata")
    fast = payload.get("fast_info")
    history = history if isinstance(history, dict) else {}
    fast = fast if isinstance(fast, dict) else {}
    return any(
        _text(value)
        for value in (
            history.get("currency"),
            history.get("exchangeName"),
            fast.get("currency"),
            fast.get("exchange"),
        )
    )


def _listing_record(config, symbol, payload, received_at, source):
    history = payload.get("history_metadata")
    fast = payload.get("fast_info")
    history = history if isinstance(history, dict) else {}
    fast = fast if isinstance(fast, dict) else {}
    quote_currency, major_currency, factor = listing_currency(history.get("currency"))
    if quote_currency is None:
        quote_currency, major_currency, factor = listing_currency(fast.get("currency"))
    price = history.get("regularMarketPrice")
    return {
        "symbol": symbol,
        "quote_currency": quote_currency,
        "quote_unit_factor": factor,
        "major_currency": major_currency,
        "exchange": _text(history.get("exchangeName")) or _text(fast.get("exchange")),
        "instrument_type": instrument_type(history.get("instrumentType"))
        or instrument_type(fast.get("quoteType")),
        "name": _text(history.get("longName")) or _text(history.get("shortName")),
        "last_quote_at": _timestamp_text(history.get("regularMarketTime")),
        "last_price": _decimal_text(fast.get("lastPrice") if price is None else price),
        "market_cap": _decimal_text(fast.get("marketCap")),
        "shares_outstanding": _decimal_text(fast.get("shares")),
        "source_id": source["source_id"],
        "received_at": received_at,
        "provider": "yahoo",
        "stale": _is_stale(received_at, config),
    }


def _lookup_failed(issues, symbol, exc):
    from portfolio_lab.providers import _error_label

    label = _error_label(exc)
    _issue(
        issues,
        "LISTING_LOOKUP_FAILED",
        f"{symbol}: listing metadata is unavailable ({label}); no listing was assumed.",
        symbol=symbol,
        provider="yahoo",
        detail=label,
    )


def _dependency_missing(issues):
    _issue(
        issues,
        "YAHOO_DEPENDENCY_MISSING",
        "Install optional yfinance to look up listing metadata; cached listings remain usable.",
        "error",
    )


def _refresh_listing(config, symbol, issues):
    from portfolio_lab import providers

    try:
        import yfinance as yf
    except ImportError:
        _dependency_missing(issues)
        return None
    try:
        ticker = yf.Ticker(symbol)
        history, history_error = _provider_fields(ticker, "history_metadata", HISTORY_FIELDS)
        fast, fast_error = _provider_fields(ticker, "fast_info", FAST_INFO_FIELDS)
        payload = {"history_metadata": history, "fast_info": fast}
        if not _has_identity(payload):
            raise history_error or fast_error or ListingUnavailable(symbol)
        received_at = providers._now()
        source = providers._archive(
            config,
            LISTING_PROVIDER,
            symbol,
            payload,
            "https://finance.yahoo.com/quote/" + quote(symbol),
            received_at,
            point_in_time="current_listing_metadata; not a historical vintage",
        )
        return _listing_record(config, symbol, payload, received_at, source)
    except Exception as exc:  # Isolate every symbol; never let one listing stop others.
        _lookup_failed(issues, symbol, exc)
        return None


def _read_cached(config, provider, key):
    """The newest archived response for a key, from an open cache scan when there is one."""
    from portfolio_lab import providers

    from .provider_cache import read_payload, scanned_source, scanning

    if not scanning(provider):
        return providers._cached(config, provider, key)
    source = scanned_source(provider, key)
    if source is None:
        raise FileNotFoundError("No compatible cached provider response")
    return read_payload(config, provider, source)


def _cached_listing(config, symbol, issues):
    try:
        payload, received_at, source = _read_cached(config, LISTING_PROVIDER, symbol)
        if not _has_identity(payload):
            raise ListingUnavailable(symbol)
        return _listing_record(config, symbol, payload, received_at, source)
    except FileNotFoundError:
        return None
    except Exception as exc:  # Any cache failure is a missing listing, never a broken view.
        _lookup_failed(issues, symbol, exc)
        return None


def listing_metadata(config, symbol, *, refresh, issues) -> dict | None:
    """Return listing identity facts for one exact provider symbol, or ``None``.

    With ``refresh`` the provider is asked first and the answer archived; a failed
    refresh falls back to the newest cached answer. Without it only the cache is read.
    Cached answers older than ``data.max_listing_age_days`` are returned with
    ``stale: True``.
    """
    key = normalize_symbol(symbol)
    if key is None:
        _issue(
            issues,
            "INVALID_LISTING_SYMBOL",
            "A listing symbol must be 1-20 letters, digits, '.', '-', '=' or '^' characters.",
        )
        return None
    record = _refresh_listing(config, key, issues) if refresh else None
    return record if record is not None else _cached_listing(config, key, issues)


def _normalize_search(text):
    if not isinstance(text, str):
        return None
    key = " ".join(text.split()).upper()
    if not key or len(key) > MAX_SEARCH_TEXT or any(ord(c) < 32 for c in key):
        return None
    return key


def _search_candidates(quotes, limit):
    candidates, seen = [], set()
    for row in quotes if isinstance(quotes, list) else []:
        symbol = normalize_symbol(row.get("symbol")) if isinstance(row, dict) else None
        if symbol is None or symbol in seen:
            continue
        seen.add(symbol)
        candidates.append(
            {
                "symbol": symbol,
                "name": _text(row.get("longname")) or _text(row.get("shortname")),
                "exchange": _text(row.get("exchange")),
                "instrument_type": instrument_type(row.get("quoteType")),
            }
        )
    return candidates[:limit]


def _search_failed(issues, key, exc):
    from portfolio_lab.providers import _error_label

    label = _error_label(exc)
    _issue(
        issues,
        "LISTING_SEARCH_FAILED",
        f"{key}: listing search is unavailable ({label}); no candidate was assumed.",
        provider="yahoo",
        detail=label,
    )


def _refresh_search(config, key, limit, issues):
    from portfolio_lab import providers

    try:
        import yfinance as yf
    except ImportError:
        _dependency_missing(issues)
        return None
    try:
        found = yf.Search(key, max_results=limit, news_count=0).quotes
        if not isinstance(found, list):
            raise ListingUnavailable(key)
        quotes = [
            {field: _json_scalar(row.get(field)) for field in SEARCH_FIELDS}
            for row in found
            if isinstance(row, dict)
        ]
        providers._archive(
            config,
            SEARCH_PROVIDER,
            key,
            quotes,
            "https://query2.finance.yahoo.com/v1/finance/search?" + urlencode({"q": key}),
            providers._now(),
            point_in_time="current_listing_search; candidates only, not an identity decision",
        )
    except Exception as exc:  # Search failures leave no candidates, never a guess.
        _search_failed(issues, key, exc)
        return None
    return quotes


def _cached_search(config, key, issues):
    try:
        payload, _, _ = _read_cached(config, SEARCH_PROVIDER, key)
    except FileNotFoundError:
        return []
    except Exception as exc:  # Any cache failure leaves no candidates, never a broken view.
        _search_failed(issues, key, exc)
        return []
    return payload


def search_listings(config, text, *, refresh, issues, limit=5) -> list[dict]:
    """Return up to ``limit`` listing candidates ``{symbol, name, exchange, instrument_type}``.

    Candidates are suggestions for an owner or an exact-match rule; they are never an
    identity decision on their own. The cache key is the whitespace-normalized,
    upper-cased search text.
    """
    if type(limit) is not int or not 1 <= limit <= MAX_SEARCH_LIMIT:
        raise ValueError(f"limit must be an integer from 1 to {MAX_SEARCH_LIMIT}.")
    key = _normalize_search(text)
    if key is None:
        _issue(
            issues,
            "INVALID_LISTING_SEARCH",
            f"Listing search text must be 1-{MAX_SEARCH_TEXT} printable characters.",
        )
        return []
    quotes = _refresh_search(config, key, limit, issues) if refresh else None
    if quotes is None:
        quotes = _cached_search(config, key, issues)
    return _search_candidates(quotes, limit)
