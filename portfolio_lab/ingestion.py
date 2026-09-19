"""Read-only portfolio import and immutable research archives.

The source database is never migrated or written. Queries may use ``:as_of``;
``column_map`` maps canonical column names to names returned by those queries.
Historical rows are reduced to one coherent date, never a latest-per-security
mixture. Unresolved data are carried as issues rather than invented holdings.
"""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import math
import os
import sqlite3
import uuid
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

FRAME_COLUMNS = {
    "positions": [
        "account_id",
        "security_id",
        "quantity",
        "price",
        "market_value",
        "currency",
        "reported_weight",
        "valuation_date",
    ],
    "accounts": [
        "account_id",
        "account_type",
        "currency",
        "total_value",
        "cash",
        "complete",
        "tax_rate",
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
        "exchange",
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


def _issue(issues, code, message, severity="error"):
    issues.append({"severity": severity, "code": code, "message": message})


def _resolved(path):
    return Path(os.path.expandvars(os.path.expanduser(str(path)))).resolve()


def _source_path(path):
    if not path:
        raise ValueError("Set source.path to your existing portfolio SQLite database.")
    result = _resolved(path)
    if not result.is_file():
        raise FileNotFoundError(
            f"Portfolio SQLite database does not exist: {result}. No demo data have been substituted."
        )
    return result


def _authorize(action, first, second, database, trigger):
    allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION}
    if hasattr(sqlite3, "SQLITE_RECURSIVE"):
        allowed.add(sqlite3.SQLITE_RECURSIVE)
    if action == sqlite3.SQLITE_FUNCTION and str(second or first).lower() in {
        "load_extension",
        "writefile",
        "readfile",
        "edit",
        "shell",
        "eval",
    }:
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK if action in allowed else sqlite3.SQLITE_DENY


def _open_source(path, guarded=True):
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=10)
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA trusted_schema=OFF")
    connection.execute("BEGIN")
    # Pin one read transaction before issuing several independent import queries.
    connection.execute("SELECT count(*) FROM sqlite_schema").fetchone()
    if guarded:
        connection.set_authorizer(_authorize)
    remaining = [20_000]

    def progress():
        remaining[0] -= 1
        return 1 if remaining[0] <= 0 else 0

    connection.set_progress_handler(progress, 1000)
    return connection


def inspect_database(path) -> dict:
    """Describe tables/views and columns without reading private row values."""
    source = _source_path(path)
    connection = _open_source(source, guarded=False)
    try:
        tables = []
        for name, kind in connection.execute(
            "SELECT name,type FROM sqlite_schema WHERE type IN ('table','view') "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ):
            escaped = name.replace('"', '""')
            columns = [
                {
                    "name": row[1],
                    "type": row[2],
                    "not_null": bool(row[3]),
                    "primary_key": bool(row[5]),
                }
                for row in connection.execute(f'PRAGMA table_info("{escaped}")')
            ]
            tables.append({"name": name, "type": kind, "columns": columns})
        return {"path": str(source), "read_only": True, "tables": tables}
    finally:
        connection.close()


def _read_frame(connection, query, name, column_map, as_of):
    if not query:
        return pd.DataFrame(columns=FRAME_COLUMNS[name])
    if not isinstance(query, str):
        raise ValueError(f"source.{name}_query must be a read-only SELECT string.")
    try:
        cursor = connection.execute(query, {"as_of": as_of})
        if cursor.description is None:
            raise ValueError("Query did not return a result set")
        columns = [item[0] for item in cursor.description]
        if len(set(columns)) != len(columns):
            raise ValueError("Query returns duplicate column names; use explicit aliases")
        frame = pd.DataFrame.from_records(cursor.fetchall(), columns=columns)
    except (sqlite3.Error, ValueError) as exc:
        raise ValueError(
            f"Cannot read source.{name}_query: {exc}. Only read-only SELECT queries are permitted."
        ) from exc
    mapping = column_map.get(name, {}) if column_map else {}
    if not isinstance(mapping, dict):
        raise ValueError(
            f"source.column_map.{name} must map canonical names to returned source columns."
        )
    for canonical, actual in mapping.items():
        if actual not in frame.columns:
            raise ValueError(
                f"Column mapping for {name}.{canonical} refers to missing returned column {actual!r}."
            )
        if canonical in frame.columns and canonical != actual:
            raise ValueError(
                f"Column mapping for {name}.{canonical} conflicts with a returned canonical column."
            )
    frame = frame.rename(columns={actual: canonical for canonical, actual in mapping.items()})
    if frame.columns.duplicated().any():
        raise ValueError(f"Column mappings create duplicate columns in {name}.")
    return frame


