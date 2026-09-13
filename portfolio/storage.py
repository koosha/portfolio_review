import csv
import hashlib
import io
import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_ROWS = 50000


def now():
    return datetime.now(timezone.utc).isoformat()


def yahoo_url(value):
    url = urlsplit(value.strip())
    parts = url.path.strip("/").split("/")
    identifier = unquote(parts[1], errors="strict") if len(parts) >= 2 else ""
    if (
        url.scheme != "https"
        or url.netloc != "finance.yahoo.com"
        or len(parts) < 2
        or parts[0] not in ("portfolio", "portfolios")
        or not re.fullmatch(r"(?:[A-Za-z0-9_-]+|yodlee\|[A-Za-z0-9_-]+)", identifier)
        or identifier.lower() in ("create", "new", "import", "compare")
    ):
        raise ValueError("Use a specific https://finance.yahoo.com/portfolio/... portfolio URL.")
    return "https://finance.yahoo.com/" + parts[0] + "/" + quote(identifier, safe="-_")


def yahoo_navigation_url(value):
    identity = yahoo_url(value)
    path = urlsplit(value).path.strip("/").split("/")
    suffix = "/".join(path[2:])
    return identity + (
        "/" + suffix if re.fullmatch(r"view(?:/[A-Za-z0-9_-]+)?", suffix) else "/view"
    )


def number(value, label, row):
    value = value.strip()
    if value.lower() in ("", "-", "--", "—", "–", "n/a", "na", "null"):
        return None
    cleaned = value.replace(",", "").replace("−", "-")
    if cleaned.startswith("(") and cleaned.endswith(")"):
        cleaned = "-" + cleaned[1:-1]
    try:
        if len(cleaned) > 128:
            raise InvalidOperation
        result = Decimal(cleaned)
        if (
            not result.is_finite()
            or abs(result.as_tuple().exponent) > 128
            or result.adjusted() > 128
        ):
            raise InvalidOperation
        return format(result, "f")
    except InvalidOperation:
        raise ValueError(f"Row {row}: invalid {label} value. No data was imported.")


def parse_csv(data):
    if len(data) > MAX_FILE_BYTES:
        raise ValueError("CSV exceeds the 10 MB limit.")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError("Use a UTF-8 CSV exported by Yahoo Finance.")
    if "\x00" in text:
        raise ValueError("This is not a text CSV.")
    try:
        reader = csv.DictReader(io.StringIO(text), strict=True)
        headers = reader.fieldnames or []
        keys = [h.strip().lower() for h in headers]
        if "symbol" not in keys or len(keys) != len(set(keys)) or "" in keys:
            raise ValueError("CSV must have a Symbol column and unique, nonempty column names.")
        rows, warnings = [], set()
        for index, original in enumerate(reader, 2):
            if len(rows) >= MAX_ROWS:
                raise ValueError("CSV exceeds the 50,000 row limit.")
            if None in original or any(v is None for v in original.values()):
                raise ValueError(f"Row {index}: column count does not match the header.")
            raw = {k.strip().lower(): v.strip() for k, v in original.items()}
            if not any(raw.values()):
                continue
            symbol = raw.get("symbol", "").strip()
            if not symbol:
                raise ValueError(f"Row {index}: Symbol is missing.")

            def pick(*aliases):
                return next((raw[k] for k in aliases if raw.get(k, "").strip()), "")

            quantity = number(pick("quantity", "shares", "shares held"), "quantity", index)
            price = number(pick("current price", "last price"), "price", index)
            cost = number(
                pick("purchase price", "cost/share", "cost per share"), "purchase price", index
            )
            average_cost = number(
                pick("ac/share", "average cost/share", "average cost per share"),
                "average cost",
                index,
            )
            total_cost = number(
                pick("total cost ($)", "total cost", "cost basis"), "total cost", index
            )
            market_value = number(pick("market value ($)", "market value"), "market value", index)
            currency = pick("currency").upper() or None
            if currency and not re.fullmatch("[A-Z]{3}", currency):
                raise ValueError(f"Row {index}: Currency must be a three-letter code.")
            if quantity is None:
                warnings.add(
                    "Some rows have no quantity; these may be watchlist entries, not holdings."
                )
            if cost is None and average_cost is None:
                warnings.add("Some rows have no purchase or average cost per share.")
            if currency is None:
                warnings.add(
                    "Some rows have no currency. No currency or exchange rate has been assumed."
                )
            rows.append(
                dict(
                    row_number=index,
                    symbol=symbol,
                    name=pick("name", "description") or None,
                    quantity=quantity,
                    price=price,
                    purchase_price=cost,
                    average_cost=average_cost,
                    total_cost=total_cost,
                    market_value=market_value,
                    trade_date=pick("trade date", "purchase date") or None,
                    currency=currency,
                    comment=pick("comment", "notes") or None,
                    raw=original,
                )
            )
        if not rows:
            raise ValueError("CSV has no records. The previous snapshot has been kept.")
        return rows, sorted(warnings)
    except csv.Error as exc:
        raise ValueError("Invalid CSV: " + str(exc))


