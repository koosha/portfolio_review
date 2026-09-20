import copy
import json
import re
import tempfile
import unittest
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import patch

from portfolio.storage import Store
from portfolio_research.adapter import _receipt_before, account_id, load_collector
from portfolio_research.current import current_snapshot
from tests.support.normalization import (
    LISTINGS,
    fx_table,
    issuer_lookup,
)

SUNDAY_RECEIPT = "2026-09-13T20:00:00+00:00"
SUNDAY_GENERATED = "2026-09-13T21:00:00+00:00"
FRIDAY = "2026-09-11"
MONDAY = "2026-09-14"
MONDAY_RECEIPT = "2026-09-14T19:00:00+00:00"
MONDAY_GENERATED = "2026-09-14T19:30:00+00:00"
EXCEPTION_KEY = re.compile(r"[0-9a-f]{40}")
EXCEPTION_FIELDS = {
    "key",
    "code",
    "severity",
    "scope",
    "source_id",
    "snapshot_id",
    "account_id",
    "raw_symbol",
    "message",
    "proposed",
    "candidates",
    "resolution",
}


def covered_usd(response):
    """Every converted amount in the presentation, counted once.

    Checked against the reported covered total, so no holding is both converted and
    left out of the sum whatever currency it turned out to be reported in.
    """
    amounts = [row["market_value_usd"] for row in response["positions"]]
    amounts += [(row.get("usd") or {}).get("cash") for row in response["accounts"]]
    return sum(Decimal(value) for value in amounts if value is not None)


@contextmanager
def cached_providers(fx=None, listings=None, search=None):
    """Listing, search, FX and issuer providers answering from fixture data only.

    Every provider call is recorded with its ``refresh`` flag; a network fetch fails.
    """
    known = LISTINGS if listings is None else listings
    table = fx_table() if fx is None else fx
    candidates = search or {}
    calls = []

    def metadata(config, symbol, *, refresh, issues):
        calls.append(("listing", symbol, refresh))
        found = known.get(symbol)
        return copy.deepcopy(found) if found else None

    def searcher(config, text, *, refresh, issues, limit=5):
        calls.append(("search", text, refresh))
        return copy.deepcopy(candidates.get(text, []))

    def fx_loader(config, currencies, start_date, end_date, *, refresh, issues):
        calls.append(("fx", tuple(currencies), refresh))
        return table

    with (
        patch("portfolio_research.market_listings.listing_metadata", side_effect=metadata),
        patch("portfolio_research.market_listings.search_listings", side_effect=searcher),
        patch("portfolio_research.fx_providers.load_fx_table", side_effect=fx_loader),
        patch("portfolio_research.issuers.sec_issuer_lookup", return_value=issuer_lookup),
        patch("portfolio_lab.providers._fetch_json", side_effect=AssertionError("network")),
    ):
        yield calls


def table(symbol="DEMO", value="10", currency="USD"):
    """A count-verified Yahoo holdings capture; ``currency=None`` omits the column."""
    headers = ["Symbol", "Shares", "Last Price", "Market Value ($)"]
    rows = [[symbol, "1", value, value], ["Total Cash", "", "2.03", ""]]
    if currency:
        headers = [*headers, "Currency"]
        rows = [[*row, currency] for row in rows]
    return {
        "method": "yahoo-holdings-table-v1",
        "headers": headers,
        "rows": rows,
        "page_count": 1,
        "expected_count": len(rows),
        "completeness": "count-verified",
    }


class CurrentSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.a = self.store.add_source(
            "Labelled account", url="https://finance.yahoo.com/portfolio/p_fixture_current_a"
        )
        self.b = self.store.add_source(
            "Unlabelled account", url="https://finance.yahoo.com/portfolio/p_fixture_current_b"
        )

    def captures(self):
        return {self.a: table("DEMO"), self.b: table("OTHER", currency=None)}

    def publish(self, captures, timestamp=SUNDAY_RECEIPT):
        with patch("portfolio.storage.now", return_value=timestamp):
            batch = self.store.begin_batch(list(captures))
            results = {
                sid: self.store.ingest_table(sid, capture, batch_id=batch)
                for sid, capture in captures.items()
            }
        return batch, results

    def evidence(self, snapshots, valuation_date=FRIDAY):
        """Attested balances for ``snapshots`` ({source_id: snapshot_id})."""
        return {
            "version": 1,
            "accounts": [
                {
                    "source_id": source_id,
                    "snapshot_id": snapshot_id,
                    "valuation_date": valuation_date,
                    "cash": "2.03",
                    "total_value": "12.03",
                    "currency": "USD",
                    "position_currency": "USD",
                    "complete": True,
                }
                for source_id, snapshot_id in snapshots.items()
            ],
            "securities": [],
            "tax_lots": [],
        }

    def snapshot(self, **kwargs):
        return current_snapshot(self.store.path, generated_at=SUNDAY_GENERATED, **kwargs)

    @staticmethod
    def by_source(response):
        return {account["source_id"]: account for account in response["accounts"]}

    def test_sunday_publication_is_current_on_sunday(self):
        batch, results = self.publish(self.captures())
        response = self.snapshot()
        self.assertEqual(response["schema_version"], 1)
        self.assertEqual(response["review_kind"], "current")
        collection = response["collection"]
        self.assertEqual(collection["status"], "published")
        self.assertEqual(collection["batch_id"], batch)
        self.assertEqual(collection["created_at"], SUNDAY_RECEIPT)
        self.assertEqual(collection["completed_at"], SUNDAY_RECEIPT)
        self.assertEqual(collection["completeness"], "count-verified")
        self.assertEqual(collection["account_count"], 2)
        self.assertFalse(collection["newer_collection_failed"])
        dates = response["dates"]
        self.assertEqual(dates["collection_received_at"], SUNDAY_RECEIPT)
        self.assertEqual(dates["market_observation_date"], FRIDAY)
        self.assertEqual(dates["generated_at"], SUNDAY_GENERATED)
        self.assertEqual(dates["information_cutoff"], SUNDAY_GENERATED)
        self.assertEqual(dates["review_month"], "2026-09")
        self.assertIsNone(dates["source_valuation_time"])
        self.assertIn("not a quote time", dates["labels"]["collection_received_at"])
        timeline = response["timeline"]
        self.assertEqual(timeline["review_kind"], "current")
        self.assertEqual(timeline["decision_date"], FRIDAY)
        self.assertEqual(timeline["collection_received_at"], SUNDAY_RECEIPT)
        self.assertEqual(timeline["earliest_execution_date"], "2026-09-14")
        self.assertEqual(len(response["accounts"]), 2)
        accounts = self.by_source(response)
        labelled, unlabelled = accounts[self.a], accounts[self.b]
        self.assertEqual(labelled["account_id"], account_id(self.a))
        self.assertEqual(labelled["name"], "Labelled account")
        self.assertEqual(labelled["snapshot_id"], results[self.a]["snapshot_id"])
        self.assertEqual(labelled["captured_at"], SUNDAY_RECEIPT)
        self.assertEqual(labelled["received_at"], SUNDAY_RECEIPT)
        self.assertEqual(labelled["completeness"], "count-verified")
        self.assertEqual(labelled["row_count"], 2)
        self.assertEqual(labelled["position_count"], 1)
        self.assertEqual(labelled["excluded_count"], 0)
        self.assertEqual(labelled["cash"], {"amount": "2.03", "currency": "USD"})
        self.assertTrue(labelled["cash_currency_known"])
        self.assertIsNone(labelled["valuation_date"])
        self.assertEqual(labelled["value_basis"], "captured_as_observed")
        self.assertIsInstance(labelled["exceptions"], list)
        self.assertEqual(unlabelled["cash"], {"amount": "2.03", "currency": None})
        self.assertFalse(unlabelled["cash_currency_known"])
        self.assertEqual(unlabelled["value_basis"], "captured_as_observed")

    def test_subtotals_and_totals_group_by_explicit_currency_without_fx(self):
        self.publish(self.captures())
        response = self.snapshot()
        accounts = self.by_source(response)
        [labelled] = accounts[self.a]["subtotals"]
        self.assertEqual(labelled["currency"], "USD")
        self.assertEqual(labelled["holdings"], "10")
        self.assertEqual(labelled["cash"], "2.03")
        self.assertEqual(labelled["total"], "12.03")
        self.assertEqual(labelled["position_count"], 1)
        [unlabelled] = accounts[self.b]["subtotals"]
        self.assertIsNone(unlabelled["currency"])
        self.assertEqual(unlabelled["holdings"], "10")
        self.assertEqual(unlabelled["cash"], "2.03")
        self.assertEqual(unlabelled["total"], "12.03")
        totals = response["totals"]
        self.assertFalse(totals["reconciled_nav"])
        self.assertIn("no FX conversion", totals["label"])
        by_currency = {row["currency"]: row for row in totals["by_currency"]}
        self.assertEqual(set(by_currency), {"USD", None})
        self.assertEqual(by_currency["USD"]["holdings"], "10")
        self.assertEqual(by_currency["USD"]["cash"], "2.03")
        self.assertEqual(by_currency["USD"]["total"], "12.03")
        self.assertEqual(by_currency["USD"]["account_count"], 1)
        self.assertEqual(by_currency[None]["holdings"], "10")
        self.assertEqual(by_currency[None]["account_count"], 1)
        for row in totals["by_currency"]:
            for field in ("holdings", "cash", "total"):
                self.assertIsInstance(row[field], str)
                Decimal(row[field])

    def test_positions_are_dated_by_capture_and_keep_exact_strings(self):
        _, results = self.publish(self.captures())
        response = self.snapshot()
        accounts = self.by_source(response)
        self.assertEqual(len(response["positions"]), 2)
        for position in response["positions"]:
            account = accounts[position["source_id"]]
            self.assertEqual(position["account_id"], account["account_id"])
            self.assertEqual(position["snapshot_id"], results[position["source_id"]]["snapshot_id"])
            self.assertEqual(position["observed_at"], account["captured_at"])
            self.assertEqual(position["observed_at"], SUNDAY_RECEIPT)
            self.assertEqual(position["value_basis"], "captured_as_observed")
            self.assertEqual(position["quantity"], "1")
            self.assertEqual(position["price"], "10")
            self.assertEqual(position["market_value"], "10")
            self.assertIsNone(position["average_cost"])
            self.assertIsNone(position["total_cost"])
            self.assertTrue(position["security_id"])
            self.assertIn("resolution_status", position)
            self.assertIsInstance(position["row_number"], int)
        symbols = {position["symbol"]: position for position in response["positions"]}
        self.assertEqual(symbols["DEMO"]["currency"], "USD")
        self.assertIsNone(symbols["OTHER"]["currency"])
        self.assertEqual(response["excluded_rows"], [])

    def test_legacy_snapshots_without_batch_are_listed_as_partial(self):
        with patch("portfolio.storage.now", return_value=SUNDAY_RECEIPT):
            results = {
                sid: self.store.ingest_table(sid, capture)
                for sid, capture in self.captures().items()
            }
        response = self.snapshot()
        self.assertEqual(response["collection"]["status"], "legacy_partial")
        self.assertIsNone(response["collection"]["batch_id"])
        self.assertEqual(response["collection"]["account_count"], 2)
        self.assertEqual(response["dates"]["collection_received_at"], SUNDAY_RECEIPT)
        self.assertEqual(len(response["accounts"]), 2)
        for account in response["accounts"]:
            self.assertEqual(account["snapshot_id"], results[account["source_id"]]["snapshot_id"])
            self.assertEqual(account["captured_at"], SUNDAY_RECEIPT)
        self.assertEqual(len(response["positions"]), 2)
        codes = {issue["code"] for issue in response["exceptions"]}
        self.assertIn("NO_COMPLETE_BATCH", codes)

    def test_no_sources_or_no_snapshots_report_none_without_raising(self):
        empty = Store(tempfile.mkdtemp(dir=self.temp.name))
        for label, path in (("no sources", empty.path), ("no snapshots", self.store.path)):
            with self.subTest(label=label):
                response = current_snapshot(path, generated_at=SUNDAY_GENERATED)
                self.assertEqual(response["collection"]["status"], "none")
                self.assertIsNone(response["collection"]["batch_id"])
                if label == "no sources":
                    self.assertEqual(response["accounts"], [])
                for account in response["accounts"]:
                    self.assertIsNone(account["snapshot_id"])
                    self.assertIsNone(account["captured_at"])
                self.assertEqual(response["positions"], [])
                self.assertEqual(response["totals"]["by_currency"], [])
                self.assertIsNone(response["dates"]["collection_received_at"])
                self.assertIsNone(response["dates"]["source_valuation_time"])
                self.assertEqual(response["dates"]["market_observation_date"], FRIDAY)
                json.dumps(response, allow_nan=False)

    def test_attested_evidence_sets_value_basis_and_common_valuation_time(self):
        _, results = self.publish(self.captures())
        snapshots = {sid: result["snapshot_id"] for sid, result in results.items()}
        response = self.snapshot(supplemental=self.evidence(snapshots))
        accounts = self.by_source(response)
        for account in accounts.values():
            self.assertEqual(account["value_basis"], "attested")
            self.assertEqual(account["valuation_date"], FRIDAY)
        self.assertEqual(response["dates"]["source_valuation_time"], FRIDAY)
        self.assertEqual(response["collection"]["status"], "published")
        self.assertEqual(response["dates"]["collection_received_at"], SUNDAY_RECEIPT)

    def test_partial_attestation_keeps_source_valuation_time_unknown(self):
        _, results = self.publish(self.captures())
        response = self.snapshot(
            supplemental=self.evidence({self.a: results[self.a]["snapshot_id"]})
        )
        accounts = self.by_source(response)
        self.assertEqual(accounts[self.a]["value_basis"], "attested")
        self.assertEqual(accounts[self.a]["valuation_date"], FRIDAY)
        self.assertEqual(accounts[self.b]["value_basis"], "captured_as_observed")
        self.assertIsNone(accounts[self.b]["valuation_date"])
        self.assertIsNone(response["dates"]["source_valuation_time"])

    def test_stale_supplement_is_not_applied(self):
        _, results = self.publish(self.captures())
        stale = self.evidence({self.a: results[self.a]["snapshot_id"] + 100})
        response = self.snapshot(supplemental=stale)
        accounts = self.by_source(response)
        self.assertEqual(accounts[self.a]["value_basis"], "captured_as_observed")
        self.assertIsNone(accounts[self.a]["valuation_date"])
        self.assertIsNone(response["dates"]["source_valuation_time"])
        codes = {issue["code"] for issue in response["exceptions"]}
        self.assertIn("STALE_ACCOUNT_SUPPLEMENT", codes)
        self.assertIn("STALE_ACCOUNT_SUPPLEMENT", accounts[self.a]["exceptions"])

    def test_exceptions_are_deduplicated_records(self):
        self.publish(self.captures())
        response = self.snapshot()
        exceptions = response["exceptions"]
        self.assertTrue(exceptions)
        for issue in exceptions:
            self.assertEqual(set(issue), {"code", "message", "severity"})
        keys = [(issue["code"], issue["message"]) for issue in exceptions]
        self.assertEqual(len(keys), len(set(keys)))

    def test_response_has_no_locators_paths_or_pairing_material(self):
        self.publish(self.captures())
        response = self.snapshot()
        serialized = json.dumps(response, allow_nan=False)
        self.assertNotIn("finance.yahoo.com", serialized)
        self.assertNotIn(str(self.store.path), serialized)
        self.assertNotIn(str(self.store.directory), serialized)
        self.assertNotIn("pairing", serialized.lower())
        self.assertNotIn("rows_json", serialized)
        self.assertNotIn("capture_json", serialized)

    def test_sunday_capture_is_current_on_sunday_but_not_historical_on_friday(self):
        self.publish(self.captures())
        response = self.snapshot()
        self.assertTrue(response["positions"])
        self.assertEqual(response["collection"]["status"], "published")
        self.assertEqual(response["dates"]["market_observation_date"], FRIDAY)
        historical = load_collector(self.store.path, FRIDAY)
        self.assertIsNone(historical["collector"]["batch"])
        self.assertTrue(historical["positions"].empty)
        codes = {issue["code"] for issue in historical["issues"]}
        self.assertIn("MISSING_ACCOUNT_SNAPSHOT", codes)

    def test_load_collector_receipt_limit_admits_sunday_publication(self):
        batch, results = self.publish(self.captures())
        result = load_collector(self.store.path, FRIDAY, receipt_through="2026-09-13")
        collector = result["collector"]
        self.assertEqual(collector["batch"]["id"], batch)
        self.assertEqual(collector["receipt_through"], "2026-09-13")
        self.assertEqual(collector["collection_received_at"], SUNDAY_RECEIPT)
        self.assertEqual(
            collector["snapshot_ids"],
            {str(sid): value["snapshot_id"] for sid, value in results.items()},
        )
        self.assertEqual(collector["captured_at"], {str(sid): SUNDAY_RECEIPT for sid in results})
        self.assertEqual(len(result["ledger"]["positions"]), 2)
        default = load_collector(self.store.path, FRIDAY)
        self.assertEqual(default["collector"]["receipt_through"], FRIDAY)
        self.assertIsNone(default["collector"]["collection_received_at"])
        self.assertEqual(default["collector"]["snapshot_ids"], {})
        with self.assertRaises(ValueError):
            load_collector(self.store.path, FRIDAY, receipt_through="2026-09-10")
        with self.assertRaises(ValueError):
            load_collector(self.store.path, FRIDAY, receipt_through="2026-09-13T00:00:00")

    def test_legacy_receipt_time_is_newest_snapshot_capture(self):
        with patch("portfolio.storage.now", return_value="2026-09-12T18:00:00+00:00"):
            self.store.ingest_table(self.a, table("DEMO"))
        with patch("portfolio.storage.now", return_value=SUNDAY_RECEIPT):
            self.store.ingest_table(self.b, table("OTHER", currency=None))
        result = load_collector(self.store.path, FRIDAY, receipt_through="2026-09-13")
        self.assertIsNone(result["collector"]["batch"])
        self.assertEqual(result["collector"]["collection_received_at"], SUNDAY_RECEIPT)
        response = self.snapshot()
        self.assertEqual(response["collection"]["status"], "legacy_partial")
        self.assertEqual(response["dates"]["collection_received_at"], SUNDAY_RECEIPT)
        accounts = self.by_source(response)
        self.assertEqual(accounts[self.a]["captured_at"], "2026-09-12T18:00:00+00:00")
        self.assertEqual(accounts[self.b]["captured_at"], SUNDAY_RECEIPT)

    def test_intraday_current_review_attests_evidence_dated_on_the_generation_day(self):
        _, results = self.publish(self.captures(), timestamp=MONDAY_RECEIPT)
        snapshots = {sid: result["snapshot_id"] for sid, result in results.items()}
        evidence = self.evidence(snapshots, valuation_date=MONDAY)
        evidence["securities"] = [
            {
                "source_id": self.a,
                "raw_symbol": "DEMO",
                "security_id": "security-demo",
                "issuer_id": "issuer-demo",
                "valid_from": MONDAY,
            }
        ]
        evidence["tax_lots"] = [
            {
                "source_id": self.a,
                "snapshot_id": snapshots[self.a],
                "security_id": "security-demo",
                "lot_id": "lot-monday",
                "acquired_date": MONDAY,
                "quantity": "1",
                "basis_per_share": "8",
                "currency": "USD",
            }
        ]
        response = current_snapshot(
            self.store.path, supplemental=evidence, generated_at=MONDAY_GENERATED
        )
        self.assertEqual(response["dates"]["market_observation_date"], FRIDAY)
        self.assertEqual(response["dates"]["source_valuation_time"], MONDAY)
        codes = {issue["code"] for issue in response["exceptions"]}
        for code in (
            "FUTURE_VALUATION",
            "FUTURE_TAX_LOT",
            "UNMATCHED_TAX_LOT",
            "NO_COMMON_VALUATION_DATE",
            "MISSING_ACCOUNT_TOTALS",
            "MISSING_CURRENCY",
        ):
            self.assertNotIn(code, codes)
        accounts = self.by_source(response)
        for account in accounts.values():
            self.assertEqual(account["value_basis"], "attested")
            self.assertEqual(account["valuation_date"], MONDAY)
            self.assertEqual(account["cash"], {"amount": "2.03", "currency": "USD"})
        demo = next(row for row in response["positions"] if row["symbol"] == "DEMO")
        self.assertEqual(demo["security_id"], "security-demo")
        self.assertEqual(demo["resolution_status"], "resolved")
        bundle = load_collector(
            self.store.path,
            FRIDAY,
            evidence,
            receipt_through=MONDAY,
            receipt_before=MONDAY_GENERATED,
        )
        self.assertEqual([lot["lot_id"] for lot in bundle["ledger"]["tax_lots"]], ["lot-monday"])
        self.assertEqual(bundle["valuation_date"], MONDAY)

    def test_historical_review_still_rejects_evidence_dated_after_its_decision_date(self):
        _, results = self.publish(self.captures(), timestamp="2026-09-11T19:00:00+00:00")
        snapshots = {sid: result["snapshot_id"] for sid, result in results.items()}
        evidence = self.evidence(snapshots, valuation_date=MONDAY)
        evidence["tax_lots"] = [
            {
                "source_id": self.a,
                "snapshot_id": snapshots[self.a],
                "security_id": "security-demo",
                "lot_id": "lot-monday",
                "acquired_date": MONDAY,
                "quantity": "1",
                "basis_per_share": "8",
                "currency": "USD",
            }
        ]
        historical = load_collector(self.store.path, FRIDAY, evidence)
        codes = {issue["code"] for issue in historical["issues"]}
        self.assertIn("FUTURE_VALUATION", codes)
        self.assertIn("FUTURE_TAX_LOT", codes)
        self.assertIsNone(historical["valuation_date"])
        self.assertEqual(historical["ledger"]["tax_lots"], [])

    def test_future_evidence_after_the_receipt_day_is_still_rejected(self):
        _, results = self.publish(self.captures(), timestamp=MONDAY_RECEIPT)
        snapshots = {sid: result["snapshot_id"] for sid, result in results.items()}
        response = current_snapshot(
            self.store.path,
            supplemental=self.evidence(snapshots, valuation_date="2026-09-15"),
            generated_at=MONDAY_GENERATED,
        )
        self.assertIn("FUTURE_VALUATION", {issue["code"] for issue in response["exceptions"]})
        for account in self.by_source(response).values():
            self.assertEqual(account["value_basis"], "captured_as_observed")
            self.assertIn("FUTURE_VALUATION", account["exceptions"])

    def test_collection_received_after_generation_is_not_selected(self):
        sunday, _ = self.publish(self.captures())
        with patch("portfolio.storage.now", return_value="2026-09-14T14:00:00+00:00"):
            later = self.store.begin_batch([self.a, self.b])
        with patch("portfolio.storage.now", return_value=MONDAY_RECEIPT):
            self.store.ingest_table(self.a, table("LATER"), batch_id=later)
            self.store.ingest_table(self.b, table("LATEST", currency=None), batch_id=later)
        generated = "2026-09-14T15:00:00+00:00"
        response = current_snapshot(self.store.path, generated_at=generated)
        self.assertEqual(response["collection"]["batch_id"], sunday)
        self.assertEqual(response["dates"]["collection_received_at"], SUNDAY_RECEIPT)
        self.assertLessEqual(
            response["dates"]["collection_received_at"], response["dates"]["information_cutoff"]
        )
        self.assertFalse(response["collection"]["newer_collection_failed"])
        self.assertTrue(response["collection"]["newer_collection_in_progress"])
        self.assertEqual({row["symbol"] for row in response["positions"]}, {"DEMO", "OTHER"})
        published = current_snapshot(self.store.path, generated_at=MONDAY_GENERATED)
        self.assertEqual(published["collection"]["batch_id"], later)
        self.assertFalse(published["collection"]["newer_collection_in_progress"])
        with self.assertRaises(ValueError):
            load_collector(
                self.store.path, FRIDAY, receipt_through=MONDAY, receipt_before="2026-09-14T15:00"
            )

    def test_legacy_snapshot_received_after_generation_is_not_selected(self):
        with patch("portfolio.storage.now", return_value=SUNDAY_RECEIPT):
            first = self.store.ingest_table(self.a, table("DEMO"))
        with patch("portfolio.storage.now", return_value=MONDAY_RECEIPT):
            self.store.ingest_table(self.a, table("LATER"))
        response = current_snapshot(self.store.path, generated_at="2026-09-14T15:00:00+00:00")
        accounts = self.by_source(response)
        self.assertEqual(accounts[self.a]["snapshot_id"], first["snapshot_id"])
        self.assertEqual(response["dates"]["collection_received_at"], SUNDAY_RECEIPT)

    def test_collection_in_progress_is_not_reported_as_failed(self):
        sunday, _ = self.publish(self.captures())
        with patch("portfolio.storage.now", return_value="2026-09-14T19:40:00+00:00"):
            pulling = self.store.begin_batch([self.a, self.b])
            self.store.ingest_table(self.a, table("NEWER"), batch_id=pulling)
        response = current_snapshot(self.store.path, generated_at="2026-09-14T19:45:00+00:00")
        collection = response["collection"]
        self.assertEqual(collection["status"], "published")
        self.assertEqual(collection["batch_id"], sunday)
        self.assertFalse(collection["newer_collection_failed"])
        self.assertTrue(collection["newer_collection_in_progress"])
        self.assertNotIn(
            "PRIOR_COMPLETE_BATCH", {issue["code"] for issue in response["exceptions"]}
        )
        with patch("portfolio.storage.now", return_value="2026-09-14T19:50:00+00:00"):
            self.store.failed(self.b, "Synthetic failure", batch_id=pulling)
        failed = current_snapshot(self.store.path, generated_at="2026-09-14T19:55:00+00:00")
        self.assertTrue(failed["collection"]["newer_collection_failed"])
        self.assertFalse(failed["collection"]["newer_collection_in_progress"])
        self.assertIn("PRIOR_COMPLETE_BATCH", {issue["code"] for issue in failed["exceptions"]})

    def test_first_collection_in_progress_is_not_reported_as_failed(self):
        with patch("portfolio.storage.now", return_value="2026-09-14T19:40:00+00:00"):
            pulling = self.store.begin_batch([self.a, self.b])
            self.store.ingest_table(self.a, table("FIRST"), batch_id=pulling)
        response = current_snapshot(self.store.path, generated_at="2026-09-14T19:45:00+00:00")
        self.assertEqual(response["collection"]["status"], "legacy_partial")
        self.assertFalse(response["collection"]["newer_collection_failed"])
        self.assertTrue(response["collection"]["newer_collection_in_progress"])

    def test_account_exceptions_come_from_the_collector_tags(self):
        """Shared-security repro: B's unresolved alias must appear on B's card only."""
        self.publish({self.a: table("SHARED"), self.b: table("SHARED")})
        evidence = {
            "version": 1,
            "accounts": [],
            "securities": [
                {
                    "source_id": self.a,
                    "raw_symbol": "SHARED",
                    "security_id": "security-001",
                    "issuer_id": "issuer-001",
                    "valid_from": "2026-01-01",
                },
                {"source_id": self.b, "raw_symbol": "SHARED", "security_id": "security-001"},
            ],
            "tax_lots": [],
        }
        response = self.snapshot(supplemental=evidence)
        top_level = {issue["code"] for issue in response["exceptions"]}
        self.assertIn("UNRESOLVED_SECURITY", top_level)
        self.assertIn("CONFLICTING_SECURITY_IDENTITY", top_level)
        accounts = self.by_source(response)
        self.assertIn("UNRESOLVED_SECURITY", accounts[self.b]["exceptions"])
        self.assertNotIn("UNRESOLVED_SECURITY", accounts[self.a]["exceptions"])
        for account in accounts.values():
            self.assertIn("CONFLICTING_SECURITY_IDENTITY", account["exceptions"])
            self.assertLessEqual(set(account["exceptions"]), top_level)
            self.assertEqual(account["exceptions"], sorted(account["exceptions"]))
            self.assertNotIn("NO_COMPLETE_BATCH", account["exceptions"])
        collector = load_collector(self.store.path, FRIDAY, evidence, receipt_through="2026-09-13")[
            "collector"
        ]
        self.assertEqual(
            collector["account_issue_codes"][account_id(self.b)], accounts[self.b]["exceptions"]
        )

    def test_receipt_before_requires_an_aware_parseable_moment(self):
        cutoff = datetime(2026, 9, 14, 15, tzinfo=UTC)
        self.assertTrue(_receipt_before("2026-09-14T15:00:00Z", cutoff))
        self.assertTrue(_receipt_before("2026-09-14T10:59:00-04:00", cutoff))
        self.assertFalse(_receipt_before("2026-09-14T15:00:01+00:00", cutoff))
        for value in ("2026-09-14T14:00:00", "not a time", None, 7):
            self.assertFalse(_receipt_before(value, cutoff))

    def test_identity_and_usd_presentation_use_cached_providers_only(self):
        self.publish(self.captures())
        with patch("portfolio_lab.providers._fetch_json", side_effect=AssertionError("network")):
            response = self.snapshot()
        records = response["open_exceptions"]
        # Neither symbol resolves to a listing with the providers offline, and that is
        # the only question left open: nothing has to be asked about either currency.
        self.assertEqual(
            sorted((record["code"], record["raw_symbol"]) for record in records),
            [("UNRESOLVED_LISTING", "DEMO"), ("UNRESOLVED_LISTING", "OTHER")],
        )
        for record in records:
            self.assertLessEqual(EXCEPTION_FIELDS, set(record))
            self.assertRegex(record["key"], EXCEPTION_KEY)
        self.assertEqual(response["identity"]["unresolved"], 2)
        self.assertEqual(response["fx_observations"], [])
        usd = response["totals"]["usd"]
        self.assertEqual(usd["covered_position_count"], 2)
        self.assertEqual(usd["unconverted_position_count"], 0)
        self.assertEqual(usd["unconverted_accounts"], [])
        self.assertEqual(Decimal(usd["covered_total"]), covered_usd(response))
        self.assertIn("not reconciled NAV", usd["label"])
        symbols = {position["symbol"]: position for position in response["positions"]}
        self.assertEqual(symbols["DEMO"]["market_value_usd"], "10")
        self.assertEqual(symbols["DEMO"]["value_currency"], "USD")
        self.assertEqual(symbols["DEMO"]["value_currency_basis"], "row")
        # The unlabelled account's page carried no currency column at all. OTHER is still
        # a bare ticker, so its value is presented in USD rather than counted unconverted.
        self.assertEqual(symbols["OTHER"]["market_value_usd"], "10")
        self.assertEqual(symbols["OTHER"]["value_currency"], "USD")
        self.assertEqual(symbols["OTHER"]["market_value"], "10")
        accounts = self.by_source(response)
        self.assertEqual(accounts[self.a]["open_exception_count"], 1)
        self.assertEqual(accounts[self.b]["open_exception_count"], 1)
        self.assertEqual(accounts[self.b]["identity"], {"resolved": 0, "open": 1})
        serialized = json.dumps(response, allow_nan=False)
        self.assertNotIn(self.temp.name, serialized)
        self.assertNotIn("finance.yahoo.com", serialized)
