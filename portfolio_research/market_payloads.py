"""Reading one live provider response into an archive-safe payload.

Every function here takes a ``yfinance.Ticker``-shaped object and returns plain JSON
values: numbers, bounded text and ISO dates. Provider objects fetch lazily and fail in
many ways, so optional parts are read one at a time and an absent part stays absent;
a response with nothing usable raises so the caller records one issue for the security.
"""

from __future__ import annotations

import pandas as pd

from .market_values import (
    ESTIMATE_PERIODS,
    MAX_SUMMARY_LENGTH,
    MAX_TEXT_LENGTH,
    _date_text,
    _number,
    _text,
)


def _attribute(ticker, name):
    try:
        return getattr(ticker, name)
    except Exception:  # One absent statement must not lose the others.
        return None


def _metadata(ticker):
    try:
        data = ticker.history_metadata
    except Exception:  # Metadata is optional; prices remain usable without it.
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        "currency": _text(data.get("currency"), 8),
        "exchange": _text(data.get("exchangeName"), 40),
        "instrument_type": _text(data.get("instrumentType"), 40),
        "timezone": _text(data.get("exchangeTimezoneName"), 60),
    }


def _price_payload(ticker, start, end):
    frame = ticker.history(start=start, end=end, auto_adjust=False, actions=True)
    if frame is None or getattr(frame, "empty", True):
        raise ValueError("No price history")
    if not {"Close", "Adj Close"}.issubset(frame.columns):
        raise ValueError("No raw and adjusted closes")
    rows = []
    for stamp, row in frame.iterrows():
        moment = pd.Timestamp(stamp)
        rows.append(
            {
                "date": moment.date().isoformat(),
                "close": _number(row.get("Close")),
                "adjusted_close": _number(row.get("Adj Close")),
                "volume": _number(row.get("Volume")),
                "dividend": _number(row.get("Dividends")),
                "split": _number(row.get("Stock Splits")),
            }
        )
    return {"rows": rows, "metadata": _metadata(ticker)}


def _statement_frame(frame):
    """A statement DataFrame as ``{period_end: {line item: number}}``."""
    if frame is None or getattr(frame, "empty", True):
        return {}
    result = {}
    for column in frame.columns:
        period = _date_text(column)
        if period is None:
            continue
        values = {}
        for label, value in frame[column].items():
            name = _text(label, 80)
            number = _number(value)
            if name is not None and number is not None:
                values[name] = number
        result[period] = values
    return result


def _filings_payload(ticker):
    filings = _attribute(ticker, "sec_filings")
    rows = []
    for item in filings if isinstance(filings, list) else []:
        if not isinstance(item, dict):
            continue
        rows.append(
            {
                "date": _date_text(item.get("date")),
                "type": _text(item.get("type"), 20),
                "title": _text(item.get("title")),
                "url": _text(item.get("edgarUrl"), MAX_TEXT_LENGTH),
            }
        )
    return rows


def _statement_payload(ticker):
    sets = {
        "annual": ("income_stmt", "balance_sheet", "cashflow"),
        "quarterly": ("quarterly_income_stmt", "quarterly_balance_sheet", "quarterly_cashflow"),
    }
    payload = {"filings": _filings_payload(ticker)}
    for period_type, (income, balance, cashflow) in sets.items():
        payload[period_type] = {
            "income": _statement_frame(_attribute(ticker, income)),
            "balance": _statement_frame(_attribute(ticker, balance)),
            "cashflow": _statement_frame(_attribute(ticker, cashflow)),
        }
    info = _attribute(ticker, "info")
    payload["currency"] = _text((info or {}).get("financialCurrency"), 8)
    if not payload["annual"]["income"] and not payload["quarterly"]["income"]:
        raise ValueError("No income statement")
    return payload


def _estimate_frame(frame, year_ago_field):
    if frame is None or getattr(frame, "empty", True):
        return {}
    result = {}
    for period in ESTIMATE_PERIODS:
        if period not in frame.index:
            continue
        row = frame.loc[period]
        analysts = _number(row.get("numberOfAnalysts"))
        result[period] = {
            "avg": _number(row.get("avg")),
            "low": _number(row.get("low")),
            "high": _number(row.get("high")),
            "year_ago": _number(row.get(year_ago_field)),
            "analysts": None if analysts is None else int(analysts),
            "growth": _number(row.get("growth")),
            "currency": _text(row.get("currency"), 8),
        }
    return result


