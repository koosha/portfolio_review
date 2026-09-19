"""Automatic company and portfolio research for the securities a review actually holds.

``enrich_market`` is the connector the input pipeline calls: for every security with an
exact provider listing it asks the versioned adapter for prices, statements, consensus
estimates, fund disclosures, events and a profile, merges those records into the
bundle's frames and writes a per-security coverage report. Nothing here invents data —
a provider that does not answer leaves an issue and a ``missing`` coverage status
confined to the security and the capability it happened to, and every other output
carries on.

Briefs and proposals are assembled from the same records while the adapter's own units
are still in hand (quote currency, statement currency), before the price frame is
presented in the mandate currency. They are proposals: every figure carries its source
and nothing is written into the owner's reviewed workspace.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

import pandas as pd

CAPABILITIES = (
    "prices",
    "statements",
    "estimates",
    "fund_disclosures",
    "events",
    "fx",
    "listings",
)
# The capabilities counted per security; FX and listing coverage come from the ledger.
SECURITY_CAPABILITIES = ("prices", "statements", "estimates", "fund_disclosures", "events")
FRAMES = ("prices", "fundamentals", "fund_holdings", "events", "fund_sectors")
FUND_TYPES = {"etf", "mutual_fund", "fund"}
UNSUPPORTED = {"plan_fund", "currency", "cash", "index"}
LIMITATIONS = (
    "Yahoo adjusted prices can be revised; not a survivorship-free historical universe",
    "Statement publication dates assumed when no filing date is available",
    "Fund disclosures are undated provider snapshots limited to top holdings",
)
# Flows a valuation reads that the trailing row does not already accumulate.
TRAILING_FLOWS = (
    "depreciation",
    "change_working_capital",
    "tax_provision",
    "pretax_income",
    "stock_compensation",
)
PROFILE_FIELDS = (
    "sector",
    "industry",
    "market_cap",
    "market_cap_as_of",
    "market_cap_received_at",
    "shares_outstanding",
    "domicile",
    "equity_type",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _clean(value):
    """One securities-frame value as a plain Python value, with missing as ``None``."""
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    if isinstance(value, (str, bool, int, float)):
        return value
    try:
        return None if pd.isna(value) else value
    except (TypeError, ValueError):
        return value


def _security(row) -> dict:
    return {key: _clean(value) for key, value in dict(row).items()}


def _is_fund(security) -> bool:
    return str(security.get("instrument_type") or "").lower() in FUND_TYPES


def _supported(security) -> bool:
    """A security this connector may ask a provider about."""
    from .market_data import listing_symbol

    kind = str(security.get("instrument_type") or "").lower()
    if kind in UNSUPPORTED:
        return False
    if str(security.get("resolution_status") or "").lower() in {"unresolved", "conflict"}:
        return False
    return listing_symbol(security) is not None


def _source(record, provider, rows) -> dict | None:
    """The receipt for one archived provider answer, in the bundle's source shape."""
    if not isinstance(record, dict) or not record.get("source_id"):
        return None
    source_id = str(record["source_id"])
    return {
        "source_id": source_id,
        "provider": provider,
        "sha256": source_id.split(":", 1)[1] if ":" in source_id else None,
        "received_at": record.get("received_at"),
        "available_at": record.get("received_at"),
        "rows": int(rows),
        "status": "ok",
        "adapter_version": record.get("adapter_version"),
    }


def _status(record, config, *, present=True) -> str:
    """``ok`` for a fresh answer, ``stale`` for one older than the refresh window."""
    from .market_data import _fresh

    if record is None or not present:
        return "missing"
    return "ok" if _fresh(record.get("received_at"), config) else "stale"


def _attempt(name, sid, log, call, /, *args, **kwargs):
    """One capability for one security; a raising helper costs that capability only.

    The adapter records its own provider failures as issues, so anything that reaches
    here is a defect in a pure helper — a malformed provider value driving a ValueError,
    say. Isolation is structural: it must not depend on every helper being careful.
    """
    from portfolio_lab.providers import _error_label

    from .market_values import _issue

    try:
        return call(*args, **kwargs)
    except Exception as exc:
        _issue(log, "RESEARCH_CAPABILITY_FAILED", sid, f"{name}: {_error_label(exc)}")
        return None


