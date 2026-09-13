"""Versioned US decision and earliest simulated execution policy."""

from datetime import datetime
from functools import lru_cache
from importlib.metadata import version
from zoneinfo import ZoneInfo

import exchange_calendars as calendars
import pandas as pd

METHOD_VERSION = "us-month-end-1"


@lru_cache(maxsize=8)
def _calendar(year):
    return calendars.get_calendar("XNYS", start=f"{year - 6}-01-01", end=f"{year + 3}-12-31")


def information_cutoff(as_of):
    day = pd.Timestamp(as_of)
    if pd.isna(day) or day.tzinfo is not None or day != day.normalize():
        raise ValueError("Decision date must be YYYY-MM-DD without a time or timezone.")
    return (day.tz_localize("America/New_York") + pd.Timedelta(hours=16)).tz_convert("UTC")


def trailing_sessions(as_of, count):
    """Prior completed sessions, including the selected session at its information cutoff."""
    if type(count) is not int or not 1 <= count <= 1500:
        raise ValueError("Session count must be an integer between 1 and 1500.")
    day = pd.Timestamp(as_of)
    calendar = _calendar(day.year)
    last = calendar.date_to_session(day, direction="previous")
    sessions = calendar.sessions[calendar.sessions <= last][-count:]
    return [session.date().isoformat() for session in sessions]


def month_end_session(year_month):
    end = pd.Period(year_month, freq="M").end_time.normalize()
    return _calendar(end.year).date_to_session(end, direction="previous").date().isoformat()


def decision_context(as_of=None, *, month=None, generated_at=None):
    generated = pd.Timestamp(generated_at or datetime.now(ZoneInfo("UTC")))
    if generated.tzinfo is None:
        raise ValueError("Generation timestamp requires an explicit timezone.")
    if as_of and month:
        raise ValueError("Choose a decision date or a month, not both.")
    if not as_of:
        if month:
            requested = pd.Period(month, freq="M").end_time.normalize()
        else:
            local_day = generated.tz_convert("America/New_York").tz_localize(None).normalize()
            requested = local_day.replace(day=1) - pd.Timedelta(days=1)
    else:
        requested = pd.Timestamp(as_of)
        information_cutoff(as_of)
    calendar = _calendar(requested.year)
    session = calendar.date_to_session(requested, direction="previous")
    cutoff = information_cutoff(session.date().isoformat())
    close = calendar.session_close(session)
    next_session = calendar.next_session(session)
    return {
        "method_version": METHOD_VERSION,
        "calendar": "XNYS",
        "calendar_version": version("exchange-calendars"),
        "requested_date": requested.date().isoformat(),
        "decision_date": session.date().isoformat(),
        "decision_cutoff": cutoff.isoformat(),
        "market_close": close.isoformat(),
        "early_close": close < cutoff,
        "earliest_execution_date": next_session.date().isoformat(),
        "earliest_execution_at": calendar.session_close(next_session).isoformat(),
        "generated_at": generated.tz_convert("UTC").isoformat(),
        "execution_policy": "No earlier than the next eligible US session close; actual decisions also require all inputs to be available before execution.",
    }