def _text(value):
    if value is None or (not isinstance(value, (list, dict)) and pd.isna(value)):
        return None
    result = str(value).strip()
    return result if result else None


def _bool(value, default=False):
    if value is None or pd.isna(value):
        return default
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "y"}
    return bool(value)


def _dates(frame, column, issues, name):
    if column not in frame:
        return frame
    parsed = pd.to_datetime(frame[column], errors="coerce", utc=True)
    invalid = parsed.isna()
    if invalid.any():
        _issue(
            issues,
            "INVALID_DATE",
            f"{name}: {int(invalid.sum())} rows have invalid or missing {column}; these rows are excluded.",
        )
    frame = frame.loc[~invalid].copy()
    frame[column] = parsed.loc[~invalid].dt.strftime("%Y-%m-%d")
    return frame


def _numeric(frame, columns, issues, name):
    for column in columns:
        if column not in frame:
            frame[column] = np.nan
            continue
        original = frame[column]
        parsed = pd.to_numeric(original, errors="coerce")
        invalid = (original.notna() & parsed.isna()) | (parsed.notna() & ~np.isfinite(parsed))
        if invalid.any():
            _issue(
                issues,
                "INVALID_NUMBER",
                f"{name}.{column}: {int(invalid.sum())} invalid numeric values retained as missing.",
            )
        frame[column] = parsed.where(np.isfinite(parsed), np.nan)
    return frame


def _unique(frame, keys, issues, name):
    if frame.empty or not all(key in frame for key in keys):
        return
    count = int(frame.duplicated(keys, keep=False).sum())
    if count:
        _issue(
            issues,
            "DUPLICATE_KEYS",
            f"{name}: {count} rows share {', '.join(keys)}. Do not aggregate or propose trades until resolved.",
        )


