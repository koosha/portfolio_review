import copy
import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from portfolio.storage import Store
from portfolio_research.adapter import (
    _read_source,
    account_id,
    assert_separate_store,
    load_collector,
    supplemental_template,
    validate_supplemental,
)


def table(symbol="DEMO", value="10.01", verified=True):
    return {
        "method": "yahoo-holdings-table-v1",
        "headers": [
            "Symbol",
            "Shares",
            "Last Price",
            "Market Value ($)",
            "AC/Share",
            "Total Cost ($)",
        ],
        "rows": [
            [symbol, "1.000000000000000001", value, value, "8.001", "8.001000000000000008001"],
            ["Total Cash", "", "2.03", "", "", ""],
            ["WATCH", "", "1", "", "", ""],
        ],
        "page_count": 1,
        "expected_count": 3 if verified else None,
        "completeness": "count-verified" if verified else "end-observed",
    }


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.a = self.store.add_source(
            "Synthetic account", url="https://finance.yahoo.com/portfolio/p_fixture_private_locator"
        )
        self.as_of = "2026-09-30"

    def publish(self, captures, timestamp="2026-09-13T18:00:00+00:00"):
        with patch("portfolio.storage.now", return_value=timestamp):
            batch = self.store.begin_batch(list(captures))
            results = {
                sid: self.store.ingest_table(sid, capture, batch_id=batch)
                for sid, capture in captures.items()
            }
        return batch, results

    def evidence(self, snapshot_id, symbol="DEMO", source_id=None, **account_changes):
        source_id = source_id or self.a
        account = {
            "source_id": source_id,
            "snapshot_id": snapshot_id,
            "valuation_date": "2026-09-13",
            "cash": "2.03",
            "total_value": "12.04",
            "currency": "USD",
            "position_currency": "USD",
            "complete": True,
            "account_type": "retirement",
        }
        account.update(account_changes)
        return {
            "version": 1,
            "accounts": [account],
            "securities": [
                {
                    "source_id": source_id,
                    "raw_symbol": symbol,
                    "security_id": "security-001",
                    "issuer_id": "issuer-001",
                    "ticker": symbol,
                    "valid_from": "2020-01-01",
                    "instrument_type": "equity",
                    "currency": "USD",
                    "eligible": True,
                }
            ],
            "tax_lots": [],
        }

    def test_reads_legacy_data_without_migration_or_private_url_leakage(self):
        with patch("portfolio.storage.now", return_value="2026-09-13T18:00:00+00:00"):
            self.store.ingest_table(self.a, table(verified=False))
        before = self.store.path.read_bytes()
        result = load_collector(self.store.path, self.as_of)
        self.assertEqual(before, self.store.path.read_bytes())
        self.assertIn("NO_COMPLETE_BATCH", {issue["code"] for issue in result["issues"]})
        self.assertIsNone(result["valuation_date"])
        self.assertTrue(result["collector"]["legacy_partial"])
        self.assertEqual(result["ledger"]["accounts"][0]["cash"], "2.03")
        self.assertIsNone(result["ledger"]["accounts"][0]["total_value"])
        self.assertIsNone(result["ledger"]["accounts"][0]["currency"])
        self.assertNotIn("finance.yahoo.com", json.dumps(result["ledger"]))
        self.assertNotIn(str(self.store.path), json.dumps(result["sources"]))

    def test_cash_watchlist_and_exact_decimal_ledger_survive_projection(self):
        _, results = self.publish({self.a: table()})
        result = load_collector(
            self.store.path, self.as_of, self.evidence(results[self.a]["snapshot_id"])
        )
        self.assertEqual(len(result["positions"]), 1)
        self.assertEqual(len(result["ledger"]["excluded_rows"]), 1)
        self.assertEqual(result["ledger"]["positions"][0]["quantity"], "1.000000000000000001")
        self.assertEqual(result["ledger"]["positions"][0]["total_cost"], "8.001000000000000008001")
        self.assertEqual(result["ledger"]["accounts"][0]["reconciliation_residual"], "0.00")
        self.assertTrue(result["accounts"].iloc[0]["complete"])
        self.assertEqual(result["valuation_date"], "2026-09-13")

    def test_incomplete_refresh_uses_previous_exact_full_batch(self):
        b = self.store.add_source("Second", url="https://finance.yahoo.com/portfolio/p_fixture_B")
        old_batch, old = self.publish({self.a: table("FIRST"), b: table("SECOND")})
        with patch("portfolio.storage.now", return_value="2026-09-14T18:00:00+00:00"):
            failed_batch = self.store.begin_batch([self.a, b])
            self.store.ingest_table(self.a, table("NEW"), batch_id=failed_batch)
            self.store.failed(b, "Synthetic failure", batch_id=failed_batch)
        result = load_collector(self.store.path, self.as_of)
        self.assertEqual(result["collector"]["batch"]["id"], old_batch)
        self.assertEqual(set(result["positions"]["raw_symbol"]), {"FIRST", "SECOND"})
        self.assertEqual(
            set(result["positions"]["snapshot_id"]),
            {value["snapshot_id"] for value in old.values()},
        )
        self.assertIn("PRIOR_COMPLETE_BATCH", {issue["code"] for issue in result["issues"]})

    def test_single_account_pull_never_becomes_household_batch_and_sold_rows_do_not_reappear(self):
        b = self.store.add_source("Second", url="https://finance.yahoo.com/portfolio/p_fixture_B")
        old_batch, _ = self.publish({self.a: table("SOLD"), b: table("HELD")})
        single, _ = self.publish({self.a: table("BOUGHT")}, "2026-09-14T18:00:00+00:00")
        combined = load_collector(self.store.path, self.as_of)
        self.assertEqual(combined["collector"]["batch"]["id"], old_batch)
        one = load_collector(self.store.path, self.as_of, account_ids=[account_id(self.a)])
        self.assertEqual(one["collector"]["batch"]["id"], single)
        self.assertEqual(list(one["positions"]["raw_symbol"]), ["BOUGHT"])
        new_batch, _ = self.publish(
            {self.a: table("BOUGHT"), b: table("HELD")}, "2026-09-15T18:00:00+00:00"
        )
        new = load_collector(self.store.path, self.as_of)
        self.assertEqual(new["collector"]["batch"]["id"], new_batch)
        self.assertNotIn("SOLD", list(new["positions"]["raw_symbol"]))

    def test_supplemental_cash_and_nav_do_not_follow_new_snapshot(self):
        _, old = self.publish({self.a: table()})
        evidence = self.evidence(old[self.a]["snapshot_id"])
        self.publish({self.a: table(value="11.01")}, "2026-09-14T18:00:00+00:00")
        result = load_collector(self.store.path, self.as_of, evidence)
        self.assertIn("STALE_ACCOUNT_SUPPLEMENT", {issue["code"] for issue in result["issues"]})
        self.assertIsNone(result["ledger"]["accounts"][0]["total_value"])
        self.assertIsNone(result["valuation_date"])

    def test_unknown_cash_is_never_the_nav_residual_or_zero(self):
        capture = table()
        capture["rows"] = capture["rows"][:1]
        capture["expected_count"] = 1
        _, results = self.publish({self.a: capture})
        evidence = self.evidence(results[self.a]["snapshot_id"], cash=None, complete=False)
        result = load_collector(self.store.path, self.as_of, evidence)
        self.assertIsNone(result["ledger"]["accounts"][0]["cash"])
        self.assertFalse(result["accounts"].iloc[0]["complete"])

    def test_unlabeled_rows_are_not_an_exception_and_still_reconcile_to_nav(self):
        """A source with no currency column is ordinary, not broken.

        The row keeps no currency of its own -- an account label is not a row label and
        still performs no conversion -- but the captured value is read as reported in the
        account's own currency, so the NAV check runs instead of being skipped in silence.
        """
        _, results = self.publish({self.a: table()})
        evidence = self.evidence(results[self.a]["snapshot_id"])
        del evidence["accounts"][0]["position_currency"]
        result = load_collector(self.store.path, self.as_of, evidence)
        self.assertIsNone(result["ledger"]["positions"][0]["currency"])
        self.assertEqual({issue["code"] for issue in result["issues"]}, set())
        self.assertTrue(result["accounts"].iloc[0]["complete"])
        self.assertEqual(result["ledger"]["accounts"][0]["reconciliation_residual"], "0.00")

    def test_unlabeled_rows_that_do_not_reach_nav_still_raise_nav_mismatch(self):
        _, results = self.publish({self.a: table()})
        evidence = self.evidence(results[self.a]["snapshot_id"], total_value="99.04")
        del evidence["accounts"][0]["position_currency"]
        result = load_collector(self.store.path, self.as_of, evidence)
        self.assertIn("NAV_MISMATCH", {issue["code"] for issue in result["issues"]})
        self.assertFalse(result["accounts"].iloc[0]["complete"])

    def test_a_row_labelled_in_another_currency_still_refuses_the_nav_sum(self):
        capture = table()
        capture["headers"].append("Currency")
        for row in capture["rows"]:
            row.append("CAD" if row[0] == "DEMO" else "USD")
        _, results = self.publish({self.a: capture})
        evidence = self.evidence(results[self.a]["snapshot_id"])
        del evidence["accounts"][0]["position_currency"]
        result = load_collector(self.store.path, self.as_of, evidence)
        self.assertEqual(result["ledger"]["positions"][0]["currency"], "CAD")
        self.assertIsNone(result["ledger"]["accounts"][0].get("reconciliation_residual"))
        self.assertFalse(result["accounts"].iloc[0]["complete"])

    def test_an_unlabeled_row_with_no_value_is_still_a_missing_value_error(self):
        """Dropping the currency exception does not drop the value one: a held quantity
        with no market value is a real gap and stays an error on an unlabelled page."""
        capture = table()
        capture["rows"][0][3] = ""
        _, results = self.publish({self.a: capture})
        evidence = self.evidence(results[self.a]["snapshot_id"])
        del evidence["accounts"][0]["position_currency"]
        result = load_collector(self.store.path, self.as_of, evidence)
        codes = {(issue["code"], issue["severity"]) for issue in result["issues"]}
        self.assertIn(("MISSING_POSITION_VALUE", "error"), codes)
        self.assertNotIn("MISSING_POSITION_CURRENCY", {code for code, _ in codes})
        self.assertFalse(result["accounts"].iloc[0]["complete"])

    def test_an_account_with_no_currency_at_all_is_still_an_error(self):
        _, results = self.publish({self.a: table()})
        evidence = self.evidence(results[self.a]["snapshot_id"], currency=None, complete=False)
        del evidence["accounts"][0]["position_currency"]
        result = load_collector(self.store.path, self.as_of, evidence)
        codes = {(issue["code"], issue["severity"]) for issue in result["issues"]}
        self.assertIn(("MISSING_CURRENCY", "error"), codes)
        self.assertNotIn("MISSING_POSITION_CURRENCY", {code for code, _ in codes})
        self.assertFalse(result["accounts"].iloc[0]["complete"])

    def test_cash_currency_mismatch_and_absent_domicile_are_not_inferred(self):
        capture = table()
        capture["headers"].append("Currency")
        for row in capture["rows"]:
            row.append("EUR" if row[0] == "Total Cash" else "USD")
        _, results = self.publish({self.a: capture})
        evidence = self.evidence(results[self.a]["snapshot_id"])
        result = load_collector(self.store.path, self.as_of, evidence)
        self.assertFalse(result["accounts"].iloc[0]["complete"])
        self.assertEqual(result["ledger"]["accounts"][0]["captured_cash_currency"], "EUR")
        self.assertIn("CASH_CURRENCY_MISMATCH", {issue["code"] for issue in result["issues"]})
        self.assertIsNone(result["securities"].iloc[0]["domicile"])
        self.assertIsNone(result["securities"].iloc[0]["equity_type"])
        evidence["securities"][0].update(
            domicile="US", equity_type="ordinary_common", exchange="XNAS"
        )
        explicit = load_collector(self.store.path, self.as_of, evidence)
        self.assertEqual(explicit["securities"].iloc[0]["domicile"], "US")
        self.assertEqual(explicit["securities"].iloc[0]["equity_type"], "ordinary_common")
        self.assertEqual(explicit["ledger"]["security_aliases"][0]["valid_from"], "2020-01-01")
        self.assertEqual(explicit["ledger"]["security_aliases"][0]["exchange"], "XNAS")

    def test_explicit_share_classes_stay_distinct_but_share_issuer(self):
        capture = table("GOOG")
        capture["rows"].append(["GOOGL", "1", "5", "5", "4", "4"])
        capture["expected_count"] = 4
        _, results = self.publish({self.a: capture})
        evidence = self.evidence(results[self.a]["snapshot_id"], symbol="GOOG", total_value="17.04")
        second = {
            **evidence["securities"][0],
            "raw_symbol": "GOOGL",
            "ticker": "GOOGL",
            "security_id": "security-002",
            "share_class": "A",
        }
        evidence["securities"][0]["share_class"] = "C"
        evidence["securities"].append(second)
        result = load_collector(self.store.path, self.as_of, evidence)
        self.assertEqual(set(result["securities"]["security_id"]), {"security-001", "security-002"})
        self.assertEqual(set(result["securities"]["issuer_id"]), {"issuer-001"})
        self.assertEqual(set(result["positions"]["security_id"]), {"security-001", "security-002"})

    def test_date_only_captures_do_not_become_valuation_dates_and_future_receipts_excluded(self):
        self.publish({self.a: table()}, "2026-09-13T18:00:00+00:00")
        result = load_collector(self.store.path, "2026-09-12")
        self.assertTrue(result["positions"].empty)
        self.assertIsNone(result["valuation_date"])
        self.assertIn("MISSING_ACCOUNT_SNAPSHOT", {issue["code"] for issue in result["issues"]})

    def test_unverified_capture_requires_explicit_reconciliation(self):
        _, results = self.publish({self.a: table(verified=False)})
        no_evidence = load_collector(self.store.path, self.as_of)
        self.assertFalse(no_evidence["accounts"].iloc[0]["complete"])
        evidence = self.evidence(results[self.a]["snapshot_id"])
        accepted = load_collector(self.store.path, self.as_of, evidence)
        self.assertTrue(accepted["accounts"].iloc[0]["complete"])
        self.assertEqual(accepted["collector"]["batch"]["completeness"], "unverified")
        self.assertEqual(
            [
                issue["severity"]
                for issue in accepted["issues"]
                if issue["code"] == "UNVERIFIED_CAPTURE"
            ],
            ["warning"],
        )

    def test_decimal_nav_mismatch_blocks_completion(self):
        _, results = self.publish({self.a: table()})
        result = load_collector(
            self.store.path,
            self.as_of,
            self.evidence(results[self.a]["snapshot_id"], total_value="12.06"),
        )
        self.assertEqual(result["ledger"]["accounts"][0]["reconciliation_residual"], "0.02")
        self.assertFalse(result["accounts"].iloc[0]["complete"])
        self.assertIn("NAV_MISMATCH", {issue["code"] for issue in result["issues"]})

    def test_source_is_read_only_and_source_store_aliases_rejected(self):
        with _read_source(self.store.path) as (db, _):
            for query in (
                "DELETE FROM sources",
                "PRAGMA query_only=OFF",
                "ATTACH DATABASE ':memory:' AS extra",
                "SELECT load_extension('no-extension')",
            ):
                with self.subTest(query=query), self.assertRaises(sqlite3.DatabaseError):
                    db.execute(query)
        link = Path(self.temp.name) / "alias.sqlite3"
        link.symlink_to(self.store.path)
        with self.assertRaises(ValueError):
            assert_separate_store(self.store.path, link)
        link.unlink()
        link.hardlink_to(self.store.path)
        with self.assertRaises(ValueError):
            assert_separate_store(self.store.path, link)

    def test_wal_read_is_consistent_and_does_not_copy_or_modify_source(self):
        _, results = self.publish({self.a: table()})
        evidence = self.evidence(results[self.a]["snapshot_id"])
        writer = sqlite3.connect(self.store.path)
        self.addCleanup(writer.close)
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("UPDATE sources SET name='Synthetic WAL label' WHERE id=?", (self.a,))
        writer.commit()
        before = hashlib.sha256(self.store.path.read_bytes()).hexdigest()
        result = load_collector(self.store.path, self.as_of, evidence)
        self.assertEqual(result["ledger"]["accounts"][0]["name"], "Synthetic WAL label")
        self.assertEqual(before, hashlib.sha256(self.store.path.read_bytes()).hexdigest())

    def test_template_contains_editable_missing_values_but_no_private_locators(self):
        self.publish({self.a: table()})
        template = supplemental_template(self.store.path)
        self.assertEqual(template["accounts"][0]["source_id"], self.a)
        self.assertIsNone(template["accounts"][0]["currency"])
        self.assertEqual(len(template["securities"]), 1)
        self.assertNotIn("finance.yahoo.com", json.dumps(template))
        self.assertNotIn(str(self.store.path), json.dumps(template))
        validate_supplemental(template)

    def test_validation_rejects_unsafe_ambiguity_and_keeps_decimal_strings(self):
        evidence = self.evidence(1)
        accepted = validate_supplemental(evidence)
        self.assertEqual(accepted["accounts"][0]["cash"], "2.03")
        changes = [
            {"complete": "true"},
            {"currency": "usd"},
            {"cash": "NaN"},
            {"cash": True},
            {"total_value": "-1"},
            {"snapshot_id": None},
            {"valuation_date": "2026-02-30"},
            {"tax_rate": "1.1"},
            {"private_key": "invalid-field"},
        ]
        for change in changes:
            invalid = copy.deepcopy(evidence)
            invalid["accounts"][0].update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_supplemental(invalid)
        duplicate = copy.deepcopy(evidence)
        duplicate["accounts"].append(copy.deepcopy(duplicate["accounts"][0]))
        with self.assertRaises(ValueError):
            validate_supplemental(duplicate)
        with self.assertRaises(ValueError):
            load_collector(self.store.path, self.as_of, [])

    def test_tax_lots_require_open_snapshot_basis_and_cannot_be_created_from_average_cost(self):
        _, results = self.publish({self.a: table()})
        snapshot_id = results[self.a]["snapshot_id"]
        evidence = self.evidence(snapshot_id)
        without = load_collector(self.store.path, self.as_of, evidence)
        self.assertTrue(without["tax_lots"].empty)
        evidence["tax_lots"] = [
            {
                "source_id": self.a,
                "snapshot_id": snapshot_id,
                "security_id": "security-001",
                "lot_id": "lot-001",
                "quantity": "1.000000000000000001",
                "basis_per_share": "8.001",
                "acquired_date": "2025-01-01",
                "currency": "USD",
            }
        ]
        result = load_collector(self.store.path, self.as_of, evidence)
        self.assertEqual(result["ledger"]["tax_lots"][0]["basis_per_share"], "8.001")
        evidence["tax_lots"][0]["snapshot_id"] += 1
        stale = load_collector(self.store.path, self.as_of, evidence)
        self.assertTrue(stale["tax_lots"].empty)
        self.assertIn("STALE_TAX_LOT", {issue["code"] for issue in stale["issues"]})

    def test_market_cap_provenance_requires_aware_timestamps_and_is_preserved(self):
        _, results = self.publish({self.a: table()})
        evidence = self.evidence(results[self.a]["snapshot_id"])
        evidence["securities"][0].update(
            market_cap="1234567890.12",
            market_cap_as_of="2026-09-13",
            market_cap_available_at="2026-09-13T09:00:00-04:00",
            market_cap_received_at="2026-09-13T14:00:00Z",
        )
        result = load_collector(self.store.path, self.as_of, evidence)
        security = result["ledger"]["securities"][0]
        self.assertEqual(security["market_cap"], "1234567890.12")
        self.assertEqual(security["market_cap_as_of"], "2026-09-13")
        self.assertEqual(security["market_cap_available_at"], "2026-09-13T13:00:00+00:00")
        self.assertEqual(security["market_cap_received_at"], "2026-09-13T14:00:00+00:00")
        for bad in ("2026-09-13", "2026-09-13T13:00:00"):
            evidence["securities"][0]["market_cap_available_at"] = bad
            with self.subTest(value=bad), self.assertRaises(ValueError):
                validate_supplemental(evidence)
