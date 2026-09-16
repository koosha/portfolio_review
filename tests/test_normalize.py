"""Gate: USD and CAD Yahoo portfolios reconcile in USD without double conversion."""

import copy
import tempfile
import unittest
from decimal import Decimal, localcontext
from unittest.mock import patch

import pandas as pd

from portfolio.storage import Store
from portfolio_lab import metrics
from portfolio_lab.pipeline import load_inputs
from portfolio_research.adapter import account_id, load_collector
from portfolio_research.fx import FxTable
from portfolio_research.identity import exception_key
from portfolio_research.normalize import normalize_bundle
from portfolio_research.service import default_config

SUNDAY_RECEIPT = "2026-09-13T20:00:00+00:00"
LATER_RECEIPT = "2026-09-13T20:30:00+00:00"
SUNDAY_GENERATED = "2026-09-13T21:00:00+00:00"
RECEIPT_DAY = "2026-09-13"
FX_DAY = "2026-09-11"
STALE_DAY = "2026-08-20"
CADUSD = Decimal("0.7211")


def listing(symbol, quote_currency, exchange, instrument_type="equity"):
    factor = 100 if quote_currency == "GBp" else 1
    return {
        "symbol": symbol,
        "quote_currency": quote_currency,
        "quote_unit_factor": factor,
        "major_currency": "GBP" if quote_currency == "GBp" else quote_currency,
        "exchange": exchange,
        "instrument_type": instrument_type,
        "name": f"{symbol} fixture listing",
        "last_quote_at": "2026-09-11T20:00:00+00:00",
        "last_price": None,
        "market_cap": None,
        "shares_outstanding": None,
        "source_id": f"yahoo_listing:{symbol}",
        "received_at": SUNDAY_RECEIPT,
        "provider": "yahoo",
        "stale": False,
    }


LISTINGS = {
    "RY.TO": listing("RY.TO", "CAD", "TOR"),
    "AAPL": listing("AAPL", "USD", "NMS"),
    "VOD.L": listing("VOD.L", "GBp", "LSE"),
}


def observation(base, quote, rate, day=FX_DAY, provider="bank_of_canada"):
    return {
        "pair": base + quote,
        "base": base,
        "quote": quote,
        "rate": rate,
        "date": day,
        "source_id": f"{provider}_fx:{base}{quote}:{day}",
        "provider": provider,
        "received_at": SUNDAY_RECEIPT,
    }


def fx_table(day=FX_DAY):
    return FxTable(
        [
            observation("CAD", "USD", "0.7211", day, "yahoo"),
            observation("GBP", "CAD", "1.8250", day),
            observation("USD", "CAD", "1.3868", day),
        ],
        max_age_days=7,
    )


def usd_table(rows):
    """Yahoo USD portfolio capture: no currency column, version 1."""
    rows = [*rows, ["Total Cash", "", "100", ""]]
    return {
        "method": "yahoo-holdings-table-v1",
        "headers": ["Symbol", "Shares", "Last Price", "Market Value ($)"],
        "rows": rows,
        "page_count": 1,
        "expected_count": len(rows),
        "completeness": "count-verified",
    }


def cad_table(rows):
    """Yahoo CAD portfolio capture with quote symbols, version 2."""
    rows = [[*row, row[0]] for row in rows] + [["Total Cash", "", "50", "", ""]]
    return {
        "method": "yahoo-holdings-table-v1",
        "headers": ["Symbol", "Shares", "Last Price", "Market Value", "Yahoo quote symbol"],
        "rows": rows,
        "page_count": 1,
        "expected_count": len(rows),
        "completeness": "count-verified",
        "capture_version": 2,
    }


A_ROWS = [["RY.TO", "10", "285.20", "2056.30"], ["AAPL", "2", "300", "600"]]
B_ROWS = [["RY.TO", "5", "285.20", "1426.00"], ["VOD.L", "100", "128.75", "235.00"]]


def issuer_lookup(meta):
    return "cik:0000320193" if meta.get("symbol") == "AAPL" else None


class NormalizeGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.a = self.store.add_source(
            "USD portfolio", url="https://finance.yahoo.com/portfolio/p_fixture_normalize_a"
        )
        self.b = self.store.add_source(
            "CAD portfolio", url="https://finance.yahoo.com/portfolio/p_fixture_normalize_b"
        )
        self.snapshots = self.publish(A_ROWS, B_ROWS)
        self.config = default_config(self.temp.name)

    def publish(self, a_rows, b_rows, timestamp=SUNDAY_RECEIPT):
        with patch("portfolio.storage.now", return_value=timestamp):
            batch = self.store.begin_batch([self.a, self.b])
            first = self.store.ingest_table(self.a, usd_table(a_rows), batch_id=batch)
            second = self.store.ingest_table(
                self.b, cad_table(b_rows), batch_id=batch, extension_version="1.2.0"
            )
        return {self.a: first["snapshot_id"], self.b: second["snapshot_id"]}

    def load(self, fx=None, supplemental=None):
        table = fx_table() if fx is None else fx

        def metadata(config, symbol, *, refresh, issues):
            self.assertFalse(refresh)
            found = LISTINGS.get(symbol)
            return copy.deepcopy(found) if found else None

        with (
            patch("portfolio_research.market_listings.listing_metadata", side_effect=metadata),
            patch("portfolio_research.fx_providers.load_fx_table", return_value=table) as loader,
            patch("portfolio_research.issuers.sec_issuer_lookup", return_value=issuer_lookup),
        ):
            bundle = load_inputs(
                self.config,
                review_kind="current",
                generated_at=SUNDAY_GENERATED,
                supplemental=supplemental,
            )
        self.loader = loader
        return bundle

    @staticmethod
    def positions(bundle):
        return {(row["source_id"], row["raw_symbol"]): row for row in bundle["ledger"]["positions"]}

    @staticmethod
    def accounts(bundle):
        return {row["source_id"]: row for row in bundle["ledger"]["accounts"]}

    @staticmethod
    def codes(records):
        return [record["code"] for record in records]

    def test_v2_capture_carries_quote_symbols_and_v1_does_not(self):
        bundle = load_collector(
            self.store.path,
            "2026-09-11",
            receipt_through=RECEIPT_DAY,
            receipt_before=SUNDAY_GENERATED,
        )
        quotes = {
            (row["source_id"], row["raw_symbol"]): row["quote_symbol"]
            for row in bundle["ledger"]["positions"]
        }
        self.assertEqual(quotes[(self.a, "RY.TO")], None)
        self.assertEqual(quotes[(self.a, "AAPL")], None)
        self.assertEqual(quotes[(self.b, "RY.TO")], "RY.TO")
        self.assertEqual(quotes[(self.b, "VOD.L")], "VOD.L")
        aliases = {
            (row["source_id"], row["raw_symbol"]): row["quote_symbol"]
            for row in bundle["ledger"]["security_aliases"]
        }
        self.assertEqual(aliases[(self.b, "VOD.L")], "VOD.L")
        self.assertIsNone(aliases[(self.a, "AAPL")])

    def test_fx_loading_requests_foreign_currencies_through_the_receipt_day(self):
        self.load()
        self.loader.assert_called_once()
        args, kwargs = self.loader.call_args
        currencies = kwargs.get("currencies", args[1] if len(args) > 1 else None)
        self.assertEqual(sorted(currencies), ["CAD", "GBP"])
        self.assertEqual(kwargs["end_date"], RECEIPT_DAY)
        self.assertLess(kwargs["start_date"], "2021-09-11")
        self.assertFalse(kwargs["refresh"])

    def test_labelled_usd_holdings_need_no_fx_and_are_not_converted(self):
        labelled = {
            "method": "yahoo-holdings-table-v1",
            "headers": ["Symbol", "Shares", "Last Price", "Market Value", "Currency"],
            "rows": [["AAPL", "2", "300", "600", "USD"], ["Total Cash", "", "5", "", "USD"]],
            "page_count": 1,
            "expected_count": 2,
            "completeness": "count-verified",
        }
        with patch("portfolio.storage.now", return_value=LATER_RECEIPT):
            batch = self.store.begin_batch([self.a, self.b])
            for source_id in (self.a, self.b):
                self.store.ingest_table(source_id, copy.deepcopy(labelled), batch_id=batch)
        bundle = self.load()
        self.loader.assert_not_called()
        for row in bundle["ledger"]["positions"]:
            self.assertEqual(row["value_currency_basis"], "row")
            self.assertEqual(row["market_value_usd"], "600")
            self.assertEqual(row["reported_market_value"], row["market_value"])
            self.assertIsNone(row["fx_rate"])
        self.assertEqual(bundle["fx_observations"], [])
        self.assertEqual(bundle["normalization"]["covered_value_usd"], "1210")

    def test_gate_positions_are_valued_in_usd_once(self):
        bundle = self.load()
        held = self.positions(bundle)
        a_ry, a_aapl = held[(self.a, "RY.TO")], held[(self.a, "AAPL")]
        b_ry, b_vod = held[(self.b, "RY.TO")], held[(self.b, "VOD.L")]

        self.assertEqual(a_ry["reported_currency"], "USD")
        self.assertEqual(a_ry["value_currency_basis"], "arithmetic_fx")
        self.assertEqual(a_ry["quote_currency"], "CAD")
        self.assertEqual(a_ry["quote_unit_factor"], 1)
        self.assertEqual(a_ry["market_value_usd"], "2056.30")
        for field in ("fx_rate", "fx_pair", "fx_observation_date", "fx_source_id"):
            self.assertIsNone(a_ry[field], field)

        self.assertEqual(a_aapl["reported_currency"], "USD")
        self.assertEqual(a_aapl["value_currency_basis"], "arithmetic_quote")
        self.assertEqual(a_aapl["market_value_usd"], "600")
        self.assertEqual(a_aapl["price_usd"], "300")
        self.assertIsNone(a_aapl["fx_rate"])

        self.assertEqual(b_ry["reported_currency"], "CAD")
        self.assertEqual(b_ry["value_currency_basis"], "arithmetic_quote")
        self.assertEqual(Decimal(b_ry["market_value_usd"]), Decimal("1426.00") * CADUSD)
        self.assertEqual(b_ry["market_value_usd"], "1028.288600")
        self.assertEqual(b_ry["fx_rate"], "0.7211")
        self.assertEqual(b_ry["fx_pair"], "CADUSD")
        self.assertEqual(b_ry["fx_observation_date"], FX_DAY)
        self.assertEqual(b_ry["fx_source_id"], "yahoo_fx:CADUSD:2026-09-11")

        self.assertEqual(b_vod["quote_currency"], "GBp")
        self.assertEqual(b_vod["quote_unit_factor"], 100)
        self.assertEqual(b_vod["price_major"], "1.2875")
        self.assertEqual(b_vod["reported_currency"], "CAD")
        self.assertEqual(b_vod["value_currency_basis"], "arithmetic_fx")
        self.assertEqual(b_vod["market_value_usd"], format(Decimal("235.00") * CADUSD, "f"))
        self.assertEqual(b_vod["fx_pair"], "CADUSD")

        for row in held.values():
            self.assertEqual(row["presentation_currency"], "USD")
            self.assertEqual(row["currency"], "USD")
            self.assertEqual(row["market_value"], row["market_value_usd"])
            self.assertEqual(row["valuation_date"], RECEIPT_DAY)
            self.assertEqual(row["valuation_basis"], "collection_receipt")
            if row["reported_currency"] == "USD":
                self.assertEqual(row["reported_market_value"], row["market_value"])
        self.assertEqual(b_ry["reported_market_value"], "1426.00")
        self.assertEqual(b_vod["reported_market_value"], "235.00")

    def test_gate_identity_accounts_totals_and_reconcile(self):
        bundle = self.load()
        held = self.positions(bundle)
        self.assertEqual(held[(self.a, "RY.TO")]["security_id"], "RY.TO")
        self.assertEqual(held[(self.b, "RY.TO")]["security_id"], "RY.TO")
        securities = {row["security_id"]: row for row in bundle["ledger"]["securities"]}
        self.assertEqual(set(securities), {"RY.TO", "AAPL", "VOD.L"})
        self.assertEqual(securities["AAPL"]["issuer_id"], "cik:0000320193")
        self.assertEqual(securities["RY.TO"]["issuer_id"], "listing:RY.TO")
        self.assertEqual(securities["VOD.L"]["currency"], "GBP")
        self.assertEqual(bundle["identity"]["resolved"], 2)
        self.assertEqual(bundle["identity"]["resolved_from_display"], 2)
        self.assertEqual(bundle["exceptions"], [])

        accounts = self.accounts(bundle)
        a, b = accounts[self.a], accounts[self.b]
        self.assertEqual(a["reported_currency"], "USD")
        self.assertEqual(a["currency_basis"], "inferred_from_positions")
        self.assertEqual(a["cash"], "100")
        self.assertEqual(b["reported_currency"], "CAD")
        self.assertEqual(b["currency_basis"], "inferred_from_positions")
        self.assertEqual(b["reported_cash"], "50")
        self.assertEqual(b["cash"], format(Decimal("50") * CADUSD, "f"))
        self.assertEqual(b["fx_pair"], "CADUSD")
        self.assertEqual(b["currency"], "USD")
        inferred = [i for i in bundle["issues"] if i["code"] == "INFERRED_ACCOUNT_CURRENCY"]
        self.assertTrue(inferred)
        self.assertTrue(all(issue["severity"] == "warning" for issue in inferred))
        self.assertEqual(set(bundle["accounts"]["currency"]), {"USD"})
        self.assertEqual(set(bundle["positions"]["currency"]), {"USD"})
        self.assertAlmostEqual(
            float(bundle["positions"]["market_value"].sum()), 3854.0471, places=6
        )
        for column in ("reported_market_value", "reported_currency", "fx_rate", "fx_pair"):
            self.assertIn(column, bundle["positions"].columns)

        expected = (
            Decimal("2056.30")
            + Decimal("600")
            + Decimal("100")
            + Decimal("1426.00") * CADUSD
            + Decimal("235.00") * CADUSD
            + Decimal("50") * CADUSD
        )
        normalization = bundle["normalization"]
        self.assertEqual(normalization["covered_value_usd"], format(expected, "f"))
        self.assertEqual(normalization["covered_value_usd"], "3990.102100")
        self.assertEqual(normalization["presentation_currency"], "USD")
        self.assertEqual(normalization["method_version"], "usd-presentation-1")
        self.assertEqual(normalization["unconverted_positions"], 0)
        self.assertEqual(normalization["unconverted_accounts"], 0)
        used = {(row["pair"], row["date"]) for row in bundle["fx_observations"]}
        self.assertEqual(used, {("CADUSD", FX_DAY), ("GBPCAD", FX_DAY)})

        reconciliation = metrics.reconcile(bundle, self.config)
        codes = self.codes(reconciliation["issues"])
        self.assertNotIn("currency_mismatch", codes)
        self.assertNotIn("position_arithmetic", codes)
        self.assertNotIn("invalid_valuation_date", codes)
        self.assertNotIn("unresolved_security", codes)

    def test_stale_fx_leaves_foreign_values_unconverted_with_one_exception_per_pair(self):
        bundle = self.load(fx=fx_table(STALE_DAY))
        stale = [row for row in bundle["exceptions"] if row["code"] == "STALE_FX"]
        # One per pair and date: B/RY.TO and A/RY.TO share CADUSD; VOD.L needs GBPCAD.
        self.assertEqual(sorted(row["pair"] for row in stale), ["CADUSD", "GBPCAD"])
        for record in stale:
            self.assertEqual(record["scope"], "fx")
            self.assertEqual(record["requested_date"], RECEIPT_DAY)
            self.assertEqual(record["observation_date"], STALE_DAY)
            self.assertEqual(record["resolution"]["kind"], "fx_manual")
            self.assertIsNone(record["source_id"])
        # A stale rate never establishes a value currency, and nothing asks for attestation.
        self.assertNotIn("UNKNOWN_VALUE_CURRENCY", self.codes(bundle["exceptions"]))
        self.assertIsNone(self.positions(bundle)[(self.a, "RY.TO")]["reported_currency"])
        self.assertEqual(bundle["normalization"]["unconverted_positions"], 3)
        held = self.positions(bundle)
        self.assertEqual(held[(self.a, "AAPL")]["market_value_usd"], "600")
        self.assertIsNone(held[(self.b, "RY.TO")]["market_value_usd"])
        self.assertIsNone(held[(self.b, "RY.TO")]["market_value"])
        self.assertEqual(held[(self.b, "RY.TO")]["reported_market_value"], "1426.00")
        self.assertEqual(held[(self.b, "RY.TO")]["currency"], "CAD")
        frame = bundle["positions"].set_index("raw_symbol", drop=False)
        self.assertTrue(pd.isna(frame[frame.source_id == self.b].loc["RY.TO", "market_value"]))
        self.assertIn(
            "currency_mismatch", self.codes(metrics.reconcile(bundle, self.config)["issues"])
        )

    def test_missing_fx_yields_missing_fx_exception(self):
        bundle = self.load(fx=FxTable())
        missing = [row for row in bundle["exceptions"] if row["code"] == "MISSING_FX"]
        # CADUSD for the CAD-valued holdings; GBPUSD for VOD.L, whose value currency no
        # observation could establish. Neither asks the owner to attest a currency.
        self.assertEqual([row["pair"] for row in missing], ["CADUSD", "GBPUSD"])
        self.assertNotIn("UNKNOWN_VALUE_CURRENCY", self.codes(bundle["exceptions"]))
        self.assertEqual(missing[0]["key"], missing[0]["key"].lower())
        self.assertEqual(len(missing[0]["key"]), 40)
        self.assertEqual(bundle["normalization"]["unconverted_positions"], 3)
        self.assertEqual(self.positions(bundle)[(self.a, "AAPL")]["market_value_usd"], "600")

    def test_attested_position_currency_overrides_arithmetic(self):
        supplemental = {
            "version": 1,
            "accounts": [
                {
                    "source_id": self.a,
                    "snapshot_id": self.snapshots[self.a],
                    "position_currency": "CAD",
                }
            ],
            "securities": [],
            "tax_lots": [],
        }
        bundle = self.load(supplemental=supplemental)
        a_ry = self.positions(bundle)[(self.a, "RY.TO")]
        self.assertEqual(a_ry["reported_currency"], "CAD")
        self.assertEqual(a_ry["value_currency_basis"], "attested")
        self.assertEqual(a_ry["market_value_usd"], format(Decimal("2056.30") * CADUSD, "f"))
        self.assertEqual(a_ry["fx_pair"], "CADUSD")
        a_aapl = self.positions(bundle)[(self.a, "AAPL")]
        self.assertEqual(a_aapl["value_currency_basis"], "attested")
        self.assertEqual(a_aapl["price_usd"], "300")
        codes = self.codes(metrics.reconcile(bundle, self.config)["issues"])
        self.assertIn("position_arithmetic", codes)

    def test_ratio_matching_nothing_is_an_unknown_value_currency_exception(self):
        snapshots = self.publish(
            A_ROWS,
            [B_ROWS[0], ["VOD.L", "100", "128.75", "500.00"]],
            timestamp=LATER_RECEIPT,
        )
        bundle = self.load()
        unknown = [row for row in bundle["exceptions"] if row["code"] == "UNKNOWN_VALUE_CURRENCY"]
        self.assertEqual(len(unknown), 1)
        record = unknown[0]
        self.assertEqual(record["scope"], "position")
        self.assertEqual(record["severity"], "error")
        self.assertEqual(record["source_id"], self.b)
        self.assertEqual(record["snapshot_id"], snapshots[self.b])
        self.assertEqual(record["account_id"], account_id(self.b))
        self.assertEqual(record["raw_symbol"], "VOD.L")
        self.assertEqual(record["quote_currency"], "GBp")
        self.assertEqual(Decimal(record["ratio"]).quantize(Decimal("0.0001")), Decimal("3.8835"))
        self.assertEqual(record["resolution"]["kind"], "account_currency")
        self.assertIn("position_currency", record["resolution"]["fields"])
        self.assertEqual(record["proposed"]["source_id"], self.b)
        self.assertEqual(record["proposed"]["snapshot_id"], snapshots[self.b])
        self.assertIn("position_currency", record["proposed"])
        self.assertEqual(
            record["key"],
            exception_key("UNKNOWN_VALUE_CURRENCY", self.b, snapshots[self.b], "VOD.L"),
        )
        vod = self.positions(bundle)[(self.b, "VOD.L")]
        self.assertIsNone(vod["reported_currency"])
        self.assertIsNone(vod["market_value_usd"])
        self.assertEqual(bundle["normalization"]["unconverted_positions"], 1)
        self.assertEqual(bundle["normalization"]["unconverted_accounts"], 1)

    def test_supplemental_security_mapping_stays_authoritative(self):
        supplemental = {
            "version": 1,
            "accounts": [],
            "securities": [
                {
                    "source_id": self.a,
                    "raw_symbol": "RY.TO",
                    "security_id": "security-ry",
                    "issuer_id": "issuer-ry",
                    "ticker": "RY.TO",
                    "currency": "CAD",
                    "instrument_type": "equity",
                    "valid_from": "2020-01-01",
                }
            ],
            "tax_lots": [],
        }
        bundle = self.load(supplemental=supplemental)
        held = self.positions(bundle)
        self.assertEqual(held[(self.a, "RY.TO")]["security_id"], "security-ry")
        self.assertEqual(held[(self.b, "RY.TO")]["security_id"], "RY.TO")
        self.assertEqual(held[(self.a, "RY.TO")]["value_currency_basis"], "arithmetic_fx")
        self.assertEqual(bundle["identity"]["mapped"], 1)

    def test_display_symbol_with_several_listings_is_an_ambiguous_exception(self):
        self.publish([*A_ROWS, ["SHOP", "1", "100", "100"]], B_ROWS, timestamp=LATER_RECEIPT)
        candidates = [
            {
                "symbol": "SHOP.TO",
                "name": "Shopify (TSX)",
                "exchange": "TOR",
                "instrument_type": "equity",
            },
            {
                "symbol": "SHOP.NE",
                "name": "Shopify (NEO)",
                "exchange": "NEO",
                "instrument_type": "equity",
            },
        ]
        with patch(
            "portfolio_research.market_listings.search_listings", return_value=candidates
        ) as searched:
            bundle = self.load()
        searched.assert_called_once()
        self.assertEqual(searched.call_args.args[1], "SHOP")
        self.assertFalse(searched.call_args.kwargs["refresh"])
        ambiguous = [row for row in bundle["exceptions"] if row["code"] == "AMBIGUOUS_LISTING"]
        self.assertEqual(len(ambiguous), 1)
        self.assertEqual(ambiguous[0]["raw_symbol"], "SHOP")
        self.assertEqual(
            [row["symbol"] for row in ambiguous[0]["candidates"]], ["SHOP.TO", "SHOP.NE"]
        )
        self.assertEqual(bundle["identity"]["ambiguous"], 1)
        shop = self.positions(bundle)[(self.a, "SHOP")]
        self.assertTrue(shop["security_id"].startswith("unresolved_"))
        self.assertIsNone(shop["market_value_usd"])

    def test_listing_provider_none_leaves_every_listing_unresolved(self):
        self.config["data"]["listing_provider"] = "none"
        with patch("portfolio_research.market_listings.search_listings") as searched:
            bundle = self.load()
        searched.assert_not_called()
        self.loader.assert_not_called()
        self.assertEqual(bundle["identity"]["unresolved"], 4)
        codes = self.codes(bundle["exceptions"])
        self.assertEqual(codes.count("UNRESOLVED_LISTING"), 4)
        self.assertEqual(bundle["normalization"]["unconverted_positions"], 4)
        self.assertEqual(bundle["normalization"]["covered_value_usd"], "0")

    def test_historical_normalization_keeps_valuation_unknown_and_input_unchanged(self):
        original = load_collector(self.store.path, "2026-09-14")
        before = copy.deepcopy(original["ledger"])
        bundle = normalize_bundle(
            original,
            self.config,
            fx=fx_table(),
            review_kind="historical",
            listings=copy.deepcopy(LISTINGS),
            search=None,
            issuer_lookup=None,
        )
        self.assertEqual(original["ledger"], before)
        held = self.positions(bundle)
        self.assertIsNone(held[(self.b, "RY.TO")]["valuation_date"])
        self.assertIsNone(held[(self.b, "RY.TO")]["valuation_basis"])
        self.assertEqual(held[(self.b, "RY.TO")]["market_value_usd"], "1028.288600")
        codes = self.codes(metrics.reconcile(bundle, self.config)["issues"])
        self.assertIn("invalid_valuation_date", codes)


