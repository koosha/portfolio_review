"""The dated candidate universe: what was acquired, what qualifies, and what did not.

Acquisition pages one Yahoo equity screen for a date and archives every page with its
receipt, so the candidate set a review used can be re-read later without the provider.
It is a candidate set, never a claim of a complete universe: the meta says so, and a
provider failure leaves no rows and one issue rather than a quietly narrower screen.

Qualification is pure and records a reason for every row it drops. It establishes
identity only — company equity, a US listing quoted in USD, ordinary common stock, one
representative class per issuer, sectors that need their own model, and the size of the
scope. Liquidity and price are never claimed here: ``metrics.score_securities`` still
evaluates them from enriched prices, so a row kept by this module is a row worth
researching, not a row already found investable.
"""

from __future__ import annotations

import re
import threading
from math import ceil
from time import monotonic

from .issuers import US_EXCHANGES
from .market_listings import normalize_symbol
from .market_values import ADAPTER_VERSION, _issue, _number, _text, sector_label

UNIVERSE_PROVIDER = "yahoo_screen"
SCREEN_URL = "https://query2.finance.yahoo.com/v1/finance/screener"
METHOD_VERSION = "universe-1"
PAGE_SIZE = 250
SORT_FIELD = "intradaymarketcap"
# The screen is a discovery tool, not a filter that decides anything: the floor only
# keeps the paging away from the long tail of shells the review would exclude anyway.
MINIMUM_MARKET_CAP = 2_000_000_000
DEFAULT_ACQUIRE_SIZE = 1500
DEFAULT_UNIVERSE_SIZE = 1000
SCOPE = "Yahoo screen region=us sorted by intraday market cap"
COVERAGE_CLAIM = "acquisition candidate set; not proof of a complete universe"
# Two classes of one issuer should carry the same issuer-wide capitalisation. A wider
# disagreement means the figures are not measuring the same thing.
ISSUER_CAP_TOLERANCE = 0.001
# One page of slack over the pages the requested size needs. Past that the screen is
# not answering the question that was asked, and paging on is a provider call the review
# cannot justify.
PAGE_SLACK = 1


class ScreenUnavailable(RuntimeError):
    """The candidate screen did not produce a usable page. The text is safe to record."""


def _now() -> str:
    from portfolio_lab.providers import _now as provider_now

    return provider_now()


def _acquisition_key(as_of, size) -> str:
    return f"{as_of}_us_equity_{size}"


def _screen_row(quote, received_at, source_id) -> dict | None:
    """One acquired screen quote as a row, or ``None`` when it carries no symbol."""
    if not isinstance(quote, dict):
        return None
    symbol = normalize_symbol(quote.get("symbol"))
    if symbol is None:
        return None
    currency = _text(quote.get("currency"), 8)
    quote_type = _text(quote.get("quoteType"), 20)
    return {
        "symbol": symbol,
        "name": _text(quote.get("longName") or quote.get("shortName")),
        "exchange": _text(quote.get("exchange"), 20),
        "currency": None if currency is None else currency.upper(),
        "quote_type": None if quote_type is None else quote_type.upper(),
        "market_cap": _number(quote.get("marketCap")),
        "shares_outstanding": _number(quote.get("sharesOutstanding")),
        "full_exchange_name": _text(quote.get("fullExchangeName"), 60),
        "received_at": received_at,
        "source_id": source_id,
    }


