"""Editable, conditional holding views derived solely from a saved review's evidence.

Company defaults are draft assumptions, never calibrated forecasts. They are evaluated
beside the joint allocation scenarios; they do not bypass that engine's reviewed-thesis,
funding, tax, or account gates. Manual workspace valuations always take precedence.
"""

from __future__ import annotations

import math
from copy import deepcopy

import pandas as pd

from portfolio_lab.analytics import json_safe
from portfolio_lab.metrics import _frame, _observed

from .scenarios import DEFAULT_SHARED_STATE, _scale
from .statements import share_units, split_adjust_dividends
from .valuation import LABELS, calculate_dcf, calculate_eps
from .valuation_basis import estimate_currency, listing_ratio, translate_per_share

METHOD_VERSION = "holding-scenarios-2"
MAX_GROWTH = 0.30
EARNINGS_SENSITIVITY = 0.5
MULTIPLE_SENSITIVITY = 0.5
GAP_STEPS = {
    "A dated price with a verified quote currency is required.": (
        "price_missing",
        "Refresh price data",
        "data",
    ),
    "Refresh the stale price before comparing scenarios.": (
        "price_stale",
        "Refresh price data",
        "data",
    ),
    "Refresh the stale financial statements before valuing earnings.": (
        "statements_stale",
        "Refresh company data",
        "data",
    ),
    "Statement EPS and the quoted price use different currencies.": (
        "currency_mismatch",
        "Check currencies",
        "data",
    ),
    "Statement EPS has no verified ordinary-share or listed-share basis.": (
        "share_basis_missing",
        "Check EPS share units",
        "data",
    ),
    "The quote source has conflicting price currencies.": (
        "quote_currency_conflict",
        "Refresh price data",
        "data",
    ),
    "No comparable statement EPS or currency-matched consensus is available.": (
        "earnings_missing",
        "Refresh company data",
        "data",
    ),
    "Positive, comparable earnings are required for a P/E scenario.": (
        "earnings_not_applicable",
        "Choose another model",
        "research",
    ),
    "Retained earnings and price sources are required.": (
        "sources_missing",
        "Refresh company data",
        "data",
    ),
    "Resolve this holding's listing and issuer identity before valuing it.": (
        "identity_unresolved",
        "Resolve holding",
        "data",
    ),
    "A company earnings model does not apply to this instrument.": (
        "instrument_not_applicable",
        "Set instrument scenarios",
        "scenarios",
    ),
    "Company models support 6, 12, or 18 month horizons.": (
        "horizon_unsupported",
        "Change the horizon",
        "settings",
    ),
    "Company defaults require Adverse, Central, and Favorable shared states.": (
        "states_missing",
        "Set shared states",
        "scenarios",
    ),
    "Update this company model to the selected analysis horizon.": (
        "horizon_mismatch",
        "Update model horizon",
        "research",
    ),
    "Choose the exact saved security for this ambiguous benchmark ticker.": (
        "benchmark_ambiguous",
        "Choose exact benchmark",
        "settings",
    ),
    "Resolve the benchmark identity and quote currency.": (
        "benchmark_identity_missing",
        "Check benchmark",
        "settings",
    ),
    "Choose VOO, VTI or a saved security; this benchmark is not available in the review.": (
        "benchmark_unknown",
        "Choose a benchmark",
        "settings",
    ),
}


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (ValueError, TypeError):
        return None
    return value if math.isfinite(value) else None


def _currency(value):
    return value.upper() if isinstance(value, str) and len(value) == 3 else None


def _sources(*records):
    return sorted({str(row["source_id"]) for row in records if row and row.get("source_id")})


def _eligible(rows, bundle, config, date_column="period_end"):
    frame = pd.DataFrame(rows or [])
    eligible = _observed(frame, bundle, date_column, config=config)
    if (
        (bundle.get("timeline") or {}).get("review_kind") == "current"
        and not eligible.empty
        and "received_at" in eligible
    ):
        from .calendar import current_receipt_limit

        received = pd.to_datetime(
            eligible["received_at"], utc=True, errors="coerce", format="mixed"
        )
        eligible = eligible.loc[received.notna() & received.le(current_receipt_limit(bundle))]
    if (bundle.get("timeline") or {}).get("review_kind") != "current" and not eligible.empty:
        from .calendar import bundle_cutoff, new_york_dates

        cutoff = bundle_cutoff(bundle)
        # Today's normalized statements/prices are not historical vintages. Original
        # filing reconstruction is permitted only when that provenance is explicit.
        known = pd.Series(False, index=eligible.index)
        if "received_at" in eligible:
            received = pd.to_datetime(
                eligible["received_at"], utc=True, errors="coerce", format="mixed"
            )
            known = new_york_dates(eligible["received_at"], received).le(cutoff)
        original = eligible.apply(
            lambda row: (
                row.get("source_policy") == "original_filing" and bool(row.get("accession"))
            ),
            axis=1,
        )
        eligible = eligible.loc[known | original]
    return eligible.to_dict("records")


def _latest(rows):
    return (
        max(
            rows,
            key=lambda row: (str(row.get("period_end") or ""), str(row.get("received_at") or "")),
        )
        if rows
        else {}
    )