CHFUSD = Decimal("1.004")
NEAR_PARITY_LISTINGS = {**LISTINGS, "NESN.SW": listing("NESN.SW", "CHF", "EBS")}


def account_facts(source_id, snapshot_id, **facts):
    return {
        "version": 1,
        "accounts": [{"source_id": source_id, "snapshot_id": snapshot_id, **facts}],
        "securities": [],
        "tax_lots": [],
    }


class NormalizationReviewTests(unittest.TestCase):
    """Regressions from the increment 2 review: prices, cash, FX gaps and parity."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.a = self.store.add_source(
            "USD portfolio", url="https://finance.yahoo.com/portfolio/p_fixture_review_a"
        )
        self.b = self.store.add_source(
            "CAD portfolio", url="https://finance.yahoo.com/portfolio/p_fixture_review_b"
        )
        self.config = default_config(self.temp.name)
        self.snapshots = self.publish(A_ROWS, B_ROWS)

    def publish(self, a_rows, b_rows, timestamp=SUNDAY_RECEIPT):
        with patch("portfolio.storage.now", return_value=timestamp):
            batch = self.store.begin_batch([self.a, self.b])
            first = self.store.ingest_table(self.a, usd_table(a_rows), batch_id=batch)
            second = self.store.ingest_table(
                self.b, cad_table(b_rows), batch_id=batch, extension_version="1.2.0"
            )
        return {self.a: first["snapshot_id"], self.b: second["snapshot_id"]}

    def load(self, fx=None, supplemental=None, listings=None):
        table = fx_table() if fx is None else fx
        known = LISTINGS if listings is None else listings

        def metadata(config, symbol, *, refresh, issues):
            found = known.get(symbol)
            return copy.deepcopy(found) if found else None

        with (
            patch("portfolio_research.market_listings.listing_metadata", side_effect=metadata),
            patch("portfolio_research.fx_providers.load_fx_table", return_value=table) as loader,
            patch("portfolio_research.issuers.sec_issuer_lookup", return_value=issuer_lookup),
        ):
            bundle = load_inputs(
                self.config,
                review_kind="current",
                generated_at=SUNDAY_GENERATED,
                supplemental=supplemental,
            )
        self.loader = loader
        return bundle

    @staticmethod
    def positions(bundle):
        return {(row["source_id"], row["raw_symbol"]): row for row in bundle["ledger"]["positions"]}

    @staticmethod
    def accounts(bundle):
        return {row["source_id"]: row for row in bundle["ledger"]["accounts"]}

    @staticmethod
    def codes(records):
        return [record["code"] for record in records]

    def test_an_arithmetic_fx_price_reconciles_with_the_value_it_explained(self):
        """A 0.3% gap between the source's own rate and the dated one is not an error."""
        self.publish([["RY.TO", "10", "285.20", "2062.83"]], B_ROWS, timestamp=LATER_RECEIPT)
        bundle = self.load()
        row = self.positions(bundle)[(self.a, "RY.TO")]
        self.assertEqual(row["value_currency_basis"], "arithmetic_fx")
        self.assertEqual(row["reported_currency"], "USD")
        self.assertEqual(row["market_value_usd"], "2062.83")
        self.assertEqual(Decimal(row["price_usd"]) * 10, Decimal("2062.83"))
        self.assertEqual(row["value_currency_fx_rate"], "0.7211")
        self.assertEqual(row["value_currency_fx_date"], FX_DAY)
        with localcontext() as context:
            context.prec = 34
            implied = Decimal("2062.83") / (Decimal("10") * Decimal("285.20"))
        self.assertEqual(Decimal(row["value_currency_implied_rate"]), implied)
        codes = self.codes(metrics.reconcile(bundle, self.config)["issues"])
        self.assertNotIn("position_arithmetic", codes)

    def test_a_converted_arithmetic_price_reconciles_with_the_converted_value(self):
        bundle = self.load()
        for key in ((self.b, "RY.TO"), (self.b, "VOD.L")):
            row = self.positions(bundle)[key]
            quantity = Decimal(row["quantity"])
            self.assertEqual(
                (Decimal(row["price_usd"]) * quantity).quantize(Decimal("0.000001")),
                Decimal(row["market_value_usd"]).quantize(Decimal("0.000001")),
                key,
            )
        codes = self.codes(metrics.reconcile(bundle, self.config)["issues"])
        self.assertNotIn("position_arithmetic", codes)

    def test_captured_cash_keeps_its_captured_currency(self):
        """Cash captured in CAD is not presented as USD because the NAV is attested in USD."""
        supplemental = account_facts(
            self.b, self.snapshots[self.b], currency="USD", position_currency="CAD"
        )
        bundle = self.load(supplemental=supplemental)
        account = self.accounts(bundle)[self.b]
        self.assertEqual(account["reported_cash"], "50")
        self.assertEqual(account["cash_currency"], "CAD")
        self.assertEqual(account["cash_currency_basis"], "captured_cash")
        self.assertEqual(account["cash_usd"], format(Decimal("50") * CADUSD, "f"))
        self.assertEqual(account["cash"], account["cash_usd"])
        self.assertEqual(account["reported_currency"], "USD")

    def test_captured_cash_without_a_currency_follows_the_holdings(self):
        supplemental = account_facts(self.b, self.snapshots[self.b], currency="USD")
        bundle = self.load(supplemental=supplemental)
        account = self.accounts(bundle)[self.b]
        self.assertEqual(account["cash_currency"], "CAD")
        self.assertEqual(account["cash_currency_basis"], "inferred_from_positions")
        self.assertEqual(account["cash_usd"], format(Decimal("50") * CADUSD, "f"))
        self.assertEqual(account["reported_currency"], "USD")
        self.assertIn("INFERRED_CASH_CURRENCY", self.codes(bundle["issues"]))

    def test_attested_cash_keeps_the_attested_account_currency(self):
        supplemental = account_facts(
            self.b, self.snapshots[self.b], currency="USD", cash="50", position_currency="CAD"
        )
        bundle = self.load(supplemental=supplemental)
        account = self.accounts(bundle)[self.b]
        self.assertEqual(account["cash_currency"], "USD")
        self.assertEqual(account["cash_currency_basis"], "attested")
        self.assertEqual(account["cash_usd"], "50")

    def test_no_observations_ask_for_a_refresh_not_an_attestation(self):
        bundle = self.load(fx=FxTable())
        codes = self.codes(bundle["exceptions"])
        self.assertNotIn("UNKNOWN_VALUE_CURRENCY", codes)
        missing = {row["pair"] for row in bundle["exceptions"] if row["code"] == "MISSING_FX"}
        self.assertEqual(missing, {"CADUSD", "GBPUSD"})
        self.assertEqual(bundle["normalization"]["unconverted_positions"], 3)

    def test_a_stale_rate_that_explains_nothing_is_still_a_stale_exception(self):
        stale = FxTable(
            [
                observation("CAD", "USD", "0.7300", STALE_DAY, "yahoo"),
                observation("GBP", "CAD", "1.8250", STALE_DAY),
                observation("USD", "CAD", "1.3868", STALE_DAY),
            ],
            max_age_days=7,
        )
        bundle = self.load(fx=stale)
        codes = self.codes(bundle["exceptions"])
        self.assertNotIn("UNKNOWN_VALUE_CURRENCY", codes)
        pairs = {row["pair"] for row in bundle["exceptions"] if row["code"] == "STALE_FX"}
        self.assertIn("CADUSD", pairs)
        self.assertIsNone(self.positions(bundle)[(self.a, "RY.TO")]["reported_currency"])

    def test_a_near_parity_quote_currency_with_a_matching_rate_is_ambiguous(self):
        """A CHF quote and a CHFUSD rate of 1.004 explain the same value; neither is assumed."""
        self.publish([["NESN.SW", "10", "100", "1004.00"]], B_ROWS, timestamp=LATER_RECEIPT)
        table = fx_table()
        table.add(observation("CHF", "USD", "1.004", FX_DAY, "yahoo"))
        bundle = self.load(fx=table, listings=NEAR_PARITY_LISTINGS)
        unknown = [row for row in bundle["exceptions"] if row["code"] == "UNKNOWN_VALUE_CURRENCY"]
        self.assertEqual([row["raw_symbol"] for row in unknown], ["NESN.SW"])
        self.assertEqual({row["currency"] for row in unknown[0]["candidates"]}, {"CHF", "USD"})
        position = self.positions(bundle)[(self.a, "NESN.SW")]
        self.assertIsNone(position["market_value_usd"])
        self.assertIsNone(position["reported_currency"])

    def test_an_attested_currency_answers_the_parity_ambiguity(self):
        snapshots = self.publish(
            [["NESN.SW", "10", "100", "1004.00"]], B_ROWS, timestamp=LATER_RECEIPT
        )
        table = fx_table()
        table.add(observation("CHF", "USD", "1.004", FX_DAY, "yahoo"))
        bundle = self.load(
            fx=table,
            listings=NEAR_PARITY_LISTINGS,
            supplemental=account_facts(self.a, snapshots[self.a], position_currency="USD"),
        )
        position = self.positions(bundle)[(self.a, "NESN.SW")]
        self.assertEqual(position["reported_currency"], "USD")
        self.assertEqual(position["value_currency_basis"], "attested")
        self.assertEqual(position["market_value_usd"], "1004.00")

    def test_load_inputs_converts_the_enriched_price_series(self):
        """Enrichment labels closes GBP; the pence series is still divided and converted."""
        from portfolio_lab import providers

        def enrich(bundle, config, as_of):
            result = providers.enrich_bundle(bundle, config, as_of)
            result["prices"] = pd.DataFrame(
                {
                    "security_id": ["VOD.L", "VOD.L"],
                    "date": ["2026-09-10", FX_DAY],
                    "close": [128.75, 130.00],
                    "adjusted_close": [128.75, 130.00],
                    "currency": ["GBP", "GBP"],
                }
            )
            return result

        table = fx_table()
        table.add(observation("GBP", "USD", "1.30", "2026-09-10", "yahoo"))
        table.add(observation("GBP", "USD", "1.31", FX_DAY, "yahoo"))
        with patch("portfolio_lab.pipeline.enrich_bundle", side_effect=enrich):
            bundle = self.load(fx=table)
        prices = bundle["prices"]
        self.assertEqual(list(prices["currency"]), ["USD", "USD"])
        self.assertEqual(list(prices["local_currency"]), ["GBp", "GBp"])
        self.assertAlmostEqual(prices["close"][0], 128.75 / 100 * 1.30, places=10)
        self.assertAlmostEqual(prices["close"][1], 130.00 / 100 * 1.31, places=10)
        self.assertEqual(list(prices["fx_observation_date"]), ["2026-09-10", FX_DAY])

    def test_without_a_base_currency_amounts_stay_as_captured(self):
        self.config["mandate"]["base_currency"] = None
        bundle = self.load()
        self.loader.assert_not_called()
        normalization = bundle["normalization"]
        self.assertIsNone(normalization["presentation_currency"])
        self.assertIsNone(normalization["covered_value_usd"])
        self.assertEqual(bundle["exceptions"], [])
        row = self.positions(bundle)[(self.b, "RY.TO")]
        self.assertEqual(row["market_value"], "1426.00")
        self.assertIsNone(row["market_value_usd"])
        self.assertIsNone(row["currency"])
        self.assertEqual(row["security_id"], "RY.TO")
        self.assertEqual(self.accounts(bundle)[self.b]["cash"], "50")


