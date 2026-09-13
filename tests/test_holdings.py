import copy
import tempfile
import unittest

from portfolio.companion import ChromeCompanion
from portfolio.storage import Store

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
