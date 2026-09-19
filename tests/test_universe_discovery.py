"""Candidate discovery stays inside its own bounds and reports the scope it reached.

Resolving a screened symbol to an issuer is part of the fetch, so the enrichment limit,
a stop request and an expired time budget all reach it: the tail it never asked about is
excluded as unattempted rather than called a foreign issuer. The one human sentence the
review prints about scope then states how much of that scope was actually enriched, and
says the discovery was interrupted instead of claiming a completed screen. No network:
the screen and the profile provider are local fakes.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.support.securities import securities, security

AS_OF = "2026-09-18"


def screen_payload(count):
    quotes = [
        {
            "symbol": f"C{index:03d}",
            "quoteType": "EQUITY",
            "currency": "USD",
            "exchange": "NMS",
            "longName": f"Candidate Issuer {index}",
            "marketCap": 900_000_000_000 - index * 1_000_000,
            "sharesOutstanding": 1_000_000_000,
        }
        for index in range(count)
    ]
    return {"start": 0, "count": count, "total": count, "quotes": quotes}


class DiscoveryBoundTests(unittest.TestCase):
    """Identity resolution is part of the fetch, and obeys the fetch's own bounds."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        from portfolio_lab.config import DEFAULTS

        self.config = {
            "research": {"path": str(Path(self.temp.name) / "research.sqlite")},
            "data": {
                "mode": "live",
                "provider_min_interval_seconds": 0,
                "provider_timeout_seconds": 5,
                "universe_acquire_size": 40,
                "universe_size": 40,
                "candidate_enrichment_limit": 1000,
                "provider_time_budget_seconds": 1800,
                "sec_enabled": False,
            },
            "signals": dict(DEFAULTS["signals"]),
            "mandate": {"account_candidate_policy": {"a1": "eligible_universe"}},
            "allocation": {"horizon_months": 12},
        }

    def bundle(self):
        return {
            "as_of": AS_OF,
            "issues": [],
            "sources": [],
            "securities": securities(security("HELD", owned=True)),
        }

    def acquire(self, *, rows=5, should_stop=None, profile=None):
        from portfolio_research.enrichment import acquire_candidates, fetch_controls

        asked = []

        def security_profile(config, security_row, **kwargs):
            asked.append(str(security_row.get("security_id")))
            return (
                profile(security_row)
                if profile
                else {
                    "domicile": "United States",
                    "sector": "Technology",
                    "instrument_type": "equity",
                    "name": str(security_row.get("security_id")),
                }
            )

        bundle = self.bundle()
        with (
            patch("yfinance.screen", return_value=screen_payload(rows)),
            patch("portfolio_research.market_data.security_profile", security_profile),
        ):
            with fetch_controls(should_stop=should_stop):
                acquire_candidates(bundle, self.config, AS_OF, refresh=True)
        return bundle, asked

    def reasons(self, bundle):
        return {row["symbol"]: row["reasons"] for row in bundle["exclusions"]}

    def test_the_enrichment_limit_bounds_the_identity_stage_too(self):
        self.config["data"]["candidate_enrichment_limit"] = 0
        bundle, asked = self.acquire(rows=5)
        self.assertEqual(asked, [])
        for symbol, reasons in self.reasons(bundle).items():
            self.assertEqual(reasons, ["identity_limit_reached"], symbol)
        self.assertNotIn("us_domicile_unverified", bundle["universe"]["counts"]["by_reason"])

    def test_a_stopped_fetch_never_calls_the_tail_a_foreign_issuer(self):
        bundle, asked = self.acquire(rows=5, should_stop=lambda: True)
        self.assertEqual(asked, [])
        for symbol, reasons in self.reasons(bundle).items():
            self.assertEqual(reasons, ["identity_not_attempted"], symbol)
        self.assertIn("UNIVERSE_DISCOVERY_INTERRUPTED", {i["code"] for i in bundle["issues"]})

    def test_a_stopped_fetch_says_so_in_the_coverage_it_publishes(self):
        from portfolio_research.enrichment import _record_universe

        bundle, _ = self.acquire(rows=5, should_stop=lambda: True)
        _record_universe(
            bundle, in_scope=0, enriched=[], not_enriched=set(), exhausted=False, stopped=False
        )
        coverage = bundle["universe"]["coverage"]
        self.assertTrue(coverage["stopped"])

    def test_a_resolved_fetch_still_qualifies_every_acquired_row(self):
        bundle, asked = self.acquire(rows=5)
        self.assertEqual(len(asked), 5)
        self.assertEqual(bundle["universe"]["counts"]["in_scope"], 5)

    def test_one_fetch_starts_one_provider_time_budget(self):
        from portfolio_research.enrichment import _budget, fetch_controls

        with fetch_controls():
            first, second = _budget(self.config), _budget(self.config)
        self.assertEqual(first, second)
        outside_first, outside_second = _budget(self.config), _budget(self.config)
        self.assertNotEqual(outside_first, outside_second)


class ScopeLabelTests(unittest.TestCase):
    """The one human sentence about scope never overstates what was researched."""

    def record(self, *, enriched, in_scope, **flags):
        from portfolio_research.enrichment import _record_universe

        bundle = {
            "universe": {
                "scope_label": f"Largest {in_scope} eligible US ordinary common issuers by "
                f"Yahoo intraday market cap on {AS_OF}",
                "counts": {"acquired": in_scope, "qualified": in_scope, "in_scope": in_scope},
            }
        }
        _record_universe(
            bundle,
            in_scope=in_scope,
            enriched=[f"C{index}" for index in range(enriched)],
            not_enriched={f"C{index}" for index in range(enriched, in_scope)},
            **{"exhausted": False, "stopped": False, **flags},
        )
        return bundle["universe"]

    def test_the_label_states_how_much_of_the_scope_was_enriched(self):
        universe = self.record(enriched=12, in_scope=1000)
        self.assertTrue(universe["scope_label"].endswith("; 12 of 1000 enriched"))

    def test_an_interrupted_discovery_says_so_instead_of_claiming_a_screen(self):
        universe = self.record(enriched=0, in_scope=0, stopped=True)
        self.assertIn("discovery interrupted", universe["scope_label"])

    def test_an_exhausted_budget_says_so(self):
        universe = self.record(enriched=3, in_scope=1000, exhausted=True)
        self.assertIn("discovery interrupted", universe["scope_label"])

    def test_the_readiness_scope_line_quotes_the_label_the_review_used(self):
        from portfolio_research.readiness import _candidates

        result = {
            "signals": [{"security_id": "C1", "data_status": "complete", "eligible": True}],
            "metadata": {"scope": "configured_universe"},
            "universe": {"scope_label": "Largest 1000 issuers on 2026-09-18; 12 of 1000 enriched"},
        }
        row = _candidates(result, [])
        self.assertIn("12 of 1000 enriched", row["scope"])
        self.assertNotIn("configured_universe", row["scope"])


if __name__ == "__main__":
    unittest.main()
