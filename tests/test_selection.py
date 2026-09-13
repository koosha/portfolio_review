import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from portfolio.storage import Store
from portfolio.yahoo import discover_links

ROOT = "https://finance.yahoo.com/portfolio/"
CSV = b"Symbol,Quantity,Purchase Price\nAAPL,1.0000000001,180.50\nAAPL,-2,200\nMSFT,,\n"


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)

    def test_discovery_preserves_choices_names_and_order_after_restart(self):
        existing = self.store.add_source("Old name", url=ROOT + "p_9")
        portfolios = discover_links(
            [
                {"href": ROOT + "p_0/view", "text": "Watchlist"},
                {"href": ROOT + "p_9/view/v1", "text": "Retirement"},
            ]
        )
        self.store.remember_portfolios(portfolios)
        self.assertEqual([s["name"] for s in self.store.sources()], ["Watchlist", "Retirement"])
        watch = self.store.sources()[0]["id"]
        self.assertEqual([s["selected"] for s in self.store.sources()], [0, 1])
        self.store.select_source(existing, False)
        self.store.select_source(watch, True)
        reopened = Store(self.temp.name)
        reopened.remember_portfolios(portfolios)
        self.assertEqual([s["selected"] for s in reopened.sources()], [1, 0])
        self.assertEqual(reopened.source(existing)["navigation_url"], ROOT + "p_9/view/v1")

    def test_latest_positions_and_exports_only_include_selected_sources(self):
        a = self.store.add_source("A", url=ROOT + "p_1/view/v2")
        b = self.store.add_source("B", url=ROOT + "p_2")
        original = self.store.ingest(a, CSV)
        self.store.ingest(b, CSV)
        self.store.select_source(b, False)
        exported = self.store.export()
        self.assertEqual(exported["schema_version"], 2)
        self.assertEqual(len(exported["portfolios"]), 1)
        self.assertEqual([r["quantity"] for r in exported["positions"]], ["1.0000000001", "-2"])
        self.assertEqual(exported["positions"][0]["purchase_price"], "180.50")
        self.assertEqual(self.store.source(a)["navigation_url"], ROOT + "p_1/view/v2")
        self.store.ingest(a, b"Symbol,Quantity\nGOOG,3\n")
        self.assertEqual([r["symbol"] for r in self.store.positions()], ["GOOG"])
        self.assertEqual(self.store.snapshot(original["snapshot_id"], raw=True), CSV)
        self.store.select_source(a, False)
        self.assertEqual(self.store.positions(), [])
        self.assertEqual(self.store.export()["portfolios"], [])
        self.assertEqual(len(self.store.snapshots(a)), 2)

    def test_old_database_backfills_positions_without_losing_snapshots(self):
        legacy = Path(self.temp.name) / "legacy"
        legacy.mkdir()
        with sqlite3.connect(legacy / "portfolio.sqlite3") as db:
            db.executescript("""CREATE TABLE sources(id INTEGER PRIMARY KEY,name TEXT NOT NULL,
                account TEXT NOT NULL DEFAULT '',url TEXT UNIQUE,currency TEXT,created_at TEXT NOT NULL,
                last_checked TEXT,last_error TEXT);
                CREATE TABLE snapshots(id INTEGER PRIMARY KEY,source_id INTEGER NOT NULL,
                captured_at TEXT NOT NULL,sha256 TEXT NOT NULL,filename TEXT NOT NULL,
                raw_csv BLOB NOT NULL,rows_json TEXT NOT NULL,warnings_json TEXT NOT NULL);""")
            db.execute('INSERT INTO sources(id,name,created_at) VALUES(1,"Legacy","then")')
            rows = [{"row_number": 2, "symbol": "AAPL", "quantity": "0.0000000001"}]
            db.execute(
                'INSERT INTO snapshots VALUES(1,1,"then","hash","quotes.csv",?,?,?)',
                (CSV, json.dumps(rows), "[]"),
            )
        for _ in range(2):
            upgraded = Store(legacy)
            self.assertEqual(upgraded.source(1)["selected"], 1)
            self.assertEqual(len(upgraded.positions()), 1)
            self.assertEqual(upgraded.positions()[0]["quantity"], "0.0000000001")
            self.assertEqual(upgraded.snapshot(1, raw=True), CSV)

    def test_selection_requires_boolean_and_existing_source(self):
        source = self.store.add_source("A")
        for value in ["false", 1, None]:
            with self.assertRaises(ValueError):
                self.store.select_source(source, value)
        with self.assertRaises(ValueError):
            self.store.select_source(999, True)
