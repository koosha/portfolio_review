"""Validated JSON configuration. Secrets are supplied through environment variables."""

from __future__ import annotations

import json
import math
from copy import deepcopy
from datetime import date
from pathlib import Path

DEFAULTS = {
    "source": {
        "path": "holdings.sqlite",
        "positions_query": "SELECT * FROM positions WHERE valuation_date <= :as_of",
        "accounts_query": "SELECT * FROM accounts",
        "securities_query": "SELECT * FROM securities",
        "tax_lots_query": None,
        "column_map": {},
    },
    "research": {"path": "research.sqlite", "output_dir": "reports"},
    "data": {
        "mode": "offline",
        "price_provider": "csv",
        "prices_csv": None,
        "fundamentals_csv": None,
        "macro_csv": None,
        "fund_holdings_csv": None,
        "forecasts_csv": None,
        "universe_csv": None,
        "sec_user_agent": "",
        "fred_api_key_env": "FRED_API_KEY",
        "sec_enabled": True,
        "fred_enabled": True,
        "fred_series": ["DGS10", "T10Y2Y", "CPIAUCSL", "UNRATE", "NFCI"],
        "lookback_years": 5,
        "max_price_age_days": 4,
        "max_fundamental_age_days": 150,
        "max_fund_holdings_age_days": 100,
        "max_forecast_age_days": 45,
        "max_holdings_age_days": 7,
        "require_received_by_cutoff": False,
        "refresh_network": False,
    },
    "mandate": {
        "confirmed": False,
        "base_currency": "USD",
        "benchmark_id": None,
        "issuer_cap": None,
        "sector_cap": None,
        "min_cash_weight": None,
        "max_turnover": None,
        "max_volatility": None,
        "max_stress_loss": None,
        "allow_taxable_proposals": False,
        "account_permissions": {},
        "locked_security_ids": [],
        "dealing_rules": {},
    },
    "signals": {
        "family_weights": {"quality": 1 / 3, "value": 1 / 3, "momentum": 1 / 3},
        "min_sector_size": 20,
        "winsor_low": 0.025,
        "winsor_high": 0.975,
        "momentum_months": 12,
        "momentum_skip_months": 1,
        "minimum_price": 5.0,
        "minimum_dollar_volume": 10_000_000.0,
        "excluded_sectors": ["Financials", "Real Estate"],
        "min_quality_metrics": 2,
    },
    "risk": {
        "lookback_years": 3,
        "min_weekly_observations": 104,
        "covariance": "ledoit_wolf",
        "stress_equity_shock": -0.30,
        "stress_growth_shock": -0.40,
        "stress_rates_shock": -0.15,
        "stress_sector_shocks": {},
        "bootstrap_samples": 300,
        "seed": 42,
    },
    "allocation": {
        "mode": "scenario",
        "horizon_months": 12,
        "transaction_cost_bps": 10.0,
        "min_trade_value": 500.0,
        "min_trade_weight": 0.0025,
        "active_sleeve_weight": None,
        "max_names": 20,
        "entry_quantile": 0.8,
        "retention_quantile": 0.6,
        "probability_overrides": {},
        "use_probabilities": True,
        "return_overrides": {},
        "return_hurdle": 0.02,
        "optimize": False,
        "residual_security_id": None,
        "forecast_shrinkage": 0.0,
        "cash_return": None,
        "prior_returns": {},
        "sleeve_budget_basis": None,
        "sleeve_membership": {},
        "new_flows": {},
        "flow_policy": None,
        "incumbent_gap_weight": 0.005,
        "incumbent_gap_fraction": 0.25,
    },
    "tax": {
        "enabled": False,
        "jurisdiction": "US",
        "short_term_rate": None,
        "long_term_rate": None,
        "loss_credit_rate": 0.0,
        "lot_method": "min_tax",
        "wash_sale_window_verified": False,
    },
}

# Only these non-secret groups can be adjusted through the loopback dashboard.
EDITABLE_GROUPS = {"signals", "risk", "allocation", "mandate", "tax"}
REPLACE_MAPS = {
    "probability_overrides",
    "return_overrides",
    "prior_returns",
    "stress_sector_shocks",
    "account_permissions",
    "column_map",
    "dealing_rules",
    "sleeve_membership",
    "new_flows",
}