def _issuer_lookup(securities):
    """``meta -> issuer_id`` for the issuers this portfolio already holds directly.

    A fund's look-through row only aggregates with a directly held position when both
    name the same issuer; without this every holding stays ``listing:SYM`` and one
    issuer is counted twice.
    """
    from .market_listings import normalize_symbol
    from .market_values import listing_symbol

    index: dict[str, str] = {}
    for security in securities:
        issuer_id = security.get("issuer_id")
        symbol = listing_symbol(security)
        if isinstance(issuer_id, str) and issuer_id.strip() and symbol:
            index.setdefault(symbol.upper(), issuer_id.strip())

    def lookup(meta):
        symbol = normalize_symbol((meta or {}).get("symbol"))
        return index.get(symbol.upper()) if symbol else None

    return lookup


def _collect(security, config, as_of, *, refresh, issues, issuer_lookup=None) -> dict:
    """Every adapter record for one security, with its own per-capability statuses."""
    from . import market_data

    fund, sec_covered = _is_fund(security), _sec_covered(security, config)
    sid = security.get("security_id")
    ask = {"refresh": refresh, "issues": issues, "as_of": as_of}

    def run(name, call, **extra):
        return _attempt(name, sid, issues, call, config, security, **ask, **extra)

    prices = run("prices", market_data.price_history)
    statements = None if sec_covered else run("statements", market_data.statements)
    _attempt("statements", sid, issues, _complete_trailing, statements)
    estimates = None if fund else run("estimates", market_data.estimates)
    disclosure = (
        run("fund_disclosures", market_data.fund_disclosure, issuer_lookup=issuer_lookup)
        if fund
        else None
    )
    events = run("events", market_data.news_and_filings, actions=(prices or {}).get("actions"))
    profile = run("profile", market_data.security_profile)
    record = {
        "security": security,
        "prices": prices,
        "statements": statements,
        "estimates": estimates,
        "fund": disclosure,
        "events": events,
        "profile": profile,
        "sources": [
            source
            for source in (
                _source(prices, market_data.PRICES_PROVIDER, len((prices or {}).get("prices", []))),
                _source(
                    statements,
                    market_data.STATEMENTS_PROVIDER,
                    len((statements or {}).get("rows", [])),
                ),
                _source(estimates, market_data.ESTIMATES_PROVIDER, 1),
                _source(
                    disclosure,
                    market_data.FUNDS_PROVIDER,
                    len((disclosure or {}).get("holdings", [])),
                ),
                _source(events, market_data.NEWS_PROVIDER, len((events or {}).get("events", []))),
                _source(profile, market_data.PROFILE_PROVIDER, 1),
            )
            if source is not None
        ],
    }
    record["coverage"] = {
        "prices": _status(prices, config, present=bool((prices or {}).get("prices"))),
        "statements": "not_applicable"
        if sec_covered
        else _status(statements, config, present=bool((statements or {}).get("rows"))),
        "estimates": "not_applicable"
        if fund
        else _status(estimates, config, present=bool((estimates or {}).get("eps"))),
        "fund_disclosures": _status(
            disclosure, config, present=bool((disclosure or {}).get("holdings"))
        )
        if fund
        else "not_applicable",
        "events": _status(events, config, present=bool((events or {}).get("events"))),
        "profile": _status(profile, config),
    }
    return record


def _complete_trailing(statements) -> None:
    """Sum the remaining trailing flows over the same four quarters, in place.

    The adapter's trailing row carries the window's revenue and cash flows; a valuation
    also reads depreciation, the change in working capital and the tax charge. Those are
    flows too, so they are summed over exactly the quarters the window names rather than
    left at the latest quarter's value. A line absent from any quarter of the window is
    recorded as missing rather than annualised from a part year.
    """
    ttm = (statements or {}).get("ttm")
    if not isinstance(ttm, dict):
        return
    window = set(str(period) for period in str(ttm.get("ttm_quarters") or "").split(",") if period)
    quarters = [
        row for row in statements.get("quarterly") or [] if str(row.get("period_end")) in window
    ]
    if len(quarters) != len(window):
        return
    missing = [name for name in str(ttm.get("missing_fields") or "").split(",") if name]
    for field in TRAILING_FLOWS:
        values = [row.get(field) for row in quarters]
        if any(not isinstance(value, (int, float)) or isinstance(value, bool) for value in values):
            ttm[field] = None
            missing.append(field)
            continue
        ttm[field] = float(sum(values))
    ttm["missing_fields"] = ",".join(dict.fromkeys(missing))


def _cutoff_day(bundle):
    """``(New York day of the review's information cutoff, is a current review)``."""
    timeline = bundle.get("timeline") or {}
    cutoff = timeline.get("information_cutoff")
    if not cutoff:
        return None
    stamp = pd.Timestamp(cutoff)
    if stamp.tzinfo is None:
        return None
    day = stamp.tz_convert("America/New_York").date().isoformat()
    return day, timeline.get("review_kind") == "current"


