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

``acquire_candidates`` adds the screened, unowned candidates of the dated universe to
the same frame, so one loop researches what the owner holds and what the review is
comparing it against. Holdings are never bounded; candidates are, by an explicit limit,
a provider time budget and the operation's own stop request, and whatever the bounds
left out is named in the coverage report rather than quietly missing.
"""

from __future__ import annotations

import math
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from time import monotonic

import pandas as pd

CAPABILITIES = (
    "prices",
    "statements",
    "estimates",
    "fund_disclosures",
    "events",
    "fx",
    "listings",
    "universe",
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
# Bounds on the candidate half of a fetch. Holdings are never bounded: a review that
# cannot say what it holds is not a review, while a narrower candidate set is a labelled
# narrower scope.
DEFAULT_ENRICHMENT_LIMIT = 1000
DEFAULT_TIME_BUDGET_SECONDS = 1800
DEFAULT_BRIEF_LIMIT = 20
EMPTY_UNIVERSE = {
    "acquired": 0,
    "qualified": 0,
    "in_scope": 0,
    "enriched": 0,
    "not_enriched": [],
    "identity_unresolved": [],
    "time_budget_exhausted": False,
    "stopped": False,
}
# The fetching stage hands the enrichment its own cancellation probe and progress sink.
# They are call-time behaviour, never bundle contents: the bundle is archived, and an
# archived review may not carry a live callable.
_CONTROLS: ContextVar[dict] = ContextVar("enrichment_controls", default={})
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


def _collect(security, config, as_of, *, refresh, issues, issuer_lookup=None, events=True) -> dict:
    """Every adapter record for one security, with its own per-capability statuses.

    ``events`` is the one capability a candidate can be researched without: news and
    filings are read by the brief, and the brief is written for the holdings and the
    largest candidates only. A capability that was not asked for says so rather than
    reporting a gap the review never looked for.
    """
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
    news = (
        run("events", market_data.news_and_filings, actions=(prices or {}).get("actions"))
        if events
        else None
    )
    profile = run("profile", market_data.security_profile)
    record = {
        "security": security,
        "prices": prices,
        "statements": statements,
        "estimates": estimates,
        "fund": disclosure,
        "events": news,
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
                _source(news, market_data.NEWS_PROVIDER, len((news or {}).get("events", []))),
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
        "events": _status(news, config, present=bool((news or {}).get("events")))
        if events
        else "not_applicable",
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


@contextmanager
def fetch_controls(*, should_stop=None, progress=None):
    """Lend this fetch a cancellation probe and a progress sink for its duration.

    Both are behaviour of the running operation, not contents of the bundle: the bundle
    is archived with the review, and an archived review may not carry a live callable.
    """
    token = _CONTROLS.set({"should_stop": should_stop, "progress": progress, "deadline": None})
    try:
        yield
    finally:
        _CONTROLS.reset(token)


def _control(name, explicit):
    """The caller's own callable, else the one the running stage lent this fetch."""
    if explicit is not None:
        return explicit
    value = _CONTROLS.get().get(name)
    return value if callable(value) else None


def _figure(value):
    """One configured or provider number as a float, with anything else as ``None``."""
    value = _clean(value)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _setting(config, group, name, default):
    value = _figure((config.get(group) or {}).get(name))
    return default if value is None else value


def _budget(config):
    """The instant this fetch stops asking providers, shared by both of its halves.

    Discovery and enrichment are one fetch, so they spend one budget. The deadline is
    computed once inside a ``fetch_controls`` scope and remembered there; outside one —
    a direct call in a test, say — each caller gets its own clock.
    """
    controls = _CONTROLS.get()
    remembered = controls.get("deadline")
    if isinstance(remembered, (int, float)) and not isinstance(remembered, bool):
        return float(remembered)
    deadline = monotonic() + float(
        _setting(config, "data", "provider_time_budget_seconds", DEFAULT_TIME_BUDGET_SECONDS)
    )
    if controls:  # Never the module-level default mapping, which every caller shares.
        controls["deadline"] = deadline
    return deadline


def _stop_requested(should_stop) -> bool:
    if should_stop is None:
        return False
    try:
        return bool(should_stop())
    except Exception:  # A probe that fails is not a cancellation, and never a failure.
        return False


def _report(progress, done, total) -> None:
    if progress is None:
        return
    try:
        progress({"done": int(done), "total": int(total)})
    except Exception:  # Progress is a courtesy; it can never end a fetch.
        pass


