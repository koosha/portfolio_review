"""The normalization gate fixture seen through the current holdings projection.

Two captured accounts -- one whose page labels nothing and one that states CAD -- read
back as the Overview shows them: what each holding is quoted in, what its account reports
values in, and the single USD rollup both accounts reach.
"""

import tempfile
import unittest
from decimal import Decimal
from unittest.mock import patch

from portfolio.storage import Store
from portfolio_research.adapter import account_id
from portfolio_research.current import current_snapshot
from portfolio_research.service import default_config
from tests.support.normalization import (
    A_ROWS,
    B_ROWS,
    CADUSD,
    FX_DAY,
    STALE_DAY,
    cad_table,
    fx_table,
    usd_table,
)
from tests.test_current import (
    EXCEPTION_KEY,
    SUNDAY_GENERATED,
    SUNDAY_RECEIPT,
    cached_providers,
    covered_usd,
)


class CurrentUsdPresentationTests(unittest.TestCase):
    """The normalization gate fixture seen through the current holdings projection."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.a = self.store.add_source(
            "USD portfolio", url="https://finance.yahoo.com/portfolio/p_fixture_current_usd"
        )
        self.b = self.store.add_source(
            "CAD portfolio", url="https://finance.yahoo.com/portfolio/p_fixture_current_cad"
        )
        with patch("portfolio.storage.now", return_value=SUNDAY_RECEIPT):
            batch = self.store.begin_batch([self.a, self.b])
            self.store.ingest_table(self.a, usd_table(A_ROWS), batch_id=batch)
            self.store.ingest_table(
                self.b, cad_table(B_ROWS), batch_id=batch, extension_version="1.2.0"
            )

    def snapshot(self, **kwargs):
        return current_snapshot(self.store.path, generated_at=SUNDAY_GENERATED, **kwargs)

    @staticmethod
    def by_symbol(response):
        return {(row["source_id"], row["symbol"]): row for row in response["positions"]}

    def test_usd_totals_cover_positions_and_cash_once(self):
        with cached_providers() as calls:
            response = self.snapshot()
        self.assertTrue(calls)
        self.assertFalse(any(refresh for _, _, refresh in calls))
        usd = response["totals"]["usd"]
        # A mixed CAD/USD/GBp portfolio rolls up to one USD total, and every captured
        # amount reaches it exactly once: nothing counted twice, nothing left behind.
        self.assertEqual(Decimal(usd["covered_total"]), covered_usd(response))
        self.assertEqual(usd["covered_position_count"], 4)
        self.assertEqual(usd["unconverted_position_count"], 0)
        self.assertEqual(usd["unconverted_accounts"], [])
        self.assertIn("dated FX observations", usd["label"])
        self.assertFalse(response["totals"]["reconciled_nav"])
        # Captured subtotals stay grouped by what each source labelled: one page states
        # CAD, the other labels nothing at all, and neither is converted here.
        self.assertEqual(
            [row["currency"] for row in response["totals"]["by_currency"]], ["CAD", None]
        )
        self.assertEqual(
            response["identity"],
            {
                "mapped": 0,
                "resolved": 2,
                "resolved_from_display": 2,
                "resolved_by_search": 0,
                "ambiguous": 0,
                "unresolved": 0,
            },
        )
        self.assertEqual(response["open_exceptions"], [])
        # Every conversion names a dated observation, and only the observations the
        # holdings actually used are reported.
        used = {(row["pair"], row["date"]) for row in response["fx_observations"]}
        self.assertTrue(used)
        self.assertEqual({day for _, day in used}, {FX_DAY})
        for row in response["positions"]:
            if row["fx_pair"]:
                self.assertIn((row["fx_pair"], row["fx_observation_date"]), used)
        accounts = {row["source_id"]: row for row in response["accounts"]}
        held = self.by_symbol(response)
        for source_id in (self.a, self.b):
            account, presented = accounts[source_id], accounts[source_id]["usd"]
            mine = [row for key, row in held.items() if key[0] == source_id]
            self.assertTrue(presented["fully_covered"])
            self.assertEqual(presented["covered_position_count"], len(mine))
            self.assertEqual(presented["unconverted_position_count"], 0)
            self.assertEqual(
                Decimal(presented["covered_total"]),
                sum(Decimal(row["market_value_usd"]) for row in mine) + Decimal(presented["cash"]),
                f"{account['name']} counts its holdings and its cash once",
            )
        self.assertEqual(accounts[self.b]["identity"], {"resolved": 2, "open": 0})
        self.assertEqual(accounts[self.b]["open_exception_count"], 0)
        # Captured cash is reported exactly as collected: labelled where the source page
        # labelled it, unlabeled where it did not.
        self.assertEqual(accounts[self.b]["cash"], {"amount": "50", "currency": "CAD"})
        self.assertEqual(accounts[self.a]["cash"], {"amount": "100", "currency": None})

    def test_positions_keep_captured_amounts_beside_their_usd_presentation(self):
        with cached_providers():
            held = self.by_symbol(self.snapshot())
        a_ry, a_aapl, b_ry, b_vod = (
            held[(self.a, "RY.TO")],
            held[(self.a, "AAPL")],
            held[(self.b, "RY.TO")],
            held[(self.b, "VOD.L")],
        )
        # Captured amounts are kept exactly as observed, whatever is derived beside them.
        self.assertEqual(b_ry["market_value"], "1426.00")
        self.assertEqual(b_ry["price"], "285.20")
        self.assertEqual(b_ry["currency"], "CAD")
        self.assertEqual(b_ry["quote_symbol"], "RY.TO")
        self.assertEqual(b_ry["security_id"], "RY.TO")
        self.assertEqual(b_ry["resolution_status"], "resolved")
        self.assertEqual(b_ry["name"], "RY.TO fixture listing")
        self.assertIsNone(a_ry["quote_symbol"])
        self.assertEqual(a_ry["market_value"], "2056.30")
        self.assertEqual(a_ry["resolution_status"], "resolved_from_display")

        # What a holding is quoted in is its listed exchange's answer, in whichever
        # account it sits: both RY.TO rows are Toronto listings in CAD, AAPL is a US
        # listing in USD, VOD.L is London pence, which is GBP at a factor of one hundred.
        self.assertEqual(a_ry["quote_currency"], "CAD")
        self.assertEqual(b_ry["quote_currency"], "CAD")
        self.assertEqual(a_aapl["quote_currency"], "USD")
        self.assertEqual(b_vod["quote_currency"], "GBp")
        self.assertEqual(b_vod["quote_unit_factor"], 100)
        self.assertEqual(b_vod["price"], "128.75")
        self.assertEqual(b_vod["price_major"], "1.2875")
        # The account captured with Yahoo quote symbols reports its values in the currency
        # those listings are quoted in, and one dated observation converts them once.
        self.assertEqual(b_ry["value_currency"], "CAD")
        self.assertEqual(Decimal(b_ry["market_value_usd"]), Decimal("1426.00") * CADUSD)
        self.assertEqual(b_ry["fx_pair"], "CADUSD")
        self.assertEqual(b_ry["fx_rate"], "0.7211")
        self.assertEqual(b_ry["fx_observation_date"], FX_DAY)

        for row in held.values():
            # No holding is left without a currency, and none is presented in anything
            # but USD; an amount already in USD is never converted a second time.
            self.assertIsNotNone(row["value_currency"], row["symbol"])
            self.assertEqual(row["presentation_currency"], "USD")
            self.assertIsNotNone(row["market_value_usd"], row["symbol"])
            if row["value_currency"] == "USD":
                self.assertIsNone(row["fx_pair"], row["symbol"])
                self.assertEqual(row["market_value_usd"], row["market_value"])
            else:
                self.assertTrue(row["fx_pair"], row["symbol"])
                self.assertEqual(row["fx_observation_date"], FX_DAY)

    def test_stale_fx_leaves_foreign_amounts_unconverted_and_open(self):
        with cached_providers(fx=fx_table(STALE_DAY)):
            response = self.snapshot()
        usd = response["totals"]["usd"]
        # The account that reports in USD needs no rate and is unaffected; the account
        # that reports in CAD has no usable rate that day, so its amounts stay exactly as
        # captured and are counted as unconverted rather than converted at a stale rate.
        self.assertEqual(Decimal(usd["covered_total"]), covered_usd(response))
        self.assertEqual(usd["covered_position_count"], 2)
        self.assertEqual(usd["unconverted_position_count"], 2)
        self.assertEqual(usd["unconverted_accounts"], [account_id(self.b)])
        # A rate that is genuinely too old is an FX question answered by refreshing market
        # data. It is never turned into a question about what currency a holding is in.
        codes = {row["code"] for row in response["open_exceptions"]}
        self.assertNotIn("UNKNOWN_VALUE_CURRENCY", codes)
        stale = [row for row in response["open_exceptions"] if row["scope"] == "fx"]
        self.assertIn("CADUSD", {row["pair"] for row in stale})
        for record in stale:
            self.assertIn(record["code"], {"STALE_FX", "MISSING_FX"})
            self.assertRegex(record["key"], EXCEPTION_KEY)
            self.assertEqual(record["resolution"]["kind"], "fx_manual")
        held = self.by_symbol(response)
        self.assertEqual(held[(self.a, "AAPL")]["market_value_usd"], "600")
        self.assertIsNone(held[(self.b, "RY.TO")]["market_value_usd"])
        self.assertEqual(held[(self.b, "RY.TO")]["market_value"], "1426.00")
        accounts = {row["source_id"]: row for row in response["accounts"]}
        self.assertFalse(accounts[self.b]["usd"]["fully_covered"])
        self.assertIsNone(accounts[self.b]["usd"]["cash"])
        self.assertTrue(accounts[self.a]["usd"]["fully_covered"])

    def test_explicit_configuration_selects_the_listing_provider(self):
        config = default_config(self.temp.name)
        config["data"]["listing_provider"] = "none"
        with cached_providers() as calls:
            response = self.snapshot(config=config)
        self.assertEqual([call for call in calls if call[0] == "listing"], [])
        self.assertEqual(response["identity"]["unresolved"], 4)
        codes = [row["code"] for row in response["open_exceptions"]]
        self.assertEqual(codes.count("UNRESOLVED_LISTING"), 4)
        # With no listing provider to ask, the ticker still says where each holding
        # trades: RY.TO a Canadian venue, VOD.L London pence, a bare ticker the United
        # States. The listing stays the open question; the currency never becomes one.
        self.assertNotIn("UNKNOWN_VALUE_CURRENCY", codes)
        held = self.by_symbol(response)
        self.assertEqual(held[(self.a, "AAPL")]["quote_currency"], "USD")
        self.assertEqual(held[(self.a, "AAPL")]["value_currency"], "USD")
        self.assertEqual(held[(self.a, "RY.TO")]["quote_currency"], "CAD")
        self.assertEqual(held[(self.b, "RY.TO")]["value_currency"], "CAD")
        self.assertEqual(held[(self.b, "VOD.L")]["quote_currency"], "GBp")
        self.assertEqual(held[(self.b, "VOD.L")]["quote_unit_factor"], 100)
        covered = Decimal(response["totals"]["usd"]["covered_total"])
        self.assertEqual(covered, covered_usd(response))
        self.assertGreater(covered, 0)