def merge_config(base: dict, patch: dict) -> dict:
    result = deepcopy(base)
    for key, value in patch.items():
        if (
            isinstance(value, dict)
            and isinstance(result.get(key), dict)
            and key not in REPLACE_MAPS
        ):
            result[key] = merge_config(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def _number(value, name, low=None, high=None, nullable=False):
    if value is None and nullable:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number" + (" or null" if nullable else ""))
    if low is not None and value < low or high is not None and value > high:
        raise ValueError(f"{name} must be between {low} and {high}")


def validate_config(config: dict) -> dict:
    if not isinstance(config, dict):
        raise ValueError("Configuration must be a JSON object")
    unknown = set(config) - set(DEFAULTS)
    if unknown:
        raise ValueError(f"Unknown configuration groups: {sorted(unknown)}")
    for group, values in config.items():
        if not isinstance(values, dict):
            raise ValueError(f"{group} must be an object")
        extra = set(values) - set(DEFAULTS[group])
        if extra:
            raise ValueError(f"Unknown {group} fields: {sorted(extra)}")
    c = merge_config(DEFAULTS, config)
    if c["data"]["mode"] not in {"demo", "live", "offline"}:
        raise ValueError("data.mode must be demo, live or offline")
    if c["data"]["price_provider"] not in {"csv", "yahoo"}:
        raise ValueError("data.price_provider must be csv or yahoo")
    if c["allocation"]["mode"] not in {"scenario", "calibrated"}:
        raise ValueError("allocation.mode must be scenario or calibrated")
    if c["risk"]["covariance"] != "ledoit_wolf":
        raise ValueError("Only the verified ledoit_wolf covariance estimator is implemented")
    for group, keys in {
        "mandate": ["confirmed", "allow_taxable_proposals"],
        "data": ["sec_enabled", "fred_enabled", "require_received_by_cutoff", "refresh_network"],
        "allocation": ["optimize", "use_probabilities"],
        "tax": ["enabled", "wash_sale_window_verified"],
    }.items():
        for key in keys:
            if not isinstance(c[group][key], bool):
                raise ValueError(f"{group}.{key} must be true or false")
    for key in [
        "issuer_cap",
        "sector_cap",
        "min_cash_weight",
        "max_turnover",
        "max_volatility",
        "max_stress_loss",
    ]:
        _number(c["mandate"][key], f"mandate.{key}", 0, 2 if key == "max_turnover" else 1, True)
    for key in ["short_term_rate", "long_term_rate"]:
        _number(c["tax"][key], f"tax.{key}", 0, 1, True)
    if c["tax"]["jurisdiction"] != "US" or c["tax"]["lot_method"] != "min_tax":
        raise ValueError("The optional tax estimator supports US min_tax lot selection only")
    if c["tax"]["loss_credit_rate"] != 0:
        raise ValueError("Loss credits require full household tax context and are not assumed")
    if c["tax"]["enabled"] and any(
        c["tax"][k] is None for k in ["short_term_rate", "long_term_rate"]
    ):
        raise ValueError("Enabled tax estimates require explicit short-term and long-term rates")
    for key in [
        "min_trade_weight",
        "entry_quantile",
        "retention_quantile",
        "forecast_shrinkage",
        "incumbent_gap_weight",
        "incumbent_gap_fraction",
    ]:
        _number(c["allocation"][key], f"allocation.{key}", 0, 1)
    _number(c["allocation"]["active_sleeve_weight"], "active_sleeve_weight", 0, 1, nullable=True)
    _number(c["allocation"]["cash_return"], "allocation.cash_return", -1, 10, nullable=True)
    _number(c["allocation"]["return_hurdle"], "return_hurdle", 0, 1)
    _number(c["allocation"]["transaction_cost_bps"], "transaction_cost_bps", 0, 500)
    _number(c["allocation"]["min_trade_value"], "min_trade_value", 0, 1e9)
    if c["allocation"]["horizon_months"] not in {6, 12, 18}:
        raise ValueError("horizon_months must be 6, 12 or 18")
    if c["allocation"]["retention_quantile"] > c["allocation"]["entry_quantile"]:
        raise ValueError("Retention threshold cannot exceed the entry threshold")
    for group, key, low, high in [
        ("signals", "min_sector_size", 2, 1000),
        ("signals", "momentum_months", 3, 36),
        ("signals", "momentum_skip_months", 0, 6),
        ("signals", "min_quality_metrics", 2, 3),
        ("allocation", "max_names", 1, 20),
        ("risk", "min_weekly_observations", 26, 520),
        ("risk", "bootstrap_samples", 50, 5000),
        ("risk", "seed", 0, 2**32 - 1),
        ("risk", "lookback_years", 1, 20),
        ("data", "lookback_years", 1, 30),
    ]:
        _number(c[group][key], f"{group}.{key}", low, high)
        if int(c[group][key]) != c[group][key]:
            raise ValueError(f"{group}.{key} must be an integer")
    if c["signals"]["momentum_skip_months"] >= c["signals"]["momentum_months"]:
        raise ValueError("Momentum skip must be smaller than lookback")
    for key in ["winsor_low", "winsor_high"]:
        _number(c["signals"][key], key, 0, 1)
    if c["signals"]["winsor_low"] >= c["signals"]["winsor_high"]:
        raise ValueError("Winsorization bounds must be increasing")
    for key in ["minimum_price", "minimum_dollar_volume"]:
        _number(c["signals"][key], key, 0)
    weights = c["signals"]["family_weights"]
    if set(weights) != {"quality", "value", "momentum"}:
        raise ValueError("Provide quality, value, and momentum weights")
    for key, value in weights.items():
        _number(value, f"family_weights.{key}", 0, 1)
    if abs(sum(weights.values()) - 1) > 1e-8:
        raise ValueError("Family weights must sum to 1")
    for group, names in {
        "allocation": ["return_overrides", "prior_returns"],
        "risk": ["stress_sector_shocks"],
    }.items():
        for name in names:
            if not isinstance(c[group][name], dict):
                raise ValueError(f"{group}.{name} must be an object")
    for key in ["stress_equity_shock", "stress_growth_shock", "stress_rates_shock"]:
        _number(c["risk"][key], key, -1, 1)
    for key, value in c["risk"]["stress_sector_shocks"].items():
        _number(value, f"stress_sector_shocks.{key}", -1, 1)
    for key in [
        "max_price_age_days",
        "max_fundamental_age_days",
        "max_fund_holdings_age_days",
        "max_forecast_age_days",
        "max_holdings_age_days",
    ]:
        _number(c["data"][key], key, 0, 1000)
    p = c["allocation"]["probability_overrides"]
    if not isinstance(p, dict):
        raise ValueError("probability_overrides must be an object")
    for key, value in p.items():
        _number(value, f"probability.{key}", 0, 1)
    if p and abs(sum(p.values()) - 1) > 1e-8:
        raise ValueError("Scenario probabilities must sum to 1")
    for sid, scenarios in c["allocation"]["return_overrides"].items():
        if not isinstance(scenarios, dict):
            raise ValueError("return_overrides must map security IDs to scenario objects")
        for label, value in scenarios.items():
            _number(value, f"return_overrides.{sid}.{label}", -1, 10)
    for sid, value in c["allocation"]["prior_returns"].items():
        _number(value, f"prior_returns.{sid}", -1, 10)
    for key in ["base_currency", "benchmark_id"]:
        if c["mandate"][key] is not None and (
            not isinstance(c["mandate"][key], str) or not c["mandate"][key]
        ):
            raise ValueError(f"mandate.{key} must be a nonempty string")
    if c["mandate"]["base_currency"] is not None and (
        len(c["mandate"]["base_currency"]) != 3
        or not c["mandate"]["base_currency"].isalpha()
        or not c["mandate"]["base_currency"].isupper()
    ):
        raise ValueError("Use a three-letter base currency")
    if not isinstance(c["mandate"]["account_permissions"], dict):
        raise ValueError("account_permissions must map account IDs to security ID lists")
    for account, permitted in c["mandate"]["account_permissions"].items():
        if not isinstance(permitted, list) or not all(isinstance(x, str) for x in permitted):
            raise ValueError(f"account_permissions.{account} must be a list of security IDs")
    if c["allocation"]["sleeve_budget_basis"] not in {None, "account_nav"}:
        raise ValueError("sleeve_budget_basis must be null or account_nav")
    if c["allocation"]["flow_policy"] not in {None, "approved_benchmark"}:
        raise ValueError("flow_policy must be null or approved_benchmark")
    for group, names in {
        "allocation": ["sleeve_membership", "new_flows", "return_overrides", "prior_returns"],
        "mandate": ["dealing_rules"],
        "risk": ["stress_sector_shocks"],
    }.items():
        for name in names:
            if not isinstance(c[group][name], dict):
                raise ValueError(f"{group}.{name} must be an object")
    for aid, members in c["allocation"]["sleeve_membership"].items():
        if not isinstance(aid, str) or not isinstance(members, dict):
            raise ValueError(
                "sleeve_membership maps account IDs to security/account-NAV weight objects"
            )
        for sid, weight in members.items():
            if not isinstance(sid, str) or not sid:
                raise ValueError("Sleeve member security IDs must be nonempty strings")
            _number(weight, f"sleeve_membership.{aid}.{sid}", 0, 1)
        if sum(members.values()) > 1 + 1e-8:
            raise ValueError("Sleeve member account-NAV weights cannot exceed one")
    for aid, rules in c["mandate"]["dealing_rules"].items():
        if not isinstance(rules, dict):
            raise ValueError("dealing_rules maps account IDs to security rule objects")
        for sid, rule in rules.items():
            if not isinstance(rule, dict) or set(rule) != {
                "fractional_shares",
                "quantity_increment",
                "dealing_allowed",
            }:
                raise ValueError(
                    "Each dealing rule requires fractional_shares, quantity_increment and dealing_allowed"
                )
            if not isinstance(rule["fractional_shares"], bool) or not isinstance(
                rule["dealing_allowed"], bool
            ):
                raise ValueError("Dealing permissions must be booleans")
            _number(
                rule["quantity_increment"], f"dealing_rules.{aid}.{sid}.quantity_increment", 1e-8
            )
            if not rule["fractional_shares"] and (
                rule["quantity_increment"] < 1 or rule["quantity_increment"] % 1
            ):
                raise ValueError("Whole-share rules require an integer quantity increment")
    for aid, flow in c["allocation"]["new_flows"].items():
        if not isinstance(flow, dict) or set(flow) != {
            "amount",
            "source_id",
            "included_in_snapshot",
            "valuation_date",
        }:
            raise ValueError(
                "New flows require amount, source_id, included_in_snapshot and valuation_date"
            )
        _number(flow["amount"], f"new_flows.{aid}.amount", 0)
        if not isinstance(flow["valuation_date"], str):
            raise ValueError("New-flow valuation_date must be an ISO date")
        date.fromisoformat(flow["valuation_date"])
        if (
            flow["included_in_snapshot"] is not True
            or not isinstance(flow["source_id"], str)
            or not flow["source_id"].strip()
        ):
            raise ValueError(
                "Flows must have source evidence and already be included in snapshot NAV/cash"
            )
    if Path(c["source"]["path"]).resolve() == Path(c["research"]["path"]).resolve():
        raise ValueError("Source holdings database and research database must be different files")
    return c


def load_config(path: str | Path) -> dict:
    path = Path(path).expanduser().resolve()
    c = validate_config(json.loads(path.read_text()))
    # All paths are relative to the config file, not the caller's working directory.
    for group, keys in {
        "source": ["path"],
        "research": ["path", "output_dir"],
        "data": [
            "prices_csv",
            "fundamentals_csv",
            "macro_csv",
            "fund_holdings_csv",
            "forecasts_csv",
            "universe_csv",
        ],
    }.items():
        for key in keys:
            if c[group].get(key):
                p = Path(c[group][key]).expanduser()
                c[group][key] = str(
                    (path.parent / p).resolve() if not p.is_absolute() else p.resolve()
                )
    return validate_config(c)


def dashboard_patch(config: dict, patch: dict) -> dict:
    if not isinstance(patch, dict) or set(patch) - EDITABLE_GROUPS:
        raise ValueError(
            "Only mandate, signals, risk, allocation, and tax can be edited in the dashboard"
        )
    merged = merge_config(config, patch)
    if merged["allocation"]["horizon_months"] != config["allocation"]["horizon_months"]:
        # Returns and priors belong to a particular horizon. Require explicit
        # replacements instead of carrying a 12-month override into 6 months.
        for key in ["return_overrides", "prior_returns", "cash_return"]:
            if key not in patch.get("allocation", {}):
                merged["allocation"][key] = None if key == "cash_return" else {}
    return validate_config(merged)


def write_config(path: str | Path, config: dict):
    validated = validate_config(config)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(validated, indent=2) + "\n")
