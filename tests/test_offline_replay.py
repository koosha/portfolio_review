"""Gate: an archived review replays from its frozen inputs with every provider refused.

One review is enriched through the canned adapter and archived, then replayed with the
input connector and the market adapter patched to raise. What the replay has to
reproduce is the researched evidence the owner reads — briefs, coverage, the acquired
universe and above all the proposals — because a preview that silently dropped them
would present a reviewed security as one with nothing to say.
"""

import tempfile
import unittest
from contextlib import ExitStack
from unittest.mock import patch

from portfolio.storage import Store
from portfolio_lab.config import validate_config
from portfolio_lab.ingestion import ResearchStore, _pack_bundle, _unpack_bundle
from portfolio_lab.pipeline import load_inputs, replay_analysis, save_analysis
from portfolio_research.application import analyze_review
from portfolio_research.fx import FxTable
from portfolio_research.service import default_config
from tests.test_enrichment import (
    A_ROWS,
    B_ROWS,
    ReviewTicker,
    fx_records,
    issuer_lookup,
    listing_metadata,
)
from tests.test_market_data import FakeTicker
from tests.test_normalize import SUNDAY_GENERATED, SUNDAY_RECEIPT, cad_table, usd_table

# What a preview needs from the archive to answer without reaching a provider.
FROZEN_RESEARCH = ("research_inputs", "coverage", "research", "universe")


def refuse(*args, **kwargs):
    raise AssertionError("a replay reached a provider")


class OfflineReplayTests(unittest.TestCase):
    """Two US equities, one UK equity and one Canadian fund, archived once for the class."""

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        store = Store(cls.temp.name)
        a = store.add_source(
            "USD portfolio", url="https://finance.yahoo.com/portfolio/p_fixture_replay_a"
        )
        b = store.add_source(
            "CAD portfolio", url="https://finance.yahoo.com/portfolio/p_fixture_replay_b"
        )
        with patch("portfolio.storage.now", return_value=SUNDAY_RECEIPT):
            batch = store.begin_batch([a, b])
            store.ingest_table(a, usd_table(A_ROWS), batch_id=batch)
            store.ingest_table(b, cad_table(B_ROWS), batch_id=batch, extension_version="1.2.0")
        config = default_config(cls.temp.name)
        config["data"].update(
            mode="live",
            price_provider="yahoo",
            lookback_years=1,
            provider_min_interval_seconds=0,
            provider_timeout_seconds=5,
        )
        config["risk"].update(min_weekly_observations=26, bootstrap_samples=50)
        cls.config = validate_config(config)
        FakeTicker.calls = []
        cls.bundle = cls.enriched()
        cls.saved = save_analysis(analyze_review(cls.bundle, cls.config), cls.config, cls.bundle)
        cls.run_id = cls.saved["run_id"]

    @classmethod
    def enriched(cls):
        """The resolved, enriched bundle for this fixture with every provider canned."""
        table = FxTable(fx_records(), max_age_days=7)
        with ExitStack() as stack:
            for context in (
                patch("yfinance.Ticker", ReviewTicker),
                patch(
                    "portfolio_research.market_listings.listing_metadata",
                    side_effect=listing_metadata,
                ),
                patch("portfolio_research.fx_providers.load_fx_table", return_value=table),
                patch("portfolio_research.issuers.sec_issuer_lookup", return_value=issuer_lookup),
            ):
                stack.enter_context(context)
            return load_inputs(
                cls.config, refresh=True, review_kind="current", generated_at=SUNDAY_GENERATED
            )

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def frozen(self):
        return ResearchStore(self.config["research"]["path"]).load_bundle(self.run_id)

    def test_the_archive_keeps_every_researched_input_of_the_bundle(self):
        frozen, packed = self.frozen(), _unpack_bundle(_pack_bundle(self.bundle))
        for name in FROZEN_RESEARCH:
            with self.subTest(name):
                self.assertTrue(self.bundle.get(name), f"{name} missing from the enriched bundle")
                self.assertEqual(frozen[name], packed[name])
        self.assertEqual(sorted(frozen["research"]), sorted(self.bundle["research"]))
        self.assertTrue(frozen["research"]["AAPL"]["proposals"]["eps"])
        self.assertTrue(frozen["research"]["AAPL"]["brief"]["sources"])
        self.assertTrue(frozen["research_inputs"]["AAPL"]["estimates"]["eps"])
        self.assertEqual(frozen["coverage"]["by_security"]["AAPL"]["prices"], "ok")
        self.assertTrue(frozen["universe"]["coverage"])

    def test_replay_reproduces_the_research_with_every_provider_refused(self):
        with (
            patch("portfolio_lab.pipeline.enrich_bundle", side_effect=refuse),
            patch("portfolio_lab.providers.enrich_bundle", side_effect=refuse),
            patch("portfolio_research.enrichment.enrich_market", side_effect=refuse),
            patch("yfinance.Ticker", side_effect=refuse),
        ):
            replayed, frozen, _ = replay_analysis(self.config, self.run_id)
        self.assertEqual(replayed["summary"], self.saved["summary"])
        self.assertTrue(replayed["metadata"]["preview"])
        self.assertEqual(replayed["metadata"]["parent_run_id"], self.run_id)
        self.assertEqual(
            {sid: row["proposals"] for sid, row in replayed["research"].items()},
            {sid: row["proposals"] for sid, row in self.saved["research"].items()},
        )
        self.assertTrue(replayed["research"]["AAPL"]["proposals"]["eps"])
        self.assertEqual(replayed["research"], self.saved["research"])
        self.assertEqual(replayed["coverage"], self.saved["coverage"])
        self.assertEqual(replayed["universe"], self.saved["universe"])
        self.assertTrue(frozen["timeline"]["frozen_input_replay"])


if __name__ == "__main__":
    unittest.main()