def _select_snapshot(positions, accounts, as_of, issues):
    positions = _dates(positions, "valuation_date", issues, "positions")
    positions = positions.loc[positions["valuation_date"] <= as_of].copy()
    accounts_dated = "valuation_date" in accounts.columns
    if accounts_dated:
        accounts = _dates(accounts, "valuation_date", issues, "accounts")
        accounts = accounts.loc[accounts["valuation_date"] <= as_of].copy()
        latest_accounts = accounts.sort_values("valuation_date").drop_duplicates(
            "account_id", keep="last"
        )
    else:
        latest_accounts = accounts.copy()
    if positions.empty:

        def explicit_cash_only(row):
            try:
                cash, nav = float(row.get("cash")), float(row.get("total_value"))
                return (
                    _bool(row.get("complete"))
                    and math.isfinite(cash)
                    and math.isfinite(nav)
                    and nav >= 0
                    and cash >= 0
                    and abs(nav - cash) < 0.005
                )
            except (TypeError, ValueError):
                return False

        if not latest_accounts.empty and all(
            explicit_cash_only(row) for row in latest_accounts.to_dict("records")
        ):
            selected = None
            if accounts_dated:
                expected_accounts = set(latest_accounts["account_id"].dropna())
                for candidate in sorted(accounts["valuation_date"].unique(), reverse=True):
                    rows = accounts.loc[accounts["valuation_date"] == candidate]
                    if expected_accounts.issubset(set(rows["account_id"].dropna())) and all(
                        explicit_cash_only(row) for row in rows.to_dict("records")
                    ):
                        latest_accounts, selected = rows.copy(), candidate
                        break
                if selected is None:
                    _issue(
                        issues,
                        "ACCOUNT_DATE_MISMATCH",
                        "Cash-only accounts have no common complete valuation date; their latest totals must not be treated as one portfolio snapshot.",
                    )
            else:
                _issue(
                    issues,
                    "ACCOUNT_DATE_UNVERIFIED",
                    "Cash-only account totals lack valuation_date; their effective date is unverified.",
                    "warning",
                )
            _issue(
                issues,
                "NO_SECURITIES_HELD",
                "No securities are reported. Explicit complete account totals equal explicit cash; cash has not been inferred.",
                "info",
            )
            return positions, latest_accounts, selected
        _issue(issues, "NO_POSITIONS", f"No portfolio positions exist on or before {as_of}.")
        return positions, latest_accounts, None

    # Account metadata defines the scope. Historical positions add unlisted accounts
    # only when no account inventory is available; closed accounts need not linger.
    if not latest_accounts.empty and "account_id" in latest_accounts:
        expected = set(latest_accounts["account_id"].dropna())
        cash_only = set()
        for row in latest_accounts.to_dict("records"):
            cash, nav = row.get("cash"), row.get("total_value")
            try:
                if (
                    _bool(row.get("complete"))
                    and pd.notna(cash)
                    and pd.notna(nav)
                    and abs(float(nav) - float(cash)) < 0.005
                ):
                    cash_only.add(row["account_id"])
            except (TypeError, ValueError):
                pass
        required = expected - cash_only
    else:
        required = set(positions["account_id"].dropna())

    dates = sorted(positions["valuation_date"].unique(), reverse=True)
    selected = None
    for candidate in dates:
        rows = positions.loc[positions["valuation_date"] == candidate]
        if not required.issubset(set(rows["account_id"].dropna())):
            continue
        if "snapshot_complete" in rows and not rows["snapshot_complete"].map(_bool).all():
            continue
        if accounts_dated:
            dated_accounts = accounts.loc[accounts["valuation_date"] == candidate]
            if not set(latest_accounts["account_id"].dropna()).issubset(
                set(dated_accounts["account_id"].dropna())
            ):
                continue
            if "complete" in dated_accounts and not dated_accounts["complete"].map(_bool).all():
                continue
            if (
                "snapshot_complete" in dated_accounts
                and not dated_accounts["snapshot_complete"].map(_bool).all()
            ):
                continue
        selected = candidate
        break
    if selected is None:
        selected = dates[0]
        _issue(
            issues,
            "INCOMPLETE_SNAPSHOT",
            "No single valuation date contains the expected invested accounts with complete snapshot markers. The newest single date is displayed; dates have not been blended.",
        )
    elif selected != dates[0]:
        _issue(
            issues,
            "INCOMPLETE_SNAPSHOT_SKIPPED",
            f"Skipped newer incomplete snapshots; selected coherent snapshot {selected}.",
            "warning",
        )
    positions = positions.loc[positions["valuation_date"] == selected].copy()
    if accounts_dated:
        selected_accounts = accounts.loc[accounts["valuation_date"] == selected].copy()
        if selected_accounts.empty:
            _issue(
                issues,
                "ACCOUNT_DATE_MISMATCH",
                f"No account totals match position date {selected}; account metadata are retained without claiming current reconciliation.",
            )
            selected_accounts = latest_accounts.copy()
            selected_accounts["complete"] = False
        accounts = selected_accounts
    if "snapshot_complete" not in positions.columns:
        _issue(
            issues,
            "SNAPSHOT_COMPLETENESS_UNVERIFIED",
            "A coherent common date was selected, but no snapshot_complete marker proves every position was imported. Reconcile to account totals.",
            "warning",
        )
    if selected != as_of:
        _issue(
            issues,
            "HISTORICAL_HOLDINGS",
            f"Holdings are valued on {selected}; requested research date is {as_of}. No current holdings or quantities have been inferred.",
            "warning",
        )
    return positions, accounts, selected


