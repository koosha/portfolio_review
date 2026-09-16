"""Dated FX observations from Bank of Canada Valet and Yahoo FX behind the provider cache.

Bank of Canada series ``FX{XXX}CAD`` are indicative CAD per 1 unit of XXX, published
once per business day around 16:30 America/Toronto; holidays are simply absent.
USD crosses for other currencies are derived explicitly through CAD on the same
date. Yahoo ``{BASE}{QUOTE}=X`` daily closes are quote per base. Live requests
happen only on refresh; otherwise cached raw responses are re-read. Failures leave
no observations and an issue; they never raise and never expose request URLs.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from decimal import Decimal, localcontext
from zoneinfo import ZoneInfo

from .fx import PRECISION, FxTable, _iso_date, _major, _unit, validate_observation

_DATE_KEY = re.compile(r"\d{4}-\d{2}-\d{2}")

VALET_OBSERVATIONS = "https://www.bankofcanada.ca/valet/observations/"
USER_AGENT = "PortfolioReview/0.2 personal-research"
BOC_CACHE = "bank_of_canada_fx"
YAHOO_CACHE = "yahoo_fx"
BOC_TIMEZONE = ZoneInfo("America/Toronto")
BOC_PUBLICATION = time(16, 30)
# The daily FX_RATES_DAILY group Valet publishes. A currency outside it (DKK, ILS)
# has no FX{XXX}CAD series, and asking for one answers 404 for the whole request.
VALET_CURRENCIES = frozenset(
    "AUD BRL CHF CNY EUR GBP HKD IDR INR JPY KRW MXN MYR NOK NZD PEN "
    "RUB SAR SEK SGD THB TRY TWD USD VND ZAR".split()
)
ANCHOR_SERIES = "FXUSDCAD"
_LABELS = {"bank_of_canada": "Bank of Canada", "yahoo": "Yahoo"}
_CACHES = {"bank_of_canada": BOC_CACHE, "yahoo": YAHOO_CACHE}


def _currencies(values) -> list[str]:
    if isinstance(values, (str, bytes)) or values is None:
        raise ValueError("currencies must be a list of currency codes")
    return sorted({_unit(value)[0] for value in values})


def _window(start_date, end_date) -> tuple[date, date]:
    start = _iso_date(start_date, "start_date")
    end = _iso_date(end_date, "end_date")
    if end < start:
        raise ValueError("end_date cannot be before start_date")
    return start, end


def _failure(issues: list, provider: str, exc: Exception, **extra) -> None:
    from portfolio_lab.providers import _error_label

    detail = _error_label(exc)
    label = _LABELS[provider]
    if isinstance(exc, FileNotFoundError):
        message = f"No cached {label} FX observations for these dates; refresh market data."
    else:
        message = f"{label} FX observations are unavailable ({detail})."
    issues.append(
        {
            "severity": "warning",
            "code": "FX_PROVIDER_FAILED",
            "provider": provider,
            **extra,
            "detail": detail,
            "message": message + " Affected amounts stay unconverted.",
        }
    )


def _publication(day: str) -> str:
    moment = datetime.combine(date.fromisoformat(day), BOC_PUBLICATION, tzinfo=BOC_TIMEZONE)
    return moment.isoformat()


def _valet_records(
    payload, series: list[str], source_id: str, received_at: str, window=None
) -> list[dict]:
    if not isinstance(payload, dict) or not isinstance(payload.get("observations"), list):
        raise ValueError("Unexpected Valet observations payload")
    records = []
    for row in payload["observations"]:
        if not isinstance(row, dict):
            raise ValueError("Unexpected Valet observation")
        day = _iso_date(row.get("d"), "Valet observation date").isoformat()
        if window and not window[0] <= day <= window[1]:
            continue  # A reused archive may cover more days than this window asked for.
        records += _valet_day(row, day, series, source_id, received_at)
    return records


def _valet_day(row, day: str, series: list[str], source_id: str, received_at: str) -> list[dict]:
    """One observation date: the requested series plus their derived USD crosses."""
    common = {
        "date": day,
        "provider": "bank_of_canada",
        "received_at": received_at,
        "published_at": _publication(day),
    }
    records, rates = [], {}
    for name in series:
        cell = row.get(name)
        value = cell.get("v") if isinstance(cell, dict) else None
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        base = name[2:5]
        record = validate_observation(
            {
                **common,
                "pair": base + "CAD",
                "base": base,
                "quote": "CAD",
                "rate": value,
                "source_id": source_id,
            }
        )
        rates[base] = record["rate"]
        records.append(record)
    if "USD" not in rates:
        return records
    for base in sorted(set(rates) - {"USD", "CAD"}):
        with localcontext() as context:
            context.prec = PRECISION
            cross = format(Decimal(rates[base]) / Decimal(rates["USD"]), "f")
        records.append(
            validate_observation(
                {
                    **common,
                    "pair": base + "USD",
                    "base": base,
                    "quote": "USD",
                    "rate": cross,
                    "source_id": f"{source_id}#FX{base}CAD/FXUSDCAD",
                    "derived_via": "CAD",
                }
            )
        )
    return records


def _unsupported(issues: list, currencies: list[str]) -> None:
    for code in currencies:
        issues.append(
            {
                "severity": "warning",
                "code": "FX_CURRENCY_UNSUPPORTED",
                "provider": "bank_of_canada",
                "currency": code,
                "message": (
                    f"The Bank of Canada publishes no daily {code} rate; another configured "
                    "FX provider must cover it."
                ),
            }
        )


def _valet_key(start, end, series: list[str]) -> str:
    return f"{start}_{end}_{'-'.join(series)}"


def _valet_url(series: list[str], start, end) -> str:
    return f"{VALET_OBSERVATIONS}{','.join(series)}/json?start_date={start}&end_date={end}"


def _valet_refresh(config, series: list[str], start, end):
    """Fetch one Valet request and archive the raw response under its window key."""
    from portfolio_lab import providers

    url = _valet_url(series, start, end)
    payload, received_at = providers._fetch_json(url, USER_AGENT)
    source = providers._archive(
        config,
        BOC_CACHE,
        _valet_key(start, end, series),
        payload,
        url,
        received_at,
        direction="CAD per 1 unit of base",
        published_by="16:30 America/Toronto business days",
    )
    return payload, received_at, source


def _valet_live(config, series: list[str], start, end):
    """Refresh, keeping the USD anchor alone when a multi-series request is rejected.

    Valet answers 404 for the whole request when any one series is unknown to it, so a
    series list that has drifted from what Valet publishes must not cost us USDCAD too.
    """
    try:
        return series, *_valet_refresh(config, series, start, end)
    except Exception as exc:
        if series == [ANCHOR_SERIES]:
            raise
        try:
            return [ANCHOR_SERIES], *_valet_refresh(config, [ANCHOR_SERIES], start, end)
        except Exception:
            raise exc from None


def _key_window(key: str):
    """``(start, end, tail)`` of a cache key that carries an adjacent date window."""
    parts = key.split("_")
    dates = [i for i, part in enumerate(parts) if _DATE_KEY.fullmatch(part)]
    if len(dates) < 2 or dates[1] != dates[0] + 1:
        return None
    first, second = dates[0], dates[1]
    return parts[first], parts[second], "_".join(parts[:first] + parts[second + 1 :])


def _archived_windows(config, provider: str):
    """``(key, start, end, tail, received_at)`` of every archived response, newest first.

    One scan answers for every pair of a presentation: an open cache scan is reused
    rather than re-reading the provider directory once per pair.
    """
    from .provider_cache import newest_sources, scanned_sources

    index = scanned_sources(provider)
    if index is None:
        index = newest_sources(config, provider)
    windows = []
    for key, source in index.items():
        parsed = _key_window(key)
        if parsed is None:
            continue
        start, end, tail = parsed
        windows.append((key, start, end, tail, source.get("received_at") or ""))
    windows.sort(key=lambda row: (row[2], row[4]), reverse=True)
    return windows


def _distinct(records: list[dict]) -> list[dict]:
    """One observation per pair and date; archives read newest first, so the first wins."""
    seen, kept = set(), []
    for record in records:
        key = (record["pair"], record["date"])
        if key not in seen:
            seen.add(key)
            kept.append(record)
    return kept


def _uncovered(ranges, window) -> list[tuple[str, str]]:
    """The parts of ``ranges`` this archived window leaves unanswered."""
    start, end = window
    remaining = []
    for first, last in ranges:
        if end < first or start > last:
            remaining.append((first, last))
            continue
        if first < start:
            remaining.append((first, _shift(start, -1)))
        if last > end:
            remaining.append((_shift(end, 1), last))
    return remaining


def _shift(day: str, days: int) -> str:
    return (date.fromisoformat(day) + timedelta(days=days)).isoformat()


def _covering_keys(config, provider: str, start, end, score) -> list[str]:
    """Archived keys that together answer ``[start, end]``, richest and newest first.

    Coverage decides, not the newest window end: a narrow recent archive answers only
    its own days, and an older wide one still supplies the days it alone holds.
    """
    candidates = []
    for key, archived_start, archived_end, tail, _ in _archived_windows(config, provider):
        if archived_start > str(end) or archived_end < str(start):
            continue  # A window that never overlaps cannot answer for these dates.
        found = score(tail)
        if found > 0:
            candidates.append((-found, key, archived_start, archived_end))
    candidates.sort(key=lambda row: row[0])
    needed, chosen = [(str(start), str(end))], []
    for _, key, archived_start, archived_end in candidates:
        if not needed:
            break
        remaining = _uncovered(needed, (archived_start, archived_end))
        if remaining != needed:
            chosen.append(key)
            needed = remaining
    return chosen


def _cached_valet(config, series: list[str], start, end) -> list[tuple]:
    """The archived response for this window, else every overlapping one that covers it."""
    from .provider_cache import cached_response

    key = _valet_key(start, end, series)
    try:
        return [cached_response(config, BOC_CACHE, key)]
    except FileNotFoundError:
        wanted = set(series)
        found = _covering_keys(
            config,
            BOC_CACHE,
            start,
            end,
            score=lambda tail: len(wanted & set(tail.split("-"))),
        )
        if not found:
            raise
        return [cached_response(config, BOC_CACHE, key) for key in found]


def bank_of_canada_observations(
    config, currencies, start_date, end_date, *, refresh, issues
) -> list[dict]:
    """Valet ``FX{XXX}CAD`` observations (plus ``FXUSDCAD``) and derived USD crosses."""
    start, end = _window(start_date, end_date)
    wanted = [code for code in _currencies(currencies) if code != "CAD"]
    _unsupported(issues, [code for code in wanted if code not in VALET_CURRENCIES])
    supported = {f"FX{code}CAD" for code in wanted if code in VALET_CURRENCIES}
    series = sorted(supported | {ANCHOR_SERIES})
    try:
        if refresh:
            answered, payload, received_at, source = _valet_live(config, series, start, end)
            responses = [(payload, received_at, source)]
        else:
            answered, responses = series, _cached_valet(config, series, start, end)
        _unsupported(issues, [name[2:5] for name in series if name not in answered])
        window = (start.isoformat(), end.isoformat())
        records = []
        for payload, received_at, source in responses:
            records += _valet_records(payload, answered, source["source_id"], received_at, window)
        records = _distinct(records)
        if not refresh and not records:
            # A reused archive that holds nothing for these dates is a miss, not silence.
            raise FileNotFoundError("No cached observations inside this window")
        return records
    except Exception as exc:  # provider isolation: never raise, never echo the URL
        _failure(issues, "bank_of_canada", exc)
        return []


def _pairs(pairs) -> list[tuple[str, str]]:
    if isinstance(pairs, (str, bytes)) or pairs is None:
        raise ValueError("pairs must be a list of (base, quote) currency codes")
    checked = []
    for pair in pairs:
        if isinstance(pair, (str, bytes)) or len(pair) != 2:
            raise ValueError("Each FX pair must be a (base, quote) pair")
        base, quote = _major(pair[0], "base"), _major(pair[1], "quote")
        if base != quote and (base, quote) not in checked:
            checked.append((base, quote))
    return checked


def _history_payload(history) -> list[dict]:
    import pandas as pd

    if history is None or history.empty or "Close" not in history.columns:
        raise ValueError("No FX closes")
    rows = []
    for stamp, close in history["Close"].items():
        if close is None or pd.isna(close) or float(close) <= 0:
            continue
        # The index is taken as given (exchange-local date), as providers.py does.
        day = pd.Timestamp(stamp).tz_localize(None).normalize()
        rows.append({"Date": day.date().isoformat(), "Close": float(close)})
    if not rows:
        raise ValueError("No FX closes")
    return rows


def _cached_yahoo(config, base: str, quote: str, start, end) -> list[tuple]:
    """The archived closes for this window, else every overlapping archive that covers it."""
    from .provider_cache import cached_response

    pair = base + quote
    try:
        return [cached_response(config, YAHOO_CACHE, f"{pair}_{start}_{end}")]
    except FileNotFoundError:
        found = _covering_keys(config, YAHOO_CACHE, start, end, lambda tail: int(tail == pair))
        if not found:
            raise
        return [cached_response(config, YAHOO_CACHE, key) for key in found]


def _yahoo_records(
    payload, base: str, quote: str, source_id: str, received_at: str, window=None
) -> list:
    if not isinstance(payload, list):
        raise ValueError("Unexpected Yahoo FX payload")
    records = []
    for row in payload:
        if not isinstance(row, dict):
            raise ValueError("Unexpected Yahoo FX row")
        if window and not window[0] <= str(row.get("Date")) <= window[1]:
            continue  # A reused archive may cover more days than this window asked for.
        records.append(
            validate_observation(
                {
                    "pair": base + quote,
                    "base": base,
                    "quote": quote,
                    "rate": str(Decimal(str(row.get("Close")))),
                    "date": row.get("Date"),
                    "provider": "yahoo",
                    "published_at": None,
                    "received_at": received_at,
                    "source_id": source_id,
                }
            )
        )
    return records


def _yahoo_history(config, yf, base: str, quote: str, start, end):
    """Refresh one pair's daily closes and archive the raw response."""
    from portfolio_lab import providers

    ticker = f"{base}{quote}=X"
    history = yf.Ticker(ticker).history(
        start=start.isoformat(),
        end=(end + timedelta(days=1)).isoformat(),
        auto_adjust=False,
    )
    payload = _history_payload(history)
    received_at = providers._now()
    source = providers._archive(
        config,
        YAHOO_CACHE,
        f"{base}{quote}_{start}_{end}",
        payload,
        "https://finance.yahoo.com/quote/" + ticker,
        received_at,
        direction=f"{quote} per 1 {base}",
        point_in_time="current retrieval of daily closes; not an official fixing",
    )
    return payload, received_at, source


