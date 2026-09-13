import tempfile
import unittest
from unittest.mock import patch

from portfolio.companion import ChromeCompanion
from portfolio.storage import Store


def table(symbol="DEMO", shares="1", verified=True):
    return {
        "method": "yahoo-holdings-table-v1",
        "headers": ["Symbol", "Shares", "Last Price", "Market Value ($)"],
        "rows": [[symbol, shares, "10", str(int(shares) * 10)]],
        "page_count": 1,
        "expected_count": 1 if verified else None,
        "completeness": "count-verified" if verified else "end-observed",
    }


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.browser = ChromeCompanion(self.store)
        self.ids = [
            self.store.add_source(name, url=f"https://finance.yahoo.com/portfolio/p_fixture_{name}")
            for name in ("A", "B")
        ]

    def batch(self, identifier):
        with self.store.connect() as db:
            return dict(
                db.execute("SELECT * FROM import_batches WHERE id=?", (identifier,)).fetchone()
            )

    def members(self, identifier):
        with self.store.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM import_batch_members WHERE batch_id=? ORDER BY source_id",
                    (identifier,),
                )
            ]

    def finish_job(self, ok=True, captured=None):
        job = self.browser.take()
        answer = self.browser.complete(
            {
                "id": job["id"],
                "url": job["url"],
                "ok": ok,
                "table": captured or table(),
                "error": "Synthetic capture failed.",
            }
        )
        return job, answer

    def test_publication_is_atomic_and_preserves_exact_snapshot_members(self):
        self.browser.submit("refresh", self.ids)
        batch_id = self.browser.jobs[0]["batch_id"]
        _, first = self.finish_job()
        self.assertEqual(self.batch(batch_id)["status"], "collecting")
        self.assertEqual([row["status"] for row in self.members(batch_id)], ["success", "pending"])
        _, second = self.finish_job()
        self.assertEqual(self.batch(batch_id)["status"], "published")
        self.assertEqual(self.batch(batch_id)["completeness"], "count-verified")
        self.assertEqual(
            [row["snapshot_id"] for row in self.members(batch_id)],
            [first["snapshot_id"], second["snapshot_id"]],
        )

    def test_successful_unverified_capture_does_not_claim_verified_holdings(self):
        self.browser.submit("refresh", [self.ids[0]])
        identifier = self.browser.jobs[0]["batch_id"]
        self.finish_job(captured=table(verified=False))
        self.assertEqual(self.batch(identifier)["status"], "published")
        self.assertEqual(self.batch(identifier)["completeness"], "unverified")

    def test_failure_retains_successful_account_but_does_not_publish_batch(self):
        self.browser.submit("refresh", self.ids)
        identifier = self.browser.jobs[0]["batch_id"]
        self.finish_job(ok=False)
        self.finish_job()
        self.assertEqual(self.batch(identifier)["status"], "failed")
        self.assertEqual([row["status"] for row in self.members(identifier)], ["failed", "success"])
        self.assertEqual(len(self.store.snapshots(self.ids[1])), 1)

    def test_unchanged_capture_keeps_snapshot_but_has_new_batch_receipt(self):
        with patch("portfolio.storage.now", return_value="2026-09-01T18:00:00+00:00"):
            batch1 = self.store.begin_batch([self.ids[0]])
            first = self.store.ingest_table(self.ids[0], table(), batch_id=batch1)
        with patch("portfolio.storage.now", return_value="2026-09-02T18:00:00+00:00"):
            batch2 = self.store.begin_batch([self.ids[0]])
            second = self.store.ingest_table(self.ids[0], table(), batch_id=batch2)
        self.assertTrue(second["unchanged"])
        self.assertEqual(first["snapshot_id"], second["snapshot_id"])
        self.assertNotEqual(
            self.members(batch1)[0]["received_at"], self.members(batch2)[0]["received_at"]
        )
        self.assertEqual(
            self.store.snapshot(first["snapshot_id"])["captured_at"], "2026-09-01T18:00:00+00:00"
        )

    def test_selection_scope_is_frozen_and_cannot_change_while_busy(self):
        self.browser.submit("refresh", [self.ids[0]])
        identifier = self.browser.jobs[0]["batch_id"]
        with self.assertRaises(ValueError):
            self.browser.select_source(self.ids[1], False)
        self.finish_job()
        self.browser.select_source(self.ids[1], False)
        self.assertEqual(self.batch(identifier)["requested_sources_json"], f"[{self.ids[0]}]")
        self.assertEqual(self.batch(identifier)["selected_scope_json"], str(self.ids))

    def test_timeout_disconnect_and_restart_never_publish_pending_members(self):
        for operation in ("timeout", "disconnect", "restart"):
            with self.subTest(operation=operation):
                self.browser.submit("refresh", [self.ids[0]])
                identifier = self.browser.jobs[0]["batch_id"]
                if operation == "timeout":
                    self.browser.jobs[0]["created"] = 0
                    self.browser.status()
                elif operation == "disconnect":
                    self.browser.submit("disconnect")
                else:
                    self.browser = ChromeCompanion(self.store)
                self.assertIn(self.batch(identifier)["status"], {"failed", "abandoned"})
                self.assertEqual(self.members(identifier)[0]["status"], "failed")

    def test_bad_member_rolls_back_snapshot_and_rejects_republication(self):
        identifier = self.store.begin_batch([self.ids[0]])
        with self.assertRaises(ValueError):
            self.store.ingest_table(self.ids[1], table(), batch_id=identifier)
        self.assertEqual(self.store.snapshots(self.ids[1]), [])
        self.store.ingest_table(self.ids[0], table(), batch_id=identifier)
        with self.assertRaises(ValueError):
            self.store.ingest_table(self.ids[0], table(shares="2"), batch_id=identifier)
        self.assertEqual(len(self.store.snapshots(self.ids[0])), 1)
