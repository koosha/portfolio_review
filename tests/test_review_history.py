"""A recorded monthly decision, and the review the next one is compared against.

Every run here is a minimal archived result written straight to the store, so what is
under test is the decision contract and the run-to-run link, not a provider fixture.
"""

import tempfile
import unittest

from portfolio.storage import Store
from portfolio_research.service import ResearchService, default_config


def archived_run(service, as_of, review_kind, candidates=()):
    """A minimal saved run, so the decision paths need no collection and no provider."""
    result = {
        "metadata": {"as_of": as_of, "review_kind": review_kind},
        "summary": {"complete": True},
        "allocation": {"candidates": [{"candidate": name} for name in candidates]},
    }
    return service.store.save_run(result, service.config, {"as_of": as_of, "workspace": {}})


class DecisionRecordTests(unittest.TestCase):
    """A recorded decision keeps what was chosen, what it was compared against and when."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        Store(self.temp.name)
        self.service = ResearchService(default_config(self.temp.name))
        self.addCleanup(self.service.close)
        self.earlier = archived_run(self.service, "2026-07-31", "current", ("balanced",))
        self.other = archived_run(self.service, "2026-08-15", "historical")
        self.run_id = archived_run(self.service, "2026-08-31", "current", ("balanced", "defensive"))

    def save(self, **fields):
        payload = {"run_id": self.run_id, "rationale": "Recorded after review.", **fields}
        return self.service.save_decision(payload)

    def test_a_decision_keeps_its_alternative_comparison_and_review_context(self):
        self.assertIsNone(self.service.status()["last_decision"])
        saved = self.save(
            action="review_candidate",
            candidate_id="balanced",
            selected_alternative="balanced",
            compared_candidates=["balanced", "defensive"],
            review_kind="current",
            as_of="2026-08-31",
        )
        last = self.service.status()["last_decision"]
        self.assertEqual(last["record_id"], saved["record_id"])
        self.assertEqual(last["run_id"], self.run_id)
        self.assertEqual(last["selected_alternative"], "balanced")
        self.assertEqual(last["compared_candidates"], ["balanced", "defensive"])
        self.assertEqual(last["review_kind"], "current")
        self.assertEqual(last["as_of"], "2026-08-31")
        self.assertEqual(last["rationale"], "Recorded after review.")
        self.assertTrue(last["created_at"])

    def test_a_no_change_decision_takes_its_review_context_from_the_run(self):
        self.save()
        last = self.service.status()["last_decision"]
        self.assertEqual(last["action"], "no_action")
        self.assertIsNone(last["selected_alternative"])
        self.assertEqual(last["compared_candidates"], ["balanced", "defensive"])
        self.assertEqual(last["review_kind"], "current")
        self.assertEqual(last["as_of"], "2026-08-31")
        self.assertIsNone(last["workflow_id"])

    def test_unstated_alternatives_kinds_dates_workflows_and_fields_are_refused(self):
        for fields in (
            {"selected_alternative": "unlisted"},
            {"compared_candidates": ["balanced", "unlisted"]},
            {"compared_candidates": "balanced"},
            {"review_kind": "quarterly"},
            {"as_of": "31-08-2026"},
            {"workflow_id": "no-such-workflow"},
            {"execution": "settled"},
        ):
            with self.subTest(**fields), self.assertRaises(ValueError):
                self.save(**fields)
        with self.assertRaises(ValueError):
            self.service.save_decision("a decision is an object")
        self.assertEqual(self.service.store.records("decision"), [])

    def test_the_client_payload_takes_unstated_kind_and_date_from_the_run(self):
        """The dashboard states every documented field; an unstated one is not a refusal.

        The client always sends the whole record, writing ``None`` where the run itself
        has to answer, so the documented run-metadata fallback has to read an explicit
        ``None`` exactly as it reads an absent key.
        """
        self.service.save_decision(
            {
                "run_id": self.run_id,
                "action": "no_action",
                "candidate_id": None,
                "selected_alternative": None,
                "rationale": "Nothing merits a trade.",
                "review_kind": None,
                "workflow_id": None,
                "compared_candidates": ["balanced", "defensive"],
                "as_of": None,
            }
        )
        last = self.service.status()["last_decision"]
        self.assertEqual(last["review_kind"], "current")
        self.assertEqual(last["as_of"], "2026-08-31")
        self.assertEqual(last["action"], "no_action")

    def test_a_run_names_the_previous_review_of_the_same_kind(self):
        self.assertEqual(self.service.run(self.run_id)["previous_run_id"], self.earlier)
        self.assertIsNone(self.service.run(self.other)["previous_run_id"])
        self.assertIsNone(self.service.run(self.earlier)["previous_run_id"])

    def test_a_review_backfilled_out_of_order_compares_by_review_date(self):
        """Saving an older month later must not make it the successor of a newer one."""
        backfilled = archived_run(self.service, "2026-06-30", "current")
        self.assertIsNone(self.service.run(backfilled)["previous_run_id"])
        self.assertEqual(self.service.run(self.earlier)["previous_run_id"], backfilled)
        self.assertEqual(self.service.run(self.run_id)["previous_run_id"], self.earlier)
        later = archived_run(self.service, "2026-09-30", "current")
        self.assertEqual(self.service.run(later)["previous_run_id"], self.run_id)
        # A same-dated review of the same kind is separated by when it was saved.
        twin = archived_run(self.service, "2026-09-30", "current")
        self.assertEqual(self.service.run(twin)["previous_run_id"], later)


if __name__ == "__main__":
    unittest.main()
