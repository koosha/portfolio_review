import hashlib
import json
import unittest
from copy import deepcopy

from portfolio_research.evidence import revise_assessment, validate_assessment


def assessment():
    return {
        "version": 1,
        "security_id": "synthetic:issuer:common",
        "author": "Example analyst",
        "horizon_months": 12,
        "decision_cutoff": "2026-08-31T16:00:00-04:00",
        "generated_at": "2026-08-31T21:00:00Z",
        "review_status": "reviewed",
        "reviewed_by": "Example reviewer",
        "reviewed_at": "2026-08-31T20:30:00Z",
        "market_expectations": "Synthetic starting multiple reflects steady growth.",
        "thesis": "Example reinvestment can sustain operations.",
        "counter_thesis": "Example operating margin may contract.",
        "catalysts": [{"description": "Synthetic product release", "target_date": "2027-03-01"}],
        "balance_sheet_risks": "Debt maturity requires review.",
        "invalidation_conditions": "Two quarters below the assumed margin.",
        "sources": [
            {
                "id": "report-v1",
                "locator": "https://example.com/synthetic-report",
                "version": "original",
                "content_hash": hashlib.sha256(b"synthetic evidence").hexdigest(),
                "published_at": "2026-08-15T12:00:00Z",
                "received_at": "2026-08-16T12:00:00Z",
            }
        ],
        "facts": [
            {
                "id": "revenue",
                "field": "revenue",
                "value": 500,
                "units": "USD millions",
                "source_id": "report-v1",
                "origin": "manual",
                "review_status": "reviewed",
                "reviewed_by": "Example reviewer",
                "reviewed_at": "2026-08-30T12:00:00Z",
            }
        ],
        "operating_assumptions": [
            {
                "field": "forward_eps",
                "value": 5.5,
                "units": "USD/share",
                "horizon_months": 12,
                "basis": "Manual hypothetical operating scenario",
                "author": "Example analyst",
                "version": 1,
                "origin": "manual",
            }
        ],
    }