def _estimate(record, bundle, config):
    """Consensus is receipt-dated; a present-day answer is never an old forecast."""
    if not isinstance(record, dict) or not record.get("received_at"):
        return None
    if record.get("unit_contract") == "legacy_unverified" or (
        str(record.get("source_id") or "").startswith("yahoo_estimates:")
        and record.get("unit_contract") != "period-currencies-2"
    ):
        return None
    from .calendar import bundle_cutoff, current_receipt_limit

    stamp = pd.to_datetime(record["received_at"], utc=True, errors="coerce")
    cutoff = bundle_cutoff(bundle)
    # Current reviews retain the adapter answers fetched during that operation.
    # Historical reviews require a genuinely pre-cutoff consensus receipt.
    current = (bundle.get("timeline") or {}).get("review_kind") == "current"
    if pd.isna(stamp) or (stamp > cutoff and not current):
        return None
    # Fetching can finish after generation, but a date beyond the operation's
    # bounded provider window is not evidence acquired for this current run.
    if current and stamp > current_receipt_limit(bundle):
        return None
    if config.get("data", {}).get("require_received_by_cutoff") and stamp > cutoff:
        return None
    age = (cutoff - stamp).days
    return record if age <= config.get("data", {}).get("max_forecast_age_days", 45) else None


def _model_facts(sid, security, bundle, config):
    inputs = (bundle.get("research_inputs") or {}).get(sid) or {}
    facts = inputs.get("valuation_facts") or {}
    statement_record = facts.get("statements") or {}
    trailing = statement_record.get("ttm") or {}
    quarters = _eligible(statement_record.get("quarterly"), bundle, config)
    annual = _eligible(statement_record.get("annual"), bundle, config)
    retained = _eligible([trailing] if trailing else [], bundle, config)
    frame = _observed(_frame(bundle, "fundamentals"), bundle, "period_end", config=config)
    if not frame.empty and "security_id" in frame:
        rows = _eligible(
            frame[frame.security_id.astype(str).eq(sid)].to_dict("records"), bundle, config
        )
        # SEC/native structured observations win over a provider copy when supplied.
        retained.extend(row for row in rows if row.get("period_type") == "ttm")
        annual.extend(row for row in rows if row.get("period_type") in {"annual", None})
    trailing = _latest(retained) or _latest(annual)
    prices = facts.get("prices") or {}
    local = _eligible(prices.get("prices"), bundle, config, "date")
    price_row = max(local, key=lambda row: str(row.get("date") or "")) if local else {}
    if not price_row:
        frame = _observed(_frame(bundle, "prices"), bundle, "date", config=config)
        if not frame.empty and "security_id" in frame:
            rows = frame[frame.security_id.astype(str).eq(sid)].sort_values("date")
            if not rows.empty:
                candidate = rows.iloc[-1].to_dict()
                # A converted close cannot be divided by unconverted statement EPS.
                if _currency(candidate.get("currency")) == _currency(security.get("currency")):
                    price_row = candidate
    currency = price_row.get("currency") or prices.get("currency") or security.get("currency")
    price_currency_issue = bool(
        prices.get("currency")
        and price_row.get("currency")
        and prices["currency"] != price_row["currency"]
    )
    profile = facts.get("profile") or security
    if profile.get("received_at"):
        from .calendar import current_receipt_limit

        stamp = pd.to_datetime(profile.get("received_at"), utc=True, errors="coerce")
        limit = current_receipt_limit(bundle)
        if pd.isna(stamp) or stamp > limit:
            profile = {}
    return {
        "trailing": trailing,
        "annual": annual,
        "quarters": quarters,
        "price": price_row,
        "currency": _currency(currency),
        "price_currency_issue": price_currency_issue,
        "statement_currency": _currency(
            statement_record.get("currency") or trailing.get("currency")
        ),
        "actions": [
            row
            for row in prices.get("actions") or []
            if str(row.get("date") or "") <= str(bundle.get("as_of") or "")
        ],
        "profile": profile,
        "estimates": _estimate(inputs.get("estimates"), bundle, config),
        "listing_basis": facts.get("listing_basis") or security.get("listing_basis"),
        "fx_observations": [
            *(bundle.get("fx_observations") or []),
            *(facts.get("fx_observations") or []),
        ],
    }