class PresentationGuardTests(unittest.TestCase):
    """Without a base currency nothing is treated as already presented."""

    def test_an_amount_is_never_reported_when_there_is_no_presentation_currency(self):
        from portfolio_research.normalize import _convert

        context = {"presentation": None, "fx": FxTable(), "used": set(), "fx_exceptions": {}}
        for currency in (None, "CAD", "USD"):
            amount, fields, status = _convert("50", currency, FX_DAY, context)
            self.assertIsNone(amount, currency)
            self.assertEqual(status, "unknown_currency", currency)
            self.assertEqual(fields, {})
        self.assertEqual(context["fx_exceptions"], {})


class PresentPricesTests(unittest.TestCase):
    """Enriched closes are labelled with the listing's quote unit before conversion."""

    def bundle(self, currency="GBP"):
        return {
            "ledger": {
                "securities": [
                    {
                        "security_id": "VOD.L",
                        "currency": "GBP",
                        "quote_currency": "GBp",
                        "quote_unit_factor": 100,
                    }
                ]
            },
            "prices": pd.DataFrame(
                {
                    "security_id": ["VOD.L", "VOD.L"],
                    "date": ["2026-09-10", "2026-09-11"],
                    "close": [128.75, 130.00],
                    "adjusted_close": [128.75, 130.00],
                    "currency": [currency, currency],
                }
            ),
            "issues": [],
        }

    def table(self):
        return FxTable(
            [
                observation("GBP", "USD", "1.30", "2026-09-10", "yahoo"),
                observation("GBP", "USD", "1.31", "2026-09-11", "yahoo"),
            ],
            max_age_days=7,
        )

    def test_pence_closes_convert_once_at_each_row_date(self):
        from portfolio_research.normalize import present_prices

        config = {"mandate": {"base_currency": "USD"}}
        result = present_prices(self.bundle(), config, self.table())
        prices = result["prices"]
        self.assertEqual(list(prices["currency"]), ["USD", "USD"])
        self.assertEqual(list(prices["local_currency"]), ["GBp", "GBp"])
        self.assertAlmostEqual(prices["close"][0], 128.75 / 100 * 1.30, places=10)
        self.assertAlmostEqual(prices["close"][1], 130.00 / 100 * 1.31, places=10)
        self.assertEqual(list(prices["fx_observation_date"]), ["2026-09-10", "2026-09-11"])
        self.assertEqual(result["issues"], [])

    def test_no_base_currency_leaves_the_series_untouched(self):
        from portfolio_research.normalize import present_prices

        bundle = self.bundle()
        result = present_prices(bundle, {"mandate": {"base_currency": None}}, self.table())
        self.assertEqual(list(result["prices"]["currency"]), ["GBP", "GBP"])


if __name__ == "__main__":
    unittest.main()
