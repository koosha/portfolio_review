"""Normalized prices, cash and NAV reconcile with the values and rates that explain them."""

import copy
import tempfile
import unittest
from decimal import Decimal, localcontext
from unittest.mock import patch

import pandas as pd

from portfolio.storage import Store
from portfolio_lab import metrics
from portfolio_lab.pipeline import load_inputs
from portfolio_research.fx import FxTable
from portfolio_research.service import default_config
from tests.support.normalization import (
    A_ROWS,
    B_ROWS,
    CADUSD,
    FX_DAY,
    LATER_RECEIPT,
    LISTINGS,
    STALE_DAY,
    SUNDAY_GENERATED,
    SUNDAY_RECEIPT,
    cad_table,
    fx_table,
    issuer_lookup,
    listing,
    observation,
    usd_table,
)

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


if __name__ == "__main__":
    unittest.main()
