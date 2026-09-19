"""One versioned shared state; one dated joint scenario set for every security.

The allocation engine refuses per-security forecasts that do not share a date and a
label set, and it never invents a return of its own. This module holds the single
owner-editable statement of the world — a small number of market states, a sector
multiplier per state, and a currency state per foreign quote currency — and expands it
into the joint frame the engine already validates.

Nothing here is a measurement. Every produced row carries ``basis="subjective"`` and
names the mapping that produced it, so a reader can tell an owner's stated view from an
observed fact. Company valuation proposals are never folded in automatically: applying
one stays an explicit action.
"""

from __future__ import annotations

import math
import re
from copy import deepcopy
from numbers import Real

import pandas as pd

from .fx import usd_scenario_return

SHARED_STATE_VERSION = "shared-state-1"
FRAME_COLUMNS = [
    "security_id",
    "scenario",
    "horizon_months",
    "return_value",
    "probability",
    "basis",
    "source",
    "forecast_date",
    "calibration_id",
    "received_at",
    "available_at",
]
SOURCE_LABEL = f"{SHARED_STATE_VERSION} mapping; owner-editable"
STATE_KEYS = {
    "version",
    "horizon_months",
    "market_returns",
    "sector_multipliers",
    "fx_returns",
    "asset_overrides",
    "probabilities",
}
CURRENCY_PATTERN = re.compile(r"^[A-Z]{3}$")
NEUTRAL_MULTIPLIER = 1.0
STATE_COUNT = 3
# The market states below are US ordinary-equity total returns. They describe an equity
# and a broad equity benchmark; they describe neither a bond fund nor a commodity trust,
# and this module never lends them to one. An owner who wants a view on such a holding
# states it in ``asset_overrides``, where it reads as their own statement.
EQUITY_INSTRUMENTS = frozenset({"equity", "common_stock", "ordinary_common", "stock", "share"})
CASH_INSTRUMENTS = frozenset({"cash"})

# Twelve-month total returns by state. `fx_returns` are stated in the presentation
# currency per unit of the foreign currency, so a foreign listing composes its local
# state return with its currency state return.
DEFAULT_SHARED_STATE = {
    "version": SHARED_STATE_VERSION,
    "horizon_months": 12,
    "market_returns": {"Adverse": -0.20, "Central": 0.06, "Favorable": 0.18},
    "sector_multipliers": {
        "Technology": 1.2,
        "Communication Services": 1.1,
        "Consumer Discretionary": 1.1,
        "Health Care": 0.9,
        "Consumer Staples": 0.7,
        "Utilities": 0.6,
        "Energy": 1.0,
        "Industrials": 1.0,
        "Materials": 1.0,
        "Financials": 1.0,
        "Real Estate": 0.9,
        "Unknown": 1.0,
    },
    "fx_returns": {
        "CAD": {"Adverse": -0.06, "Central": 0.0, "Favorable": 0.04},
        "GBP": {"Adverse": -0.08, "Central": 0.0, "Favorable": 0.05},
        "EUR": {"Adverse": -0.07, "Central": 0.0, "Favorable": 0.05},
    },
    "asset_overrides": {},
    "probabilities": None,
}


def _issue(issues, code, security_id, detail, severity="warning"):
    """Record one gap in the mapping. A gap never silently becomes a number."""
    issues.append(
        {
            "code": code,
            "security_id": security_id,
            "detail": detail,
            "severity": severity,
            "message": f"{security_id}: {detail}" if security_id else detail,
        }
    )


def _number(value, name, low=None, high=None) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{name} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be a finite number")
    if (low is not None and number < low) or (high is not None and number > high):
        raise ValueError(f"{name} must be between {low} and {high}")
    return number


def _whole(value, name, low, high) -> int:
    number = _number(value, name, low, high)
    if not float(number).is_integer():
        raise ValueError(f"{name} must be a whole number of months")
    return int(number)


def _mapping(value, name) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    for key in value:
        if not isinstance(key, str) or not key.strip():
            raise ValueError(f"{name} keys must be nonempty names")
    return value


def _labels(state) -> list[str]:
    return sorted(state["market_returns"])


