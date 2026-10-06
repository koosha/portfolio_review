"""Recalculation cannot recycle annual-backed DCF defaults from older archives."""

import unittest
from copy import deepcopy

from portfolio_lab.config import validate_config
from portfolio_research.application import _research_for_quarterly_policy


class QuarterlyRecalculationTests(unittest.TestCase):
    def setUp(self):
        self.config = validate_config({})
        self.bundle = {"as_of": "2026-08-31"}
        self.research = {
            "EXAMPLE": {
                "proposals": {
                    "eps": {"proposal_meta": {"model": "eps_multiple", "estimate_period": "+1y"}},
                    "dcf": {"proposal_meta": {"method_version": "proposal-template-2"}},
                    "reasons": [],
                }
            }
        }

    def test_annual_backed_defaults_are_withheld_without_changing_frozen_sources(self):
        original = deepcopy(self.research)
        current = _research_for_quarterly_policy(self.research, self.bundle, self.config)
        self.assertIsNone(current["EXAMPLE"]["proposals"]["dcf"])
        self.assertEqual(
            current["EXAMPLE"]["proposals"]["eps"], original["EXAMPLE"]["proposals"]["eps"]
        )
        self.assertEqual(self.research, original)
        self.assertEqual(
            current["EXAMPLE"]["proposals"]["reasons"][0]["code"], "dcf_quarterly_history_required"
        )

    def test_in_window_quarterly_defaults_remain_available(self):
        self.research["EXAMPLE"]["proposals"]["dcf"]["proposal_meta"].update(
            financial_history={
                "basis": "quarterly",
                "lookback_years": 3,
                "latest_period_end": "2026-06-30",
                "current_ttm_quarters": ["2026-06-30", "2026-03-31", "2025-12-31", "2025-09-30"],
            }
        )
        current = _research_for_quarterly_policy(self.research, self.bundle, self.config)
        self.assertEqual(current, self.research)

    def test_claimed_quarterly_defaults_outside_the_window_are_withheld(self):
        self.research["EXAMPLE"]["proposals"]["dcf"]["proposal_meta"].update(
            financial_history={
                "basis": "quarterly",
                "lookback_years": 3,
                "latest_period_end": "2023-09-30",
                "current_ttm_quarters": ["2023-09-30", "2023-06-30", "2023-03-31", "2022-12-31"],
            }
        )
        current = _research_for_quarterly_policy(self.research, self.bundle, self.config)
        self.assertIsNone(current["EXAMPLE"]["proposals"]["dcf"])


if __name__ == "__main__":
    unittest.main()