def _trailing_eps(facts):
    trailing = facts["trailing"]
    if not trailing:
        return None, None, []
    quarters = sorted(facts["quarters"], key=lambda row: str(row.get("period_end")), reverse=True)
    window = str(trailing.get("ttm_quarters") or "").split(",")
    quarters = [row for row in quarters if row.get("period_end") in window]
    values = [_number(row.get("diluted_eps")) for row in quarters]
    if len(quarters) == 4 and all(value is not None for value in values):
        periods = [pd.Timestamp(row["period_end"]) for row in quarters]
        if len(set(periods)) != 4 or any(
            not 60 <= (newer - older).days <= 100 for newer, older in zip(periods, periods[1:])
        ):
            facts["earnings_issue"] = (
                "Statement EPS requires four distinct, nonoverlapping fiscal quarters."
            )
            return None, None, []
        currencies = {row.get("currency") for row in quarters}
        bases = {row.get("per_share_basis") for row in quarters}
        if len(currencies) != 1 or None in currencies or len(bases) != 1:
            facts["earnings_issue"] = "Quarterly EPS has inconsistent currencies or share units."
            return None, None, []
        facts["earnings_per_share_basis"] = next(iter(bases))
        facts["earnings_currency"] = next(iter(currencies))
        # Financial statements often already restate comparative EPS for a split.
        # Unlike paid dividends, that basis cannot be inferred from a price action.
        splits = [row for row in facts["actions"] if row.get("kind") == "split"]
        relevant = [
            row
            for row in splits
            if str(row.get("date") or "") > min(str(quarter["period_end"]) for quarter in quarters)
        ]
        share_basis = trailing.get("eps_share_basis")
        if relevant and share_basis not in {"current_share_terms", "original_share_terms"}:
            facts["earnings_issue"] = (
                "Statement EPS spans a split with an undeclared adjustment basis."
            )
            return None, None, []
        applied_splits = relevant if share_basis == "original_share_terms" else []
        adjusted = split_adjust_dividends(
            [{"date": row["period_end"], "value": row["diluted_eps"]} for row in quarters],
            applied_splits,
        )
        return (
            sum(row["value"] for row in adjusted),
            "four_quarter_diluted_eps",
            _sources(*quarters),
        )
    if trailing.get("period_type") == "annual" and _number(trailing.get("diluted_eps")) is not None:
        facts["earnings_per_share_basis"] = trailing.get("per_share_basis")
        facts["earnings_currency"] = trailing.get("currency") or facts["statement_currency"]
        splits = [
            row
            for row in facts["actions"]
            if row.get("kind") == "split"
            and str(row.get("date") or "") > str(trailing.get("period_end"))
        ]
        share_basis = trailing.get("eps_share_basis")
        if splits and share_basis not in {"current_share_terms", "original_share_terms"}:
            facts["earnings_issue"] = (
                "Annual EPS precedes a split with an undeclared adjustment basis."
            )
            return None, None, []
        adjusted = split_adjust_dividends(
            [{"date": trailing["period_end"], "value": trailing["diluted_eps"]}],
            splits if share_basis == "original_share_terms" else [],
        )
        return adjusted[0]["value"], "annual_diluted_eps", _sources(trailing)
    income = _number(trailing.get("income_common"))
    statement_shares = _number(trailing.get("diluted_shares"))
    profile = facts["profile"]
    listing_shares = _number(profile.get("shares_outstanding"))
    units = share_units(statement_shares, listing_shares)
    if trailing.get("share_count_basis") == "ordinary_share" and facts.get("listing_ratio"):
        units = share_units(statement_shares / facts["listing_ratio"], listing_shares)
    if income is not None and units["factor"]:
        facts["earnings_per_share_basis"] = trailing.get("share_count_basis")
        facts["earnings_currency"] = trailing.get("currency") or facts["statement_currency"]
        return (
            income / (statement_shares * units["factor"]),
            "common_earnings_per_diluted_share",
            _sources(trailing, profile),
        )
    return None, None, []


def _growth(facts):
    estimates = facts["estimates"] or {}
    periods = estimates.get("eps") or {}
    current = _number((periods.get("0y") or {}).get("avg"))
    next_year = _number((periods.get("+1y") or {}).get("avg"))
    current_currency = estimate_currency(estimates, "0y")
    next_currency = estimate_currency(estimates, "+1y")
    current_basis = (periods.get("0y") or {}).get("per_share_basis")
    next_basis = (periods.get("+1y") or {}).get("per_share_basis")
    if (
        current is not None
        and current > 0
        and next_year is not None
        and next_year > 0
        and current_currency is not None
        and current_currency == next_currency
        and current_basis == next_basis
    ):
        return (
            min(MAX_GROWTH, max(-MAX_GROWTH, next_year / current - 1)),
            "consensus_year_to_year",
            _sources(estimates),
        )
    annual = sorted(facts["annual"], key=lambda row: str(row.get("period_end")), reverse=True)
    if len(annual) >= 2:
        first, second = annual[:2]
        dates = [pd.Timestamp(row["period_end"]) for row in (first, second)]
        before, now = _number(second.get("income_common")), _number(first.get("income_common"))
        if (
            before is not None
            and before > 0
            and now is not None
            and now > 0
            and _currency(first.get("currency")) is not None
            and _currency(first.get("currency")) == _currency(second.get("currency"))
            and 330 <= (dates[0] - dates[1]).days <= 400
        ):
            return (
                min(MAX_GROWTH, max(-MAX_GROWTH, now / before - 1)),
                "reported_annual_common_earnings_growth",
                _sources(first, second),
            )
    return 0.0, "flat_earnings_assumption", []