def _yahoo_dependency(issues) -> bool:
    issues.append(
        {
            "severity": "warning",
            "code": "YAHOO_DEPENDENCY_MISSING",
            "provider": "yahoo",
            "message": "Install optional yfinance to load Yahoo FX observations.",
        }
    )
    return False


def yahoo_fx_observations(config, pairs, start_date, end_date, *, refresh, issues) -> list[dict]:
    """Daily ``{BASE}{QUOTE}=X`` closes as quote-per-base observations, isolated per pair."""
    start, end = _window(start_date, end_date)
    checked = _pairs(pairs)
    if not checked:
        return []
    yf = None
    if refresh:
        try:
            import yfinance as yf
        except ImportError:
            _yahoo_dependency(issues)
            return []
    window = (start.isoformat(), end.isoformat())
    records = []
    for base, quote in checked:
        try:
            records += _pair_records(config, yf, base, quote, (start, end), window, refresh)
        except Exception as exc:  # per-pair isolation; never echo provider messages
            _failure(issues, "yahoo", exc, pair=base + quote)
    return records


def _pair_records(config, yf, base: str, quote: str, dates, window, refresh) -> list[dict]:
    """One pair's observations inside ``window``, from a refresh or the archives."""
    start, end = dates
    if refresh:
        responses = [_yahoo_history(config, yf, base, quote, start, end)]
    else:
        responses = _cached_yahoo(config, base, quote, start, end)
    records = []
    for payload, received_at, source in responses:
        records += _yahoo_records(payload, base, quote, source["source_id"], received_at, window)
    records = _distinct(records)
    if not refresh and not records:
        # A reused archive that holds nothing for these dates is a miss, not silence.
        raise FileNotFoundError("No cached closes inside this window")
    return records


