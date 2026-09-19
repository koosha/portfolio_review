"""Account-feasible, explicitly conditional portfolio alternatives.

Ranks are used only to select a simple research candidate. Expected returns
must come from supplied joint forecasts. No function in this module places
orders, computes an invented tax bill, or transfers capital between accounts.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import ROUND_DOWN, Decimal
from typing import Any

import numpy as np
import pandas as pd
from scipy.optimize import Bounds, linprog, minimize


def _issue(code: str, message: str, severity: str = "error") -> dict:
    return {"severity": severity, "code": code, "message": message}


def _finite(value: Any) -> bool:
    try:
        return not isinstance(value, (bool, np.bool_)) and bool(np.isfinite(float(value)))
    except (TypeError, ValueError):
        return False


def _date(value: Any) -> pd.Timestamp | None:
    try:
        result = pd.Timestamp(value)
        if pd.isna(result):
            return None
        return result.tz_localize("UTC") if result.tzinfo is None else result.tz_convert("UTC")
    except (TypeError, ValueError):
        return None


def _frame(bundle: dict, key: str) -> pd.DataFrame:
    value = bundle.get(key)
    return value.copy() if isinstance(value, pd.DataFrame) else pd.DataFrame()


def _blocked(issues: list[dict], comparison: list | None = None) -> dict:
    return {
        "status": "blocked",
        "issues": issues,
        "proposals": [],
        "decisions": [
            {"action": "gather_evidence", "reason": issue["message"], "code": issue["code"]}
            for issue in issues
            if issue.get("severity") == "error"
        ],
        "comparison": comparison or [],
        "solver": {"status": "not_run", "executable": False},
    }


def _forecast_panel(
    bundle: dict, config: dict, ids: list[str], *, require_probabilities=True
) -> tuple[dict | None, list[dict]]:
    """Validate one dated joint set; optional probabilities never become invented means."""
    from .metrics import _cutoff, _observed

    forecasts = _frame(bundle, "forecasts")
    issues = []
    allocation = config.get("allocation", {})
    horizon = int(allocation.get("horizon_months", 12))
    needed = {"security_id", "scenario", "horizon_months", "return_value", "basis", "forecast_date"}
    if forecasts.empty or not needed.issubset(forecasts.columns):
        return None, [
            _issue(
                "missing_forecasts", "Dated joint scenarios are required; ranks are not returns."
            )
        ]
    forecasts = forecasts[
        (pd.to_numeric(forecasts.horizon_months, errors="coerce") == horizon)
        & forecasts.security_id.isin(ids)
    ].copy()
    forecasts["_parsed_date"] = pd.to_datetime(
        forecasts.forecast_date, utc=True, errors="coerce", format="mixed"
    )
    if forecasts._parsed_date.isna().any():
        return None, [_issue("invalid_forecast_date", "Every scenario row needs a valid date.")]
    forecasts = _observed(forecasts, bundle, "forecast_date", None, config)
    selected = [
        rows[rows._parsed_date == rows._parsed_date.max()]
        for _, rows in forecasts.groupby("security_id")
    ]
    forecasts = pd.concat(selected, ignore_index=True) if selected else forecasts
    labels = None
    cutoff = _cutoff(bundle)
    for sid in ids:
        rows = forecasts[forecasts.security_id == sid]
        if rows.empty:
            issues.append(
                _issue("missing_forecasts", f"No {horizon}-month joint scenarios for {sid}.")
            )
            continue
        if (
            rows.scenario.isna().any()
            or rows.scenario.astype(str).str.strip().eq("").any()
            or rows.scenario.duplicated().any()
        ):
            issues.append(
                _issue(
                    "duplicate_forecast", f"Scenario labels for {sid} must be nonempty and unique."
                )
            )
        names = set(rows.scenario)
        if labels is None:
            labels = names
        elif labels != names:
            issues.append(
                _issue(
                    "incomplete_joint_scenarios",
                    "Every required security must cover the same joint scenarios.",
                )
            )
        if (cutoff - rows._parsed_date.max()).days > int(
            config.get("data", {}).get("max_forecast_age_days", 45)
        ):
            issues.append(_issue("stale_forecast", f"Scenarios for {sid} are stale."))
        if not rows.basis.isin(["subjective", "calibrated"]).all():
            issues.append(_issue("forecast_basis", f"Unknown scenario basis for {sid}."))
    if forecasts._parsed_date.nunique() > 1:
        issues.append(
            _issue(
                "mixed_joint_vintage",
                "All assets and benchmark must share one joint scenario date.",
            )
        )
    if allocation.get("mode") == "calibrated":
        issues.append(
            _issue(
                "uncalibrated_forecast",
                "Calibrated allocation is unavailable: a free-text ID is not a validated calibration record with matured outcomes, compatible horizon and diagnostics.",
            )
        )
    elif forecasts.basis.eq("calibrated").any():
        issues.append(
            _issue(
                "calibration_not_validated",
                "Supplied calibrated labels lack supported validation evidence; results remain subjective scenarios.",
                "warning",
            )
        )
    if any(i["severity"] == "error" for i in issues):
        return None, issues
    names = sorted(labels or [])
    if len(names) < 2:
        return None, issues + [
            _issue("insufficient_scenarios", "Supply at least two distinct joint scenarios.")
        ]
    use_probabilities = allocation.get("use_probabilities", True)
    overrides = (allocation.get("probability_overrides", {}) or {}) if use_probabilities else {}
    if overrides and set(overrides) != set(names):
        return None, issues + [
            _issue(
                "probability_override", "Overrides must specify every joint scenario exactly once."
            )
        ]
    return_overrides = allocation.get("return_overrides", {}) or {}
    known_ids = set(_frame(bundle, "securities").get("security_id", []))
    for sid, changes in return_overrides.items():
        if sid not in known_ids or set(changes) - set(names):
            return None, issues + [
                _issue("invalid_return_overrides", "Unknown asset or scenario in return overrides.")
            ]
    returns = np.empty((len(names), len(ids)))
    probabilities = []
    missing_probability = False
    for k, name in enumerate(names):
        rows = forecasts[forecasts.scenario == name].set_index("security_id")
        raw = pd.to_numeric(
            rows.get("probability", pd.Series(index=rows.index, dtype=float)), errors="coerce"
        )
        if not use_probabilities:
            probability = None
        elif overrides:
            probability = overrides[name]
        elif raw.isna().all():
            probability = None
        elif raw.isna().any() or not np.isfinite(raw).all() or raw.max() - raw.min() > 1e-10:
            return None, issues + [
                _issue(
                    "inconsistent_joint_probability",
                    f"{name} probabilities must agree across all assets, or all remain blank.",
                )
            ]
        else:
            probability = float(raw.iloc[0])
        if probability is None:
            missing_probability = True
        elif not _finite(probability) or not 0 <= float(probability) <= 1:
            return None, issues + [
                _issue("invalid_probability", f"Invalid probability for {name}.")
            ]
        probabilities.append(float(probability) if probability is not None else None)
        for j, sid in enumerate(ids):
            value = return_overrides.get(sid, {}).get(name, rows.loc[sid, "return_value"])
            if not _finite(value) or float(value) < -1:
                return None, issues + [
                    _issue("invalid_forecast_return", f"Invalid total return for {sid}/{name}.")
                ]
            returns[k, j] = float(value)
    if missing_probability:
        if any(value is not None for value in probabilities):
            return None, issues + [
                _issue(
                    "partial_probabilities",
                    "Enter a complete common distribution or leave every probability blank.",
                )
            ]
        if require_probabilities:
            return None, issues + [
                _issue(
                    "missing_probabilities",
                    "A consistent probability distribution is required for weighted means and optimization.",
                )
            ]
        probability_array = None
        means = np.zeros(
            len(ids)
        )  # Internal fixed-target feasibility coefficients only; never published as returns.
        issues.append(
            _issue(
                "unweighted_scenarios",
                "Scenario outcomes are available; probability-weighted means are unavailable.",
                "info",
            )
        )
    else:
        probability_array = np.asarray(probabilities, dtype=float)
        if abs(probability_array.sum() - 1) > 1e-8:
            return None, issues + [
                _issue(
                    "probability_sum",
                    "Joint probabilities must sum to one; they are never normalized.",
                )
            ]
        means = probability_array @ returns
    objective_means = means.copy()
    shrink = float(allocation.get("forecast_shrinkage", 0))
    priors = allocation.get("prior_returns", {}) or {}
    if shrink and probability_array is not None:
        if any(not _finite(priors.get(sid)) or float(priors[sid]) < -1 for sid in ids):
            return None, issues + [
                _issue(
                    "missing_risk_aware_prior",
                    "Shrinkage needs an explicit same-horizon prior for every required security.",
                )
            ]
        objective_means = (1 - shrink) * means + shrink * np.array([priors[sid] for sid in ids])
    return {
        "ids": ids,
        "names": names,
        "returns": returns,
        "probabilities": probability_array,
        "means": means,
        "objective_means": objective_means,
        "weighted_available": probability_array is not None,
        "forecast_date": str(forecasts._parsed_date.iloc[0]),
        "basis": "subjective",
    }, issues


def _exposure_coefficients(
    bundle: dict, config: dict, ids: list[str]
) -> tuple[dict, dict, list[dict]]:
    from .metrics import _cutoff, _observed

    securities = _frame(bundle, "securities").set_index("security_id")
    funds = _frame(bundle, "fund_holdings")
    cutoff = _cutoff(bundle)
    limit = int(config.get("data", {}).get("max_fund_holdings_age_days", 120))
    issuer_sector: dict[str, str] = {}
    for row in securities.to_dict("records"):
        issuer, sector = row.get("issuer_id"), row.get("sector")
        if isinstance(issuer, str) and isinstance(sector, str) and sector.strip():
            if issuer in issuer_sector and issuer_sector[issuer] != sector:
                return (
                    {},
                    {},
                    [
                        _issue(
                            "conflicting_issuer_sector", f"Conflicting sectors for issuer {issuer}."
                        )
                    ],
                )
            issuer_sector[issuer] = sector
    issuer_coeffs: dict[str, dict[str, float]] = {}
    sector_coeffs: dict[str, dict[str, float]] = {}
    issues: list[dict] = []
    for sid in ids:
        row = securities.loc[sid]
        if row.instrument_type == "equity":
            issuer = row.get("issuer_id")
            sector = row.get("sector")
            if not isinstance(issuer, str) or not issuer.strip():
                issues.append(_issue("unresolved_issuer", f"Missing issuer for {sid}."))
                continue
            issuer_coeffs.setdefault(issuer, {})[sid] = 1.0
            if not isinstance(sector, str) or not sector.strip() or sector.lower() == "unknown":
                issues.append(_issue("unresolved_sector", f"Missing sector for {sid}."))
            else:
                sector_coeffs.setdefault(sector, {})[sid] = 1.0
        elif row.instrument_type in {"etf", "plan_fund"}:
            if funds.empty or not {
                "fund_id",
                "issuer_id",
                "weight",
                "holdings_date",
                "available_at",
            }.issubset(funds.columns):
                issues.append(
                    _issue(
                        "missing_fund_lookthrough", f"Complete dated holdings required for {sid}."
                    )
                )
                continue
            holdings = funds[funds.fund_id == sid].copy()
            holdings = _observed(holdings, bundle, "holdings_date", "available_at", config)
            if holdings.empty:
                issues.append(_issue("missing_fund_lookthrough", f"No holdings for {sid}."))
                continue
            dates = holdings.holdings_date.map(_date)
            available = holdings.available_at.map(_date)
            if dates.isna().any() or available.isna().any() or any(v > cutoff for v in available):
                issues.append(
                    _issue("invalid_fund_date", f"Missing or future holdings disclosure for {sid}.")
                )
                continue
            # A bundle must select one known holdings vintage, not stack filings.
            if len(set(dates)) != 1 or any(
                (cutoff.normalize() - v.normalize()).days > limit or v > cutoff for v in dates
            ):
                issues.append(
                    _issue(
                        "stale_fund_lookthrough",
                        f"Mixed, future, or stale holdings vintage for {sid}.",
                    )
                )
                continue
            weights = pd.to_numeric(holdings.weight, errors="coerce")
            if (
                not np.isfinite(weights).all()
                or (weights < 0).any()
                or not np.isclose(weights.sum(), 1, atol=1e-5)
            ):
                issues.append(
                    _issue(
                        "partial_fund_lookthrough",
                        f"{sid} holdings do not account for 100% of fund assets.",
                    )
                )
                continue
            for holding in holdings.to_dict("records"):
                issuer = str(holding["issuer_id"])
                weight = float(holding["weight"])
                if issuer == "CASH":
                    continue
                if issuer not in issuer_sector:
                    issues.append(
                        _issue(
                            "unresolved_fund_issuer",
                            f"Missing sector mapping for {sid} underlying issuer {issuer}.",
                        )
                    )
                    continue
                issuer_coeffs.setdefault(issuer, {})[sid] = (
                    issuer_coeffs.get(issuer, {}).get(sid, 0) + weight
                )
                sector = issuer_sector[issuer]
                sector_coeffs.setdefault(sector, {})[sid] = (
                    sector_coeffs.get(sector, {}).get(sid, 0) + weight
                )
        else:
            issues.append(
                _issue("unsupported_instrument", f"Unsupported instrument type for {sid}.")
            )
    return issuer_coeffs, sector_coeffs, issues


@dataclass
class _Problem:
    accounts: list[str]
    ids: list[str]
    pairs: list[tuple[str, str]]
    navs: np.ndarray
    current: np.ndarray
    nav_for_pair: np.ndarray
    fee: float
    bounds: list[tuple[float, float]]
    a_eq: np.ndarray
    b_eq: np.ndarray
    a_ub: np.ndarray
    b_ub: np.ndarray
    objective: np.ndarray
    household_map: np.ndarray
    covariance: np.ndarray
    max_volatility: float
    tax_coefficients: np.ndarray
    tax_specs: list[dict]

    @property
    def n(self) -> int:
        return len(self.pairs)

    def canonicalize_auxiliaries(self, values: np.ndarray) -> np.ndarray:
        """Remove economically meaningless turnover/lot-variable degeneracy.

        This preserves security targets and enforces actual absolute trades,
        minimum-tax lot usage and the resulting account cash. In particular,
        zero commission and zero-gain lots cannot create fictitious trades.
        """
        x = values.copy()
        x[self.n : 2 * self.n] = np.abs(x[: self.n] - self.current)
        start = 2 * self.n + len(self.accounts)
        x[start:] = 0
        for pair_index in {spec["pair_index"] for spec in self.tax_specs}:
            remaining = max(0.0, self.current[pair_index] - x[pair_index])
            ordered = sorted(
                [
                    (k, spec)
                    for k, spec in enumerate(self.tax_specs)
                    if spec["pair_index"] == pair_index
                ],
                key=lambda v: (v[1]["tax_per_weight"], v[0]),
            )
            for k, spec in ordered:
                amount = min(remaining, spec["max_weight"])
                x[start + k] = amount
                remaining -= amount
        for k, aid in enumerate(self.accounts):
            indices = [j for j, (account, _) in enumerate(self.pairs) if account == aid]
            tax = sum(
                x[start + m] * spec["tax_per_weight"]
                for m, spec in enumerate(self.tax_specs)
                if spec["account_id"] == aid
            )
            x[2 * self.n + k] = (
                1
                - sum(x[j] for j in indices)
                - self.fee * sum(x[self.n + j] for j in indices)
                - tax
            )
        return x

    def evaluate_constraints(self, x: np.ndarray) -> tuple[bool, dict]:
        eq = float(np.max(np.abs(self.a_eq @ x - self.b_eq)))
        linear = float(np.max(self.a_ub @ x - self.b_ub)) if len(self.a_ub) else 0.0
        low = max([lo - x[j] for j, (lo, _) in enumerate(self.bounds)] + [0.0])
        high = max([x[j] - hi for j, (_, hi) in enumerate(self.bounds)] + [0.0])
        weights = self.household_map @ x[: self.n]
        volatility = float(np.sqrt(max(0, weights @ self.covariance @ weights)))
        true_turnover = np.abs(x[: self.n] - self.current)
        aux_gap = float(np.max(np.abs(x[self.n : 2 * self.n] - true_turnover))) if self.n else 0.0
        tax_gap = 0.0
        start = 2 * self.n + len(self.accounts)
        for pair_index in {spec["pair_index"] for spec in self.tax_specs}:
            sold = sum(
                float(x[start + k])
                for k, spec in enumerate(self.tax_specs)
                if spec["pair_index"] == pair_index
            )
            tax_gap = max(tax_gap, abs(sold - max(0.0, self.current[pair_index] - x[pair_index])))
        details = {
            "account_budget_residual": eq,
            "max_linear_violation": max(0.0, linear),
            "max_bound_violation": max(low, high),
            "annualized_volatility": volatility,
            "absolute_trade_auxiliary_gap": aux_gap,
            "tax_sale_auxiliary_gap": tax_gap,
        }
        ok = (
            eq <= 1e-6
            and linear <= 1e-6
            and max(low, high) <= 1e-6
            and volatility <= self.max_volatility + 1e-6
            and aux_gap <= 1e-5
            and tax_gap <= 1e-5
        )
        return ok, details


def _solve(
    problem: _Problem, fixed_weights: np.ndarray | None = None
) -> tuple[np.ndarray | None, dict]:
    bounds = list(problem.bounds)
    if fixed_weights is not None:
        for j, value in enumerate(fixed_weights):
            lo, hi = bounds[j]
            if value < lo - 1e-8 or value > hi + 1e-8:
                return None, {
                    "status": "infeasible",
                    "message": "Simple candidate violates a locked holding or eligibility bound.",
                }
            bounds[j] = (float(value), float(value))
    linear = linprog(
        problem.objective,
        A_ub=problem.a_ub,
        b_ub=problem.b_ub,
        A_eq=problem.a_eq,
        b_eq=problem.b_eq,
        bounds=bounds,
        method="highs",
    )
    if not linear.success:
        return None, {"status": "infeasible", "message": str(linear.message)}
    x = problem.canonicalize_auxiliaries(linear.x)
    # Linear optimum is globally optimal if it also satisfies the quadratic cap.
    feasible, details = problem.evaluate_constraints(x)
    if feasible:
        return x, {"status": "optimal", "method": "HiGHS linear programming", "validation": details}
    if fixed_weights is not None:
        return None, {
            "status": "infeasible",
            "message": "Simple candidate violates risk or true-cost feasibility.",
            "validation": details,
        }

    def variance(z: np.ndarray) -> float:
        w = problem.household_map @ z[: problem.n]
        return float(w @ problem.covariance @ w)

    def variance_jac(z: np.ndarray) -> np.ndarray:
        g = np.zeros_like(z)
        w = problem.household_map @ z[: problem.n]
        g[: problem.n] = -2 * problem.household_map.T @ problem.covariance @ w
        return g

    constraints = [
        {
            "type": "eq",
            "fun": lambda z: problem.a_eq @ z - problem.b_eq,
            "jac": lambda z: problem.a_eq,
        },
        {
            "type": "ineq",
            "fun": lambda z: problem.b_ub - problem.a_ub @ z,
            "jac": lambda z: -problem.a_ub,
        },
        {
            "type": "ineq",
            "fun": lambda z: problem.max_volatility**2 - variance(z),
            "jac": variance_jac,
        },
    ]
    solution = minimize(
        lambda z: float(problem.objective @ z),
        x,
        jac=lambda z: problem.objective,
        method="SLSQP",
        bounds=Bounds(*zip(*bounds)),
        constraints=constraints,
        options={"ftol": 1e-11, "maxiter": 1500},
    )
    candidate = problem.canonicalize_auxiliaries(solution.x)
    feasible, details = problem.evaluate_constraints(candidate)
    if not solution.success or not feasible:
        return None, {
            "status": "infeasible_or_unresolved",
            "message": str(solution.message),
            "validation": details,
        }
    return candidate, {
        "status": "optimal",
        "method": "SLSQP convex variance constraint",
        "validation": details,
    }


def build_proposals(bundle: dict, config: dict, scores: pd.DataFrame) -> dict:
    """Build conditional alternatives, preserving all account funding boundaries.

    All output is research. Subjective forecasts are never marked executable;
    taxable alternatives require complete lots and conditional tax assumptions.
    """
    from .metrics import _cutoff, _observed, portfolio_covariance, stress_returns
    from .taxes import estimate_sale

    issues: list[dict] = []
    mandate, allocation, data = (config.get(k, {}) for k in ("mandate", "allocation", "data"))
    failed_stages = [
        issue for issue in bundle.get("issues", []) if issue.get("severity") == "error"
    ]
    if failed_stages:
        return _blocked(
            failed_stages
            + [
                _issue(
                    "upstream_analysis_failed",
                    "Source validation or an upstream analytical stage reported an error; no allocation basket can be certified.",
                )
            ]
        )
    if mandate.get("confirmed") is not True:
        return _blocked(
            [
                _issue(
                    "mandate_unconfirmed",
                    "Confirm the investment mandate and numerical limits before proposing allocations.",
                )
            ]
        )
    positions, accounts, securities = (
        _frame(bundle, k) for k in ("positions", "accounts", "securities")
    )
    required = {
        "positions": (
            positions,
            {"account_id", "security_id", "quantity", "market_value", "currency", "valuation_date"},
        ),
        "accounts": (
            accounts,
            {"account_id", "account_type", "currency", "total_value", "cash", "complete"},
        ),
        "securities": (
            securities,
            {"security_id", "issuer_id", "instrument_type", "currency", "eligible", "sector"},
        ),
    }
    for name, (frame, columns) in required.items():
        if (frame.empty and name != "positions") or not columns.issubset(frame.columns):
            issues.append(
                _issue(
                    "missing_portfolio_fields",
                    f"Missing {name} data/fields: {sorted(columns - set(frame.columns))}.",
                )
            )
    if issues:
        return _blocked(issues)
    if (
        accounts.account_id.duplicated().any()
        or securities.security_id.duplicated().any()
        or positions.duplicated(["account_id", "security_id"]).any()
    ):
        return _blocked(
            [
                _issue(
                    "duplicate_identity",
                    "Account IDs, security IDs, and account/security position pairs must be unique.",
                )
            ]
        )
    if _date(bundle.get("as_of")) is None:
        return _blocked([_issue("missing_as_of", "A valid analysis date is required.")])
    cutoff = _cutoff(bundle)
    base = mandate.get("base_currency")
    if not isinstance(base, str) or not base:
        issues.append(_issue("missing_base_currency", "An explicit base currency is required."))
    permissions = mandate.get("account_permissions", {}) or {}
    locked_ids = set(mandate.get("locked_security_ids", []))
    account_ids = sorted(accounts.account_id.tolist())
    accounts = accounts.set_index("account_id")
    security_map = securities.set_index("security_id")
    navs: list[float] = []
    taxable_locked: set[str] = set()
    taxable_unverified: set[str] = set()
    taxable_enabled: set[str] = set()
    for aid in account_ids:
        row = accounts.loc[aid]
        if aid not in permissions or not isinstance(permissions[aid], list):
            issues.append(
                _issue(
                    "missing_account_permissions",
                    f"Explicit permitted securities required for {aid}.",
                )
            )
        if row.get("complete") not in (True, np.bool_(True)):
            issues.append(
                _issue("incomplete_account", f"Account {aid} has incomplete holdings coverage.")
            )
        nav, cash = row.total_value, row.cash
        if not _finite(nav) or float(nav) <= 0 or not _finite(cash) or float(cash) < 0:
            issues.append(
                _issue(
                    "unknown_account_cash",
                    f"Positive NAV and known nonnegative cash required for {aid}.",
                )
            )
            navs.append(0)
            continue
        navs.append(float(nav))
        owned = positions[positions.account_id == aid]
        values = pd.to_numeric(owned.market_value, errors="coerce")
        if not np.isfinite(values).all() or (values < 0).any():
            issues.append(
                _issue("invalid_position_value", f"Invalid or short position values in {aid}.")
            )
        elif abs(values.sum() + float(cash) - float(nav)) > max(1.0, float(nav) * 1e-6):
            issues.append(
                _issue(
                    "account_not_reconciled",
                    f"Positions plus known cash do not reconcile to NAV in {aid}; no residual is assumed cash.",
                )
            )
        if row.currency != base or not (owned.currency == base).all():
            issues.append(
                _issue(
                    "currency_conversion_required",
                    f"{aid} requires explicit compatible base-currency values; FX conversion is not assumed.",
                )
            )
        if row.account_type not in {"taxable", "retirement"}:
            issues.append(_issue("unknown_account_type", f"Account type required for {aid}."))
        if row.account_type == "taxable":
            if not mandate.get("allow_taxable_proposals", False):
                taxable_locked.add(aid)
                issues.append(
                    _issue(
                        "taxable_account_locked",
                        f"{aid} is fixed at current holdings because taxable proposals are disabled.",
                        "warning",
                    )
                )
            else:
                taxable_unverified.add(aid)
    if not set(positions.account_id).issubset(account_ids):
        issues.append(_issue("unresolved_account", "A position refers to an unknown account."))
    for row in positions.to_dict("records"):
        dt = _date(row.get("valuation_date"))
        if (
            dt is None
            or dt.normalize() > cutoff.normalize()
            or (cutoff.normalize() - dt.normalize()).days
            > int(data.get("max_holdings_age_days", data.get("max_price_age_days", 5)))
        ):
            issues.append(
                _issue(
                    "stale_position_valuation",
                    f"Missing, future, or stale holdings valuation for {row['security_id']}.",
                )
            )
        if not _finite(row.get("quantity")) or float(row["quantity"]) <= 0:
            issues.append(
                _issue(
                    "missing_quantity",
                    f"Positive position quantity required for {row['security_id']}; dollars cannot become order quantities.",
                )
            )
    pair_ids: list[tuple[str, str]] = []
    for aid in account_ids:
        owned_ids = set(positions.loc[positions.account_id == aid, "security_id"])
        permitted = set(permissions.get(aid, []))
        for sid in sorted(owned_ids | permitted):
            if sid not in security_map.index:
                issues.append(
                    _issue("unresolved_security", f"Unknown security ID {sid} in account {aid}.")
                )
                continue
            security = security_map.loc[sid]
            if security.currency != base:
                issues.append(
                    _issue(
                        "security_currency",
                        f"Security {sid} does not have confirmed {base} valuation units.",
                    )
                )
            if sid in owned_ids or (
                security.get("eligible") in (True, np.bool_(True)) and aid not in taxable_locked
            ):
                pair_ids.append((aid, sid))
    benchmark = mandate.get("benchmark_id")
    if not isinstance(benchmark, str) or benchmark not in security_map.index:
        issues.append(
            _issue("missing_benchmark", "The benchmark must have a resolved security ID.")
        )
    if any(i["severity"] == "error" for i in issues):
        return _blocked(issues)
    cash_return = allocation.get("cash_return")
    if allocation.get("optimize", False) and not _finite(cash_return):
        return _blocked(
            issues
            + [
                _issue(
                    "missing_cash_return",
                    "An explicit horizon cash-return assumption is required for optimization.",
                )
            ]
        )
    cash_rate = (
        float(cash_return) if _finite(cash_return) else 0.0
    )  # Internal feasibility only; unknown results stay null.
    ids = sorted({sid for _, sid in pair_ids} | {benchmark})
    # Benchmark is a comparison instrument even when an account cannot buy it.
    panel, forecast_issues = _forecast_panel(
        bundle, config, ids, require_probabilities=bool(allocation.get("optimize", False))
    )
    issues.extend(forecast_issues)
    issuer_coeffs, sector_coeffs, exposure_issues = _exposure_coefficients(bundle, config, ids)
    issues.extend(exposure_issues)
    prices = _frame(bundle, "prices")
    last_prices: dict[str, float] = {}
    if prices.empty or not {"security_id", "date", "close", "available_at"}.issubset(
        prices.columns
    ):
        issues.append(
            _issue(
                "missing_trade_prices",
                "Dated raw tradable prices and publication timestamps are required for all candidate securities.",
            )
        )
    else:
        for sid in ids:
            rows = prices[prices.security_id == sid].copy()
            rows = _observed(rows, bundle, "date", "available_at", config)
            rows["_date"] = rows.date.map(_date)
            rows = rows[rows._date.notna() & (rows._date <= cutoff)]
            if rows.empty:
                issues.append(_issue("missing_trade_prices", f"No known raw price for {sid}."))
                continue
            latest = rows.sort_values("_date").iloc[-1]
            if (
                (cutoff.normalize() - latest._date.normalize()).days
                > int(data.get("max_price_age_days", 5))
                or not _finite(latest.close)
                or float(latest.close) <= 0
            ):
                issues.append(_issue("stale_trade_price", f"Invalid or stale raw price for {sid}."))
            else:
                last_prices[sid] = float(latest.close)
    for name in (
        "issuer_cap",
        "sector_cap",
        "min_cash_weight",
        "max_turnover",
        "max_volatility",
        "max_stress_loss",
    ):
        value = mandate.get(name)
        upper = 2 if name == "max_turnover" else 1
        if not _finite(value) or not 0 <= float(value) <= upper:
            issues.append(
                _issue("missing_risk_limit", f"Explicit valid mandate.{name} is required.")
            )
    if any(i["severity"] == "error" for i in issues):
        return _blocked(issues)
    # Taxable accounts stay fixed unless full holdings are covered by valid lots
    # and explicit user-supplied tax rates. A modeled reserve is not a tax bill.
    tax_lots = _frame(bundle, "tax_lots")
    full_sale_estimates: dict[tuple[str, str], dict] = {}
    for aid in sorted(taxable_unverified):
        usable = bool(config.get("tax", {}).get("enabled", False))
        for position in positions[positions.account_id == aid].to_dict("records"):
            sid, quantity = position["security_id"], float(position["quantity"])
            if tax_lots.empty or not {"account_id", "security_id", "quantity"}.issubset(
                tax_lots.columns
            ):
                usable = False
                continue
            matching = tax_lots[(tax_lots.account_id == aid) & (tax_lots.security_id == sid)]
            lot_quantity = pd.to_numeric(matching.quantity, errors="coerce").sum()
            if not np.isclose(lot_quantity, quantity, atol=1e-7, rtol=1e-8):
                usable = False
                continue
            estimate = estimate_sale(
                tax_lots, aid, sid, quantity, last_prices[sid], bundle["as_of"], config
            )
            if estimate.get("status") != "estimated":
                usable = False
            else:
                full_sale_estimates[(aid, sid)] = estimate
        if usable:
            taxable_enabled.add(aid)
            issues.append(
                _issue(
                    "conditional_tax_reserve",
                    f"{aid}: costs include a conditional US lot-based gain-tax reserve at configured marginal rates; losses receive no credit. This is not a final tax liability.",
                    "warning",
                )
            )
        else:
            taxable_locked.add(aid)
            issues.append(
                _issue(
                    "tax_analysis_unavailable",
                    f"{aid} remains fixed: taxable proposals require enabled tax assumptions and complete, validated matching tax lots.",
                    "warning",
                )
            )
    # Every quantity calculation uses the same prices as the position ledger. A collected
    # snapshot values positions at its own receipt, so the accepted difference is explicit.
    valuation_tolerance = float(config["allocation"]["valuation_tolerance"])
    for position in positions.to_dict("records"):
        implied = float(position["quantity"]) * last_prices[position["security_id"]]
        value = float(position["market_value"])
        if abs(implied - value) > max(0.02, abs(value) * valuation_tolerance):
            issues.append(
                _issue(
                    "valuation_price_mismatch",
                    f"{position['security_id']} quantity × latest price differs from the position value; refresh and reconcile the account snapshot first.",
                )
            )
    if any(i["severity"] == "error" for i in issues):
        return _blocked(issues)
    covariance_result = portfolio_covariance(bundle, config, ids)
    covariance = covariance_result.get("matrix")
    if covariance is None or covariance_result.get("complete", True) is False:
        issues.extend(covariance_result.get("issues", []))
        issues.append(
            _issue(
                "incomplete_covariance",
                "A complete aligned weekly covariance panel is required for the volatility limit.",
            )
        )
        return _blocked(issues)
    cov_ids = covariance_result.get("ids", ids)
    reorder = [cov_ids.index(sid) for sid in ids]
    covariance = np.asarray(covariance, dtype=float)[np.ix_(reorder, reorder)]
    if covariance.shape != (len(ids), len(ids)) or not np.isfinite(covariance).all():
        return _blocked(
            issues + [_issue("invalid_covariance", "Invalid covariance dimensions or values.")]
        )
    stress = stress_returns(bundle, config, ids)
    for name, values in stress.items():
        if any(sid not in values or not _finite(values[sid]) for sid in ids):
            issues.append(
                _issue(
                    "incomplete_stress_exposure",
                    f"Cannot compute {name} for all required securities.",
                )
            )
    if any(i["severity"] == "error" for i in issues):
        return _blocked(issues)
    n, a, total_nav = len(pair_ids), len(account_ids), float(sum(navs))
    nav_array = np.asarray(navs)
    account_index = {aid: j for j, aid in enumerate(account_ids)}
    id_index = {sid: j for j, sid in enumerate(ids)}
    current = np.array(
        [
            float(
                positions.loc[
                    (positions.account_id == aid) & (positions.security_id == sid), "market_value"
                ].sum()
            )
            / float(accounts.loc[aid, "total_value"])
            for aid, sid in pair_ids
        ]
    )
    nav_for_pair = np.array([float(accounts.loc[aid, "total_value"]) for aid, _ in pair_ids])
    household_map = np.zeros((len(ids), n))
    for j, (_, sid) in enumerate(pair_ids):
        household_map[id_index[sid], j] = nav_for_pair[j] / total_nav
    fee = float(allocation.get("transaction_cost_bps", 10)) / 10_000
    tax_specs: list[dict] = []
    for j, (aid, sid) in enumerate(pair_ids):
        if aid not in taxable_enabled or (aid, sid) not in full_sale_estimates:
            continue
        for lot in full_sale_estimates[(aid, sid)]["lot_plan"]:
            quantity = float(lot["quantity"])
            value = quantity * last_prices[sid]
            tax_specs.append(
                {
                    "pair_index": j,
                    "account_id": aid,
                    "security_id": sid,
                    "lot_id": lot["lot_id"],
                    "max_weight": value / nav_for_pair[j],
                    "tax_per_weight": float(lot["estimated_tax"]) / value if value > 0 else 0,
                }
            )
    tax_start = 2 * n + a
    size = tax_start + len(tax_specs)
    a_eq, b_eq, a_ub, b_ub = [], [], [], []
    bounds: list[tuple[float, float]] = []
    for j, (aid, sid) in enumerate(pair_ids):
        dealing = mandate.get("dealing_rules", {}).get(aid, {}).get(sid, {})
        locked = (
            sid in locked_ids or aid in taxable_locked or dealing.get("dealing_allowed") is False
        )
        can_add = sid in permissions[aid] and security_map.loc[sid, "eligible"] in (
            True,
            np.bool_(True),
        )
        bounds.append((current[j], current[j]) if locked else (0.0, 1.0 if can_add else current[j]))
    bounds += [(0.0, 2.0)] * n
    bounds += [(float(mandate["min_cash_weight"]), 1.0)] * a
    bounds += [(0.0, spec["max_weight"]) for spec in tax_specs]
    tax_coefficients = np.zeros(size)
    for k, spec in enumerate(tax_specs):
        tax_coefficients[tax_start + k] = (
            spec["tax_per_weight"] * nav_for_pair[spec["pair_index"]] / total_nav
        )
    for j in range(n):
        row = np.zeros(size)
        row[j] = 1
        row[n + j] = -1
        a_ub.append(row)
        b_ub.append(current[j])
        row = np.zeros(size)
        row[j] = -1
        row[n + j] = -1
        a_ub.append(row)
        b_ub.append(-current[j])
    for k, aid in enumerate(account_ids):
        indices = [j for j, (account, _) in enumerate(pair_ids) if account == aid]
        row = np.zeros(size)
        row[indices] = 1
        row[np.array(indices, dtype=int) + n] = fee
        row[2 * n + k] = 1
        for m, spec in enumerate(tax_specs):
            if spec["account_id"] == aid:
                row[tax_start + m] = spec["tax_per_weight"]
        a_eq.append(row)
        b_eq.append(1.0)
        turnover = np.zeros(size)
        turnover[np.array(indices, dtype=int) + n] = 1
        a_ub.append(turnover)
        b_ub.append(float(mandate["max_turnover"]))
    for pair_index in {spec["pair_index"] for spec in tax_specs}:
        # With t=abs(target-current), (t-target+current)/2 is sale
        # notional. This equality prevents gratuitous sales of zero-tax lots.
        row = np.zeros(size)
        row[pair_index] = 0.5
        row[n + pair_index] = -0.5
        for k, spec in enumerate(tax_specs):
            if spec["pair_index"] == pair_index:
                row[tax_start + k] = 1
        a_eq.append(row)
        b_eq.append(0.5 * current[pair_index])
    for coeffs, cap in (
        (issuer_coeffs, mandate["issuer_cap"]),
        (sector_coeffs, mandate["sector_cap"]),
    ):
        for values in coeffs.values():
            row = np.zeros(size)
            for j, (_, sid) in enumerate(pair_ids):
                row[j] = values.get(sid, 0) * nav_for_pair[j] / total_nav
            a_ub.append(row)
            b_ub.append(float(cap))
    # Stress loss includes transaction costs once, consistent with terminal wealth.
    stress_arrays = {
        name: np.array([values[sid] for sid in ids]) for name, values in stress.items()
    }
    for k, name in enumerate(panel["names"]):
        stress_arrays[f"joint:{name}"] = panel["returns"][k]
    for stress_name, shocks in stress_arrays.items():
        row = np.zeros(size)
        if stress_name.startswith("joint:"):
            row[2 * n : 2 * n + a] = -cash_rate * nav_array / total_nav
        row[:n] = -(shocks @ household_map)
        row[n : 2 * n] = fee * nav_for_pair / total_nav
        row += tax_coefficients
        a_ub.append(row)
        b_ub.append(float(mandate["max_stress_loss"]))
    objective = np.zeros(size)
    objective[:n] = -((1 + panel["objective_means"]) @ household_map)
    objective[2 * n : 2 * n + a] = -(1 + cash_rate) * nav_array / total_nav
    objective[n : 2 * n] = 1e-9  # deterministic minimum-turnover tie break only
    problem = _Problem(
        account_ids,
        ids,
        pair_ids,
        nav_array,
        current,
        nav_for_pair,
        fee,
        bounds,
        np.array(a_eq),
        np.array(b_eq),
        np.array(a_ub),
        np.array(b_ub),
        objective,
        household_map,
        covariance,
        float(mandate["max_volatility"]),
        tax_coefficients,
        tax_specs,
    )
    current_vector = np.zeros(size)
    current_vector[:n] = current
    current_vector[2 * n : 2 * n + a] = (
        np.array([float(accounts.loc[aid, "cash"]) for aid in account_ids]) / nav_array
    )

    def comparison_row(name: str, x: np.ndarray) -> dict:
        weights = household_map @ x[:n]
        true_trades = np.abs(x[:n] - current)
        cost = float(np.sum(true_trades * nav_for_pair) * fee)
        tax_reserve = float(problem.tax_coefficients @ x * total_nav)
        cash_weight = float(x[2 * n : 2 * n + a] @ nav_array / total_nav)
        cash_contribution = cash_weight * cash_rate
        cash_complete = cash_weight <= 1e-12 or _finite(cash_return)
        scenarios = {
            s: float(
                weights @ panel["returns"][k] + cash_contribution - (cost + tax_reserve) / total_nav
            )
            if cash_complete
            else None
            for k, s in enumerate(panel["names"])
        }
        by_account = []
        for k, aid in enumerate(account_ids):
            indices = [j for j, (account, _) in enumerate(pair_ids) if account == aid]
            expected = (
                sum(x[j] * panel["means"][id_index[pair_ids[j][1]]] for j in indices)
                + float(x[2 * n + k]) * cash_rate
            )
            account_cost = float(sum(true_trades[j] for j in indices) * fee * nav_array[k])
            account_tax = float(
                sum(
                    x[tax_start + m] * spec["tax_per_weight"]
                    for m, spec in enumerate(tax_specs)
                    if spec["account_id"] == aid
                )
                * nav_array[k]
            )
            by_account.append(
                {
                    "account_id": aid,
                    "ending_cash": float(x[2 * n + k] * nav_array[k]),
                    "execution_cost": account_cost,
                    "gross_trade_value": float(sum(true_trades[j] for j in indices) * nav_array[k]),
                    "conditional_tax_reserve": account_tax,
                    "scenario_weighted_return_after_trading_cost_before_tax": float(
                        expected - account_cost / nav_array[k]
                    ),
                    "scenario_weighted_return_after_cost_and_tax_reserve": float(
                        expected - (account_cost + account_tax) / nav_array[k]
                    ),
                    "budget_total": float(
                        sum(x[j] for j in indices) * nav_array[k]
                        + x[2 * n + k] * nav_array[k]
                        + account_cost
                        + account_tax
                    ),
                }
            )
        feasible, constraints = problem.evaluate_constraints(x)
        return {
            "candidate": name,
            "basis": panel["basis"],
            "feasible": feasible,
            "scenario_weighted_return_after_trading_cost_before_tax": float(
                weights @ panel["means"] + cash_contribution - cost / total_nav
            ),
            "objective_expected_return_after_trading_cost_before_tax": float(
                weights @ panel["objective_means"] + cash_contribution - cost / total_nav
            ),
            "estimated_terminal_wealth_before_tax": float(
                total_nav * (1 + weights @ panel["means"] + cash_contribution) - cost
            ),
            "scenario_weighted_return_after_cost_and_tax_reserve": float(
                weights @ panel["means"] + cash_contribution - (cost + tax_reserve) / total_nav
            ),
            "objective_expected_return_after_cost_and_tax_reserve": float(
                weights @ panel["objective_means"]
                + cash_contribution
                - (cost + tax_reserve) / total_nav
            ),
            "estimated_terminal_wealth_after_cost_and_tax_reserve": float(
                total_nav * (1 + weights @ panel["means"] + cash_contribution) - cost - tax_reserve
            ),
            "conditional_tax_reserve": tax_reserve,
            "execution_cost": cost,
            "cash_return_assumption": cash_return,
            "cash_return_complete": cash_complete,
            "scenarios": scenarios,
            "annualized_volatility": constraints["annualized_volatility"],
            "max_modeled_stress_loss": max(
                0.0,
                max(
                    float(
                        -shocks @ weights
                        - (cash_contribution if stress_name.startswith("joint:") else 0.0)
                        + (cost + tax_reserve) / total_nav
                    )
                    for stress_name, shocks in stress_arrays.items()
                ),
            ),
            "accounts": by_account,
            "after_tax_return": None,
            "executable": False,
        }

    def finalize(name, vector, *, reason, membership=None, experimental=False):
        """Validate the complete rounded/banded basket; never discard a leg in isolation."""
        candidate_issues = []
        raw = vector.copy()
        rounded = raw[:n].copy()
        rules = mandate.get("dealing_rules", {})
        missing_rules = []
        rounding_applied = False
        for j, (aid, sid) in enumerate(pair_ids):
            value = (rounded[j] - current[j]) * nav_for_pair[j]
            if abs(value) < 0.005:
                continue
            rule = rules.get(aid, {}).get(sid)
            if not rule:
                missing_rules.append(f"{aid}/{sid}")
                continue
            if not rule["dealing_allowed"]:
                rounded[j] = current[j]
                continue
            quantity = Decimal(str(value)) / Decimal(str(last_prices[sid]))
            increment = Decimal(str(rule["quantity_increment"]))
            units = quantity / increment
            nearest = units.to_integral_value()
            # Snap only numerical solver noise at an exact dealing increment.
            units = (
                nearest
                if abs(units - nearest) < Decimal("0.0000001")
                else units.to_integral_value(rounding=ROUND_DOWN)
            )
            quantity = units * increment
            target = current[j] + float(quantity) * last_prices[sid] / nav_for_pair[j]
            rounding_applied |= abs(target - rounded[j]) > 1e-12
            rounded[j] = max(0.0, target)
        rounded_vector = raw.copy()
        rounded_vector[:n] = rounded
        rounded_vector = problem.canonicalize_auxiliaries(rounded_vector)
        banded = rounded_vector.copy()
        band_indices = []
        for j in range(n):
            delta = abs(rounded[j] - current[j])
            minimum = max(
                float(allocation.get("min_trade_value", 500)),
                float(allocation.get("min_trade_weight", 0.0025)) * nav_for_pair[j],
            )
            gap = max(
                float(allocation.get("incumbent_gap_weight", 0.005)),
                float(allocation.get("incumbent_gap_fraction", 0.25)) * rounded[j],
            )
            if delta > 1e-12 and (
                delta * nav_for_pair[j] < minimum or (current[j] > 0 and delta < gap)
            ):
                banded[j] = current[j]
                band_indices.append(j)
        banded = problem.canonicalize_auxiliaries(banded)
        feasible, validation = problem.evaluate_constraints(banded)
        hard_override = False
        if not feasible and band_indices:
            # Preserve all necessary legs together when convenience bands would break a hard limit.
            banded = rounded_vector
            feasible, validation = problem.evaluate_constraints(banded)
            hard_override = feasible
        candidate = comparison_row(name, banded)
        candidate.update(
            status="review_ready" if feasible else "infeasible",
            reason=reason,
            validation=validation,
            proposals=[],
            decisions=[],
            issues=candidate_issues,
            experimental=experimental,
            rounding_applied=rounding_applied,
            band_policy="Whole-basket bands with hard-constraint override; complete targets revalidated",
            missing_dealing_rules=missing_rules,
            executable=False,
        )
        if not candidate["cash_return_complete"] and feasible:
            candidate["status"] = "draft"
            candidate["validation"]["joint_scenario_cash_assumption_complete"] = False
            candidate["max_modeled_stress_loss"] = None
            candidate_issues.append(
                _issue(
                    "missing_cash_return",
                    "Portfolio scenario outcomes require an explicit horizon return on retained cash.",
                    "warning",
                )
            )
        if missing_rules and feasible:
            candidate["status"] = "draft"
            candidate_issues.append(
                _issue(
                    "dealing_rules_unconfirmed",
                    "Confirm dealing/fractional permissions and quantity increments for: "
                    + ", ".join(missing_rules),
                    "warning",
                )
            )
        if not feasible:
            candidate_issues.append(
                _issue(
                    "post_rounding_infeasible",
                    "Candidate fails full funding/lock/turnover/exposure/risk/stress validation after dealing increments and bands; no constraint was relaxed.",
                )
            )
        baseline = comparison_row("no_change", current_vector)
        improvement = (
            candidate["objective_expected_return_after_cost_and_tax_reserve"]
            - baseline["objective_expected_return_after_cost_and_tax_reserve"]
        ) * total_nav
        purchases = float(np.maximum(banded[:n] - current, 0) @ nav_for_pair)
        sales = float(np.maximum(current - banded[:n], 0) @ nav_for_pair)
        principal = max(purchases, sales)
        weighted_complete = (
            panel["weighted_available"]
            and candidate["cash_return_complete"]
            and baseline["cash_return_complete"]
        )
        per_dollar = improvement / principal if principal > 0.005 and weighted_complete else None
        hurdle = float(allocation.get("return_hurdle", 0.02))
        horizon_matches = int(allocation.get("horizon_months", 12)) == 12
        candidate.update(
            redeployed_principal=principal,
            improvement_per_redeployed_dollar=per_dollar,
            expected_improvement_dollars_after_cost_and_tax_reserve=improvement
            if weighted_complete
            else None,
            return_hurdle_met=(per_dollar >= hurdle)
            if per_dollar is not None and horizon_matches
            else None,
            hurdle_basis="Subjective 12-month improvement per dollar redeployed after friction; other horizons remain unavailable",
        )
        for j, (aid, sid) in enumerate(pair_ids):
            value = float((banded[j] - current[j]) * nav_for_pair[j])
            if abs(value) < 0.005:
                candidate["decisions"].append(
                    {
                        "account_id": aid,
                        "security_id": sid,
                        "action": "hold",
                        "reason": "Inside whole-basket no-trade bands"
                        if j in band_indices
                        else reason,
                    }
                )
                continue
            flags = []
            if hard_override and j in band_indices:
                flags.append("hard_constraint_overrides_trade_band")
            if candidate["return_hurdle_met"] is False and baseline["feasible"]:
                flags.append("below_return_hurdle")
            if not horizon_matches:
                flags.append("12_month_hurdle_not_applicable")
            if f"{aid}/{sid}" in missing_rules:
                flags.append("dealing_rules_unconfirmed")
            estimate = {"estimated_tax": 0.0, "lot_plan": []}
            if aid in taxable_enabled and value < 0:
                estimate = estimate_sale(
                    tax_lots,
                    aid,
                    sid,
                    -value / last_prices[sid],
                    last_prices[sid],
                    bundle["as_of"],
                    config,
                )
                reserve = sum(
                    float(banded[tax_start + k]) * spec["tax_per_weight"] * nav_for_pair[j]
                    for k, spec in enumerate(tax_specs)
                    if spec["pair_index"] == j
                )
                if estimate.get("status") != "estimated" or abs(
                    float(estimate.get("estimated_tax") or 0) - reserve
                ) > max(0.02, nav_for_pair[j] * 1e-6):
                    candidate["status"] = "blocked"
                    candidate["feasible"] = False
                    candidate_issues.append(
                        _issue(
                            "tax_reserve_mismatch",
                            f"Final lot reserve failed verification for {aid}/{sid}.",
                        )
                    )
                flags.append("conditional_tax_estimate_review")
            action = (
                "buy"
                if value > 0 and current[j] == 0
                else "add"
                if value > 0
                else "exit"
                if banded[j] < 1e-8
                else "trim"
            )
            proposal = {
                "candidate": name,
                "account_id": aid,
                "security_id": sid,
                "action": action,
                "current_weight": float(current[j]),
                "target_weight": float(banded[j]),
                "trade_value": value,
                "estimated_cost": abs(value) * fee,
                "estimated_tax": estimate["estimated_tax"],
                "lot_plan": estimate["lot_plan"],
                "fractional_quantity_change": value / last_prices[sid],
                "price_used": last_prices[sid],
                "currency": base,
                "basis": "subjective",
                "executable": False,
                "reason": reason,
                "review_flags": flags,
                "decimal_amounts": {
                    "trade_value": str(Decimal(str(value)).quantize(Decimal(".01"))),
                    "quantity_change": str(
                        Decimal(str(value / last_prices[sid])).quantize(Decimal(".00000001"))
                    ),
                },
            }
            candidate["proposals"].append(proposal)
            candidate["decisions"].append(
                {
                    "account_id": aid,
                    "security_id": sid,
                    "action": "review",
                    "reason": reason,
                    "review_flags": flags,
                }
            )
        for row in candidate["accounts"]:
            aid = row["account_id"]
            legs = [p for p in candidate["proposals"] if p["account_id"] == aid]
            row.update(
                starting_cash=float(accounts.loc[aid, "cash"]),
                sales=sum(-p["trade_value"] for p in legs if p["trade_value"] < 0),
                purchases=sum(p["trade_value"] for p in legs if p["trade_value"] > 0),
                external_flow_added=0.0,
                flow_convention="New-flow allocations are already included in snapshot NAV and starting cash",
            )
            row["cash_identity_residual"] = (
                row["starting_cash"]
                + row["sales"]
                - row["purchases"]
                - row["execution_cost"]
                - row["conditional_tax_reserve"]
                - row["ending_cash"]
            )
        candidate["cost_sensitivities"] = []
        gross = purchases + sales
        for bps in [5, 10, 25]:
            new_fee = bps / 10000
            peq = problem.a_eq.copy()
            pub = problem.a_ub.copy()
            peq[:a, n : 2 * n] *= new_fee / fee if fee else 0
            if not fee:
                for k, aid in enumerate(account_ids):
                    for j, (account, _) in enumerate(pair_ids):
                        if account == aid:
                            peq[k, n + j] = new_fee
            stress_count = len(stress_arrays)
            if stress_count:
                pub[-stress_count:, n : 2 * n] = new_fee * nav_for_pair / total_nav
            sensitivity_problem = replace(problem, fee=new_fee, a_eq=peq, a_ub=pub)
            sensitivity_vector = sensitivity_problem.canonicalize_auxiliaries(banded)
            valid, details = sensitivity_problem.evaluate_constraints(sensitivity_vector)
            delta_cost = gross * (new_fee - fee) * (1 + cash_rate)
            candidate["cost_sensitivities"].append(
                {
                    "transaction_cost_bps": bps,
                    "execution_cost": gross * new_fee,
                    "feasible": valid,
                    "validation": details,
                    "scenario_weighted_return_after_cost_and_tax_reserve": candidate[
                        "scenario_weighted_return_after_cost_and_tax_reserve"
                    ]
                    - delta_cost / total_nav
                    if weighted_complete
                    else None,
                    "scope": "Same rounded security basket; cash and full constraints recomputed",
                }
            )
        candidate["hurdle_sensitivities"] = [
            {
                "return_hurdle": h,
                "improvement_per_redeployed_dollar": per_dollar,
                "met": per_dollar >= h if per_dollar is not None and horizon_matches else None,
            }
            for h in [0.01, 0.02, 0.04]
        ]
        if membership is not None:
            old_members = allocation.get("sleeve_membership", {})
            # Membership follows the actually rounded/banded targets, not the discarded ideal targets.
            actual_members = {}
            for j, (aid, sid) in enumerate(pair_ids):
                legacy_weight = current[j] - float(old_members.get(aid, {}).get(sid, 0.0))
                sleeve_weight = max(0.0, float(banded[j]) - legacy_weight)
                actual_members.setdefault(aid, {})
                if sleeve_weight > 1e-10:
                    actual_members[aid][sid] = sleeve_weight
            candidate["sleeve_membership"] = actual_members
            candidate["target_sleeve_membership"] = membership
        if not panel["weighted_available"] or not candidate["cash_return_complete"]:
            for key in list(candidate):
                if (
                    "weighted_return" in key
                    or "expected_return" in key
                    or "estimated_terminal_wealth" in key
                ):
                    candidate[key] = None
            for row in candidate["accounts"]:
                for key in list(row):
                    if "weighted_return" in key:
                        row[key] = None
        return candidate

    baseline = finalize(
        "no_change",
        current_vector,
        reason="No discretionary change; retain the starting account balances and quantities",
    )
    # A no-change baseline does not become a mandate-compliant allocation merely because it is the baseline.
    baseline["status"] = (
        ("review_ready" if baseline["cash_return_complete"] else "draft")
        if baseline["feasible"]
        else "infeasible"
    )
    candidates = [baseline]
    benchmark_return = (
        float(panel["means"][id_index[benchmark]]) if panel["weighted_available"] else None
    )
    reference = {
        "candidate": "benchmark_reference",
        "security_id": benchmark,
        "scenario_weighted_return_before_costs_and_tax": benchmark_return,
        "feasible": None,
        "reference_only": True,
        "executable": False,
        "status": "reference",
        "reason": "Unfunded benchmark return reference; not a feasible transition or personal history",
    }

    def unavailable(name, message, status="blocked"):
        return {
            "candidate": name,
            "status": status,
            "feasible": False,
            "reason": message,
            "proposals": [],
            "accounts": [],
            "decisions": [{"action": "hold", "reason": message}],
            "issues": [],
            "executable": False,
        }

    flows = allocation.get("new_flows", {})
    if allocation.get("flow_policy") == "approved_benchmark" and flows:
        targets = current.copy()
        flow_error = None
        for aid, flow in flows.items():
            if (
                aid not in account_index
                or (aid, benchmark) not in pair_ids
                or benchmark not in permissions.get(aid, [])
            ):
                flow_error = (
                    "Every new flow needs an account permitted to buy the approved benchmark"
                )
                break
            valuation_date = bundle.get("valuation_date")
            if not valuation_date:
                dates = positions.get("valuation_date", pd.Series(dtype=str)).dropna().unique()
                valuation_date = str(dates[0]) if len(dates) == 1 else None
            if not valuation_date or flow.get("valuation_date") != valuation_date:
                flow_error = "New-flow evidence must match this snapshot valuation date; old deposits cannot be reused"
                break
            amount = float(flow["amount"])
            if amount > float(accounts.loc[aid, "cash"]) + 0.005:
                flow_error = "New flows exceed explicit snapshot cash; contributions are not invented or added twice"
                break
            j = pair_ids.index((aid, benchmark))
            targets[j] += amount / (1 + fee) / nav_for_pair[j]
        solved, check = _solve(problem, targets) if not flow_error else (None, {})
        candidates.append(
            finalize(
                "approved_benchmark_new_flows",
                solved,
                reason="Invest documented new cash already included in the snapshot into the approved eligible benchmark",
            )
            if solved is not None
            else unavailable(
                "approved_benchmark_new_flows",
                flow_error or check.get("message", "Flow candidate infeasible"),
                "infeasible",
            )
        )
    else:
        candidates.append(
            unavailable(
                "approved_benchmark_new_flows",
                "Configure approved-benchmark flow policy and evidence-backed new cash already included in the snapshot",
            )
        )

    # Minimum gross turnover repairs hard constraints; no expected-return objective is used.
    if not baseline["feasible"]:
        repair_objective = np.zeros(size)
        repair_objective[n : 2 * n] = nav_for_pair / total_nav
        repair_problem = replace(problem, objective=repair_objective)
        repair, repair_check = _solve(repair_problem)
        candidates.append(
            finalize(
                "minimal_constraint_repair",
                repair,
                reason="Minimum gross turnover needed to repair configured constraints",
            )
            if repair is not None
            else unavailable(
                "minimal_constraint_repair",
                repair_check.get("message", "Locks make repair infeasible"),
                "infeasible",
            )
        )
    else:
        candidates.append(
            finalize(
                "minimal_constraint_repair",
                current_vector,
                reason="No constraint repair is needed; retain existing quantities",
            )
        )

    membership = allocation.get("sleeve_membership", {})
    budget = allocation.get("active_sleeve_weight")
    sleeve_error = None
    targets = current.copy()
    selections = {}
    next_membership = {}
    if budget is None or allocation.get("sleeve_budget_basis") != "account_nav":
        sleeve_error = "Confirm the sleeve budget as a fraction of each account NAV"
    elif set(membership) != set(account_ids):
        sleeve_error = "Explicit sleeve membership is required for every account; legacy holdings are not incumbents"
    elif not isinstance(scores, pd.DataFrame) or scores.empty:
        scores = pd.DataFrame(columns=["security_id", "score"])
    if not sleeve_error:
        score_column = next(
            (name for name in ["score", "composite", "composite_score", "S"] if name in scores),
            None,
        )
        scored = scores.drop_duplicates("security_id").set_index("security_id")
        issuer_scores = {}
        if score_column:
            for sid in scored.index:
                if sid in security_map.index and _finite(scored.loc[sid, score_column]):
                    issuer_scores[str(security_map.loc[sid, "issuer_id"])] = float(
                        scored.loc[sid, score_column]
                    )
        percentile = pd.Series(issuer_scores, dtype=float).rank(pct=True).to_dict()
        for aid in account_ids:
            members = membership[aid]
            indices = {sid: j for j, (account, sid) in enumerate(pair_ids) if account == aid}
            if any(
                sid not in indices or float(weight) > current[indices[sid]] + 1e-8
                for sid, weight in members.items()
            ):
                sleeve_error = f"{aid}: sleeve membership exceeds the actual owned position"
                break
            if aid in taxable_locked:
                next_membership[aid] = dict(members)
                continue
            legacy = {sid: current[j] - float(members.get(sid, 0.0)) for sid, j in indices.items()}
            incumbent, entrant = [], []
            for sid, j in indices.items():
                if (
                    sid not in permissions[aid]
                    or sid in locked_ids
                    or problem.bounds[j][0] == problem.bounds[j][1]
                    or not score_column
                    or sid not in scored.index
                ):
                    continue
                value = scored.loc[sid, score_column]
                if not _finite(value):
                    continue
                issuer = str(security_map.loc[sid, "issuer_id"])
                is_incumbent = float(members.get(sid, 0.0)) > 0
                threshold = float(
                    allocation.get("retention_quantile", 0.6)
                    if is_incumbent
                    else allocation.get("entry_quantile", 0.8)
                )
                if percentile.get(issuer, 0) >= threshold:
                    (incumbent if is_incumbent else entrant).append(
                        (-float(value), float(members.get(sid, 0.0)), issuer, sid)
                    )
            slots = min(
                20,
                int(allocation.get("max_names", 20)),
                len({row[2] for row in incumbent + entrant}),
            )
            target_member = float(budget) / slots if slots else 0.0

            def selection_order(row):
                score, owned, issuer, sid = row
                # Compare total turnover against liquidating this membership. An
                # oversized holding has no extra tie advantage beyond target size.
                turnover = abs(target_member - owned) - owned
                return score, round(turnover, 12), issuer, sid

            selected = []
            seen = set()
            for _, _, issuer, sid in sorted(incumbent, key=selection_order) + sorted(
                entrant, key=selection_order
            ):
                if issuer not in seen:
                    selected.append(sid)
                    seen.add(issuer)
                if len(selected) == min(20, int(allocation.get("max_names", 20))):
                    break
            if not selected:
                residual = allocation.get("residual_security_id")
                if (
                    residual in indices
                    and residual in permissions[aid]
                    and problem.bounds[indices[residual]][1] > problem.bounds[indices[residual]][0]
                ):
                    selected = [residual]
                else:
                    sleeve_error = (
                        f"{aid}: no eligible sleeve selection or approved residual instrument"
                    )
                    break
            selections[aid] = selected
            next_membership[aid] = {sid: float(budget) / len(selected) for sid in selected}
            for sid, j in indices.items():
                targets[j] = legacy[sid] + next_membership[aid].get(sid, 0.0)
        if not sleeve_error:
            solved, check = _solve(problem, targets)
            candidate = (
                finalize(
                    "simple_equal_issuer_sleeve",
                    solved,
                    reason="Explicit account-NAV sleeve with retained eligible incumbents and preserved legacy holdings",
                    membership=next_membership,
                )
                if solved is not None
                else unavailable(
                    "simple_equal_issuer_sleeve",
                    check.get(
                        "message", "Sleeve cannot be funded without changing legacy holdings"
                    ),
                    "infeasible",
                )
            )
            candidate["selections"] = selections
            candidate["budget_basis"] = "account_nav"
            candidate["budget_weight"] = budget
            candidates.append(candidate)
    if sleeve_error:
        candidates.append(unavailable("simple_equal_issuer_sleeve", sleeve_error))

    solver = {"status": "disabled", "executable": False}
    if allocation.get("optimize", False):
        solved, solver = _solve(problem)
        if solved is not None:
            candidates.append(
                finalize(
                    "conditional_optimum",
                    solved,
                    reason="Advanced experiment under subjective scenarios",
                    experimental=True,
                )
            )
        else:
            candidates.append(
                unavailable(
                    "conditional_optimum",
                    solver.get("message", "No verified optimum"),
                    "infeasible",
                )
            )
        solver.update(
            executable=False,
            forecast_basis="subjective",
            covariance_observations=covariance_result.get("observations"),
            covariance_security_ids=ids,
            cost_accounting="Separate account securities + cash + costs + tax reserve = original NAV",
            rounding="Instrument quantity increments, whole-basket bands and complete post-rounding validation",
            baseline_feasible=baseline["feasible"],
        )
    preference = (
        (["conditional_optimum"] if allocation.get("optimize", False) else [])
        + (["minimal_constraint_repair"] if not baseline["feasible"] else [])
        + ["approved_benchmark_new_flows", "simple_equal_issuer_sleeve", "no_change"]
    )
    selected = next(
        (
            c
            for name in preference
            for c in candidates
            if c["candidate"] == name and c.get("feasible")
        ),
        baseline,
    )
    # Preserve a failed opted-in optimization status rather than disguising it as success via a baseline.
    if allocation.get("optimize", False):
        optimum = next(c for c in candidates if c["candidate"] == "conditional_optimum")
        if not optimum.get("feasible"):
            selected = optimum
            issues.append(
                _issue(
                    "allocation_infeasible",
                    "No verified feasible optimum found; no limit was relaxed.",
                )
            )
    for key in [
        "redeployed_principal",
        "improvement_per_redeployed_dollar",
        "return_hurdle_met",
        "expected_improvement_dollars_after_cost_and_tax_reserve",
    ]:
        solver[key] = selected.get(key)
    issues.extend(selected.get("issues", []))
    return {
        "status": selected["status"],
        "issues": issues,
        "selected_candidate": selected["candidate"],
        "proposals": selected.get("proposals", []) if selected.get("feasible") else [],
        "decisions": selected.get("decisions", []),
        "comparison": candidates + [reference],
        "candidates": candidates,
        "solver": solver,
    }