def _sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_portfolio(config, as_of) -> dict:
    """Import portfolio input frames without writes to the source database.

    Critical integrity problems are returned in ``issues``. They deliberately do
    not disappear through dropping duplicate holdings or treating missing cash as
    zero. Consumers must block allocation on error-severity ingestion issues.
    """
    try:
        as_of = dt.date.fromisoformat(str(as_of)).isoformat()
    except ValueError as exc:
        raise ValueError("as_of must be an ISO date such as 2026-09-30") from exc
    source_cfg = config.get("source", {})
    source = _source_path(source_cfg.get("path"))
    research_path = config.get("research", {}).get("path")
    if research_path and _resolved(research_path) == source:
        raise ValueError(
            "research.path must differ from source.path. The original portfolio database is read-only."
        )
    if (
        research_path
        and _resolved(research_path).exists()
        and os.path.samefile(source, _resolved(research_path))
    ):
        raise ValueError(
            "research.path points to the source database through another filesystem link."
        )
    if not source_cfg.get("positions_query"):
        raise ValueError(
            "Set source.positions_query to a SELECT returning portfolio position columns."
        )
    connection = _open_source(source)
    try:
        frames = {
            name: _read_frame(
                connection,
                source_cfg.get(name + "_query"),
                name,
                source_cfg.get("column_map", {}),
                as_of,
            )
            for name in FRAME_COLUMNS
        }
    finally:
        connection.close()
    issues = []
    p, a, s, lots = (frames[name] for name in FRAME_COLUMNS)
    required = ["account_id", "security_id", "market_value", "currency", "valuation_date"]
    absent = [column for column in required if column not in p]
    if absent:
        raise ValueError(
            "positions_query lacks required canonical columns: "
            + ", ".join(absent)
            + ". Add SELECT aliases or source.column_map.positions."
        )
    for name, frame in frames.items():
        for column in ("account_id", "security_id", "issuer_id", "ticker", "currency", "lot_id"):
            if column in frame:
                frame[column] = frame[column].map(_text)
        for column in FRAME_COLUMNS[name]:
            if column not in frame:
                frame[column] = None
    p = _numeric(p, ["quantity", "price", "market_value", "reported_weight"], issues, "positions")
    a = _numeric(a, ["total_value", "cash", "tax_rate"], issues, "accounts")
    s = _numeric(s, ["market_cap"], issues, "securities")
    lots = _numeric(lots, ["quantity", "basis_per_share"], issues, "tax_lots")
    a["complete"] = a["complete"].map(_bool)
    s["eligible"] = s["eligible"].map(_bool)
    p, a, snapshot = _select_snapshot(p, a, as_of, issues)

    for name, frame, keys in (
        ("positions", p, ["account_id", "security_id"]),
        ("accounts", a, ["account_id"]),
        ("securities", s, ["security_id"]),
        ("tax_lots", lots, ["account_id", "lot_id"]),
    ):
        _unique(frame, keys, issues, name)
        for key in keys:
            if frame[key].isna().any():
                _issue(
                    issues,
                    "MISSING_IDENTITY",
                    f"{name}.{key} has {int(frame[key].isna().sum())} unresolved values.",
                )
        missing_currency = frame["currency"].isna() | ~frame["currency"].fillna("").str.fullmatch(
            r"[A-Z]{3}"
        )
        if missing_currency.any():
            _issue(
                issues,
                "MISSING_CURRENCY",
                f"{name}: {int(missing_currency.sum())} rows lack an explicit uppercase ISO currency.",
            )
    if p["market_value"].isna().any():
        _issue(
            issues,
            "MISSING_POSITION_VALUE",
            "Some position values are missing. A complete valuation cannot be calculated.",
        )
    if p["quantity"].isna().any():
        _issue(
            issues,
            "MISSING_QUANTITIES",
            "Some holdings have value but no quantity. Current repricing, share sizing, and lot matching are unavailable for those holdings.",
            "warning",
        )
    if (p["market_value"] < 0).any() or (p["quantity"] < 0).any():
        _issue(
            issues,
            "SHORT_POSITION",
            "Negative position values or quantities conflict with the initial long-only mandate.",
        )
    if p["reported_weight"].notna().any():
        if not p["reported_weight"].dropna().between(0, 1).all():
            _issue(
                issues,
                "INVALID_REPORTED_WEIGHT",
                "reported_weight must be a fraction in [0,1], not percentage points.",
            )
        # Denominator may be account- or household-level; never infer it from a sum.
        _issue(
            issues,
            "REPORTED_WEIGHT_SCOPE",
            "Reported weights are preserved; their denominator is not inferred. Recompute account weights only from reconciled totals.",
            "info",
        )
    if a.empty:
        _issue(
            issues,
            "MISSING_ACCOUNTS",
            "No account inventory was supplied. Cash, completeness, and funding constraints are unknown.",
        )
    elif a["cash"].isna().any() or a["total_value"].isna().any():
        _issue(
            issues,
            "MISSING_ACCOUNT_TOTALS",
            "Some accounts lack explicit cash or total_value. Unexplained portfolio value is not cash.",
        )
    if not a.empty and not a["complete"].all():
        _issue(
            issues,
            "INCOMPLETE_ACCOUNT_COVERAGE",
            "One or more accounts are not marked complete; portfolio-wide rebalancing remains blocked.",
        )
    if not a["tax_rate"].dropna().between(0, 1).all():
        _issue(issues, "INVALID_TAX_RATE", "Account tax rates must be fractions in [0,1].")
    account_ids = set(a["account_id"].dropna())
    unknown_accounts = set(p["account_id"].dropna()) - account_ids
    if unknown_accounts:
        _issue(
            issues,
            "UNRESOLVED_ACCOUNT",
            "Positions refer to accounts missing from the account inventory: "
            + ", ".join(sorted(unknown_accounts)),
        )
    security_ids = set(s["security_id"].dropna())
    unknown = set(p["security_id"].dropna()) - security_ids
    if unknown:
        _issue(
            issues,
            "UNRESOLVED_SECURITY",
            "Positions lack verified security records: " + ", ".join(sorted(unknown)),
        )
    if not s.empty:
        owned = s.loc[s["security_id"].isin(p["security_id"])]
        if owned["issuer_id"].isna().any():
            _issue(
                issues,
                "MISSING_ISSUER",
                "Owned securities lack issuer identities; issuer aggregation is incomplete.",
            )
    if not lots.empty:
        lots = _dates(lots, "acquired_date", issues, "tax_lots")
        lots = lots.loc[lots["acquired_date"] <= as_of].copy()
        _issue(
            issues,
            "TAX_LOT_SCOPE",
            "Tax lots must represent lots open at the selected snapshot. Acquisition dates alone do not establish that a lot remains open.",
            "warning",
        )
    received_at = dt.datetime.now(dt.timezone.utc).isoformat()
    source_record = {
        "source_id": "sqlite:" + uuid.uuid4().hex,
        "provider": "portfolio_sqlite",
        "path": str(source),
        "sha256": _sha256(source),
        "received_at": received_at,
        "valuation_date": snapshot,
        "read_only": True,
        "query_hash": hashlib.sha256(
            _dumps({name: source_cfg.get(name + "_query") for name in FRAME_COLUMNS}).encode()
        ).hexdigest(),
    }
    wal = Path(str(source) + "-wal")
    if wal.exists():
        source_record["wal_sha256"] = _sha256(wal)
        source_record["hash_note"] = (
            "File hashes are provenance hints; the archived normalized bundle is the replay input for the pinned read transaction."
        )
    return {
        "as_of": as_of,
        "valuation_date": snapshot,
        "positions": p.reset_index(drop=True),
        "accounts": a.reset_index(drop=True),
        "securities": s.reset_index(drop=True),
        "tax_lots": lots.reset_index(drop=True),
        "issues": issues,
        "sources": [source_record],
    }


