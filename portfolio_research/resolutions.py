"""Owner resolutions of open identity and currency exceptions as supplemental evidence.

A resolution never edits a stored record: it builds the next supplemental version from
the latest one. Account facts bind the exception's exact collector snapshot. A listing
mapping applies from the review day; an earlier open mapping of the same source symbol
is closed the day before, so exactly one mapping applies on every date and earlier
reviews keep the mapping they used.
"""

from __future__ import annotations

import copy
from datetime import date, timedelta

CLOSED_EXCEPTION = "This exception is no longer open."
STILL_OPEN = (
    "This answer does not close the exception; nothing was saved. "
    "Check the account and date it applies to."
)
PAYLOAD_FIELDS = frozenset({"key", "kind", "source_id", "snapshot_id", "values"})
ACCOUNT_KINDS = frozenset({"account_currency", "account_facts"})
VALUE_FIELDS = {
    "account_currency": frozenset({"position_currency", "currency"}),
    "account_facts": frozenset(
        {"valuation_date", "cash", "total_value", "currency", "position_currency", "complete"}
    ),
    "security_listing": frozenset(
        {
            "security_id",
            "symbol",
            "ticker",
            "issuer_id",
            "name",
            "instrument_type",
            "currency",
            "exchange",
        }
    ),
}
REQUIRED_VALUES = {
    "account_currency": ("position_currency",),
    "security_listing": ("security_id", "symbol"),
}
MISSING_VALUES = {
    "account_currency": "Choose the position_currency this account's values are reported in.",
    "account_facts": "Enter at least one account fact: valuation date, cash, NAV, currency or completeness.",
    "security_listing": "Choose a listing: a security_id or exact quote symbol is required.",
}
FX_MANUAL = "Manual FX rates are not supported; refresh market data to load a dated FX observation."
LISTING_OVERRIDES = ("ticker", "name", "instrument_type", "currency", "exchange")
EMPTY_SUPPLEMENTAL = {"version": 1, "accounts": [], "securities": [], "tax_lots": []}


def validate_payload(payload) -> dict:
    """Shape of a resolution request: ``{key, kind, source_id, snapshot_id, values}``."""
    if not isinstance(payload, dict):
        raise ValueError("Send an object describing the resolution.")
    if set(payload) - PAYLOAD_FIELDS:
        raise ValueError(
            "A resolution contains only key, kind, source_id, snapshot_id and values fields."
        )
    if not isinstance(payload.get("key"), str):
        raise ValueError(CLOSED_EXCEPTION)
    return payload


def open_exception(payload, open_records) -> dict:
    """The open exception a validated payload names; ``ValueError`` when it cannot apply."""
    record = next((row for row in open_records if row.get("key") == payload["key"]), None)
    if record is None:
        raise ValueError(CLOSED_EXCEPTION)
    kind = (record.get("resolution") or {}).get("kind")
    if payload.get("kind") != kind:
        raise ValueError(f"This exception is resolved with kind {kind}.")
    if payload.get("source_id") != record.get("source_id") or payload.get(
        "snapshot_id"
    ) != record.get("snapshot_id"):
        raise ValueError("The resolution must name the exception's account and snapshot.")
    if kind == "fx_manual":
        raise ValueError(FX_MANUAL)
    if kind not in VALUE_FIELDS:
        raise ValueError("This exception cannot be resolved with supplemental evidence.")
    bound = record.get("snapshot_id") if kind in ACCOUNT_KINDS else record.get("raw_symbol")
    if record.get("source_id") is None or bound is None:
        raise ValueError("This exception is not bound to a collected account.")
    return record


def resolution_values(kind, values) -> dict:
    """Supplied values for ``kind`` without nulls; unknown fields are rejected."""
    allowed = VALUE_FIELDS[kind]
    if not isinstance(values, dict):
        raise ValueError(MISSING_VALUES[kind])
    if set(values) - allowed:
        raise ValueError(
            f"This resolution accepts only these fields: {', '.join(sorted(allowed))}."
        )
    cleaned = {field: copy.deepcopy(value) for field, value in values.items() if value is not None}
    if not any(field in cleaned for field in REQUIRED_VALUES.get(kind, allowed)):
        raise ValueError(MISSING_VALUES[kind])
    return cleaned


