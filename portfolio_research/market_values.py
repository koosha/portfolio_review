"""Shared vocabulary and value readers for the market-data adapter.

The adapter version, the label vocabularies (sector, domicile, equity type, statement
line items) and the small readers that reduce a provider value to data: a finite
number, bounded text, a calendar date, a currency unit and the instant a session close
becomes knowable. Nothing here reaches a provider or the cache.
"""

from __future__ import annotations

import math
import unicodedata
from datetime import date, datetime, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

import pandas as pd

ADAPTER_VERSION = "yfinance-adapter-1"
MAX_TEXT_LENGTH = 300
MAX_SUMMARY_LENGTH = 500
NEW_YORK = ZoneInfo("America/New_York")
ESTIMATE_LABEL = "External consensus estimate; not an established outcome"
DISCLOSURE_BASIS = "provider_snapshot_undated"
PRICE_ADJUSTMENT = "yahoo_adj_close"
ESTIMATE_PERIODS = ("0q", "+1q", "0y", "+1y")
FILING_FORMS = ("10-K", "10-Q", "20-F", "40-F")
# A filing establishes a period's publication only when its form reports that period.
PERIOD_FORMS = {"annual": frozenset({"10-K", "20-F", "40-F"}), "quarterly": frozenset({"10-Q"})}
# Control, format and surrogate characters: a zero-width space or a right-to-left
# override would let stored provider text render as something other than what it says.
UNSAFE_CATEGORIES = frozenset({"Cc", "Cf", "Cs"})


# Yahoo's sector vocabulary mapped onto the application's (signals.excluded_sectors).
SECTOR_LABELS = {"Financial Services": "Financials"}
FUND_SECTORS = {
    "technology": "Technology",
    "financial_services": "Financials",
    "realestate": "Real Estate",
    "healthcare": "Health Care",
    "consumer_cyclical": "Consumer Discretionary",
    "consumer_defensive": "Consumer Staples",
    "communication_services": "Communication Services",
    "industrials": "Industrials",
    "energy": "Energy",
    "utilities": "Utilities",
    "basic_materials": "Materials",
}
DOMICILES = {
    "United States": "US",
    "Canada": "CA",
    "United Kingdom": "GB",
    "Ireland": "IE",
    "Netherlands": "NL",
    "Switzerland": "CH",
    "Japan": "JP",
    "Germany": "DE",
    "France": "FR",
    "Denmark": "DK",
    "Sweden": "SE",
    "Norway": "NO",
    "Finland": "FI",
    "Spain": "ES",
    "Italy": "IT",
    "Belgium": "BE",
    "Luxembourg": "LU",
    "Australia": "AU",
    "New Zealand": "NZ",
    "China": "CN",
    "Hong Kong": "HK",
    "Taiwan": "TW",
    "South Korea": "KR",
    "Singapore": "SG",
    "India": "IN",
    "Israel": "IL",
    "Brazil": "BR",
    "Mexico": "MX",
    "South Africa": "ZA",
    "Bermuda": "BM",
    "Cayman Islands": "KY",
    "Jersey": "JE",
}
# Preferred, warrant, unit and preference-share suffixes are not ordinary common stock.
SHARE_CLASS_SUFFIXES = ("-P", "-W", "-U", ".PR")
INCOME_LINES = {
    "revenue": "Total Revenue",
    "gross_profit": "Gross Profit",
    "operating_income": "Operating Income",
    "net_income": "Net Income",
    "income_common": "Net Income Common Stockholders",
    "pretax_income": "Pretax Income",
    "tax_provision": "Tax Provision",
    "diluted_eps": "Diluted EPS",
    "diluted_shares": "Diluted Average Shares",
    "basic_shares": "Basic Average Shares",
}
BALANCE_LINES = {
    "assets": "Total Assets",
    "debt": "Total Debt",
    "cash": "Cash And Cash Equivalents",
    "minority_interest": "Minority Interest",
    "preferred": "Preferred Stock Equity",
}
# Reinvestment and the effective tax rate need the cash-flow lines a valuation reads.
CASHFLOW_LINES = {
    "operating_cash_flow": "Operating Cash Flow",
    "depreciation": "Reconciled Depreciation",
    "change_working_capital": "Change In Working Capital",
    "stock_compensation": "Stock Based Compensation",
}
CAPEX_LINE = "Capital Expenditure"
WORKING_CAPITAL_LINE = "Change In Working Capital"
COMMON_LINE = "Net Income Common Stockholders"