def _call_screen(config, yahoo, query, *, offset, size) -> dict:
    """One screen page, bounded by the configured timeout and minimum call interval."""
    from portfolio_lab.providers import _error_label

    from .market_data import _pace

    timeout = _number(config.get("data", {}).get("provider_timeout_seconds")) or 30.0
    _pace(config)
    answered, outcome = threading.Event(), {}

    def call():
        try:
            outcome["value"] = yahoo.screen(
                query, offset=offset, size=size, sortField=SORT_FIELD, sortAsc=False
            )
        except Exception as exc:  # Reported on the calling thread, never here.
            outcome["error"] = exc
        finally:
            answered.set()

    # A daemon worker, like every other provider call: an abandoned screen must not be
    # joined at interpreter exit.
    threading.Thread(target=call, name=f"{UNIVERSE_PROVIDER}-fetch", daemon=True).start()
    if not answered.wait(timeout):
        raise ScreenUnavailable(f"the candidate screen did not answer within {timeout:g} s")
    if "error" in outcome:
        # Provider exceptions can carry request URLs and tokens; only the label is kept.
        raise ScreenUnavailable(_error_label(outcome["error"]))
    payload = outcome["value"]
    if not isinstance(payload, dict) or not isinstance(payload.get("quotes"), list):
        raise ScreenUnavailable("the candidate screen returned no quote records")
    return payload


def _interrupted(should_stop) -> bool:
    if should_stop is None:
        return False
    try:
        return bool(should_stop())
    except Exception:  # A probe that fails is not a cancellation, and never a failure.
        return False


def _acquire_pages(
    config, *, as_of, size, key, should_stop=None, deadline=None
) -> tuple[list[dict], dict]:
    """Page the screen up to ``size`` symbols, archiving each page as it arrives.

    The loop is bounded by arithmetic the review does itself — the pages ``size`` needs,
    plus one — and by a page that contributes no new symbol, so a screen that omits
    ``total`` or replays one page cannot hold the fetch open. The owner's stop request
    and the fetch's own deadline are checked before every page, and what the loop did
    not finish is stated in the meta rather than passed off as the whole screen.
    """
    from portfolio_lab.providers import _archive

    try:
        import yfinance as yf
    except ImportError as exc:
        raise ScreenUnavailable("yfinance is not installed") from exc

    query = yf.EquityQuery(
        "and",
        [
            yf.EquityQuery("eq", ["region", "us"]),
            yf.EquityQuery("gt", [SORT_FIELD, MINIMUM_MARKET_CAP]),
        ],
    )
    rows, seen, pages, offset, total = [], set(), 0, 0, None
    page_limit = ceil(max(size, 1) / PAGE_SIZE) + PAGE_SLACK
    stopped = exhausted = incomplete = False
    while len(rows) < size:
        if _interrupted(should_stop):
            stopped = incomplete = True
            break
        if deadline is not None and monotonic() > deadline:
            exhausted = incomplete = True
            break
        if pages >= page_limit:
            incomplete = True
            break
        payload = _call_screen(
            config, yf, query, offset=offset, size=min(PAGE_SIZE, size - len(rows))
        )
        received_at = _now()
        source = _archive(
            config,
            UNIVERSE_PROVIDER,
            f"{key}_p{offset:05d}",
            payload,
            SCREEN_URL,
            received_at,
            adapter_version=ADAPTER_VERSION,
            as_of=as_of,
            offset=offset,
            point_in_time="current_retrieval; not a historical vintage",
        )
        pages += 1
        reported = _number(payload.get("total"))
        if reported is not None:
            total = int(reported)
        quotes = payload.get("quotes") or []
        before = len(rows)
        for quote in quotes:
            row = _screen_row(quote, received_at, source["source_id"])
            if row is not None and row["symbol"] not in seen:
                seen.add(row["symbol"])
                rows.append(row)
            if len(rows) >= size:
                break
        if not quotes:
            break
        if len(rows) == before:
            # The page answered, and named nothing the earlier pages had not. Whatever
            # the provider intended, offset is no longer moving through a list.
            incomplete = True
            break
        offset += len(quotes)
        if total is not None and offset >= total:
            break
    meta = {
        "acquired_at": _now(),
        "as_of": as_of,
        "total_reported": total,
        "pages": pages,
        "acquire_size": size,
        "scope": SCOPE,
        "coverage_claim": COVERAGE_CLAIM,
        "adapter_version": ADAPTER_VERSION,
        "stopped": stopped,
        "time_budget_exhausted": exhausted,
        "incomplete": incomplete,
    }
    _archive(
        config,
        UNIVERSE_PROVIDER,
        key,
        {"rows": rows, "meta": meta},
        SCREEN_URL,
        meta["acquired_at"],
        adapter_version=ADAPTER_VERSION,
        as_of=as_of,
        point_in_time="current_retrieval; not a historical vintage",
    )
    return rows, meta


