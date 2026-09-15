"""Deterministic portfolio measurements; missing evidence is never a zero return.

Adjusted closes are required to follow a distribution-inclusive total-return
convention. No dividends are added by this module. Historical risk charts replay
today's weights and are not the investor's historical performance.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd
from sklearn.covariance import LedoitWolf


def _issue(code: str, message: str, severity: str = "warning") -> dict:
    return {"severity": severity, "code": code, "message": message}


def _number(value: Any) -> float | None:
    try:
        if isinstance(value, (bool, np.bool_)):
            return None
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _text_present(value: Any) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and value.strip().lower() not in {"nan", "none", "null"}
    )


def _clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (float, np.floating)):
        return _number(value)
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if isinstance(value, (pd.Timestamp,)):
        return value.isoformat()
    if value is pd.NA or value is pd.NaT:
        return None
    return value


def _frame(bundle: dict, name: str) -> pd.DataFrame:
    value = bundle.get(name)
    return value.copy() if isinstance(value, pd.DataFrame) else pd.DataFrame()


def _cutoff(bundle: dict) -> pd.Timestamp:
    from portfolio_research.calendar import bundle_cutoff

    return bundle_cutoff(bundle)


def _available_times(values: pd.Series) -> pd.Series:
    """Parse availability stamps; a bare calendar date starts its New York day."""
    from portfolio_research.calendar import new_york_dates

    parsed = pd.to_datetime(values, errors="coerce", utc=True, format="mixed")
    return new_york_dates(values, parsed)


def _observed(
    frame: pd.DataFrame,
    bundle: dict,
    date_column: str,
    publication_column: str | None = "available_at",
    config: dict | None = None,
) -> pd.DataFrame:
    if frame.empty or date_column not in frame:
        return frame.iloc[0:0].copy()
    cutoff = _cutoff(bundle)
    dates = _available_times(frame[date_column])
    mask = dates.notna() & dates.le(cutoff)
    if publication_column and publication_column not in frame:
        return frame.iloc[0:0].copy()
    if publication_column:
        available = _available_times(frame[publication_column])
        mask &= available.notna() & available.le(cutoff)
    if config and config.get("data", {}).get("require_received_by_cutoff", False):
        if "received_at" not in frame:
            return frame.iloc[0:0].copy()
        received = _available_times(frame.received_at)
        mask &= received.notna() & received.le(cutoff)
    return frame.loc[mask].copy()


def reconcile(bundle: dict, config: dict) -> dict:
    """Reconcile explicit account NAV and cash. Never normalize partial holdings."""
    accounts, positions = _frame(bundle, "accounts"), _frame(bundle, "positions")
    securities = _frame(bundle, "securities")
    base = config.get("mandate", {}).get("base_currency", "USD")
    issues, account_rows, holdings = [], [], []
    if accounts.empty:
        issues.append(_issue("missing_accounts", "Account totals and cash are required.", "error"))
    if not accounts.empty and accounts.account_id.duplicated().any():
        issues.append(
            _issue("duplicate_accounts", "Duplicate account IDs prevent reconciliation.", "error")
        )
    if not positions.empty and positions.duplicated(["account_id", "security_id"]).any():
        issues.append(
            _issue(
                "duplicate_positions",
                "Duplicate account/security positions require reconciliation.",
                "error",
            )
        )
    secmap = (
        securities.set_index("security_id").to_dict("index")
        if not securities.empty and not securities.security_id.duplicated().any()
        else {}
    )
    account_ids = set(accounts.get("account_id", []))
    for _, p in positions.iterrows():
        row = p.to_dict()
        row.update(
            {
                k: secmap.get(p.security_id, {}).get(k)
                for k in ("ticker", "issuer_id", "sector", "instrument_type", "name")
            }
        )
        row["calculated_weight"] = None
        mv, quantity, price = (
            _number(p.get("market_value")),
            _number(p.get("quantity")),
            _number(p.get("price")),
        )
        if p.account_id not in account_ids:
            issues.append(
                _issue(
                    "unknown_account",
                    f"{p.security_id} references unknown account {p.account_id}.",
                    "error",
                )
            )
        if mv is None or mv < 0:
            issues.append(
                _issue(
                    "invalid_market_value",
                    f"{p.security_id} has missing/negative market value.",
                    "error",
                )
            )
        if p.get("currency") != base:
            issues.append(
                _issue(
                    "currency_mismatch",
                    f"{p.security_id} is not valued in {base}; verified FX conversion required.",
                    "error",
                )
            )
        if p.security_id not in secmap or not _text_present(
            secmap.get(p.security_id, {}).get("issuer_id")
        ):
            issues.append(
                _issue(
                    "unresolved_security", f"Unresolved security/issuer: {p.security_id}.", "error"
                )
            )
        valuation = pd.to_datetime(
            p.get("valuation_date"), errors="coerce", utc=True, format="mixed"
        )
        if pd.isna(valuation) or valuation > _cutoff(bundle):
            issues.append(
                _issue(
                    "invalid_valuation_date",
                    f"{p.security_id} valuation date is missing or in the future.",
                    "error",
                )
            )
        elif (_cutoff(bundle) - valuation).days > config.get("data", {}).get(
            "max_holdings_age_days", 7
        ):
            issues.append(
                _issue(
                    "stale_position",
                    f"{p.security_id} position valuation is stale for this run.",
                    "error",
                )
            )
        if (
            quantity is not None
            and price is not None
            and mv is not None
            and abs(quantity * price - mv) > max(0.02, abs(mv) * 0.001)
        ):
            issues.append(
                _issue(
                    "position_arithmetic",
                    f"{p.security_id}: quantity × price does not reconcile to value.",
                    "error",
                )
            )
        holdings.append(row)
    for _, account in accounts.drop_duplicates("account_id").iterrows():
        aid = account.account_id
        selected = positions[positions.account_id == aid] if not positions.empty else positions
        values = pd.to_numeric(
            selected.get("market_value", pd.Series(dtype=float)), errors="coerce"
        )
        known_positions = float(values.dropna().sum())
        nav, cash = _number(account.get("total_value")), _number(account.get("cash"))
        valid_nav = nav is not None and nav > 0
        valid_cash = cash is not None and cash >= 0
        currency_valid = account.get("currency") == base and (
            selected.empty or selected.currency.eq(base).all()
        )
        residual = (
            nav - known_positions - (cash if valid_cash else 0)
            if valid_nav and currency_valid
            else None
        )
        tolerance = max(0.02, (nav or 0) * 1e-7)
        complete = (
            bool(account.get("complete", False))
            and valid_nav
            and valid_cash
            and currency_valid
            and values.notna().all()
            and residual is not None
            and abs(residual) <= tolerance
        )
        if not valid_nav:
            issues.append(
                _issue(
                    "missing_account_nav",
                    f"{aid}: positive reported total_value is required.",
                    "error",
                )
            )
        if not valid_cash:
            issues.append(
                _issue(
                    "missing_account_cash",
                    f"{aid}: cash is unknown or invalid; residual is not inferred cash.",
                    "error",
                )
            )
        if not currency_valid:
            issues.append(
                _issue(
                    "account_currency",
                    f"{aid}: all values must be in explicit base currency {base}.",
                    "error",
                )
            )
        if not bool(account.get("complete", False)):
            issues.append(
                _issue(
                    "account_incomplete",
                    f"{aid}: source does not confirm complete account coverage.",
                    "error",
                )
            )
        if residual is not None and abs(residual) > tolerance:
            issues.append(
                _issue(
                    "unclassified_value" if residual > 0 else "overstated_assets",
                    f"{aid}: positions and explicit cash differ from NAV by {residual:,.2f} {base}.",
                    "error",
                )
            )
        account_rows.append(
            {
                "account_id": aid,
                "total_value": nav,
                "known_position_value": known_positions,
                "cash": cash,
                "unclassified_value": residual,
                "coverage": (known_positions + (cash or 0)) / nav
                if valid_nav and currency_valid
                else None,
                "complete": complete,
                "currency": account.get("currency"),
            }
        )
        for row in holdings:
            if row["account_id"] == aid:
                row["calculated_weight"] = (
                    _number(row.get("market_value")) / nav
                    if valid_nav and currency_valid and _number(row.get("market_value")) is not None
                    else None
                )
    consolidated = (
        bool(account_rows)
        and all(
            r["total_value"] is not None and r["total_value"] > 0 and r["currency"] == base
            for r in account_rows
        )
        and not any(
            i["code"] in {"currency_mismatch", "unknown_account", "duplicate_accounts"}
            for i in issues
        )
    )
    nav = sum(r["total_value"] for r in account_rows) if consolidated else None
    position_value = sum(r["known_position_value"] for r in account_rows) if consolidated else None
    known_cash = (
        sum(r["cash"] for r in account_rows if r["cash"] is not None) if consolidated else None
    )
    summary = {
        "total_value": nav,
        "known_position_value": position_value,
        "known_cash": known_cash,
        "unclassified_value": nav - position_value - known_cash if nav is not None else None,
        "coverage": (position_value + known_cash) / nav if nav else None,
        "complete": bool(account_rows)
        and all(r["complete"] for r in account_rows)
        and not any(i["severity"] == "error" for i in issues),
        "currency": base,
        "accounts": account_rows,
        "position_count": len(positions),
        "unknown_cash_accounts": [r["account_id"] for r in account_rows if r["cash"] is None],
    }
    return _clean({"summary": summary, "issues": issues, "holdings": holdings})


def score_securities(bundle: dict, config: dict) -> pd.DataFrame:
    """Cross-sectional Q/V/M ranks over unique eligible issuers, not held names.

    Duplicate share classes inherit their representative issuer's score, with an
    explicit marker; they never count twice in a sector cross-section.
    """
    securities = _frame(bundle, "securities")
    if securities.empty:
        return pd.DataFrame(
            columns=["security_id", "quality", "value", "momentum", "score", "eligible", "reasons"]
        )
    signals, data = config.get("signals", {}), config.get("data", {})
    prices = _observed(_frame(bundle, "prices"), bundle, "date", config=config)
    fundamentals = _observed(_frame(bundle, "fundamentals"), bundle, "period_end", config=config)
    now = _cutoff(bundle)
    if not prices.empty:
        prices["_date"] = pd.to_datetime(prices.date, utc=True)
        prices = prices.sort_values("_date")
    if not fundamentals.empty:
        fundamentals["_period"] = pd.to_datetime(fundamentals.period_end, utc=True)
        fundamentals["_available"] = pd.to_datetime(fundamentals.available_at, utc=True)
        fundamentals = fundamentals.sort_values(["_period", "_available"])
    rows = []
    anchor = pd.Period(now.tz_convert("America/New_York").tz_localize(None), freq="M")
    old_month = anchor - int(signals.get("momentum_months", 12))
    recent_month = anchor - int(signals.get("momentum_skip_months", 1))
    from portfolio_research.calendar import month_end_session, trailing_sessions

    liquidity_days = set(trailing_sessions(bundle["as_of"], 60))
    momentum_start_day = month_end_session(str(old_month))
    momentum_end_day = month_end_session(str(recent_month))
    for _, security in securities.iterrows():
        sid, reasons = security.security_id, []
        p = prices[prices.security_id == sid] if not prices.empty else prices
        f = (
            fundamentals[fundamentals.security_id == sid]
            if not fundamentals.empty
            else fundamentals
        )
        frow = f.iloc[-1] if not f.empty else pd.Series(dtype=object)
        row = {
            "security_id": sid,
            "ticker": security.get("ticker"),
            "issuer_id": security.get("issuer_id"),
            "sector": security.get("sector"),
            "market_cap": _number(security.get("market_cap")),
            "quality": np.nan,
            "value": np.nan,
            "momentum": np.nan,
            "score": np.nan,
            "reasons": reasons,
        }
        latest = _number(p.iloc[-1].get("close")) if not p.empty else None
        observed_days = p["_date"].dt.strftime("%Y-%m-%d") if not p.empty else pd.Series(dtype=str)
        liquidity_rows = p.loc[observed_days.isin(liquidity_days)] if not p.empty else p
        dollar_vol = pd.to_numeric(
            liquidity_rows.get("close", pd.Series(dtype=float)), errors="coerce"
        ) * pd.to_numeric(liquidity_rows.get("volume", pd.Series(dtype=float)), errors="coerce")
        observed_liquidity_days = (
            liquidity_rows["_date"].dt.strftime("%Y-%m-%d")
            if not liquidity_rows.empty
            else pd.Series(dtype=str)
        )
        adv = (
            _number(dollar_vol.median())
            if len(dollar_vol) == 60
            and set(observed_liquidity_days) == liquidity_days
            and dollar_vol.notna().all()
            and np.isfinite(dollar_vol).all()
            and dollar_vol.ge(0).all()
            else None
        )
        row.update({"price": latest, "median_dollar_volume": adv})
        if security.get("instrument_type") != "equity":
            reasons.append("not_company_equity")
        if security.get("domicile") != "US":
            reasons.append("us_domicile_unverified")
        if security.get("equity_type") != "ordinary_common":
            reasons.append("ordinary_common_equity_unverified")
        if not bool(security.get("eligible", False)):
            reasons.append("source_ineligible")
        if security.get("currency") != config.get("mandate", {}).get("base_currency", "USD"):
            reasons.append("currency_mismatch")
        if (
            not p.empty
            and not p.currency.eq(config.get("mandate", {}).get("base_currency", "USD")).all()
        ):
            reasons.append("price_currency_mismatch")
        if security.get("sector") in signals.get("excluded_sectors", []):
            reasons.append("separate_sector_model_required")
        if latest is None or latest < signals.get("minimum_price", 5):
            reasons.append("price_ineligible")
        if adv is None or adv < signals.get("minimum_dollar_volume", 10_000_000):
            reasons.append("liquidity_ineligible")
        if p.empty or (now - p.iloc[-1]["_date"]).days > data.get("max_price_age_days", 7):
            reasons.append("stale_price")
        if row["market_cap"] is None or row["market_cap"] <= 0:
            reasons.append("invalid_issuer_market_cap")
        cap_date = pd.to_datetime(
            security.get("market_cap_as_of"), errors="coerce", utc=True, format="mixed"
        )
        if (
            pd.isna(cap_date)
            or cap_date > now
            or (now - cap_date).days > data.get("max_price_age_days", 7)
        ):
            reasons.append("missing_future_or_stale_market_cap_date")
        cap_public = pd.to_datetime(
            security.get("market_cap_available_at"), errors="coerce", utc=True, format="mixed"
        )
        if pd.isna(cap_public) or cap_public > now:
            reasons.append("market_cap_publication_missing_or_future")
        if data.get("require_received_by_cutoff", False):
            cap_received = pd.to_datetime(
                security.get("market_cap_received_at"), errors="coerce", utc=True, format="mixed"
            )
            if pd.isna(cap_received) or cap_received > now:
                reasons.append("market_cap_not_observed_by_cutoff")
        row["eligible"] = not reasons
        valid_f = not f.empty and (now - frow["_period"]).days <= data.get(
            "max_fundamental_age_days", 150
        )
        if not valid_f:
            reasons.append("missing_or_stale_fundamentals")
        if frow.get("currency") != security.get("currency") or frow.get("currency") != config.get(
            "mandate", {}
        ).get("base_currency", "USD"):
            valid_f = False
            reasons.append("missing_or_mismatched_fundamental_currency")

        def ratio(numerator, denominator):
            n, d = _number(numerator), _number(denominator)
            return n / d if valid_f and n is not None and d is not None and d > 0 else np.nan

        assets, begin = _number(frow.get("assets")), _number(frow.get("assets_begin"))
        average = (
            (assets + begin) / 2
            if assets is not None and begin is not None and assets > 0 and begin > 0
            else None
        )
        income, ocf, capex = (
            _number(frow.get(k)) for k in ("net_income", "operating_cash_flow", "capex")
        )
        accrual = income - ocf if income is not None and ocf is not None else None
        cashflow = ocf - capex if ocf is not None and capex is not None and capex >= 0 else None
        common_income = (
            _number(frow.get("income_common"))
            if frow.get("earnings_definition") == "common_shareholders"
            else None
        )
        if common_income is None:
            reasons.append("common_share_earnings_unqualified")
        row.update(
            {
                "earnings_definition": frow.get("earnings_definition"),
                "gross_profitability": ratio(frow.get("gross_profit"), begin),
                "operating_profitability": ratio(frow.get("operating_income"), average),
                "negative_accruals": ratio(-accrual if accrual is not None else None, average),
                "earnings_yield": ratio(common_income, row["market_cap"]),
                "cashflow_yield": ratio(cashflow, row["market_cap"]),
                "raw_momentum": np.nan,
                "momentum_start_session": momentum_start_day,
                "momentum_end_session": momentum_end_day,
                "liquidity_session_count": int(len(liquidity_rows)),
            }
        )
        if not p.empty:
            session_days = p["_date"].dt.strftime("%Y-%m-%d")
            older, recent = (
                p[session_days == momentum_start_day],
                p[session_days == momentum_end_day],
            )
            if not older.empty and not recent.empty:
                start, end = (
                    _number(older.iloc[-1].adjusted_close),
                    _number(recent.iloc[-1].adjusted_close),
                )
                if start is not None and start > 0 and end is not None and end > 0:
                    row["raw_momentum"] = end / start - 1
        if pd.isna(row["raw_momentum"]):
            reasons.append("missing_momentum_history")
        rows.append(row)
    result = pd.DataFrame(rows)
    for issuer, group in result[result.eligible].groupby("issuer_id"):
        caps = group.market_cap.dropna()
        if len(caps) > 1 and (caps.max() - caps.min()) / caps.max() > 0.001:
            for index in group.index:
                result.loc[index, "eligible"] = False
                result.at[index, "reasons"].append(
                    "inconsistent_issuer_wide_market_cap_across_classes"
                )
    result["representative_security_id"] = None
    result["is_representative"] = False
    # Common-equity market capitalization is issuer-wide; never sum classes.
    eligible = result[result.eligible].sort_values(
        ["median_dollar_volume", "security_id"], ascending=[False, True]
    )
    representatives = (
        eligible.drop_duplicates("issuer_id").sort_values("market_cap", ascending=False).head(1000)
    )
    indices = representatives.index
    qcols = ["gross_profitability", "operating_profitability", "negative_accruals"]
    vcols = ["earnings_yield", "cashflow_yield"]
    lo, hi = signals.get("winsor_low", 0.025), signals.get("winsor_high", 0.975)
    minimum = int(signals.get("min_sector_size", 20))
    for _, group in result.loc[indices].groupby("sector", dropna=False):
        rank = pd.DataFrame(index=group.index)
        for col in qcols + vcols:
            usable = group[col].dropna()
            if len(usable) >= minimum:
                rank[col] = (
                    group[col]
                    .clip(usable.quantile(lo), usable.quantile(hi))
                    .rank(pct=True, method="average")
                )
            else:
                rank[col] = np.nan
        valid_quality = rank[qcols].notna().sum(axis=1) >= signals.get("min_quality_metrics", 2)
        result.loc[group.index, "quality"] = rank[qcols].mean(axis=1).where(valid_quality)
        result.loc[group.index, "value"] = (
            rank[vcols].mean(axis=1).where(rank[vcols].notna().all(axis=1))
        )
    momentum = result.loc[indices, "raw_momentum"]
    result.loc[indices, "momentum"] = momentum.clip(
        momentum.quantile(lo), momentum.quantile(hi)
    ).rank(pct=True, method="average")
    family_weights = signals.get(
        "family_weights", {"quality": 1 / 3, "value": 1 / 3, "momentum": 1 / 3}
    )
    denominator = sum(family_weights.values())
    complete = result.loc[indices, ["quality", "value", "momentum"]].notna().all(axis=1)
    composite = (
        sum(result.loc[indices, col] * weight for col, weight in family_weights.items())
        / denominator
    )
    result.loc[indices, "score"] = composite.where(complete)
    for index in indices:
        representative = result.loc[index]
        members = result.issuer_id == representative.issuer_id
        result.loc[members, "representative_security_id"] = representative.security_id
        result.loc[index, "is_representative"] = True
        for target in result.index[members]:
            if result.loc[target, "eligible"]:
                for col in ("quality", "value", "momentum", "score"):
                    result.loc[target, col] = representative[col]
                if target != index:
                    result.at[target, "reasons"].append("inherits_representative_issuer_score")
    for index, row in result.iterrows():
        if row.eligible and pd.isna(row.score):
            result.at[index, "reasons"].append("incomplete_family_or_insufficient_sector_peers")
    result["data_status"] = np.where(result.score.notna(), "complete", "incomplete")
    return result


def _fund_rows(bundle: dict, config: dict, fund_id: str) -> tuple[pd.DataFrame, list[dict]]:
    rows = _observed(_frame(bundle, "fund_holdings"), bundle, "holdings_date", config=config)
    rows = rows[rows.fund_id == fund_id] if not rows.empty else rows
    if rows.empty:
        return rows, [
            _issue("missing_fund_holdings", f"{fund_id}: underlying holdings unavailable.", "error")
        ]
    dates = pd.to_datetime(rows.holdings_date, utc=True)
    latest = dates.max()
    rows = rows.loc[dates == latest].copy()
    if (_cutoff(bundle) - latest).days > config.get("data", {}).get(
        "max_fund_holdings_age_days", 120
    ):
        return rows.iloc[0:0], [
            _issue(
                "stale_fund_holdings", f"{fund_id}: latest underlying holdings are stale.", "error"
            )
        ]
    weights = pd.to_numeric(rows.weight, errors="coerce")
    if (
        weights.isna().any()
        or (weights < 0).any()
        or weights.sum() > 1 + 1e-6
        or rows.issuer_id.duplicated().any()
    ):
        return rows.iloc[0:0], [
            _issue(
                "invalid_fund_holdings",
                f"{fund_id}: invalid/duplicate underlying weights.",
                "error",
            )
        ]
    return rows, []


def exposures(bundle: dict, config: dict) -> dict:
    reconciliation = reconcile(bundle, config)
    summary = reconciliation["summary"]
    nav = summary["total_value"]
    securities = _frame(bundle, "securities")
    if securities.empty:
        return {
            "issuer_exposure": [],
            "sector_exposure": [],
            "issues": [_issue("missing_securities", "Security metadata missing.", "error")],
            "status": "incomplete",
        }
    metadata = securities.drop_duplicates("security_id").set_index("security_id").to_dict("index")
    issuer_sector = {}
    for issuer, group in securities.groupby("issuer_id"):
        sectors = group.sector.dropna().unique()
        issuer_sector[issuer] = sectors[0] if len(sectors) == 1 else "Unknown"
    issuers, sectors, issues = {}, {}, []
    unknown = 0.0

    def add(issuer, sector, value, direct, source):
        item = issuers.setdefault(
            str(issuer),
            {
                "issuer_id": str(issuer),
                "sector": sector,
                "market_value": 0.0,
                "direct_value": 0.0,
                "indirect_value": 0.0,
                "sources": [],
            },
        )
        item["market_value"] += value
        item["direct_value" if direct else "indirect_value"] += value
        if source not in item["sources"]:
            item["sources"].append(source)
        sectors[sector] = sectors.get(sector, 0.0) + value

    for position in reconciliation["holdings"]:
        value = _number(position.get("market_value"))
        if value is None or position.get("currency") != summary["currency"]:
            continue
        sid = position["security_id"]
        security = metadata.get(sid, {})
        if security.get("instrument_type") in {"etf", "plan_fund"}:
            fund, fund_issues = _fund_rows(bundle, config, sid)
            issues.extend(fund_issues)
            covered = 0.0
            for _, row in fund.iterrows():
                fraction = float(row.weight)
                if row.issuer_id == "CASH":
                    sectors["Cash"] = sectors.get("Cash", 0.0) + value * fraction
                    covered += fraction
                    continue
                sector = issuer_sector.get(row.issuer_id, "Unknown")
                add(row.issuer_id, sector, value * fraction, False, sid)
                covered += fraction
            remainder = max(0.0, 1.0 - covered) * value
            unknown += remainder
            if remainder > 0.01:
                issues.append(
                    _issue(
                        "partial_fund_coverage",
                        f"{sid}: {1 - covered:.1%} underlying exposure is unclassified.",
                        "error",
                    )
                )
        elif _text_present(security.get("issuer_id")):
            add(
                security["issuer_id"],
                security.get("sector") if _text_present(security.get("sector")) else "Unknown",
                value,
                True,
                sid,
            )
        else:
            unknown += value
    unclassified = summary.get("unclassified_value")
    unknown += max(0.0, unclassified or 0.0)
    if unknown:
        sectors["Unclassified"] = sectors.get("Unclassified", 0.0) + unknown
    known_cash = summary.get("known_cash")
    if known_cash is not None and known_cash > 0:
        sectors["Cash"] = sectors.get("Cash", 0.0) + known_cash
    issuer_rows = sorted(issuers.values(), key=lambda x: x["market_value"], reverse=True)
    for row in issuer_rows:
        row["weight"] = row["market_value"] / nav if nav else None
    sector_rows = [
        {"sector": sector, "market_value": value, "weight": value / nav if nav else None}
        for sector, value in sorted(sectors.items(), key=lambda x: -x[1])
    ]
    status = "complete" if summary["complete"] and unknown <= 0.01 and not issues else "incomplete"
    return _clean(
        {
            "issuer_exposure": issuer_rows,
            "sector_exposure": sector_rows,
            "unclassified_value": unknown,
            "status": status,
            "issues": issues,
        }
    )


def _global_weights(bundle: dict, config: dict, weights=None) -> tuple[dict, list[dict]]:
    recon = reconcile(bundle, config)
    issues = list(recon["issues"])
    nav = recon["summary"]["total_value"]
    if not nav or not recon["summary"]["complete"]:
        return {}, issues + [
            _issue(
                "incomplete_portfolio",
                "Complete reconciled holdings, cash, and account NAV are required.",
                "error",
            )
        ]
    accounts = {a["account_id"]: a for a in recon["summary"]["accounts"]}
    rows = []
    if weights is None:
        for p in recon["holdings"]:
            rows.append(
                {
                    "account_id": p["account_id"],
                    "security_id": p["security_id"],
                    "weight": p["market_value"] / accounts[p["account_id"]]["total_value"],
                }
            )
        rows.extend(
            {
                "account_id": aid,
                "security_id": "CASH",
                "weight": account["cash"] / account["total_value"],
            }
            for aid, account in accounts.items()
        )
        weights = pd.DataFrame(rows)
    elif not isinstance(weights, pd.DataFrame):
        return {}, [
            _issue("invalid_weights", "Weights must be an account/security DataFrame.", "error")
        ]
    if not {"account_id", "security_id", "weight"}.issubset(weights):
        return {}, [_issue("invalid_weights", "Missing account_id/security_id/weight.", "error")]
    if (
        set(weights.account_id) != set(accounts)
        or weights.duplicated(["account_id", "security_id"]).any()
    ):
        return {}, [
            _issue(
                "invalid_weights", "Every account must occur exactly once per security.", "error"
            )
        ]
    result = {}
    reserves = weights.attrs.get("reserved_cost_weight_by_account", {})
    for aid, group in weights.groupby("account_id"):
        w = pd.to_numeric(group.weight, errors="coerce")
        reserve = _number(reserves.get(aid, 0.0))
        if (
            reserve is None
            or not 0 <= reserve < 1
            or w.isna().any()
            or (w < -1e-10).any()
            or abs(w.sum() + reserve - 1) > 1e-6
        ):
            return {}, [
                _issue(
                    "invalid_weights",
                    f"{aid}: nonnegative weights including cash plus explicit fees/tax reserve must sum to one.",
                    "error",
                )
            ]
        for row in group.itertuples():
            if row.weight > 1e-12:
                result[row.security_id] = (
                    result.get(row.security_id, 0.0)
                    + float(row.weight) * accounts[aid]["total_value"] / nav
                )
    return result, issues


def _reserved_cost_weight(bundle: dict, config: dict, weights=None) -> float:
    if not isinstance(weights, pd.DataFrame):
        return 0.0
    reserves = weights.attrs.get("reserved_cost_weight_by_account", {})
    summary = reconcile(bundle, config)["summary"]
    nav = summary["total_value"]
    if not nav:
        return 0.0
    return sum(
        float(reserves.get(account["account_id"], 0.0)) * account["total_value"] / nav
        for account in summary["accounts"]
    )


def portfolio_covariance(bundle: dict, config: dict, security_ids: list[str]) -> dict:
    """Annualized common-panel covariance, shared with constrained allocation."""
    ids = sorted(set(security_ids) - {"CASH"})
    empty = {
        "ids": ids,
        "matrix": None,
        "observations": 0,
        "weekly_returns": pd.DataFrame(),
        "complete": False,
        "issues": [],
    }
    if not ids:
        return {**empty, "matrix": np.empty((0, 0)), "complete": True}
    prices = _observed(_frame(bundle, "prices"), bundle, "date", config=config)
    metadata = _frame(bundle, "securities")
    base = config.get("mandate", {}).get("base_currency", "USD")
    if prices.empty:
        empty["issues"].append(
            _issue("missing_prices", "Total-return price history unavailable.", "error")
        )
        return empty
    prices = prices[prices.security_id.isin(ids)].copy()
    prices["date"] = pd.to_datetime(prices.date, utc=True).dt.tz_localize(None).dt.normalize()
    prices["adjusted_close"] = pd.to_numeric(prices.adjusted_close, errors="coerce")
    cutoff = pd.Timestamp(bundle["as_of"]).normalize().tz_localize(None)
    prices = prices[
        prices.date
        >= cutoff - pd.DateOffset(years=int(config.get("risk", {}).get("lookback_years", 3)))
    ]
    for sid in ids:
        p = prices[prices.security_id == sid]
        security = metadata[metadata.security_id == sid] if not metadata.empty else metadata
        if (
            p.empty
            or security.empty
            or not p.currency.eq(base).all()
            or not security.currency.eq(base).all()
        ):
            empty["issues"].append(
                _issue(
                    "missing_risk_asset",
                    f"{sid}: complete {base} total-return history and identity required.",
                    "error",
                )
            )
        elif (cutoff - p.date.max()).days > config.get("data", {}).get("max_price_age_days", 7):
            empty["issues"].append(
                _issue("stale_risk_prices", f"{sid}: price history is stale.", "error")
            )
    if prices.duplicated(["date", "security_id"]).any():
        empty["issues"].append(
            _issue(
                "duplicate_prices",
                "Select a single point-in-time price per security/date.",
                "error",
            )
        )
    if empty["issues"]:
        return empty
    prices.loc[prices.adjusted_close <= 0, "adjusted_close"] = np.nan
    panel = prices.pivot(index="date", columns="security_id", values="adjusted_close").reindex(
        columns=ids
    )
    weekly = panel.resample("W-FRI").last().loc[:cutoff]
    returns = (
        weekly.pct_change(fill_method=None).replace([np.inf, -np.inf], np.nan).dropna(how="any")
    )
    n = len(returns)
    if n < int(config.get("risk", {}).get("min_weekly_observations", 104)):
        return {
            **empty,
            "observations": n,
            "issues": [
                _issue(
                    "insufficient_common_history",
                    f"Only {n} aligned weekly returns; no filling or partial covariance substituted.",
                    "error",
                )
            ],
        }
    matrix = LedoitWolf().fit(returns.to_numpy()).covariance_ * 52
    return {
        "ids": ids,
        "matrix": matrix,
        "observations": n,
        "weekly_returns": returns,
        "complete": True,
        "issues": [],
    }


def stress_returns(bundle: dict, config: dict, security_ids: list[str]) -> dict:
    """User-specified mechanical shocks, not forecasts of economic effects."""
    risk = config.get("risk", {})
    securities = _frame(bundle, "securities")
    metadata = (
        securities.drop_duplicates("security_id").set_index("security_id").to_dict("index")
        if not securities.empty
        else {}
    )
    sector_lookup = {}
    if not securities.empty:
        for issuer, group in securities.groupby("issuer_id"):
            values = group.sector.dropna().unique()
            sector_lookup[issuer] = values[0] if len(values) == 1 else None
    growth = {
        "Technology",
        "Information Technology",
        "Communication Services",
        "Consumer Discretionary",
        "Consumer Cyclical",
    }

    def sector_fraction(sid, target):
        sec = metadata.get(sid, {})
        if sec.get("instrument_type") in {"etf", "plan_fund"}:
            fund, issues = _fund_rows(bundle, config, sid)
            if issues or fund.empty or abs(float(fund.weight.sum()) - 1) > 1e-6:
                return None
            if any(
                sector_lookup.get(issuer) is None for issuer in fund.issuer_id if issuer != "CASH"
            ):
                return None
            return sum(
                float(row.weight)
                for row in fund.itertuples()
                if row.issuer_id != "CASH" and sector_lookup.get(row.issuer_id) in target
            )
        if not sec.get("sector"):
            return None
        return float(sec.get("sector") in target)

    def risky_fraction(sid):
        # Explicit fund cash receives zero equity shock. Unknown fund holdings
        # receive the full configured instrument shock, a conservative mechanical
        # stress assumption; this does not classify the unknown exposure as known.
        if metadata.get(sid, {}).get("instrument_type") in {"etf", "plan_fund"}:
            fund, issues = _fund_rows(bundle, config, sid)
            if not issues and not fund.empty:
                cash_weight = float(fund.loc[fund.issuer_id == "CASH", "weight"].sum())
                return 1.0 - cash_weight
        return 1.0

    result = {"broad_equity": {}, "growth_reversal": {}, "rate_repricing": {}}
    for sector in risk.get("stress_sector_shocks", {}):
        result[f"sector_{sector}"] = {}
    for sid in security_ids:
        if sid == "CASH":
            for values in result.values():
                values[sid] = 0.0
            continue
        resolved = sid in metadata
        result["broad_equity"][sid] = (
            float(risk.get("stress_equity_shock", -0.3)) * risky_fraction(sid) if resolved else None
        )
        result["rate_repricing"][sid] = (
            float(risk.get("stress_rates_shock", -0.15)) * risky_fraction(sid) if resolved else None
        )
        fraction = sector_fraction(sid, growth)
        result["growth_reversal"][sid] = (
            float(risk.get("stress_growth_shock", -0.4)) * fraction
            if fraction is not None
            else None
        )
        for sector, shock in risk.get("stress_sector_shocks", {}).items():
            fraction = sector_fraction(sid, {sector})
            result[f"sector_{sector}"][sid] = (
                float(shock) * fraction if fraction is not None else None
            )
    return result


def risk_analysis(bundle: dict, config: dict, weights=None) -> dict:
    global_weights, issues = _global_weights(bundle, config, weights)
    result = {
        "status": "incomplete",
        "issues": issues,
        "annualized_volatility": None,
        "beta": None,
        "tracking_error": None,
        "observations": 0,
        "history": [],
        "risk_contributions": [],
        "stresses": [],
        "max_drawdown": None,
        "history_label": "Hypothetical constant-weight weekly replay; not actual investor performance",
    }
    if not global_weights:
        return result
    result["reserved_cost_weight"] = _reserved_cost_weight(bundle, config, weights)
    if result["reserved_cost_weight"]:
        result["history_label"] += (
            "; original-NAV exposure weights; upfront costs excluded from this risk-only replay"
        )
    stress = stress_returns(bundle, config, list(global_weights))
    for scenario, shocks in stress.items():
        complete = all(shocks.get(sid) is not None for sid in global_weights)
        value = (
            sum(global_weights[sid] * shocks[sid] for sid in global_weights) if complete else None
        )
        result["stresses"].append(
            {
                "scenario": scenario,
                "return": value,
                "status": "complete" if complete else "incomplete",
                "basis": "User-defined mechanical shock; explicit cash including fund cash receives zero; unknown fund exposure receives full broad/rate shock",
            }
        )
    covariance_ids = list(global_weights)
    if (
        isinstance(weights, pd.DataFrame)
        and weights.attrs.get("covariance_security_ids") is not None
    ):
        covariance_ids = sorted(set(covariance_ids) | set(weights.attrs["covariance_security_ids"]))
    covariance = portfolio_covariance(bundle, config, covariance_ids)
    result["issues"].extend(covariance["issues"])
    result["observations"] = covariance["observations"]
    if not covariance["complete"]:
        return _clean(result)
    ids, matrix = covariance["ids"], covariance["matrix"]
    w = np.array([global_weights.get(sid, 0.0) for sid in ids])
    variance = float(w @ matrix @ w) if ids else 0.0
    volatility = math.sqrt(max(0.0, variance))
    result["annualized_volatility"] = volatility
    result["status"] = "complete"
    result["risk_contributions"] = [
        {
            "security_id": sid,
            "contribution": w[i] * (matrix @ w)[i] / volatility if volatility else 0.0,
        }
        for i, sid in enumerate(ids)
        if w[i] > 0
    ]
    result["covariance_security_ids"] = ids
    result["covariance_asset_count"] = len(ids)
    returns = covariance["weekly_returns"]
    if not returns.empty:
        portfolio = returns[ids].to_numpy() @ w
        wealth = np.cumprod(1 + portfolio)
        running_max = np.maximum.accumulate(np.r_[1.0, wealth])[1:]
        result["max_drawdown"] = float(np.min(wealth / running_max - 1))
        keep = np.unique(np.linspace(0, len(wealth) - 1, min(200, len(wealth))).astype(int))
        result["history"] = [
            {"date": returns.index[i].strftime("%Y-%m-%d"), "value": float(wealth[i])} for i in keep
        ]
        # Resample calendar blocks jointly across assets, preserving local serial
        # dependence and cross-asset dependence. This estimates sampling uncertainty
        # in historical volatility, not a confidence band for future wealth.
        samples = int(config.get("risk", {}).get("bootstrap_samples", 300))
        rng = np.random.default_rng(int(config.get("risk", {}).get("seed", 42)))
        block = min(13, len(returns))
        nblocks = math.ceil(len(returns) / block)
        raw = returns[ids].to_numpy()
        estimates = []
        for _ in range(samples):
            starts = rng.integers(0, len(returns) - block + 1, size=nblocks)
            take = (starts[:, None] + np.arange(block)).ravel()[: len(returns)]
            bootcov = LedoitWolf().fit(raw[take]).covariance_ * 52
            estimates.append(math.sqrt(max(0.0, float(w @ bootcov @ w))))
        result["volatility_interval"] = {
            "lower": float(np.quantile(estimates, 0.025)),
            "upper": float(np.quantile(estimates, 0.975)),
            "level": 0.95,
            "samples": samples,
            "block_weeks": block,
            "method": "Moving-block percentile bootstrap of common-panel Ledoit-Wolf historical volatility; not a forecast interval",
        }
    else:
        result["max_drawdown"] = 0.0
    benchmark = config.get("mandate", {}).get("benchmark_id")
    if benchmark:
        comparison = portfolio_covariance(bundle, config, ids + [benchmark])
        if comparison["complete"] and comparison["observations"]:
            frame = comparison["weekly_returns"]
            pr = frame.reindex(columns=ids).to_numpy() @ w
            br = frame[benchmark].to_numpy()
            bv = float(np.var(br, ddof=1))
            result["beta"] = float(np.cov(pr, br, ddof=1)[0, 1] / bv) if bv > 0 else None
            result["tracking_error"] = float(np.std(pr - br, ddof=1) * math.sqrt(52))
            result["benchmark_observations"] = len(br)
            portfolio_wealth, benchmark_wealth = np.cumprod(1 + pr), np.cumprod(1 + br)
            keep = np.unique(np.linspace(0, len(br) - 1, min(200, len(br))).astype(int))
            result["history"] = [
                {
                    "date": frame.index[i].strftime("%Y-%m-%d"),
                    "value": float(portfolio_wealth[i]),
                    "benchmark": float(benchmark_wealth[i]),
                }
                for i in keep
            ]
            result["history_observations"] = len(br)
        else:
            result["issues"].append(
                _issue(
                    "benchmark_risk_unavailable",
                    "Portfolio risk computed; benchmark-relative risk lacks a valid common history.",
                )
            )
    if any(s["status"] != "complete" for s in result["stresses"]):
        result["issues"].append(
            _issue(
                "incomplete_stress_exposure",
                "Some sector/growth stresses lack complete underlying fund exposures.",
            )
        )
    result["worst_stress_return"] = min(
        (s["return"] for s in result["stresses"] if s["return"] is not None), default=None
    )
    # Keep the same assets/weights; change only the observation window.
    from copy import deepcopy

    sensitivity_config = deepcopy(config)
    sensitivity_config.setdefault("risk", {}).update(
        lookback_years=5,
        min_weekly_observations=max(
            208, int(config.get("risk", {}).get("min_weekly_observations", 104))
        ),
    )
    sensitivity = portfolio_covariance(bundle, sensitivity_config, covariance_ids)
    result["five_year_sensitivity"] = {
        "status": "complete" if sensitivity["complete"] else "incomplete",
        "requested_years": 5,
        "observations": sensitivity["observations"],
        "annualized_volatility": None,
        "issues": sensitivity["issues"],
        "scope": "Same held assets and NAV weights; five-year common-panel covariance",
    }
    if sensitivity["complete"]:
        sw = np.array([global_weights.get(sid, 0.0) for sid in sensitivity["ids"]])
        result["five_year_sensitivity"]["annualized_volatility"] = float(
            np.sqrt(max(0.0, sw @ sensitivity["matrix"] @ sw))
        )
    result["covariance_method"] = "Ledoit-Wolf shrinkage, common weekly panel, annualized ×52"
    return _clean(result)


def scenario_analysis(bundle: dict, config: dict, weights=None) -> dict:
    from .allocation import _forecast_panel

    global_weights, issues = _global_weights(bundle, config, weights)
    horizon = int(config.get("allocation", {}).get("horizon_months", 12))
    result = {
        "status": "incomplete",
        "horizon_months": horizon,
        "basis": "subjective",
        "weighted_return": None,
        "benchmark_weighted_return": None,
        "objective_weighted_return": None,
        "benchmark_objective_weighted_return": None,
        "net_weighted_return": None,
        "net_objective_weighted_return": None,
        "scenarios": [],
        "issues": issues,
    }
    if not global_weights:
        return result
    cash_return = _number(config.get("allocation", {}).get("cash_return"))
    cash_weight = global_weights.get("CASH", 0.0)
    cash_complete = cash_weight <= 1e-12 or cash_return is not None
    cash_contribution = cash_weight * (cash_return or 0.0)
    if not cash_complete:
        result["issues"].append(
            _issue(
                "missing_cash_return",
                "Retained cash has no explicit horizon return assumption; asset scenarios remain available but portfolio outcomes do not.",
                "warning",
            )
        )
    benchmark = config.get("mandate", {}).get("benchmark_id")
    ids = sorted((set(global_weights) | ({benchmark} if benchmark else set())) - {"CASH"})
    if not ids:
        use_probabilities = config.get("allocation", {}).get("use_probabilities", True)
        result.update(
            status="complete" if cash_complete else "incomplete",
            cash_return=cash_return,
            weighted_return=cash_return if use_probabilities else None,
            probability_status="complete" if use_probabilities else "unweighted",
            cash_assumption="Explicit horizon cash total return; unavailable until supplied",
        )
        return result
    panel, problems = _forecast_panel(bundle, config, ids, require_probabilities=False)
    result["issues"].extend(problems)
    if panel is None:
        codes = {issue["code"] for issue in problems}
        result["status"] = (
            "incomplete" if codes & {"missing_forecasts", "stale_forecast"} else "invalid"
        )
        if "missing_forecasts" in codes:
            result["issues"].append(
                _issue(
                    "missing_forecast_assets",
                    "Required same-horizon asset or benchmark scenarios are unavailable.",
                    "error",
                )
            )
        return result
    nav = reconcile(bundle, config)["summary"]["total_value"]
    w = np.array([global_weights.get(sid, 0.0) for sid in ids])
    reserved = _reserved_cost_weight(bundle, config, weights)
    benchmark_index = ids.index(benchmark) if benchmark in ids else None
    if panel["weighted_available"] and benchmark_index is not None:
        result["benchmark_weighted_return"] = float(panel["means"][benchmark_index])
        result["benchmark_objective_weighted_return"] = float(
            panel["objective_means"][benchmark_index]
        )
    for k, label in enumerate(panel["names"]):
        outcomes = dict(zip(ids, panel["returns"][k]))
        outcomes["CASH"] = cash_return
        value = float(w @ panel["returns"][k]) + cash_contribution if cash_complete else None
        br = outcomes.get(benchmark)
        result["scenarios"].append(
            {
                "scenario": label,
                "probability": float(panel["probabilities"][k])
                if panel["weighted_available"]
                else None,
                "return": value,
                "benchmark_return": br,
                "active_return": value - br if br is not None and value is not None else None,
                "terminal_value": nav * (1 + value) if value is not None else None,
                "net_return": value - reserved if value is not None else None,
                "net_terminal_value": nav * (1 + value - reserved) if value is not None else None,
                "contributions": [
                    {
                        "security_id": sid,
                        "weight": weight,
                        "return": outcomes[sid],
                        "contribution": weight * outcomes[sid]
                        if outcomes[sid] is not None
                        else None,
                    }
                    for sid, weight in global_weights.items()
                ],
            }
        )
    if panel["weighted_available"] and cash_complete:
        value = float(w @ panel["means"]) + cash_contribution
        objective = float(w @ panel["objective_means"]) + cash_contribution
        result.update(
            weighted_return=value,
            objective_weighted_return=objective,
            net_weighted_return=value - reserved,
            net_objective_weighted_return=objective - reserved,
            benchmark_weighted_return=float(panel["means"][benchmark_index])
            if benchmark_index is not None
            else None,
            benchmark_objective_weighted_return=float(panel["objective_means"][benchmark_index])
            if benchmark_index is not None
            else None,
        )
    result.update(
        status="complete" if cash_complete else "partial",
        cash_return=cash_return,
        cash_return_complete=cash_complete,
        probability_status="complete" if panel["weighted_available"] else "unweighted",
        label="Subjective joint scenarios; weighted means require an explicit common distribution",
        cash_assumption="Cash earns the explicit supplied horizon return; distributions are included in asset horizon returns and never added twice",
        distribution_convention="Asset inputs are horizon total returns including distributions under the supplied reinvestment convention; no additional dividend or buyback yield is added",
        forecast_date=panel["forecast_date"],
        reserved_cost_weight=reserved,
        forecast_shrinkage=config.get("allocation", {}).get("forecast_shrinkage", 0.0),
        objective_label="Scenario mean shrunk toward explicit horizon priors; unshrunk outcomes retained",
    )
    return _clean(result)
