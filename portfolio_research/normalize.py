"""USD presentation of collector holdings from listing identity and dated FX observations.

The ledger stays exact: every converted amount is a Decimal string beside the reported
amount and currency it came from. A value currency is taken from the source row, an
owner's attestation, or quantity x price arithmetic against the listing's quote
currency (directly, or through one fresh dated FX observation). Nothing is converted
twice: an amount already reported in the presentation currency is used as reported.
What cannot be established stays unconverted and becomes an explicit exception.
"""

from __future__ import annotations

import copy
import re
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, localcontext
from zoneinfo import ZoneInfo

import pandas as pd

METHOD_VERSION = "usd-presentation-1"
PRECISION = 34
QUOTE_TOLERANCE = Decimal("0.005")
FX_TOLERANCE = Decimal("0.01")
NEW_YORK = ZoneInfo("America/New_York")
AMOUNT_STATUSES = frozenset({"converted", "identity", "unit_normalized"})
LISTED_STATUSES = frozenset({"resolved", "resolved_from_display", "resolved_by_search"})
FALLBACK_CANDIDATE = "CAD"
# A pair no provider published is crossed through one of these, both legs dated.
CROSS_CURRENCIES = ("USD", "CAD")
FX_HISTORY_PADDING_DAYS = 10
_CURRENCY = re.compile(r"[A-Za-z]{3}")
FX_FIELDS = ("fx_rate", "fx_pair", "fx_observation_date", "fx_source_id")
EVIDENCE_FIELDS = (
    "value_currency_ratio",
    "value_currency_implied_rate",
    "value_currency_fx_pair",
    "value_currency_fx_rate",
    "value_currency_fx_date",
    "value_currency_fx_source_id",
)
COVERED_PRECISION = 512


def _live(config) -> bool:
    data = config.get("data", {})
    return bool(data.get("refresh_network")) and data.get("mode") == "live"


def _presentation(config) -> str | None:
    """The configured base currency; ``None`` means amounts stay as they were captured."""
    return config.get("mandate", {}).get("base_currency", "USD")


def _text(number: Decimal) -> str:
    return format(number, "f")


def _number(value) -> Decimal | None:
    if value is None:
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _major(code) -> str | None:
    """Major currency of a valid code (``GBp`` -> ``GBP``); ``None`` when unusable."""
    from .market_listings import listing_currency

    if not isinstance(code, str) or not _CURRENCY.fullmatch(code):
        return None
    return listing_currency(code)[1]


def _convertible(code) -> bool:
    """A code the FX table accepts: an uppercase major currency or a known subunit."""
    from .fx import MINOR_UNITS

    return isinstance(code, str) and (code in MINOR_UNITS or re.fullmatch(r"[A-Z]{3}", code))


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
    """Distinct major currencies the normalization may need, minus the presentation one."""
    ledger = bundle.get("ledger") or {}
    positions = ledger.get("positions") or []
    codes, arithmetic = set(), False
    for position in positions:
        codes.add(_major(position.get("currency")))
        meta = _find_listing(listings, _listing_key(position))
        quote = _major(meta.get("quote_currency")) if meta else None
        codes.add(quote)
        arithmetic = arithmetic or (quote is not None and not position.get("currency"))
    for account in ledger.get("accounts") or []:
        for field in ("currency", "position_currency", "captured_cash_currency"):
            codes.add(_major(account.get(field)))
    codes.discard(None)
    if arithmetic:
        # CAD is always a value-currency candidate for quantity x price arithmetic.
        codes.add(FALLBACK_CANDIDATE)
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


def _exception(
    code, message, scope, resolution, *, position=None, key_pair=None, proposed=None, **fields
):
    """An owner-facing exception record; ``key_pair`` scopes FX keys to a pair and date."""
    from .identity import exception_key

    position = position or {}
    source_id, snapshot_id = position.get("source_id"), position.get("snapshot_id")
    raw_symbol = position.get("raw_symbol")
    return {
        "key": exception_key(code, source_id, snapshot_id, raw_symbol, key_pair),
        "code": code,
        "severity": "error",
        "scope": scope,
        "source_id": source_id,
        "snapshot_id": snapshot_id,
        "account_id": position.get("account_id"),
        "raw_symbol": raw_symbol,
        "message": message,
        "proposed": proposed,
        "candidates": None,
        "resolution": resolution,
        **fields,
    }