def validate_shared_state(
    state, *, name: str = "shared_state", require_probability_sum: bool = True
) -> dict:
    """Validate one shared state. Every problem is refused, never repaired."""
    if not isinstance(state, dict):
        raise ValueError(f"{name} must be an object")
    missing = sorted(STATE_KEYS - set(state))
    unknown = sorted(set(state) - STATE_KEYS)
    if missing or unknown:
        raise ValueError(
            f"{name} must state exactly {sorted(STATE_KEYS)}"
            + (f"; missing {missing}" if missing else "")
            + (f"; unknown {unknown}" if unknown else "")
        )
    version = state["version"]
    if not isinstance(version, str) or not version.strip():
        raise ValueError(f"{name}.version must be a nonempty version string")
    if version != SHARED_STATE_VERSION:
        raise ValueError(
            f"{name}.version {version!r} is not supported; this build reads {SHARED_STATE_VERSION}"
        )
    _whole(state["horizon_months"], f"{name}.horizon_months", 1, 120)

    market = _mapping(state["market_returns"], f"{name}.market_returns")
    if len(market) != STATE_COUNT:
        raise ValueError(f"{name}.market_returns must state exactly {STATE_COUNT} market states")
    for label, value in market.items():
        _number(value, f"{name}.market_returns.{label}", -1, 10)
    labels = set(market)

    worst = min(market.values())
    for sector, value in _mapping(
        state["sector_multipliers"], f"{name}.sector_multipliers"
    ).items():
        multiplier = _number(value, f"{name}.sector_multipliers.{sector}", 0, 100)
        # A holding cannot lose more than itself. A multiplier that would state a worse
        # loss than total is refused here, so no expansion ever has to decide what
        # "worse than -100%" compounds to.
        if multiplier * worst < -1:
            raise ValueError(
                f"{name}.sector_multipliers.{sector} ({multiplier:g}) times the worst market "
                f"state ({worst:g}) states a loss worse than the whole investment"
            )

    for code, states in _mapping(state["fx_returns"], f"{name}.fx_returns").items():
        if not CURRENCY_PATTERN.fullmatch(code):
            raise ValueError(f"{name}.fx_returns keys must be three-letter currency codes")
        if set(_mapping(states, f"{name}.fx_returns.{code}")) != labels:
            raise ValueError(f"{name}.fx_returns.{code} must cover every market state once")
        for label, value in states.items():
            _number(value, f"{name}.fx_returns.{code}.{label}", -1, 10)

    for sid, overrides in _mapping(state["asset_overrides"], f"{name}.asset_overrides").items():
        scenarios = _mapping(overrides, f"{name}.asset_overrides.{sid}")
        if not scenarios or set(scenarios) - labels:
            raise ValueError(f"{name}.asset_overrides.{sid} must name declared market states only")
        for label, value in scenarios.items():
            _number(value, f"{name}.asset_overrides.{sid}.{label}", -1, 10)

    probabilities = state["probabilities"]
    if probabilities is not None:
        if set(_mapping(probabilities, f"{name}.probabilities")) != labels:
            raise ValueError(f"{name}.probabilities must cover every market state exactly once")
        for label, value in probabilities.items():
            _number(value, f"{name}.probabilities.{label}", 0, 1)
        if require_probability_sum and abs(sum(probabilities.values()) - 1) > 1e-8:
            raise ValueError(f"{name}.probabilities must sum to one; they are never normalized")
    return state


def _as_of_text(value) -> str:
    stamp = pd.Timestamp(value) if not isinstance(value, str) else pd.Timestamp(value)
    if pd.isna(stamp):
        raise ValueError("as_of must be a valid calendar date")
    return stamp.date().isoformat()


def _currency(value) -> str | None:
    if not isinstance(value, str):
        return None
    code = value.strip().upper()
    return code if CURRENCY_PATTERN.fullmatch(code) else None


def _sector(value) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _instrument(record) -> str | None:
    kind = record.get("instrument_type")
    return kind.strip().lower() if isinstance(kind, str) and kind.strip() else None


def _is_cash(record, security_id) -> bool:
    return security_id == "CASH" or _instrument(record) in CASH_INSTRUMENTS


def _is_equity(record, security_id, benchmarks) -> bool:
    """Whether the equity market states describe this asset at all.

    An ordinary equity, or a broad equity benchmark the mandate names. Everything else —
    a bond fund, a commodity trust, a security whose instrument type was never resolved —
    is left to say for itself, because a market state for US shares is not a view about
    it.
    """
    return _instrument(record) in EQUITY_INSTRUMENTS or security_id in benchmarks


def _scale(stated: float, horizon_months: int, basis_months: int) -> float:
    """Compound a return stated over ``basis_months`` onto the requested horizon.

    A base of exactly zero is a total loss, which compounds to a total loss at every
    horizon. A negative base is not a return at all; validation refuses the states that
    could produce one, and this guard makes sure a fractional exponent can never turn
    one into a complex number behind the engine's back.
    """
    base = 1 + stated
    if base <= 0:
        return -1.0
    return base ** (horizon_months / basis_months) - 1


def _distribution(state, issues) -> dict | None:
    probabilities = state["probabilities"]
    if probabilities is None:
        return None
    if abs(sum(probabilities.values()) - 1) > 1e-8:
        _issue(
            issues,
            "INCOMPLETE_STATE_PROBABILITIES",
            None,
            "State probabilities do not sum to one; scenario outcomes stay unweighted.",
        )
        return None
    return dict(probabilities)


