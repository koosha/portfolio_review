"""Proposed model inputs, verified against the valuation engines before they are offered.

Nothing here is a validated forecast: every payload carries `proposal_meta.basis
== "proposed"`, names its consequential assumptions and cites the source of each
number. A proposal is only returned when the matching engine accepts it, so the
workspace never receives inputs that would raise on use. Missing or unusable data
returns None and records a reason; it never becomes a zero.

Input shapes (plain dicts; the market-data adapter produces them):

security: {"security_id", "name", "instrument_type": "equity"|"etf"|..., "equity_type":
    "ordinary_common"|None, "sector": app sector label, "currency"}
estimates: {"security_id", "currency", "eps": {"0q"|"+1q"|"0y"|"+1y": {"avg", "low",
    "high", "year_ago", "analysts", "growth"}}, "revenue": {same periods},
    "price_targets": {...}, "recommendations": {...}, "earnings_dates": [...],
    "ex_dividend_date", "received_at", "source_id", "label"}
ttm: {"security_id", "period_end", "currency", "revenue", "gross_profit",
    "operating_income", "net_income", "income_common", "operating_cash_flow",
    "capex" (positive gross spending), "depreciation", "change_working_capital"
    (investment sign: positive consumes cash), "tax_provision", "pretax_income",
    "stock_compensation", "assets", "assets_begin", "cash", "debt", "preferred",
    "minority_interest", "diluted_shares" (statement units), "source_id", "received_at"}
annual_statements: list of the same shape for annual periods, newest last or any order;
    the most recent period supplies a line the trailing window does not carry.
quote_currency: the listing's own quote currency for `price_major`; the DCF is
    denominated in the statement `currency`, so the two are compared only when they match.
defaults: {"discount_rate", "terminal_growth_rate", "projection_years"}
"""

from __future__ import annotations

import math
from collections.abc import Mapping

METHOD_VERSION = "proposal-template-1"
EPS_PERIOD_BY_HORIZON = {6: "0y", 12: "+1y", 18: "+1y"}
FINANCIAL_SECTORS = {"Financials", "Real Estate"}
DEFAULT_DISCOUNT_RATE = 0.09
DEFAULT_TERMINAL_GROWTH = 0.025
DEFAULT_PROJECTION_YEARS = 5
MAX_REVENUE_GROWTH = 0.30
MAX_TAX_RATE = 0.40
TERMINAL_ROIC_PREMIUM = 0.01
SHARE_FACTORS = (1.0, 1e3, 1e6)
SHARE_TOLERANCE = 0.2
EPS_COHERENCE = "Not a coherent portfolio scenario; low/high analyst range is not a joint state"
DCF_CONVERGENCE = (
    "DCF intrinsic value is not a horizon return until a convergence assumption is chosen"
)
ESTIMATE_LABEL = "External consensus estimate; not an established outcome"


def _refuse(issues, code, security_id, detail):
    if isinstance(issues, list):
        issues.append(
            {"code": code, "severity": "info", "security_id": security_id, "detail": detail}
        )
    return None


def _number(value):
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _currency(value):
    if isinstance(value, str) and len(value) == 3 and value.isalpha() and value.isascii():
        return value.upper()
    return None


def _equity_reason(security):
    if security.get("instrument_type") != "equity":
        return (
            f"instrument_type {security.get('instrument_type')!r} is not an ordinary common "
            "equity listing; the per-share earnings model does not apply"
        )
    equity_type = security.get("equity_type")
    if equity_type is not None and equity_type != "ordinary_common":
        return (
            f"equity_type {equity_type!r} is not ordinary common equity; the per-share "
            "earnings model does not apply"
        )
    return None


def _estimate_block(estimates, family, period):
    if not isinstance(estimates, Mapping):
        return None
    block = estimates.get(family)
    if not isinstance(block, Mapping):
        return None
    row = block.get(period)
    return row if isinstance(row, Mapping) else None


