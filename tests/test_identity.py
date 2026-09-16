import ast
import copy
import pathlib
import unittest

from portfolio_research import identity
from portfolio_research.adapter import FRAME_COLUMNS
from portfolio_research.identity import exception_key, resolve_identities

SNAPSHOT = 10


def position(source_id=1, raw_symbol="RY.TO", quote_symbol=None, security_id=None):
    return {
        "account_id": f"account_{source_id}",
        "source_id": source_id,
        "snapshot_id": SNAPSHOT + source_id,
        "row_number": 1,
        "raw_symbol": raw_symbol,
        "quote_symbol": quote_symbol,
        "security_id": security_id or f"unresolved_{source_id}_{raw_symbol}",
        "quantity": "10",
        "price": "285.20",
        "market_value": "2852.00",
        "currency": None,
    }


def adapter_security(security_id, raw_symbol, **fields):
    row = {column: None for column in FRAME_COLUMNS["securities"]}
    row.update(
        security_id=security_id,
        ticker=raw_symbol,
        name=f"Captured {raw_symbol}",
        eligible=False,
        resolution_status="unresolved",
    )
    row.update(fields)
    return row


def ledger(*positions, securities=None):
    rows = securities or {}
    for row in positions:
        rows.setdefault(row["security_id"], adapter_security(row["security_id"], row["raw_symbol"]))
    return {
        "positions": list(positions),
        "securities": list(rows.values()),
        "security_aliases": [
            {
                "source_id": row["source_id"],
                "raw_symbol": row["raw_symbol"],
                "security_id": row["security_id"],
                "valid_from": None,
                "valid_to": None,
                "exchange": None,
                "share_class": None,
                "snapshot_id": row["snapshot_id"],
            }
            for row in positions
        ],
    }


def listing(symbol, quote_currency, exchange="TOR", instrument_type="equity", **fields):
    factor = 100 if quote_currency == "GBp" else 1
    record = {
        "symbol": symbol,
        "quote_currency": quote_currency,
        "quote_unit_factor": factor,
        "major_currency": "GBP" if quote_currency == "GBp" else quote_currency,
        "exchange": exchange,
        "instrument_type": instrument_type,
        "name": f"{symbol} listing",
        "last_quote_at": "2026-09-11T20:00:00+00:00",
        "last_price": "285.20",
        "market_cap": None,
        "shares_outstanding": None,
        "source_id": "yahoo_listing:fixture",
        "received_at": "2026-09-13T20:00:00+00:00",
        "provider": "yahoo",
        "stale": False,
    }
    record.update(fields)
    return record


def by_id(result):
    return {row["security_id"]: row for row in result["securities"]}