def _recommendations(frame):
    if frame is None or getattr(frame, "empty", True):
        return None
    row = frame.iloc[0]
    counts = {
        "strong_buy": "strongBuy",
        "buy": "buy",
        "hold": "hold",
        "sell": "sell",
        "strong_sell": "strongSell",
    }
    result = {"period": _text(row.get("period"), 10)}
    for field, column in counts.items():
        number = _number(row.get(column))
        result[field] = None if number is None else int(number)
    return result


def _calendar_payload(ticker):
    data = _attribute(ticker, "calendar")
    if not isinstance(data, dict):
        return {}
    earnings = data.get("Earnings Date")
    earnings = earnings if isinstance(earnings, list) else [earnings]
    return {
        "earnings_dates": [day for day in (_date_text(item) for item in earnings) if day],
        "ex_dividend_date": _date_text(data.get("Ex-Dividend Date")),
        "dividend_date": _date_text(data.get("Dividend Date")),
    }


def _estimates_payload(ticker):
    eps = _estimate_frame(_attribute(ticker, "earnings_estimate"), "yearAgoEps")
    revenue = _estimate_frame(_attribute(ticker, "revenue_estimate"), "yearAgoRevenue")
    targets = _attribute(ticker, "analyst_price_targets")
    targets = targets if isinstance(targets, dict) else {}
    payload = {
        "eps": eps,
        "revenue": revenue,
        "price_targets": {
            field: _number(targets.get(field))
            for field in ("current", "low", "high", "mean", "median")
        },
        "recommendations": _recommendations(_attribute(ticker, "recommendations")),
        **_calendar_payload(ticker),
    }
    if not eps and not revenue and not any(payload["price_targets"].values()):
        raise ValueError("No consensus estimates")
    return payload


def _holdings_payload(funds):
    frame = getattr(funds, "top_holdings", None)
    rows = []
    if frame is not None and not getattr(frame, "empty", True):
        for symbol, row in frame.iterrows():
            rows.append(
                {
                    "symbol": _text(symbol, 40),
                    "name": _text(row.get("Name")),
                    "weight": _number(row.get("Holding Percent")),
                }
            )
    return rows


def _keyed(values):
    """A provider mapping with sanitized string keys; an unusable key drops its entry."""
    rows = {}
    for key, value in (values if isinstance(values, dict) else {}).items():
        name = _text(key, 60)
        if name is not None:
            rows[name] = _number(value)
    return rows


def _funds_payload(ticker):
    funds = ticker.funds_data
    sectors = getattr(funds, "sector_weightings", None)
    classes = getattr(funds, "asset_classes", None)
    overview = getattr(funds, "fund_overview", None)
    payload = {
        "top_holdings": _holdings_payload(funds),
        # A key that sanitizes to nothing is dropped: a ``None`` key would make the
        # archived payload unserializable and end the run for every security.
        "sector_weightings": _keyed(sectors),
        "asset_classes": _keyed(classes),
        "fund_overview": {
            "category": _text((overview or {}).get("categoryName")),
            "family": _text((overview or {}).get("family")),
            "legal_type": _text((overview or {}).get("legalType")),
        },
    }
    if not payload["top_holdings"] and not payload["sector_weightings"]:
        raise ValueError("No fund disclosure")
    return payload


def _news_payload(ticker):
    stories = _attribute(ticker, "news")
    rows = []
    for item in stories if isinstance(stories, list) else []:
        content = item.get("content") if isinstance(item, dict) else None
        if not isinstance(content, dict):
            continue
        provider = content.get("provider")
        url = content.get("canonicalUrl")
        rows.append(
            {
                "title": _text(content.get("title")),
                "published_at": _date_text(content.get("pubDate")),
                "provider": _text((provider or {}).get("displayName"), 80),
                "url": _text((url or {}).get("url"), MAX_TEXT_LENGTH),
                "summary": _text(content.get("summary"), MAX_SUMMARY_LENGTH),
            }
        )
    payload = {"news": rows, "filings": _filings_payload(ticker), **_calendar_payload(ticker)}
    if not rows and not payload["filings"] and not payload.get("earnings_dates"):
        raise ValueError("No news, filings or scheduled dates")
    return payload


def _profile_payload(ticker):
    info = _attribute(ticker, "info")
    info = info if isinstance(info, dict) else {}
    payload = {
        "sector": _text(info.get("sector"), 60),
        "industry": _text(info.get("industry"), 80),
        "market_cap": _number(info.get("marketCap")),
        "shares_outstanding": _number(info.get("sharesOutstanding")),
        "name": _text(info.get("longName")),
        "country": _text(info.get("country"), 60),
        "financial_currency": _text(info.get("financialCurrency"), 8),
        "metadata": _metadata(ticker),
    }
    if not any(value for key, value in payload.items() if key != "metadata"):
        raise ValueError("No company profile")
    return payload