def propose_eps_model(
    security: Mapping,
    *,
    price_major,
    quote_currency,
    estimates,
    ttm,
    dividends_ttm_per_share,
    horizon_months,
    dividends_source_id: str | None = None,
    issues: list | None = None,
) -> dict | None:
    """Propose a forward P/E payload for `valuation.calculate_eps`, or None with a reason.

    Scenarios take the low/average/high consensus EPS of the +1y period (12 and 18
    months) or the 0y period (6 months). The multiple is today's price over the
    average forward EPS, held constant for all three scenarios: the terminal
    multiple is an assumption, named in `proposal_meta.assumptions_visible`.
    Distributions are trailing cash dividends per starting share, without
    reinvestment. Reasons are appended to `issues` when a list is supplied.
    """
    security = security if isinstance(security, Mapping) else {}
    sid = security.get("security_id")
    code = "eps_proposal_unavailable"
    reason = _equity_reason(security)
    if reason:
        return _refuse(issues, code, sid, reason)
    if horizon_months not in EPS_PERIOD_BY_HORIZON:
        return _refuse(issues, code, sid, f"horizon_months {horizon_months!r} must be 6, 12 or 18")
    currency = _currency(quote_currency)
    if currency is None:
        return _refuse(issues, code, sid, f"quote currency {quote_currency!r} is not a code")
    price = _number(price_major)
    if price is None or price <= 0:
        return _refuse(issues, code, sid, "a positive quoted price in major units is required")
    estimate_currency = (
        _currency(estimates.get("currency")) if isinstance(estimates, Mapping) else None
    )
    if estimate_currency is not None and estimate_currency != currency:
        return _refuse(
            issues,
            code,
            sid,
            f"consensus EPS is quoted in {estimate_currency} while the price is in {currency}; "
            "a verified conversion is required",
        )
    period = EPS_PERIOD_BY_HORIZON[horizon_months]
    block = _estimate_block(estimates, "eps", period)
    if block is None:
        return _refuse(issues, code, sid, f"consensus EPS for {period} is unavailable")
    values = {key: _number(block.get(key)) for key in ("avg", "low", "high")}
    missing = sorted(key for key, value in values.items() if value is None)
    if missing:
        return _refuse(issues, code, sid, f"consensus EPS {period} is missing {', '.join(missing)}")
    if values["avg"] == 0:
        return _refuse(issues, code, sid, f"consensus EPS {period} average is zero; no multiple")
    multiple = price / values["avg"]
    distributions = _number(dividends_ttm_per_share)
    estimate_source = estimates.get("source_id") if isinstance(estimates, Mapping) else None
    trailing = ttm if isinstance(ttm, Mapping) else {}
    payload = {
        "security_id": sid,
        "starting_price": price,
        "currency": currency,
        "horizon_months": int(horizon_months),
        "eps_convention": "forward",
        "pe_convention": "forward",
        "scenarios": [
            {
                "label": label,
                "eps": values[key],
                "pe": multiple,
                "distributions_per_starting_share": distributions,
            }
            for label, key in (("adverse", "low"), ("central", "avg"), ("favorable", "high"))
        ],
    }
    from portfolio_research import valuation

    try:
        verified = valuation.calculate_eps(payload)
    except ValueError as exc:
        return _refuse(issues, code, sid, str(exc))
    if verified["status"] == "blocked":
        detail = "; ".join(verified["issues"]) or "the EPS engine could not value any scenario"
        return _refuse(issues, code, sid, detail)
    central = next(row for row in verified["scenarios"] if row["label"] == "central")
    payload["proposal_meta"] = {
        "basis": "proposed",
        "method_version": METHOD_VERSION,
        "model": "eps_multiple",
        "estimate_period": period,
        "estimate_label": ESTIMATE_LABEL,
        "evidence": {
            key: source
            for key, source in (
                ("eps_adverse", estimate_source),
                ("eps_central", estimate_source),
                ("eps_favorable", estimate_source),
                ("pe", estimate_source),
                ("starting_price", security.get("price_source_id")),
                (
                    "distributions_per_starting_share",
                    dividends_source_id or trailing.get("source_id"),
                ),
            )
            if source is not None
        },
        "assumptions_visible": ["eps_growth", "terminal_multiple"],
        "coherence": EPS_COHERENCE,
        "notes": [
            f"forward P/E of {multiple:.2f} is today's price over average forward EPS, "
            "held constant across scenarios",
            "distributions are trailing cash dividends per starting share, without reinvestment",
        ],
        "trailing": {
            "period_end": trailing.get("period_end"),
            "income_common": _number(trailing.get("income_common")),
        },
        "verification": {
            "status": verified["status"],
            "method_version": verified["method_version"],
            "central_total_return": central["total_return"],
            "issues": list(verified["issues"]),
        },
    }
    return payload


