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

    def publish(self, a_rows, b_rows, timestamp=SUNDAY_RECEIPT, cash_currency="CAD"):
        with patch("portfolio.storage.now", return_value=timestamp):
            batch = self.store.begin_batch([self.a, self.b])
            first = self.store.ingest_table(self.a, usd_table(a_rows), batch_id=batch)
            second = self.store.ingest_table(
                self.b,
                cad_table(b_rows, cash_currency=cash_currency),
                batch_id=batch,
                extension_version="1.2.0",
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

    def test_a_source_converted_price_reconciles_with_the_value_it_reported(self):
        """A 0.3% gap between the source's own rate and the dated one is not an error.

        RY.TO is quoted in CAD and reported in USD, so the source converted it at a rate
        of its own. The presented price follows that value rather than the dated
        observation, and the gap between the two rates is a rounding difference, not a
        question about the currency.
        """
        self.publish([["RY.TO", "10", "285.20", "2062.83"]], B_ROWS, timestamp=LATER_RECEIPT)
        bundle = self.load()
        row = self.positions(bundle)[(self.a, "RY.TO")]
        self.assertEqual(row["value_currency_basis"], "presentation")
        self.assertEqual(row["quote_currency"], "CAD")
        self.assertEqual(row["reported_currency"], "USD")
        self.assertEqual(row["market_value_usd"], "2062.83")
        self.assertEqual(Decimal(row["price_usd"]) * 10, Decimal("2062.83"))
        with localcontext() as context:
            context.prec = 34
            drift = Decimal("2062.83") / (Decimal("10") * Decimal("285.20") * CADUSD) - 1
        self.assertLess(abs(drift), Decimal("0.005"))
        codes = self.codes(metrics.reconcile(bundle, self.config)["issues"])
        self.assertNotIn("position_arithmetic", codes)
        # Well inside the tolerance, so the data-quality check stays quiet.
        self.assertNotIn("VALUE_ARITHMETIC_MISMATCH", self.codes(bundle["issues"]))

    def test_a_converted_price_reconciles_with_the_converted_value(self):
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
        snapshots = self.publish(A_ROWS, B_ROWS, timestamp=LATER_RECEIPT, cash_currency="")
        supplemental = account_facts(self.b, snapshots[self.b], currency="USD")
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

    def test_a_source_that_reports_in_another_currency_is_reported_loudly(self):
        """The check that stands behind assuming an unlabelled source reports in USD.

        Here account A labels nothing but its captured value really is CAD: quantity x
        the CAD price equals it exactly. Taking that value as USD would overstate the
        holding by about 39%, so the arithmetic that used to guess the currency now says
        plainly that these three numbers cannot all be right.
        """
        self.publish([["RY.TO", "10", "285.20", "2852.00"]], B_ROWS, timestamp=LATER_RECEIPT)
        bundle = self.load()
        mismatched = [i for i in bundle["issues"] if i["code"] == "VALUE_ARITHMETIC_MISMATCH"]
        self.assertEqual(len(mismatched), 1)
        self.assertEqual(mismatched[0]["severity"], "warning")
        self.assertIn("RY.TO", mismatched[0]["message"])
        self.assertIn("CAD", mismatched[0]["message"])
        # It is a warning about the row, not a question to answer: nothing is reopened.
        self.assertEqual(bundle["exceptions"], [])

    def test_a_holding_the_source_converted_itself_raises_no_warning(self):
        """The owner's real shape: quoted in CAD, reported in USD, and silent."""
        bundle = self.load()
        self.assertEqual(
            [i for i in bundle["issues"] if i["code"] == "VALUE_ARITHMETIC_MISMATCH"], []
        )
        self.assertEqual(self.positions(bundle)[(self.a, "RY.TO")]["quote_currency"], "CAD")
        self.assertEqual(self.positions(bundle)[(self.a, "RY.TO")]["reported_currency"], "USD")

    def test_no_observations_ask_for_a_refresh_not_an_attestation(self):
        bundle = self.load(fx=FxTable())
        codes = self.codes(bundle["exceptions"])
        self.assertNotIn("UNKNOWN_VALUE_CURRENCY", codes)
        # Only B's CAD-labelled values need a rate to be *converted*; A already reports in
        # USD. A's Toronto row still needs one to be *checked*, and says so below.
        missing = {row["pair"] for row in bundle["exceptions"] if row["code"] == "MISSING_FX"}
        self.assertEqual(missing, {"CADUSD"})
        self.assertEqual(set(codes), {"MISSING_FX"})
        # B's two CAD-reported rows, plus A's RY.TO: quoted in CAD and taken as USD with
        # no rate to confirm that, so it is withheld rather than counted at 1:1.
        self.assertEqual(bundle["normalization"]["unconverted_positions"], 3)

    def test_a_currency_that_cannot_be_checked_is_withheld_not_counted(self):
        """No rate means the assumption is unverifiable, so it never reaches the total."""
        bundle = self.load(fx=FxTable())
        held = self.positions(bundle)[(self.a, "RY.TO")]
        self.assertEqual(held["quote_currency"], "CAD")
        self.assertEqual(held["reported_currency"], "USD")
        self.assertEqual(held["reported_market_value"], "2056.30")
        self.assertIsNone(held["market_value_usd"])
        self.assertIsNone(held["price_usd"])
        # A's Toronto row and B's London one: both quoted somewhere their value is not
        # reported in, and no rate exists to confirm either.
        unchecked = {
            i["message"].split()[0]: i
            for i in bundle["issues"]
            if i["code"] == "VALUE_CURRENCY_UNCHECKED"
        }
        self.assertEqual(set(unchecked), {"RY.TO", "VOD.L"})
        self.assertIn("CAD", unchecked["RY.TO"]["message"])
        self.assertEqual(unchecked["RY.TO"]["severity"], "warning")
        # AAPL's 600 and A's 100 of cash: an unverifiable 2056.30 is not silently added.
        self.assertEqual(Decimal(bundle["normalization"]["covered_value_usd"]), Decimal("700"))

    def test_a_pair_reachable_through_an_intermediate_still_checks(self):
        """GBP/CAD is not published here, but GBP->USD->CAD is, so VOD.L is still tested."""
        through_usd = FxTable(
            [
                observation("CAD", "USD", "0.7211", FX_DAY, "yahoo"),
                observation("GBP", "USD", "1.3160", FX_DAY, "yahoo"),
            ],
            max_age_days=7,
        )
        bundle = self.load(fx=through_usd)
        codes = {i["code"] for i in bundle["issues"]}
        self.assertNotIn("VALUE_CURRENCY_UNCHECKED", codes)
        self.assertNotIn("VALUE_ARITHMETIC_MISMATCH", codes)
        self.assertIsNotNone(self.positions(bundle)[(self.b, "VOD.L")]["market_value_usd"])

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
        # The stale rate blocks B's conversion; it never unsettles a currency, and A,
        # which needs no rate, is presented as usual.
        self.assertIsNone(self.positions(bundle)[(self.b, "RY.TO")]["market_value_usd"])
        a_ry = self.positions(bundle)[(self.a, "RY.TO")]
        self.assertEqual(a_ry["reported_currency"], "USD")
        self.assertEqual(a_ry["market_value_usd"], "2056.30")

    def test_a_near_parity_quote_currency_is_decided_by_its_listing(self):
        """CHF and USD near parity is where guessing from arithmetic was worst.

        A CHF quote and a CHFUSD rate of 1.004 explain the captured value equally well,
        so the ratio could never separate them and the holding became a question. The
        listing has always known: ``NESN.SW`` is quoted in CHF, and the account says it
        reports in USD, so there is nothing left to be ambiguous about.
        """
        self.publish([["NESN.SW", "10", "100", "1004.00"]], B_ROWS, timestamp=LATER_RECEIPT)
        table = fx_table()
        table.add(observation("CHF", "USD", "1.004", FX_DAY, "yahoo"))
        bundle = self.load(fx=table, listings=NEAR_PARITY_LISTINGS)
        position = self.positions(bundle)[(self.a, "NESN.SW")]
        self.assertEqual(position["quote_currency"], "CHF")
        self.assertEqual(position["reported_currency"], "USD")
        self.assertEqual(position["value_currency_basis"], "presentation")
        self.assertEqual(position["market_value_usd"], "1004.00")
        self.assertEqual([row["raw_symbol"] for row in bundle["exceptions"]], [])

    def test_a_swiss_venue_suffix_answers_when_no_listing_does(self):
        """Without listing metadata, ``.SW`` still states CHF and the value still stands."""
        self.publish([["NESN.SW", "10", "100", "1004.00"]], B_ROWS, timestamp=LATER_RECEIPT)
        table = fx_table()
        table.add(observation("CHF", "USD", "1.004", FX_DAY, "yahoo"))
        bundle = self.load(fx=table, listings=LISTINGS)
        position = self.positions(bundle)[(self.a, "NESN.SW")]
        self.assertEqual(position["quote_currency"], "CHF")
        self.assertEqual(position["quote_unit_factor"], 1)
        self.assertEqual(position["price_major"], "100")
        self.assertEqual(position["reported_currency"], "USD")
        self.assertEqual(position["market_value_usd"], "1004.00")
        # The listing itself is still unknown, and that stays the only open question.
        self.assertEqual(self.codes(bundle["exceptions"]), ["UNRESOLVED_LISTING"])

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
        # Captured, and still carrying the currency the capture itself labelled it with.
        self.assertEqual(row["currency"], "CAD")
        self.assertEqual(row["reported_currency"], "CAD")
        self.assertEqual(row["security_id"], "RY.TO")
        self.assertEqual(self.accounts(bundle)[self.b]["cash"], "50")


if __name__ == "__main__":
    unittest.main()
