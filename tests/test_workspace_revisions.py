import unittest
from copy import deepcopy

from portfolio_research.application import validate_workspace, validate_workspace_revision


class WorkspaceRevisionTests(unittest.TestCase):
    def test_changed_assessment_requires_new_version_and_new_review(self):
        previous = {
            "assessments": {
                "DEMO": {
                    "security_id": "DEMO",
                    "version": 1,
                    "review_status": "reviewed",
                    "reviewed_at": "2026-08-30T20:00:00Z",
                    "thesis": "Original",
                }
            }
        }
        edited = deepcopy(previous)
        edited["assessments"]["DEMO"]["thesis"] = "Revised"
        with self.assertRaisesRegex(ValueError, "new version"):
            validate_workspace_revision(edited, previous)
        edited["assessments"]["DEMO"]["version"] = 2
        with self.assertRaisesRegex(ValueError, "review timestamp"):
            validate_workspace_revision(edited, previous)
        edited["assessments"]["DEMO"]["review_status"] = "needs_review"
        edited["assessments"]["DEMO"]["reviewed_at"] = None
        validate_workspace_revision(edited, previous)
        validate_workspace_revision(previous, previous)

    def test_company_identity_cannot_be_substituted_by_another_assessment(self):
        with self.assertRaisesRegex(ValueError, "workspace identity"):
            validate_workspace({"assessments": {"DEMO": {"security_id": "OTHER"}}})