def _watchlist(config) -> set[str]:
    from .market_listings import normalize_symbol

    entries = (config.get("signals") or {}).get("watchlist") or []
    return {symbol for symbol in (normalize_symbol(entry) for entry in entries) if symbol}


def _is_candidate(security) -> bool:
    """A screened, unowned row the review compares against, rather than a holding."""
    return bool(security.get("candidate")) and not bool(security.get("owned"))


def _plan(securities, config) -> dict:
    """Which securities this fetch asks about, in what order, and which it will not.

    Holdings come first and are never dropped. Candidates follow in the order the owner
    would choose to spend a bounded provider budget in: the watchlist by name, then the
    largest issuers. The limit and the brief limit are applied to that order, so what a
    bound leaves out is the tail rather than an arbitrary set.
    """
    held, candidates = [], []
    for index, row in securities.iterrows():
        security = _security(row)
        if not security.get("security_id") or not _supported(security):
            continue
        (candidates if _is_candidate(security) else held).append((index, security))
    watchlist = _watchlist(config)

    def rank(item):
        _, security = item
        symbol = str(security.get("ticker") or security.get("security_id") or "").upper()
        return (
            0 if symbol in watchlist else 1,
            -(_figure(security.get("market_cap")) or 0.0),
            str(security.get("security_id")),
        )

    ordered = sorted(candidates, key=rank)
    limit = int(_setting(config, "data", "candidate_enrichment_limit", DEFAULT_ENRICHMENT_LIMIT))
    briefs = int(_setting(config, "signals", "candidate_brief_limit", DEFAULT_BRIEF_LIMIT))
    within, beyond = ordered[:limit], ordered[limit:]
    with_events = {str(security.get("security_id")) for _, security in within[:briefs]}
    return {
        "held": held,
        "candidates": within,
        "beyond_limit": sorted(str(security.get("security_id")) for _, security in beyond),
        "with_events": with_events,
        # A brief is built where one will be read: for every holding, and for the
        # candidates the brief limit admits. The rest report coverage only.
        "with_briefs": with_events | {str(security.get("security_id")) for _, security in held},
    }


def _identity_facts(
    config, rows, as_of, *, refresh, issues, should_stop, deadline, progress=None
) -> dict:
    """Sector, domicile and instrument facts for the acquired rows, watchlist first.

    The screen states a listing and a size; it does not state what the issuer is or
    where it is domiciled, and qualification may not assume either. Those facts come
    from the same company profile the enrichment reads, so asking for them here costs
    one provider answer per symbol for the whole fetch rather than two.

    This is provider work, so the fetch's own bounds apply to it: the rows are ranked
    the way the enrichment ranks them, ``data.candidate_enrichment_limit`` caps how many
    are asked about, and the shared deadline and stop probe end the pass. What a bound
    left untouched is returned as untouched — never as a row whose domicile was checked
    and found wanting.
    """
    from . import market_data

    known, unresolved, unattempted = {}, [], {}
    watchlist = _watchlist(config)

    def rank(row):
        symbol = str(row.get("symbol") or "").upper()
        return (
            0 if symbol in watchlist else 1,
            -(_figure(row.get("market_cap")) or 0.0),
            str(row.get("symbol")),
        )

    ordered = sorted((row for row in rows if isinstance(row, dict)), key=rank)
    limit = int(_setting(config, "data", "candidate_enrichment_limit", DEFAULT_ENRICHMENT_LIMIT))
    within, beyond = ordered[:limit], ordered[limit:]
    for row in beyond:
        symbol = str(row.get("symbol") or "")
        if symbol:
            unattempted[symbol] = "identity_limit_reached"
    stopped = exhausted = False
    total = len(within)
    _report(progress, 0, total)
    for position, row in enumerate(within):
        symbol = str(row.get("symbol") or "")
        if not symbol:
            continue
        stopped = _stop_requested(should_stop)
        exhausted = not stopped and monotonic() > deadline
        if stopped or exhausted:
            for item in within[position:]:
                tail = str(item.get("symbol") or "")
                if tail:
                    unattempted[tail] = "identity_not_attempted"
            break
        security = {
            "security_id": symbol,
            "ticker": symbol,
            "instrument_type": "equity",
            "resolution_status": "resolved",
        }
        profile = _attempt(
            "profile",
            symbol,
            issues,
            market_data.security_profile,
            config,
            security,
            refresh=refresh,
            issues=issues,
            as_of=as_of,
        )
        _report(progress, position + 1, total)
        if not isinstance(profile, dict):
            unresolved.append(symbol)
            continue
        known[symbol] = {
            # ``domicile`` is the profile's country code; qualification reads it as the
            # country it stands for and accepts nothing when it is absent.
            "country": profile.get("domicile"),
            "sector": profile.get("sector"),
            "instrument_type": profile.get("instrument_type"),
            "name": profile.get("name"),
        }
    return {
        "known": known,
        "unresolved": sorted(set(unresolved)),
        "unattempted": unattempted,
        "stopped": stopped,
        "time_budget_exhausted": exhausted,
        "identity_limited": bool(beyond),
    }


