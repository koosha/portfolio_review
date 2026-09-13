"""Deterministic, conditional company valuations; no provider or portfolio side effects.

Amounts use the declared currency. EPS/distributions are per starting share; DCF
amounts use monetary_unit and diluted_shares is an actual count of shares.
Terminal reinvestment follows g / ROIC, not last-year FCFF blindly grown by g.
Method reference: https://pages.stern.nyu.edu/~adamodar/New_Home_Page/background/valintro.htm
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any

METHOD_VERSION = "company-valuation-v1"
LABELS = ("adverse", "central", "favorable")
HORIZONS = (6, 12, 18)
UNIT_FACTORS = {"units": 1, "thousands": 1_000, "millions": 1_000_000, "billions": 1_000_000_000}


def _mapping(value: Any, path: str) -> Mapping:
    if not isinstance(value, Mapping):
        raise ValueError(f"{path} must be an object")
    return value


def _number(value: Any, path: str, issues: list[str], *, minimum=None, positive=False):
    if value is None:
        issues.append(f"{path} is missing")
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"{path} must be a finite number")
    try:
        number = float(value)
    except (ValueError, OverflowError) as exc:
        raise ValueError(f"{path} must be a finite number") from exc
    if not math.isfinite(number):
        raise ValueError(f"{path} must be a finite number")
    if positive and number <= 0:
        raise ValueError(f"{path} must be positive")
    if minimum is not None and number < minimum:
        raise ValueError(f"{path} must be at least {minimum}")
    return number


def _finite(value: float, path: str) -> float:
    if not math.isfinite(value):
        raise ValueError(f"{path} overflowed; reduce input magnitudes")
    return value


def _currency(value: Any, issues: list[str]) -> str | None:
    if value is None or value == "":
        issues.append("currency is missing")
        return None
    if not isinstance(value, str) or len(value) != 3 or not value.isascii() or not value.isalpha():
        raise ValueError("currency must be a three-letter currency code")
    return value.upper()


def _horizon(value: Any, issues: list[str]):
    if value is None:
        issues.append("horizon_months is missing")
        return None
    if isinstance(value, bool) or value not in HORIZONS:
        raise ValueError("horizon_months must be 6, 12, or 18")
    return int(value)


def _no_buyback_addition(data: Mapping, path: str):
    for key in ("buyback_yield", "buybacks_per_share", "buyback_return"):
        if data.get(key) is not None:
            value = _number(data[key], f"{path}.{key}", [])
            if value != 0:
                raise ValueError(
                    "Buybacks must enter shares/EPS, never an additive return or distribution"
                )


def calculate_eps(payload: Mapping) -> dict:
    """Value adverse/central/favorable EPS scenarios, retaining unavailable outcomes.

    Missing inputs block affected outcomes. Invalid supplied values raise ValueError.
    Negative/zero earnings are valid observations but cannot support this P/E model.
    Distributions are cash paid per starting share without dividend reinvestment.
    """
    data = _mapping(payload, "EPS input")
    issues: list[str] = []
    price = _number(data.get("starting_price"), "starting_price", issues, positive=True)
    currency = _currency(data.get("currency"), issues)
    horizon = _horizon(data.get("horizon_months"), issues)
    eps_basis, pe_basis = data.get("eps_convention"), data.get("pe_convention")
    for field, value in (("eps_convention", eps_basis), ("pe_convention", pe_basis)):
        if value is None:
            issues.append(f"{field} is missing")
        elif value not in ("trailing", "forward"):
            raise ValueError(f"{field} must be trailing or forward")
    if eps_basis is not None and pe_basis is not None and eps_basis != pe_basis:
        raise ValueError("EPS and P/E must use matching trailing/forward conventions")
    _no_buyback_addition(data, "input")
    supplied = data.get("scenarios", [])
    if not isinstance(supplied, list):
        raise ValueError("scenarios must be a list")
    by_label = {}
    for item in supplied:
        scenario = _mapping(item, "scenario")
        label = scenario.get("label")
        if label not in LABELS or label in by_label:
            raise ValueError("Scenario labels must be unique adverse, central, and favorable")
        by_label[label] = scenario
    results = []
    for label in LABELS:
        row = by_label.get(label, {})
        reasons = list(issues)
        eps = _number(row.get("eps"), f"{label}.eps", reasons)
        multiple = _number(row.get("pe"), f"{label}.pe", reasons, positive=True)
        distributions = _number(
            row.get("distributions_per_starting_share"),
            f"{label}.distributions_per_starting_share",
            reasons,
            minimum=0,
        )
        _no_buyback_addition(row, label)
        for field, convention in (("eps_convention", eps_basis), ("pe_convention", pe_basis)):
            if row.get(field, convention) != convention:
                raise ValueError(f"{label}.{field} differs from the scenario set's convention")
        if row.get("currency", currency) != currency:
            reasons.append(f"{label}: mixed currencies require an explicit verified FX conversion")
        if eps is not None and eps <= 0:
            reasons.append(f"{label}: a positive earnings basis is required for P/E valuation")
        terminal_price = total_return = None
        if not reasons:
            terminal_price = _finite(eps * multiple, f"{label}.horizon_price")
            total_return = _finite((terminal_price + distributions) / price - 1, f"{label}.return")
        results.append(
            {
                "label": label,
                "eps": eps,
                "pe": multiple,
                "distributions_per_starting_share": distributions,
                "horizon_price": terminal_price,
                "total_return": total_return,
                "status": "blocked" if reasons else "ready",
                "issues": reasons,
            }
        )
    available = sum(row["status"] == "ready" for row in results)
    return {
        "method_version": METHOD_VERSION,
        "model": "eps_multiple",
        "status": "ready" if available == 3 else "partial" if available else "blocked",
        "currency": currency,
        "starting_price": price,
        "horizon_months": horizon,
        "eps_convention": eps_basis,
        "pe_convention": pe_basis,
        "distribution_convention": "cash_per_starting_share_without_reinvestment",
        "buyback_convention": "reflected_in_eps_and_shares_only",
        "return_unit": "fraction_over_horizon",
        "forecast_type": "subjective_scenario",
        "calibrated": False,
        "scenarios": results,
        "issues": list(dict.fromkeys(reason for row in results for reason in row["issues"])),
    }


def _grid(values: Sequence, name: str) -> list[float]:
    if not isinstance(values, (list, tuple)) or not 1 <= len(values) <= 25:
        raise ValueError(f"{name} must contain 1 to 25 values")
    if any(value is None for value in values):
        raise ValueError(f"{name} cannot contain missing values")
    return [_number(value, name, [], positive=True) for value in values]


def eps_sensitivity(payload: Mapping, eps_values: Sequence, pe_values: Sequence) -> dict:
    """Grid of prices/returns, using the central scenario's cash distributions."""
    eps_axis, pe_axis = _grid(eps_values, "eps_values"), _grid(pe_values, "pe_values")
    original = calculate_eps(payload)
    central = next(row for row in original["scenarios"] if row["label"] == "central")
    rows = []
    for eps in eps_axis:
        cells = []
        for pe in pe_axis:
            draft = deepcopy(dict(payload))
            central_input = next(
                (
                    dict(row)
                    for row in payload.get("scenarios", [])
                    if row.get("label") == "central"
                ),
                {"label": "central"},
            )
            central_input.update(
                eps=eps,
                pe=pe,
                distributions_per_starting_share=central["distributions_per_starting_share"],
            )
            draft["scenarios"] = [central_input]
            result = calculate_eps(draft)["scenarios"][1]
            cells.append({key: result[key] for key in ("horizon_price", "total_return", "issues")})
        rows.append(cells)
    return {
        "eps_values": eps_axis,
        "pe_values": pe_axis,
        "currency": original["currency"],
        "horizon_months": original["horizon_months"],
        "cells": rows,
        "method_version": METHOD_VERSION,
    }