def _jsonable(value: Any):
    """Strict JSON: missing/nonfinite pandas/numpy values become JSON null."""
    if value is None or value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, pd.DataFrame):
        return [_jsonable(row) for row in value.to_dict("records")]
    if isinstance(value, pd.Series):
        return [_jsonable(item) for item in value.tolist()]
    if isinstance(value, np.ndarray):
        return [_jsonable(item) for item in value.tolist()]
    if isinstance(value, np.generic):
        return _jsonable(value.item())
    if isinstance(value, (dt.datetime, dt.date, pd.Timestamp)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    # Decimal and similar scalar values: preserve precision as text in the archive.
    if value.__class__.__name__ == "Decimal":
        return str(value) if value.is_finite() else None
    raise TypeError(f"Unsupported archive value type: {type(value).__name__}")


def _dumps(value):
    return json.dumps(
        _jsonable(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def _pack_bundle(bundle):
    return {
        key: {
            "__dataframe__": True,
            "columns": list(value.columns),
            "dtypes": {name: str(dtype) for name, dtype in value.dtypes.items()},
            "records": _jsonable(value),
        }
        if isinstance(value, pd.DataFrame)
        else _jsonable(value)
        for key, value in bundle.items()
    }


def _unpack_bundle(value):
    result = {}
    for key, item in value.items():
        if isinstance(item, dict) and item.get("__dataframe__"):
            frame = pd.DataFrame.from_records(item["records"], columns=item["columns"])
            for column, dtype in item.get("dtypes", {}).items():
                try:
                    if dtype.startswith("datetime"):
                        frame[column] = pd.to_datetime(frame[column], utc="UTC" in dtype)
                    elif dtype not in {"object", "category"}:
                        frame[column] = frame[column].astype(dtype)
                except (ValueError, TypeError):
                    # Nulls can require a nullable dtype; values remain preserved.
                    pass
            result[key] = frame
        else:
            result[key] = item
    return result


class ResearchStore:
    """Separate SQLite archive with database-enforced append-only run records."""

    def __init__(self, path):
        if not path:
            raise ValueError("Set research.path to a separate analysis SQLite database.")
        self.path = _resolved(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as connection:
            # Refuse to initialize in an existing unrelated database. The caller
            # additionally compares source/research paths before instantiating.
            existing = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            }
            if existing and "research_store_metadata" not in existing:
                raise ValueError(
                    "research.path is an existing unrelated SQLite database. Choose a new file so portfolio data are never modified."
                )
            if "research_store_metadata" in existing:
                versions = connection.execute(
                    "SELECT schema_version FROM research_store_metadata"
                ).fetchall()
                if versions != [(1,)]:
                    raise ValueError(
                        "Unsupported research archive schema; no migration was applied."
                    )
            if "review_metadata" in existing:
                versions = connection.execute(
                    "SELECT schema_version FROM review_metadata"
                ).fetchall()
                if versions != [(1,)]:
                    raise ValueError(
                        "Unsupported research application schema; no migration was applied."
                    )
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS research_store_metadata (schema_version INTEGER NOT NULL);
                INSERT INTO research_store_metadata SELECT 1 WHERE NOT EXISTS (SELECT 1 FROM research_store_metadata);
                CREATE TABLE IF NOT EXISTS research_runs (
                    run_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, as_of TEXT,
                    config_hash TEXT NOT NULL, bundle_hash TEXT NOT NULL,
                    config_json TEXT NOT NULL, result_json TEXT NOT NULL,
                    bundle_json TEXT NOT NULL, manifest_json TEXT NOT NULL
                );
                CREATE TRIGGER IF NOT EXISTS immutable_runs_update BEFORE UPDATE ON research_runs
                BEGIN SELECT RAISE(ABORT, 'research runs are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS immutable_runs_delete BEFORE DELETE ON research_runs
                BEGIN SELECT RAISE(ABORT, 'research runs are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS immutable_runs_replace BEFORE INSERT ON research_runs
                WHEN EXISTS (SELECT 1 FROM research_runs WHERE run_id=NEW.run_id)
                BEGIN SELECT RAISE(ABORT, 'research runs are immutable'); END;
                CREATE TABLE IF NOT EXISTS research_payloads (
                    source_id TEXT PRIMARY KEY, provider TEXT NOT NULL, cache_key TEXT NOT NULL,
                    received_at TEXT NOT NULL, sha256 TEXT NOT NULL, payload_json TEXT NOT NULL
                );
                CREATE TRIGGER IF NOT EXISTS immutable_payloads_update BEFORE UPDATE ON research_payloads
                BEGIN SELECT RAISE(ABORT, 'research payloads are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS immutable_payloads_delete BEFORE DELETE ON research_payloads
                BEGIN SELECT RAISE(ABORT, 'research payloads are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS immutable_payloads_replace BEFORE INSERT ON research_payloads
                WHEN EXISTS (SELECT 1 FROM research_payloads WHERE source_id=NEW.source_id)
                BEGIN SELECT RAISE(ABORT, 'research payloads are immutable'); END;
            """)
        self.path.chmod(0o600)

    def save_run(self, result, config, bundle) -> str:
        source = config.get("source", {}).get("path")
        if source:
            path = _resolved(source)
            if path == self.path or (path.exists() and os.path.samefile(path, self.path)):
                raise ValueError("Research archive cannot be the portfolio source database.")
        run_id = str(result.get("run_id") or uuid.uuid4().hex)
        created_at = dt.datetime.now(dt.timezone.utc).isoformat()
        config_json = _dumps(config)
        bundle_json = _dumps(_pack_bundle(bundle))
        config_hash = hashlib.sha256(config_json.encode()).hexdigest()
        bundle_hash = hashlib.sha256(bundle_json.encode()).hexdigest()
        saved_result = copy.deepcopy(result)
        saved_result["run_id"] = run_id
        saved_result.setdefault("metadata", {}).update(
            {
                "run_id": run_id,
                "created_at": created_at,
                "config_hash": config_hash,
                "bundle_hash": bundle_hash,
            }
        )
        manifest = {
            "sources": bundle.get("sources", []),
            "frames": {
                key: {"rows": len(value), "columns": list(value.columns)}
                for key, value in bundle.items()
                if isinstance(value, pd.DataFrame)
            },
            "bundle_hash": bundle_hash,
            "config_hash": config_hash,
        }
        try:
            with sqlite3.connect(self.path) as connection:
                connection.execute(
                    "INSERT INTO research_runs VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        run_id,
                        created_at,
                        bundle.get("as_of"),
                        config_hash,
                        bundle_hash,
                        config_json,
                        _dumps(saved_result),
                        bundle_json,
                        _dumps(manifest),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError(
                f"Run {run_id!r} already exists; saved runs cannot be overwritten."
            ) from exc
        return run_id

    def list_runs(self) -> list:
        with sqlite3.connect(self.path) as connection:
            rows = connection.execute(
                "SELECT run_id,created_at,as_of,config_hash,bundle_hash FROM research_runs ORDER BY created_at DESC,run_id DESC"
            ).fetchall()
        return [
            dict(zip(("run_id", "created_at", "as_of", "config_hash", "bundle_hash"), row))
            for row in rows
        ]

    def _row(self, run_id):
        with sqlite3.connect(self.path) as connection:
            row = connection.execute(
                "SELECT result_json,config_json,bundle_json,manifest_json FROM research_runs WHERE run_id=?",
                (str(run_id),),
            ).fetchone()
        if row is None:
            raise KeyError(f"No archived research run: {run_id}")
        return row

    def load_run(self, run_id) -> dict:
        row = self._row(run_id)
        result = json.loads(row[0])
        result["saved_config"] = json.loads(row[1])
        result["input_manifest"] = json.loads(row[3])
        return result

    def load_run_part(self, run_id, name: str):
        """One top-level field of an archived result, extracted by the database.

        A saved run carries every security's research, which at the documented candidate
        scope is megabytes. A caller that needs one field — the previous run's research,
        say — should not parse the rest of the archive to reach it.
        """
        if not str(name).isidentifier():
            raise ValueError("A result field name must be a plain identifier.")
        with sqlite3.connect(self.path) as connection:
            row = connection.execute(
                "SELECT json_extract(result_json,?) FROM research_runs WHERE run_id=?",
                (f"$.{name}", str(run_id)),
            ).fetchone()
        if row is None:
            raise KeyError(f"No archived research run: {run_id}")
        return None if row[0] is None else json.loads(row[0])

    def load_bundle(self, run_id) -> dict:
        """Restore the frozen normalized inputs for network-free scenario replay."""
        row = self._row(run_id)
        return _unpack_bundle(json.loads(row[2]))

    def save_payload(self, provider, key, payload, received_at=None) -> dict:
        received_at = received_at or dt.datetime.now(dt.timezone.utc).isoformat()
        payload_json = _dumps(payload)
        digest = hashlib.sha256(payload_json.encode()).hexdigest()
        source_id = "payload:" + uuid.uuid4().hex
        # Callers provide a credential-free stable key, not a secret-bearing URL.
        if any(token in str(key).lower() for token in ("api_key=", "apikey=", "token=", "secret=")):
            raise ValueError(
                "Payload keys must not contain credentials; use a redacted URL or stable identifier."
            )
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "INSERT INTO research_payloads VALUES (?,?,?,?,?,?)",
                (source_id, str(provider), str(key), received_at, digest, payload_json),
            )
        return {
            "source_id": source_id,
            "provider": str(provider),
            "key": str(key),
            "received_at": received_at,
            "sha256": digest,
        }