def holding_summary(rows, capture=None):
    """Reconcile captured market values within a portfolio, retaining cash and missing-value warnings."""
    groups = {}
    missing = 0
    with localcontext() as context:
        context.prec = 512
        for row in rows:
            currency = row.get("currency")
            group = groups.setdefault(
                currency, dict(currency=currency, holdings=Decimal(0), cash=Decimal(0), count=0)
            )
            is_cash = row["symbol"].strip().lower() == "total cash" and row.get("quantity") is None
            value = row.get("market_value")
            # Yahoo's observed Total Cash row puts its balance in the Last Price column.
            if (
                is_cash
                and value is None
                and capture
                and capture.get("method") == "yahoo-holdings-table-v1"
            ):
                value = row.get("price")
            if value is None:
                missing += 1
                continue
            group["cash" if is_cash else "holdings"] += Decimal(value)
            group["count"] += 1
        result = []
        for group in groups.values():
            amount = group["holdings"] + group["cash"] if group["count"] else None
            result.append(
                dict(
                    currency=group["currency"],
                    total=format(amount, "f") if amount is not None else None,
                    formatted_total=format(amount, ",.2f") if amount is not None else None,
                    holdings=format(group["holdings"], "f"),
                    cash=format(group["cash"], "f"),
                )
            )
    completeness = (capture or {}).get("completeness", "unverified")
    return dict(
        groups=result,
        total_market_value=result[0]["total"] if len(result) == 1 else None,
        missing_market_values=missing,
        completeness=completeness,
        completeness_verified=completeness == "count-verified",
        row_count=len(rows),
        cash_row_count=sum(
            r["symbol"].strip().lower() == "total cash" and r.get("quantity") is None for r in rows
        ),
    )


