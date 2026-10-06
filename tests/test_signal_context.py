"""The published Q/V/M view keeps the actual cross-section behind its scores."""

import unittest

from portfolio_lab.analytics import analyze
from portfolio_research.public import public_result
from tests.reference.test_metrics import sample


class SignalContextTests(unittest.TestCase):
    def test_analysis_and_public_view_preserve_unique_issuer_scope(self):
        bundle, config = sample()
        result = analyze(bundle, config)
        context = result["signal_context"]
        self.assertEqual(context["supplied_securities"], 5)
        self.assertEqual(context["ranked_issuers"], 3)
        self.assertEqual(context["minimum_sector_peers"], 2)
        self.assertEqual(context["complete_composite_issuers"], 3)
        self.assertFalse(context["market_wide"])
        published = public_result(result, bundle)
        self.assertEqual(published["signal_context"], context)
        self.assertEqual(len(published["signals"]), 5)
        self.assertEqual(
            next(row for row in published["signals"] if row["security_id"] == "A2")[
                "representative_security_id"
            ],
            "A",
        )

    def test_production_peer_minimum_reports_incomplete_composites(self):
        bundle, config = sample()
        config["signals"]["min_sector_size"] = 20
        result = public_result(analyze(bundle, config), bundle)
        context = result["signal_context"]
        self.assertEqual(context["ranked_issuers"], 3)
        self.assertEqual(context["momentum_issuers"], 3)
        self.assertEqual(context["complete_composite_issuers"], 0)
        self.assertTrue(all(row["score"] is None for row in result["signals"]))
        self.assertTrue(
            all(row["sector_peer_count"] == 3 for row in result["signals"] if row["eligible"])
        )


if __name__ == "__main__":
    unittest.main()
