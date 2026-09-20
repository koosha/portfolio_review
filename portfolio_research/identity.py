"""Listing identity for collector holdings from quote symbols and provider metadata.

Pure: listing metadata, listing search and issuer lookup are injected, so resolution is
deterministic and network-free. An owner's dated supplemental mapping stays
authoritative; provider metadata never overrides it. A captured quote symbol is an
exact listing and is never replaced by a display-text search. Eligibility is an
explicit decision and is never inferred from metadata. What cannot be established
becomes an exception with the candidates an owner can choose from.
"""

from __future__ import annotations

import copy
import hashlib
import json

from .venues import venue_currency

STATUS_RANK = {"mapped": 0, "resolved": 1, "resolved_by_search": 2, "resolved_from_display": 3}
COUNT_KEYS = (
    "mapped",
    "resolved",
    "resolved_from_display",
    "resolved_by_search",
    "ambiguous",
    "unresolved",
)
LISTING_RESOLUTION_FIELDS = (
    "security_id",
    "ticker",
    "issuer_id",
    "instrument_type",
    "currency",
    "exchange",
)
MAX_ISSUER_ID_LENGTH = 200


def exception_key(code, source_id, snapshot_id, raw_symbol=None, pair=None):
    """Stable 40-character key of an exception's scope; ``None`` and ``""`` are equal."""
    parts = [code, source_id, snapshot_id, raw_symbol or "", pair or ""]
    raw = json.dumps(["" if part is None else part for part in parts], separators=(",", ":"))
    return hashlib.sha1(raw.encode(), usedforsecurity=False).hexdigest()


def _optional_text(value, label):
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be nonempty text or null.")
    return value.strip()


def _validated_positions(ledger):
    if not isinstance(ledger, dict):
        raise ValueError("ledger must be an object.")
    positions = ledger.get("positions", [])
    securities = ledger.get("securities", [])
    if not isinstance(positions, list) or not isinstance(securities, list):
        raise ValueError("ledger positions and securities must be lists.")
    rows = {}
    for index, row in enumerate(securities):
        if not isinstance(row, dict) or not isinstance(row.get("security_id"), str):
            raise ValueError(f"ledger securities[{index}] must identify a security.")
        rows.setdefault(row["security_id"], row)
    for index, position in enumerate(positions):
        label = f"ledger positions[{index}]"
        if not isinstance(position, dict):
            raise ValueError(f"{label} must be an object.")
        if type(position.get("source_id")) is not int or position["source_id"] < 1:
            raise ValueError(f"{label}.source_id must be a positive integer.")
        if _optional_text(position.get("raw_symbol"), f"{label}.raw_symbol") is None:
            raise ValueError(f"{label}.raw_symbol is required.")
        _optional_text(position.get("quote_symbol"), f"{label}.quote_symbol")
        if _optional_text(position.get("security_id"), f"{label}.security_id") is None:
            raise ValueError(f"{label}.security_id is required.")
    return positions, rows


def _find_listing(listings, symbol):
    """Return ``(listing_symbol, metadata)`` for an exact symbol, or ``None``."""
    if not isinstance(symbol, str) or not symbol.strip():
        return None
    for key in dict.fromkeys((symbol.strip(), symbol.strip().upper())):
        if key in listings:
            meta = listings[key]
            if not isinstance(meta, dict):
                raise ValueError(f"listings[{key!r}] must be a listing metadata object.")
            return key, meta
    return None


def _listing_currency(meta, key):
    from .market_listings import listing_currency

    value = meta.get("quote_currency")
    quote_currency, major_currency, factor = listing_currency(value)
    if value is not None and quote_currency is None:
        raise ValueError(f"listings[{key!r}].quote_currency must be a three-letter code or null.")
    return quote_currency, major_currency, factor


def _base_row(columns, security_id, ticker):
    row = {column: None for column in columns}
    row.update(security_id=security_id, ticker=ticker, eligible=False)
    return row


def _with_listing_fields(row, quote_currency=None, factor=None, last_quote_at=None, **fields):
    return {
        **row,
        **fields,
        "quote_currency": quote_currency,
        "quote_unit_factor": factor,
        "last_quote_at": last_quote_at,
    }


def _alias(position, security_id, status, basis, mapping=None, exchange=None):
    mapping = mapping or {}
    return {
        "source_id": position["source_id"],
        "raw_symbol": position["raw_symbol"],
        "quote_symbol": position.get("quote_symbol"),
        "security_id": security_id,
        "valid_from": mapping.get("valid_from"),
        "valid_to": mapping.get("valid_to"),
        "exchange": mapping.get("exchange") if mapping else exchange,
        "share_class": mapping.get("share_class"),
        "snapshot_id": position.get("snapshot_id"),
        "resolution_status": status,
        "resolution_basis": basis,
    }


