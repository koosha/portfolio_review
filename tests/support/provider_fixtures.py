"""Recorded provider responses replayed as the objects the adapters actually read.

The files under ``tests/fixtures/providers`` hold one recorded response per provider
capability in the provider's own field names. This module rebuilds them into the shapes
the adapters consume — a ``yfinance.Ticker``-alike whose frames are pandas objects, and
the raw JSON payloads the Bank of Canada and SEC readers are handed — so a contract test
asserts against the recorded shape rather than a hand-built stand-in that has drifted
along with the code. ``rename`` produces the one thing a contract test needs beside the
recorded shape: the same response with one field renamed, as a provider release would.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from copy import deepcopy
from datetime import date
from pathlib import Path

import pandas as pd

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "providers"
PRICES = "yahoo_prices"
STATEMENTS = "yahoo_statements"
ESTIMATES = "yahoo_estimates"
FUNDS = "yahoo_funds"
NEWS = "yahoo_news"
PROFILE = "yahoo_profile"
VALET = "bank_of_canada_valet"
SEC_TICKERS = "sec_company_tickers"
SEC_COMPANYFACTS = "sec_companyfacts"
YAHOO_FIXTURES = (PRICES, STATEMENTS, ESTIMATES, FUNDS, NEWS, PROFILE)


def load(name: str) -> dict:
    """One recorded response, as a fresh mutable copy of the checked-in JSON."""
    with (FIXTURES / f"{name}.json").open(encoding="utf-8") as handle:
        return json.load(handle)


def rename(payload: dict, path: tuple[str, ...], old: str, new: str) -> dict:
    """A copy of ``payload`` with one key renamed, the shape drift a release can ship."""
    copied = deepcopy(payload)
    target = copied
    for step in path:
        target = target[step]
    if old not in target:
        raise KeyError(f"{'.'.join((*path, old))} is not in the recorded response")
    target[new] = target.pop(old)
    return copied


def _frame(spec) -> pd.DataFrame:
    """A recorded ``{index, columns}`` table as a DataFrame in the provider's own names."""
    frame = pd.DataFrame(dict(spec["columns"]), index=pd.Index(spec["index"]))
    frame.index.name = spec.get("index_name")
    return frame


def _history(spec) -> pd.DataFrame:
    index = pd.DatetimeIndex(
        [pd.Timestamp(day, tz=spec["timezone"]) for day in spec["index"]], name="Date"
    )
    return pd.DataFrame(dict(spec["columns"]), index=index)


class HistoryMetadata(Mapping):
    """``Ticker.history_metadata`` as the pinned yfinance returns it: a Mapping, not a dict.

    ``yfinance.scrapers.history.HistoryMetadata`` subclasses ``collections.abc.Mapping``
    and nothing else, so ``isinstance(metadata, dict)`` is False for every live response.
    A reader that gates on ``dict`` drops currency, exchange, instrument type and timezone
    for every security while the recorded dict lets it look correct, so the fixture serves
    the provider's own type rather than the JSON it was recorded from.
    """

    def __init__(self, values):
        self._values = dict(values)

    def __getitem__(self, key):
        return self._values[key]

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)

    def __repr__(self):  # pragma: no cover - debugging aid only
        return f"HistoryMetadata({self._values!r})"


def _statement(spec) -> pd.DataFrame:
    """A recorded statement as the provider returns it: line items by period-end column."""
    columns = [pd.Timestamp(period) for period in spec["periods"]]
    return pd.DataFrame.from_dict(dict(spec["rows"]), orient="index", columns=columns)


def _calendar(spec) -> dict:
    """The provider's calendar mapping, with its date values as ``date`` objects."""
    result = {}
    for key, value in spec.items():
        if isinstance(value, list):
            result[key] = [date.fromisoformat(item) for item in value]
        elif isinstance(value, str):
            result[key] = date.fromisoformat(value)
        else:
            result[key] = value
    return result


class RecordedFunds:
    """The ``funds_data`` object: attributes the fund reader reads, nothing more."""

    def __init__(self, spec):
        self.top_holdings = _frame(spec["top_holdings"]) if "top_holdings" in spec else None
        self.sector_weightings = spec.get("sector_weightings")
        self.asset_classes = spec.get("asset_classes")
        self.fund_overview = spec.get("fund_overview")


