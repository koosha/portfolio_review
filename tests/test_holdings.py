import copy
import tempfile
import unittest

from portfolio.companion import ChromeCompanion
from portfolio.storage import Store, version_at_least

TABLE = dict(
    method="yahoo-holdings-table-v1",
    headers=["Symbol", "Shares", "Last Price", "AC/Share", "Total Cost ($)", "Market Value ($)"],
    rows=[["DEMO", "12.345", "20.10", "18.25", "225.29625", "248.1345"]],
    page_count=1,
    expected_count=1,
    completeness="count-verified",
)


class HoldingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.source = self.store.add_source(
            "Synthetic", url="https://finance.yahoo.com/portfolio/p_fixture"
        )

    def test_table_capture_preserves_cost_meaning_decimal_values_and_provenance(self):
        result = self.store.ingest_table(self.source, TABLE)
        snapshot = self.store.snapshot(result["snapshot_id"])
        position = self.store.positions()[0]
        self.assertEqual(position["quantity"], "12.345")
        self.assertEqual(position["average_cost"], "18.25")
        self.assertEqual(position["total_cost"], "225.29625")
        self.assertEqual(position["market_value"], "248.1345")
        self.assertIsNone(position["purchase_price"])
        self.assertIsNone(position["currency"])
        self.assertEqual(snapshot["capture"]["method"], "yahoo-holdings-table-v1")
        self.assertEqual(snapshot["rows"][0]["raw"]["Total Cost ($)"], "225.29625")
        self.assertTrue(self.store.ingest_table(self.source, TABLE)["unchanged"])
        self.assertEqual(len(Store(self.temp.name).positions()), 1)

    def test_incomplete_malformed_empty_and_missing_shares_keep_previous_data(self):
        previous = self.store.ingest_table(self.source, TABLE)
        changes = [
            {"rows": []},
            {"expected_count": 64},
            {"completeness": "unknown"},
            {"rows": [["DEMO"]]},
            {"rows": [["DEMO", "bad", "1", "1", "1", "1"]]},
            {"headers": ["Symbol", "Price"]},
            {"page_count": True},
        ]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.store.ingest_table(self.source, {**copy.deepcopy(TABLE), **change})
        self.assertEqual(self.store.sources()[0]["snapshot_id"], previous["snapshot_id"])

    def test_unknown_total_exposes_a_warning(self):
        result = self.store.ingest_table(
            self.source, {**TABLE, "expected_count": None, "completeness": "end-observed"}
        )
        self.assertTrue(any("Completeness is unverified" in x for x in result["warnings"]))

    def test_blank_rows_cannot_claim_verified_completeness(self):
        previous = self.store.ingest_table(self.source, TABLE)
        for blank in ("", "   "):
            table = copy.deepcopy(TABLE)
            table["rows"].append([blank] * len(table["headers"]))
            table["expected_count"] += 1
            with self.subTest(blank=blank), self.assertRaisesRegex(ValueError, "no Symbol"):
                self.store.ingest_table(self.source, table)
        self.assertEqual(self.store.sources()[0]["snapshot_id"], previous["snapshot_id"])
        self.assertEqual(len(self.store.snapshots(self.source)), 1)

    def test_unlabeled_action_columns_do_not_shift_saved_position_values(self):
        table = copy.deepcopy(TABLE)
        table["headers"] = ["Unlabeled column 1"] + table["headers"] + ["Unlabeled column 8"]
        table["rows"] = [[""] + table["rows"][0] + ["Edit"]]
        result = self.store.ingest_table(self.source, table)
        position = self.store.positions()[0]
        self.assertEqual(position["symbol"], "DEMO")
        self.assertEqual(position["quantity"], "12.345")
        self.assertEqual(position["market_value"], "248.1345")
        snapshot = self.store.snapshot(result["snapshot_id"])
        self.assertEqual(snapshot["holding_summary"]["total_market_value"], "248.1345")
        self.assertEqual(snapshot["rows"][0]["raw"]["Unlabeled column 8"], "Edit")

    def test_add_prompt_preserved_as_missing_quantity_and_not_a_position(self):
        table = copy.deepcopy(TABLE)
        table["headers"].append("Yahoo Shares display")
        table["rows"][0].append("12.345")
        table["rows"].append(["UNHELD", "", "50", "--", "--", "--", "Add"])
        table["expected_count"] = 2
        result = self.store.ingest_table(self.source, table)
        self.assertEqual(result["position_count"], 1)
        snapshot = self.store.snapshot(result["snapshot_id"])
        self.assertIsNone(snapshot["rows"][1]["quantity"])
        self.assertEqual(snapshot["rows"][1]["raw"]["Yahoo Shares display"], "Add")
        self.assertEqual([p["symbol"] for p in self.store.positions()], ["DEMO"])
        self.assertEqual(snapshot["holding_summary"]["total_market_value"], "248.1345")

    def test_direct_bridge_ingests_table_and_rejects_another_account(self):
        browser = ChromeCompanion(self.store)
        for correct in (False, True):
            browser.submit("refresh", [self.source])
            job = browser.take()
            self.assertEqual(job["collection_method"], "holdings-table-v1")
            result = browser.complete(
                dict(
                    id=job["id"],
                    ok=True,
                    url=job["url"] if correct else job["url"] + "wrong",
                    table=TABLE,
                )
            )
            self.assertEqual(result["ok"], correct)
        self.assertEqual(len(self.store.positions()), 1)

    def listing_table(self, capture_version=2):
        table = copy.deepcopy(TABLE)
        table["headers"].append("Yahoo quote symbol")
        table["rows"][0].append("RY.TO")
        if capture_version is not None:
            table["capture_version"] = capture_version
        return table

    def test_v2_capture_from_current_extension_stores_quote_symbols_in_rows(self):
        for version in ("1.2.0", "1.10.3"):
            with self.subTest(version=version):
                store = Store(tempfile.mkdtemp(dir=self.temp.name))
                source = store.add_source(
                    "Synthetic", url="https://finance.yahoo.com/portfolio/p_fixture"
                )
                result = store.ingest_table(source, self.listing_table(), extension_version=version)
                snapshot = store.snapshot(result["snapshot_id"])
                self.assertEqual(snapshot["capture"]["capture_version"], 2)
                self.assertEqual(snapshot["rows"][0]["quote_symbol"], "RY.TO")
                self.assertEqual(snapshot["rows"][0]["raw"]["Yahoo quote symbol"], "RY.TO")
                self.assertFalse(any("Listing metadata ignored" in w for w in result["warnings"]))
                self.assertNotIn("quote_symbol", store.positions()[0])
                self.assertEqual(store.positions()[0]["symbol"], "DEMO")

    def test_v2_capture_from_older_extension_drops_quote_symbols_with_warning(self):
        warning = (
            "Listing metadata ignored: reload the Local Portfolio extension (1.2.0 or newer) "
            "to capture Yahoo quote symbols."
        )
        for version in ("1.1.6", None, "older / unknown", "1.2"):
            with self.subTest(version=version):
                store = Store(tempfile.mkdtemp(dir=self.temp.name))
                source = store.add_source(
                    "Synthetic", url="https://finance.yahoo.com/portfolio/p_fixture"
                )
                result = store.ingest_table(source, self.listing_table(), extension_version=version)
                snapshot = store.snapshot(result["snapshot_id"])
                self.assertEqual(snapshot["capture"]["capture_version"], 1)
                self.assertIsNone(snapshot["rows"][0]["quote_symbol"])
                self.assertNotIn("Yahoo quote symbol", snapshot["rows"][0]["raw"])
                self.assertNotIn(b"Yahoo quote symbol", store.snapshot(result["snapshot_id"], True))
                self.assertIn(warning, result["warnings"])
                self.assertIn(warning, snapshot["warnings"])
                self.assertEqual(snapshot["rows"][0]["market_value"], "248.1345")

    def test_quote_symbol_column_without_v2_declaration_is_dropped(self):
        result = self.store.ingest_table(
            self.source, self.listing_table(capture_version=None), extension_version="1.2.0"
        )
        snapshot = self.store.snapshot(result["snapshot_id"])
        self.assertEqual(snapshot["capture"]["capture_version"], 1)
        self.assertIsNone(snapshot["rows"][0]["quote_symbol"])
        self.assertTrue(any("Listing metadata ignored" in w for w in result["warnings"]))

    def test_v1_capture_records_version_one_without_listing_warning(self):
        result = self.store.ingest_table(self.source, TABLE, extension_version="1.2.0")
        snapshot = self.store.snapshot(result["snapshot_id"])
        self.assertEqual(snapshot["capture"]["capture_version"], 1)
        self.assertIsNone(snapshot["rows"][0]["quote_symbol"])
        self.assertFalse(any("Listing metadata ignored" in w for w in result["warnings"]))
        second = self.store.ingest_table(self.source, {**TABLE, "capture_version": 1})
        self.assertTrue(second["unchanged"])

    def test_malformed_capture_version_is_rejected_and_keeps_previous_data(self):
        previous = self.store.ingest_table(self.source, TABLE)
        for value in (0, 3, "2", 2.0, True, None, [2]):
            table = self.listing_table()
            table["capture_version"] = value
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "capture version"):
                self.store.ingest_table(self.source, table, extension_version="1.2.0")
        self.assertEqual(self.store.sources()[0]["snapshot_id"], previous["snapshot_id"])
        self.assertEqual(len(self.store.snapshots(self.source)), 1)

    def test_csv_rows_without_quote_symbol_column_have_no_quote_symbol(self):
        result = self.store.ingest(self.source, b"Symbol,Quantity\nAAPL,1\n")
        self.assertIsNone(self.store.snapshot(result["snapshot_id"])["rows"][0]["quote_symbol"])
        listed = self.store.ingest(self.source, b"Symbol,Quantity,Yahoo quote symbol\nRY,1,RY.TO\n")
        self.assertEqual(
            self.store.snapshot(listed["snapshot_id"])["rows"][0]["quote_symbol"], "RY.TO"
        )

    def test_version_at_least_compares_first_three_numeric_parts(self):
        cases = (
            ("1.2.0", True),
            ("1.2.1", True),
            ("1.10.0", True),
            ("2.0.0", True),
            ("v1.2.0", True),
            ("1.2.0.7", True),
            ("1.2.0-beta", True),
            ("1.1.9", False),
            ("0.9.99", False),
            ("1.2", False),
            ("", False),
            (None, False),
            ("latest", False),
        )
        for version, expected in cases:
            with self.subTest(version=version):
                self.assertIs(version_at_least(version, "1.2.0"), expected)
        with self.assertRaises(ValueError):
            version_at_least("1.2.0", "1.2")
