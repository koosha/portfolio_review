"""Currency and share-unit bridges for source-declared per-share values.

FX alone cannot turn ordinary-share earnings into ADS earnings. A share ratio is
accepted only as dated, sourced evidence, never inferred from two share counts.
All functions consume retained observations and make no provider calls.
"""

from __future__ import annotations

import math
from collections.abc import Mapping

import pandas as pd

from .fx import MINOR_UNITS, FxTable, normalize_unit

METHOD_VERSION = "valuation-listing-basis-1"
PER_SHARE_BASES = {"listed_share", "ordinary_share"}


def number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (ValueError, TypeError):
        return None
    return result if math.isfinite(result) else None


def currency(value):
    return (
        value.upper()
        if isinstance(value, str)
        and len(value) == 3
        and value.isascii()
        and value.isalpha()
        and value not in MINOR_UNITS
        else None
    )


def _known(record, cutoff, *, current=False, require_received=True, receipt_limit=None):
    if not isinstance(record, Mapping) or not record.get("source_id"):
        return False
    published = record.get("available_at") or record.get("published_at")
    received = record.get("received_at")
    if not published or not received:
        return False
    cutoff = pd.Timestamp(cutoff)
    public = pd.to_datetime(published, utc=True, errors="coerce")
    stamp = pd.to_datetime(received, utc=True, errors="coerce") if received else None
    receipt_limit = (
        pd.Timestamp(receipt_limit)
        if receipt_limit is not None
        else cutoff + (pd.Timedelta(hours=1) if current else pd.Timedelta(0))
    )
    return bool(
        not pd.isna(public)
        and public <= cutoff
        and (stamp is None or (not pd.isna(stamp) and stamp <= receipt_limit))
    )


def listing_ratio(
    record, *, as_of, cutoff, current=False, require_received=True, receipt_limit=None
):
    """Ordinary shares represented by one listed share, or an explicit gap."""
    record = record if isinstance(record, Mapping) else {}
    ratio = number(record.get("ordinary_shares_per_listed_share"))
    if (
        ratio is None
        or ratio <= 0
        or not _known(
            record,
            cutoff,
            current=current,
            require_received=require_received,
            receipt_limit=receipt_limit,
        )
    ):
        return None, "A dated, sourced ordinary-share to listed-share ratio is required."
    start, end = record.get("effective_from"), record.get("effective_to")
    try:
        day = pd.Timestamp(as_of).date()
        if not start or pd.Timestamp(start).date() > day:
            return None, "The share ratio is not effective at this valuation date."
        if end and pd.Timestamp(end).date() <= day:
            return None, "The retained share ratio has expired at this valuation date."
    except (ValueError, TypeError):
        return None, "The share ratio has invalid effective dates."
    return ratio, None


def translate_per_share(
    value,
    *,
    from_currency,
    quote_currency,
    per_share_basis=None,
    listing_basis=None,
    fx_observations=(),
    as_of,
    cutoff,
    current=False,
    require_received=True,
    max_fx_age_days=7,
    same_currency_listing_contract=False,
    receipt_limit=None,
):
    """A per-listed-share value in quote currency, with both bridges preserved.

    The legacy same-currency listing contract is explicit at the call site. It
    cannot authorize a cross-currency conversion with undeclared share units.
    """
    value, quote = number(value), currency(quote_currency)
    try:
        normalized, source, unit_factor = normalize_unit(
            value, from_currency if from_currency in MINOR_UNITS else currency(from_currency)
        )
        value = number(normalized)
    except ValueError:
        source, unit_factor = None, None
    result = {
        "method_version": METHOD_VERSION,
        "value": None,
        "status": "unavailable",
        "from_currency": source,
        "input_currency": from_currency,
        "input_unit_factor": unit_factor,
        "currency": quote,
        "input_per_share_basis": per_share_basis,
        "per_share_basis": "listed_share",
        "share_multiplier": None,
        "share_ratio_source_id": None,
        "fx": None,
        "reason": None,
    }
    if value is None or source is None or quote is None:
        return {**result, "reason": "A numeric per-share value and both currencies are required."}
    if per_share_basis == "listed_share":
        multiplier = 1.0
    elif per_share_basis == "ordinary_share":
        multiplier, reason = listing_ratio(
            listing_basis,
            as_of=as_of,
            cutoff=cutoff,
            current=current,
            require_received=require_received,
            receipt_limit=receipt_limit,
        )
        if reason:
            return {**result, "reason": reason}
        result["share_ratio_source_id"] = listing_basis["source_id"]
    elif (
        source == quote
        and same_currency_listing_contract
        and per_share_basis in (None, "provider_listing_contract", "retained_listing_contract")
    ):
        multiplier = 1.0
        result["input_per_share_basis"] = "retained_listing_contract"
    else:
        return {
            **result,
            "reason": "Statement EPS has no verified ordinary-share or listed-share basis.",
        }
    result["share_multiplier"] = multiplier
    listed_value = number(value * multiplier)
    if listed_value is None:
        return {**result, "reason": "The share-unit conversion is not a finite per-share value."}
    if source == quote:
        return {**result, "value": listed_value, "status": "identity"}
    observations = []
    for row in fx_observations or []:
        if not isinstance(row, Mapping):
            continue
        # An FX date without a release stamp uses the dated observation as its
        # publication boundary; its explicit receipt must still pass the cutoff.
        dated = {**row, "available_at": row.get("published_at") or row.get("date")}
        if not _known(
            dated, cutoff, current=current, require_received=True, receipt_limit=receipt_limit
        ):
            continue
        try:
            FxTable([dict(row)])
        except (ValueError, TypeError):
            continue
        observations.append(dict(row))
    converted = FxTable(observations, max_age_days=max_fx_age_days).convert(
        listed_value, source, quote, as_of
    )
    if converted["amount"] is None:
        return {
            **result,
            "fx": converted,
            "reason": f"A fresh, retained {source}/{quote} FX observation is required.",
        }
    amount = number(converted["amount"])
    if amount is None:
        return {
            **result,
            "reason": "The FX conversion is not a finite per-share value.",
            "fx": converted,
        }
    return {**result, "value": amount, "status": "converted", "fx": converted}


def estimate_currency(estimates, period, family="eps"):
    """Use a period's own unit declaration before the legacy family contract."""
    estimates = estimates if isinstance(estimates, Mapping) else {}
    block = (estimates.get(family) or {}).get(period) or {}
    if "currency" in block:
        return currency(block.get("currency"))
    field = "currency" if family == "eps" else "revenue_currency"
    return currency(estimates.get(field))