def _cached_acquisition(config, key):
    """``(rows, meta)`` from the archived acquisition for this key, or ``None``."""
    from portfolio_lab.providers import _cached

    try:
        payload, received_at, _ = _cached(config, UNIVERSE_PROVIDER, key)
    except Exception:  # A missing or damaged archive is simply no cached acquisition.
        return None
    if not isinstance(payload, dict):
        return None
    rows, meta = payload.get("rows"), payload.get("meta")
    if not isinstance(rows, list) or not isinstance(meta, dict):
        return None
    meta = dict(meta)
    meta.setdefault("acquired_at", received_at)
    return [row for row in rows if isinstance(row, dict)], meta


def _empty(as_of, size) -> dict:
    return {
        "rows": [],
        "meta": {
            "acquired_at": None,
            "as_of": as_of,
            "total_reported": None,
            "pages": 0,
            "acquire_size": size,
            "scope": SCOPE,
            "coverage_claim": COVERAGE_CLAIM,
            "adapter_version": ADAPTER_VERSION,
            "stopped": False,
            "time_budget_exhausted": False,
            "incomplete": True,
        },
    }


def acquire_universe(config, *, as_of, refresh, issues, should_stop=None, deadline=None) -> dict:
    """The dated candidate screen as ``{"rows": [...], "meta": {...}}``.

    Without ``refresh``, and in demo or offline mode, only the archived acquisition for
    this date answers. A failed refresh falls back to that archive when there is one;
    otherwise the rows are empty and ``UNIVERSE_UNAVAILABLE`` records why. An
    acquisition is never carried across dates: the key names the date it was taken for.

    ``should_stop`` and ``deadline`` belong to the fetch as a whole, so a cancelled or
    out-of-time review stops paging where it is and says the screen is partial instead
    of publishing a short set as though it were the whole one.
    """
    from portfolio_lab.providers import _error_label

    from .market_data import _fresh

    data = config.get("data", {})
    size = int(_number(data.get("universe_acquire_size")) or DEFAULT_ACQUIRE_SIZE)
    key = _acquisition_key(as_of, size)
    cached = _cached_acquisition(config, key)
    live = bool(refresh) and data.get("mode") not in {"demo", "offline"}
    if cached is not None and (not live or _fresh(cached[1].get("acquired_at"), config)):
        return {"rows": cached[0], "meta": cached[1]}
    if not live:
        _issue(
            issues,
            "UNIVERSE_UNAVAILABLE",
            None,
            f"No archived candidate screen for {as_of}; run with refresh to acquire one.",
            "error",
        )
        return _empty(as_of, size)
    try:
        rows, meta = _acquire_pages(
            config, as_of=as_of, size=size, key=key, should_stop=should_stop, deadline=deadline
        )
    except Exception as exc:  # One failed screen ends discovery, never the review.
        detail = str(exc) if isinstance(exc, ScreenUnavailable) else _error_label(exc)
        if cached is not None:
            _issue(
                issues,
                "UNIVERSE_REFRESH_FAILED",
                None,
                f"Candidate screen refresh failed ({detail}); the archived acquisition was used.",
            )
            return {"rows": cached[0], "meta": cached[1]}
        _issue(
            issues,
            "UNIVERSE_UNAVAILABLE",
            None,
            f"Candidate screen unavailable ({detail}); no candidates were acquired.",
            "error",
        )
        return _empty(as_of, size)
    if meta.get("incomplete"):
        _issue(
            issues,
            "UNIVERSE_ACQUISITION_INCOMPLETE",
            None,
            _acquisition_detail(meta, len(rows), size),
        )
    return {"rows": rows, "meta": meta}