def _owned_symbols(securities) -> list[str]:
    from .market_values import listing_symbol

    symbols = []
    for _, row in securities.iterrows():
        symbol = listing_symbol(_security(row))
        if symbol:
            symbols.append(symbol)
    return symbols


def candidates_requested(config) -> bool:
    """Whether this review has been asked to look beyond what it already knows.

    Discovery is the owner's decision, not a default: it is a large provider ask and a
    claim about scope. A review acquires the screen once an account admits the screened
    eligible universe or the watchlist names symbols, and otherwise researches exactly
    the securities it already has — with no screen call and no scope claim.
    """
    policies = (config.get("mandate") or {}).get("account_candidate_policy") or {}
    admitted = any(policy == "eligible_universe" for policy in policies.values())
    return bool(admitted or _watchlist(config))


def acquire_candidates(bundle: dict, config: dict, as_of: str, *, refresh: bool) -> None:
    """Add the dated screen's qualifying unowned candidates to ``bundle``, in place.

    The acquisition and its qualification are recorded whatever happens: an unavailable
    screen leaves no candidates and an issue, never a quietly narrower review. Rows the
    qualification drops stay inspectable in ``bundle["exclusions"]`` with their reasons.
    """
    from portfolio_lab.providers import _error_label

    from .market_values import _issue
    from .universe import acquire_universe, qualify_universe

    issues = bundle.setdefault("issues", [])
    securities = bundle.get("securities")
    if not isinstance(securities, pd.DataFrame) or not candidates_requested(config):
        return
    should_stop = _control("should_stop", None)
    progress = _control("progress", None)
    deadline = _budget(config)
    acquired = acquire_universe(
        config,
        as_of=as_of,
        refresh=refresh,
        issues=issues,
        should_stop=should_stop,
        deadline=deadline,
    )
    rows = acquired.get("rows") or []
    identity = _identity_facts(
        config,
        rows,
        as_of,
        refresh=refresh,
        issues=issues,
        should_stop=should_stop,
        deadline=deadline,
        progress=progress,
    )
    meta = acquired.get("meta") or {}
    discovery = {
        "stopped": bool(identity["stopped"] or meta.get("stopped")),
        "time_budget_exhausted": bool(
            identity["time_budget_exhausted"] or meta.get("time_budget_exhausted")
        ),
        "identity_limited": bool(identity["identity_limited"]),
    }
    if discovery["stopped"] or discovery["time_budget_exhausted"]:
        _issue(
            issues,
            "UNIVERSE_DISCOVERY_INTERRUPTED",
            None,
            "Candidate discovery stopped before every acquired symbol was identified"
            + (" because the review was asked to stop" if discovery["stopped"] else "")
            + (
                " because the provider time budget ran out"
                if discovery["time_budget_exhausted"] and not discovery["stopped"]
                else ""
            )
            + "; the symbols it did not reach are excluded as not attempted, not as "
            "issuers found wanting.",
        )
    lookup = None
    if config.get("data", {}).get("sec_enabled"):
        from .issuers import sec_issuer_lookup

        lookup = _attempt(
            "issuers", None, issues, sec_issuer_lookup, config, refresh=refresh, issues=issues
        )
    try:
        qualified = qualify_universe(
            rows,
            owned_securities=_owned_symbols(securities),
            listings=identity["known"],
            issuer_lookup=lookup,
            config=config,
            watchlist=sorted(_watchlist(config)),
            as_of=as_of,
            unattempted=identity["unattempted"],
        )
    except Exception as exc:  # A qualification defect costs discovery, not the review.
        _issue(issues, "UNIVERSE_QUALIFICATION_FAILED", None, _error_label(exc), "error")
        return
    existing = set(securities["security_id"].astype(str)) if "security_id" in securities else set()
    fresh = [
        dict(row, candidate=True)
        for row in qualified["candidates"]
        if str(row.get("security_id")) not in existing
    ]
    if fresh:
        bundle["securities"] = pd.concat(
            [securities, pd.DataFrame(fresh)], ignore_index=True
        ).reset_index(drop=True)
    bundle["universe"] = {
        "meta": meta,
        "counts": dict(qualified["counts"]),
        "scope_label": qualified["scope_label"],
        "scope_base": qualified["scope_label"],
        "method_version": qualified["method_version"],
        "notes": qualified.get("notes") or [],
        "identity_unresolved": identity["unresolved"],
        "discovery": discovery,
    }
    bundle["exclusions"] = qualified["exclusions"]