def _issue(issues, code, security_id, detail, severity="warning"):
    """Record one provider failure. Details never carry provider URLs or secrets."""
    if issues is not None:
        issues.append(
            {
                "code": code,
                "security_id": security_id,
                "detail": detail,
                "severity": severity,
                "message": f"{security_id}: {detail}" if security_id else detail,
            }
        )


def _value(security, field):
    """One field of a security row, as a plain value, with pandas nulls read as missing."""
    try:
        value = security[field]
    except (KeyError, IndexError, TypeError):
        return None
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    return value


def security_id(security) -> str | None:
    value = _value(security, "security_id")
    return str(value) if value is not None and str(value).strip() else None


def listing_symbol(security) -> str | None:
    """The exact provider symbol for a security row, or ``None``."""
    from .market_listings import normalize_symbol

    for field in ("listing_symbol", "ticker", "security_id"):
        symbol = normalize_symbol(_value(security, field))
        if symbol is not None:
            return symbol
    return None


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _text(value, limit=MAX_TEXT_LENGTH):
    """Provider text reduced to safe, bounded data.

    Control characters are dropped, and so is every Unicode format character: a bidi
    override or a zero-width joiner would make the rendered order of a news title
    disagree with the text this record stores and classifies.
    """
    if not isinstance(value, str):
        return None
    cleaned = "".join(
        character for character in value if unicodedata.category(character) not in UNSAFE_CATEGORIES
    ).strip()
    return cleaned[:limit] or None


def _date_text(value):
    """An ISO calendar date for a provider timestamp, date or string, else ``None``."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (datetime, date, pd.Timestamp)):
        return pd.Timestamp(value).date().isoformat()
    if isinstance(value, (int, float)):
        if not math.isfinite(float(value)):
            return None
        return datetime.fromtimestamp(float(value), tz=timezone.utc).date().isoformat()
    text = _text(value, 40)
    if text is None:
        return None
    stamp = pd.to_datetime(text, errors="coerce", utc=True)
    return None if pd.isna(stamp) else stamp.date().isoformat()


@lru_cache(maxsize=8192)
def _session_stamp(day: str) -> str:
    from .calendar import _calendar

    stamp = pd.Timestamp(day)
    calendar = _calendar(stamp.year)
    if calendar.is_session(stamp):
        return calendar.session_close(stamp).isoformat()
    return datetime(stamp.year, stamp.month, stamp.day, 23, 59, 59, tzinfo=NEW_YORK).isoformat()


def available_at(day) -> str:
    """The instant a session's close is knowable: the XNYS close, else the New York day end.

    Prices for a foreign listing become usable for a US decision at the end of that
    New York day when the US market did not trade; both stamps are timezone-aware.
    """
    return _session_stamp(pd.Timestamp(day).tz_localize(None).normalize().date().isoformat())


def sector_label(info_sector) -> str | None:
    """A yfinance sector string in the application's sector vocabulary."""
    text = _text(info_sector, 60)
    return None if text is None else SECTOR_LABELS.get(text, text)


def domicile_code(country) -> str | None:
    """The ISO alpha-2 code for a provider country name, or ``None`` when unknown."""
    text = _text(country, 60)
    return None if text is None else DOMICILES.get(text)


def equity_type_label(instrument_type, symbol) -> tuple[str | None, str | None]:
    """``("ordinary_common", None)`` or ``(None, reason)``; never a guess."""
    kind = _text(instrument_type, 40)
    if kind is None:
        return None, "Instrument type is unknown; ordinary common stock is not established."
    if kind.upper() != "EQUITY":
        return None, f"Instrument type {kind} is not an equity listing."
    name = _text(symbol, 40) or ""
    for suffix in SHARE_CLASS_SUFFIXES:
        if name.upper().endswith(suffix):
            return None, (
                f"Symbol {name} carries a {suffix} share-class or unit suffix; "
                "ordinary common stock is not established."
            )
    return "ordinary_common", None


def _units(currency):
    """``(quote_currency, major_currency, factor)`` for a provider quote currency."""
    from .fx import normalize_unit

    code = _text(currency, 8)
    if code is None:
        return None, None, 1
    try:
        _, major, factor = normalize_unit(None, code)
    except ValueError:
        return code, None, 1
    return code, major, factor


def _major(amount, currency, factor):
    """An amount expressed in the major currency, through the shared FX unit rule."""
    from .fx import normalize_unit

    value = _number(amount)
    if value is None or currency is None:
        return value
    if factor == 1:
        return value
    text, _, _ = normalize_unit(value, currency)
    return None if text is None else float(text)
