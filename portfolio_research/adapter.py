"""Read completed collector publications without opening the writable Store.

Decimal strings in ``ledger`` are the archival input. DataFrames are a deliberate
numerical projection, never a monetary ledger or a replacement for provenance.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import date, datetime, time, timezone
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

VERSION = 1
FRAME_COLUMNS = {
    "accounts": [
        "account_id",
        "account_type",
        "currency",
        "total_value",
        "cash",
        "complete",
        "tax_rate",
        "valuation_date",
    ],
    "positions": [
        "account_id",
        "security_id",
        "quantity",
        "price",
        "market_value",
        "currency",
        "reported_weight",
        "valuation_date",
        "snapshot_complete",
    ],
    "securities": [
        "security_id",
        "ticker",
        "issuer_id",
        "name",
        "sector",
        "instrument_type",
        "currency",
        "cik",
        "eligible",
        "market_cap",
        "market_cap_as_of",
        "market_cap_available_at",
        "market_cap_received_at",
        "exchange",
        "share_class",
        "domicile",
        "equity_type",
        "resolution_status",
    ],
    "tax_lots": [
        "account_id",
        "security_id",
        "lot_id",
        "acquired_date",
        "quantity",
        "basis_per_share",
        "currency",
    ],
}
NUMERIC_COLUMNS = {
    "accounts": ["total_value", "cash", "tax_rate"],
    "positions": ["quantity", "price", "market_value", "reported_weight"],
    "securities": ["market_cap"],
    "tax_lots": ["quantity", "basis_per_share"],
}
FIELDS = {
    "accounts": {
        "source_id",
        "snapshot_id",
        "name",
        "account_type",
        "currency",
        "position_currency",
        "cash",
        "total_value",
        "complete",
        "valuation_date",
        "tax_rate",
        "tax_jurisdiction",
    },
    "securities": {
        "source_id",
        "raw_symbol",
        "security_id",
        "issuer_id",
        "name",
        "ticker",
        "sector",
        "instrument_type",
        "currency",
        "cik",
        "eligible",
        "market_cap",
        "market_cap_as_of",
        "market_cap_available_at",
        "market_cap_received_at",
        "exchange",
        "share_class",
        "domicile",
        "equity_type",
        "valid_from",
        "valid_to",
    },
    "tax_lots": {
        "source_id",
        "snapshot_id",
        "security_id",
        "lot_id",
        "acquired_date",
        "quantity",
        "basis_per_share",
        "currency",
    },
}


def _issue(issues, code, message, severity="error"):
    issues.append({"code": code, "message": message, "severity": severity})


def _iso_date(value, label, nullable=True):
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError(f"{label} must be an ISO calendar date or null.")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise ValueError(f"{label} is not a valid calendar date.") from exc


def _decimal(value, label, nullable=True):
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError(f"{label} must be a decimal string, number, or null.")
    try:
        text = str(value)
        number = Decimal(text)
        if (
            len(text) > 128
            or not number.is_finite()
            or abs(number.as_tuple().exponent) > 128
            or number.adjusted() > 128
        ):
            raise InvalidOperation
    except InvalidOperation as exc:
        raise ValueError(f"{label} must be a finite decimal of at most 128 digits.") from exc
    return format(number, "f")


def _timestamp(value, label):
    if value is None:
        return None
    try:
        if not isinstance(value, str) or "T" not in value:
            raise ValueError
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
        return parsed.astimezone(timezone.utc).isoformat()
    except ValueError as exc:
        raise ValueError(
            f"{label} requires a full timestamp with a timezone offset, or null."
        ) from exc


def _identifier(value, label, nullable=True):
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value):
        raise ValueError(
            f"{label} must be a stable identifier (letters, numbers, dot, colon, dash, underscore)."
        )
    return value


def validate_supplemental(payload):
    """Validate versioned evidence; nulls remain useful incomplete draft fields.

    Account balances and lots bind an exact collector snapshot, so future pulls
    never inherit old cash, NAV, or a declaration of completeness accidentally.
    ``position_currency`` explicitly attests the unit of otherwise unlabeled
    source values; ordinary account currency does not perform FX conversion.
    """
    if not isinstance(payload, dict) or set(payload) - {"version", *FIELDS}:
        raise ValueError(
            "Supplemental data must contain only version, accounts, securities, and tax_lots."
        )
    if (
        type(payload.get("version", VERSION)) is not int
        or payload.get("version", VERSION) != VERSION
    ):
        raise ValueError("Unsupported supplemental schema version; expected 1.")
    result = {"version": VERSION}
    for group, allowed in FIELDS.items():
        records = payload.get(group, [])
        if not isinstance(records, list) or len(records) > 50000:
            raise ValueError(f"{group} must be a list containing at most 50,000 records.")
        result[group] = []
        seen = set()
        for index, record in enumerate(records):
            label = f"{group}[{index}]"
            if not isinstance(record, dict) or set(record) - allowed:
                raise ValueError(f"{label} contains unsupported fields.")
            row = copy.deepcopy(record)
            if type(row.get("source_id")) is not int or row["source_id"] < 1:
                raise ValueError(f"{label}.source_id must identify an existing collector account.")
            if group != "securities":
                sid = row.get("snapshot_id")
                if sid is not None and (type(sid) is not int or sid < 1):
                    raise ValueError(f"{label}.snapshot_id must be a positive integer or null.")
            for field, value in list(row.items()):
                field_label = f"{label}.{field}"
                if field in {"source_id", "snapshot_id"}:
                    continue
                if field in {"complete", "eligible"}:
                    if value is not None and type(value) is not bool:
                        raise ValueError(f"{field_label} must be true, false, or null.")
                elif field in {
                    "cash",
                    "total_value",
                    "quantity",
                    "basis_per_share",
                    "market_cap",
                    "tax_rate",
                }:
                    row[field] = _decimal(value, field_label)
                    if row[field] is not None and Decimal(row[field]) < 0:
                        raise ValueError(f"{field_label} cannot be negative.")
                    if field == "tax_rate" and row[field] is not None and Decimal(row[field]) > 1:
                        raise ValueError(f"{field_label} must be a fraction between 0 and 1.")
                elif field in {
                    "valuation_date",
                    "valid_from",
                    "valid_to",
                    "acquired_date",
                    "market_cap_as_of",
                }:
                    row[field] = _iso_date(value, field_label)
                elif field in {"market_cap_available_at", "market_cap_received_at"}:
                    row[field] = _timestamp(value, field_label)
                elif field in {"currency", "position_currency"}:
                    if value is not None and (
                        not isinstance(value, str) or not re.fullmatch(r"[A-Z]{3}", value)
                    ):
                        raise ValueError(
                            f"{field_label} must be an uppercase three-letter currency or null."
                        )
                elif field in {"security_id", "issuer_id", "lot_id"}:
                    row[field] = _identifier(value, field_label)
                elif field == "domicile":
                    if value is not None and (
                        not isinstance(value, str) or not re.fullmatch(r"[A-Z]{2}", value)
                    ):
                        raise ValueError(
                            f"{field_label} must be an uppercase two-letter country code or null."
                        )
                elif value is not None and (
                    not isinstance(value, str)
                    or not value.strip()
                    or len(value) > 500
                    or any(ord(c) < 32 for c in value)
                ):
                    raise ValueError(
                        f"{field_label} must be nonempty text of at most 500 characters or null."
                    )
            if group == "securities":
                if not isinstance(row.get("raw_symbol"), str) or not row["raw_symbol"].strip():
                    raise ValueError(f"{label}.raw_symbol is required.")
                if (
                    row.get("valid_from")
                    and row.get("valid_to")
                    and row["valid_to"] < row["valid_from"]
                ):
                    raise ValueError(f"{label} alias dates are reversed.")
                key = (
                    row["source_id"],
                    row["raw_symbol"],
                    row.get("valid_from"),
                    row.get("valid_to"),
                )
            elif group == "tax_lots":
                for required in (
                    "snapshot_id",
                    "security_id",
                    "lot_id",
                    "acquired_date",
                    "quantity",
                    "basis_per_share",
                    "currency",
                ):
                    if row.get(required) is None:
                        raise ValueError(f"{label}.{required} is required for an open tax lot.")
                if Decimal(row["quantity"]) <= 0:
                    raise ValueError(f"{label}.quantity must be positive for an open lot.")
                key = (row["source_id"], row["snapshot_id"], row["lot_id"])
            else:
                key = (row["source_id"], row.get("snapshot_id"))
                if row.get("complete") is True and any(
                    row.get(k) is None
                    for k in ("snapshot_id", "valuation_date", "cash", "total_value", "currency")
                ):
                    raise ValueError(
                        f"{label}: complete accounts require snapshot_id, valuation_date, cash, total_value, and currency."
                    )
            if key in seen:
                raise ValueError(f"{label} duplicates an existing record identity.")
            seen.add(key)
            result[group].append(row)
    return result


def _opaque(kind, *parts):
    return (
        kind
        + "_"
        + uuid.uuid5(
            uuid.NAMESPACE_URL, "portfolio-review:collector:" + ":".join(map(str, parts))
        ).hex
    )


def account_id(source_id):
    return _opaque("account", source_id)


def _authorize(action, first, second, database, trigger):
    allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION}
    if action == sqlite3.SQLITE_FUNCTION and str(second or first).lower() in {
        "load_extension",
        "readfile",
        "writefile",
        "edit",
        "shell",
        "eval",
    }:
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK if action in allowed else sqlite3.SQLITE_DENY


@contextmanager
def _read_source(source_path):
    source = Path(source_path).expanduser().resolve(strict=True)
    if not source.is_file():
        raise ValueError("The collector source must be an existing SQLite file.")
    db = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=15)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA query_only=ON")
        db.execute("PRAGMA trusted_schema=OFF")
        db.execute("BEGIN")
        objects = dict(db.execute("SELECT name,type FROM sqlite_schema"))
        if any(objects.get(name) != "table" for name in ("sources", "snapshots")):
            raise ValueError(
                "Expected collector sources and snapshots tables; source schema was not changed."
            )
        if "import_batches" in objects and any(
            objects.get(name) != "table" for name in ("import_batches", "import_batch_members")
        ):
            raise ValueError("Invalid collector batch schema.")
        db.set_authorizer(_authorize)
        budget = [20000]

        def bounded_query():
            budget[0] -= 1
            return int(budget[0] <= 0)

        db.set_progress_handler(bounded_query, 1000)
        yield db, objects
    finally:
        db.close()


def assert_separate_store(source_path, research_path):
    """Reject symlink and hardlink aliases before opening a writable research DB."""
    source, research = (
        Path(source_path).expanduser().resolve(),
        Path(research_path).expanduser().resolve(),
    )
    if source == research or (source.exists() and research.exists() and source.samefile(research)):
        raise ValueError("Research storage must be separate from the read-only collector database.")


def _receipt_before(value, cutoff):
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return moment.tzinfo is not None and moment <= cutoff
    except (AttributeError, TypeError, ValueError):
        return False


def _read_snapshot_set(source_path, as_of, account_ids=None):
    cutoff = datetime.combine(date.fromisoformat(as_of), time.max, ZoneInfo("America/New_York"))
    with _read_source(source_path) as (db, objects):
        sources = [
            dict(row) for row in db.execute("SELECT id,name,selected FROM sources ORDER BY id")
        ]
        if account_ids is None:
            chosen = {row["id"] for row in sources if row["selected"]}
        else:
            if not isinstance(account_ids, (list, tuple, set)):
                raise ValueError(
                    "account_ids must be a list of collector source IDs or canonical account IDs."
                )
            lookup = {account_id(row["id"]): row["id"] for row in sources}
            chosen = set()
            for value in account_ids:
                source_id = value if type(value) is int else lookup.get(value)
                if source_id is None or source_id not in {row["id"] for row in sources}:
                    raise ValueError("An account ID is not present in the collector.")
                chosen.add(source_id)
        sources = [row for row in sources if row["id"] in chosen]
        selected_batch, snapshots, newer_failed = None, {}, False
        if chosen and "import_batches" in objects:
            batches = [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM import_batches ORDER BY created_at DESC,id DESC"
                )
            ]
            for batch in batches:
                requested = set(json.loads(batch["requested_sources_json"]))
                if not chosen.issubset(requested) or not _receipt_before(
                    batch["created_at"], cutoff
                ):
                    continue
                if batch["status"] != "published":
                    newer_failed = True
                    continue
                if not _receipt_before(batch["completed_at"], cutoff):
                    continue
                members = [
                    dict(row)
                    for row in db.execute(
                        "SELECT m.source_id,m.received_at,m.status,s.id,s.captured_at,s.sha256,s.rows_json,s.capture_json FROM import_batch_members m JOIN snapshots s ON s.id=m.snapshot_id AND s.source_id=m.source_id WHERE m.batch_id=?",
                        (batch["id"],),
                    )
                ]
                if (
                    len(members) != len(requested)
                    or {row["source_id"] for row in members} != requested
                    or any(row["status"] != "success" for row in members)
                ):
                    newer_failed = True
                    continue
                selected_batch = {
                    key: batch[key]
                    for key in ("id", "created_at", "completed_at", "status", "completeness")
                }
                snapshots = {row["source_id"]: row for row in members if row["source_id"] in chosen}
                break
        if selected_batch is None:
            # Display existing holdings honestly; legacy per-account history is
            # not upgraded into a made-up complete portfolio publication.
            for source in sources:
                for row in db.execute(
                    "SELECT id,source_id,captured_at,sha256,rows_json,capture_json FROM snapshots WHERE source_id=? ORDER BY id DESC",
                    (source["id"],),
                ):
                    if _receipt_before(row["captured_at"], cutoff):
                        snapshots[source["id"]] = {**dict(row), "received_at": row["captured_at"]}
                        break
        return sources, snapshots, selected_batch, newer_failed


def _frames(ledger, issues):
    frames = {}
    for name, columns in FRAME_COLUMNS.items():
        frame = pd.DataFrame(ledger[name])
        for column in columns:
            if column not in frame:
                frame[column] = None
        for column in NUMERIC_COLUMNS[name]:
            values = []
            for value in frame[column]:
                converted = float(value) if value is not None else math.nan
                if not math.isfinite(converted) and value is not None:
                    _issue(
                        issues,
                        "NUMERICAL_RANGE",
                        f"{name}.{column} is outside the finite analytical range.",
                    )
                    converted = math.nan
                values.append(converted)
            frame[column] = pd.Series(values, dtype=float)
        frames[name] = frame
    return frames


def load_collector(source_path, as_of, supplemental=None, account_ids=None):
    """Map one exact collector publication and separately supplied evidence.

    Legacy holdings remain inspectable with blocking issues. ``as_of`` limits
    source receipts through that New York calendar date; it never turns receipt
    time into a quote date. Decision cutoff policy belongs to orchestration.
    """
    as_of = _iso_date(as_of, "as_of", nullable=False)
    supplemental = validate_supplemental({} if supplemental is None else supplemental)
    sources, snapshots, batch, fallback = _read_snapshot_set(source_path, as_of, account_ids)
    issues, provenance = [], []
    ledger = {name: [] for name in FRAME_COLUMNS}
    ledger.update(captures=[], excluded_rows=[], security_aliases=[])
    if not sources:
        _issue(issues, "NO_SELECTED_ACCOUNTS", "Select collector accounts before running a review.")
    if sources and batch is None:
        _issue(
            issues,
            "NO_COMPLETE_BATCH",
            "Existing account snapshots have no common published collection batch. Holdings are shown as legacy partial input; refresh the selected accounts together.",
        )
    if fallback:
        _issue(
            issues,
            "PRIOR_COMPLETE_BATCH",
            "A newer collection did not publish the full account scope. The prior published batch is used with its original dates.",
            "warning",
        )
    by_account = {
        (row["source_id"], row.get("snapshot_id")): row for row in supplemental["accounts"]
    }
    security_rows, selected_dates = {}, []
    for source in sources:
        source_id, aid = source["id"], account_id(source["id"])
        snapshot = snapshots.get(source_id)
        snapshot_id = snapshot["id"] if snapshot else None
        account_evidence = by_account.get((source_id, snapshot_id), {})
        if not account_evidence and any(
            row["source_id"] == source_id for row in supplemental["accounts"]
        ):
            _issue(
                issues,
                "STALE_ACCOUNT_SUPPLEMENT",
                "Supplemental account facts refer to a different snapshot and were not applied.",
            )
        valuation = account_evidence.get("valuation_date")
        if valuation and valuation > as_of:
            _issue(
                issues,
                "FUTURE_VALUATION",
                "An account valuation date is after the decision date; its supplied balances and date were not applied.",
            )
            account_evidence, valuation = {}, None
        selected_dates.append(valuation)
        capture = json.loads(snapshot["capture_json"] or "null") if snapshot else None
        rows = json.loads(snapshot["rows_json"]) if snapshot else []
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError("A collector snapshot contains invalid row records.")
        if not snapshot:
            _issue(
                issues,
                "MISSING_ACCOUNT_SNAPSHOT",
                "A selected account has no snapshot received on or before the requested date.",
            )
        elif (capture or {}).get("completeness") != "count-verified":
            _issue(
                issues,
                "UNVERIFIED_CAPTURE",
                "Yahoo table coverage is unverified. Explicit account reconciliation evidence is required.",
                "warning" if account_evidence.get("complete") is True else "error",
            )
        if not valuation:
            _issue(
                issues,
                "UNKNOWN_VALUATION_DATE",
                "A capture/receipt timestamp is not a quote valuation date. Supply the dated snapshot evidence.",
            )
        cash_values, cash_currencies = [], []
        for row in rows:
            if (
                str(row.get("symbol", "")).strip().lower() == "total cash"
                and row.get("quantity") is None
            ):
                cash = row.get("market_value")
                if cash is None and (capture or {}).get("method") == "yahoo-holdings-table-v1":
                    cash = row.get("price")
                cash_values.append(_decimal(cash, "captured cash"))
                cash_currencies.append(
                    row.get("currency") or account_evidence.get("position_currency")
                )
        observed_cash = cash_values[0] if len(cash_values) == 1 else None
        observed_cash_currency = cash_currencies[0] if len(cash_currencies) == 1 else None
        if len(cash_values) > 1:
            _issue(
                issues,
                "AMBIGUOUS_CASH",
                "Multiple cash rows prevent determining a unique account cash balance.",
            )
        currency = account_evidence.get("currency")
        cash_currency_mismatch = bool(
            observed_cash_currency and currency and observed_cash_currency != currency
        )
        if cash_currency_mismatch:
            _issue(
                issues,
                "CASH_CURRENCY_MISMATCH",
                "Captured cash uses a different currency from account balances; no FX conversion was inferred.",
            )
        cash = account_evidence.get("cash", observed_cash)
        if cash is None:
            cash = observed_cash
        if (
            account_evidence.get("cash") is not None
            and observed_cash is not None
            and Decimal(cash) != Decimal(observed_cash)
        ):
            _issue(
                issues,
                "CASH_MISMATCH",
                "Supplemental cash differs from the explicitly captured cash row; reconcile before allocation.",
            )
        nav = account_evidence.get("total_value")
        complete = bool(
            batch
            and account_evidence.get("complete") is True
            and valuation
            and currency
            and nav is not None
            and cash is not None
            and not cash_currency_mismatch
        )
        account = {
            "account_id": aid,
            "source_id": source_id,
            "snapshot_id": snapshot_id,
            "name": source["name"],
            "account_type": account_evidence.get("account_type"),
            "currency": currency,
            "total_value": nav,
            "cash": cash,
            "captured_cash": observed_cash,
            "captured_cash_currency": observed_cash_currency,
            "complete": complete,
            "tax_rate": account_evidence.get("tax_rate"),
            "tax_jurisdiction": account_evidence.get("tax_jurisdiction"),
            "valuation_date": valuation,
        }
        ledger["accounts"].append(account)
        if nav is None or cash is None:
            _issue(
                issues,
                "MISSING_ACCOUNT_TOTALS",
                "Explicit dated account NAV and cash are required; a holdings subtotal is not reported NAV.",
            )
        if currency is None:
            _issue(
                issues,
                "MISSING_CURRENCY",
                "An account has no explicit currency; no currency or FX rate was assumed.",
            )
        if not complete:
            _issue(
                issues,
                "INCOMPLETE_ACCOUNT_COVERAGE",
                "An account has not been reconciled as complete within a published batch.",
            )
        for row in rows:
            symbol = str(row.get("symbol", "")).strip()
            if symbol.lower() == "total cash" and row.get("quantity") is None:
                continue
            if row.get("quantity") is None and row.get("market_value") is None:
                ledger["excluded_rows"].append(
                    {
                        "account_id": aid,
                        "snapshot_id": snapshot_id,
                        "row_number": row.get("row_number"),
                        "symbol": symbol,
                        "reason": "No held quantity or value; retained as a watchlist/unknown holding record.",
                    }
                )
                continue
            mappings = [
                record
                for record in supplemental["securities"]
                if record["source_id"] == source_id
                and record["raw_symbol"] == symbol
                and (not record.get("valid_from") or record["valid_from"] <= (valuation or as_of))
                and (not record.get("valid_to") or record["valid_to"] >= (valuation or as_of))
            ]
            if len(mappings) > 1:
                _issue(
                    issues,
                    "AMBIGUOUS_SECURITY_ALIAS",
                    "Multiple security mappings apply to the same dated collector symbol.",
                )
            mapping = mappings[0] if len(mappings) == 1 else {}
            security_id = mapping.get("security_id") or _opaque("unresolved", source_id, symbol)
            ledger["security_aliases"].append(
                {
                    "source_id": source_id,
                    "raw_symbol": symbol,
                    "security_id": security_id,
                    "valid_from": mapping.get("valid_from"),
                    "valid_to": mapping.get("valid_to"),
                    "exchange": mapping.get("exchange"),
                    "share_class": mapping.get("share_class"),
                    "snapshot_id": snapshot_id,
                }
            )
            resolved = bool(
                mapping.get("security_id")
                and mapping.get("issuer_id")
                and mapping.get("valid_from")
            )
            if not resolved:
                _issue(
                    issues,
                    "UNRESOLVED_SECURITY",
                    "A held source symbol lacks an explicit dated security/issuer mapping. Separate share classes and unknown plan funds remain separate.",
                )
            security = {field: mapping.get(field) for field in FRAME_COLUMNS["securities"]}
            security.update(
                security_id=security_id,
                ticker=mapping.get("ticker") or symbol,
                name=mapping.get("name") or row.get("name"),
                eligible=bool(resolved and mapping.get("eligible") is True),
                resolution_status="resolved" if resolved else "unresolved",
            )
            if security_id in security_rows and security_rows[security_id] != security:
                _issue(
                    issues,
                    "CONFLICTING_SECURITY_IDENTITY",
                    "Different source mappings disagree about a shared security identity.",
                )
                security_rows[security_id]["resolution_status"] = "conflict"
                security_rows[security_id]["eligible"] = False
            else:
                security_rows[security_id] = security
            position = {
                "account_id": aid,
                "security_id": security_id,
                "source_id": source_id,
                "snapshot_id": snapshot_id,
                "row_number": row.get("row_number"),
                "raw_symbol": symbol,
                "quantity": _decimal(row.get("quantity"), "quantity"),
                "price": _decimal(row.get("price"), "price"),
                "market_value": _decimal(row.get("market_value"), "market_value"),
                "currency": row.get("currency") or account_evidence.get("position_currency"),
                "reported_weight": None,
                "valuation_date": valuation,
                "snapshot_complete": complete,
                "average_cost": _decimal(row.get("average_cost"), "average_cost"),
                "total_cost": _decimal(row.get("total_cost"), "total_cost"),
            }
            if position["currency"] is None:
                _issue(
                    issues,
                    "MISSING_POSITION_CURRENCY",
                    "A holding's value currency is unknown; an account label or security trading currency is not a conversion.",
                )
            if position["market_value"] is None:
                _issue(
                    issues,
                    "MISSING_POSITION_VALUE",
                    "Some held quantities lack market values; portfolio totals cannot be completed.",
                )
            if position["quantity"] is None:
                _issue(
                    issues,
                    "MISSING_QUANTITY",
                    "A value-bearing holding lacks quantity; sizing and lot matching are unavailable.",
                )
            ledger["positions"].append(position)
        if snapshot:
            clean_capture = {
                key: (capture or {}).get(key)
                for key in ("method", "completeness", "expected_count", "page_count", "row_count")
            }
            record = {
                "source_id": _opaque("capture", source_id, snapshot_id),
                "provider": "yahoo_collector",
                "account_id": aid,
                "snapshot_id": snapshot_id,
                "sha256": snapshot["sha256"],
                "captured_at": snapshot["captured_at"],
                "received_at": snapshot["received_at"],
                "valuation_date": valuation,
                "read_only": True,
                "capture": clean_capture,
            }
            ledger["captures"].append(record)
            provenance.append(record)
    ledger["securities"] = list(security_rows.values())
    source_accounts = {row["source_id"]: row for row in ledger["accounts"]}
    for lot in supplemental["tax_lots"]:
        account = source_accounts.get(lot["source_id"])
        if not account or lot["snapshot_id"] != account["snapshot_id"]:
            _issue(
                issues,
                "STALE_TAX_LOT",
                "An open-lot record is outside the selected account snapshot and was not applied.",
            )
            continue
        if lot["acquired_date"] > (account["valuation_date"] or as_of):
            _issue(
                issues,
                "FUTURE_TAX_LOT",
                "A lot acquisition is after the selected valuation date and was not applied.",
            )
            continue
        if not any(
            position["account_id"] == account["account_id"]
            and position["security_id"] == lot["security_id"]
            for position in ledger["positions"]
        ):
            _issue(
                issues,
                "UNMATCHED_TAX_LOT",
                "An open-lot identity has no corresponding position in this snapshot.",
            )
            continue
        ledger["tax_lots"].append({**lot, "account_id": account["account_id"]})
    # Exact decimal boundary check precedes conversion to numerical arrays.
    with localcontext() as context:
        context.prec = 512
        for account in ledger["accounts"]:
            held = [
                row for row in ledger["positions"] if row["account_id"] == account["account_id"]
            ]
            if (
                account["total_value"] is not None
                and account["cash"] is not None
                and all(
                    row["market_value"] is not None and row["currency"] == account["currency"]
                    for row in held
                )
            ):
                residual = (
                    Decimal(account["total_value"])
                    - Decimal(account["cash"])
                    - sum((Decimal(row["market_value"]) for row in held), Decimal(0))
                )
                account["reconciliation_residual"] = format(residual, "f")
                if abs(residual) > Decimal("0.01"):
                    _issue(
                        issues,
                        "NAV_MISMATCH",
                        "Explicit NAV does not reconcile to captured holdings plus explicit cash within one cent.",
                    )
                    account["complete"] = False
            if any(
                row["market_value"] is None or row["currency"] != account["currency"]
                for row in held
            ):
                account["complete"] = False
    common_date = (
        selected_dates[0]
        if selected_dates
        and all(value == selected_dates[0] and value is not None for value in selected_dates)
        else None
    )
    if selected_dates and common_date is None:
        _issue(
            issues,
            "NO_COMMON_VALUATION_DATE",
            "Selected accounts lack one common explicit valuation date; dates were not blended.",
        )
        for account in ledger["accounts"]:
            account["complete"] = False
    completeness = {account["account_id"]: account["complete"] for account in ledger["accounts"]}
    for position in ledger["positions"]:
        position["snapshot_complete"] = completeness[position["account_id"]]
    counts = {}
    for row in ledger["positions"]:
        key = (row["account_id"], row["security_id"])
        counts[key] = counts.get(key, 0) + 1
    if any(count > 1 for count in counts.values()):
        _issue(
            issues,
            "DUPLICATE_POSITIONS",
            "Multiple rows share account/security identity. Exact source records remain separate until their lot/position semantics are reconciled.",
        )
    # De-duplicate general missing-data messages without losing any blocked gate.
    issues = list(
        {(issue["code"], issue["message"], issue["severity"]): issue for issue in issues}.values()
    )
    frames = _frames(ledger, issues)
    normalized_hash = hashlib.sha256(
        json.dumps(ledger, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()
    return {
        **frames,
        "as_of": as_of,
        "valuation_date": common_date,
        "mode": "offline",
        "scope": "selected_accounts",
        "ledger": ledger,
        "issues": issues,
        "sources": provenance,
        "collector": {
            "batch": batch,
            "legacy_partial": batch is None,
            "account_count": len(sources),
            "normalized_sha256": normalized_hash,
            "schema_version": VERSION,
        },
    }


def supplemental_template(source_path, account_ids=None):
    """Return an editable, private local UI shape containing no paths/URLs/keys."""
    today = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    sources, snapshots, _, _ = _read_snapshot_set(source_path, today, account_ids)
    template = {"version": VERSION, "accounts": [], "securities": [], "tax_lots": []}
    for source in sources:
        snapshot = snapshots.get(source["id"])
        template["accounts"].append(
            {
                "source_id": source["id"],
                "snapshot_id": snapshot["id"] if snapshot else None,
                "name": source["name"],
                "account_type": None,
                "currency": None,
                "position_currency": None,
                "valuation_date": None,
                "cash": None,
                "total_value": None,
                "complete": False,
                "tax_jurisdiction": None,
                "tax_rate": None,
            }
        )
        seen = set()
        for row in json.loads(snapshot["rows_json"]) if snapshot else []:
            symbol = row.get("symbol")
            if (
                symbol in seen
                or (str(symbol).lower() == "total cash" and row.get("quantity") is None)
                or (row.get("quantity") is None and row.get("market_value") is None)
            ):
                continue
            seen.add(symbol)
            template["securities"].append(
                {
                    "source_id": source["id"],
                    "raw_symbol": symbol,
                    "security_id": None,
                    "issuer_id": None,
                    "name": row.get("name"),
                    "ticker": symbol,
                    "instrument_type": None,
                    "domicile": None,
                    "equity_type": None,
                    "market_cap": None,
                    "market_cap_as_of": None,
                    "market_cap_available_at": None,
                    "market_cap_received_at": None,
                    "sector": None,
                    "share_class": None,
                    "exchange": None,
                    "currency": None,
                    "eligible": False,
                    "valid_from": None,
                    "valid_to": None,
                }
            )
    return template