def _record_universe(bundle, *, in_scope, enriched, not_enriched, exhausted, stopped) -> None:
    """State what the candidate half of this fetch reached, and what it did not.

    The stop and budget flags cover the whole fetch, acquisition and identity included,
    so an interruption before any candidate survived still reads as an interruption.
    The scope label is rewritten from the same numbers, because the one sentence a
    reader sees and the counts beside it may never disagree.
    """
    universe = dict(bundle.get("universe") or {})
    counts = dict(universe.get("counts") or {})
    discovery = universe.get("discovery") or {}
    coverage = {
        "acquired": int(counts.get("acquired") or 0),
        "qualified": int(counts.get("qualified") or 0),
        "in_scope": int(counts.get("in_scope") or in_scope),
        "enriched": len(enriched),
        "not_enriched": sorted(not_enriched),
        "identity_unresolved": sorted(universe.get("identity_unresolved") or []),
        "time_budget_exhausted": bool(exhausted or discovery.get("time_budget_exhausted")),
        "stopped": bool(stopped or discovery.get("stopped")),
    }
    counts["enriched"] = coverage["enriched"]
    counts["not_enriched"] = coverage["not_enriched"]
    counts.setdefault("in_scope", coverage["in_scope"])
    universe["counts"] = counts
    universe["coverage"] = coverage
    universe["scope_label"] = _scope_label(universe, coverage)
    bundle["universe"] = universe


def _scope_label(universe, coverage) -> str:
    """The scope sentence, ending in how much of that scope this review researched."""
    base = universe.get("scope_base") or universe.get("scope_label")
    if not base:
        return ""
    universe["scope_base"] = base
    reached = f"{coverage['enriched']} of {coverage['in_scope']} enriched"
    if coverage["stopped"] or coverage["time_budget_exhausted"]:
        return f"{base}; discovery interrupted, {reached}"
    return f"{base}; {reached}"


def _forecast_ids(bundle, as_of, horizon) -> tuple[set[str], set[str]]:
    """``(covered, elsewhere)``: ids stated for this run, and ids stated only otherwise.

    The engine needs one joint set: the same horizon on the same date for every asset it
    compares. An imported row dated a fortnight ago, or stated over another horizon, is
    the owner's statement about a different question, so it is not coverage for this
    run — which is exactly the gap the shared state exists to fill.
    """
    frame = bundle.get("forecasts")
    if not isinstance(frame, pd.DataFrame) or frame.empty or "security_id" not in frame:
        return set(), set()
    ids = frame["security_id"].astype(str)
    if "horizon_months" not in frame or "forecast_date" not in frame:
        return set(), set(ids.dropna())
    dates = pd.to_datetime(frame["forecast_date"], errors="coerce", utc=True, format="mixed")
    wanted = pd.to_datetime(as_of, errors="coerce", utc=True)
    matches = (pd.to_numeric(frame["horizon_months"], errors="coerce") == horizon) & (
        dates.dt.date == wanted.date()
    )
    covered = set(ids[matches].dropna())
    return covered, set(ids.dropna()) - covered


def _apply_shared_state(bundle, config, as_of) -> None:
    """Expand the owner's shared scenario state over the assets this run has no view on.

    An imported forecast for this run — this horizon, dated as of this review — is the
    owner's own statement and always wins; the shared state is never asked about it.
    An import of another vintage or another horizon is superseded rather than mixed into
    the joint set, and every superseded security is named so the precedence is visible.
    """
    from portfolio_lab.providers import _error_label, _merge

    from .market_values import _issue
    from .scenarios import DEFAULT_SHARED_STATE, generate_joint_forecasts

    securities = bundle.get("securities")
    if not isinstance(securities, pd.DataFrame) or securities.empty:
        return
    horizon = int(_setting(config, "allocation", "horizon_months", 12))
    stated, elsewhere = _forecast_ids(bundle, as_of, horizon)
    missing = securities[~securities["security_id"].astype(str).isin(stated)]
    if missing.empty:
        return
    state = (config.get("allocation") or {}).get("shared_state") or DEFAULT_SHARED_STATE
    mandate = config.get("mandate") or {}
    issues = bundle.setdefault("issues", [])
    try:
        generated = generate_joint_forecasts(
            missing,
            state,
            as_of=as_of,
            horizon_months=horizon,
            presentation_currency=str(mandate.get("base_currency") or "USD"),
            benchmark_ids=(mandate.get("benchmark_id"),),
        )
    except Exception as exc:  # A malformed state leaves the run without its own set.
        _issue(issues, "SHARED_STATE_UNUSABLE", None, _error_label(exc), "error")
        return
    issues.extend(generated.attrs.get("issues") or [])
    if generated.empty:
        return
    for sid in sorted(elsewhere & set(generated["security_id"].astype(str))):
        _issue(
            issues,
            "IMPORTED_FORECAST_SUPERSEDED",
            sid,
            f"The imported forecast for {sid} is not a {horizon}-month view as of {as_of}; "
            "the shared scenario state states this run's view instead.",
        )
    _merge(bundle, "forecasts", generated)