def _latest_annual(annual_statements):
    rows = [row for row in annual_statements or [] if isinstance(row, Mapping)]
    if not rows:
        return {}
    return max(rows, key=lambda row: str(row.get("period_end") or ""))


def _line(field, ttm, annual, evidence, notes):
    """Trailing value for `field`, falling back to the latest annual period once."""
    value = _number(ttm.get(field))
    if value is not None:
        evidence[field] = ttm.get("source_id")
        return value
    value = _number(annual.get(field))
    if value is not None:
        evidence[field] = annual.get("source_id")
        notes.append(
            f"{field}: trailing line absent; the annual period ending "
            f"{annual.get('period_end')} was used"
        )
    return value


def _bridge_line(field, ttm, annual, evidence, notes):
    value = _line(field, ttm, annual, evidence, notes)
    if value is None:
        notes.append(f"{field}: line absent; explicit zero proposed")
        return 0
    return value


def _share_factor(statement_shares, listing_shares):
    """Reconcile statement and listing share counts, or refuse with the reason.

    `statements.share_units` performs the detection when that module is present;
    an unresolved scale is refused rather than guessed, because a thousand-scaled
    share count silently misprices every per-share result.
    """
    if statement_shares is None or statement_shares <= 0:
        return None, "the statement diluted share count is unavailable"
    detected = None
    try:
        from portfolio_research import statements as statements_module

        detected = statements_module.share_units(statement_shares, listing_shares)
    except (ImportError, AttributeError, TypeError, ValueError):
        detected = None
    if isinstance(detected, Mapping):
        factor = _number(detected.get("factor"))
        if factor:
            return factor, str(detected.get("basis") or "share_units")
        reason = detected.get("reason")
        if isinstance(reason, str) and reason:
            return None, reason
    if listing_shares is None or listing_shares <= 0:
        return None, "listing shares outstanding are unavailable; the share scale is unchecked"
    ratio = listing_shares / statement_shares
    factor = min(SHARE_FACTORS, key=lambda candidate: abs(math.log(ratio / candidate)))
    if abs(ratio / factor - 1) > SHARE_TOLERANCE:
        return None, (
            f"statement diluted shares ({statement_shares:,.0f}) and listing shares "
            f"({listing_shares:,.0f}) differ by {ratio:,.2f}x; the share scale is unresolved"
        )
    basis = "shares" if factor == 1.0 else f"statement_units_x{factor:,.0f}"
    return factor, basis


