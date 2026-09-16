"""Dated FX observations: exact conversion, quote subunits, inversion and freshness.

Pure module (no network, no provider access). Every observation is stated as
"quote per base": pair ``GBPCAD`` with rate ``1.8250`` means 1 GBP = 1.8250 CAD.
Snapshot amounts stay exact Decimal strings; only the price-series projection
(``convert_price_series``) is numerical. A conversion never uses an observation
dated after the requested date, and a stale or missing rate yields no amount.
"""

from __future__ import annotations

import math
import re
from bisect import bisect_right, insort
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, localcontext
from numbers import Real

import pandas as pd

MINOR_UNITS = {"GBp": ("GBP", 100), "GBX": ("GBP", 100), "ZAc": ("ZAR", 100), "ILA": ("ILS", 100)}
PRECISION = 34
MAX_NUMBER_LENGTH = 128
MAX_NUMBER_EXPONENT = 128
DIRECTION = "quote per base"
_MAJOR = re.compile(r"[A-Z]{3}")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _decimal(value, name: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{name} must be a decimal number")
    try:
        if isinstance(value, (Decimal, int)):
            number = Decimal(value)
        elif isinstance(value, (str, float)):
            number = Decimal(str(value).strip())
        else:
            raise TypeError(type(value).__name__)
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f"{name} must be a decimal number") from None
    if not number.is_finite():
        raise ValueError(f"{name} must be a finite decimal number")
    return number


def _bounded(value, name: str) -> Decimal:
    """A provider number small enough to format: at most 128 digits and exponent."""
    number = _decimal(value, name)
    text = str(value).strip() if isinstance(value, (str, float)) else str(number)
    if (
        len(text) > MAX_NUMBER_LENGTH
        or abs(number.as_tuple().exponent) > MAX_NUMBER_EXPONENT
        or number.adjusted() > MAX_NUMBER_EXPONENT
    ):
        raise ValueError(f"{name} must be a decimal of at most {MAX_NUMBER_LENGTH} digits")
    return number


def _text(number: Decimal) -> str:
    return format(number, "f")


def _major(code, name: str = "currency") -> str:
    if not isinstance(code, str) or not _MAJOR.fullmatch(code) or code in MINOR_UNITS:
        raise ValueError(f"{name} must be a three-letter major currency code")
    return code


def _unit(currency) -> tuple[str, int]:
    if isinstance(currency, str) and currency in MINOR_UNITS:
        return MINOR_UNITS[currency]
    return _major(currency), 1


def _iso_date(value, name: str) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not _DATE.fullmatch(value):
        raise ValueError(f"{name} must be an ISO date (YYYY-MM-DD)")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{name} must be a valid ISO date (YYYY-MM-DD)") from None


def _aware(value, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be an ISO timestamp with a UTC offset")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{name} must be an ISO timestamp with a UTC offset") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must include a UTC offset")
    return value