def merged_accounts(latest, record, values) -> dict:
    """Next supplemental version with ``values`` set on the exception's account snapshot."""
    base = copy.deepcopy(latest) if latest else copy.deepcopy(EMPTY_SUPPLEMENTAL)
    scope = {"source_id": record["source_id"], "snapshot_id": record["snapshot_id"]}
    accounts, matched = [], False
    for row in base.get("accounts", []):
        if (
            row.get("source_id") == scope["source_id"]
            and row.get("snapshot_id") == (scope["snapshot_id"])
        ):
            accounts.append({**row, **values})
            matched = True
        else:
            accounts.append(row)
    if not matched:
        accounts.append({**scope, **values})
    return {**base, "accounts": accounts}


def _listing_symbol(values) -> str:
    from .market_listings import normalize_symbol

    chosen = {
        normalize_symbol(values[field]) for field in ("security_id", "symbol") if field in values
    }
    if None in chosen or len(chosen) != 1:
        raise ValueError(
            "Enter one exact listing symbol of 1-20 letters, digits, '.', '-', '=' or '^'."
        )
    return chosen.pop()


def listing_mapping(config, record, values, valid_from, *, issues) -> dict:
    """A dated security mapping for the chosen listing, from cached provider metadata.

    Owner-supplied fields win over metadata; the issuer comes from the supplied value,
    the cached SEC ticker list, or the listing itself. Providers are never refreshed.
    """
    from . import issuers, market_listings
    from .identity import _issuer_id, _listing_currency

    symbol = _listing_symbol(values)
    meta = market_listings.listing_metadata(config, symbol, refresh=False, issues=issues) or {}
    issuer_id = values.get("issuer_id")
    if issuer_id is None:
        lookup = issuers.sec_issuer_lookup(config, refresh=False, issues=issues) if meta else None
        issuer_id = _issuer_id(lookup, meta, symbol)
    name = meta.get("name")
    mapping = {
        "source_id": record["source_id"],
        "raw_symbol": record["raw_symbol"],
        "security_id": symbol,
        "issuer_id": issuer_id,
        "ticker": symbol,
        "name": name if isinstance(name, str) else None,
        "instrument_type": market_listings.instrument_type(meta.get("instrument_type")),
        "currency": _listing_currency(meta, symbol)[1] if meta else None,
        "exchange": meta.get("exchange"),
        "valid_from": valid_from,
    }
    mapping.update({field: values[field] for field in LISTING_OVERRIDES if field in values})
    return {field: value for field, value in mapping.items() if value is not None}


def merged_securities(latest, mapping) -> dict:
    """Next supplemental version with ``mapping`` applying from its ``valid_from`` day."""
    base = copy.deepcopy(latest) if latest else copy.deepcopy(EMPTY_SUPPLEMENTAL)
    start = mapping["valid_from"]
    previous_day = (date.fromisoformat(start) - timedelta(days=1)).isoformat()
    securities = []
    for row in base.get("securities", []):
        same_symbol = (row.get("source_id"), row.get("raw_symbol")) == (
            mapping["source_id"],
            mapping["raw_symbol"],
        )
        if not same_symbol:
            securities.append(row)
            continue
        if row.get("valid_from") == start:
            continue  # Replaced by the new mapping for the same day.
        starts_before = not row.get("valid_from") or row["valid_from"] < start
        open_on_start = not row.get("valid_to") or row["valid_to"] >= start
        securities.append(
            {**row, "valid_to": previous_day} if starts_before and open_on_start else row
        )
    return {**base, "securities": [*securities, mapping]}


def require_closed(key, open_records) -> None:
    """Refuse an answer that leaves the exception open, before anything is written."""
    if any(row.get("key") == key for row in open_records):
        raise ValueError(STILL_OPEN)


def append_resolution(store, record, kind, values, supplemental) -> tuple:
    """Append the next supplemental version and the resolution that produced it.

    Returns ``(supplemental_record_id, resolution_record_id)``. The resolution is a
    child of the version it wrote, so a stored answer is always traceable to its input.
    """
    supplemental_id = store.append_record("supplemental", supplemental)
    record_id = store.append_record(
        "resolution",
        {
            "key": record["key"],
            "kind": kind,
            "values": values,
            "snapshot_id": record["snapshot_id"],
            "source_id": record["source_id"],
        },
        parent_id=supplemental_id,
    )
    return supplemental_id, record_id