def _date_disclosures(record, bundle, issues) -> None:
    """Date an undated fund snapshot against the review that asked for it.

    A current review fixes its information cutoff when the run begins and the
    disclosures arrive moments later, describing holdings the fund published before
    that: those rows are dated at the cutoff, not at the fetch instant, or the as-of
    filter would drop every freshly fetched snapshot. A review of any earlier date
    cannot use a snapshot taken after its cutoff at all, so the rows are dropped and
    coverage says missing rather than claiming look-through the filter will remove.
    """
    from .market_values import _issue

    fund = record.get("fund") or {}
    rows = [*(fund.get("holdings") or []), *(fund.get("sectors") or [])]
    stamped = _cutoff_day(bundle)
    if not rows or stamped is None:
        return
    day, current = stamped
    late = [row for row in rows if str(row.get("holdings_date") or "") > day]
    if not late:
        return
    if current:
        for row in late:
            row["holdings_date"] = day
            row["available_at"] = day
            row["disclosure_note"] = (
                "Undated provider snapshot fetched during this review; dated at the "
                "review's information cutoff rather than at the fetch instant."
            )
        return
    fund["holdings"], fund["sectors"] = [], []
    record["coverage"]["fund_disclosures"] = "missing"
    _issue(
        issues,
        "FUND_DISCLOSURE_AFTER_CUTOFF",
        record["security"].get("security_id"),
        f"The fund snapshot was received after this review's information cutoff ({day}); "
        "look-through is unavailable for this date.",
    )


def _sec_covered(security, config) -> bool:
    """SEC statements already cover this issuer; the provider copy is not asked for."""
    cik = security.get("cik") or ""
    return bool(config.get("data", {}).get("sec_enabled")) and bool(str(cik).strip())


def _apply_profile(securities, index, record) -> None:
    """Fill the securities row from the provider profile without overwriting identity."""
    profile = record.get("profile")
    issuer_id = str(record["security"].get("issuer_id") or "")
    if issuer_id.startswith("cik:") and not str(record["security"].get("cik") or "").strip():
        securities.at[index, "cik"] = issuer_id.removeprefix("cik:")
    if not isinstance(profile, dict):
        return
    for field in PROFILE_FIELDS:
        value = profile.get(field)
        if value is not None:
            securities.at[index, field] = value
    if profile.get("market_cap_received_at"):
        # Parity with the price connector: a capitalisation states when it was knowable.
        securities.at[index, "market_cap_available_at"] = profile["market_cap_received_at"]
    for field in ("name", "exchange"):
        if profile.get(field) and not str(record["security"].get(field) or "").strip():
            securities.at[index, field] = profile[field]


def _vintage_key(row) -> tuple:
    return (row.get("security_id"), str(row.get("period_end")), str(row.get("received_at")))


def _newest_vintage(rows, history) -> list[dict]:
    """The newest vintage of each period; a restated period's earlier row goes to history."""
    from .statements import latest_vintage

    if not rows:
        return []
    newest = latest_vintage(rows)
    kept = {_vintage_key(row) for row in newest}
    history.extend(row for row in rows if _vintage_key(row) not in kept)
    return newest


def _statement_rows(record, history) -> list[dict]:
    """The statements the engines read: the annual periods and the trailing window.

    The fundamentals frame holds one row per ``(security_id, period_end)``, so a quarter
    would shadow the annual period that ends on the same date and a quarterly figure
    would be read as a year. Quarterly rows build the trailing window and the brief and
    stay out of the frame; the trailing row is the most recent annualised period.
    """
    statements = record.get("statements") or {}
    annual = _newest_vintage(list(statements.get("annual") or []), history)
    # A restated quarter's earlier vintage is still kept, even though the quarterly rows
    # themselves never enter the frame.
    _newest_vintage(list(statements.get("quarterly") or []), history)
    trailing = statements.get("ttm")
    return [*annual, *([trailing] if isinstance(trailing, dict) else [])]


def _frames(records, bundle) -> dict:
    """Every adapter record grouped into the bundle's frame shapes."""
    frames = {name: [] for name in FRAMES}
    history: list[dict] = []
    for record in records.values():
        frames["prices"].extend((record.get("prices") or {}).get("prices") or [])
        frames["fundamentals"].extend(_statement_rows(record, history))
        frames["fund_holdings"].extend((record.get("fund") or {}).get("holdings") or [])
        frames["fund_sectors"].extend((record.get("fund") or {}).get("sectors") or [])
        frames["events"].extend((record.get("events") or {}).get("events") or [])
    if history:
        bundle["fundamentals_history"] = [*(bundle.get("fundamentals_history") or []), *history]
    return frames