def automatic_eps(sid, security, bundle, config):
    """Draft three scenarios from retained statements/consensus and shared shocks."""
    if security.get("instrument_type") != "equity" or security.get("equity_type") not in (
        None,
        "ordinary_common",
        "depositary_receipt",
    ):
        return None, "A company earnings model does not apply to this instrument."
    if security.get("resolution_status") in {"unresolved", "conflict"}:
        return None, "Resolve this holding's listing and issuer identity before valuing it."
    facts = _model_facts(sid, security, bundle, config)
    price = _number(facts["price"].get("close"))
    currency = facts["currency"]
    if not price or price <= 0 or not currency:
        return None, "A dated price with a verified quote currency is required."
    from .calendar import bundle_cutoff, current_receipt_limit

    cutoff = bundle_cutoff(bundle)
    if facts["price_currency_issue"]:
        return None, "The quote source has conflicting price currencies."
    price_date = pd.Timestamp(facts["price"].get("date"))
    if (cutoff.date() - price_date.date()).days > config["data"].get("max_price_age_days", 4):
        return None, "Refresh the stale price before comparing scenarios."
    basis_context = {
        "as_of": bundle["as_of"],
        "cutoff": cutoff,
        "current": (bundle.get("timeline") or {}).get("review_kind") == "current",
        "require_received": config["data"].get("require_received_by_cutoff", False),
        "receipt_limit": current_receipt_limit(bundle),
    }
    ratio, _ = listing_ratio(facts["listing_basis"], **basis_context)
    facts["listing_ratio"] = ratio
    starting_eps, eps_basis, eps_sources = _trailing_eps(facts)
    convention = "trailing"
    earnings_bridge = None
    domicile = facts["profile"].get("domicile") or security.get("domicile")
    requires_share_basis = bool(
        security.get("depositary_receipt") is True
        or facts["profile"].get("depositary_receipt") is True
        or security.get("equity_type") == "depositary_receipt"
        or (ratio is not None and ratio != 1)
        or (currency == "USD" and domicile and domicile != "US")
    )
    if starting_eps is not None:
        earnings_bridge = translate_per_share(
            starting_eps,
            from_currency=facts.get("earnings_currency") or facts["statement_currency"],
            quote_currency=currency,
            per_share_basis=facts.get("earnings_per_share_basis"),
            listing_basis=facts["listing_basis"],
            fx_observations=facts["fx_observations"],
            max_fx_age_days=config["data"].get("max_fx_age_days", 7),
            same_currency_listing_contract=not requires_share_basis,
            **basis_context,
        )
        if earnings_bridge["value"] is None:
            facts["earnings_issue"] = earnings_bridge["reason"]
            starting_eps = None
        else:
            starting_eps = earnings_bridge["value"]
            if earnings_bridge.get("share_ratio_source_id"):
                eps_sources = sorted({*eps_sources, earnings_bridge["share_ratio_source_id"]})
            if (earnings_bridge.get("fx") or {}).get("source_id"):
                eps_sources = sorted({*eps_sources, earnings_bridge["fx"]["source_id"]})
    if starting_eps is not None:
        end = pd.Timestamp(facts["trailing"]["period_end"])
        if (cutoff.date() - end.date()).days > config["data"].get("max_fundamental_age_days", 150):
            return None, "Refresh the stale financial statements before valuing earnings."
    else:
        estimates = facts["estimates"] or {}
        consensus = (estimates.get("eps") or {}).get("0y") or {}
        if estimate_currency(estimates, "0y") != currency:
            return None, facts.get("earnings_issue") or (
                "No comparable statement EPS or currency-matched consensus is available."
            )
        # This is the retained listing-estimate contract, explicitly distinct from
        # validating an ADR ratio. A contrary declared ordinary-share basis must pass
        # the same sourced share bridge as a filing value.
        consensus_bridge = translate_per_share(
            consensus.get("avg"),
            from_currency=estimate_currency(estimates, "0y"),
            quote_currency=currency,
            per_share_basis=consensus.get("per_share_basis") or estimates.get("per_share_basis"),
            listing_basis=facts["listing_basis"],
            same_currency_listing_contract=True,
            **basis_context,
        )
        if consensus_bridge["value"] is None:
            return None, consensus_bridge["reason"]
        starting_eps = consensus_bridge["value"]
        earnings_bridge = consensus_bridge
        eps_basis, eps_sources, convention = (
            "current_fiscal_consensus_eps",
            _sources(estimates),
            "forward",
        )
    if starting_eps is None or starting_eps <= 0:
        return None, "Positive, comparable earnings are required for a P/E scenario."
    if not eps_sources or not facts["price"].get("source_id"):
        return None, "Retained earnings and price sources are required."
    growth, growth_basis, growth_sources = _growth(facts)
    if facts["estimates"] and estimate_currency(facts["estimates"], "0y") != currency:
        # Growth ratios in another currency still mix accounting/listing assumptions;
        # prefer the observed issuer trend instead of silently using that consensus.
        clean = {**facts, "estimates": None}
        growth, growth_basis, growth_sources = _growth(clean)
    horizon = config["allocation"]["horizon_months"]
    if horizon not in (6, 12, 18):
        return None, "Company models support 6, 12, or 18 month horizons."
    state = config["allocation"].get("shared_state") or DEFAULT_SHARED_STATE
    labels = {str(label).lower(): label for label in state["market_returns"]}
    if not set(LABELS) <= set(labels):
        return None, "Company defaults require Adverse, Central, and Favorable shared states."
    sector = security.get("sector") or "Unknown"
    multiplier = float(state["sector_multipliers"].get(sector, 1.0))
    multiple = price / starting_eps
    # Missing future dividends are an explicit draft zero distribution assumption,
    # not a claim that the provider observed a valid zero.
    actions = facts["actions"]
    dividends = [row for row in actions if row.get("kind") == "dividend"]
    splits = [row for row in actions if row.get("kind") == "split"]
    adjusted = split_adjust_dividends(dividends, splits)
    start = (pd.Timestamp(cutoff.date()) - pd.DateOffset(years=1)).date().isoformat()
    paid = [
        row for row in adjusted if start < str(row.get("date") or "") <= cutoff.date().isoformat()
    ]
    observed_dividends = sum(row["value"] for row in paid if _number(row.get("value")) is not None)
    distributions = observed_dividends * horizon / 12
    rows = []
    for label in LABELS:
        market = _scale(float(state["market_returns"][labels[label]]), 12, state["horizon_months"])
        shock = (market - DEFAULT_SHARED_STATE["market_returns"]["Central"]) * multiplier
        eps_growth = max(-0.90, min(1.0, growth + shock * EARNINGS_SENSITIVITY))
        multiple_factor = max(0.10, 1 + shock * MULTIPLE_SENSITIVITY * horizon / 12)
        rows.append(
            {
                "label": label,
                "eps": starting_eps * (1 + eps_growth) ** (horizon / 12),
                "pe": multiple * multiple_factor,
                "distributions_per_starting_share": distributions,
                "earnings_growth": eps_growth,
                "multiple_change": multiple_factor - 1,
            }
        )
    payload = {
        "security_id": sid,
        "starting_price": price,
        "currency": currency,
        "horizon_months": horizon,
        "eps_convention": convention,
        "pe_convention": convention,
        "scenarios": rows,
        "proposal_meta": {
            "basis": "automatic_draft",
            "method_version": METHOD_VERSION,
            "evidence": {
                "earnings": eps_sources,
                "earnings_growth": growth_sources,
                "starting_price": facts["price"]["source_id"],
            },
            "inputs": {
                "starting_eps": starting_eps,
                "starting_pe": multiple,
                "base_annual_earnings_growth": growth,
                "earnings_basis": eps_basis,
                "earnings_bridge": earnings_bridge,
                "growth_basis": growth_basis,
                "growth_convention": (
                    "consensus_growth_applied_to_statement_eps_draft_proxy"
                    if convention == "trailing" and growth_basis.startswith("consensus")
                    else growth_basis
                ),
                "statement_period_end": facts["trailing"].get("period_end"),
                "price_date": facts["price"].get("date"),
                "estimates_received_at": (facts["estimates"] or {}).get("received_at"),
                "statements_used": convention == "trailing",
                "estimates_used": bool(growth_sources and growth_basis.startswith("consensus"))
                or convention == "forward",
            },
            "assumptions_visible": [
                "earnings_growth",
                "terminal_multiple",
                "distributions",
                "market_shock_sensitivity",
            ],
            "assumptions": {
                "earnings_sensitivity": EARNINGS_SENSITIVITY,
                "multiple_sensitivity": MULTIPLE_SENSITIVITY,
                "dividend_basis": "trailing_cash_annualized" if paid else "explicit_zero_draft",
                "growth_cap": MAX_GROWTH,
                "share_count": "constant_after_starting_eps",
                "reporting_to_quote_fx": (
                    "constant_retained_spot_rate" if (earnings_bridge or {}).get("fx") else None
                ),
            },
            "notes": [
                "Draft scenarios; shared market shocks adjust earnings growth and terminal P/E.",
                "Growth is a scenario assumption, not a calibrated return forecast.",
                "Cash dividends scale with the horizon; buybacks are reflected only in EPS/shares.",
                "FRED observations provide context; they do not numerically change this model.",
            ]
            + ([facts["earnings_issue"]] if facts.get("earnings_issue") else [])
            + (
                [
                    "Consensus EPS growth is a draft proxy applied to reported EPS; their accounting definitions may differ."
                ]
                if convention == "trailing" and growth_basis.startswith("consensus")
                else []
            )
            + (
                [
                    "Consensus EPS uses the retained listing-estimate unit contract; no ADS ratio was inferred."
                ]
                if convention == "forward"
                and earnings_bridge["input_per_share_basis"] == "retained_listing_contract"
                else []
            ),
        },
    }
    return payload, None