def enrich_market(
    bundle: dict, config: dict, as_of: str, *, should_stop=None, progress=None
) -> None:
    """Merge adapter research for every held and screened listing into ``bundle``.

    Failures are per security and per capability: they become issues and a coverage
    status, never an exception and never a substituted number. Candidates are asked
    about within the configured limit, the provider time budget and the operation's own
    stop request; every candidate a bound left out is named in the coverage report.
    """
    from portfolio_lab.providers import FRAME_COLUMNS, _error_label, _merge

    from .market_values import _issue
    from .research_build import build_research

    issues = bundle.setdefault("issues", [])
    bundle.setdefault("sources", [])
    refresh = bool(config.get("data", {}).get("refresh_network", False))
    should_stop = _control("should_stop", should_stop)
    progress = _control("progress", progress)
    securities = bundle["securities"].copy()
    lookup = _issuer_lookup([_security(row) for _, row in securities.iterrows()])

    plan = _plan(securities, config)
    queue = [*plan["held"], *plan["candidates"]]
    holdings = len(plan["held"])
    total, deadline = len(queue), _budget(config)
    exhausted = stopped = False
    unreached: list[str] = []
    records, inputs = {}, {}
    _report(progress, 0, total)
    for position, (index, security) in enumerate(queue):
        sid = security.get("security_id")
        if position >= holdings:
            stopped = stopped or _stop_requested(should_stop)
            exhausted = exhausted or monotonic() > deadline
            if stopped or exhausted:
                unreached = [str(row.get("security_id")) for _, row in queue[position:]]
                break
        try:
            record = _collect(
                security,
                config,
                as_of,
                refresh=refresh,
                issues=issues,
                issuer_lookup=lookup,
                events=position < holdings or str(sid) in plan["with_events"],
            )
            _date_disclosures(record, bundle, issues)
            _apply_profile(securities, index, record)
        except Exception as exc:  # No security's defect may end another's research.
            _issue(issues, "RESEARCH_SECURITY_FAILED", sid, _error_label(exc))
            _report(progress, position + 1, total)
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
        _report(progress, position + 1, total)
    bundle["securities"] = securities
    for name, rows in _frames(records, bundle).items():
        # Adapter rows carry their own provenance columns (quote unit, adjustment,
        # adapter version) beside the frame's required ones; keep them.
        frame = pd.DataFrame(rows) if rows else pd.DataFrame(columns=FRAME_COLUMNS.get(name, []))
        _merge(bundle, name, frame)
    bundle["research_inputs"] = {**(bundle.get("research_inputs") or {}), **inputs}
    planned = [str(security.get("security_id")) for _, security in plan["candidates"]]
    _record_universe(
        bundle,
        in_scope=len(planned) + len(plan["beyond_limit"]),
        enriched=[sid for sid in planned if sid in records],
        not_enriched=set(planned) - set(records) | set(plan["beyond_limit"]) | set(unreached),
        exhausted=exhausted,
        stopped=stopped,
    )
    _apply_shared_state(bundle, config, as_of)
    bundle["coverage"] = coverage_report(bundle, config)
    bundle["research"] = build_research(
        records, bundle, config, as_of, brief_ids=plan["with_briefs"]
    )


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
    # Candidate coverage is counted in issuers, not in per-security capability states:
    # it says how much of the screened universe this review actually researched.
    universe = (bundle.get("universe") or {}).get("coverage")
    capabilities["universe"] = (
        dict(universe) if isinstance(universe, dict) else dict(EMPTY_UNIVERSE)
    )
    return {
        "adapter_version": ADAPTER_VERSION,
        "generated_at": _now(),
        "capabilities": capabilities,
        "by_security": by_security,
        "limitations": list(LIMITATIONS),
    }