def _mapped(position, context):
    """An owner's dated mapping chosen by the adapter stays authoritative."""
    matches = [
        record
        for record in context["mappings"]
        if record["source_id"] == position["source_id"]
        and record["raw_symbol"] == position["raw_symbol"]
        and record.get("security_id") == position["security_id"]
    ]
    if not matches:
        return None
    mapping, columns = matches[0], context["columns"]
    captured = context["rows"].get(position["security_id"])
    if captured is not None:
        row = copy.deepcopy(captured)
    else:
        row = _base_row(columns, mapping["security_id"], mapping.get("ticker"))
        row.update({c: mapping[c] for c in columns if mapping.get(c) is not None})
        row["ticker"] = row["ticker"] or position["raw_symbol"]
        row["eligible"] = bool(
            mapping.get("issuer_id") and mapping.get("valid_from") and mapping.get("eligible")
        )
    listing = {}
    for symbol in (row.get("ticker"), position.get("quote_symbol") or position["raw_symbol"]):
        found = _find_listing(context["listings"], symbol)
        if found is None:
            continue
        quote_currency, major_currency, factor = _listing_currency(found[1], found[0])
        # Quote units come from provider metadata only when it agrees with the mapping.
        if major_currency is not None and major_currency == row.get("currency"):
            listing = {
                "quote_currency": quote_currency,
                "factor": factor,
                "last_quote_at": found[1].get("last_quote_at"),
            }
        break
    status = "conflict" if row.get("resolution_status") == "conflict" else "mapped"
    security = _with_listing_fields(
        row, **listing, resolution_status=status, resolution_basis="supplemental"
    )
    alias = _alias(position, row["security_id"], "mapped", "supplemental", mapping)
    return security, alias, None


def _issuer_id(issuer_lookup, meta, key):
    if issuer_lookup is not None:
        found = issuer_lookup(copy.deepcopy({**meta, "symbol": meta.get("symbol") or key}))
        if found is not None and (
            not isinstance(found, str)
            or not found.strip()
            or len(found.strip()) > MAX_ISSUER_ID_LENGTH
        ):
            raise ValueError("issuer_lookup must return a nonempty issuer id or null.")
        if found is not None:
            return found.strip()
    return "listing:" + key


def _resolved(position, found, status, basis, context):
    from .market_listings import instrument_type

    key, meta = found
    quote_currency, major_currency, factor = _listing_currency(meta, key)
    if quote_currency is None:
        # The listing resolved but states no currency of its own, so its ticker does --
        # the same venue table an unresolved symbol falls back to, consulted here as well
        # so both paths give one answer. Without this a Toronto listing whose payload
        # carried only an exchange would present its CAD price as a USD price.
        quote_currency, major_currency, factor = venue_currency(key)
    issuer_ids = context["issuer_ids"]
    if key not in issuer_ids:
        issuer_ids[key] = _issuer_id(context["issuer_lookup"], meta, key)
    captured = context["rows"].get(position["security_id"]) or {}
    name = meta.get("name") if isinstance(meta.get("name"), str) else None
    security = _with_listing_fields(
        _base_row(context["columns"], key, key),
        quote_currency,
        factor,
        meta.get("last_quote_at"),
        issuer_id=issuer_ids[key],
        name=name or captured.get("name"),
        instrument_type=instrument_type(meta.get("instrument_type")),
        currency=major_currency,
        exchange=meta.get("exchange"),
        domicile=None,
        equity_type=None,
        eligible=False,
        resolution_status=status,
        resolution_basis=basis,
    )
    return security, _alias(position, key, status, basis, exchange=meta.get("exchange")), None


def _search_candidates(search, raw_symbol):
    found = search(raw_symbol)
    if found is None:
        return []
    if not isinstance(found, (list, tuple)):
        raise ValueError("search must return a list of listing candidates.")
    candidates = []
    for item in found:
        if not isinstance(item, dict) or _optional_text(item.get("symbol"), "symbol") is None:
            raise ValueError("search candidates must identify a listing symbol.")
        candidates.append(
            {
                "symbol": item["symbol"].strip(),
                "name": item.get("name"),
                "exchange": item.get("exchange"),
                "instrument_type": item.get("instrument_type"),
            }
        )
    return candidates


def _open(position, candidates, context):
    """No listing could be established: keep the captured row and raise an exception."""
    raw_symbol, quote_symbol = position["raw_symbol"], position.get("quote_symbol")
    ambiguous = len(candidates) > 1
    status = "ambiguous" if ambiguous else "unresolved"
    code = "AMBIGUOUS_LISTING" if ambiguous else "UNRESOLVED_LISTING"
    captured = context["rows"].get(position["security_id"])
    row = (
        copy.deepcopy(captured)
        if captured is not None
        else _base_row(context["columns"], position["security_id"], raw_symbol)
    )
    # No listing states this symbol's currency, so its ticker does: the venue suffix is
    # the owner's stated rule and answers while this exception stays open. An ambiguous
    # symbol is the exception -- several venues match it, so its own suffix names none.
    quote_currency, major_currency, factor = (
        (None, None, None) if ambiguous else venue_currency(quote_symbol or raw_symbol)
    )
    security = _with_listing_fields(
        row,
        quote_currency,
        factor,
        currency=row.get("currency") or major_currency,
        eligible=False,
        resolution_status=status,
        resolution_basis=None,
    )
    exception = _open_exception(position, candidates, code)
    return security, _alias(position, position["security_id"], status, None), exception