def _yahoo_pairs(currencies: list[str], presentation: str) -> list[tuple[str, str]]:
    foreign = [code for code in currencies if code != presentation]
    pairs = [(code, presentation) for code in foreign]
    pairs += [(a, b) for i, a in enumerate(foreign) for b in foreign[i + 1 :]]
    return pairs


def load_fx_table(config, currencies, start_date, end_date, *, refresh, issues) -> FxTable:
    """Collect observations from ``data.fx_providers`` in order (first per pair/date wins).

    A cache-only load indexes each provider directory once, so its cost does not grow
    with the number of currency pairs a presentation needs.
    """
    from .provider_cache import scanned

    data = config["data"]
    live = bool(refresh) and data.get("mode") == "live"
    if live:
        return _fx_table(config, currencies, start_date, end_date, live=True, issues=issues)
    caches = [_CACHES[name] for name in data["fx_providers"] if name in _CACHES]
    with scanned(config, *caches):
        return _fx_table(config, currencies, start_date, end_date, live=False, issues=issues)


def _fx_table(config, currencies, start_date, end_date, *, live, issues) -> FxTable:
    data = config["data"]
    presentation = _major(config.get("mandate", {}).get("base_currency", "USD"), "base_currency")
    wanted = _currencies(currencies)
    observations: list[dict] = []
    for name in data["fx_providers"]:
        if name == "bank_of_canada":
            observations += bank_of_canada_observations(
                config, wanted, start_date, end_date, refresh=live, issues=issues
            )
        elif name == "yahoo":
            observations += yahoo_fx_observations(
                config,
                _yahoo_pairs(wanted, presentation),
                start_date,
                end_date,
                refresh=live,
                issues=issues,
            )
        else:
            raise ValueError(f"Unknown FX provider: {name}")
    return FxTable(observations, max_age_days=data["max_fx_age_days"])
