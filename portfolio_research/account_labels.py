"""Account labels for presentation; opaque identities and frozen inputs stay intact."""

import re
from collections import Counter
from copy import deepcopy

from portfolio_lab.analytics import json_safe

OPAQUE_NAME = re.compile(r"(?:account_[0-9a-f]{6,}|[0-9a-f]{16,}|[0-9a-f-]{36})", re.I)
NARRATIVE_FIELDS = frozenset({"message", "reason", "rationale", "description", "label", "why"})


def _label(value):
    """A useful source label, excluding identifiers and accidental private strings."""
    from .public import _safe_text

    if not isinstance(value, str):
        return None
    value = value.strip()
    if (
        not value
        or len(value) > 200
        or any(ord(character) < 32 for character in value)
        or OPAQUE_NAME.fullmatch(value)
        or _safe_text(value) != value
    ):
        return None
    return value


def validate_alias(value):
    """``None`` clears an alias; a supplied label is bounded, plain text."""
    if value is None:
        return None
    result = _label(value)
    if result is None:
        raise ValueError(
            "Use an account name of 1–200 characters, without identifiers or credentials."
        )
    return result


def _rows(value):
    if hasattr(value, "to_dict"):
        value = value.to_dict("records")
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def _account_ids(value):
    if isinstance(value, dict):
        if value.get("account_id") is not None:
            yield str(value["account_id"])
        for item in value.values():
            yield from _account_ids(item)
    elif isinstance(value, list):
        for item in value:
            yield from _account_ids(item)


def _order(row):
    source_id = row.get("source_id")
    return (0, source_id) if type(source_id) is int else (1, str(row["account_id"]))


def account_catalog(result=None, bundle=None, *, aliases=None):
    """Prefer retained source names; stable numbered labels cover unnamed accounts.

    Names supplement identity only. They cannot infer brokerage, tax treatment or cash.
    Explicit aliases are live display preferences, separate from the saved analysis.
    """
    result, bundle = result or {}, bundle or {}
    records = {identifier: {"account_id": identifier} for identifier in _account_ids(result)}
    groups = (
        (result.get("summary") or {}).get("accounts"),
        result.get("accounts"),
        bundle.get("accounts"),
        (bundle.get("ledger") or {}).get("accounts"),
    )
    for group in groups:
        for row in _rows(group):
            if row.get("account_id") is not None:
                identifier = str(row["account_id"])
                records.setdefault(identifier, {"account_id": identifier}).update(row)
    catalog = []
    for index, row in enumerate(sorted(records.values(), key=_order), start=1):
        identifier = str(row["account_id"])
        source_name = (
            _label(row.get("source_name")) if "source_name" in row else _label(row.get("name"))
        )
        alias = _label((aliases or {}).get(identifier))
        if aliases is None and row.get("label_source") == "alias":
            alias = _label(row.get("display_name"))
        number = row["source_id"] if type(row.get("source_id")) is int else index
        fallback = f"Account {number}"
        name = alias or source_name or fallback
        catalog.append(
            {
                "account_id": identifier,
                **({"source_id": row["source_id"]} if row.get("source_id") is not None else {}),
                "name": name,
                "display_name": name,
                "source_name": source_name,
                "label_source": "alias" if alias else "source" if source_name else "fallback",
                "alias": alias,
                "fallback_name": fallback,
            }
        )
    counts = Counter(row["display_name"] for row in catalog)
    for row in catalog:
        if counts[row["display_name"]] > 1:
            row["display_name"] = row["name"] = f"{row['display_name']} · {row['fallback_name']}"
    return catalog


def with_account_labels(result, bundle=None, *, aliases=None):
    """A copied presentation of current or saved data with labels on account records."""
    presented = json_safe(deepcopy(result))
    catalog = account_catalog(presented, bundle, aliases=aliases)
    labels = {row["account_id"]: row["display_name"] for row in catalog}
    metadata = {row["account_id"]: row for row in catalog}
    opaque = [identifier for identifier in labels if OPAQUE_NAME.fullmatch(identifier)]
    identifiers = (
        re.compile("|".join(re.escape(identifier) for identifier in opaque)) if opaque else None
    )

    def decorate(value):
        if isinstance(value, dict):
            identifier = str(value.get("account_id", ""))
            if identifier in labels:
                value["account_name"] = labels[identifier]
            for key, item in value.items():
                if key in NARRATIVE_FIELDS and isinstance(item, str) and identifiers is not None:
                    value[key] = identifiers.sub(lambda match: labels[match.group()], item)
                decorate(item)
        elif isinstance(value, list):
            for item in value:
                decorate(item)

    decorate(presented)
    # Current account rows also carry capture/FX detail; keep those fields available.
    accounts = _rows(presented.get("accounts"))
    if accounts:
        presented["accounts"] = [{**row, **metadata[str(row["account_id"])]} for row in accounts]
    else:
        presented["accounts"] = catalog
    presented["account_labels"] = labels
    return presented


def collector_accounts(source_path):
    """Account identity and source label only, from the read-only collector boundary."""
    from .adapter import _read_source, account_id

    with _read_source(source_path) as (connection, _):
        sources = connection.execute("SELECT id,name FROM sources ORDER BY id").fetchall()
    return [
        {"account_id": account_id(row["id"]), "source_id": row["id"], "name": row["name"]}
        for row in sources
    ]
