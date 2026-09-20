"""USD presentation of collector holdings from listing identity and dated FX observations.

The ledger stays exact: every converted amount is a Decimal string beside the reported
amount and currency it came from. Two currencies are kept apart because a statement
keeps them apart. A holding's *quote* currency is the currency of the exchange its
ticker names -- the resolved listing states it, and :mod:`portfolio_research.venues`
answers from the ticker's venue suffix when no listing could be resolved. A holding's
*value* currency is the one currency its account reports every market value in, which
is why the owner's Toronto-quoted names arrive priced in CAD and reported in USD by the
same source. Neither is ever inferred by comparing quantity x price against the
reported value; that arithmetic is only ever a check, and a mismatch is a data-quality
warning, never a question about the currency.

This module assembles the review: it gathers listings and rates, applies identity, walks
the ledger and totals what was covered. One row at a time -- which currency its value is
in, what its dated rate is and what price is presented -- is
:mod:`portfolio_research.presentation`.

Nothing is converted twice: an amount already reported in the presentation currency is
used as reported. What cannot be established stays unconverted and becomes an explicit
exception, and a currency that cannot be checked is withheld rather than assumed.
"""

from __future__ import annotations

import copy
from datetime import date, datetime, timedelta
from decimal import Decimal, localcontext
from zoneinfo import ZoneInfo

import pandas as pd

from .presentation import (
    AMOUNT_STATUSES,
    _convert,
    _major,
    _on_date,
    _price_major,
    _price_usd,
    _reconciled,
    _text,
    _valuation,
    _value_currency,
)

METHOD_VERSION = "usd-presentation-1"
NEW_YORK = ZoneInfo("America/New_York")
LISTED_STATUSES = frozenset({"resolved", "resolved_from_display", "resolved_by_search"})
FX_HISTORY_PADDING_DAYS = 10
FX_FIELDS = ("fx_rate", "fx_pair", "fx_observation_date", "fx_source_id")
# Cash can be denominated apart from its account, so its conversion is evidenced apart too.
CASH_FX_FIELDS = (
    "cash_fx_rate",
    "cash_fx_pair",
    "cash_fx_observation_date",
    "cash_fx_source_id",
    "cash_fx_inverted",
)
COVERED_PRECISION = 512


def _live(config) -> bool:
    data = config.get("data", {})
    return bool(data.get("refresh_network")) and data.get("mode") == "live"


def _presentation(config) -> str | None:
    """The configured base currency; ``None`` means amounts stay as they were captured."""
    return config.get("mandate", {}).get("base_currency", "USD")


def _listing_key(position) -> str | None:
    for value in (position.get("quote_symbol"), position.get("raw_symbol")):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def gather_listings(bundle, config, *, refresh, issues) -> dict:
    """Listing metadata, a search callable and an issuer lookup for the held symbols.

    Returns ``{"listings", "search", "issuer_lookup"}``. Each distinct exact listing
    key (captured quote symbol, else display symbol) is looked up once. Display symbols
    without a listing are searched so an exact single match can be resolved and
    several candidates become an explicit choice; without ``refresh`` searches and
    lookups read the provider cache only. ``data.listing_provider: none`` gathers
    nothing.
    """
    from . import issuers, market_listings

    gathered = {"listings": {}, "search": None, "issuer_lookup": None}
    positions = (bundle.get("ledger") or {}).get("positions") or []
    if config.get("data", {}).get("listing_provider", "yahoo") != "yahoo" or not positions:
        return gathered
    listings, looked_up, searches = {}, {}, {}

    def lookup(symbol):
        if symbol not in looked_up:
            found = market_listings.listing_metadata(config, symbol, refresh=refresh, issues=issues)
            looked_up[symbol] = found
            if found is not None:
                listings[found.get("symbol") or symbol.upper()] = found
        return looked_up[symbol]

    def search(text):
        if text not in searches:
            searches[text] = market_listings.search_listings(
                config, text, refresh=refresh, issues=issues
            )
        return copy.deepcopy(searches[text])

    for position in positions:
        key = _listing_key(position)
        if key is None or lookup(key) is not None or position.get("quote_symbol"):
            continue
        wanted = key.upper()
        exact = [row for row in search(key) if str(row.get("symbol", "")).upper() == wanted]
        if len(exact) == 1:
            lookup(exact[0]["symbol"])
    gathered["listings"] = listings
    gathered["search"] = search
    if listings:
        gathered["issuer_lookup"] = issuers.sec_issuer_lookup(
            config, refresh=refresh, issues=issues
        )
    return gathered


