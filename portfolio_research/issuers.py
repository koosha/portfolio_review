"""SEC issuer identifiers for US listings from the public company ticker list.

The list maps exchange tickers to Central Index Keys. It identifies an issuer only for
US-listed symbols; other listings keep a listing-scoped issuer id chosen elsewhere.
Failures leave no lookup and a warning, never a guessed issuer.
"""

from __future__ import annotations

SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_TICKERS_PROVIDER = "sec_tickers"
SEC_TICKERS_KEY = "company_tickers"
US_EXCHANGES = frozenset({"NMS", "NYQ", "NGM", "NCM", "ASE", "PCX", "BTS"})
MAX_CIK = 9_999_999_999


class TickerListUnavailable(LookupError):
    """The SEC answer contained no usable ticker records."""


def ticker_key(symbol):
    """Normalize share-class separators so ``BRK.B`` and ``BRK-B`` compare equal."""
    if not isinstance(symbol, str) or not symbol.strip():
        return None
    return symbol.strip().upper().replace(".", "-")


def _cik(value):
    if type(value) is int:
        number = value
    elif isinstance(value, str) and value.strip().isdigit():
        number = int(value.strip())
    else:
        return None
    return number if 1 <= number <= MAX_CIK else None


def _ticker_index(payload):
    """Build ``ticker -> cik``; tickers claimed by different CIKs are left out."""
    if not isinstance(payload, dict):
        raise TickerListUnavailable("SEC ticker list is not an object")
    index, conflicts = {}, set()
    for record in payload.values():
        if not isinstance(record, dict):
            continue
        key, cik = ticker_key(record.get("ticker")), _cik(record.get("cik_str"))
        if key is None or cik is None:
            continue
        if index.setdefault(key, cik) != cik:
            conflicts.add(key)
    for key in conflicts:
        del index[key]
    if not index:
        raise TickerListUnavailable("SEC ticker list has no usable records")
    return index


def _is_us_listing(symbol, exchange):
    return exchange in US_EXCHANGES or "." not in symbol


def _fetch(config):
    from portfolio_lab import providers

    agent = config.get("data", {}).get("sec_user_agent") or ""
    arguments = (SEC_TICKERS_URL, agent) if agent.strip() else (SEC_TICKERS_URL,)
    payload, received_at = providers._fetch_json(*arguments)
    index = _ticker_index(payload)
    providers._archive(
        config,
        SEC_TICKERS_PROVIDER,
        SEC_TICKERS_KEY,
        payload,
        SEC_TICKERS_URL,
        received_at,
        point_in_time="current_ticker_list; not a historical vintage",
    )
    return index


def _cached(config):
    from portfolio_lab import providers

    payload, _, _ = providers._cached(config, SEC_TICKERS_PROVIDER, SEC_TICKERS_KEY)
    return _ticker_index(payload)


def _warn(issues, code, message, exc):
    from portfolio_lab.providers import _error_label

    label = _error_label(exc)
    issues.append(
        {
            "code": code,
            "severity": "warning",
            "message": message.format(label=label),
            "provider": SEC_TICKERS_PROVIDER,
            "detail": label,
        }
    )


def sec_issuer_lookup(config, *, refresh, issues):
    """Return ``meta -> "cik:##########" | None`` for US listings, or ``None`` on failure.

    With ``refresh`` the SEC list is fetched and archived; a failed refresh falls back to
    the cached list with a warning. Without it only the cache is read.
    """
    index, refresh_error = None, None
    if refresh:
        try:
            index = _fetch(config)
        except Exception as exc:  # Network, HTTP and parse failures are all non-fatal.
            refresh_error = exc
    if index is None:
        try:
            index = _cached(config)
        except Exception as exc:  # A missing or damaged cache means no issuer lookup.
            _warn(
                issues,
                "SEC_TICKERS_UNAVAILABLE",
                "SEC ticker list is unavailable ({label}); issuers stay listing-scoped.",
                refresh_error if refresh else exc,
            )
            return None
        if refresh:
            _warn(
                issues,
                "SEC_TICKERS_REFRESH_FAILED",
                "SEC ticker list refresh failed ({label}); the cached list was used.",
                refresh_error,
            )

    def lookup(meta):
        if not isinstance(meta, dict):
            return None
        symbol = meta.get("symbol") or meta.get("ticker")
        key = ticker_key(symbol)
        if key is None or not _is_us_listing(symbol, meta.get("exchange")):
            return None
        cik = index.get(key)
        return None if cik is None else f"cik:{cik:010d}"

    return lookup