class EvidenceTests(unittest.TestCase):
    def test_reviewed_manual_evidence_is_usable_but_not_calibrated(self):
        result = validate_assessment(assessment())
        self.assertEqual(result["status"], "ready")
        self.assertTrue(result["can_supply_scenarios"])
        self.assertEqual(len(result["eligible_facts"]), 1)
        self.assertFalse(result["calibrated"])
        self.assertEqual(result["forecast_type"], "subjective_assessment")

    def test_after_cutoff_publication_is_excluded_even_with_earlier_receipt(self):
        data = assessment()
        data["sources"][0]["published_at"] = "2026-08-31T20:01:00Z"
        result = validate_assessment(data)
        self.assertFalse(result["can_supply_scenarios"])
        self.assertEqual(result["eligible_facts"], [])
        self.assertTrue(any("published after" in reason for reason in result["issues"]))

    def test_strict_receipt_cutoff_and_public_only_replay_are_distinct(self):
        data = assessment()
        data["sources"][0]["received_at"] = "2026-09-01T12:00:00Z"
        self.assertEqual(validate_assessment(data)["eligible_facts"], [])
        public_only = validate_assessment(data, strict_receipt=False)
        self.assertEqual(len(public_only["eligible_facts"]), 1)
        self.assertEqual(public_only["timing_policy"], "public_availability_only")
        self.assertNotEqual(
            public_only["assessment_id"], validate_assessment(data)["assessment_id"]
        )

    def test_date_only_and_timezone_missing_never_establish_intraday_availability(self):
        for stamp in ("2026-08-15", "2026-08-15T12:00:00", None):
            data = assessment()
            data["sources"][0]["published_at"] = stamp
            self.assertEqual(validate_assessment(data)["eligible_facts"], [], stamp)

    def test_original_and_later_restatement_remain_separate(self):
        data = assessment()
        revised = deepcopy(data["sources"][0])
        revised.update(id="report-v2", version="restated", published_at="2026-09-15T12:00:00Z")
        data["sources"].append(revised)
        later_fact = deepcopy(data["facts"][0])
        later_fact.update(id="restated-revenue", value=600, source_id="report-v2")
        data["facts"].append(later_fact)
        result = validate_assessment(data)
        self.assertEqual([fact["value"] for fact in result["eligible_facts"]], [500])
        self.assertFalse(result["can_supply_scenarios"])

    def test_unreviewed_extraction_and_unknown_source_never_unlock(self):
        for origin in ("extracted", "llm"):
            data = assessment()
            data["facts"][0].update(origin=origin, review_status="draft", eligible=True)
            result = validate_assessment(data)
            self.assertFalse(result["facts"][0]["eligible"])
            self.assertFalse(result["can_supply_scenarios"])
        data = assessment()
        data["facts"][0]["source_id"] = "not-retained"
        self.assertFalse(validate_assessment(data)["facts"][0]["eligible"])

    def test_extracted_assumption_requires_source_and_review(self):
        data = assessment()
        data["operating_assumptions"][0].update(origin="llm", calibrated=True)
        result = validate_assessment(data)
        self.assertFalse(result["operating_assumptions"][0]["eligible"])
        self.assertFalse(result["operating_assumptions"][0]["calibrated"])
        self.assertFalse(result["can_supply_scenarios"])

    def test_arbitrary_calibration_id_never_creates_validated_expected_returns(self):
        data = assessment()
        data.update(calibrated=True, calibration_id="trust-me", calibration_record={"passed": True})
        result = validate_assessment(data)
        self.assertFalse(result["calibrated"])
        self.assertTrue(result["calibration_issues"])
        self.assertEqual(result["forecast_type"], "subjective_assessment")

    def test_missing_provenance_and_operating_basis_stay_blocked(self):
        for field in ("content_hash", "locator", "version"):
            data = assessment()
            del data["sources"][0][field]
            self.assertEqual(validate_assessment(data)["eligible_facts"], [])
        data = assessment()
        del data["operating_assumptions"][0]["basis"]
        self.assertFalse(validate_assessment(data)["can_supply_scenarios"])

    def test_horizon_mismatch_or_out_of_horizon_catalyst_requires_review(self):
        data = assessment()
        data["operating_assumptions"][0]["horizon_months"] = 18
        self.assertFalse(validate_assessment(data)["can_supply_scenarios"])
        data = assessment()
        data["catalysts"][0]["target_date"] = "2028-01-01"
        self.assertTrue(any("outside" in issue for issue in validate_assessment(data)["issues"]))

    def test_revision_is_new_draft_and_leaves_old_version_immutable(self):
        old = assessment()
        original = deepcopy(old)
        new = revise_assessment(old, {"thesis": "Different assumption", "calibrated": True})
        self.assertEqual(old, original)
        self.assertEqual(new["version"], 2)
        self.assertEqual(new["parent_assessment_id"], validate_assessment(old)["assessment_id"])
        self.assertFalse(new["calibrated"])
        self.assertNotIn("reviewed_at", new)
        self.assertFalse(validate_assessment(new)["can_supply_scenarios"])

    def test_timestamps_versions_and_nonfinite_values_are_validated(self):
        for field, value in (
            ("version", 0),
            ("version", True),
            ("horizon_months", 9),
            ("decision_cutoff", "not-a-date"),
            ("generated_at", "2026-08-01T00:00:00Z"),
        ):
            data = assessment()
            data[field] = value
            with self.assertRaises(ValueError, msg=field):
                validate_assessment(data)
        data = assessment()
        data["facts"][0]["value"] = float("nan")
        with self.assertRaises(ValueError):
            validate_assessment(data)

    def test_duplicate_source_or_fact_ids_are_rejected(self):
        for name in ("sources", "facts"):
            data = assessment()
            data[name].append(deepcopy(data[name][0]))
            with self.assertRaises(ValueError):
                validate_assessment(data)

    def test_canonical_identity_is_deterministic_and_input_is_not_mutated(self):
        data = assessment()
        original = deepcopy(data)
        first = validate_assessment(data)
        reordered = dict(reversed(list(data.items())))
        self.assertEqual(first["assessment_id"], validate_assessment(reordered)["assessment_id"])
        self.assertEqual(data, original)
        json.dumps(first, allow_nan=False)

    def test_empty_assessment_remains_useful_missing_state(self):
        result = validate_assessment({})
        self.assertEqual(result["status"], "blocked")
        self.assertFalse(result["can_supply_scenarios"])
        self.assertTrue(result["issues"])


if __name__ == "__main__":
    unittest.main()