def _find_listing(listings, key):
    if not isinstance(key, str) or not key.strip():
        return None
    return listings.get(key.strip()) or listings.get(key.strip().upper())


def fx_currencies(bundle, config, listings) -> list[str]:
    """Distinct major currencies the normalization may need, minus the presentation one.

    A holding whose listing has not been resolved still states its currency through its
    ticker's venue, and its rate has to be loaded here, before normalization runs, or the
    ordinary Toronto holding the venue table exists to serve would reach a CAD price with
    no CAD observation behind it.
    """
    from .venues import venue_currency

    ledger = bundle.get("ledger") or {}
    codes = set()
    for position in ledger.get("positions") or []:
        codes.add(_major(position.get("currency")))
        key = _listing_key(position)
        meta = _find_listing(listings, key)
        quote = _major(meta.get("quote_currency")) if meta else None
        codes.add(quote if quote is not None else venue_currency(key)[1])
    for account in ledger.get("accounts") or []:
        for field in ("currency", "position_currency", "captured_cash_currency"):
            codes.add(_major(account.get(field)))
    codes.discard(None)
    codes.discard(_presentation(config))
    return sorted(codes)


def fx_window(bundle, config) -> tuple[str, str]:
    """FX observation window: the price lookback before ``as_of`` through the receipt day."""
    as_of = date.fromisoformat(bundle["as_of"])
    years = int(config.get("data", {}).get("lookback_years", 5))
    start = as_of - timedelta(days=years * 365 + FX_HISTORY_PADDING_DAYS)
    end = (bundle.get("collector") or {}).get("receipt_through") or bundle["as_of"]
    return start.isoformat(), end


def _alias_key(row):
    return (row.get("source_id"), row.get("raw_symbol"), row.get("quote_symbol"))


def _apply_identity(ledger, identity):
    """Re-identify positions resolved to a listing; owner mappings and open ones keep ids."""
    aliases = {_alias_key(row): row for row in identity["aliases"]}
    renamed, positions = {}, []
    for position in ledger["positions"]:
        alias = aliases.get(_alias_key(position))
        if alias is None:
            positions.append({**position, "resolution_status": None})
            continue
        security_id = position["security_id"]
        if alias["resolution_status"] in LISTED_STATUSES:
            renamed[(position["account_id"], security_id)] = alias["security_id"]
            security_id = alias["security_id"]
        positions.append(
            {
                **position,
                "security_id": security_id,
                "resolution_status": alias["resolution_status"],
            }
        )
    security_aliases = []
    for row in ledger.get("security_aliases", []):
        alias = aliases.get(_alias_key(row))
        if alias is None:
            security_aliases.append(dict(row))
            continue
        security_aliases.append(
            {
                **row,
                "security_id": alias["security_id"],
                "resolution_status": alias["resolution_status"],
                "resolution_basis": alias["resolution_basis"],
            }
        )
    lots = [
        {
            **lot,
            "security_id": renamed.get((lot["account_id"], lot["security_id"]), lot["security_id"]),
        }
        for lot in ledger.get("tax_lots", [])
    ]
    return {
        **ledger,
        "positions": positions,
        "securities": identity["securities"],
        "security_aliases": security_aliases,
        "tax_lots": lots,
    }