def _fx_exception(result, context):
    code = "STALE_FX" if result["status"] == "stale" else "MISSING_FX"
    pair, day = result["pair"], result["requested_date"]
    if (code, pair, day) in context["fx_exceptions"]:
        return
    issue = result["issues"][0] if result["issues"] else {}
    message = issue.get("message") or f"{pair}: no usable dated observation on {day}."
    context["fx_exceptions"][(code, pair, day)] = _exception(
        code,
        message + " Refresh market data to load a dated FX observation.",
        "fx",
        {"kind": "fx_manual", "fields": ["rate", "observation_date"]},
        key_pair=f"{pair}:{day}",
        pair=pair,
        requested_date=day,
        observation_date=result.get("observation_date"),
        age_days=result.get("age_days"),
    )


def _use_observation(context, observed_pair, observation_date):
    if observed_pair and observation_date:
        context["used"].add((observed_pair, observation_date))


def _convert(amount, currency, on_date, context, *, report=True):
    """Presentation amount of an exact amount: ``(amount | None, fx fields, status)``."""
    presentation = context["presentation"]
    if amount is None:
        return None, {}, "no_amount"
    if presentation is None or not _convertible(currency):
        # An unlabelled amount is never "already in the presentation currency", and
        # without a presentation currency nothing is presented at all.
        return None, {}, "unknown_currency"
    if currency == presentation:
        return amount, {}, "reported"
    result = context["fx"].convert(amount, currency, presentation, on_date)
    if result["status"] in AMOUNT_STATUSES and result["amount"] is not None:
        if result["status"] != "converted":
            return result["amount"], {}, result["status"]
        _use_observation(context, result["observed_pair"], result["observation_date"])
        fields = {
            "fx_rate": result["rate"],
            "fx_pair": result["pair"],
            "fx_observation_date": result["observation_date"],
            "fx_source_id": result["source_id"],
            "fx_inverted": result["inverted"],
        }
        return result["amount"], fields, "converted"
    if report:
        _fx_exception(result, context)
    return None, {}, result["status"]


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


def _on_date(account, context) -> str:
    """Attested valuation date, else a current review's receipt day, else ``as_of``."""
    if account.get("valuation_date"):
        return account["valuation_date"]
    return context["receipt_day"] or context["as_of"]


def _fresh(observation, on_date, fx) -> bool:
    age = (date.fromisoformat(on_date) - date.fromisoformat(observation["date"])).days
    return 0 <= age <= fx.max_age_days


def _candidates(quote_major, account, context):
    wanted = [
        context["presentation"],
        account.get("currency"),
        account.get("captured_cash_currency"),
        FALLBACK_CANDIDATE,
    ]
    unique = []
    for code in wanted:
        if code is None or code == quote_major or code in unique or _major(code) != code:
            continue
        unique.append(code)
    return unique


def _attested(account):
    """The currency an owner or the capture itself already stated for this account."""
    for code in (account.get("currency"), account.get("captured_cash_currency")):
        if _major(code) == code and code is not None:
            return code
    return None


def _cross(fx, base, quote, on_date):
    """A rate for a pair no provider published, through one intermediate currency."""
    for middle in CROSS_CURRENCIES:
        if middle in (base, quote):
            continue
        first, second = fx.latest(base, middle, on_date), fx.latest(middle, quote, on_date)
        if first is None or second is None:
            continue
        with localcontext() as decimal_context:
            decimal_context.prec = PRECISION
            rate = _text(Decimal(first["rate"]) * Decimal(second["rate"]))
        return {
            "pair": base + quote,
            "base": base,
            "quote": quote,
            "rate": rate,
            "date": min(first["date"], second["date"]),
            "source_id": f"{first['source_id']}+{second['source_id']}",
            "provider": first["provider"],
            "observed_pair": None,
            "derived_via": middle,
            "legs": [
                (first["observed_pair"], first["date"]),
                (second["observed_pair"], second["date"]),
            ],
        }
    return None


def _candidate_rate(fx, base, quote, on_date):
    """``(observation | None, "fresh" | "stale")`` for a candidate value currency."""
    found = fx.latest(base, quote, on_date) or _cross(fx, base, quote, on_date)
    if found is None:
        return None, "missing"
    return found, "fresh" if _fresh(found, on_date, fx) else "stale"


def _undecided(ratio=None, **fields):
    return {
        "currency": None,
        "basis": None,
        "ratio": ratio,
        "observation": None,
        "unavailable_pair": None,
        "candidates": None,
        **fields,
    }


def _ambiguous(ratio, quote_major, matches):
    """Two currencies explain the same value; record both and decide nothing."""
    candidates = [{"currency": quote_major, "basis": "arithmetic_quote"}]
    candidates += [
        {
            "currency": currency,
            "basis": "arithmetic_fx",
            "pair": found["pair"],
            "rate": found["rate"],
            "observation_date": found["date"],
        }
        for currency, found in matches
    ]
    return _undecided(ratio, candidates=candidates)