def _frozen_proposal(sid, security, bundle, config):
    """Older archives retain verified proposals but not the newer native facts.

    Reuse those exact assumptions only at their original horizon and currency. Their
    provenance stays visible; no missing EPS level or growth rate is synthesized.
    """
    if security.get("instrument_type") != "equity" or security.get("resolution_status") in {
        "unresolved",
        "conflict",
    }:
        return None
    if ((bundle.get("research_inputs") or {}).get(sid) or {}).get("valuation_facts"):
        # A known failed native fact is a gate, not a reason to fall back to an old
        # assumption. This compatibility route is only for genuinely older archives.
        return None
    source = (((bundle.get("research") or {}).get(sid) or {}).get("proposals") or {}).get("eps")
    if not isinstance(source, dict):
        return None
    if source.get("horizon_months") != config["allocation"]["horizon_months"]:
        return None
    if _currency(source.get("currency")) != _currency(security.get("currency")):
        return None
    meta = source.get("proposal_meta") or {}
    if not meta.get("evidence") or not meta["evidence"].get("starting_price"):
        return None
    estimates = ((bundle.get("research_inputs") or {}).get(sid) or {}).get("estimates")
    if estimates and _estimate(estimates, bundle, config) is None:
        return None
    from .calendar import bundle_cutoff, current_receipt_limit

    cutoff = bundle_cutoff(bundle)
    quote = _observed(_frame(bundle, "prices"), bundle, "date", config=config)
    if not quote.empty and "security_id" in quote:
        quote = quote[quote.security_id.astype(str).eq(sid)]
        if not quote.empty:
            day = pd.Timestamp(quote["date"].max()).date()
            if (cutoff.date() - day).days > config["data"].get("max_price_age_days", 4):
                return None
    period = (meta.get("trailing") or {}).get("period_end")
    if period and (cutoff.date() - pd.Timestamp(period).date()).days > config["data"].get(
        "max_fundamental_age_days", 150
    ):
        return None
    identifiers = {value for value in meta["evidence"].values() if isinstance(value, str)}
    for receipt in bundle.get("sources") or []:
        if receipt.get("source_id") not in identifiers:
            continue
        stamp = pd.to_datetime(receipt.get("received_at"), utc=True, errors="coerce")
        if pd.isna(stamp) or stamp > current_receipt_limit(bundle):
            return None
    verified = calculate_eps(source)
    if verified["status"] != "ready":
        return None
    payload = deepcopy(source)
    meta = payload["proposal_meta"]
    meta["basis"] = "retained_proposal_draft"
    meta["inputs"] = {
        **(meta.get("inputs") or {}),
        "earnings_basis": "retained_consensus_range",
        "growth_basis": "retained_proposal_assumptions",
        "statements_used": False,
        "estimates_used": True,
        "statement_period_end": (meta.get("trailing") or {}).get("period_end"),
        "proposal_review_date": bundle.get("as_of"),
    }
    meta["notes"] = [
        *(meta.get("notes") or []),
        "Retained proposal from an earlier review; refresh to build the current statement-based defaults.",
    ]
    return payload


