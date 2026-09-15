"""Versioned US decision and earliest simulated execution policy."""

from datetime import datetime
from functools import lru_cache
from importlib.metadata import version
from zoneinfo import ZoneInfo

import exchange_calendars as calendars
import pandas as pd

METHOD_VERSION = "us-month-end-1"
CURRENT_METHOD_VERSION = "us-current-review-1"
REVIEW_KINDS = ("current", "historical")
DATE_ONLY = r"\d{4}-\d{2}-\d{2}"


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


def _aware_utc(value, nullable=False):
    """Parse an explicit timezone-aware timestamp and normalize it to UTC."""
    if value is None and nullable:
        return None
    stamp = pd.Timestamp(value)
    if pd.isna(stamp) or stamp.tzinfo is None:
        raise ValueError("Generation timestamp requires an explicit timezone.")
    return stamp.tz_convert("UTC")


def _first_session_closing_after(moment):
    """First XNYS session whose close is strictly after ``moment`` (an aware UTC timestamp)."""
    local_day = moment.tz_convert("America/New_York").tz_localize(None).normalize()
    calendar = _calendar(local_day.year)
    session = calendar.date_to_session(local_day, direction="next")
    while calendar.session_close(session) <= moment:
        session = calendar.next_session(session)
    return session


def _execution_stamps(session):
    return {
        "earliest_execution_date": session.date().isoformat(),
        "earliest_execution_at": _calendar(session.year).session_close(session).isoformat(),
    }


def _current_context(generated):
    local_day = generated.tz_convert("America/New_York").tz_localize(None).normalize()
    calendar = _calendar(local_day.year)
    observed = calendar.date_to_session(local_day, direction="previous")
    if calendar.session_close(observed) > generated:
        # The session is still open; the last completed observation is the prior close.
        observed = calendar.previous_session(observed)
    execution = _first_session_closing_after(generated)
    decision_date = observed.date().isoformat()
    close = calendar.session_close(observed)
    return {
        "review_kind": "current",
        "method_version": CURRENT_METHOD_VERSION,
        "calendar": "XNYS",
        "calendar_version": version("exchange-calendars"),
        "requested_date": local_day.date().isoformat(),
        "review_month": local_day.strftime("%Y-%m"),
        "decision_date": decision_date,
        "market_observation_date": decision_date,
        "market_close": close.isoformat(),
        "early_close": close < information_cutoff(decision_date),
        "information_cutoff": generated.isoformat(),
        "decision_cutoff": generated.isoformat(),
        "generated_at": generated.isoformat(),
        **_execution_stamps(execution),
        "execution_policy": "First eligible US session close strictly after generation; inputs must be available before execution.",
        "execution_basis": "after_generation",
    }


def review_context(
    kind="current",
    *,
    as_of=None,
    month=None,
    generated_at=None,
    collection_received_at=None,
    source_valuation_time=None,
):
    """Dated context for a current review (latest collection) or a historical month-end review.

    A current review observes the last completed NYSE session at generation time and
    treats generation time as its information cutoff. A historical review keeps the
    ``decision_context`` contract unchanged and only adds explicit labels.
    """
    if kind not in REVIEW_KINDS:
        raise ValueError("Review kind must be current or historical.")
    generated = _aware_utc(generated_at or datetime.now(ZoneInfo("UTC")))
    received = _aware_utc(collection_received_at, nullable=True)
    valued = _aware_utc(source_valuation_time, nullable=True)
    stamps = {
        "collection_received_at": received.isoformat() if received is not None else None,
        "source_valuation_time": valued.isoformat() if valued is not None else None,
    }
    if kind == "current":
        if as_of or month:
            raise ValueError(
                "Current reviews use the latest collection; choose a historical review for a specific date."
            )
        context = _current_context(generated)
        return {**context, **stamps}
    base = decision_context(as_of, month=month, generated_at=generated_at)
    execution_at = pd.Timestamp(base["earliest_execution_at"])
    return {
        **base,
        "review_kind": "historical",
        "review_month": base["decision_date"][:7],
        "market_observation_date": base["decision_date"],
        "information_cutoff": base["decision_cutoff"],
        **stamps,
        "execution_basis": "after_decision_cutoff",
        "historical_basis": "reconstruction" if generated > execution_at else "recorded",
    }


def bundle_cutoff(bundle):
    """Information cutoff for a loaded bundle, honoring an explicit review timeline."""
    timeline = bundle.get("timeline") or {}
    if timeline.get("information_cutoff"):
        stamp = pd.Timestamp(timeline["information_cutoff"])
        if stamp.tzinfo is None:
            raise ValueError("Timeline information cutoff must be timezone-aware.")
        return stamp.tz_convert("UTC")
    return information_cutoff(bundle["as_of"])


def settle_execution(timeline, completed_at):
    """Return a timeline whose earliest execution follows the completed result.

    A current review executes at the first close after generation, which can be minutes
    away. When the analysis completes on or after that close, execution moves to the
    first session closing strictly after completion and ``generated_at`` records the
    completion instant; the information cutoff stays at load start. Historical
    timelines execute after their decision date and are returned unchanged.
    """
    settled = dict(timeline)
    if timeline.get("execution_basis") != "after_generation":
        return settled
    completed = _aware_utc(completed_at)
    if completed < pd.Timestamp(timeline["earliest_execution_at"]):
        return settled
    return {
        **settled,
        **_execution_stamps(_first_session_closing_after(completed)),
        "generated_at": completed.isoformat(),
    }


def new_york_dates(raw, parsed):
    """Read bare calendar dates as New York midnight instead of UTC midnight.

    ``parsed`` is ``raw`` parsed with ``utc=True``, which places ``YYYY-MM-DD`` at UTC
    midnight: 20:00 or 19:00 of the previous New York evening. A current review's cutoff
    is an instant inside a New York day, so a bare date is placed at the start of its New
    York day. Full timestamps are unchanged, and a historical 16:00 New York cutoff selects
    the same rows under either reading.
    """
    text = pd.Series(raw, copy=False).astype("string")
    date_only = text.str.fullmatch(DATE_ONLY).fillna(False).astype(bool) & parsed.notna()
    if not date_only.any():
        return parsed
    adjusted = parsed.copy()
    adjusted.loc[date_only] = (
        parsed.loc[date_only]
        .dt.tz_localize(None)
        .dt.tz_localize("America/New_York")
        .dt.tz_convert("UTC")
    )
    return adjusted