def generate_joint_forecasts(
    securities_frame,
    shared_state,
    *,
    as_of,
    horizon_months,
    presentation_currency: str = "USD",
    benchmark_ids=(),
) -> pd.DataFrame:
    """Expand one shared state into a dated joint scenario set.

    Every returned security carries the same ``forecast_date`` and the same scenario
    labels, which is what the allocation engine requires of a joint set. Assets the
    state cannot price — an instrument the equity states do not describe, or a foreign
    quote currency with no declared currency state — are left out with an issue rather
    than priced on an assumed rate. Cash is never included here: its horizon return
    stays the explicit ``allocation.cash_return`` setting.

    ``benchmark_ids`` names the broad equity benchmarks the market states do describe,
    so a mandate's index fund is priced while an aggregate-bond fund is not.

    Issues are attached to the returned frame as ``frame.attrs["issues"]``.
    """
    state = validate_shared_state(
        deepcopy(shared_state), name="shared_state", require_probability_sum=False
    )
    horizon = _whole(horizon_months, "horizon_months", 1, 120)
    forecast_date = _as_of_text(as_of)
    presentation = _currency(presentation_currency)
    if presentation is None:
        raise ValueError("presentation_currency must be a three-letter currency code")

    issues: list[dict] = []
    labels = _labels(state)
    multipliers = state["sector_multipliers"]
    overrides = state["asset_overrides"]
    probabilities = _distribution(state, issues)
    # The state says what period its returns are returns over. Requesting a different
    # horizon compounds them onto it, and says so: a stated number is never quietly
    # reinterpreted as a number for some other period.
    basis = int(state["horizon_months"])
    benchmarks = {str(sid).strip() for sid in (benchmark_ids or ()) if str(sid).strip()}
    if basis != horizon:
        _issue(
            issues,
            "SHARED_STATE_HORIZON_RESCALED",
            None,
            f"The shared state states {basis}-month returns; every row below compounds "
            f"them onto the {horizon}-month allocation horizon.",
            "info",
        )

    frame = securities_frame if isinstance(securities_frame, pd.DataFrame) else pd.DataFrame()
    records = frame.to_dict("records") if not frame.empty and "security_id" in frame.columns else []
    rows: list[dict] = []
    seen: set[str] = set()
    for record in records:
        raw = record.get("security_id")
        security_id = raw.strip() if isinstance(raw, str) else None
        if not security_id:
            _issue(issues, "MISSING_SECURITY_ID", None, "A security row has no identifier.")
            continue
        if security_id in seen:
            _issue(
                issues,
                "DUPLICATE_SECURITY_ROW",
                security_id,
                "Repeated security row; the first row states this asset's scenarios.",
            )
            continue
        seen.add(security_id)
        if _is_cash(record, security_id):
            continue
        if not _is_equity(record, security_id, benchmarks):
            _issue(
                issues,
                "NO_STATE_FOR_INSTRUMENT",
                security_id,
                f"The shared state describes US equity market states; {_instrument(record) or 'an unresolved instrument'} "
                "is not one of them, so no scenario is stated. State a view in "
                "allocation.shared_state.asset_overrides to cover it.",
            )
            continue
        currency = _currency(record.get("currency"))
        if currency is None:
            _issue(
                issues,
                "MISSING_QUOTE_CURRENCY",
                security_id,
                "No quote currency, so no presentation-currency scenario is stated.",
            )
            continue
        fx_states = None
        if currency != presentation:
            fx_states = state["fx_returns"].get(currency)
            if fx_states is None:
                _issue(
                    issues,
                    "MISSING_FX_STATE",
                    security_id,
                    f"The shared state declares no {currency} currency state, so no "
                    f"{presentation} scenario is stated.",
                )
                continue
        sector = _sector(record.get("sector"))
        multiplier = multipliers.get(sector) if sector is not None else None
        if multiplier is None:
            _issue(
                issues,
                "UNKNOWN_SECTOR_MULTIPLIER",
                security_id,
                f"No multiplier for sector {sector or 'Unknown'}; the market state return is "
                "used unscaled.",
            )
            multiplier = NEUTRAL_MULTIPLIER
        for label in labels:
            override = overrides.get(security_id, {}).get(label)
            local = (
                float(override)
                if override is not None
                else float(multiplier) * float(state["market_returns"][label])
            )
            value = _scale(local, horizon, basis)
            if fx_states is not None:
                value = usd_scenario_return(value, _scale(float(fx_states[label]), horizon, basis))
            rows.append(
                {
                    "security_id": security_id,
                    "scenario": label,
                    "horizon_months": horizon,
                    "return_value": value,
                    "probability": probabilities[label] if probabilities else None,
                    "basis": "subjective",
                    "source": SOURCE_LABEL,
                    "forecast_date": forecast_date,
                    "calibration_id": None,
                    # An expansion of the owner's own state is knowable on the date it
                    # is stated for; it waits on no provider. Saying so lets strict
                    # receipt mode keep the set instead of refusing a whole review.
                    "received_at": forecast_date,
                    "available_at": forecast_date,
                }
            )
    result = pd.DataFrame(rows, columns=FRAME_COLUMNS)
    result.attrs["issues"] = issues
    result.attrs["method_version"] = SHARED_STATE_VERSION
    result.attrs["forecast_date"] = forecast_date
    return result