def _probabilities(result, config, labels):
    allocation = config["allocation"]
    if not allocation.get("use_probabilities", True):
        return None
    stated = allocation.get("probability_overrides") or (allocation.get("shared_state") or {}).get(
        "probabilities"
    )
    if not stated:
        # A complete retained distribution may be supplied through a forecast import.
        benchmark = config["mandate"].get("benchmark_id")
        stated = {
            str(row.get("scenario", "")).lower(): row.get("probability")
            for row in result.get("forecast_inputs", [])
            if row.get("security_id") == benchmark
        }
    values = {str(label).lower(): _number(value) for label, value in (stated or {}).items()}
    if set(values) != set(labels) or any(
        value is None or value < 0 or value > 1 for value in values.values()
    ):
        return None
    return values if abs(sum(values.values()) - 1) < 1e-8 else None


def _summary_return(scenarios, probabilities):
    values = {row["label"]: _number(row.get("return_value")) for row in scenarios}
    if (
        probabilities
        and set(values) == set(probabilities)
        and all(v is not None for v in values.values())
    ):
        return sum(
            values[label] * probability for label, probability in probabilities.items()
        ), "weighted_scenario"
    return values.get("central"), "central_scenario"


def _presentation_return(value, label, currency, config):
    base = config["mandate"].get("base_currency")
    if value is None or not currency or not base:
        return None
    if currency == base:
        return value
    state = config["allocation"].get("shared_state") or DEFAULT_SHARED_STATE
    states = state["fx_returns"].get(currency) or {}
    keys = {str(key).lower(): key for key in states}
    if label not in keys:
        return None
    fx = _scale(
        float(states[keys[label]]), config["allocation"]["horizon_months"], state["horizon_months"]
    )
    return (1 + value) * (1 + fx) - 1


def _dcf_cases(payload):
    """Intrinsic sensitivities, deliberately without a price-convergence forecast."""
    rows = []
    for label, rate_shift, margin_factor in (
        ("adverse", 0.02, 0.9),
        ("central", 0, 1),
        ("favorable", -0.02, 1.1),
    ):
        draft = deepcopy(payload)
        rate = _number(draft.get("discount_rate"))
        if rate is not None:
            draft["discount_rate"] = rate + rate_shift
        for projection in draft.get("projections", []):
            if _number(projection.get("ebit")) is not None:
                projection["ebit"] *= margin_factor
        try:
            calculated = calculate_dcf(draft)
            value, issues = calculated.get("value_per_share"), calculated.get("issues", [])
        except ValueError as exc:
            value, issues = None, [str(exc)]
        rows.append(
            {
                "label": label,
                "value_per_share": value,
                "horizon_price": None,
                "return_value": None,
                "local_return_value": None,
                "probability": None,
                "discount_rate": draft.get("discount_rate"),
                "operating_margin_multiplier": margin_factor,
                "status": "ready" if value is not None else "blocked",
                "issues": issues,
            }
        )
    return rows


def _next_step(action, reason, model, payload):
    if action != "Review":
        code, label, section = "adjust_assumptions", "Edit assumptions", "research"
    elif model == "fcff_dcf":
        code, label, section = "horizon_scenarios_missing", "Set horizon scenarios", "research"
    elif reason == "Select a benchmark with matching scenarios to compare this holding.":
        code, label, section = "benchmark_missing", "Choose a benchmark", "settings"
    elif reason in GAP_STEPS:
        code, label, section = GAP_STEPS[reason]
    elif payload:
        code, label, section = "model_inputs_missing", "Complete assumptions", "research"
    else:
        code, label, section = "inputs_incomplete", "Check inputs", "data"
    return {"code": code, "label": label, "section": section}


