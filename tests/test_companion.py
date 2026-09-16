import json
import tempfile
import threading
import time
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from portfolio.companion import ChromeCompanion
from portfolio.server import make_handler
from portfolio.storage import Store

CSV = b"Symbol,Quantity,Purchase Price\nAAPL,1.25,180.50\n"


class CompanionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "data")
        self.browser = ChromeCompanion(self.store)
        self.source = self.store.add_source("IRA", url="https://finance.yahoo.com/portfolio/p_1")

    def capture(self, job, data=CSV):
        import csv
        import io

        records = list(csv.reader(io.StringIO(data.decode())))
        table = dict(
            method="yahoo-holdings-table-v1",
            headers=records[0],
            rows=records[1:],
            page_count=1,
            expected_count=len(records) - 1,
            completeness="count-verified",
        )
        return dict(id=job["id"], ok=True, url=job["url"], table=table)

    def test_pairing_persists_and_disconnect_revokes(self):
        key = self.browser.key
        self.assertTrue(ChromeCompanion(self.store).authenticate(key))
        self.assertFalse(self.browser.authenticate("wrong"))
        self.browser.submit("disconnect")
        self.assertFalse(self.browser.authenticate(key))
        self.assertNotEqual(self.browser.key, key)

    def test_idle_poll_never_initiates_a_sync(self):
        self.assertIsNone(self.browser.take())
        self.assertTrue(self.browser.status()["browser_open"])
        self.assertEqual(self.store.snapshots(self.source), [])

    def test_selection_is_saved_before_a_concurrent_pull_can_start(self):
        saving = threading.Event()
        allow_save = threading.Event()
        pull_started = threading.Event()
        pull_finished = threading.Event()
        selection_errors, pull_errors = [], []
        original_select = self.store.select_source

        def delayed_select(source_id, selected):
            saving.set()
            if not allow_save.wait(3):
                raise RuntimeError("Timed out waiting to save test selection.")
            original_select(source_id, selected)

        def select():
            try:
                self.browser.select_source(self.source, False)
            except Exception as exc:
                selection_errors.append(exc)

        def pull():
            pull_started.set()
            try:
                self.browser.submit("refresh", [self.source])
            except Exception as exc:
                pull_errors.append(exc)
            finally:
                pull_finished.set()

        selection_thread = threading.Thread(target=select)
        pull_thread = threading.Thread(target=pull)
        with patch.object(self.store, "select_source", side_effect=delayed_select):
            selection_thread.start()
            try:
                self.assertTrue(saving.wait(3))
                pull_thread.start()
                self.assertTrue(pull_started.wait(3))
                self.assertFalse(pull_finished.wait(0.1))
            finally:
                allow_save.set()
                selection_thread.join(3)
                if pull_thread.ident is not None:
                    pull_thread.join(3)
        self.assertFalse(selection_thread.is_alive())
        self.assertFalse(pull_thread.is_alive())
        self.assertEqual(selection_errors, [])
        self.assertEqual(len(pull_errors), 1)
        self.assertIsInstance(pull_errors[0], ValueError)
        self.assertIn("Select at least one portfolio", str(pull_errors[0]))
        self.assertFalse(self.store.source(self.source)["selected"])
        self.assertFalse(self.browser.status()["busy"])
        self.assertEqual(self.browser.jobs, [])

    def test_connect_does_not_assert_yahoo_authentication(self):
        self.browser.submit("connect")
        job = self.browser.take()
        self.browser.complete(dict(id=job["id"], ok=True))
        self.assertIn("Complete Google sign-in", self.browser.status()["message"])

    def test_full_refresh_saves_table_and_is_idempotent(self):
        self.browser.submit("refresh", [self.source])
        job = self.browser.take()
        self.assertIsNone(self.browser.take())
        result = self.browser.complete(self.capture(job))
        self.assertTrue(result["ok"])
        self.assertEqual(self.browser.complete(self.capture(job)), result)
        self.assertEqual(len(self.store.snapshots(self.source)), 1)
        self.assertEqual(self.store.positions()[0]["quantity"], "1.25")
        self.assertFalse(self.browser.status()["busy"])

    def test_arbitrary_file_reads_rejected(self):
        self.browser.submit("refresh", [self.source])
        job = self.browser.take()
        result = self.browser.complete(
            dict(id=job["id"], ok=True, url=job["url"], filename="/etc/passwd")
        )
        self.assertFalse(result["ok"])
        self.assertEqual(self.store.snapshots(self.source), [])

    def test_partial_failure_keeps_existing_data_and_other_account_imports(self):
        old = self.store.ingest(self.source, CSV)
        second = self.store.add_source("Brokerage", url="https://finance.yahoo.com/portfolio/p_2")
        self.browser.submit("refresh", [self.source, second])
        first = self.browser.take()
        self.browser.complete(dict(id=first["id"], ok=False, error="Holdings table not found."))
        next_job = self.browser.take()
        self.browser.complete(self.capture(next_job))
        self.assertEqual([r["ok"] for r in self.browser.status()["results"]], [False, True])
        self.assertEqual(self.store.snapshot(old["snapshot_id"], raw=True), CSV)

    def test_wrong_portfolio_and_bad_table_rejected(self):
        for case in ("wrong_url", "bad_csv"):
            self.browser.submit("refresh", [self.source])
            job = self.browser.take()
            payload = self.capture(job, b"<html>Sign in</html>" if case == "bad_csv" else CSV)
            if case == "wrong_url":
                payload["url"] = "https://finance.yahoo.com/portfolio/p_other"
            self.assertFalse(self.browser.complete(payload)["ok"])
        self.assertEqual(self.store.snapshots(self.source), [])

    def test_timed_out_jobs_release_busy_state(self):
        self.browser.submit("refresh", [self.source])
        self.browser.jobs[0]["created"] = time.time() - 301
        self.assertFalse(self.browser.status()["busy"])
        self.assertIn("did not finish", self.browser.status()["error"])

    def test_discovery_validates_domains_and_deduplicates(self):
        self.browser.submit("discover")
        job = self.browser.take()
        links = [
            {"href": "https://finance.yahoo.com/portfolio/p_1", "text": "IRA"},
            {"href": "https://finance.yahoo.com/portfolio/p_1/view/v1", "text": "IRA"},
            {"href": "https://evil.example/portfolio/p_2", "text": "Ignore"},
        ]
        self.assertTrue(self.browser.complete(dict(id=job["id"], ok=True, links=links))["ok"])
        self.assertEqual(len(self.browser.status()["discovered"]), 1)

    def test_discovery_keeps_seven_manual_and_six_linked_accounts(self):
        self.browser.submit("discover")
        job = self.browser.take()
        links = [
            {"href": f"https://finance.yahoo.com/portfolio/p_{i}", "text": f"Manual {i}"}
            for i in range(7)
        ]
        links += [
            {
                "href": f"https://finance.yahoo.com/portfolio/yodlee%7C11111111-2222-3333-4444-{i:012d}/view",
                "text": f"Linked {i}",
            }
            for i in range(6)
        ]
        self.browser.complete(
            dict(id=job["id"], ok=True, links=links, portfolio_names=[x["text"] for x in links])
        )
        found = self.browser.status()["discovered"]
        self.assertEqual(len(found), 13)
        self.assertEqual(len({item["url"] for item in found}), 13)
        self.assertIsNone(self.browser.status()["discovery_warning"])

    def test_discovery_reports_rows_that_cannot_be_identified(self):
        self.browser.submit("discover")
        job = self.browser.take()
        self.browser.complete(
            dict(
                id=job["id"],
                ok=True,
                links=[{"href": "https://finance.yahoo.com/portfolio/p_1", "text": "IRA"}],
                portfolio_names=["IRA", "Missing linked account"],
            )
        )
        self.assertIn("Missing linked account", self.browser.status()["discovery_warning"])
        self.assertIn("incomplete", self.browser.status()["message"])

    def test_linked_account_can_be_registered_and_refreshed(self):
        source = self.store.add_source(
            "Linked account",
            url="https://finance.yahoo.com/portfolio/yodlee%7C11111111-2222-3333-4444-555555555555/view",
        )
        self.browser.submit("refresh", [source])
        job = self.browser.take()
        self.assertIn("yodlee%7C", job["url"])
        result = self.browser.complete(self.capture(job))
        self.assertTrue(result["ok"])
        self.assertEqual(self.store.snapshot(result["snapshot_id"])["source_id"], source)

    def listing_capture(self, job):
        payload = self.capture(job, b"Symbol,Shares,Yahoo quote symbol\nRY,2,RY.TO\n")
        payload["table"]["capture_version"] = 2
        return payload

    def test_status_reports_required_version_and_listing_capture_support(self):
        status = self.browser.status()
        self.assertEqual(status["required_extension_version"], "1.2.0")
        self.assertFalse(status["listing_capture_supported"])
        for version, supported in (
            ("1.1.6", False),
            (None, False),
            ("1.2.0", True),
            ("2.0.1", True),
        ):
            with self.subTest(version=version):
                self.browser.take(version)
                self.assertEqual(self.browser.status()["listing_capture_supported"], supported)
                self.assertEqual(self.browser.status()["required_extension_version"], "1.2.0")

    def test_complete_passes_extension_version_to_listing_capture(self):
        self.browser.submit("refresh", [self.source])
        job = self.browser.take("1.2.0")
        result = self.browser.complete(self.listing_capture(job))
        self.assertTrue(result["ok"])
        snapshot = self.store.snapshot(result["snapshot_id"])
        self.assertEqual(snapshot["capture"]["capture_version"], 2)
        self.assertEqual(snapshot["rows"][0]["quote_symbol"], "RY.TO")

    def test_the_claiming_client_version_decides_the_capture_not_a_later_poll(self):
        """A second paired profile polling while a capture runs must not downgrade it."""
        self.browser.submit("refresh", [self.source])
        job = self.browser.take("1.2.0")
        self.assertIsNone(self.browser.take("1.1.6"))  # another profile, older extension
        result = self.browser.complete(self.listing_capture(job))
        snapshot = self.store.snapshot(result["snapshot_id"])
        self.assertEqual(snapshot["capture"]["capture_version"], 2)
        self.assertEqual(snapshot["rows"][0]["quote_symbol"], "RY.TO")
        self.assertFalse(any("Listing metadata ignored" in w for w in result["warnings"]))
        self.assertEqual(self.browser.status()["extension_version"], "1.1.6")

    def test_older_extension_listing_capture_is_saved_without_quote_symbols(self):
        self.browser.submit("refresh", [self.source])
        job = self.browser.take("1.1.6")
        result = self.browser.complete(self.listing_capture(job))
        self.assertTrue(result["ok"])
        snapshot = self.store.snapshot(result["snapshot_id"])
        self.assertEqual(snapshot["capture"]["capture_version"], 1)
        self.assertIsNone(snapshot["rows"][0]["quote_symbol"])
        self.assertTrue(any("Listing metadata ignored" in w for w in result["warnings"]))
        self.assertEqual(self.store.positions()[0]["quantity"], "2")


class CompanionHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.browser = ChromeCompanion(self.store)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), lambda *args: None)
        self.port = self.server.server_port
        self.server.RequestHandlerClass = make_handler(self.store, self.browser, self.port)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def request(self, method, path, headers=None, body=None):
        connection = HTTPConnection("127.0.0.1", self.port, timeout=3)
        connection.request(method, path, headers=headers or {}, body=body)
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return result

    def test_extension_preflight_and_authenticated_job(self):
        origin = "chrome-extension://" + "a" * 32
        status, headers, _ = self.request("OPTIONS", "/api/companion/job", {"Origin": origin})
        self.assertEqual(status, 204)
        self.assertEqual(headers["Access-Control-Allow-Origin"], origin)
        self.browser.submit("connect")
        status, headers, body = self.request(
            "GET", "/api/companion/job", {"Origin": origin, "X-Companion-Key": self.browser.key}
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["job"]["action"], "connect")

    def test_companion_key_cannot_access_normal_app_endpoints(self):
        headers = {"Origin": "chrome-extension://" + "a" * 32, "X-Companion-Key": self.browser.key}
        self.assertEqual(self.request("GET", "/api/state", headers)[0], 403)
        self.assertEqual(self.request("GET", "/api/export", headers)[0], 403)

    def test_unauthenticated_and_foreign_origins_rejected(self):
        self.assertEqual(self.request("GET", "/api/companion/job")[0], 403)
        self.assertEqual(
            self.request(
                "GET",
                "/api/companion/job",
                {"Origin": "https://evil.example", "X-Companion-Key": self.browser.key},
            )[0],
            403,
        )
        self.assertEqual(
            self.request("OPTIONS", "/api/companion/job", {"Origin": "https://evil.example"})[0],
            403,
        )

    def test_checkbox_selection_controls_pull_and_survives_restart(self):
        self.store.remember_portfolios(
            [
                {"name": f"Portfolio {i}", "url": f"https://finance.yahoo.com/portfolio/p_{i}"}
                for i in range(13)
            ]
        )
        sources = self.store.sources()
        _, _, body = self.request("GET", "/api/state")
        headers = {"Content-Type": "application/json", "X-Local-Token": json.loads(body)["token"]}
        self.assertEqual(self.request("POST", "/api/pull", headers, "{}")[0], 400)
        for source in sources[4:]:
            self.assertEqual(
                self.request(
                    "POST", f"/api/sources/{source['id']}/selection", headers, '{"selected":true}'
                )[0],
                200,
            )
        status, _, body = self.request("POST", "/api/pull", headers, "{}")
        self.assertEqual(status, 202)
        self.assertEqual(json.loads(body)["portfolio_count"], 9)
        self.assertEqual(
            [j["source_id"] for j in self.browser.jobs], [s["id"] for s in sources[4:]]
        )
        self.assertEqual(
            self.request(
                "POST", f"/api/sources/{sources[0]['id']}/selection", headers, '{"selected":true}'
            )[0],
            400,
        )
        self.assertEqual(
            [s["selected"] for s in Store(self.temp.name).sources()], [0] * 4 + [1] * 9
        )

    def test_queued_accounts_do_not_expire_while_prior_pull_is_running(self):
        first = self.store.add_source("First", url="https://finance.yahoo.com/portfolio/p_1")
        second = self.store.add_source("Second", url="https://finance.yahoo.com/portfolio/p_2")
        self.browser.submit("refresh", [first, second])
        job = self.browser.take()
        for queued in self.browser.jobs:
            queued["created"] = time.time() - 400
        self.assertTrue(self.browser.status()["busy"])
        self.browser.complete(dict(id=job["id"], ok=False, error="Holdings unavailable"))
        self.assertEqual(self.browser.take()["source_id"], second)
        self.assertEqual(self.browser.status()["progress"]["completed"], 1)

    def test_authenticated_table_collection_through_http_reports_version_and_saves_positions(self):
        source = self.store.add_source(
            "Synthetic", url="https://finance.yahoo.com/portfolio/p_fixture"
        )
        self.browser.submit("refresh", [source])
        headers = {
            "Origin": "chrome-extension://" + "a" * 32,
            "X-Companion-Key": self.browser.key,
            "X-Companion-Version": "1.1.0",
            "Content-Type": "application/json",
        }
        status, _, body = self.request("GET", "/api/companion/job", headers)
        job = json.loads(body)["job"]
        table = dict(
            method="yahoo-holdings-table-v1",
            headers=["Symbol", "Shares"],
            rows=[["DEMO", "1.2345678901"]],
            page_count=1,
            expected_count=1,
            completeness="count-verified",
        )
        status, _, body = self.request(
            "POST",
            "/api/companion/result",
            headers,
            json.dumps(dict(id=job["id"], ok=True, url=job["url"], table=table)),
        )
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])
        self.assertEqual(self.store.positions()[0]["quantity"], "1.2345678901")
        self.assertEqual(self.browser.status()["extension_version"], "1.1.0")