def _receipt_day(bundle) -> str | None:
    received = (bundle.get("collector") or {}).get("collection_received_at")
    if not isinstance(received, str):
        return None
    try:
        moment = datetime.fromisoformat(received.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        return None
    return moment.astimezone(NEW_YORK).date().isoformat()


def _normalize_position(position, account, security, context):
    presentation, on_date = context["presentation"], _on_date(account, context)
    quote_currency = security.get("quote_currency")
    quote_major = _major(quote_currency)
    price_major = _price_major(position, security)
    currency, basis = _value_currency(position, account, context)
    reported = position.get("market_value")
    usd, fx_fields, _status = _convert(reported, currency, on_date, context)
    unchecked = _reconciled(position, currency, price_major, quote_major, on_date, context)
    price_usd = _price_usd(position, currency, price_major, quote_major, on_date, context)
    if unchecked:
        # The value currency could not be checked against the listing's, so nothing
        # derived from it is presented and the holding stays out of the covered total.
        usd, fx_fields, price_usd = None, {}, None
    valuation_date, valuation_basis = _valuation(position, account, context)
    return {
        **position,
        "reported_market_value": reported,
        "reported_price": position.get("price"),
        "reported_currency": currency,
        "value_currency_basis": basis,
        "quote_currency": quote_currency,
        "quote_unit_factor": security.get("quote_unit_factor"),
        "price_major": None if price_major is None else _text(price_major),
        "price_usd": price_usd,
        "market_value_usd": usd,
        **dict.fromkeys(FX_FIELDS),
        "fx_inverted": None,
        **fx_fields,
        "presentation_currency": presentation,
        "market_value": usd,
        "price": price_usd,
        "currency": presentation if usd is not None else currency,
        "valuation_date": valuation_date,
        "valuation_basis": valuation_basis,
    }


def _inferred(context, code, currency, subject):
    context["issues"].append(
        {
            "code": code,
            "message": (
                f"An account's {subject} currency was inferred as {currency} because every "
                "holding in it is valued in that currency; attest the account currency to confirm."
            ),
            "severity": "warning",
        }
    )


def _held_currency(held):
    """The one value currency every holding in an account shares, else ``None``."""
    values = {row["reported_currency"] for row in held}
    if not held or len(values) != 1 or None in values:
        return None
    return values.pop()


def _account_currency(account, held, context):
    """The account's own currency: attested, else the capture's, else its holdings'."""
    if account.get("currency"):
        return account["currency"], "attested"
    if account.get("captured_cash_currency"):
        return account["captured_cash_currency"], "captured_cash"
    currency = _held_currency(held)
    if currency is None:
        return None, None
    _inferred(context, "INFERRED_ACCOUNT_CURRENCY", currency, "cash and NAV")
    return currency, "inferred_from_positions"


def _captured_cash(account):
    """Whether the account's cash is the captured amount rather than an attested one."""
    return account.get("cash_basis") == "captured"


def _cash_currency(account, held, currency, basis, context):
    """The currency of the cash amount itself, which may differ from the account's.

    Captured cash is denominated where it was captured: the capture's own currency, else
    the currency its account's holdings were established in. Only an attested cash amount
    takes the attested account currency.
    """
    if account.get("cash") is None or not _captured_cash(account):
        return currency, basis
    if account.get("captured_cash_currency"):
        return account["captured_cash_currency"], "captured_cash"
    held_currency = _held_currency(held)
    if held_currency is None or held_currency == currency:
        return currency, basis
    _inferred(context, "INFERRED_CASH_CURRENCY", held_currency, "captured cash")
    return held_currency, "inferred_from_positions"


def _cash_evidence(cash_fx):
    """The cash conversion's own FX evidence, kept apart from the NAV's rate."""
    return {f"cash_{name}": value for name, value in (cash_fx or {}).items()}


def _normalize_account(account, held, context):
    """``(account row, converted cash or None, unconverted flag)``."""
    currency, basis = _account_currency(account, held, context)
    cash_currency, cash_basis = _cash_currency(account, held, currency, basis, context)
    on_date = _on_date(account, context)
    cash, cash_fx, cash_status = _convert(account.get("cash"), cash_currency, on_date, context)
    nav, nav_fx, nav_status = _convert(account.get("total_value"), currency, on_date, context)
    succeeded = AMOUNT_STATUSES | {"reported", "no_amount"}
    known = currency is not None and (account.get("cash") is None or cash_currency is not None)
    converted = known and {cash_status, nav_status} <= succeeded
    row = {
        **account,
        "reported_cash": account.get("cash"),
        "reported_total_value": account.get("total_value"),
        "reported_currency": currency,
        "currency_basis": basis,
        "cash_currency": cash_currency,
        "cash_currency_basis": cash_basis,
        "cash_usd": cash,
        "total_value_usd": nav,
        **dict.fromkeys(FX_FIELDS),
        "fx_inverted": None,
        # The row's own rate evidences its NAV; it stands in for cash only when no NAV
        # was converted, so a reader never reconciles a NAV against the cash's pair.
        **(nav_fx or cash_fx),
        **dict.fromkeys(CASH_FX_FIELDS),
        **_cash_evidence(cash_fx),
        "presentation_currency": context["presentation"],
        "currency": currency,
    }
    if converted:
        row.update(cash=cash, total_value=nav, currency=context["presentation"])
    has_amount = account.get("cash") is not None or account.get("total_value") is not None
    return row, cash, has_amount and not converted


def _unique_exceptions(*groups):
    unique = {}
    for group in groups:
        for record in group:
            unique.setdefault(record["key"], record)
    return list(unique.values())


def _quote_currencies(ledger):
    """Quote currency per resolved security, when it differs from the major unit."""
    quotes = {}
    for row in ledger.get("securities") or []:
        quote = row.get("quote_currency")
        if isinstance(quote, str) and quote and quote != row.get("currency"):
            quotes[row.get("security_id")] = quote
    return quotes


def present_prices(bundle, config, fx):
    """Convert an enriched price series to the presentation currency at each row's date.

    A price connector that does not state its own unit labels every close with the
    security's major currency, but a listing quoted in a subunit reports pence or cents,
    so those rows are relabelled with the listing's quote currency first and the
    conversion divides by its unit factor. A row that carries its own
    ``quote_unit_factor`` has already been expressed in major units by the adapter and
    keeps the currency it states. Without a presentation currency the series is left
    exactly as it was collected.
    """
    from .fx import convert_price_series

    presentation = _presentation(config)
    prices = bundle.get("prices")
    required = {"security_id", "date", "close", "adjusted_close", "currency"}
    if presentation is None or not isinstance(prices, pd.DataFrame) or prices.empty:
        return bundle
    if required - set(prices.columns):
        return bundle
    quotes = _quote_currencies(bundle.get("ledger") or {})
    frame = prices.copy()
    if quotes:
        stated = (
            frame["quote_unit_factor"].notna()
            if "quote_unit_factor" in frame.columns
            else pd.Series(False, index=frame.index)
        )
        frame["currency"] = [
            currency if own else quotes.get(security, currency)
            for security, currency, own in zip(
                frame["security_id"], frame["currency"], stated, strict=True
            )
        ]
    converted, issues = convert_price_series(frame, fx, to_currency=presentation)
    return {**bundle, "prices": converted, "issues": [*(bundle.get("issues") or []), *issues]}


def _identified(bundle, config, sources, supplemental, issues):
    """``(ledger with listing identity applied, identity result)`` for a collector bundle.

    Cache-only runs index each provider directory once instead of re-reading it per
    symbol, so a presentation costs one scan however many holdings there are.
    """
    from . import market_listings
    from .provider_cache import scanned

    if _live(config):
        return _identify(bundle, config, sources, supplemental, issues)
    with scanned(config, market_listings.LISTING_PROVIDER, market_listings.SEARCH_PROVIDER):
        return _identify(bundle, config, sources, supplemental, issues)


def _identify(bundle, config, sources, supplemental, issues):
    from .identity import resolve_identities

    listings, search, issuer_lookup = sources
    if listings is None:
        gathered = gather_listings(bundle, config, refresh=_live(config), issues=issues)
        listings = gathered["listings"]
        search = search if search is not None else gathered["search"]
        issuer_lookup = issuer_lookup if issuer_lookup is not None else gathered["issuer_lookup"]
    ledger = copy.deepcopy(bundle["ledger"])
    identity = resolve_identities(
        {"positions": ledger["positions"], "securities": ledger["securities"]},
        supplemental,
        listings=listings,
        search=search,
        issuer_lookup=issuer_lookup,
    )
    return _apply_identity(ledger, identity), identity


def _context(bundle, config, fx, review_kind, issues):
    return {
        "fx": fx,
        "presentation": _presentation(config),
        "current": review_kind == "current",
        "receipt_day": _receipt_day(bundle) if review_kind == "current" else None,
        "as_of": bundle["as_of"],
        "used": set(),
        "fx_exceptions": {},
        "exceptions": [],
        "issues": issues,
    }


def _normalize_rows(ledger, context):
    """``(positions, accounts, converted cash amounts, unconverted account ids)``."""
    securities = {row["security_id"]: row for row in ledger["securities"]}
    accounts = {row["account_id"]: row for row in ledger["accounts"]}
    positions = [
        _normalize_position(
            position,
            accounts.get(position["account_id"], {}),
            securities.get(position["security_id"], {}),
            context,
        )
        for position in ledger["positions"]
    ]
    rows, covered_cash, unconverted = [], [], []
    for account in ledger["accounts"]:
        held = [row for row in positions if row["account_id"] == account["account_id"]]
        row, cash, unconverted_account = _normalize_account(account, held, context)
        rows.append(row)
        if cash is not None:
            covered_cash.append(Decimal(cash))
        if unconverted_account:
            unconverted.append(account["account_id"])
    return positions, rows, covered_cash, unconverted


def _covered(positions, covered_cash):
    with localcontext() as decimal_context:
        decimal_context.prec = COVERED_PRECISION
        total = sum(
            (Decimal(row["market_value_usd"]) for row in positions if row["market_value_usd"]),
            Decimal(0),
        ) + sum(covered_cash, Decimal(0))
    return _text(total)


def _summary(context, positions, unconverted_accounts, covered, review_kind, generated_at):
    return {
        "presentation_currency": context["presentation"],
        "method_version": METHOD_VERSION,
        "covered_value_usd": covered,
        "unconverted_positions": sum(row["market_value_usd"] is None for row in positions),
        "unconverted_accounts": len(unconverted_accounts),
        "unconverted_account_ids": unconverted_accounts,
        "review_kind": review_kind,
        "generated_at": generated_at,
    }


def _unpresented_position(position, account, security, context):
    """A position kept exactly as collected: identified, dated, but never converted."""
    currency = position.get("currency")
    valuation_date, valuation_basis = _valuation(position, account, context)
    return {
        **position,
        "reported_market_value": position.get("market_value"),
        "reported_price": position.get("price"),
        "reported_currency": currency,
        "value_currency_basis": "row" if currency else None,
        "quote_currency": security.get("quote_currency"),
        "quote_unit_factor": security.get("quote_unit_factor"),
        "price_major": None,
        "price_usd": None,
        "market_value_usd": None,
        **dict.fromkeys(FX_FIELDS),
        "fx_inverted": None,
        "presentation_currency": None,
        "valuation_date": valuation_date,
        "valuation_basis": valuation_basis,
    }


def _unpresented_account(account):
    return {
        **account,
        "reported_cash": account.get("cash"),
        "reported_total_value": account.get("total_value"),
        "reported_currency": account.get("currency"),
        "currency_basis": "attested" if account.get("currency") else None,
        "cash_currency": account.get("currency") or account.get("captured_cash_currency"),
        "cash_currency_basis": None,
        "cash_usd": None,
        "total_value_usd": None,
        **dict.fromkeys(FX_FIELDS),
        "fx_inverted": None,
        "presentation_currency": None,
    }


def _unpresented(ledger, context, review_kind, generated_at):
    """Rows and summary when no base currency is configured: nothing is converted."""
    securities = {row["security_id"]: row for row in ledger["securities"]}
    accounts = {row["account_id"]: row for row in ledger["accounts"]}
    positions = [
        _unpresented_position(
            position,
            accounts.get(position["account_id"], {}),
            securities.get(position["security_id"], {}),
            context,
        )
        for position in ledger["positions"]
    ]
    context["issues"].append(
        {
            "code": "NO_PRESENTATION_CURRENCY",
            "severity": "warning",
            "message": (
                "No base currency is configured, so captured amounts are shown as collected "
                "and no USD presentation was computed."
            ),
        }
    )
    rows = [_unpresented_account(account) for account in ledger["accounts"]]
    summary = _summary(context, positions, [], None, review_kind, generated_at)
    return positions, rows, summary


def normalize_bundle(
    bundle,
    config,
    *,
    fx,
    review_kind,
    generated_at=None,
    supplemental=None,
    listings=None,
    search=None,
    issuer_lookup=None,
) -> dict:
    """Resolve listing identity and present a collector bundle in the base currency.

    Returns a new bundle; the input is never mutated. ``listings``/``search``/
    ``issuer_lookup`` may be injected; when ``listings`` is omitted they are gathered
    with :func:`gather_listings` (provider cache only unless the run refreshes live).
    ``supplemental`` keeps an owner's dated security mappings authoritative. Without a
    configured base currency identity is still resolved and nothing is converted.
    """
    from .calendar import REVIEW_KINDS
    from .fx import FxTable

    if review_kind not in REVIEW_KINDS:
        raise ValueError("Review kind must be current or historical.")
    if not isinstance(fx, FxTable):
        raise ValueError("fx must be an FxTable of dated observations.")
    if not isinstance(bundle, dict) or not isinstance(bundle.get("ledger"), dict):
        raise ValueError("Normalization requires a collector bundle with an exact ledger.")
    issues = copy.deepcopy(bundle.get("issues") or [])
    sources = (listings, search, issuer_lookup)
    ledger, identity = _identified(bundle, config, sources, supplemental, issues)
    context = _context(bundle, config, fx, review_kind, issues)
    ledger, summary = _presented_ledger(ledger, context, review_kind, generated_at)
    result = _presented_bundle(bundle, ledger, identity, context, fx, summary, issues)
    return present_prices(result, config, fx)


def _presented_ledger(ledger, context, review_kind, generated_at):
    """``(ledger with presented rows, normalization summary)``."""
    if context["presentation"] is None:
        positions, accounts, summary = _unpresented(ledger, context, review_kind, generated_at)
    else:
        positions, accounts, cash, unconverted = _normalize_rows(ledger, context)
        summary = _summary(
            context, positions, unconverted, _covered(positions, cash), review_kind, generated_at
        )
    return {**ledger, "positions": positions, "accounts": accounts}, summary


def _presented_bundle(bundle, ledger, identity, context, fx, summary, issues):
    """The bundle a review sees: exact ledger, rebuilt frames, exceptions and evidence."""
    from .adapter import _frames

    return {
        **bundle,
        **_frames(ledger, issues),
        "ledger": ledger,
        "issues": issues,
        "identity": identity["counts"],
        "exceptions": _unique_exceptions(
            identity["exceptions"], context["exceptions"], context["fx_exceptions"].values()
        ),
        "fx_observations": [
            record
            for record in fx.to_records()
            if (record["pair"], record["date"]) in context["used"]
        ],
        "normalization": summary,
    }