def build_holding_analysis(result, bundle, config, workspace):
    """One calculation/action record per holding; no side effects or provider calls."""
    securities = _frame(bundle, "securities")
    secmap = (
        {str(row["security_id"]): row for row in securities.to_dict("records")}
        if "security_id" in securities
        else {}
    )
    holdings = {}
    for row in result.get("holdings", []):
        sid = str(row.get("security_id") or "")
        if sid:
            holdings.setdefault(sid, []).append(row)
    forecasts = {}
    for row in result.get("forecast_inputs", []):
        if row.get("horizon_months") not in {None, config["allocation"]["horizon_months"]}:
            continue
        if (
            row.get("forecast_date") is not None
            and str(row["forecast_date"])[:10] != str(bundle.get("as_of"))[:10]
        ):
            continue
        forecasts.setdefault(str(row["security_id"]), []).append(row)
    benchmark_id = config["mandate"].get("benchmark_id")
    benchmark = [
        {"label": str(row["scenario"]).lower(), "return_value": row.get("return_value")}
        for row in forecasts.get(benchmark_id, [])
    ]
    matching_result = {
        **result,
        "forecast_inputs": [row for rows in forecasts.values() for row in rows],
    }
    probabilities = _probabilities(matching_result, config, LABELS)
    benchmark_return, benchmark_metric = _summary_return(benchmark, probabilities)
    # An unweighted central comparison still requires the same complete three-case
    # benchmark packet; a partial or mismatched comparator cannot create a Buy/Sell.
    complete_benchmark = (
        {row["label"] for row in benchmark} == set(LABELS)
        and len(benchmark) == len(LABELS)
        and all(_number(row["return_value"]) is not None for row in benchmark)
        and all(
            row.get("horizon_months") in {None, config["allocation"]["horizon_months"]}
            and (
                row.get("forecast_date") is None
                or str(row["forecast_date"])[:10] == str(bundle.get("as_of"))[:10]
            )
            for row in forecasts.get(benchmark_id, [])
        )
    )
    if not complete_benchmark:
        benchmark_return = None
    result_rows = []
    for sid, positions in holdings.items():
        security = secmap.get(sid) or positions[0]
        manual = workspace.get("valuations", {}).get(sid) or {}
        active_model = manual.get("active_model") or (
            "dcf" if manual.get("dcf") and not manual.get("eps") else "eps"
        )
        payload, gap = (
            (deepcopy(manual["eps"]), None)
            if manual.get("eps")
            else automatic_eps(sid, security, bundle, config)
        )
        source_type = "user_assumption" if manual.get("eps") else "automatic_draft"
        if payload is None:
            retained = _frozen_proposal(sid, security, bundle, config)
            if retained:
                payload, gap, source_type = retained, None, "retained_proposal_draft"
        rows, model = [], "unavailable"
        model_result = None
        inputs = {}
        if payload:
            model_result = calculate_eps(payload)
            compatible_horizon = (
                payload.get("horizon_months") == config["allocation"]["horizon_months"]
            )
            for scenario in model_result["scenarios"]:
                local = scenario["total_return"]
                rows.append(
                    {
                        **scenario,
                        "local_return_value": local,
                        "return_value": _presentation_return(
                            local, scenario["label"], model_result["currency"], config
                        )
                        if compatible_horizon
                        else None,
                        "probability": (probabilities or {}).get(scenario["label"]),
                    }
                )
            inputs = deepcopy((payload.get("proposal_meta") or {}).get("inputs") or {})
            inputs.update(
                starting_price=model_result["starting_price"],
                currency=model_result["currency"],
                eps_convention=model_result["eps_convention"],
                horizon_months=model_result["horizon_months"],
            )
            model = "eps_multiple"
            if not compatible_horizon:
                gap = "Update this company model to the selected analysis horizon."
            company = result.setdefault("company_research", {}).setdefault(
                sid, {"security_id": sid, "basis": "subjective", "calibrated": False}
            )
            if not manual.get("eps"):
                company.update(
                    eps=model_result,
                    origin="automatic_draft",
                    proposal_meta=deepcopy(payload.get("proposal_meta")),
                )
        dcf_payload = None
        intrinsic_gap = None
        if active_model == "dcf":
            dcf_payload = manual.get("dcf")
            payload = None
            model, source_type = "fcff_dcf", "user_assumption"
            gap = "DCF estimates intrinsic value; horizon scenarios are required for a buy/sell comparison."
            rows = _dcf_cases(dcf_payload) if dcf_payload else []
            facts = _model_facts(sid, security, bundle, config)
            price = _number(facts["price"].get("close"))
            dcf_currency = _currency((dcf_payload or {}).get("currency"))
            central_value = next(
                (row.get("value_per_share") for row in rows if row["label"] == "central"), None
            )
            if central_value is not None and price and dcf_currency == facts["currency"]:
                intrinsic_gap = central_value / price - 1
            inputs = {
                "starting_price": price,
                "currency": dcf_currency,
                "active_model": "dcf",
                "horizon_months": None,
                "convergence_assumption": None,
                "statements_used": bool(
                    (dcf_payload or {}).get("proposal_meta", {}).get("evidence")
                ),
                "estimates_used": False,
            }
            result.setdefault("company_research", {}).setdefault(sid, {"security_id": sid})[
                "active_model"
            ] = "dcf"
        if not rows and active_model != "dcf":
            rows = [
                {
                    "label": str(row["scenario"]).lower(),
                    "return_value": row.get("return_value"),
                    "local_return_value": None,
                    "probability": row.get("probability"),
                }
                for row in forecasts.get(sid, [])
            ]
            if rows:
                model, source_type = "shared_state", "shared_market_assumption"
                if security.get("instrument_type") != "equity":
                    gap = None
        by_label = {row["label"]: row for row in rows}
        rows = [
            by_label.get(
                label,
                {
                    "label": label,
                    "return_value": None,
                    "probability": (probabilities or {}).get(label),
                },
            )
            for label in LABELS
        ]
        value, metric = _summary_return(rows, probabilities)
        if active_model == "dcf":
            value, metric = None, "intrinsic_value"
        excess = (
            value - benchmark_return
            if value is not None and benchmark_return is not None and metric == benchmark_metric
            else None
        )
        hurdle = _scale(
            float(config["allocation"].get("return_hurdle", 0.02)),
            config["allocation"]["horizon_months"],
            12,
        )
        cost = 2 * float(config["allocation"].get("transaction_cost_bps", 10)) / 10_000
        action, reason = "Review", gap or "Complete comparable company and benchmark scenarios."
        if value is not None and not gap:
            if model == "shared_state":
                action, reason = (
                    "Hold",
                    "Market scenario only; review company evidence before changing the holding.",
                )
            elif excess is None:
                action, reason = (
                    "Review",
                    (bundle.get("benchmark_reference") or {}).get("reason")
                    or "Select a benchmark with matching scenarios to compare this holding.",
                )
            elif all(row.get("return_value") is not None for row in rows):
                if excess > hurdle + cost:
                    action, reason = (
                        "Buy",
                        "Scenario return exceeds the benchmark and review hurdle.",
                    )
                elif excess < -(hurdle + cost):
                    action, reason = (
                        "Sell",
                        "Scenario return trails the benchmark beyond the review hurdle.",
                    )
                else:
                    action, reason = "Hold", "Scenario difference is within the review hurdle."
        meta = (payload or {}).get("proposal_meta") or {}
        evidence_ids = meta.get("evidence") or {}
        coverage = ((bundle.get("coverage") or {}).get("by_security") or {}).get(sid) or {}
        macro = [str(row["series_id"]) for row in result.get("macro", []) if row.get("series_id")]
        reasons = ["Subjective scenario judgement requires human review."]
        if not config["mandate"].get("confirmed"):
            reasons.append("Confirm the investment mandate.")
        if not result.get("summary", {}).get("complete"):
            reasons.append("Reconcile account coverage and funding.")
        if action == "Review":
            reasons.append(reason)
        reasons.extend(
            str(issue.get("message"))
            for issue in (result.get("allocation") or {}).get("issues", [])
            if issue.get("severity") == "error"
        )
        row = {
            "security_id": sid,
            "ticker": security.get("ticker") or sid,
            "name": security.get("name") or positions[0].get("name") or sid,
            "account_ids": sorted({str(p["account_id"]) for p in positions}),
            "market_value": sum(_number(p.get("market_value")) or 0 for p in positions),
            "action": action,
            "reason": reason,
            "next_step": _next_step(action, reason, model, payload),
            "status": "ready" if value is not None and not gap else "unavailable",
            "model": model,
            "source_type": source_type,
            "basis": "subjective_scenario",
            "calibrated": False,
            "executable": False,
            "return_metric": metric,
            "scenario_return": value,
            "benchmark_return": benchmark_return,
            "compare_to_benchmark": {
                "security_id": benchmark_id,
                "return_value": benchmark_return,
                "return_metric": benchmark_metric,
                "horizon_months": config["allocation"]["horizon_months"],
                "source_type": "retained_joint_scenarios",
                "as_of": bundle.get("as_of"),
            },
            "excess_return": excess,
            "review_hurdle": hurdle,
            "round_trip_cost_fraction": cost,
            "horizon_months": config["allocation"]["horizon_months"],
            "scenarios": rows,
            "valuation_input": payload,
            "dcf_valuation_input": dcf_payload,
            "intrinsic_value_gap": intrinsic_gap,
            "inputs": inputs,
            "evidence": {
                "statements": {
                    "used": bool(inputs.get("statements_used")),
                    "status": coverage.get(
                        "statements", "available" if inputs.get("statements_used") else "missing"
                    ),
                    "source_ids": evidence_ids.get("earnings", [])
                    if inputs.get("statements_used")
                    else [],
                },
                "estimates": {
                    "used": bool(inputs.get("estimates_used")),
                    "status": coverage.get(
                        "estimates", "available" if inputs.get("estimates_used") else "missing"
                    ),
                    "source_ids": evidence_ids.get("earnings_growth", [])
                    if inputs.get("estimates_used")
                    else [],
                },
                "macro": {
                    "used": False,
                    "status": "context_only" if macro else "missing",
                    "series_ids": macro,
                    "note": "Economic observations are context; edit shared scenarios to express their assumed impact.",
                },
            },
            "trade_readiness": {"status": "human_review", "reasons": list(dict.fromkeys(reasons))},
            "method_version": METHOD_VERSION,
        }
        result_rows.append(row)
        for position in positions:
            position["research_action"] = action
            position["research_reason"] = reason
    return json_safe(
        sorted(result_rows, key=lambda row: (-row["market_value"], row["security_id"]))
    )