class Store:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.directory / "portfolio.sqlite3"
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS sources (
                  id INTEGER PRIMARY KEY, name TEXT NOT NULL, account TEXT NOT NULL DEFAULT '',
                  url TEXT UNIQUE, currency TEXT, created_at TEXT NOT NULL,
                  last_checked TEXT, last_error TEXT);
                CREATE TABLE IF NOT EXISTS snapshots (
                  id INTEGER PRIMARY KEY, source_id INTEGER NOT NULL REFERENCES sources(id),
                  captured_at TEXT NOT NULL, sha256 TEXT NOT NULL, filename TEXT NOT NULL,
                  raw_csv BLOB NOT NULL, rows_json TEXT NOT NULL, warnings_json TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS snapshots_source ON snapshots(source_id, id DESC);
                CREATE TABLE IF NOT EXISTS positions (
                  snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
                  source_id INTEGER NOT NULL REFERENCES sources(id), row_number INTEGER NOT NULL,
                  symbol TEXT NOT NULL, name TEXT, quantity TEXT NOT NULL, price TEXT,
                  purchase_price TEXT, trade_date TEXT, currency TEXT,
                  PRIMARY KEY(snapshot_id,row_number));
                CREATE INDEX IF NOT EXISTS positions_source ON positions(source_id,snapshot_id);
            """)
            columns = {r["name"] for r in db.execute("PRAGMA table_info(sources)")}
            for name, definition in [
                ("selected", "INTEGER NOT NULL DEFAULT 1"),
                ("navigation_url", "TEXT"),
                ("display_order", "INTEGER"),
                ("managed", "INTEGER"),
            ]:
                if name not in columns:
                    db.execute(f"ALTER TABLE sources ADD COLUMN {name} {definition}")
            if "positions_indexed" not in {
                r["name"] for r in db.execute("PRAGMA table_info(snapshots)")
            }:
                db.execute(
                    "ALTER TABLE snapshots ADD COLUMN positions_indexed INTEGER NOT NULL DEFAULT 0"
                )
            if "capture_json" not in {
                r["name"] for r in db.execute("PRAGMA table_info(snapshots)")
            }:
                db.execute("ALTER TABLE snapshots ADD COLUMN capture_json TEXT")
            position_columns = {r["name"] for r in db.execute("PRAGMA table_info(positions)")}
            for field in ("average_cost", "total_cost", "market_value"):
                if field not in position_columns:
                    db.execute(f"ALTER TABLE positions ADD COLUMN {field} TEXT")
            for snapshot in db.execute(
                "SELECT id,source_id,rows_json FROM snapshots WHERE positions_indexed=0"
            ).fetchall():
                self._index_positions(
                    db, snapshot["id"], snapshot["source_id"], json.loads(snapshot["rows_json"])
                )
            db.execute("""CREATE VIEW IF NOT EXISTS latest_positions AS SELECT p.* FROM positions p
                JOIN sources s ON s.id=p.source_id WHERE s.selected=1
                AND p.snapshot_id=(SELECT MAX(id) FROM snapshots WHERE source_id=p.source_id)""")
        self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def add_source(self, name, account="", url=None, currency=None):
        name, account = name.strip(), account.strip()
        currency = (currency or "").strip().upper() or None
        if not name or len(name) > 200 or len(account) > 200:
            raise ValueError("A portfolio name is required (200 characters maximum).")
        if currency and not re.fullmatch("[A-Z]{3}", currency):
            raise ValueError("Currency must be a three-letter code or blank.")
        navigation_url = yahoo_navigation_url(url) if url else None
        url = yahoo_url(url) if url else None
        with self.connect() as db:
            try:
                result = db.execute(
                    "INSERT INTO sources(name,account,url,currency,created_at,navigation_url) VALUES(?,?,?,?,?,?)",
                    (name, account, url, currency, now(), navigation_url),
                )
            except sqlite3.IntegrityError:
                raise ValueError("This Yahoo portfolio is already registered.")
            return result.lastrowid

    def remember_portfolios(self, portfolios):
        """Save discovered names automatically; never change an existing selection."""
        prepared = [
            (
                p["name"].strip()[:200],
                yahoo_url(p["url"]),
                yahoo_navigation_url(p.get("navigation_url") or p["url"]),
            )
            for p in portfolios
        ]
        if any(not name or yahoo_url(nav) != url for name, url, nav in prepared):
            raise ValueError("Invalid portfolio identity.")
        with self.connect() as db:
            for order, (name, url, nav) in enumerate(prepared):
                db.execute(
                    """INSERT INTO sources(name,url,navigation_url,selected,created_at,display_order) VALUES(?,?,?,0,?,?)
                    ON CONFLICT(url) DO UPDATE SET name=excluded.name,navigation_url=excluded.navigation_url,
                    display_order=excluded.display_order""",
                    (name, url, nav, now(), order),
                )

    def select_source(self, source_id, selected):
        if type(selected) is not bool:
            raise ValueError("Selection must be true or false.")
        with self.connect() as db:
            if not db.execute(
                "UPDATE sources SET selected=? WHERE id=?", (int(selected), source_id)
            ).rowcount:
                raise ValueError("Portfolio not found.")

    @staticmethod
    def _index_positions(db, snapshot_id, source_id, rows):
        db.executemany(
            """INSERT OR IGNORE INTO positions(snapshot_id,source_id,row_number,symbol,name,quantity,
            price,purchase_price,trade_date,currency,average_cost,total_cost,market_value) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [
                (
                    snapshot_id,
                    source_id,
                    r["row_number"],
                    r["symbol"],
                    r.get("name"),
                    r["quantity"],
                    r.get("price"),
                    r.get("purchase_price"),
                    r.get("trade_date"),
                    r.get("currency"),
                    r.get("average_cost"),
                    r.get("total_cost"),
                    r.get("market_value"),
                )
                for r in rows
                if r.get("quantity") is not None
            ],
        )
        db.execute("UPDATE snapshots SET positions_indexed=1 WHERE id=?", (snapshot_id,))

    def positions(self):
        with self.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM latest_positions ORDER BY source_id,row_number"
                )
            ]

    def source(self, source_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
            if row is None:
                raise ValueError("Portfolio not found.")
            return dict(row)

    def sources(self):
        with self.connect() as db:
            sources = [
                dict(row)
                for row in db.execute("""SELECT s.*,
                latest.id AS snapshot_id, latest.captured_at,
                latest.rows_json AS saved_rows, latest.capture_json AS saved_capture,
                (SELECT COUNT(*) FROM positions WHERE source_id=s.id AND snapshot_id=latest.id) AS position_count
                FROM sources s LEFT JOIN snapshots latest ON latest.id=(SELECT MAX(id) FROM snapshots WHERE source_id=s.id)
                ORDER BY s.display_order IS NULL,s.display_order,s.id""")
            ]
        for source in sources:
            rows, capture = source.pop("saved_rows"), source.pop("saved_capture")
            source["holding_summary"] = (
                holding_summary(json.loads(rows), json.loads(capture or "null")) if rows else None
            )
        return sources

    def ingest(self, source_id, data, filename="quotes.csv", capture=None):
        rows, warnings = parse_csv(data)
        if capture:
            warnings.append(
                "Captured from Yahoo’s displayed Holdings table; values may be rounded. No purchase lots or currency were inferred."
            )
            if capture["completeness"] == "end-observed":
                warnings.append(
                    "Reached the end of the loaded table, but Yahoo did not expose a total row count. Completeness is unverified."
                )
        digest, timestamp = hashlib.sha256(data).hexdigest(), now()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT id FROM sources WHERE id=?", (source_id,)).fetchone():
                raise ValueError("Portfolio not found.")
            latest = db.execute(
                "SELECT id, sha256, capture_json FROM snapshots WHERE source_id=? ORDER BY id DESC LIMIT 1",
                (source_id,),
            ).fetchone()
            unchanged = bool(
                latest
                and latest["sha256"] == digest
                and json.loads(latest["capture_json"] or "null") == capture
            )
            if unchanged:
                snapshot_id = latest["id"]
            else:
                snapshot_id = db.execute(
                    """INSERT INTO snapshots
                    (source_id,captured_at,sha256,filename,raw_csv,rows_json,warnings_json,capture_json)
                    VALUES(?,?,?,?,?,?,?,?)""",
                    (
                        source_id,
                        timestamp,
                        digest,
                        Path(filename).name,
                        data,
                        json.dumps(rows),
                        json.dumps(warnings),
                        json.dumps(capture) if capture else None,
                    ),
                ).lastrowid
                self._index_positions(db, snapshot_id, source_id, rows)
            db.execute(
                "UPDATE sources SET last_checked=?, last_error=NULL WHERE id=?",
                (timestamp, source_id),
            )
        return dict(
            snapshot_id=snapshot_id,
            unchanged=unchanged,
            row_count=len(rows),
            position_count=sum(r["quantity"] is not None for r in rows),
            warnings=warnings,
        )

    def ingest_table(self, source_id, table):
        if not isinstance(table, dict) or table.get("method") != "yahoo-holdings-table-v1":
            raise ValueError(
                "Unsupported holdings capture. Restart the app and reload the Chrome extension."
            )
        headers, records = table.get("headers"), table.get("rows")
        if (
            not isinstance(headers, list)
            or not 2 <= len(headers) <= 80
            or any(not isinstance(h, str) or not h.strip() or len(h) > 200 for h in headers)
        ):
            raise ValueError("Invalid holdings table headers.")
        keys = [h.strip().lower() for h in headers]
        if (
            len(set(keys)) != len(keys)
            or "symbol" not in keys
            or not any(k in keys for k in ("shares", "quantity", "shares held"))
        ):
            raise ValueError("Holdings require unique Symbol and Shares columns.")
        if not isinstance(records, list) or not 1 <= len(records) <= MAX_ROWS:
            raise ValueError(
                "No holdings rows captured, or the table is too large. Previous holdings were kept."
            )
        if any(
            not isinstance(row, list)
            or len(row) != len(headers)
            or any(not isinstance(cell, str) or len(cell) > 4000 for cell in row)
            for row in records
        ):
            raise ValueError("Invalid holdings table row. Previous holdings were kept.")
        symbol_index = keys.index("symbol")
        if any(not row[symbol_index].strip() for row in records):
            raise ValueError("A captured holdings row has no Symbol. Previous holdings were kept.")
        expected, completeness, pages = (
            table.get("expected_count"),
            table.get("completeness"),
            table.get("page_count"),
        )
        if type(pages) is not int or not 1 <= pages <= 200:
            raise ValueError("Invalid holdings page count.")
        if expected is not None and (type(expected) is not int or expected != len(records)):
            raise ValueError(
                "Holdings count does not match Yahoo’s total. Previous holdings were kept."
            )
        if completeness != ("end-observed" if expected is None else "count-verified"):
            raise ValueError(
                "Holdings completeness was not established. Previous holdings were kept."
            )
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(headers)
        writer.writerows(records)
        data = output.getvalue().encode("utf-8")
        capture = dict(
            method=table["method"],
            completeness=completeness,
            expected_count=expected,
            page_count=pages,
            row_count=len(records),
            source_url=self.source(source_id)["url"],
        )
        return self.ingest(source_id, data, "yahoo-holdings-table.csv", capture=capture)

    def failed(self, source_id, message):
        with self.connect() as db:
            db.execute("UPDATE sources SET last_error=? WHERE id=?", (message, source_id))

    def snapshots(self, source_id):
        self.source(source_id)
        with self.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    """SELECT id,source_id,captured_at,sha256,filename
                    FROM snapshots WHERE source_id=? ORDER BY id DESC""",
                    (source_id,),
                )
            ]

    def snapshot(self, snapshot_id, raw=False):
        with self.connect() as db:
            result = db.execute("SELECT * FROM snapshots WHERE id=?", (snapshot_id,)).fetchone()
            if result is None:
                raise ValueError("Snapshot not found.")
            result = dict(result)
            if raw:
                return result["raw_csv"]
            del result["raw_csv"]
            result["rows"] = json.loads(result.pop("rows_json"))
            result["warnings"] = json.loads(result.pop("warnings_json"))
            result["capture"] = json.loads(result.pop("capture_json") or "null")
            result["holding_summary"] = holding_summary(result["rows"], result["capture"])
            return result

    def export(self):
        return dict(
            schema_version=2,
            exported_at=now(),
            positions=self.positions(),
            scope="Latest imported holdings/lot records for selected portfolios; quantities are source records, not aggregated performance.",
            portfolios=[
                dict(
                    source=s, snapshot=self.snapshot(s["snapshot_id"]) if s["snapshot_id"] else None
                )
                for s in self.sources()
                if s["selected"]
            ],
        )