def _acquisition_detail(meta, acquired, size) -> str:
    """Why the screen stopped short, in the words a reader can check against the meta."""
    if meta.get("stopped"):
        why = "the review was asked to stop"
    elif meta.get("time_budget_exhausted"):
        why = "the provider time budget ran out"
    else:
        why = "the screen stopped naming new symbols"
    return (
        f"The candidate screen acquired {acquired} of the {size} symbols asked for after "
        f"{meta.get('pages') or 0} pages because {why}; the scope below is that narrower set."
    )


# Preferred, warrant, unit and rights lines are not ordinary common stock, whatever
# the issuer behind them is.
_NON_COMMON_SUFFIX = re.compile(r"-(?:P|W|U|R)[A-Z]?$")
_NON_COMMON_MARK = re.compile(r"\.PR|\^")
_DEPOSITARY = re.compile(r"\bADR\b|[Dd]epositar|[Dd]epositor")
_SHARE_CLASS = re.compile(r"\bCLASS\s+([A-Z])\b")
# Legal-form and class words carry no identity: two filings of one issuer differ by
# them alone, so a name-matched issuer key drops them from the end of the name.
_NAME_NOISE = frozenset(
    "INC INCORPORATED CORP CORPORATION CO COMPANY LTD LIMITED PLC LLC LP THE CLASS "
    "HOLDING HOLDINGS GROUP SA NV AG AB ASA A B C D".split()
)
_US_COUNTRIES = frozenset({"United States", "United States of America", "USA", "US"})
_DEPOSITARY_TYPES = frozenset({"adr", "gdr", "depositary_receipt", "depository_receipt"})


def _name_key(name) -> str | None:
    """A name reduced to the words that identify an issuer, or ``None``."""
    text = _text(name, 200)
    if text is None:
        return None
    tokens = [token for token in re.split(r"[^A-Za-z0-9]+", text.upper()) if token]
    while len(tokens) > 1 and tokens[-1] in _NAME_NOISE:
        tokens.pop()
    return " ".join(tokens) or None


def _share_class(name) -> str | None:
    text = _text(name, 200)
    if text is None:
        return None
    found = _SHARE_CLASS.search(text.upper())
    return found.group(1) if found else None


def _identity_reasons(row, listing) -> list[str]:
    """Why this row is not one issuer's ordinary common US listing, in order."""
    reasons = []
    symbol = row["symbol"]
    if _NON_COMMON_SUFFIX.search(symbol) or _NON_COMMON_MARK.search(symbol):
        reasons.append("not_ordinary_common")
    kind = _text(listing.get("instrument_type"), 40) or ""
    name = _text(listing.get("name")) or _text(row.get("name")) or ""
    if kind.lower() in _DEPOSITARY_TYPES or _DEPOSITARY.search(name):
        reasons.append("depositary_receipt")
    if _text(listing.get("country"), 60) not in _US_COUNTRIES:
        # Domicile comes from the resolved listing. An unresolved listing establishes
        # nothing, so the row is not claimed as a US issuer either way.
        reasons.append("us_domicile_unverified")
    return reasons


def _listing_reasons(row) -> list[str]:
    """Why this acquired row is not a US-listed company equity quoted in USD."""
    reasons = []
    if row.get("quote_type") != "EQUITY":
        reasons.append("not_company_equity")
    if row.get("exchange") not in US_EXCHANGES:
        reasons.append("non_us_listing")
    if row.get("currency") != "USD":
        reasons.append("currency_mismatch")
    return reasons


def _cap_spread(rows) -> float | None:
    """The relative disagreement between the issuer caps of an issuer's classes."""
    caps = [row["_cap"] for row in rows if row["_cap"] > 0]
    if len(caps) < 2:
        return None
    return (max(caps) - min(caps)) / max(caps)