def calculate_dcf(payload: Mapping) -> dict:
    """End-of-year FCFF DCF for nonfinancial operating companies.

    Each projection's invested_capital is BEGINNING operating invested capital;
    the following year's capital must equal it plus net reinvestment. ROIC and
    growth diagnostics expose both capital growth and changes in operating returns.
    SBC remains an operating expense: either already in EBIT or deducted once here.
    """
    data = _mapping(payload, "DCF input")
    issues: list[str] = []
    currency = _currency(data.get("currency"), issues)
    monetary_unit = data.get("monetary_unit")
    if monetary_unit is None:
        issues.append("monetary_unit is missing")
    elif not isinstance(monetary_unit, str) or monetary_unit not in UNIT_FACTORS:
        raise ValueError("monetary_unit must be units, thousands, millions, or billions")
    if data.get("company_type") != "nonfinancial":
        issues.append("FCFF model requires confirmed nonfinancial company applicability")
    sbc = data.get("sbc_treatment")
    if sbc is None:
        issues.append("sbc_treatment is missing")
    elif sbc not in ("expensed_in_ebit", "deduct_from_ebit"):
        raise ValueError("sbc_treatment must be expensed_in_ebit or deduct_from_ebit")
    discount = _number(data.get("discount_rate"), "discount_rate", issues, positive=True)
    growth = _number(data.get("terminal_growth_rate"), "terminal_growth_rate", issues)
    terminal_roic = _number(data.get("terminal_roic"), "terminal_roic", issues, positive=True)
    if growth is not None and growth <= -1:
        raise ValueError("terminal_growth_rate must exceed -1")
    if growth is not None and discount is not None and growth >= discount:
        raise ValueError("terminal_growth_rate must be below discount_rate")
    if growth is not None and terminal_roic is not None and growth > terminal_roic:
        raise ValueError("terminal growth exceeds ROIC and requires over 100% reinvestment")
    bridge = {
        key: _number(data.get(key), key, issues, minimum=0)
        for key in ("debt", "preferred", "nci", "excess_cash", "nonoperating_assets")
    }
    shares = _number(data.get("diluted_shares"), "diluted_shares", issues, positive=True)
    if data.get("share_unit", "shares") != "shares":
        raise ValueError(
            "diluted_shares must be an actual count in shares, not scaled monetary units"
        )
    if data.get("bridge_monetary_unit", monetary_unit) != monetary_unit:
        issues.append("Bridge and operating projections use different monetary units")
    for field in ("bridge_currency", "discount_currency"):
        if data.get(field, currency) != currency:
            issues.append(f"{field} differs; provide a verified conversion before valuation")
    raw_rows = data.get("projections", [])
    if not isinstance(raw_rows, list) or len(raw_rows) > 50:
        raise ValueError("projections must be a list of at most 50 annual periods")
    if not raw_rows:
        issues.append("annual operating projections are missing")
    rows = []
    for index, raw in enumerate(raw_rows, 1):
        item = _mapping(raw, f"projections[{index}]")
        prefix = f"year_{index}"
        if item.get("year") != index or isinstance(item.get("year"), bool):
            raise ValueError("projection years must be consecutive integers starting at 1")
        if item.get("currency", currency) != currency:
            issues.append(f"{prefix}: mixed currencies require verified conversion")
        if item.get("monetary_unit", monetary_unit) != monetary_unit:
            issues.append(f"{prefix}: mixed monetary units require explicit conversion")
        row = {"year": index}
        for key in (
            "revenue",
            "ebit",
            "tax_rate",
            "depreciation",
            "capex",
            "change_working_capital",
            "invested_capital",
            "stock_compensation",
        ):
            row[key] = _number(
                item.get(key),
                f"{prefix}.{key}",
                issues,
                minimum=0 if key not in ("ebit", "change_working_capital") else None,
                positive=key == "invested_capital",
            )
        if row["tax_rate"] is not None and row["tax_rate"] > 1:
            raise ValueError(f"{prefix}.tax_rate must lie between 0 and 1")
        if row["revenue"] is not None and row["ebit"] is not None and row["ebit"] > row["revenue"]:
            raise ValueError(f"{prefix}.ebit cannot exceed operating revenue")
        if item.get("roic") is not None:
            row["asserted_roic"] = _number(item["roic"], f"{prefix}.roic", issues)
        rows.append(row)
    result = {
        "method_version": METHOD_VERSION,
        "model": "fcff_dcf",
        "status": "blocked",
        "currency": currency,
        "monetary_unit": monetary_unit,
        "share_unit": "shares",
        "discount_convention": "end_of_year",
        "sbc_treatment": sbc,
        "discount_rate": discount,
        "terminal_growth_rate": growth,
        "terminal_roic": terminal_roic,
        "enterprise_value": None,
        "common_equity_value": None,
        "value_per_share": None,
        "terminal_value": None,
        "terminal_present_value": None,
        "explicit_present_value": None,
        "terminal_reinvestment_rate": None,
        "terminal_fcff": None,
        "diluted_shares": shares,
        "bridge": bridge,
        "projections": [],
        "issues": issues,
        "forecast_type": "conditional_intrinsic_value",
        "horizon_total_return": None,
        "return_reason": "Intrinsic value is not a horizon price target; convergence must be explicit",
        "calibrated": False,
    }
    if issues:
        return result
    previous = None
    for row in rows:
        operating_ebit = row["ebit"] - (
            row["stock_compensation"] if sbc == "deduct_from_ebit" else 0
        )
        nopat = operating_ebit * (1 - row["tax_rate"])
        reinvestment = row["capex"] - row["depreciation"] + row["change_working_capital"]
        roic = nopat / row["invested_capital"]
        if previous and not math.isclose(
            row["invested_capital"],
            previous["invested_capital"] + previous["reinvestment"],
            rel_tol=1e-6,
            abs_tol=1e-8,
        ):
            raise ValueError(
                f"year_{row['year']}: invested capital does not reconcile to prior reinvestment"
            )
        if "asserted_roic" in row and not math.isclose(
            row["asserted_roic"], roic, rel_tol=1e-6, abs_tol=1e-8
        ):
            raise ValueError(
                f"year_{row['year']}: ROIC differs from NOPAT / beginning invested capital"
            )
        fcff = nopat - reinvestment
        try:
            present_value = fcff / (1 + discount) ** row["year"]
        except OverflowError as exc:
            raise ValueError("Discount factor overflowed") from exc
        row.update(
            {
                "operating_ebit": operating_ebit,
                "nopat": nopat,
                "reinvestment": reinvestment,
                "roic": roic,
                "reinvestment_rate": reinvestment / nopat if nopat != 0 else None,
                "fcff": fcff,
                "present_value": present_value,
                "nopat_growth": nopat / previous["nopat"] - 1
                if previous and previous["nopat"] > 0
                else None,
                "capital_growth": reinvestment / row["invested_capital"],
            }
        )
        for key, value in row.items():
            if isinstance(value, float):
                _finite(value, f"year_{row['year']}.{key}")
        previous = row
    result["projections"] = rows
    if rows[-1]["nopat"] <= 0:
        issues.append("Terminal NOPAT must be positive for this stable-growth FCFF model")
        return result
    terminal_reinvestment = growth / terminal_roic
    terminal_fcff = rows[-1]["nopat"] * (1 + growth) * (1 - terminal_reinvestment)
    terminal_value = terminal_fcff / (discount - growth)
    terminal_pv = terminal_value / (1 + discount) ** len(rows)
    try:
        explicit_pv = math.fsum(row["present_value"] for row in rows)
    except OverflowError as exc:
        raise ValueError("Present values overflowed; reduce input magnitudes") from exc
    ev = explicit_pv + terminal_pv
    equity = (
        ev
        - bridge["debt"]
        - bridge["preferred"]
        - bridge["nci"]
        + bridge["excess_cash"]
        + bridge["nonoperating_assets"]
    )
    per_share = equity / shares * UNIT_FACTORS[monetary_unit]
    values = {
        "enterprise_value": ev,
        "common_equity_value": equity,
        "value_per_share": per_share,
        "terminal_value": terminal_value,
        "terminal_present_value": terminal_pv,
        "explicit_present_value": explicit_pv,
        "terminal_reinvestment_rate": terminal_reinvestment,
        "terminal_fcff": terminal_fcff,
    }
    result.update({key: _finite(value, key) for key, value in values.items()})
    if equity < 0:
        result["value_per_share"] = None
        issues.append(
            "Claims exceed modeled asset value; the simple bridge cannot value common equity"
        )
    result["status"] = "blocked" if issues else "ready"
    return result


