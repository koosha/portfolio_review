"""Gate: USD and CAD Yahoo portfolios reconcile in USD without double conversion."""

import copy
import tempfile
import unittest
from decimal import Decimal
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
from tests.support.normalization import (
    A_ROWS,
    B_ROWS,
    CADUSD,
    FX_DAY,
    LATER_RECEIPT,
    LISTINGS,
    RECEIPT_DAY,
    STALE_DAY,
    SUNDAY_GENERATED,
    SUNDAY_RECEIPT,
    cad_table,
    fx_table,
    issuer_lookup,
    usd_table,
)


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


if __name__ == "__main__":
    unittest.main()