def _candidate(row, *, issuer_id, sector, owned, as_of) -> dict:
    """One qualified row in the securities-frame shape the research bundle merges."""
    name = _text(row.get("name"))
    return {
        "security_id": row["symbol"],
        "ticker": row["symbol"],
        "issuer_id": issuer_id,
        "name": name,
        "sector": sector,
        "instrument_type": "equity",
        "currency": "USD",
        "cik": issuer_id[4:] if issuer_id.startswith("cik:") else None,
        "eligible": True,
        "market_cap": row.get("market_cap"),
        "market_cap_as_of": as_of,
        "market_cap_available_at": row.get("received_at"),
        "market_cap_received_at": row.get("received_at"),
        "exchange": row.get("exchange"),
        "share_class": _share_class(name),
        "domicile": "US",
        "equity_type": "ordinary_common",
        "resolution_status": "resolved",
        "owned": owned,
        "in_scope": True,
        "source_id": row.get("source_id"),
    }


def qualify_universe(
    rows, *, owned_securities, listings, issuer_lookup, config, watchlist, as_of, unattempted=None
) -> dict:
    """Which acquired rows are worth researching, which are not, and why.

    Pure: it reads the acquisition, the resolved listings and the configuration, and
    returns candidate rows in the securities-frame shape together with one exclusion
    record per dropped row. Every acquired row ends in exactly one of the two lists.

    ``unattempted`` maps a symbol whose identity was never asked about — a stopped
    fetch, an expired budget, a bound that stopped short of it — to the reason it was
    not asked about. Such a row is excluded under that reason instead of under
    ``us_domicile_unverified``, which would assert a check that never ran.

    A watchlisted symbol the acquisition never returned is recorded as an exclusion
    with reason ``watchlist_not_acquired`` rather than passed over: the owner named it,
    so the review owes them an answer about it either way.

    Watchlisted and owned symbols are kept past the size limit and past a sector the
    review models separately, because the owner asked for them by name. They are not
    kept past identity: a row that is not one issuer's US ordinary common listing in
    USD is excluded with its reasons, watchlisted or not, so the candidate frame never
    claims something the provider did not establish.
    """
    signals = config.get("signals", {}) or {}
    size = int(_number(config.get("data", {}).get("universe_size")) or DEFAULT_UNIVERSE_SIZE)
    excluded_sectors = {s for s in (signals.get("excluded_sectors") or []) if isinstance(s, str)}
    require_consistent = signals.get("require_consistent_issuer_cap", True) is not False
    owned = {s for s in (normalize_symbol(x) for x in owned_securities or ()) if s}
    watched = {s for s in (normalize_symbol(x) for x in watchlist or ()) if s}
    known = listings if isinstance(listings, dict) else {}
    held_back = unattempted if isinstance(unattempted, dict) else {}
    exclusions, notes = [], []

    def drop(row, reasons, **extra):
        exclusions.append(
            {
                "symbol": row.get("symbol"),
                "name": _text(row.get("name")),
                "market_cap": row.get("market_cap"),
                "reasons": reasons,
                **extra,
            }
        )

    surviving = []
    for acquired in rows or ():
        if not isinstance(acquired, dict):
            continue
        symbol = normalize_symbol(acquired.get("symbol"))
        if symbol is None:
            drop(acquired, ["listing_symbol_unreadable"])
            continue
        listing = known.get(symbol)
        row = dict(
            acquired,
            symbol=symbol,
            _cap=_number(acquired.get("market_cap")) or 0.0,
            _listing=listing if isinstance(listing, dict) else {},
        )
        reasons = _listing_reasons(row)
        if not reasons:
            skipped = held_back.get(symbol)
            reasons = [skipped] if skipped else _identity_reasons(row, row["_listing"])
        if reasons:
            drop(row, reasons)
            continue
        surviving.append(row)

    groups: dict[str, list[dict]] = {}
    for row in surviving:
        key = None
        if callable(issuer_lookup):
            try:
                key = issuer_lookup(row)
            except Exception:  # An issuer lookup that fails leaves the row listing-scoped.
                key = None
        key = key if isinstance(key, str) and key.strip() else None
        row["_authoritative"] = key is not None
        if key is None:
            name_key = _name_key(row.get("name")) or _name_key(row["_listing"].get("name"))
            key = f"name:{name_key}" if name_key else f"listing:{row['symbol']}"
        row["_issuer_id"] = key
        groups.setdefault(key, []).append(row)

    representatives = []
    for members in groups.values():
        ordered = sorted(members, key=lambda row: (-row["_cap"], row["symbol"]))
        representative = ordered[0]
        for other in ordered[1:]:
            drop(other, ["duplicate_share_class"], representative_symbol=representative["symbol"])
        # A cap disagreement is evidence only when the issuer key is the issuer's own
        # identifier. Names matched across two rows may simply be two issuers.
        spread = _cap_spread(ordered) if representative["_authoritative"] else None
        representative["_inconsistent"] = spread is not None and spread > ISSUER_CAP_TOLERANCE
        if representative["_inconsistent"] and require_consistent:
            drop(representative, ["issuer_cap_inconsistent"])
            continue
        representatives.append(representative)

    qualified = []
    for row in representatives:
        row["_sector"] = sector_label(row["_listing"].get("sector"))
        asked_for = row["symbol"] in watched or row["symbol"] in owned
        row["_sector_rescue"] = row["_sector"] in excluded_sectors and asked_for
        if row["_sector"] in excluded_sectors and not asked_for:
            drop(row, ["separate_sector_model_required"])
            continue
        qualified.append(row)

    candidates, watchlist_included, owned_retained = [], 0, 0
    for rank, row in enumerate(sorted(qualified, key=lambda row: (-row["_cap"], row["symbol"]))):
        beyond = rank >= size or row["_sector_rescue"]
        by_watchlist = beyond and row["symbol"] in watched
        by_ownership = beyond and not by_watchlist and row["symbol"] in owned
        if rank >= size and not (by_watchlist or by_ownership):
            drop(row, ["beyond_size_limit"])
            continue
        watchlist_included += int(by_watchlist)
        owned_retained += int(by_ownership)
        candidates.append(
            _candidate(
                row,
                issuer_id=row["_issuer_id"],
                sector=row["_sector"],
                owned=row["symbol"] in owned,
                as_of=as_of,
            )
        )
        tags = ["watchlist"] if row["symbol"] in watched else []
        if row["_inconsistent"]:
            tags.append("issuer_cap_inconsistent")
        if tags:
            notes.append({"symbol": row["symbol"], "reasons": tags})

    # A symbol the owner named by hand and the screen never returned is neither a
    # candidate nor a dropped row, so nothing above would have mentioned it. It is
    # recorded here as what it is: asked for, not acquired. Owned symbols are left out —
    # the review already researches those as holdings.
    named = {row["security_id"] for row in candidates} | {row["symbol"] for row in exclusions}
    unacquired = sorted(watched - named - owned)
    for symbol in unacquired:
        exclusions.append(
            {
                "symbol": symbol,
                "name": None,
                "market_cap": None,
                "reasons": ["watchlist_not_acquired"],
            }
        )

    by_reason: dict[str, int] = {}
    for entry in exclusions:
        for reason in entry["reasons"]:
            by_reason[reason] = by_reason.get(reason, 0) + 1
    extras = []
    if watchlist_included:
        extras.append(
            f"{watchlist_included} watchlist symbol{'s' if watchlist_included > 1 else ''}"
        )
    if owned_retained:
        extras.append(f"{owned_retained} owned securit{'ies' if owned_retained > 1 else 'y'}")
    scope_label = (
        f"Largest {min(size, len(qualified))} eligible US ordinary common issuers by "
        f"Yahoo intraday market cap on {as_of}"
    )
    if extras:
        scope_label += ", plus " + " and ".join(extras)
    return {
        "candidates": candidates,
        "exclusions": exclusions,
        "notes": notes,
        "counts": {
            "acquired": len(rows or ()),
            "qualified": len(qualified),
            "in_scope": len(candidates),
            "excluded": len(exclusions),
            "size_limit": size,
            "watchlist_included": watchlist_included,
            "watchlist_not_acquired": len(unacquired),
            "owned_retained": owned_retained,
            "by_reason": by_reason,
        },
        "scope_label": scope_label,
        "method_version": METHOD_VERSION,
    }