def _open_message(raw_symbol, quote_symbol, candidates):
    if len(candidates) > 1:
        return f"{raw_symbol} matches {len(candidates)} listings; choose the exact listing."
    if quote_symbol:
        return (
            f"No listing metadata is available for quote symbol {quote_symbol}; refresh "
            "market data or enter the exact listing."
        )
    return f"No listing was found for {raw_symbol}; enter the exact quote symbol."


def _open_exception(position, candidates, code):
    raw_symbol = position["raw_symbol"]
    return {
        "key": exception_key(code, position["source_id"], position.get("snapshot_id"), raw_symbol),
        "code": code,
        "severity": "error",
        "scope": "listing",
        "source_id": position["source_id"],
        "snapshot_id": position.get("snapshot_id"),
        "account_id": position.get("account_id"),
        "raw_symbol": raw_symbol,
        "message": _open_message(raw_symbol, position.get("quote_symbol"), candidates),
        "proposed": None,
        "candidates": candidates,
        "resolution": {"kind": "security_listing", "fields": list(LISTING_RESOLUTION_FIELDS)},
    }


def _resolve_position(position, context):
    mapped = _mapped(position, context)
    if mapped is not None:
        return mapped
    quote_symbol = position.get("quote_symbol")
    found = _find_listing(context["listings"], quote_symbol or position["raw_symbol"])
    if found is not None:
        if quote_symbol:
            return _resolved(position, found, "resolved", "quote_symbol", context)
        return _resolved(position, found, "resolved_from_display", "display_symbol", context)
    candidates = []
    # A captured quote symbol is exact; display-text search could only substitute a guess.
    if not quote_symbol and context["search"] is not None:
        candidates = _search_candidates(context["search"], position["raw_symbol"])
        wanted = position["raw_symbol"].strip().upper()
        exact = [row for row in candidates if row["symbol"].upper() == wanted]
        found = _find_listing(context["listings"], exact[0]["symbol"]) if len(exact) == 1 else None
        if found is not None:
            return _resolved(position, found, "resolved_by_search", "search_exact_match", context)
    return _open(position, candidates, context)


def _keep_security(securities, security):
    """One row per security id; the strongest basis (owner mapping first) wins."""
    order = {"supplemental": 0, "quote_symbol": 1, "search_exact_match": 2, "display_symbol": 3}
    current = securities.get(security["security_id"])
    if current is None or order.get(security["resolution_basis"], 9) < order.get(
        current["resolution_basis"], 9
    ):
        securities[security["security_id"]] = security


def _validate_providers(listings, search, issuer_lookup) -> None:
    if not isinstance(listings, dict):
        raise ValueError("listings must map listing symbols to metadata objects.")
    for key, meta in listings.items():
        if not isinstance(key, str) or not isinstance(meta, dict):
            raise ValueError("listings must map listing symbols to metadata objects.")
    for name, provider in (("search", search), ("issuer_lookup", issuer_lookup)):
        if provider is not None and not callable(provider):
            raise ValueError(f"{name} must be callable or null.")


def resolve_identities(ledger, supplemental, *, listings, search, issuer_lookup) -> dict:
    """Resolve each distinct ``(source_id, raw_symbol, quote_symbol)`` held in the ledger.

    Returns ``{"securities", "aliases", "exceptions", "counts"}``. Security rows carry the
    adapter's securities columns plus ``quote_currency``, ``quote_unit_factor``,
    ``last_quote_at`` and ``resolution_basis``. Aliases map every source symbol to its
    security id for the caller to apply. Inputs are never mutated.
    """
    from .adapter import FRAME_COLUMNS, validate_supplemental

    positions, rows = _validated_positions(ledger)
    _validate_providers(listings, search, issuer_lookup)
    context = {
        "columns": FRAME_COLUMNS["securities"],
        "rows": rows,
        "listings": listings,
        "search": search,
        "issuer_lookup": issuer_lookup,
        "issuer_ids": {},
        "mappings": validate_supplemental({} if supplemental is None else supplemental)[
            "securities"
        ],
    }
    securities, aliases, exceptions, seen = {}, [], [], set()
    for position in positions:
        identity = (position["source_id"], position["raw_symbol"], position.get("quote_symbol"))
        if identity in seen:
            continue
        seen.add(identity)
        security, alias, exception = _resolve_position(position, context)
        _keep_security(securities, security)
        aliases.append(alias)
        if exception is not None:
            exceptions.append(exception)
    counts = dict.fromkeys(COUNT_KEYS, 0)
    for alias in aliases:
        counts[alias["resolution_status"]] += 1
    result = {
        "securities": list(securities.values()),
        "aliases": aliases,
        "exceptions": exceptions,
        "counts": counts,
    }
    return copy.deepcopy(result)
