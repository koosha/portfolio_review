"""Prospective forecast maturity audit and a chronological split primitive.

This is deliberately an outcome audit, not a portfolio backtest.  The caller
must supply the originally frozen forecasts and timestamped total-return price
observations; this module cannot prove that a forecast was made prospectively.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

FORECAST_COLUMNS = {
    "security_id",
    "forecast_date",
    "horizon_months",
    "scenario",
    "return_value",
    "probability",
    "basis",
}
PRICE_COLUMNS = {"security_id", "date", "adjusted_close", "available_at"}
LIMITATIONS = [
    "A frozen source record must establish that forecasts preceded outcomes; this function cannot certify prospective provenance.",
    "Price ratios use the supplied distribution-inclusive adjusted series. Source conventions and revisions require independent verification.",
    "Errors measure forecast accuracy, not portfolio performance or profitable alpha; fees, taxes, trade execution, risk, and benchmark comparisons are not included.",
    "Monthly 6/12/18-month forecasts overlap. Observation counts are not independent sample sizes, and no significance claim is made.",
    "The supplied subjective/calibrated label is retained as metadata; it is not a validation certificate.",
]


def _issue(code: str, message: str, severity: str = "warning") -> dict:
    return {"severity": severity, "code": code, "message": message}


def _timestamp(value: Any, name: str) -> pd.Timestamp:
    try:
        stamp = pd.Timestamp(value)
        if pd.isna(stamp):
            raise ValueError
        return stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")
    except (ValueError, TypeError, OverflowError):
        raise ValueError(f"{name} must be a valid date or timestamp") from None


def _series_dates(values: pd.Series) -> pd.Series:
    return pd.to_datetime(values, errors="coerce", utc=True, format="mixed")


def _finite(value: Any) -> float | None:
    if isinstance(value, (bool, np.bool_)):
        return None
    try:
        parsed = float(value)
    except (ValueError, TypeError):
        return None
    return parsed if math.isfinite(parsed) else None


def _stats(rows: list[dict]) -> dict:
    scored = [r for r in rows if r["status"] == "scored"]
    matured = [r for r in rows if r.get("matured") is True]
    errors = np.array([r["error"] for r in scored], dtype=float)
    return {
        "forecast_count": len(rows),
        "matured_count": len(matured),
        "scored_count": len(scored),
        "immature_count": sum(r["status"] == "immature" for r in rows),
        "missing_endpoint_count": sum(r["status"] == "missing_endpoint" for r in rows),
        "invalid_forecast_count": sum(r["status"] == "invalid_forecast" for r in rows),
        "matured_coverage": len(scored) / len(matured) if matured else None,
        "mean_error": float(errors.mean()) if len(errors) else None,
        "mae": float(np.abs(errors).mean()) if len(errors) else None,
        "rmse": float(np.sqrt(np.square(errors).mean())) if len(errors) else None,
    }


def _eligible_prices(
    prices: pd.DataFrame, cutoff: pd.Timestamp, issues: list[dict]
) -> pd.DataFrame:
    columns = ["security_id", "date", "adjusted_close", "available_at"]
    if not isinstance(prices, pd.DataFrame) or not PRICE_COLUMNS <= set(prices.columns):
        missing = (
            sorted(PRICE_COLUMNS - set(prices.columns))
            if isinstance(prices, pd.DataFrame)
            else sorted(PRICE_COLUMNS)
        )
        issues.append(
            _issue(
                "EVALUATION_PRICE_COLUMNS_MISSING",
                f"Price data are missing columns: {', '.join(missing)}.",
            )
        )
        return pd.DataFrame(columns=columns)
    data = prices.loc[:, columns].copy()
    data["date"] = _series_dates(data["date"]).dt.normalize()
    data["available_at"] = _series_dates(data["available_at"])
    data["adjusted_close"] = data["adjusted_close"].map(_finite)
    valid = (
        data["security_id"].notna()
        & data["security_id"].astype(str).str.strip().ne("")
        & data["date"].notna()
        & data["available_at"].notna()
        & data["adjusted_close"].notna()
        & data["adjusted_close"].gt(0)
    )
    invalid_count = int((~valid).sum())
    if invalid_count:
        issues.append(
            _issue(
                "EVALUATION_PRICE_ROWS_INVALID",
                f"Excluded {invalid_count} rows with missing identities, dates, publication times, or positive adjusted prices.",
            )
        )
    data = data.loc[valid].copy()
    # A price allegedly available before its own observation date has broken
    # provenance. A same-date close may publish later that day or the next day.
    bad_timing = data["available_at"] < data["date"]
    if bad_timing.any():
        issues.append(
            _issue(
                "EVALUATION_PRICE_TIMING_INVALID",
                f"Excluded {int(bad_timing.sum())} prices published before their observation date.",
            )
        )
        data = data.loc[~bad_timing].copy()
    usable = (data["date"] <= cutoff.normalize()) & (data["available_at"] <= cutoff)
    excluded = int((~usable).sum())
    if excluded:
        issues.append(
            _issue(
                "EVALUATION_PRICE_AFTER_CUTOFF",
                f"Excluded {excluded} observations dated or made available after the evaluation cutoff.",
                "info",
            )
        )
    data = data.loc[usable].copy()
    data["security_id"] = data["security_id"].astype(str)
    if data.empty:
        return data
    # Latest permissible revision wins only if that timestamp is unambiguous.
    newest = data.groupby(["security_id", "date"])["available_at"].transform("max")
    latest = data.loc[data["available_at"] == newest].copy()
    conflicts = latest.groupby(["security_id", "date"])["adjusted_close"].transform("nunique") > 1
    if conflicts.any():
        count = len(latest.loc[conflicts, ["security_id", "date"]].drop_duplicates())
        issues.append(
            _issue(
                "EVALUATION_PRICE_CONFLICT",
                f"Excluded {count} security/date observations with conflicting prices at the same publication timestamp.",
            )
        )
        latest = latest.loc[~conflicts].copy()
    return latest.drop_duplicates(["security_id", "date"]).sort_values(["security_id", "date"])


def evaluate_forecasts(
    frozen_bundle: dict, future_prices_df: pd.DataFrame, evaluation_date: str
) -> dict:
    """Score frozen joint-scenario forecasts after their *full* horizon matures.

    Grouping is security_id, horizon_months, forecast_date. Valid horizons are
    6, 12, and 18 calendar months. The target date is forecast_date + horizon;
    it is not shortened to the last price supplied. The start is the first
    observation on/after forecast_date within seven calendar days; the end is
    the last on/before target_date within seven days, and must follow start.

    ``evaluation_date`` is interpreted as an inclusive UTC calendar date;
    published observations are admitted through 23:59:59.999999999 that date.
    A delayed publication is unavailable until that cutoff includes it, even
    when its observation date is historical. No input is mutated.

    Predicted return is sum(probability * scenario return). Error is predicted
    minus realized. The source forecasts must have unique scenario names,
    probabilities summing to one, a consistent basis, and returns >= -1.
    """
    eval_day = _timestamp(evaluation_date, "evaluation_date").normalize()
    cutoff = eval_day + pd.Timedelta(days=1) - pd.Timedelta(nanoseconds=1)
    output = {
        "status": "unavailable",
        "evaluation_date": eval_day.date().isoformat(),
        "cutoff_utc": cutoff.isoformat(),
        "error_convention": "predicted_return - realized_return",
        "results": [],
        "summary": _stats([]),
        "by_horizon": [],
        "issues": [],
        "limitations": list(LIMITATIONS),
    }
    if not isinstance(frozen_bundle, dict):
        output["issues"].append(
            _issue(
                "EVALUATION_BUNDLE_INVALID",
                "Supply a frozen bundle containing the original forecasts.",
            )
        )
        return output
    forecasts = frozen_bundle.get("forecasts")
    if isinstance(forecasts, list):
        forecasts = pd.DataFrame(forecasts)
    if forecasts is None or isinstance(forecasts, pd.DataFrame) and forecasts.empty:
        output["status"] = "no_forecasts"
        return output
    if not isinstance(forecasts, pd.DataFrame) or not FORECAST_COLUMNS <= set(forecasts.columns):
        missing = (
            sorted(FORECAST_COLUMNS - set(forecasts.columns))
            if isinstance(forecasts, pd.DataFrame)
            else sorted(FORECAST_COLUMNS)
        )
        output["issues"].append(
            _issue(
                "EVALUATION_FORECAST_COLUMNS_MISSING",
                f"Forecasts are missing columns: {', '.join(missing)}.",
            )
        )
        return output
    data = forecasts.copy()
    data["_forecast_day"] = _series_dates(data["forecast_date"]).dt.normalize()
    # Invalid group identifiers cannot form a legitimate forecast observation.
    valid_keys = (
        data["security_id"].notna()
        & data["security_id"].astype(str).str.strip().ne("")
        & data["_forecast_day"].notna()
        & data["horizon_months"].notna()
    )
    invalid_key_count = int((~valid_keys).sum())
    if invalid_key_count:
        output["issues"].append(
            _issue(
                "EVALUATION_FORECAST_KEYS_INVALID",
                f"Excluded {invalid_key_count} scenario rows with missing security, horizon, or forecast date; they cannot define a forecast group.",
            )
        )
    data = data.loc[valid_keys].copy()
    data["security_id"] = data["security_id"].astype(str)
    prices = _eligible_prices(future_prices_df, cutoff, output["issues"])
    for (security, raw_horizon, forecast_day), group in data.groupby(
        ["security_id", "horizon_months", "_forecast_day"], sort=True
    ):
        horizon_number = _finite(raw_horizon)
        valid_horizon = horizon_number in (6, 12, 18)
        horizon = int(horizon_number) if valid_horizon else None
        target = forecast_day + pd.DateOffset(months=horizon) if valid_horizon else None
        result = {
            "security_id": security,
            "horizon_months": horizon,
            "forecast_date": forecast_day.date().isoformat(),
            "target_date": target.date().isoformat() if target is not None else None,
            "matured": bool(target <= eval_day) if target is not None else None,
            "status": "invalid_forecast",
            "basis": None,
            "predicted_return": None,
            "realized_return": None,
            "error": None,
            "absolute_error": None,
            "start_date": None,
            "end_date": None,
            "start_adjusted_close": None,
            "end_adjusted_close": None,
            "scenarios": [],
            "issues": [],
        }
        if not valid_horizon:
            result["issues"].append(
                _issue(
                    "EVALUATION_HORIZON_INVALID",
                    "Only 6, 12, and 18 calendar-month forecasts are supported.",
                )
            )
        bases = group["basis"].dropna().unique().tolist()
        if (
            len(bases) != 1
            or bases[0] not in {"subjective", "calibrated"}
            or group["basis"].isna().any()
        ):
            result["issues"].append(
                _issue(
                    "EVALUATION_BASIS_INVALID",
                    "A forecast group must have one consistent subjective or calibrated basis.",
                )
            )
        else:
            result["basis"] = bases[0]
        labels = group["scenario"]
        if (
            labels.isna().any()
            or labels.astype(str).str.strip().eq("").any()
            or labels.astype(str).duplicated().any()
        ):
            result["issues"].append(
                _issue(
                    "EVALUATION_SCENARIO_INVALID",
                    "Scenario names must be present and unique in each forecast group.",
                )
            )
        probabilities = [_finite(x) for x in group["probability"]]
        returns = [_finite(x) for x in group["return_value"]]
        if (
            any(x is None or x < 0 or x > 1 for x in probabilities)
            or abs(sum(x or 0 for x in probabilities) - 1) > 1e-8
        ):
            result["issues"].append(
                _issue(
                    "EVALUATION_PROBABILITIES_INVALID",
                    "Scenario probabilities must be finite, nonnegative, and sum to one.",
                )
            )
        if any(x is None or x < -1 for x in returns):
            result["issues"].append(
                _issue(
                    "EVALUATION_RETURNS_INVALID", "Scenario returns must be finite and at least -1."
                )
            )
        if result["issues"]:
            output["results"].append(result)
            continue
        result["predicted_return"] = sum(p * r for p, r in zip(probabilities, returns))
        result["scenarios"] = [
            {"scenario": str(s), "probability": p, "return_value": r}
            for s, p, r in zip(labels, probabilities, returns)
        ]
        if not result["matured"]:
            result["status"] = "immature"
            result["issues"].append(
                _issue(
                    "EVALUATION_HORIZON_IMMATURE",
                    f"No outcome is scored before the full target date {result['target_date']}.",
                    "info",
                )
            )
            output["results"].append(result)
            continue
        security_prices = prices.loc[prices["security_id"] == security]
        start_rows = security_prices.loc[
            (security_prices["date"] >= forecast_day)
            & (security_prices["date"] <= forecast_day + pd.Timedelta(days=7))
        ]
        end_rows = security_prices.loc[
            (security_prices["date"] <= target)
            & (security_prices["date"] >= target - pd.Timedelta(days=7))
        ]
        if start_rows.empty:
            result["issues"].append(
                _issue(
                    "EVALUATION_START_MISSING",
                    "No usable adjusted price within seven days on/after the forecast date.",
                )
            )
        else:
            start = start_rows.iloc[0]
            result["start_date"] = start["date"].date().isoformat()
            result["start_adjusted_close"] = float(start["adjusted_close"])
        if end_rows.empty:
            result["issues"].append(
                _issue(
                    "EVALUATION_END_MISSING",
                    "No usable adjusted price within seven days on/before the full target date.",
                )
            )
        else:
            end = end_rows.iloc[-1]
            result["end_date"] = end["date"].date().isoformat()
            result["end_adjusted_close"] = float(end["adjusted_close"])
        if not start_rows.empty and not end_rows.empty and end["date"] <= start["date"]:
            result["issues"].append(
                _issue(
                    "EVALUATION_ENDPOINT_ORDER_INVALID",
                    "The end observation must follow the start observation.",
                )
            )
        if result["issues"]:
            result["status"] = "missing_endpoint"
        else:
            result["status"] = "scored"
            result["realized_return"] = (
                result["end_adjusted_close"] / result["start_adjusted_close"] - 1
            )
            result["error"] = result["predicted_return"] - result["realized_return"]
            result["absolute_error"] = abs(result["error"])
        output["results"].append(result)

    output["summary"] = _stats(output["results"])
    output["summary"]["invalid_scenario_key_rows"] = invalid_key_count
    for horizon in (6, 12, 18):
        rows = [r for r in output["results"] if r["horizon_months"] == horizon]
        if rows:
            output["by_horizon"].append({"horizon_months": horizon, **_stats(rows)})
    output["status"] = (
        "evaluated"
        if output["summary"]["scored_count"]
        else (
            "no_matured_forecasts"
            if output["results"] and all(r["status"] == "immature" for r in output["results"])
            else "unavailable"
        )
    )
    return output


def purged_walk_forward_splits(
    frame: pd.DataFrame,
    validation_start: str,
    validation_end: str,
    date_col: str = "forecast_date",
    label_end_col: str = "label_end",
) -> dict:
    """Return original indices for one chronological, strictly purged fold.

    Train rows must originate before validation_start AND have label_end
    strictly before validation_start. Validation origins are [start, end).
    Invalid dates, labels preceding origins, and all other rows are excluded.
    The caller must separately require validation outcomes to be matured by
    the evaluation cutoff; this split helper does not look into the future.
    """
    if not isinstance(frame, pd.DataFrame) or not {date_col, label_end_col} <= set(frame.columns):
        raise ValueError("The frame must contain forecast and label-end columns")
    if not frame.index.is_unique:
        raise ValueError("Row indices must be unique to identify fold membership unambiguously")
    start = _timestamp(validation_start, "validation_start")
    end = _timestamp(validation_end, "validation_end")
    if start >= end:
        raise ValueError("validation_end must be later than validation_start")
    origins = _series_dates(frame[date_col])
    label_ends = _series_dates(frame[label_end_col])
    valid = origins.notna() & label_ends.notna() & (label_ends >= origins)
    train = valid & (origins < start) & (label_ends < start)
    validation = valid & (origins >= start) & (origins < end)
    excluded = ~(train | validation)
    return {
        "training": frame.index[train].tolist(),
        "validation": frame.index[validation].tolist(),
        "excluded": frame.index[excluded].tolist(),
    }
