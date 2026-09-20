"""What one recorded monthly decision may state, and what makes it rereadable later.

A decision is the one thing in a review that is not a calculation: it is the owner's
choice, and the next review explains what changed by rereading it. So the record names
the alternatives the choice was weighed against, the review it belongs to and the day it
was taken, and a decision naming anything the saved run does not contain is refused
rather than stored in a form nobody can reconstruct.
"""

import re
from datetime import date

from .calendar import DATE_ONLY, REVIEW_KINDS
from .public import _clean

DECISION_ACTIONS = {"no_action", "review_candidate", "override"}
# Everything a recorded decision may state. An unlisted field is refused rather than
# silently dropped: a caller that believed it recorded something must hear otherwise.
DECISION_FIELDS = {
    "run_id",
    "parent_id",
    "action",
    "candidate_id",
    "rationale",
    "selected_alternative",
    "compared_candidates",
    "review_kind",
    "workflow_id",
    "as_of",
}
RATIONALE_LIMIT = 10000


def decision_date(payload, metadata):
    """The day the decision was taken, as a plain calendar date.

    A caller that sends the field unstated is asking the run it names to supply it, so an
    explicit ``None`` falls back to the run's own date exactly as an absent key does.
    """
    as_of = payload.get("as_of") or metadata.get("as_of")
    if not isinstance(as_of, str) or not re.fullmatch(DATE_ONLY, as_of):
        raise ValueError("Record the decision date as YYYY-MM-DD.")
    try:
        date.fromisoformat(as_of)
    except ValueError as exc:
        raise ValueError("Record the decision date as YYYY-MM-DD.") from exc
    return as_of


def decision_candidates(payload, result):
    """``(chosen, alternative, compared)`` once every one of them belongs to this run.

    An unstated comparison defaults to the alternatives the run itself produced, which
    is what the reader saw: the choice was made against all of them.
    """
    candidates = [
        row.get("candidate")
        for row in (result.get("allocation") or {}).get("candidates", [])
        if isinstance(row, dict) and row.get("candidate") is not None
    ]
    chosen, alternative = payload.get("candidate_id"), payload.get("selected_alternative")
    if not {value for value in (chosen, alternative) if value is not None} <= set(candidates):
        raise ValueError("Choose a candidate belonging to this saved run.")
    compared = payload.get("compared_candidates", candidates)
    if not isinstance(compared, list) or not set(compared) <= set(candidates):
        raise ValueError("Compare only candidates belonging to this saved run.")
    return chosen, alternative, list(compared)


def decision_fields(payload, result, default_review_kind):
    """One validated decision record, refused outright when any part of it is unstated."""
    if set(payload) - DECISION_FIELDS:
        raise ValueError("Send only the documented decision fields.")
    metadata = result.get("metadata") or {}
    chosen, alternative, compared = decision_candidates(payload, result)
    rationale = payload.get("rationale")
    if not isinstance(rationale, str) or not rationale.strip() or len(rationale) > RATIONALE_LIMIT:
        raise ValueError("Record a decision or no-action rationale.")
    action = payload.get("action", "no_action")
    if action not in DECISION_ACTIONS:
        raise ValueError("Unsupported research decision action.")
    # An unstated review kind is answered by the run the decision belongs to, whether the
    # caller omitted the field or sent it empty.
    review_kind = payload.get("review_kind") or metadata.get("review_kind") or default_review_kind
    if review_kind not in REVIEW_KINDS:
        raise ValueError("Review kind must be " + " or ".join(REVIEW_KINDS) + ".")
    return {
        "action": action,
        "candidate_id": chosen,
        "selected_alternative": alternative,
        "compared_candidates": compared,
        "review_kind": review_kind,
        "workflow_id": payload.get("workflow_id"),
        "as_of": decision_date(payload, metadata),
        "rationale": rationale.strip(),
        "execution": "none",
    }


def last_decision(records):
    """The newest recorded decision as the dashboard reads it, or ``None``."""
    if not records:
        return None
    newest = records[0]
    return _clean(
        {
            "record_id": newest["record_id"],
            "created_at": newest["created_at"],
            "run_id": newest["run_id"],
            **newest["payload"],
        }
    )
