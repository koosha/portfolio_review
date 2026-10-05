import hashlib
import json
import tempfile
import threading
import unittest
from copy import deepcopy
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from unittest.mock import MagicMock, patch

import pandas as pd

from portfolio.server import make_handler
from portfolio.storage import Store
from portfolio_research.account_labels import account_catalog, validate_alias, with_account_labels
from portfolio_research.adapter import account_id, load_collector
from portfolio_research.public import public_result
from portfolio_research.service import ResearchService, default_config
from tests.test_current import SUNDAY_RECEIPT, cached_providers, table


class AccountLabelTests(unittest.TestCase):
    def test_names_survive_saved_numerical_views_without_changing_identity_or_values(self):
        identifier = account_id(7)
        result = {
            "summary": {"accounts": [{"account_id": identifier, "cash": 12.5}]},
            "holdings": [{"account_id": identifier, "security_id": "GOOG", "market_value": 250}],
            "allocation": {"proposals": [{"account_id": identifier, "amount": 20}]},
            "issues": [{"message": f"{identifier}: cash is unknown."}],
        }
        bundle = {
            "ledger": {
                "accounts": [{"account_id": identifier, "source_id": 7, "name": "My investments"}]
            }
        }
        original_result, original_bundle = deepcopy(result), deepcopy(bundle)
        presented = public_result(result, bundle)
        self.assertEqual(presented["account_labels"], {identifier: "My investments"})
        self.assertEqual(presented["holdings"][0]["account_name"], "My investments")
        self.assertEqual(presented["allocation"]["proposals"][0]["account_name"], "My investments")
        self.assertEqual(presented["holdings"][0]["market_value"], 250)
        self.assertEqual(presented["summary"]["accounts"][0]["cash"], 12.5)
        self.assertEqual(presented["issues"][0]["message"], "My investments: cash is unknown.")
        self.assertEqual(result, original_result)
        self.assertEqual(bundle, original_bundle)

    def test_source_name_wins_and_opaque_names_have_stable_numbered_fallbacks(self):
        result = {
            "accounts": [{"account_id": account_id(5), "source_id": 5, "name": account_id(5)}]
        }
        row = account_catalog(result)[0]
        self.assertEqual(row["display_name"], "Account 5")
        added = {
            "accounts": [
                {"account_id": account_id(2), "source_id": 2, "name": None},
                *result["accounts"],
            ]
        }
        self.assertEqual(account_catalog(added)[1]["display_name"], "Account 5")
        demo = {"accounts": pd.DataFrame([{"account_id": "opaque-a"}, {"account_id": "opaque-b"}])}
        self.assertEqual(
            [row["name"] for row in account_catalog(bundle=demo)], ["Account 1", "Account 2"]
        )

    def test_duplicate_source_names_remain_distinguishable(self):
        accounts = [
            {"account_id": account_id(number), "source_id": number, "name": "Investments"}
            for number in (1, 2)
        ]
        names = [row["display_name"] for row in account_catalog({"accounts": accounts})]
        self.assertEqual(names, ["Investments · Account 1", "Investments · Account 2"])

    def test_alias_overrides_and_clear_restores_original_name(self):
        identifier = account_id(1)
        result = {
            "accounts": [{"account_id": identifier, "source_id": 1, "name": "Original Yahoo name"}]
        }
        aliased = with_account_labels(result, aliases={identifier: "Long-term portfolio"})
        self.assertEqual(aliased["accounts"][0]["source_name"], "Original Yahoo name")
        self.assertEqual(
            public_result(aliased)["account_labels"][identifier], "Long-term portfolio"
        )
        self.assertEqual(
            with_account_labels(aliased, aliases={})["account_labels"][identifier],
            "Original Yahoo name",
        )

    def test_bad_aliases_do_not_publish_private_strings_or_identifiers(self):
        for value in (
            "",
            "x" * 201,
            "hello\nthere",
            "token=secret",
            "/Users/owner/private/file",
            account_id(1),
            7,
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_alias(value)
        self.assertEqual(validate_alias("  Research portfolio  "), "Research portfolio")
        self.assertIsNone(validate_alias(None))
        self.assertEqual(
            account_catalog({"accounts": [{"account_id": "x", "name": "token=hidden"}]})[0]["name"],
            "Account 1",
        )


class AccountNameServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.source = Store(self.temp.name)
        self.source_id = self.source.add_source(
            "Actual Yahoo portfolio", url="https://finance.yahoo.com/portfolio/p_labels"
        )
        with patch("portfolio.storage.now", return_value=SUNDAY_RECEIPT):
            batch = self.source.begin_batch([self.source_id])
            self.source.ingest_table(self.source_id, table(), batch_id=batch)
        self.identifier = account_id(self.source_id)
        self.service = ResearchService(default_config(self.temp.name))
        self.addCleanup(self.service.close)

    def test_alias_applies_to_current_old_saved_run_and_history_without_mutating_archives(self):
        bundle = load_collector(self.source.path, "2026-09-13")
        result = {
            "metadata": {"as_of": "2026-09-13"},
            "holdings": [
                {"account_id": self.identifier, "security_id": "DEMO", "market_value": 10}
            ],
            "summary": {"accounts": [{"account_id": self.identifier}]},
        }
        run_id = self.service.store.save_run(result, self.service.config, bundle)
        record_id = self.service.store.append_record(
            "decision",
            {"account_id": self.identifier, "security_id": "DEMO", "action": "Hold"},
            run_id=run_id,
        )
        before_run, before_bundle = (
            self.service.store.load_run(run_id),
            self.service.store.load_bundle(run_id),
        )
        source_hash = hashlib.sha256(self.source.path.read_bytes()).hexdigest()
        response = self.service.save_account_name(
            {"account_id": self.identifier, "display_name": "Core portfolio"}
        )
        self.assertEqual(response["account_labels"][self.identifier], "Core portfolio")
        with cached_providers():
            current = self.service.current()["current"]
        self.assertEqual(current["accounts"][0]["display_name"], "Core portfolio")
        self.assertEqual(current["positions"][0]["account_name"], "Core portfolio")
        self.assertEqual(
            self.service.run(run_id)["result"]["holdings"][0]["account_name"], "Core portfolio"
        )
        history = self.service.records("decision", run_id)
        self.assertEqual(history[0]["record_id"], record_id)
        self.assertEqual(history[0]["payload"]["account_name"], "Core portfolio")
        self.assertEqual(self.service.store.load_run(run_id), before_run)
        self.assertEqual(self.service.store.load_bundle(run_id)["ledger"], before_bundle["ledger"])
        self.assertEqual(hashlib.sha256(self.source.path.read_bytes()).hexdigest(), source_hash)
        response = self.service.save_account_name(
            {"account_id": self.identifier, "display_name": None}
        )
        self.assertEqual(response["account_labels"][self.identifier], "Actual Yahoo portfolio")

    def test_alias_persists_restart_and_new_source_snapshot(self):
        self.service.save_account_name(
            {"account_id": self.identifier, "display_name": "Custom name"}
        )
        with patch("portfolio.storage.now", return_value="2026-09-14T21:00:00+00:00"):
            batch = self.source.begin_batch([self.source_id])
            self.source.ingest_table(self.source_id, table(value="15"), batch_id=batch)
        restarted = ResearchService(self.service.config)
        self.addCleanup(restarted.close)
        self.assertEqual(restarted.accounts()["account_labels"][self.identifier], "Custom name")

    def test_unknown_identity_cannot_create_alias_and_invalid_payload_cannot_write(self):
        for payload in (
            {"account_id": "unknown", "display_name": "Mine"},
            {"account_id": self.identifier, "display_name": "Mine", "cash": 0},
            {"account_id": self.identifier, "display_name": "api_key=private"},
        ):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                self.service.save_account_name(payload)
        self.assertEqual(self.service.store.records("account_aliases"), [])


class AccountNameRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.research = MagicMock()
        self.research.accounts.return_value = {"accounts": [], "account_labels": {}}
        self.research.save_account_name.return_value = {"record_id": "name-1", "accounts": []}
        browser = MagicMock()
        browser.status.return_value = {"busy": False}
        server = ThreadingHTTPServer(("127.0.0.1", 0), lambda *args: None)
        server.RequestHandlerClass = make_handler(
            Store(self.temp.name), browser, server.server_port, self.research
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.port = server.server_port
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.token = self.request("GET", "/api/state")[1]["token"]

    def request(self, method, path, body=None, authenticated=False):
        headers = {"Content-Type": "application/json"}
        if authenticated:
            headers["X-Local-Token"] = self.token
        connection = HTTPConnection("127.0.0.1", self.port, timeout=3)
        connection.request(method, path, body=json.dumps(body) if body else None, headers=headers)
        response = connection.getresponse()
        payload = json.loads(response.read())
        connection.close()
        return response.status, payload

    def test_account_name_routes_keep_existing_mutation_authentication(self):
        self.assertEqual(
            self.request("GET", "/api/research/accounts"),
            (200, {"accounts": [], "account_labels": {}}),
        )
        payload = {"account_id": "id", "display_name": "Core"}
        self.assertEqual(self.request("POST", "/api/research/accounts", payload)[0], 403)
        self.research.save_account_name.assert_not_called()
        self.assertEqual(self.request("POST", "/api/research/accounts", payload, True)[0], 200)
        self.research.save_account_name.assert_called_once_with(payload)
        self.research.save_account_name.side_effect = ValueError("Choose an existing account.")
        self.assertEqual(self.request("POST", "/api/research/accounts", payload, True)[0], 400)