class ResolveIdentitiesTests(unittest.TestCase):
    def resolve(self, data, supplemental=None, listings=None, search=None, issuer_lookup=None):
        return resolve_identities(
            data,
            supplemental,
            listings={} if listings is None else listings,
            search=search,
            issuer_lookup=issuer_lookup,
        )

    def test_captured_quote_symbol_resolves_the_exact_listing(self):
        data = ledger(position(raw_symbol="RY", quote_symbol="RY.TO"))
        result = self.resolve(data, listings={"RY.TO": listing("RY.TO", "CAD")})
        security = by_id(result)["RY.TO"]
        self.assertEqual(security["resolution_status"], "resolved")
        self.assertEqual(security["ticker"], "RY.TO")
        self.assertEqual(security["issuer_id"], "listing:RY.TO")
        self.assertEqual(security["currency"], "CAD")
        self.assertEqual(security["quote_currency"], "CAD")
        self.assertEqual(security["quote_unit_factor"], 1)
        self.assertEqual(security["exchange"], "TOR")
        self.assertEqual(security["instrument_type"], "equity")
        self.assertEqual(security["name"], "RY.TO listing")
        self.assertEqual(security["last_quote_at"], "2026-09-11T20:00:00+00:00")
        self.assertIs(security["eligible"], False)
        self.assertIsNone(security["domicile"])
        self.assertIsNone(security["equity_type"])
        self.assertTrue(set(FRAME_COLUMNS["securities"]).issubset(security))
        [alias] = result["aliases"]
        self.assertEqual(alias["security_id"], "RY.TO")
        self.assertEqual(alias["raw_symbol"], "RY")
        self.assertEqual(alias["quote_symbol"], "RY.TO")
        self.assertEqual(alias["resolution_status"], "resolved")
        self.assertEqual(alias["snapshot_id"], SNAPSHOT + 1)
        self.assertEqual(result["exceptions"], [])
        self.assertEqual(result["counts"]["resolved"], 1)

    def test_display_symbol_without_capture_metadata_resolves_from_display(self):
        data = ledger(position(raw_symbol="AAPL"))
        result = self.resolve(data, listings={"AAPL": listing("AAPL", "USD", exchange="NMS")})
        security = by_id(result)["AAPL"]
        self.assertEqual(security["resolution_status"], "resolved_from_display")
        self.assertEqual(result["aliases"][0]["resolution_status"], "resolved_from_display")
        self.assertEqual(security["currency"], "USD")
        self.assertEqual(result["counts"]["resolved_from_display"], 1)

    def test_supplemental_mapping_wins_over_provider_metadata(self):
        mapped = adapter_security(
            "security-aapl",
            "AAPL",
            issuer_id="issuer-apple",
            currency="USD",
            instrument_type="equity",
            eligible=True,
            resolution_status="resolved",
        )
        row = position(raw_symbol="AAPL", security_id="security-aapl")
        data = ledger(row, securities={"security-aapl": mapped})
        supplemental = {
            "version": 1,
            "accounts": [],
            "securities": [
                {
                    "source_id": 1,
                    "raw_symbol": "AAPL",
                    "security_id": "security-aapl",
                    "issuer_id": "issuer-apple",
                    "valid_from": "2020-01-01",
                }
            ],
            "tax_lots": [],
        }
        provider = listing("AAPL", "CAD", exchange="NEO", instrument_type="etf")
        result = self.resolve(
            data,
            supplemental,
            listings={"AAPL": provider},
            issuer_lookup=lambda meta: "cik:0000320193",
        )
        self.assertNotIn("AAPL", by_id(result))
        security = by_id(result)["security-aapl"]
        self.assertEqual(security["resolution_status"], "mapped")
        self.assertEqual(security["issuer_id"], "issuer-apple")
        self.assertEqual(security["currency"], "USD")
        self.assertEqual(security["instrument_type"], "equity")
        self.assertIs(security["eligible"], True)
        [alias] = result["aliases"]
        self.assertEqual(alias["security_id"], "security-aapl")
        self.assertEqual(alias["resolution_status"], "mapped")
        self.assertEqual(alias["valid_from"], "2020-01-01")
        self.assertEqual(result["exceptions"], [])
        self.assertEqual(result["counts"]["mapped"], 1)

    def test_ambiguous_search_returns_candidates_as_an_exception(self):
        row = position(raw_symbol="SHOPX")
        candidates = [
            {
                "symbol": "SHOPX.TO",
                "name": "Shop Canada",
                "exchange": "TOR",
                "instrument_type": "equity",
            },
            {
                "symbol": "SHOPX.L",
                "name": "Shop London",
                "exchange": "LSE",
                "instrument_type": "equity",
            },
        ]
        searched = []

        def search(text):
            searched.append(text)
            return candidates

        result = self.resolve(ledger(row), search=search)
        self.assertEqual(searched, ["SHOPX"])
        [exception] = result["exceptions"]
        self.assertEqual(exception["code"], "AMBIGUOUS_LISTING")
        self.assertEqual(exception["severity"], "error")
        self.assertEqual(exception["scope"], "listing")
        self.assertEqual(exception["source_id"], 1)
        self.assertEqual(exception["snapshot_id"], SNAPSHOT + 1)
        self.assertEqual(exception["account_id"], "account_1")
        self.assertEqual(exception["raw_symbol"], "SHOPX")
        self.assertEqual([c["symbol"] for c in exception["candidates"]], ["SHOPX.TO", "SHOPX.L"])
        self.assertEqual(exception["resolution"]["kind"], "security_listing")
        self.assertIn("security_id", exception["resolution"]["fields"])
        self.assertEqual(
            exception["key"],
            exception_key("AMBIGUOUS_LISTING", 1, SNAPSHOT + 1, "SHOPX"),
        )
        self.assertEqual(len(exception["key"]), 40)
        security = by_id(result)[row["security_id"]]
        self.assertEqual(security["resolution_status"], "ambiguous")
        self.assertIsNone(security["quote_currency"])
        self.assertEqual(result["aliases"][0]["security_id"], row["security_id"])
        self.assertEqual(result["counts"]["ambiguous"], 1)

    def test_single_exact_search_match_is_looked_up_again(self):
        row = position(raw_symbol="shop")
        listings = {}

        def search(text):
            # A lazy provider may add metadata for the exact candidate it found.
            listings["SHOP"] = listing("SHOP", "USD", exchange="NYQ")
            return [
                {
                    "symbol": "SHOP",
                    "name": "Shopify",
                    "exchange": "NYQ",
                    "instrument_type": "equity",
                },
                {
                    "symbol": "SHOP.TO",
                    "name": "Shopify",
                    "exchange": "TOR",
                    "instrument_type": "equity",
                },
            ]

        result = self.resolve(ledger(row), listings=listings, search=search)
        security = by_id(result)["SHOP"]
        self.assertEqual(security["resolution_status"], "resolved_by_search")
        self.assertEqual(security["currency"], "USD")
        self.assertEqual(result["aliases"][0]["security_id"], "SHOP")
        self.assertEqual(result["exceptions"], [])

    def test_no_provider_data_is_unresolved(self):
        for search in (None, lambda text: []):
            with self.subTest(search=search):
                row = position(raw_symbol="MYSTERY")
                result = self.resolve(ledger(row), search=search)
                [exception] = result["exceptions"]
                self.assertEqual(exception["code"], "UNRESOLVED_LISTING")
                self.assertEqual(exception["candidates"], [])
                self.assertEqual(exception["resolution"]["kind"], "security_listing")
                security = by_id(result)[row["security_id"]]
                self.assertEqual(security["resolution_status"], "unresolved")
                self.assertEqual(result["counts"]["unresolved"], 1)

    def test_captured_quote_symbol_is_never_replaced_by_a_display_search(self):
        row = position(raw_symbol="RY", quote_symbol="RY.TO")
        result = self.resolve(
            ledger(row), search=lambda text: self.fail("searched a captured quote symbol")
        )
        self.assertEqual(result["exceptions"][0]["code"], "UNRESOLVED_LISTING")

    def test_issuer_lookup_supplies_a_cik_identifier(self):
        seen = []

        def lookup(meta):
            seen.append(meta["symbol"])
            return "cik:0000320193" if meta["symbol"] == "AAPL" else None

        data = ledger(position(raw_symbol="AAPL"), position(source_id=2, raw_symbol="RY.TO"))
        result = self.resolve(
            data,
            listings={
                "AAPL": listing("AAPL", "USD", exchange="NMS"),
                "RY.TO": listing("RY.TO", "CAD"),
            },
            issuer_lookup=lookup,
        )
        self.assertEqual(by_id(result)["AAPL"]["issuer_id"], "cik:0000320193")
        self.assertEqual(by_id(result)["RY.TO"]["issuer_id"], "listing:RY.TO")
        self.assertEqual(seen, ["AAPL", "RY.TO"])

    def test_gbp_listing_uses_major_currency_and_unit_factor(self):
        meta = {"symbol": "VOD.L", "quote_currency": "GBp", "exchange": "LSE"}
        result = self.resolve(
            ledger(position(raw_symbol="VOD.L", quote_symbol="VOD.L")),
            listings={"VOD.L": meta},
        )
        security = by_id(result)["VOD.L"]
        self.assertEqual(security["currency"], "GBP")
        self.assertEqual(security["quote_currency"], "GBp")
        self.assertEqual(security["quote_unit_factor"], 100)
        self.assertEqual(security["exchange"], "LSE")

    def test_instrument_types_are_mapped(self):
        cases = {
            "EQUITY": "equity",
            "ETF": "etf",
            "MUTUALFUND": "mutual_fund",
            "CURRENCY": "currency",
            "INDEX": "index",
            "WARRANT": "other",
            "etf": "etf",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                result = self.resolve(
                    ledger(position(raw_symbol="XYZ")),
                    listings={"XYZ": listing("XYZ", "USD", instrument_type=raw)},
                )
                self.assertEqual(by_id(result)["XYZ"]["instrument_type"], expected)

    def test_shared_listing_across_accounts_yields_one_security_and_separate_aliases(self):
        data = ledger(
            position(source_id=1, raw_symbol="RY.TO", quote_symbol="RY.TO"),
            position(source_id=2, raw_symbol="RY.TO"),
            position(source_id=2, raw_symbol="RY.TO"),
        )
        result = self.resolve(data, listings={"RY.TO": listing("RY.TO", "CAD")})
        self.assertEqual(len(result["securities"]), 1)
        self.assertEqual(by_id(result)["RY.TO"]["resolution_status"], "resolved")
        self.assertEqual(
            [(a["source_id"], a["resolution_status"]) for a in result["aliases"]],
            [(1, "resolved"), (2, "resolved_from_display")],
        )

    def test_inputs_are_not_mutated(self):
        data = ledger(position(raw_symbol="MYSTERY"), position(source_id=2, raw_symbol="AAPL"))
        listings = {"AAPL": listing("AAPL", "USD", exchange="NMS")}
        before = copy.deepcopy((data, listings))
        result = self.resolve(data, listings=listings)
        result["securities"][0]["name"] = "changed"
        self.assertEqual((data, listings), before)

    def test_invalid_inputs_raise_value_error(self):
        with self.assertRaises(ValueError):
            self.resolve({"positions": "nope"})
        with self.assertRaises(ValueError):
            resolve_identities(ledger(), None, listings=[], search=None, issuer_lookup=None)
        with self.assertRaises(ValueError):
            self.resolve(ledger(position(raw_symbol="AAPL")), listings={"AAPL": "USD"})

    def mapped_ledger(self, security_id, raw_symbol, currency, source_id=1):
        mapped = adapter_security(
            security_id,
            raw_symbol,
            issuer_id="issuer-" + security_id,
            currency=currency,
            resolution_status="resolved",
        )
        row = position(source_id=source_id, raw_symbol=raw_symbol, security_id=security_id)
        supplemental = {
            "securities": [
                {
                    "source_id": source_id,
                    "raw_symbol": raw_symbol,
                    "security_id": security_id,
                    "issuer_id": "issuer-" + security_id,
                    "valid_from": "2020-01-01",
                }
            ]
        }
        return row, mapped, supplemental

    def test_mapped_security_takes_quote_units_only_from_an_agreeing_listing(self):
        row, mapped, supplemental = self.mapped_ledger("VOD.L", "VOD.L", "GBP")
        data = ledger(row, securities={"VOD.L": mapped})
        meta = {"symbol": "VOD.L", "quote_currency": "GBp", "last_quote_at": "2026-09-11T15:30"}
        security = by_id(self.resolve(data, supplemental, listings={"VOD.L": meta}))["VOD.L"]
        self.assertEqual(security["resolution_status"], "mapped")
        self.assertEqual(security["resolution_basis"], "supplemental")
        self.assertEqual(security["currency"], "GBP")
        self.assertEqual(security["quote_currency"], "GBp")
        self.assertEqual(security["quote_unit_factor"], 100)
        disagreeing = {"VOD.L": listing("VOD.L", "USD", exchange="NYQ")}
        security = by_id(self.resolve(data, supplemental, listings=disagreeing))["VOD.L"]
        self.assertEqual(security["currency"], "GBP")
        self.assertIsNone(security["quote_currency"])
        self.assertIsNone(security["quote_unit_factor"])

    def test_owner_mapping_row_wins_over_a_provider_row_with_the_same_id(self):
        mapped_row, mapped, supplemental = self.mapped_ledger("AAPL", "AAPL", "USD", source_id=2)
        display_row = position(source_id=1, raw_symbol="AAPL")
        provider = {"AAPL": listing("AAPL", "USD", exchange="NMS", name="Provider name")}
        for rows in ((display_row, mapped_row), (mapped_row, display_row)):
            with self.subTest(order=[row["source_id"] for row in rows]):
                data = ledger(*rows, securities={"AAPL": mapped})
                result = self.resolve(data, supplemental, listings=provider)
                [security] = [row for row in result["securities"] if row["security_id"] == "AAPL"]
                self.assertEqual(security["resolution_basis"], "supplemental")
                self.assertEqual(security["issuer_id"], "issuer-AAPL")
                self.assertEqual(
                    sorted((a["source_id"], a["resolution_status"]) for a in result["aliases"]),
                    [(1, "resolved_from_display"), (2, "mapped")],
                )

    def test_exact_candidate_without_metadata_stays_unresolved_with_candidates(self):
        row = position(raw_symbol="NEWCO")
        candidate = {
            "symbol": "NEWCO",
            "name": "New Co",
            "exchange": "NMS",
            "instrument_type": "equity",
        }
        result = self.resolve(ledger(row), search=lambda text: [candidate])
        [exception] = result["exceptions"]
        self.assertEqual(exception["code"], "UNRESOLVED_LISTING")
        self.assertEqual(exception["candidates"], [candidate])
        self.assertIsNone(exception["proposed"])
        self.assertEqual(by_id(result)[row["security_id"]]["resolution_status"], "unresolved")

    def test_invalid_provider_answers_raise_value_error(self):
        data = ledger(position(raw_symbol="AAPL"))
        good = {"AAPL": listing("AAPL", "USD", exchange="NMS")}
        cases = [
            dict(search=lambda text: "AAPL"),
            dict(search=lambda text: [{"name": "no symbol"}]),
            dict(search="not callable"),
            dict(listings=good, issuer_lookup=lambda meta: 42),
            dict(listings={"AAPL": listing("AAPL", "US Dollar")}),
        ]
        for case in cases:
            with self.subTest(case=sorted(case)):
                with self.assertRaises(ValueError):
                    self.resolve(data, **case)

    def test_exception_key_is_stable_and_distinguishes_scope(self):
        first = exception_key("STALE_FX", 1, 11, None, "CADUSD")
        self.assertEqual(first, exception_key("STALE_FX", 1, 11, "", "CADUSD"))
        self.assertNotEqual(first, exception_key("STALE_FX", 1, 11, None, "GBPUSD"))


class IdentityModuleShapeTests(unittest.TestCase):
    """The resolution rules stay readable only while no single function owns all of them."""

    def test_every_function_stays_under_the_repo_line_limit(self):
        source = pathlib.Path(identity.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        oversized = {
            node.name: node.end_lineno - node.lineno + 1
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.end_lineno - node.lineno + 1 > 50
        }
        self.assertEqual(oversized, {})


if __name__ == "__main__":
    unittest.main()