def propose_dcf_inputs(
    security: Mapping,
    *,
    ttm,
    annual_statements,
    shares_outstanding,
    price_major,
    currency,
    estimates,
    defaults,
    quote_currency=None,
    issues: list | None = None,
) -> dict | None:
    """Propose an FCFF payload for `valuation.calculate_dcf`, or None with a reason.

    Revenue grows at the consensus +1y rate (capped at ±30%) decaying linearly to
    the terminal rate; operating margin, tax rate, depreciation, capex and working
    capital hold their trailing share of revenue. Beginning invested capital is
    proxied by total assets less cash and rolls forward EXACTLY by the engine's own
    reinvestment (capex − depreciation + change in working capital). The payload is
    verified by calling the engine; a ValueError becomes a reason, never a raise.
    """
    security = security if isinstance(security, Mapping) else {}
    sid = security.get("security_id")
    code = "dcf_proposal_unavailable"
    reason = _equity_reason(security)
    if reason:
        return _refuse(issues, code, sid, reason)
    sector = security.get("sector")
    if sector in FINANCIAL_SECTORS:
        return _refuse(
            issues,
            code,
            sid,
            f"sector {sector} requires a financial-company framework; this FCFF model covers "
            "nonfinancial operating companies only",
        )
    unit_currency = _currency(currency)
    if unit_currency is None:
        return _refuse(issues, code, sid, f"statement currency {currency!r} is not a code")
    trailing = ttm if isinstance(ttm, Mapping) else {}
    annual = _latest_annual(annual_statements)
    if not trailing:
        return _refuse(issues, code, sid, "trailing twelve-month statements are unavailable")
    evidence, notes = {}, []
    revenue_now = _line("revenue", trailing, annual, evidence, notes)
    ebit_now = _line("operating_income", trailing, annual, evidence, notes)
    depreciation = _line("depreciation", trailing, annual, evidence, notes)
    capex = _line("capex", trailing, annual, evidence, notes)
    working_capital = _line("change_working_capital", trailing, annual, evidence, notes)
    tax_provision = _line("tax_provision", trailing, annual, evidence, notes)
    pretax = _line("pretax_income", trailing, annual, evidence, notes)
    assets = _line("assets", trailing, annual, evidence, notes)
    cash = _line("cash", trailing, annual, evidence, notes)
    required = {
        "revenue": revenue_now,
        "operating_income": ebit_now,
        "depreciation": depreciation,
        "capex": capex,
        "change_working_capital": working_capital,
        "assets": assets,
        "cash": cash,
    }
    absent = sorted(field for field, value in required.items() if value is None)
    if absent:
        return _refuse(issues, code, sid, f"trailing statements are missing {', '.join(absent)}")
    if revenue_now <= 0:
        return _refuse(issues, code, sid, "trailing revenue is not positive; no margin basis")
    if capex < 0:
        return _refuse(
            issues,
            code,
            sid,
            "trailing capex is negative; gross capital spending is not inferred from a "
            "negative cash-flow value",
        )
    if depreciation < 0:
        return _refuse(issues, code, sid, "trailing depreciation is negative; review the source")
    if tax_provision is None or pretax is None or pretax == 0:
        return _refuse(
            issues,
            code,
            sid,
            "an effective tax rate needs a trailing tax provision and pretax income",
        )
    tax_rate = min(max(tax_provision / pretax, 0.0), MAX_TAX_RATE)
    stock_compensation = _bridge_line("stock_compensation", trailing, annual, evidence, notes)
    debt = _bridge_line("debt", trailing, annual, evidence, notes)
    preferred = _bridge_line("preferred", trailing, annual, evidence, notes)
    nci = _bridge_line("minority_interest", trailing, annual, evidence, notes)
    settings = defaults if isinstance(defaults, Mapping) else {}
    discount_rate = _number(settings.get("discount_rate")) or DEFAULT_DISCOUNT_RATE
    terminal_growth = _number(settings.get("terminal_growth_rate"))
    terminal_growth = DEFAULT_TERMINAL_GROWTH if terminal_growth is None else terminal_growth
    years = settings.get("projection_years") or DEFAULT_PROJECTION_YEARS
    if not isinstance(years, int) or isinstance(years, bool) or not 2 <= years <= 15:
        return _refuse(issues, code, sid, f"projection_years {years!r} must be between 2 and 15")
    growth_block = _estimate_block(estimates, "revenue", "+1y")
    growth = _number((growth_block or {}).get("growth"))
    if growth is None and growth_block is not None:
        consensus_revenue = _number(growth_block.get("avg"))
        growth = consensus_revenue / revenue_now - 1 if consensus_revenue is not None else None
        if growth is not None:
            notes.append("revenue growth derived from the consensus +1y revenue level")
    if growth is None:
        return _refuse(issues, code, sid, "consensus revenue growth for +1y is unavailable")
    growth = max(-MAX_REVENUE_GROWTH, min(MAX_REVENUE_GROWTH, growth))
    evidence["revenue_growth"] = (
        estimates.get("source_id") if isinstance(estimates, Mapping) else None
    )
    margin = ebit_now / revenue_now
    capex_ratio = capex / revenue_now
    depreciation_ratio = depreciation / revenue_now
    working_capital_ratio = working_capital / revenue_now
    sbc_ratio = stock_compensation / revenue_now
    statement_shares = _number(trailing.get("diluted_shares"))
    factor, share_basis = _share_factor(statement_shares, _number(shares_outstanding))
    if factor is None:
        return _refuse(issues, code, sid, share_basis)
    diluted_shares = statement_shares * factor
    invested_capital = assets - cash
    notes.append("invested_capital[1] proxied by total assets less cash")
    notes.append("excess_cash: all cash treated as excess")
    notes.append("nonoperating_assets: line absent; explicit zero proposed")
    notes.append("capex, depreciation and working capital hold their trailing share of revenue")
    projections, revenue, capital, growth_path = [], revenue_now, invested_capital, []
    for year in range(1, years + 1):
        step = growth + (terminal_growth - growth) * (year - 1) / (years - 1)
        growth_path.append(step)
        revenue = revenue * (1 + step)
        year_capex = revenue * capex_ratio
        year_depreciation = revenue * depreciation_ratio
        year_working_capital = revenue * working_capital_ratio
        projections.append(
            {
                "year": year,
                "revenue": revenue,
                "ebit": revenue * margin,
                "tax_rate": tax_rate,
                "depreciation": year_depreciation,
                "capex": year_capex,
                "change_working_capital": year_working_capital,
                "invested_capital": capital,
                "stock_compensation": revenue * sbc_ratio,
            }
        )
        capital = capital + year_capex - year_depreciation + year_working_capital
    trailing_nopat = ebit_now * (1 - tax_rate)
    terminal_roic = max(
        trailing_nopat / invested_capital if invested_capital > 0 else 0.0,
        terminal_growth + TERMINAL_ROIC_PREMIUM,
    )
    payload = {
        "security_id": sid,
        "currency": unit_currency,
        "monetary_unit": "units",
        "company_type": "nonfinancial",
        "sbc_treatment": "expensed_in_ebit",
        "discount_rate": discount_rate,
        "terminal_growth_rate": terminal_growth,
        "terminal_roic": terminal_roic,
        "debt": debt,
        "preferred": preferred,
        "nci": nci,
        "excess_cash": cash,
        "nonoperating_assets": 0,
        "diluted_shares": diluted_shares,
        "projections": projections,
    }
    from portfolio_research import valuation

    try:
        verified = valuation.calculate_dcf(payload)
    except ValueError as exc:
        return _refuse(issues, code, sid, str(exc))
    price = _number(price_major)
    quote = _currency(quote_currency)
    value_per_share = verified["value_per_share"]
    # `value_per_share` is denominated in the statement currency. A quote in another
    # currency is a different unit, so the two are never divided; the change is simply
    # unavailable until a verified conversion exists.
    comparable = quote is not None and quote == unit_currency
    if not comparable and price is not None:
        notes.append(
            f"implied change against the quoted price is unavailable: the statements are "
            f"in {unit_currency} and the quote is in {quote or 'an unstated currency'}"
        )
    payload["proposal_meta"] = {
        "basis": "proposed",
        "method_version": METHOD_VERSION,
        "model": "fcff_dcf",
        "evidence": {key: value for key, value in evidence.items() if value is not None},
        "assumptions_visible": [
            "discount_rate",
            "terminal_growth_rate",
            "margins",
            "reinvestment",
            "terminal_roic",
        ],
        "convergence": DCF_CONVERGENCE,
        "estimate_label": ESTIMATE_LABEL,
        "notes": notes,
        "inputs": {
            "trailing_period_end": trailing.get("period_end"),
            "ebit_margin": margin,
            "tax_rate": tax_rate,
            "revenue_growth_path": growth_path,
            "capex_ratio": capex_ratio,
            "depreciation_ratio": depreciation_ratio,
            "working_capital_ratio": working_capital_ratio,
            "share_factor": factor,
            "share_basis": share_basis,
        },
        "verification": {
            "status": verified["status"],
            "method_version": verified["method_version"],
            "value_per_share": value_per_share,
            "issues": list(verified["issues"]),
        },
        "price_major": price,
        "price_currency": quote,
        "implied_change_vs_price": (
            value_per_share / price - 1
            if comparable and price and value_per_share is not None
            else None
        ),
    }
    return payload
