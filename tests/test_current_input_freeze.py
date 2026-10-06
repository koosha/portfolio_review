"""Fresh current inputs become eligible without changing historical/replay cutoffs."""

import unittest
from copy import deepcopy
from unittest.mock import patch

from portfolio_lab.metrics import score_securities
from portfolio_lab.providers import enrich_bundle
from portfolio_research.calendar import (
    current_receipt_limit,
    freeze_current_inputs,
    review_context,
)
from tests.reference.test_metrics import sample

START = "2026-08-31T21:00:00+00:00"
RECEIVED = "2026-08-31T21:00:02+00:00"
COMPLETED = "2026-08-31T21:00:03+00:00"


class CurrentInputFreezeTests(unittest.TestCase):
    def inputs(self):
        bundle, config = sample()
        bundle["timeline"] = review_context("current", generated_at=START)
        bundle["sources"] = [{"source_id": "profile:current", "received_at": RECEIVED}]
        for field in ("market_cap_available_at", "market_cap_received_at"):
            bundle["securities"][field] = RECEIVED
        return bundle, config

    def test_fresh_quote_caps_rank_only_after_their_real_receipt(self):
        bundle, config = self.inputs()
        self.assertEqual(
            score_securities(bundle, config).attrs["scoring_context"]["ranked_issuers"], 0
        )
        before = deepcopy(bundle["sources"])
        freeze_current_inputs(bundle, completed_at=COMPLETED)
        self.assertEqual(bundle["timeline"]["information_cutoff"], RECEIVED)
        self.assertEqual(current_receipt_limit(bundle).isoformat(), RECEIVED)
        self.assertEqual(bundle["timeline"]["input_acquisition_started_at"], START)
        self.assertEqual(bundle["timeline"]["market_observation_date"], "2026-08-31")
        self.assertEqual(bundle["sources"], before)
        self.assertTrue(bundle["securities"].market_cap_available_at.eq(RECEIVED).all())
        config["data"]["require_received_by_cutoff"] = True
        self.assertEqual(
            score_securities(bundle, config).attrs["scoring_context"]["ranked_issuers"], 3
        )

    def test_future_and_out_of_window_receipts_cannot_advance_cutoff(self):
        for receipt in ("2026-08-31T21:00:04+00:00", "2026-08-31T23:00:00+00:00"):
            bundle, _ = self.inputs()
            bundle["sources"][0]["received_at"] = receipt
            freeze_current_inputs(bundle, completed_at=COMPLETED)
            self.assertEqual(bundle["timeline"]["information_cutoff"], START)
            self.assertEqual(current_receipt_limit(bundle).isoformat(), START)

    def test_historical_and_replay_timelines_are_immutable(self):
        for kind in ("historical", "replay"):
            bundle, _ = self.inputs()
            if kind == "historical":
                bundle["timeline"] = review_context(
                    "historical", as_of="2026-08-31", generated_at=START
                )
            else:
                bundle["timeline"]["frozen_input_replay"] = True
            before = deepcopy(bundle["timeline"])
            freeze_current_inputs(bundle, completed_at=COMPLETED)
            self.assertEqual(bundle["timeline"], before)

    def test_provider_pipeline_freezes_before_filtering_newly_available_facts(self):
        bundle, config = self.inputs()
        config["data"].update(
            mode="live", price_provider="yahoo", sec_enabled=False, fred_enabled=False
        )
        bundle["fundamentals"]["available_at"] = RECEIVED
        bundle["fundamentals"]["received_at"] = RECEIVED
        with patch("portfolio_lab.providers._enrich_yahoo"):
            enriched = enrich_bundle(bundle, config, bundle["as_of"])
        self.assertEqual(enriched["timeline"]["information_cutoff"], RECEIVED)
        self.assertEqual(len(enriched["fundamentals"]), 5)
        self.assertEqual(bundle["timeline"]["information_cutoff"], START)


if __name__ == "__main__":
    unittest.main()
