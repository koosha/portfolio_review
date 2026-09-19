"""Briefs and proposals assembled from one review's adapter records.

The review's brief and its EPS and DCF assumption sets are built while the adapter's own
units are still in hand: prices in the listing's quote currency and statements in the
company's reporting currency, before the price frame is presented in the mandate
currency. Nothing here is an established assumption — each payload is proposed, carries
its evidence and stays out of the owner's reviewed workspace until it is adopted.

Changes since the previous review are written only when the caller supplies
``bundle["previous_research"]``: ``{security_id: {"as_of", "close", "currency",
"source_id", "estimates"}}`` taken from the last published review. Without it the brief
simply omits those bullets rather than comparing against an unstated baseline.
"""

from __future__ import annotations

import pandas as pd


def _dividends(record, as_of, reasons):
    """Trailing twelve-month cash dividends per current share, split adjusted.

    The window ends at the review date, never at the last payment: anchoring it on the
    latest dividend would give an issuer that stopped paying years ago a full year of
    distributions. An empty window is missing, not zero, and says when the payments
    stopped.
    """
    from .statements import split_adjust_dividends

    actions = (record.get("prices") or {}).get("actions") or []
    dividends = [row for row in actions if row.get("kind") == "dividend"]
    splits = [row for row in actions if row.get("kind") == "split"]
    if not dividends or not as_of:
        return None
    end = pd.Timestamp(as_of).date().isoformat()
    start = (pd.Timestamp(end) - pd.DateOffset(years=1)).date().isoformat()
    adjusted = split_adjust_dividends(dividends, splits)
    trailing = [row for row in adjusted if start < str(row.get("date") or "") <= end]
    values = [row.get("value") for row in trailing if isinstance(row.get("value"), (int, float))]
    if values:
        return float(sum(values))
    latest = max((str(row.get("date") or "") for row in adjusted), default="")
    if latest and isinstance(reasons, list):
        reasons.append(
            {
                "code": "dividends_ttm_unavailable",
                "severity": "info",
                "security_id": (record.get("security") or {}).get("security_id"),
                "detail": (
                    f"the last dividend was paid on {latest}, outside the twelve months "
                    f"to {end}; trailing distributions are unavailable"
                ),
            }
        )
    return None


def _prior_ttm(record):
    """The trailing window one year earlier, for growth in the brief."""
    from .statements import ttm_from_quarters

    quarters = (record.get("statements") or {}).get("quarterly") or []
    return ttm_from_quarters(quarters[4:8]) if len(quarters) >= 8 else None


def _latest_price(record):
    rows = (record.get("prices") or {}).get("prices") or []
    return max(rows, key=lambda row: str(row.get("date") or "")) if rows else None


def _fx_note(record, config):
    presentation = (config.get("mandate") or {}).get("base_currency")
    currency = (record.get("prices") or {}).get("currency")
    if not presentation or not currency or currency == presentation:
        return None
    return (
        f"Amounts are in {currency}, the listing's own quote currency; the {presentation} "
        "presentation applies each row's own dated observation."
    )


def _proposals(record, config, ttm, as_of):
    """The EPS and DCF payloads a reviewer may adopt, with the reasons for refusals."""
    from .proposals import (
        DEFAULT_DISCOUNT_RATE,
        DEFAULT_PROJECTION_YEARS,
        DEFAULT_TERMINAL_GROWTH,
        propose_dcf_inputs,
        propose_eps_model,
    )

    latest = _latest_price(record) or {}
    statements = record.get("statements") or {}
    security = {
        **record["security"],
        "price_source_id": latest.get("source_id"),
        "equity_type": (record.get("profile") or {}).get("equity_type")
        or record["security"].get("equity_type"),
        "sector": (record.get("profile") or {}).get("sector") or record["security"].get("sector"),
    }
    reasons: list[dict] = []
    eps = propose_eps_model(
        security,
        price_major=latest.get("close"),
        quote_currency=(record.get("prices") or {}).get("currency"),
        estimates=record.get("estimates"),
        ttm=ttm,
        dividends_ttm_per_share=_dividends(record, as_of, reasons),
        horizon_months=config["allocation"]["horizon_months"],
        issues=reasons,
    )
    dcf = propose_dcf_inputs(
        security,
        ttm=ttm,
        annual_statements=statements.get("annual"),
        shares_outstanding=(record.get("profile") or {}).get("shares_outstanding"),
        price_major=latest.get("close"),
        currency=statements.get("currency"),
        quote_currency=(record.get("prices") or {}).get("currency"),
        estimates=record.get("estimates"),
        defaults={
            "discount_rate": DEFAULT_DISCOUNT_RATE,
            "terminal_growth_rate": DEFAULT_TERMINAL_GROWTH,
            "projection_years": DEFAULT_PROJECTION_YEARS,
        },
        issues=reasons,
    )
    return {"eps": eps, "dcf": dcf, "reasons": reasons}


def _guarded(name, sid, issues, build, default=None):
    """Build one part of a security's research; a defect costs that part alone."""
    from portfolio_lab.providers import _error_label

    from .market_values import _issue

    try:
        return build()
    except Exception as exc:
        _issue(issues, "RESEARCH_BUILD_FAILED", sid, f"{name}: {_error_label(exc)}")
        return default


def build_research(records: dict, bundle: dict, config: dict, as_of: str, brief_ids=None) -> dict:
    """A dated brief and reviewable proposals for the securities a reader will read.

    ``brief_ids`` names them — the holdings and the ``signals.candidate_brief_limit``
    largest candidates. A screened candidate past that bound reports its coverage and
    nothing else: a brief nobody asked for costs roughly twelve kilobytes in every saved
    run, and the prioritisation and signals read only the coverage. ``None`` means every
    enriched security, which is what a caller without a bound wants.
    """
    from .briefs import build_brief

    previous = bundle.get("previous_research") or {}
    generated_at = (bundle.get("timeline") or {}).get("generated_at")
    issues = bundle.setdefault("issues", [])
    wanted = None if brief_ids is None else {str(sid) for sid in brief_ids}
    research = {}
    for sid, record in records.items():
        if wanted is not None and str(sid) not in wanted:
            research[sid] = {"brief": None, "proposals": None, "coverage": dict(record["coverage"])}
            continue
        statements = record.get("statements") or {}
        ttm = statements.get("ttm")
        if isinstance(ttm, dict):
            ttm = {**ttm, "prior": _prior_ttm(record), "currency": statements.get("currency")}
        research[sid] = {
            "brief": _guarded(
                "brief",
                sid,
                issues,
                lambda record=record, ttm=ttm: build_brief(
                    {
                        **record["security"],
                        "name": (record.get("profile") or {}).get("name")
                        or record["security"].get("name"),
                    },
                    prices=(record.get("prices") or {}).get("prices") or [],
                    estimates=record.get("estimates"),
                    ttm=ttm,
                    events=(record.get("events") or {}).get("events") or [],
                    previous=previous.get(sid),
                    fx_note=_fx_note(record, config),
                    as_of=as_of,
                    generated_at=generated_at,
                    horizon_months=config["allocation"]["horizon_months"],
                ),
            ),
            "proposals": _guarded(
                "proposals",
                sid,
                issues,
                lambda record=record, ttm=ttm: _proposals(record, config, ttm, as_of),
                default={"eps": None, "dcf": None, "reasons": []},
            ),
            "coverage": dict(record["coverage"]),
        }
    return research
