import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from portfolio_lab.ingestion import ResearchStore, inspect_database, load_portfolio


class IngestionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.source = self.directory / "positions.sqlite"
        with sqlite3.connect(self.source) as db:
            db.executescript("""
                CREATE TABLE holdings(acct TEXT, sid TEXT, amount REAL, ccy TEXT, dated TEXT, snapshot_complete INTEGER);
                INSERT INTO holdings VALUES
                  ('a','AA',100,'USD','2026-07-31',1),
                  ('b','BB',200,'USD','2026-07-31',1),
                  ('a','AA',110,'USD','2026-08-31',1),
                  ('b','BB',210,'USD','2026-08-31',1),
                  ('a','AA',120,'USD','2026-09-30',0);
                CREATE TABLE accounts(account_id TEXT,account_type TEXT,currency TEXT,total_value REAL,cash REAL,complete INTEGER);
                INSERT INTO accounts VALUES ('a','retirement','USD',110,0,1),('b','taxable','USD',210,0,1);
                CREATE TABLE securities(security_id TEXT,ticker TEXT,issuer_id TEXT,currency TEXT,eligible INTEGER);
                INSERT INTO securities VALUES ('AA','AAA','issuer_a','USD',1),('BB','BBB','issuer_b','USD',1);
            """)
        self.config = {
            "source": {
                "path": str(self.source),
                "positions_query": "SELECT * FROM holdings WHERE dated <= :as_of",
                "accounts_query": "SELECT * FROM accounts",
                "securities_query": "SELECT * FROM securities",
                "column_map": {
                    "positions": {
                        "account_id": "acct",
                        "security_id": "sid",
                        "market_value": "amount",
                        "currency": "ccy",
                        "valuation_date": "dated",
                    }
                },
            },
            "research": {"path": str(self.directory / "research.sqlite")},
        }

    def tearDown(self):
        self.temp.cleanup()

    def test_latest_coherent_snapshot_and_source_unchanged(self):
        before = hashlib.sha256(self.source.read_bytes()).hexdigest()
        result = load_portfolio(self.config, "2026-09-30")
        self.assertEqual(result["valuation_date"], "2026-08-31")
        self.assertEqual(set(result["positions"].valuation_date), {"2026-08-31"})
        self.assertEqual(result["positions"].market_value.sum(), 320)
        self.assertTrue(result["positions"].quantity.isna().all())
        self.assertIn("INCOMPLETE_SNAPSHOT_SKIPPED", {i["code"] for i in result["issues"]})
        self.assertEqual(before, hashlib.sha256(self.source.read_bytes()).hexdigest())

    def test_date_cutoff_and_mapping_aliases(self):
        result = load_portfolio(self.config, "2026-08-01")
        self.assertEqual(result["valuation_date"], "2026-07-31")
        self.assertEqual(result["positions"].market_value.sum(), 300)
        self.config["source"]["positions_query"] = (
            "SELECT acct account_id,sid security_id,amount market_value,ccy currency,dated valuation_date FROM holdings"
        )
        self.config["source"]["column_map"] = {}
        self.assertEqual(load_portfolio(self.config, "2026-08-01")["valuation_date"], "2026-07-31")

    def test_mutating_sql_and_extensions_are_denied(self):
        for query in (
            "DELETE FROM holdings",
            "PRAGMA user_version=3",
            "ATTACH DATABASE ':memory:' AS other",
            "SELECT load_extension('evil')",
            "SELECT * FROM holdings; DELETE FROM holdings",
        ):
            with self.subTest(query=query):
                self.config["source"]["positions_query"] = query
                with self.assertRaises(ValueError):
                    load_portfolio(self.config, "2026-09-30")
        with sqlite3.connect(self.source) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM holdings").fetchone()[0], 5)

    def test_duplicate_rows_remain_and_block(self):
        with sqlite3.connect(self.source) as db:
            db.execute("INSERT INTO holdings VALUES ('a','AA',110,'USD','2026-08-31',1)")
        result = load_portfolio(self.config, "2026-08-31")
        self.assertEqual(len(result["positions"]), 3)
        self.assertIn("DUPLICATE_KEYS", {i["code"] for i in result["issues"]})

    def test_dated_account_incompleteness_skips_latest_snapshot(self):
        with sqlite3.connect(self.source) as db:
            db.execute("ALTER TABLE accounts ADD COLUMN valuation_date TEXT")
            db.execute("UPDATE accounts SET valuation_date='2026-08-31',complete=0")
            db.execute("INSERT INTO accounts VALUES ('a','retirement','USD',100,0,1,'2026-07-31')")
            db.execute("INSERT INTO accounts VALUES ('b','taxable','USD',200,0,1,'2026-07-31')")
        result = load_portfolio(self.config, "2026-08-31")
        self.assertEqual(result["valuation_date"], "2026-07-31")
        self.assertEqual(result["accounts"].total_value.sum(), 300)

    def test_missing_cash_is_not_inferred(self):
        with sqlite3.connect(self.source) as db:
            db.execute("UPDATE accounts SET cash=NULL,total_value=1000 WHERE account_id='a'")
        result = load_portfolio(self.config, "2026-08-31")
        self.assertTrue(pd.isna(result["accounts"].iloc[0]["cash"]))
        self.assertIn("MISSING_ACCOUNT_TOTALS", {i["code"] for i in result["issues"]})

    def test_explicit_cash_only_empty_positions_is_valid_but_missing_cash_is_not(self):
        with sqlite3.connect(self.source) as db:
            db.execute("DELETE FROM holdings")
            db.execute("UPDATE accounts SET cash=total_value")
        result = load_portfolio(self.config, "2026-08-31")
        codes = {issue["code"] for issue in result["issues"]}
        self.assertIn("NO_SECURITIES_HELD", codes)
        self.assertNotIn("NO_POSITIONS", codes)
        with sqlite3.connect(self.source) as db:
            db.execute("UPDATE accounts SET cash=NULL WHERE account_id='a'")
        result = load_portfolio(self.config, "2026-08-31")
        self.assertIn("NO_POSITIONS", {issue["code"] for issue in result["issues"]})

    def test_supplemental_security_provenance_columns_preserved(self):
        with sqlite3.connect(self.source) as db:
            db.execute("ALTER TABLE securities ADD COLUMN market_cap_as_of TEXT")
            db.execute("ALTER TABLE securities ADD COLUMN market_cap_available_at TEXT")
            db.execute("ALTER TABLE securities ADD COLUMN market_cap_received_at TEXT")
            db.execute(
                "UPDATE securities SET market_cap_as_of='2026-08-31',market_cap_available_at='2026-08-31T20:00:00Z',market_cap_received_at='2026-08-31T20:01:00Z'"
            )
        result = load_portfolio(self.config, "2026-08-31")
        self.assertEqual(result["securities"].iloc[0]["market_cap_as_of"], "2026-08-31")
        self.assertEqual(
            result["securities"].iloc[0]["market_cap_available_at"], "2026-08-31T20:00:00Z"
        )
        self.assertEqual(
            result["securities"].iloc[0]["market_cap_received_at"], "2026-08-31T20:01:00Z"
        )

    def test_source_research_alias_and_unrelated_db_protection(self):
        self.config["research"]["path"] = str(self.source)
        with self.assertRaises(ValueError):
            load_portfolio(self.config, "2026-08-31")
        before = self.source.read_bytes()
        with self.assertRaises(ValueError):
            ResearchStore(self.source)
        self.assertEqual(before, self.source.read_bytes())

    def test_inspection_exposes_schema_not_row_values(self):
        result = inspect_database(self.source)
        self.assertTrue(result["read_only"])
        self.assertEqual(
            {table["name"] for table in result["tables"]}, {"holdings", "accounts", "securities"}
        )
        self.assertNotIn("issuer_a", json.dumps(result))

    def test_immutable_runs_and_null_safe_replay(self):
        bundle = load_portfolio(self.config, "2026-08-31")
        bundle["prices"] = pd.DataFrame(
            {
                "value": [np.float64(1.2), np.nan],
                "date": pd.to_datetime(["2026-08-30", "2026-08-31"], utc=True),
            }
        )
        store = ResearchStore(self.config["research"]["path"])
        run_id = store.save_run(
            {"run_id": "fixed", "risk": {"x": np.inf, "y": pd.NA, "z": pd.NaT}}, self.config, bundle
        )
        self.assertEqual(run_id, "fixed")
        result = store.load_run(run_id)
        self.assertIsNone(result["risk"]["x"])
        self.assertIsNone(result["risk"]["y"])
        self.assertIsNone(result["risk"]["z"])
        self.assertIn("config_hash", result["metadata"])
        self.assertEqual(len(store.list_runs()), 1)
        restored = store.load_bundle(run_id)
        pd.testing.assert_frame_equal(restored["prices"], bundle["prices"])
        with self.assertRaises(ValueError):
            store.save_run({"run_id": "fixed"}, self.config, bundle)
        with sqlite3.connect(store.path) as db:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("DELETE FROM research_runs")
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE research_runs SET result_json='{}'")
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("INSERT OR REPLACE INTO research_runs SELECT * FROM research_runs")


if __name__ == "__main__":
    unittest.main()