def _nonempty(value, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")
    return value


def normalize_unit(amount, currency) -> tuple[str | None, str, int]:
    """Express an amount quoted in a subunit (GBp, GBX, ZAc, ILA) in its major currency."""
    major, factor = _unit(currency)
    if amount is None:
        return None, major, factor
    number = _decimal(amount, "amount")
    if factor == 1:
        return _text(number), major, 1
    with localcontext() as context:
        context.prec = PRECISION
        return _text(number / factor), major, factor


def validate_observation(record) -> dict:
    """Return the canonical form of one dated FX observation or raise ValueError."""
    if not isinstance(record, dict):
        raise ValueError("An FX observation must be an object")
    base = _major(record.get("base"), "base")
    quote = _major(record.get("quote"), "quote")
    if base == quote:
        raise ValueError("FX observation base and quote must differ")
    if record.get("pair") != base + quote:
        raise ValueError("FX observation pair must equal base followed by quote")
    rate = _bounded(record.get("rate"), "rate")
    if rate <= 0:
        raise ValueError("FX rate must be positive")
    canonical = {
        "pair": base + quote,
        "base": base,
        "quote": quote,
        "rate": _text(rate),
        "date": _iso_date(record.get("date"), "date").isoformat(),
        "source_id": _nonempty(record.get("source_id"), "source_id"),
        "provider": _nonempty(record.get("provider"), "provider"),
        "received_at": _aware(record.get("received_at"), "received_at"),
    }
    if "published_at" in record:
        published = record["published_at"]
        canonical["published_at"] = None if published is None else _aware(published, "published_at")
    if record.get("derived_via") is not None:
        canonical["derived_via"] = _major(record["derived_via"], "derived_via")
    return canonical


def _max_age(value, name: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number of days")
    if value < 0:
        raise ValueError(f"{name} cannot be negative")
    return value


class FxTable:
    """Dated observations indexed by pair; the first record for a pair and date is kept."""

    def __init__(self, observations=(), *, max_age_days=7):
        self.max_age_days = _max_age(max_age_days, "max_age_days")
        self._records: dict[tuple[str, str], dict] = {}
        self._dates: dict[str, list[str]] = {}
        for record in observations:
            self.add(record)

    def __len__(self) -> int:
        return len(self._records)

    def add(self, record) -> None:
        canonical = validate_observation(record)
        key = (canonical["pair"], canonical["date"])
        if key in self._records:
            return
        self._records[key] = canonical
        insort(self._dates.setdefault(canonical["pair"], []), canonical["date"])

    def _newest(self, pair: str, day: str) -> dict | None:
        dates = self._dates.get(pair, [])
        index = bisect_right(dates, day)
        return self._records[(pair, dates[index - 1])] if index else None

    def latest(self, base, quote, on_date) -> dict | None:
        """Newest observation dated on or before ``on_date``; the reverse pair is inverted."""
        base, quote = _major(base, "base"), _major(quote, "quote")
        day = _iso_date(on_date, "on_date").isoformat()
        if base == quote:
            return None
        direct = self._newest(base + quote, day)
        reverse = self._newest(quote + base, day)
        if direct is not None and (reverse is None or direct["date"] >= reverse["date"]):
            return {**direct, "observed_pair": direct["pair"], "inverted": False}
        if reverse is None:
            return None
        with localcontext() as context:
            context.prec = PRECISION
            rate = _text(Decimal(1) / Decimal(reverse["rate"]))
        return {
            **reverse,
            "pair": base + quote,
            "base": base,
            "quote": quote,
            "rate": rate,
            "observed_pair": reverse["pair"],
            "inverted": True,
        }

    def convert(self, amount, from_currency, to_currency, on_date) -> dict:
        """Convert an exact amount at the newest usable observation on or before ``on_date``."""
        day = _iso_date(on_date, "on_date")
        target = _major(to_currency, "to_currency")
        normalized, source, factor = normalize_unit(amount, from_currency)
        result = _blank_conversion(from_currency, to_currency, day, factor)
        if source == target:
            status = "unit_normalized" if factor != 1 else "identity"
            return {**result, "amount": normalized, "status": status, "rate": "1"}
        pair = source + target
        found = self.latest(source, target, day)
        if found is None:
            message = f"{pair}: no dated observation on or before {day.isoformat()}."
            return {**result, "pair": pair, "issues": [_fx_issue("MISSING_FX", pair, day, message)]}
        age = (day - date.fromisoformat(found["date"])).days
        result = {**result, **_found_fields(found, pair, age)}
        if age > self.max_age_days:
            message = (
                f"{pair}: newest observation {found['date']} is {age} days before "
                f"{day.isoformat()}, beyond the {self.max_age_days}-day limit."
            )
            issue = _fx_issue("STALE_FX", pair, day, message, observation_date=found["date"])
            return {**result, "status": "stale", "issues": [issue]}
        if normalized is None:
            return {**result, "status": "converted"}
        with localcontext() as context:
            context.prec = PRECISION
            converted = _text(Decimal(normalized) * Decimal(found["rate"]))
        return {**result, "amount": converted, "status": "converted"}

    def to_records(self) -> list[dict]:
        ordered = sorted(
            self._records.values(), key=lambda r: (r["pair"], r["date"], r["provider"])
        )
        return [dict(record) for record in ordered]


def _blank_conversion(from_currency, to_currency, day: date, factor: int) -> dict:
    """A conversion that found nothing yet: every field stated, no amount claimed."""
    result = {
        "amount": None,
        "from_currency": from_currency,
        "to_currency": to_currency,
        "status": "missing",
        "rate": None,
        "pair": None,
        "observed_pair": None,
        "direction": DIRECTION,
        "observation_date": None,
        "requested_date": day.isoformat(),
        "age_days": None,
        "inverted": False,
        "source_id": None,
        "provider": None,
        "issues": [],
    }
    return {**result, "unit_factor": factor} if factor != 1 else result


def _found_fields(found: dict, pair: str, age: int) -> dict:
    fields = {
        "rate": found["rate"],
        "pair": pair,
        "observed_pair": found["observed_pair"],
        "observation_date": found["date"],
        "age_days": age,
        "inverted": found["inverted"],
        "source_id": found["source_id"],
        "provider": found["provider"],
    }
    if found.get("derived_via"):
        fields["derived_via"] = found["derived_via"]
    return fields


def _fx_issue(code: str, pair: str, day: date, message: str, **extra) -> dict:
    return {
        "severity": "warning",
        "code": code,
        "pair": pair,
        "requested_date": day.isoformat(),
        **extra,
        "message": message + " No converted amount is given.",
    }


def _row_day(value) -> str | None:
    try:
        stamp = pd.Timestamp(value)
    except (TypeError, ValueError):
        return None
    if pd.isna(stamp):
        return None
    if stamp.tzinfo is not None:
        stamp = stamp.tz_localize(None)
    return stamp.normalize().date().isoformat()


def _series_rate(table: FxTable, currency, target: str, day: str | None, max_gap_days):
    """(float multiplier, major rate, pair, observation date, source id) or None."""
    if day is None:
        return None
    try:
        major, factor = _unit(currency)
    except ValueError:
        return None
    if major == target:
        return 1.0 / factor, 1.0, None, None, None
    found = table.latest(major, target, day)
    if found is None:
        return None
    if (date.fromisoformat(day) - date.fromisoformat(found["date"])).days > max_gap_days:
        return None
    rate = float(found["rate"])
    return rate / factor, rate, found["pair"], found["date"], found["source_id"]


def _converted_rows(result, table, target: str, max_gap_days):
    """Multiply each row by its own dated rate; report which rows had none."""
    closes = pd.to_numeric(result["close"], errors="coerce").astype(float).tolist()
    adjusted = pd.to_numeric(result["adjusted_close"], errors="coerce").astype(float).tolist()
    currencies = result["currency"].tolist()
    out = {
        name: [None] * len(result) for name in ["fx_pair", "fx_observation_date", "fx_source_id"]
    }
    out["fx_rate"] = [math.nan] * len(result)
    new_close, new_adjusted, keep, gaps, cache = list(closes), list(adjusted), [], {}, {}
    for i, (security, raw_day, currency) in enumerate(
        zip(result["security_id"], result["date"], currencies)
    ):
        if currency == target:
            keep.append(True)
            continue
        day = _row_day(raw_day)
        key = (currency if isinstance(currency, str) else None, day)
        if key not in cache:
            cache[key] = _series_rate(table, currency, target, day, max_gap_days)
        found = cache[key]
        if found is None:
            keep.append(False)
            gaps.setdefault(security, {"rows": 0, "currency": currency})["rows"] += 1
            continue
        multiplier, rate, pair, observed, source_id = found
        new_close[i] = closes[i] * multiplier
        new_adjusted[i] = adjusted[i] * multiplier
        out["fx_rate"][i], out["fx_pair"][i] = rate, pair
        out["fx_observation_date"][i], out["fx_source_id"][i] = observed, source_id
        keep.append(True)
    local = {"close": closes, "adjusted_close": adjusted, "currency": currencies}
    return {"close": new_close, "adjusted_close": new_adjusted}, local, out, keep, gaps


def _gap_issues(gaps: dict, target: str, max_gap_days) -> list[dict]:
    return [
        {
            "severity": "warning",
            "code": "FX_SERIES_GAPS",
            "security_id": security,
            "currency": gap["currency"],
            "to_currency": target,
            "rows": gap["rows"],
            "message": (
                f"{security}: {gap['rows']} price rows had no dated {gap['currency']} to "
                f"{target} observation within {max_gap_days} days and were excluded."
            ),
        }
        for security, gap in sorted(gaps.items(), key=lambda item: str(item[0]))
    ]


def convert_price_series(frame, table, *, to_currency="USD", max_gap_days=5):
    """Convert each price row at its own dated FX rate (never today's rate).

    A row uses the observation dated on its own date, else the newest one within
    ``max_gap_days`` before it. Rows without a usable rate are dropped and counted
    in one ``FX_SERIES_GAPS`` issue per security. Returns ``(frame, issues)``.
    """
    target = _major(to_currency, "to_currency")
    _max_age(max_gap_days, "max_gap_days")
    required = {"security_id", "date", "close", "adjusted_close", "currency"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"Price frame is missing columns: {sorted(missing)}")
    result = frame.copy().reset_index(drop=True)
    converted, local, out, keep, gaps = _converted_rows(result, table, target, max_gap_days)
    result["local_close"] = local["close"]
    result["local_adjusted_close"] = local["adjusted_close"]
    result["local_currency"] = local["currency"]
    result["close"] = converted["close"]
    result["adjusted_close"] = converted["adjusted_close"]
    result["currency"] = target
    for name in ["fx_rate", "fx_pair", "fx_observation_date", "fx_source_id"]:
        result[name] = pd.Series(out[name], index=result.index, dtype=object)
    result["fx_rate"] = result["fx_rate"].astype(float)
    result = result[pd.Series(keep, index=result.index, dtype=bool)].reset_index(drop=True)
    return result, _gap_issues(gaps, target, max_gap_days)


def usd_scenario_return(local_return, fx_return) -> float:
    """Compound a local return with the change in USD per unit of the foreign currency."""
    values = []
    for name, value in (("local_return", local_return), ("fx_return", fx_return)):
        if isinstance(value, bool) or not isinstance(value, (Real, Decimal)):
            raise ValueError(f"{name} must be a number")
        number = float(value)
        if not math.isfinite(number) or number < -1:
            raise ValueError(f"{name} must be a finite return of at least -100%")
        values.append(number)
    return (1 + values[0]) * (1 + values[1]) - 1
