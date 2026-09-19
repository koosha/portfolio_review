"""The research build writes a brief only where the review will read one.

``candidate_brief_limit`` bounds the brief that is built, not merely the events fetch
behind it, so the securities the fetch plan names are the securities that get a brief
and the rest still report their own coverage. No network: the brief writer is a local
fake and every record is an explicit dict.
"""

import unittest
from unittest.mock import patch

from tests.support.securities import securities, security

AS_OF = "2026-09-18"


class BriefLimitTests(unittest.TestCase):
    """`candidate_brief_limit` bounds the brief that is built, not only the events fetch."""

    def records(self, *ids):
        return {
            sid: {
                "security": {"security_id": sid, "ticker": sid, "currency": "USD"},
                "profile": {"name": f"{sid} Inc"},
                "prices": {"prices": [], "currency": "USD"},
                "statements": {},
                "estimates": None,
                "events": {"events": []},
                "coverage": {"prices": "ok", "statements": "missing"},
                "sources": [],
            }
            for sid in ids
        }

    def build(self, brief_ids):
        from portfolio_research.research_build import build_research

        asked = []

        def fake_brief(security, **kwargs):
            asked.append(str(security["security_id"]))
            return {
                "security_id": security["security_id"],
                "name": security.get("name"),
                "facts": [],
                "invalidation_conditions": [],
            }

        bundle = {"issues": [], "timeline": {"generated_at": f"{AS_OF}T20:00:00Z"}}
        config = {"allocation": {"horizon_months": 12}}
        with patch("portfolio_research.briefs.build_brief", fake_brief):
            research = build_research(
                self.records("A", "B"), bundle, config, AS_OF, brief_ids=brief_ids
            )
        return research, asked

    def test_only_the_named_securities_get_a_brief(self):
        research, asked = self.build({"A"})
        self.assertEqual(asked, ["A"])
        self.assertIsNotNone(research["A"]["brief"])
        self.assertIsNone(research["B"]["brief"])
        self.assertIsNone(research["B"]["proposals"])

    def test_every_security_still_reports_its_coverage(self):
        research, _ = self.build({"A"})
        for sid in ("A", "B"):
            self.assertEqual(research[sid]["coverage"], {"prices": "ok", "statements": "missing"})

    def test_no_named_set_means_every_enriched_security(self):
        research, asked = self.build(None)
        self.assertEqual(sorted(asked), ["A", "B"])
        self.assertIsNotNone(research["B"]["brief"])

    def test_the_fetch_plan_names_the_holdings_and_the_brief_limit(self):
        from portfolio_lab.config import DEFAULTS
        from portfolio_research.enrichment import _plan

        frame = securities(
            security("HELD", owned=True),
            security("C1", candidate=True, market_cap=900.0),
            security("C2", candidate=True, market_cap=800.0),
            security("C3", candidate=True, market_cap=700.0),
        )
        config = {
            "signals": {**DEFAULTS["signals"], "candidate_brief_limit": 2, "watchlist": []},
            "data": {"candidate_enrichment_limit": 1000},
        }
        plan = _plan(frame, config)
        self.assertEqual(plan["with_events"], {"C1", "C2"})
        self.assertEqual(plan["with_briefs"], {"HELD", "C1", "C2"})


if __name__ == "__main__":
    unittest.main()
