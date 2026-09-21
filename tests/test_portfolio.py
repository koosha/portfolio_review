import json
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import MagicMock

from portfolio.server import make_handler
from portfolio.storage import Store, parse_csv, yahoo_url
from portfolio.yahoo import discover_links

CSV = b"Symbol,Current Price,Trade Date,Purchase Price,Quantity,Comment\nAAPL,220.15,2024/03/12,175.50,10,first\nAAPL,220.15,2025/05/08,190.25,2.5,second\n"


class ParsingTests(unittest.TestCase):
    def test_preserves_lots_and_decimal_precision(self):
        rows, warnings = parse_csv(CSV)
        self.assertEqual([r["quantity"] for r in rows], ["10", "2.5"])
        self.assertEqual(rows[0]["purchase_price"], "175.50")
        self.assertEqual(rows[0]["raw"]["Comment"], "first")
        self.assertTrue(any("currency" in w for w in warnings))

    def test_missing_holdings_not_fabricated(self):
        rows, warnings = parse_csv(b"Symbol,Current Price\nMSFT,430\n")
        self.assertIsNone(rows[0]["quantity"])
        self.assertIsNone(rows[0]["currency"])
        self.assertEqual(len(warnings), 3)

    def test_zero_negative_fractional_and_quoted_values(self):
        rows, _ = parse_csv(
            b'Symbol,Quantity,Purchase Price\nA,0,0\nB,-2.0000000001,"1,200.05"\nC,(3),10\n'
        )
        self.assertEqual([r["quantity"] for r in rows], ["0", "-2.0000000001", "-3"])
        self.assertEqual(rows[1]["purchase_price"], "1200.05")

    def test_bom_crlf_and_currency(self):
        rows, _ = parse_csv("\ufeffSymbol,Quantity,Currency\r\nSHOP.TO,1,cad\r\n".encode())
        self.assertEqual(rows[0]["currency"], "CAD")

    def test_rejects_invalid_files_without_dropping_bad_rows(self):
        cases = [
            b"",
            b"<html>sign in</html>",
            b"Symbol\n",
            b"Symbol,Symbol\nA,A\n",
            b"Symbol,Quantity\nA,nope\n",
            b"Symbol,Quantity\nA,NaN\n",
            b"Symbol,Quantity\nA,Infinity\n",
            b"Symbol,Quantity\n,10\n",
            b"Symbol,Quantity\nA,1e999999999\n",
            b"Symbol,Quantity\nA,1,extra\n",
            b"Symbol,Quantity\nA\n",
            b'Symbol,Quantity\nA,"broken\n',
            b"Symbol,Currency\nA,US dollars\n",
            b"Symbol\n\x00",
            b"\xff\xfe",
        ]
        for data in cases:
            with self.subTest(data=data), self.assertRaises(ValueError):
                parse_csv(data)

    def test_url_restrictions(self):
        self.assertEqual(
            yahoo_url("https://finance.yahoo.com/portfolio/p_1/view/v1?x=2"),
            "https://finance.yahoo.com/portfolio/p_1",
        )
        for url in [
            "http://finance.yahoo.com/portfolio/p_1",
            "https://evil.com/portfolio/p_1",
            "https://finance.yahoo.com.evil.com/portfolio/p_1",
            "https://finance.yahoo.com/portfolios/",
            "https://finance.yahoo.com/portfolio/new",
            "https://user@finance.yahoo.com/portfolio/p_1",
        ]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                yahoo_url(url)

    def test_linked_brokerage_url_is_canonical_and_safe(self):
        root = "https://finance.yahoo.com/portfolio/"
        account = "yodlee%7C11111111-2222-3333-4444-555555555555"
        self.assertEqual(yahoo_url(root + account + "/view"), root + account)
        self.assertEqual(yahoo_url(root + account.replace("%7C", "%7c") + "/view"), root + account)
        self.assertEqual(yahoo_url(root + account.replace("%7C", "|")), root + account)
        for bad in [
            "yodlee%7C..%2Fprivate",
            "yodlee%7Caccount%3Fother",
            "yodlee%257Caccount",
            "yodlee%7C",
        ]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                yahoo_url(root + bad)

    def test_discovery_deduplicates_and_ignores_external_links(self):
        found = discover_links(
            [
                {"href": "https://finance.yahoo.com/portfolio/p_1", "text": "Retirement"},
                {"href": "https://finance.yahoo.com/portfolio/p_1/view/v1", "text": "Retirement"},
                {"href": "https://evil.com/portfolio/p_2", "text": "Ignore"},
                {"href": "https://finance.yahoo.com/portfolios/", "text": "Overview"},
            ]
        )
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["name"], "Retirement")


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.source = self.store.add_source(
            "Retirement", "IRA", "https://finance.yahoo.com/portfolio/p_1", "USD"
        )

    def test_account_isolation(self):
        other = self.store.add_source("Brokerage", "Taxable")
        self.store.ingest(self.source, CSV)
        self.store.ingest(other, CSV)
        self.assertEqual(len(self.store.export()["portfolios"]), 2)
        self.assertNotEqual(
            self.store.sources()[0]["snapshot_id"], self.store.sources()[1]["snapshot_id"]
        )

    def test_deduplicate_only_latest_and_preserve_return_to_old_data(self):
        a = self.store.ingest(self.source, CSV)
        self.assertTrue(self.store.ingest(self.source, CSV)["unchanged"])
        self.store.ingest(self.source, CSV.replace(b",10,first", b",11,first"))
        c = self.store.ingest(self.source, CSV)
        self.assertEqual(len(self.store.snapshots(self.source)), 3)
        self.assertEqual(self.store.snapshot(a["snapshot_id"], raw=True), CSV)
        self.assertFalse(c["unchanged"])
        self.assertNotEqual(a["snapshot_id"], c["snapshot_id"])

    def test_bad_import_leaves_snapshot_untouched(self):
        result = self.store.ingest(self.source, CSV)
        with self.assertRaises(ValueError):
            self.store.ingest(self.source, b"Symbol,Quantity\nAAPL,10\nMSFT,invalid\n")
        self.assertEqual(len(self.store.snapshots(self.source)), 1)
        self.assertEqual(self.store.snapshot(result["snapshot_id"], raw=True), CSV)

    def test_failure_keeps_previous_success_timestamp_and_recovers(self):
        self.store.ingest(self.source, CSV)
        checked = self.store.source(self.source)["last_checked"]
        self.store.failed(self.source, "Sign in required.")
        self.assertEqual(self.store.source(self.source)["last_checked"], checked)
        self.store.ingest(self.source, CSV)
        self.assertIsNone(self.store.source(self.source)["last_error"])

    def test_currency_label_never_becomes_inferred_row_currency(self):
        self.store.ingest(self.source, CSV)
        exported = self.store.export()
        self.assertEqual(exported["portfolios"][0]["source"]["currency"], "USD")
        self.assertIsNone(exported["portfolios"][0]["snapshot"]["rows"][0]["currency"])

    def test_no_duplicate_yahoo_sources(self):
        with self.assertRaises(ValueError):
            self.store.add_source(
                "Duplicate", url="https://finance.yahoo.com/portfolio/p_1/view/v1"
            )

    def test_unknown_source_does_not_create_orphan_snapshot(self):
        with self.assertRaises(ValueError):
            self.store.ingest(999, CSV)

    def test_sensitive_file_permissions(self):
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)

    def test_fixture_is_importable(self):
        rows, warnings = parse_csv(
            (Path(__file__).resolve().parent.parent / "examples/yahoo-demo.csv").read_bytes()
        )
        self.assertEqual(len(rows), 4)
        self.assertIsNone(rows[-1]["quantity"])


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.browser = MagicMock()
        self.browser.status.return_value = {"busy": False, "message": "Test"}
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), lambda *args: None)
        self.port = self.server.server_port
        self.server.RequestHandlerClass = make_handler(self.store, self.browser, self.port)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.cleanup)
        self.token = self.request("GET", "/api/state")[1]["token"]

    def cleanup(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def request(self, method, path, body=None, headers=None):
        connection = HTTPConnection("127.0.0.1", self.port, timeout=3)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        data = response.read()
        result = (
            json.loads(data) if response.getheader("Content-Type") == "application/json" else data
        )
        status = response.status
        connection.close()
        return status, result

    def post(self, path, data):
        return self.request(
            "POST",
            path,
            json.dumps(data),
            {"Content-Type": "application/json", "X-Local-Token": self.token},
        )

    def test_end_to_end_add_import_snapshot_and_export(self):
        status, source = self.post("/api/sources", {"name": "Test IRA", "account": "IRA"})
        self.assertEqual(status, 201)
        status, imported = self.request(
            "POST",
            f"/api/sources/{source['id']}/import",
            CSV,
            {"X-Local-Token": self.token, "Content-Type": "text/csv"},
        )
        self.assertEqual(status, 200)
        status, snapshot = self.request("GET", f"/api/snapshots/{imported['snapshot_id']}")
        self.assertEqual(len(snapshot["rows"]), 2)
        self.assertEqual(
            self.request("GET", f"/api/snapshots/{imported['snapshot_id']}/csv")[1], CSV
        )
        exported = self.request("GET", "/api/export")[1]
        self.assertEqual(exported["portfolios"][0]["source"]["account"], "IRA")

    def test_rejects_cross_site_and_untrusted_host(self):
        self.assertEqual(
            self.request("GET", "/api/state", headers={"Host": "evil.example"})[0], 403
        )
        self.assertEqual(
            self.request("GET", "/api/export", headers={"Origin": "https://evil.example"})[0], 403
        )
        self.assertEqual(
            self.request("GET", "/api/state", headers={"Sec-Fetch-Site": "cross-site"})[0], 403
        )

    def test_a_link_from_another_site_may_open_the_page(self):
        """A link elsewhere may send the browser here; the page it gets is not readable there."""
        navigation = {
            "Sec-Fetch-Site": "cross-site",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Dest": "document",
        }
        self.assertEqual(self.request("GET", "/", headers=navigation)[0], 200)
        # The data behind the page keeps the strict rule, however the request is dressed.
        self.assertEqual(self.request("GET", "/api/state", headers=navigation)[0], 403)
        self.assertEqual(self.request("GET", "/api/export", headers=navigation)[0], 403)
        # Only a document navigation is one: a page loading this app any other way --
        # into a frame, as a script, as a stylesheet, or by fetch -- stays refused.
        for dest in ("empty", "iframe", "frame", "script", "style", "image"):
            self.assertEqual(
                self.request(
                    "GET",
                    "/",
                    headers={"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Dest": dest},
                )[0],
                403,
                f"Sec-Fetch-Dest: {dest} is not a navigation",
            )
        # A cross-site POST is refused for being a POST, not merely for lacking the token:
        # it stays refused even when it carries the token a loaded page would hold.
        token = self.request("GET", "/api/state")[1]["token"]
        self.assertEqual(
            self.request("POST", "/api/sources", "{}", {**navigation, "X-Local-Token": token})[0],
            403,
        )
        self.assertEqual(self.request("POST", "/api/sources", "{}", navigation)[0], 403)

    def test_mutations_require_token(self):
        self.assertEqual(
            self.request("POST", "/api/sources", "{}", {"Content-Type": "application/json"})[0], 403
        )

    def test_malformed_request_and_path_traversal(self):
        self.assertEqual(self.post("/api/sources", {"name": 123})[0], 400)
        self.assertEqual(
            self.post("/api/browser", {"action": "refresh", "source_ids": "1"})[0], 400
        )
        self.assertEqual(self.request("GET", "/../../portfolio/storage.py")[0], 404)

    def test_browser_action_is_queued(self):
        self.assertEqual(
            self.post("/api/browser", {"action": "refresh", "source_ids": [1]})[0], 202
        )
        self.browser.submit.assert_called_once_with("refresh", [1])


if __name__ == "__main__":
    unittest.main()