def _explained(ratio, quote_major, account, on_date, context):
    """Candidate value currencies whose dated rate explains ``ratio``, and what was missing."""
    fx, matches, stale_pair, unusable = context["fx"], [], None, None
    for candidate in _candidates(quote_major, account, context):
        found, freshness = _candidate_rate(fx, quote_major, candidate, on_date)
        pair = (quote_major, candidate)
        if found is None:
            unusable = unusable or pair
            continue
        rate = Decimal(found["rate"])
        close = abs(ratio - rate) / rate <= FX_TOLERANCE
        if freshness == "stale":
            unusable = unusable or pair
            stale_pair = stale_pair or (pair if close else None)
            continue
        if close:
            matches.append((candidate, found))
    return matches, stale_pair or unusable


def _arithmetic(quantity, price_major, value, quote_major, account, on_date, context):
    """Decide the value currency from quantity x price, or say what stopped the decision.

    A value matching the quote currency needs no FX, unless a dated rate explains it just
    as well (currencies near parity): then nothing is assumed. Otherwise the first
    candidate currency whose fresh observation explains the ratio within 1% is the value
    currency. A missing or stale rate leaves the pair to report, so the exception asks for
    a market-data refresh instead of an attestation.
    """
    if None in (quantity, price_major, value, quote_major):
        return _undecided()
    with localcontext() as decimal_context:
        decimal_context.prec = PRECISION
        product = quantity * price_major
        if product <= 0:
            return _undecided()
        ratio = value / product
        matches, unavailable = _explained(ratio, quote_major, account, on_date, context)
        if abs(ratio - 1) <= QUOTE_TOLERANCE:
            return _quote_decision(ratio, quote_major, account, matches)
        if matches:
            currency, found = matches[0]
            return _undecided(ratio, currency=currency, basis="arithmetic_fx", observation=found)
        if unavailable is not None:
            return _undecided(ratio, unavailable_pair=unavailable)
        return _undecided(ratio)


def _quote_decision(ratio, quote_major, account, matches):
    """A near-parity ratio: the quote currency, unless a dated rate explains it too."""
    if not matches:
        return _undecided(ratio, currency=quote_major, basis="arithmetic_quote")
    attested = _attested(account)
    if attested == quote_major:
        return _undecided(ratio, currency=quote_major, basis="arithmetic_quote")
    for currency, found in matches:
        if currency == attested:
            return _undecided(ratio, currency=currency, basis="arithmetic_fx", observation=found)
    return _ambiguous(ratio, quote_major, matches)


def _price_major(position, security):
    price, factor = _number(position.get("price")), security.get("quote_unit_factor")
    if price is None or not security.get("quote_currency") or type(factor) is not int:
        return None
    with localcontext() as decimal_context:
        decimal_context.prec = PRECISION
        return price / factor


def _price_usd(position, decision, price_major, quote_major, on_date, context):
    """Presentation price consistent with how the value itself was established.

    An arithmetic basis accepted the source's own conversion, so the price it implies is
    the value divided by the quantity: ``quantity x price_usd`` then equals the presented
    value exactly, whatever small gap there was between the source's rate and the dated
    observation. A row or attested currency converts the quoted price on its own and lets
    reconcile's arithmetic check speak.
    """
    presentation, currency, basis = context["presentation"], decision["currency"], decision["basis"]
    if price_major is None:
        # Without listing metadata an unlabeled price is only ever taken in the value's
        # own unit, exactly as before normalization; reconcile's arithmetic check tests it.
        if quote_major is None and currency == presentation and currency is not None:
            return position.get("price")
        return None
    if basis in ("arithmetic_quote", "arithmetic_fx"):
        in_value = _implied_price(position)
        if in_value is None:
            return None
    elif quote_major == presentation:
        return _text(price_major)
    else:
        amount, _, _ = _convert(_text(price_major), quote_major, on_date, context, report=False)
        return amount
    if currency == presentation:
        return _text(in_value)
    amount, _, _ = _convert(_text(in_value), currency, on_date, context, report=False)
    return amount


def _implied_price(position):
    """The per-unit price the captured value itself implies, in the value's currency."""
    quantity = _number(position.get("quantity"))
    value = _number(position.get("market_value"))
    if quantity is None or value is None or quantity == 0:
        return None
    with localcontext() as decimal_context:
        decimal_context.prec = PRECISION
        return value / quantity


