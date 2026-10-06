"""Explicit comparison instruments, separate from holdings and purchase candidates.

The selection is retained exactly in configuration. Calculation copies resolve a
known security ID or an unambiguous ticker; the VTI catalog entry supplies identity,
never price history or a return forecast. Shared-state returns remain assumptions.
"""

from copy import deepcopy

import pandas as pd

VERSION = "benchmark-reference-1"
DEFAULT_BENCHMARK = "VOO"
CATALOG = {
    "VOO": {
        "security_id": "benchmark:VOO",
        "ticker": "VOO",
        "name": "S&P 500 (VOO)",
        "identity_locator": "https://investor.vanguard.com/investment-products/etfs/profile/voo",
    },
    "VTI": {
        "security_id": "benchmark:VTI",
        "ticker": "VTI",
        "name": "VTI · U.S. equities",
        "identity_locator": "https://advisors.vanguard.com/investments/products/vti/vanguard-total-stock-market-etf",
    },
}
COMMON_EQUITY_REFERENCE = {
    "instrument_type": "etf",
    "currency": "USD",
    "quote_currency": "USD",
    "sector": None,
    "domicile": "US",
    "resolution_status": "resolved",
    "equity_shared_state": True,
    "identity_source": "known_equity_comparator",
}


def _text(value):
    return value.strip() if isinstance(value, str) and value.strip() else None


def resolve_benchmark(bundle, config):
    """Resolve only an owner's explicit choice; a ticker never overrides an exact ID."""
    configured = _text((config.get("mandate") or {}).get("benchmark_id"))
    selected = configured or DEFAULT_BENCHMARK
    reference = {
        "version": VERSION,
        "selection": selected,
        "configured_id": configured,
        "default_applied": configured is None,
        "security_id": None,
        "ticker": None,
        "name": None,
        "status": "not_selected",
        "reference_only": True,
        "tradable": False,
        "equity_shared_state": False,
        "reason": "Choose a benchmark to compare this holding.",
    }
    frame = bundle.get("securities")
    records = frame.to_dict("records") if isinstance(frame, pd.DataFrame) else []
    exact = [row for row in records if _text(row.get("security_id")) == selected]
    matched = exact or [
        row for row in records if (_text(row.get("ticker")) or "").upper() == selected.upper()
    ]
    if len(matched) > 1:
        reference.update(
            status="ambiguous",
            reason="Choose the exact saved security for this ambiguous benchmark ticker.",
        )
        return reference
    if matched:
        row = matched[0]
        sid, ticker = _text(row.get("security_id")), _text(row.get("ticker"))
        currency = _text(row.get("quote_currency")) or _text(row.get("currency"))
        reference.update(
            security_id=sid,
            ticker=ticker,
            name=_text(row.get("name")) or ticker or sid,
            instrument_type=_text(row.get("instrument_type")),
            currency=currency,
            quote_currency=currency,
            sector=_text(row.get("sector")),
            identity_source="saved_security_id" if exact else "saved_unique_ticker",
            existing_security=True,
            equity_shared_state=bool(ticker and ticker.upper() in CATALOG)
            or str(row.get("equity_shared_state")).lower() in {"true", "1", "1.0"},
            status="resolved",
            reason=None,
        )
        if (
            str(row.get("resolution_status") or "").lower() in {"unresolved", "conflict"}
            or not currency
            or len(currency) != 3
            or not currency.isalpha()
        ):
            reference.update(
                status="unresolved", reason="Resolve the benchmark identity and quote currency."
            )
        return reference
    catalog = next(
        (
            row
            for ticker, row in CATALOG.items()
            if selected.upper() == ticker or selected == row["security_id"]
        ),
        None,
    )
    if catalog:
        reference.update(
            deepcopy(COMMON_EQUITY_REFERENCE) | deepcopy(catalog),
            status="resolved",
            reason=None,
            existing_security=False,
        )
        return reference
    reference.update(
        status="unresolved",
        reason="Choose VOO, VTI or a saved security; this benchmark is not available in the review.",
    )
    return reference


def prepare_benchmark(bundle, config):
    """Attach a resolution record and return an immutable calculation config copy."""
    reference = resolve_benchmark(bundle, config)
    prior = bundle.get("benchmark_reference") or {}
    if prior.get("security_id") == reference.get("security_id") and prior.get(
        "price_acquisition_status"
    ):
        reference["price_acquisition_status"] = prior["price_acquisition_status"]
    bundle["benchmark_reference"] = reference
    used = deepcopy(config)
    sid, selected = reference.get("security_id"), reference.get("selection")
    if reference["status"] == "resolved":
        used["mandate"]["benchmark_id"] = sid
        # Scenario edits may name the displayed ticker or the canonical security ID.
        # Neither account permissions nor eligible-universe membership is changed.
        mappings = [
            used["allocation"].get("return_overrides", {}),
            used["allocation"].get("prior_returns", {}),
            (used["allocation"].get("shared_state") or {}).get("asset_overrides", {}),
        ]
        securities = bundle.get("securities")
        ids = (
            set(securities.get("security_id", []))
            if isinstance(securities, pd.DataFrame)
            else set()
        )
        catalog_ids = set(CATALOG) | {row["security_id"] for row in CATALOG.values()}
        for mapping in mappings:
            for inactive in catalog_ids - ids - {selected, sid, reference.get("ticker")}:
                mapping.pop(inactive, None)
            for alias in ({selected, reference.get("ticker")} - {sid, None}) - (ids - {selected}):
                if alias in mapping:
                    if sid in mapping and mapping[sid] != mapping[alias]:
                        raise ValueError(
                            "Benchmark assumptions name conflicting ticker and security ID values."
                        )
                    mapping[sid] = mapping.pop(alias)
        for name, keys in (
            ("forecasts", ["scenario", "horizon_months", "forecast_date"]),
            ("prices", ["date"]),
        ):
            frame = bundle.get(name)
            if not isinstance(frame, pd.DataFrame) or "security_id" not in frame:
                continue
            aliases = {selected, reference.get("ticker")} - {sid, None} - ids
            changed = frame.security_id.isin(aliases)
            if not changed.any():
                continue
            remapped = frame.copy()
            remapped.loc[changed, "original_security_id"] = remapped.loc[changed, "security_id"]
            remapped.loc[changed, "security_id"] = sid
            eligible_keys = ["security_id", *[key for key in keys if key in frame]]
            if remapped.loc[remapped.security_id.eq(sid)].duplicated(eligible_keys).any():
                raise ValueError(
                    "Benchmark source rows name both its ticker and security ID; use one identity."
                )
            bundle[name] = remapped
    return used, reference


