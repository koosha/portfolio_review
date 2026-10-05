"""What one recorded monthly decision may state, and what makes it rereadable later.

A decision is the one thing in a review that is not a calculation: it is the owner's
choice, and the next review explains what changed by rereading it. So the record names
the alternatives the choice was weighed against, the review it belongs to and the day it
was taken, and a decision naming anything the saved run does not contain is refused
rather than stored in a form nobody can reconstruct.
"""

import re
from copy import deepcopy
from datetime import date

from .calendar import DATE_ONLY, REVIEW_KINDS
from .public import _clean

DECISION_ACTIONS = {"no_action", "review_candidate", "override", "follow_recommendations"}
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
    "recommendations",
}
RATIONALE_LIMIT = 10000
RECOMMENDATION_ACTIONS = {"buy", "sell", "hold"}
RECOMMENDATION_ROW_FIELDS = {"security_id", "action", "buy_share", "sell_fraction", "note"}
NOTE_LIMIT = 1000
SHARE_TOLERANCE = 1e-6


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


def _fraction(value, name, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number between 0 and 1.")
    if not 0 <= value <= 1 or (positive and value == 0):
        raise ValueError(
            f"{name} must be {'above 0 and at most' if positive else 'between 0 and'} 1."
        )
    return float(value)


def decision_recommendations(payload, result):
    """The per-position decision the owner accepted, checked against the run's table.

    Rows the owner did not send keep the run's own recommendation. Each stored row says
    what the run recommended beside what was decided, so an override stays visible.
    """
    recommended = {
        str(row["security_id"]): row
        for row in ((result.get("recommendations") or {}).get("rows") or [])
        if isinstance(row, dict) and row.get("security_id") is not None
    }
    if not recommended:
        raise ValueError("This saved run has no recommendations to follow.")
    sent = payload.get("recommendations")
    if not isinstance(sent, dict) or set(sent) - {"rows"}:
        raise ValueError("Send the recommendations decision as an object with rows.")
    rows = sent.get("rows", [])
    if not isinstance(rows, list) or len(rows) > len(recommended):
        raise ValueError("Send at most one decided row per recommended security.")
    decided = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) - RECOMMENDATION_ROW_FIELDS:
            raise ValueError("Send only the documented recommendation row fields.")
        sid = row.get("security_id")
        if sid not in recommended or sid in decided:
            raise ValueError("Decide each security this run recommended, once.")
        decided[sid] = row
    stored, share_sum = [], 0.0
    for sid in sorted(recommended):
        original = recommended[sid]
        row = decided.get(sid, original)
        action = row.get("action")
        if action not in RECOMMENDATION_ACTIONS:
            raise ValueError("A recommendation action must be buy, sell or hold.")
        buy_share = row.get("buy_share") if action == "buy" else None
        sell_fraction = row.get("sell_fraction") if action == "sell" else None
        if action == "buy":
            buy_share = _fraction(buy_share, f"{sid} buy share", positive=True)
            share_sum += buy_share
        if action == "sell":
            sell_fraction = _fraction(sell_fraction, f"{sid} sell fraction", positive=True)
        note = row.get("note") if sid in decided else None
        if note is not None and (not isinstance(note, str) or len(note) > NOTE_LIMIT):
            raise ValueError(f"A row note is text of at most {NOTE_LIMIT} characters.")
        overridden = (action, buy_share, sell_fraction) != (
            original.get("action"),
            original.get("buy_share") if original.get("action") == "buy" else None,
            original.get("sell_fraction") if original.get("action") == "sell" else None,
        )
        stored.append(
            {
                "security_id": sid,
                "action": action,
                "buy_share": buy_share,
                "sell_fraction": sell_fraction,
                "recommended_action": original.get("action"),
                "overridden": overridden,
                "note": note.strip() if isinstance(note, str) and note.strip() else None,
            }
        )
    if share_sum and abs(share_sum - 1) > SHARE_TOLERANCE:
        raise ValueError("Decided buy shares must add up to 100% of available cash.")
    return {
        "parameters": deepcopy((result.get("recommendations") or {}).get("parameters") or {}),
        "buy_share_sum": round(share_sum, 6),
        "overrides": sum(row["overridden"] for row in stored),
        "rows": stored,
    }


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
    if action == "no_action" and (chosen is not None or alternative is not None):
        raise ValueError("A no-change decision names no alternative.")
    recommendations = None
    if action == "follow_recommendations":
        recommendations = decision_recommendations(payload, result)
    elif payload.get("recommendations") is not None:
        raise ValueError("Send recommendations only with a follow_recommendations decision.")
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
        **({"recommendations": recommendations} if recommendations is not None else {}),
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