class RecordedTicker:
    """Shaped like ``yfinance.Ticker`` over recorded responses, and counting its calls.

    Every attribute is served from the recorded payload for that capability. An absent
    capability raises ``AttributeError`` the way a provider object does when a release
    withdraws a property, so a contract test can pin that case too.
    """

    def __init__(self, symbol, payloads, calls):
        self.symbol = symbol
        self._payloads = payloads
        self._calls = calls

    def _read(self, fixture, field):
        self._calls.append((self.symbol, field))
        payload = self._payloads.get(fixture) or {}
        if field not in payload:
            raise AttributeError(f"{field} is not in the recorded {fixture} response")
        return payload[field]

    @property
    def ticker(self):
        """``yfinance.Ticker`` names the symbol this way; the recorder reads it."""
        return self.symbol

    def history(self, start=None, end=None, auto_adjust=False, actions=True, **kwargs):
        return _history(self._read(PRICES, "history"))

    @property
    def history_metadata(self):
        fixture = PRICES if PRICES in self._payloads else PROFILE
        return HistoryMetadata(self._read(fixture, "history_metadata"))

    @property
    def info(self):
        fixture = PROFILE if PROFILE in self._payloads else STATEMENTS
        return self._read(fixture, "info")

    @property
    def sec_filings(self):
        fixture = STATEMENTS if STATEMENTS in self._payloads else NEWS
        return self._read(fixture, "sec_filings")

    @property
    def income_stmt(self):
        return _statement(self._read(STATEMENTS, "income_stmt"))

    @property
    def balance_sheet(self):
        return _statement(self._read(STATEMENTS, "balance_sheet"))

    @property
    def cashflow(self):
        return _statement(self._read(STATEMENTS, "cashflow"))

    @property
    def quarterly_income_stmt(self):
        return _statement(self._read(STATEMENTS, "quarterly_income_stmt"))

    @property
    def quarterly_balance_sheet(self):
        return _statement(self._read(STATEMENTS, "quarterly_balance_sheet"))

    @property
    def quarterly_cashflow(self):
        return _statement(self._read(STATEMENTS, "quarterly_cashflow"))

    @property
    def earnings_estimate(self):
        return _frame(self._read(ESTIMATES, "earnings_estimate"))

    @property
    def revenue_estimate(self):
        return _frame(self._read(ESTIMATES, "revenue_estimate"))

    @property
    def analyst_price_targets(self):
        return self._read(ESTIMATES, "analyst_price_targets")

    @property
    def recommendations(self):
        return _frame(self._read(ESTIMATES, "recommendations"))

    @property
    def calendar(self):
        fixture = ESTIMATES if ESTIMATES in self._payloads else NEWS
        return _calendar(self._read(fixture, "calendar"))

    @property
    def news(self):
        return self._read(NEWS, "news")

    @property
    def funds_data(self):
        return RecordedFunds(self._read(FUNDS, "funds_data"))


def recorded_tickers(**replacements):
    """``(factory, calls)``: a ``yfinance.Ticker`` stand-in over the recorded responses.

    Pass ``prices=...`` (or any other fixture name) to replace one recorded response with
    a drifted copy; every other capability keeps the shape that was recorded.
    """
    payloads = {name: load(name) for name in YAHOO_FIXTURES}
    for name, payload in replacements.items():
        key = f"yahoo_{name}"
        if key not in payloads:
            raise KeyError(f"No recorded {key} response to replace")
        payloads[key] = payload
    calls: list[tuple[str, str]] = []

    def factory(symbol):
        return RecordedTicker(symbol, payloads, calls)

    return factory, calls


def scalar(value):
    """One provider value as JSON: a plain number, an ISO date or bounded text."""
    if value is None or (isinstance(value, float) and value != value):
        return None
    if isinstance(value, (pd.Timestamp, date)):
        return pd.Timestamp(value).date().isoformat()
    if hasattr(value, "item"):  # numpy scalars carry their own Python value.
        value = value.item()
    if isinstance(value, (bool, int, float, str)) or value is None:
        return value
    return str(value)


def encode_frame(frame) -> dict:
    """A provider DataFrame in the fixture's ``{index, columns}`` encoding.

    ``index_name`` is written only when the provider named the index, so an unnamed one
    stays out of the file rather than appearing as a null a reader has to interpret.
    """
    encoded = {
        "index": [scalar(value) for value in frame.index],
        "columns": {str(name): [scalar(value) for value in frame[name]] for name in frame.columns},
    }
    if frame.index.name is not None:
        return {"index_name": frame.index.name, **encoded}
    return encoded


def encode_history(frame) -> dict:
    """A price-history frame with its exchange timezone kept beside the session dates."""
    return {
        "timezone": str(frame.index.tz) if frame.index.tz is not None else "UTC",
        "index": [pd.Timestamp(stamp).date().isoformat() for stamp in frame.index],
        "columns": {str(name): [scalar(value) for value in frame[name]] for name in frame.columns},
    }


def encode_statement(frame) -> dict:
    """A statement frame as ``{periods, rows}``: line items across period-end columns."""
    return {
        "periods": [pd.Timestamp(column).date().isoformat() for column in frame.columns],
        "rows": {
            str(label): [scalar(value) for value in frame.loc[label]] for label in frame.index
        },
    }


def write(name: str, payload: dict) -> Path:
    """Replace one recorded response on disk, pretty-printed so a diff stays readable."""
    path = FIXTURES / f"{name}.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    return path