def enrich_reference_prices(bundle, config, as_of, *, deadline, should_stop=None):
    """One normal adapter price capability, bounded by the review's existing budget."""
    from time import monotonic

    from portfolio_lab.providers import _merge

    from . import market_data
    from .enrichment import _attempt, _source

    _, reference = prepare_benchmark(bundle, config)
    security = reference_security(bundle)
    if (
        security is None
        or config.get("data", {}).get("mode") != "live"
        or config.get("data", {}).get("price_provider") != "yahoo"
    ):
        return
    if monotonic() >= deadline or should_stop and should_stop():
        reference["price_acquisition_status"] = "not_attempted"
        return
    record = _attempt(
        "benchmark_prices",
        reference["security_id"],
        bundle.setdefault("issues", []),
        market_data.price_history,
        config,
        security,
        refresh=bool(config.get("data", {}).get("refresh_network", False)),
        issues=bundle["issues"],
        as_of=as_of,
    )
    rows = (record or {}).get("prices") or []
    if record and record.get("currency") == reference["quote_currency"] and rows:
        _merge(bundle, "prices", pd.DataFrame(rows))
        source = _source(record, market_data.PRICES_PROVIDER, len(rows))
        if source:
            bundle.setdefault("sources", []).append(source)
        reference["price_acquisition_status"] = "available"
    else:
        reference["price_acquisition_status"] = "unavailable"


def reference_security(bundle):
    """A comparison-only metadata row for engine lookups; never a universe member."""
    reference = bundle.get("benchmark_reference") or {}
    if reference.get("status") != "resolved" or reference.get("existing_security"):
        return None
    return {
        **deepcopy(reference),
        "issuer_id": None,
        "eligible": False,
        "owned": False,
        "candidate": False,
        "comparison_only": True,
    }


def comparison_metadata(bundle, securities):
    """Include reference identity solely where risk/allocation needs a lookup."""
    reference = reference_security(bundle)
    if reference is None:
        return securities
    prices = bundle.get("prices")
    if isinstance(prices, pd.DataFrame) and {"security_id", "currency"} <= set(prices):
        rows = prices[prices.security_id == reference["security_id"]]
        currencies = rows.currency.dropna().unique()
        if len(currencies) == 1:
            # Price presentation already verified any historical currency conversion.
            reference["currency"] = str(currencies[0])
    return pd.concat([securities, pd.DataFrame([reference])], ignore_index=True)


def retained_joint_probabilities(bundle, config, as_of, horizon, labels):
    """A complete, agreeing imported joint distribution can cover an added comparator."""
    from portfolio_lab.metrics import _observed

    frame = bundle.get("forecasts")
    required = {"forecast_date", "horizon_months", "scenario", "probability"}
    if not isinstance(frame, pd.DataFrame) or not required <= set(frame):
        return None
    eligible = _observed(frame, bundle, "forecast_date", None, config)
    dates = pd.to_datetime(eligible.forecast_date, utc=True, errors="coerce", format="mixed")
    eligible = eligible[
        (dates.dt.date == pd.Timestamp(as_of).date())
        & (pd.to_numeric(eligible.horizon_months, errors="coerce") == horizon)
    ]
    if set(eligible.scenario) != set(labels):
        return None
    distribution = {}
    for label, rows in eligible.groupby("scenario"):
        values = pd.to_numeric(rows.probability, errors="coerce")
        if (
            values.isna().any()
            or not values.between(0, 1).all()
            or values.max() - values.min() > 1e-10
        ):
            return None
        distribution[label] = float(values.iloc[0])
    return distribution if abs(sum(distribution.values()) - 1) < 1e-8 else None


def benchmark_view(bundle, result):
    """Publish whether a comparator has scenarios and an observed common history."""
    reference = deepcopy(bundle.get("benchmark_reference") or {})
    sid = reference.get("security_id")
    rows = [row for row in result.get("forecast_inputs", []) if row.get("security_id") == sid]
    shared = any("shared-state" in str(row.get("source")) for row in rows)
    reference.update(
        scenario_status="available"
        if rows and all(row.get("return_value") is not None for row in rows)
        else "unavailable",
        scenario_source="subjective_shared_states"
        if shared
        else "retained_scenarios"
        if rows
        else None,
        scenario_assumptions="Editable shared market states; conditional returns, not calibrated forecasts."
        if shared
        else "Retained matching-horizon scenario inputs with their recorded source and basis."
        if rows
        else None,
        history_status="available"
        if (result.get("risk") or {}).get("benchmark_observations", 0) > 0
        else "unavailable",
    )
    return reference