def _unknown_value_currency(position, decision, quote_currency):
    ratio = decision["ratio"]
    shown = "unknown" if ratio is None else _text(ratio)
    candidates = decision["candidates"]
    if candidates:
        listed = " or ".join(row["currency"] for row in candidates)
        detail = (
            f"could be {listed}: the captured value is explained by the quote currency and by a "
            f"dated rate alike (value/(quantity x price) ratio {shown})"
        )
    else:
        detail = (
            f"could not be established (quote currency {quote_currency or 'unknown'}, "
            f"value/(quantity x price) ratio {shown})"
        )
    message = (
        f"{position['raw_symbol']}: the currency of the captured value {detail}. "
        "Attest the currency this account's values are reported in."
    )
    return _exception(
        "UNKNOWN_VALUE_CURRENCY",
        message,
        "position",
        {"kind": "account_currency", "fields": ["position_currency"]},
        position=position,
        proposed={
            "source_id": position.get("source_id"),
            "snapshot_id": position.get("snapshot_id"),
            "position_currency": None,
        },
        ratio=None if ratio is None else _text(ratio),
        quote_currency=quote_currency,
        candidates=candidates,
    )


def _value_currency(position, account, price_major, quote_major, on_date, context):
    """Decision dict for the value currency: row, attestation, then arithmetic."""
    currency = position.get("currency")
    if currency:
        basis = position.get("currency_basis")
        if basis not in ("row", "attested"):
            basis = "attested" if currency == account.get("position_currency") else "row"
        return _undecided(currency=currency, basis=basis)
    quantity, value = _number(position.get("quantity")), _number(position.get("market_value"))
    return _arithmetic(quantity, price_major, value, quote_major, account, on_date, context)


def _valuation(position, account, context):
    if account.get("valuation_date"):
        return account["valuation_date"], "attested"
    if context["current"] and context["receipt_day"]:
        return context["receipt_day"], "collection_receipt"
    return position.get("valuation_date"), None


def _evidence(decision, context):
    """Ledger fields naming the dated observation and the rate the source itself implied."""
    observation, ratio = decision["observation"], decision["ratio"]
    fields = {
        "value_currency_ratio": None if ratio is None else _text(ratio),
        "value_currency_implied_rate": None,
        "value_currency_fx_pair": None,
        "value_currency_fx_rate": None,
        "value_currency_fx_date": None,
        "value_currency_fx_source_id": None,
    }
    if decision["basis"] == "arithmetic_fx" and ratio is not None:
        fields["value_currency_implied_rate"] = _text(ratio)
    if observation is None:
        return fields
    _use_evidence(context, observation)
    return {
        **fields,
        "value_currency_fx_pair": observation["pair"],
        "value_currency_fx_rate": observation["rate"],
        "value_currency_fx_date": observation["date"],
        "value_currency_fx_source_id": observation["source_id"],
    }


def _use_evidence(context, observation):
    for pair, day in observation.get("legs") or [
        (observation.get("observed_pair"), observation["date"])
    ]:
        _use_observation(context, pair, day)


def _normalize_position(position, account, security, context):
    presentation, on_date = context["presentation"], _on_date(account, context)
    quote_currency = security.get("quote_currency")
    quote_major = _major(quote_currency)
    price_major = _price_major(position, security)
    decision = _value_currency(position, account, price_major, quote_major, on_date, context)
    currency = decision["currency"]
    evidence = _evidence(decision, context)
    reported = position.get("market_value")
    usd, fx_fields, status = _convert(reported, currency, on_date, context)
    if reported is not None and status == "unknown_currency":
        pair = decision["unavailable_pair"]
        if pair is not None:
            _fx_exception(context["fx"].convert("1", *pair, on_date), context)
        else:
            context["exceptions"].append(
                _unknown_value_currency(position, decision, quote_currency)
            )
    price_usd = _price_usd(position, decision, price_major, quote_major, on_date, context)
    valuation_date, valuation_basis = _valuation(position, account, context)
    return {
        **position,
        "reported_market_value": reported,
        "reported_price": position.get("price"),
        "reported_currency": currency,
        "value_currency_basis": decision["basis"],
        **evidence,
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
        **(cash_fx or nav_fx),
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

    Enrichment labels every close with the security's major currency, but a listing quoted
    in a subunit reports pence or cents, so rows are relabelled with the listing's quote
    currency first and the conversion divides by its unit factor. Without a presentation
    currency the series is left exactly as it was collected.
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
        frame["currency"] = [
            quotes.get(security, currency)
            for security, currency in zip(frame["security_id"], frame["currency"])
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
        **dict.fromkeys(EVIDENCE_FIELDS),
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