def enrich_market(bundle: dict, config: dict, as_of: str) -> None:
    """Merge adapter research for every held listing into ``bundle``, in place.

    Failures are per security and per capability: they become issues and a coverage
    status, never an exception and never a substituted number.
    """
    from portfolio_lab.providers import FRAME_COLUMNS, _error_label, _merge

    from .market_values import _issue
    from .research_build import build_research

    issues = bundle.setdefault("issues", [])
    bundle.setdefault("sources", [])
    refresh = bool(config.get("data", {}).get("refresh_network", False))
    securities = bundle["securities"].copy()
    lookup = _issuer_lookup([_security(row) for _, row in securities.iterrows()])

    records, inputs = {}, {}
    for index, row in securities.iterrows():
        security = _security(row)
        sid = security.get("security_id")
        if not sid or not _supported(security):
            continue
        try:
            record = _collect(
                security, config, as_of, refresh=refresh, issues=issues, issuer_lookup=lookup
            )
            _date_disclosures(record, bundle, issues)
            _apply_profile(securities, index, record)
        except Exception as exc:  # No security's defect may end another's research.
            _issue(issues, "RESEARCH_SECURITY_FAILED", sid, _error_label(exc))
            continue
        records[str(sid)] = record
        bundle["sources"].extend(record["sources"])
        inputs[str(sid)] = {
            "symbol": (record.get("prices") or {}).get("symbol"),
            "currency": (record.get("prices") or {}).get("currency"),
            "quote_currency": (record.get("prices") or {}).get("quote_currency"),
            "estimates": record.get("estimates"),
            "fund_overview": (record.get("fund") or {}).get("fund_overview") or {},
            "coverage": record["coverage"],
        }
    bundle["securities"] = securities
    for name, rows in _frames(records, bundle).items():
        # Adapter rows carry their own provenance columns (quote unit, adjustment,
        # adapter version) beside the frame's required ones; keep them.
        frame = pd.DataFrame(rows) if rows else pd.DataFrame(columns=FRAME_COLUMNS.get(name, []))
        _merge(bundle, name, frame)
    bundle["research_inputs"] = {**(bundle.get("research_inputs") or {}), **inputs}
    bundle["coverage"] = coverage_report(bundle, config)
    bundle["research"] = build_research(records, bundle, config, as_of)


def _capability(by_security, name) -> dict:
    states = [(sid, row.get(name)) for sid, row in by_security.items()]
    considered = [(sid, state) for sid, state in states if state != "not_applicable"]
    return {
        "requested": len(considered),
        "available": sum(1 for _, state in considered if state in {"ok", "stale"}),
        "stale": sum(1 for _, state in considered if state == "stale"),
        "missing": sorted(sid for sid, state in considered if state == "missing"),
    }


def _ledger_capabilities(bundle) -> dict:
    """FX and listing coverage read from the ledger the collector already resolved."""
    ledger = bundle.get("ledger") or {}
    positions = [row for row in (ledger.get("positions") or []) if isinstance(row, dict)]
    unconverted = sorted(
        {
            str(row.get("security_id"))
            for row in positions
            if row.get("market_value_usd") in (None, "")
        }
    )
    securities = [row for row in (ledger.get("securities") or []) if isinstance(row, dict)]
    unresolved = sorted(
        {
            str(row.get("security_id"))
            for row in securities
            if str(row.get("resolution_status") or "").lower() != "resolved"
        }
    )
    return {
        "fx": {
            "requested": len(positions),
            "available": len(positions) - len(unconverted),
            "stale": 0,
            "missing": unconverted,
        },
        "listings": {
            "requested": len(securities),
            "available": len(securities) - len(unresolved),
            "stale": 0,
            "missing": unresolved,
        },
    }


def coverage_report(bundle: dict, config: dict) -> dict:
    """What each capability was asked for, what answered and what stayed missing."""
    from .market_data import ADAPTER_VERSION

    inputs = bundle.get("research_inputs") or {}
    by_security = {
        str(sid): dict((record or {}).get("coverage") or {}) for sid, record in inputs.items()
    }
    capabilities = {name: _capability(by_security, name) for name in SECURITY_CAPABILITIES}
    capabilities.update(_ledger_capabilities(bundle))
    return {
        "adapter_version": ADAPTER_VERSION,
        "generated_at": _now(),
        "capabilities": capabilities,
        "by_security": by_security,
        "limitations": list(LIMITATIONS),
    }