def dcf_sensitivity(
    payload: Mapping, discount_rates: Sequence, terminal_growth_rates: Sequence
) -> dict:
    """Keep invalid terminal cells visible, without relaxing their constraints."""
    rates = _grid(discount_rates, "discount_rates")
    if (
        not isinstance(terminal_growth_rates, (list, tuple))
        or not 1 <= len(terminal_growth_rates) <= 25
    ):
        raise ValueError("terminal_growth_rates must contain 1 to 25 values")
    growths = [_number(value, "terminal_growth_rates", []) for value in terminal_growth_rates]
    if any(value is None for value in growths):
        raise ValueError("terminal_growth_rates cannot contain missing values")
    calculate_dcf(payload)  # Reject invalid base inputs, rather than masking them as grid failures.
    cells = []
    for rate in rates:
        row = []
        for growth in growths:
            draft = deepcopy(dict(payload))
            draft.update(discount_rate=rate, terminal_growth_rate=growth)
            try:
                result = calculate_dcf(draft)
                row.append({key: result[key] for key in ("value_per_share", "status", "issues")})
            except ValueError as exc:
                row.append({"value_per_share": None, "status": "invalid", "issues": [str(exc)]})
        cells.append(row)
    return {
        "discount_rates": rates,
        "terminal_growth_rates": growths,
        "cells": cells,
        "currency": payload.get("currency"),
        "method_version": METHOD_VERSION,
    }
