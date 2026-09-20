"""One captured amount, presented: its currency, its dated rate and the price behind it.

Everything here works on a single row of a statement. A holding's *quote* currency is the
currency of the exchange its ticker names; a holding's *value* currency is the one
currency its account reports every market value in. They are established separately and
neither is ever inferred by comparing quantity x price against the reported value -- that
arithmetic is only ever a check.

The check earns its keep by refusing rather than guessing, and by never declining in
silence. A value currency the account states or the source printed needs no corroboration;
one merely assumed -- defaulted from the account or from the review's presentation
currency -- is corroborated by the exchange whenever the exchange names that same
currency. When it names a different one, only quantity x price can say whether the source
already converted the value, so every way that arithmetic can fail to run -- no dated
rate, no price, no quantity -- is reported as ``VALUE_CURRENCY_UNCHECKED`` and the caller
withholds the amount instead of counting an unverified assumption into the presented
total. A listing that names no currency at all is the one exception: it is already an
open question about the listing, and the account is not in doubt about what it reports.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation, localcontext

PRECISION = 34
AMOUNT_STATUSES = frozenset({"converted", "identity", "unit_normalized"})
# How far quantity x quoted price may sit from the reported value before it is reported
# as a data-quality warning; wide enough for the gap between a source's own conversion
# rate and the dated observation, far narrower than any currency mistake.
RECONCILE_TOLERANCE = Decimal("0.02")
# Intermediates the reconciliation check may route a pair through when no provider
# published it directly, so a London listing valued in CAD is still testable.
CROSS_CURRENCIES = ("USD", "CAD")
# Bases on which a value currency is assumed rather than stated. A currency the source
# printed or the owner attested stands on its own; one of these stands on the check.
ASSUMED_BASES = frozenset({"presentation", "account"})
REFRESH_FX = "Refresh market data to load a dated FX observation."
STATE_CURRENCY = "State this source's reporting currency on the Data page to count it."
_CURRENCY = re.compile(r"[A-Za-z]{3}")


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


def _on_date(account, context) -> str:
    """Attested valuation date, else a current review's receipt day, else ``as_of``."""
    if account.get("valuation_date"):
        return account["valuation_date"]
    return context["receipt_day"] or context["as_of"]


def _price_major(position, security):
    price, factor = _number(position.get("price")), security.get("quote_unit_factor")
    if price is None or not security.get("quote_currency") or type(factor) is not int:
        return None
    with localcontext() as decimal_context:
        decimal_context.prec = PRECISION
        return price / factor


def _price_usd(position, currency, price_major, quote_major, on_date, context):
    """Presentation price, taken so that quantity x price reproduces the presented value.

    A quote currency that differs from the value's means the source itself converted:
    it quoted a Toronto listing in CAD and reported that holding's value in USD at its
    own rate. The price that value implies -- the value divided by the quantity -- is
    therefore the one that reconciles, whatever small gap there is between the source's
    rate and the dated observation. When both currencies agree -- or when the row carries
    no value for a price to be implied from -- there is no conversion to follow, so the
    quoted price converts on its own at that date's rate.
    """
    presentation = context["presentation"]
    if price_major is None:
        # Without listing metadata an unlabeled price is only ever taken in the value's
        # own unit, exactly as before normalization; reconcile's arithmetic check tests it.
        if quote_major is None and currency == presentation and currency is not None:
            return position.get("price")
        return None
    if currency is not None and quote_major is not None and currency != quote_major:
        in_value = _implied_price(position)
        if in_value is not None:
            if currency == presentation:
                return _text(in_value)
            amount, _, _ = _convert(_text(in_value), currency, on_date, context, report=False)
            return amount
        # No captured value, so there is no source conversion to follow and nothing to
        # imply a price from. The quoted price and its own dated rate are both in hand,
        # so the row still presents a price rather than losing it.
    if quote_major == presentation:
        return _text(price_major)
    amount, _, _ = _convert(_text(price_major), quote_major, on_date, context, report=False)
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


def _value_currency(position, account, context):
    """``(currency, basis)``: the currency this account reports its market values in.

    A source reports every holding's value in one currency, whatever each holding is
    quoted in -- the owner's Toronto names arrive priced in CAD and valued in USD on the
    same statement -- so this is a fact about the account, never about the ticker. The
    ticker's own currency is the quote currency and is established separately, from the
    listing. The row's own label wins, then an owner's attestation, then whatever
    currency the account itself states, and absent all of those the review's base
    currency, which is what a statement reports in unless it says otherwise.
    :func:`_reconciled` is what speaks up when that turns out to be wrong.

    An account's ``captured_cash_currency`` is deliberately not consulted here. It is the
    label observed on the account's *cash* row, and a cash balance's denomination says
    nothing about what a position's market value is denominated in; reading it here would
    re-denominate every holding in a multi-currency account from its cash line.
    :func:`_cash_currency` is its one legitimate reader.
    """
    currency = position.get("currency")
    if currency:
        basis = position.get("currency_basis")
        if basis not in ("row", "attested"):
            basis = "attested" if currency == account.get("position_currency") else "row"
        return currency, basis
    for field, basis in (
        ("position_currency", "attested"),
        ("currency", "account"),
    ):
        stated = account.get(field)
        if stated is not None and _major(stated) == stated:
            return stated, basis
    presentation = context["presentation"]
    return presentation, "presentation" if presentation is not None else None


def _cross_rate(fx, base, quote, on_date):
    """A rate for a pair no provider published, through one intermediate currency."""
    for middle in CROSS_CURRENCIES:
        if middle in (base, quote):
            continue
        first, second = fx.latest(base, middle, on_date), fx.latest(middle, quote, on_date)
        if first is None or second is None:
            continue
        with localcontext() as decimal_context:
            decimal_context.prec = PRECISION
            return Decimal(first["rate"]) * Decimal(second["rate"])
    return None


def _check_rate(quote_major, currency, on_date, context):
    """The quote-to-value rate the arithmetic check needs, directly or through one cross."""
    if quote_major == currency:
        return Decimal(1)
    found = context["fx"].latest(quote_major, currency, on_date)
    if found is not None:
        return Decimal(found["rate"])
    return _cross_rate(context["fx"], quote_major, currency, on_date)


def _unchecked_currency(position, why, remedy, context):
    """Report a value currency nothing available can confirm, and withhold the amount.

    Always returns ``True`` so a caller can both report and refuse in one statement:
    every route into here leaves the holding out of the presented total rather than
    counting an assumption no evidence stands behind.
    """
    context["issues"].append(
        {
            "code": "VALUE_CURRENCY_UNCHECKED",
            "message": (
                f"{position['raw_symbol']} {why}; the holding is left out of the presented "
                f"total. {remedy}"
            ),
            "severity": "warning",
        }
    )
    return True


NO_PRODUCT = "the captured row has nothing for quantity x price to check that against"


def _uncheckable(quantity, price_major):
    """Why quantity x price cannot be formed from this row, else ``None``."""
    if price_major is None:
        return "the captured row carries no price to check that against"
    if quantity is None:
        return "the captured row carries no quantity to check that against"
    if quantity == 0 or price_major == 0:
        return NO_PRODUCT
    return None


def _quoted_against(quote_major, currency, tail):
    """The shared phrasing for a holding quoted in one currency and valued in another."""
    return f"is quoted in {quote_major} but its value is taken as {currency}, and {tail}"


def _no_rate_why(quote_major, currency, on_date):
    return _quoted_against(
        quote_major,
        currency,
        f"no {quote_major}/{currency} rate dated on or before {on_date} is available to check that",
    )


def _mismatch(position, currency, quote_major):
    """Quantity x price and the reported value cannot all three be right."""
    return {
        "code": "VALUE_ARITHMETIC_MISMATCH",
        "message": (
            f"{position['raw_symbol']}: quantity x the price quoted in {quote_major} does "
            f"not reach the value reported in {currency} at that date's rate; check the "
            "source's quantity, price or reported value."
        ),
        "severity": "warning",
    }


def _reconciled(position, currency, basis, price_major, quote_major, on_date, context):
    """Check quantity x the quoted price against the reported value at that date.

    The listing says what a holding is quoted in and the account says what its values are
    reported in; this is the check that those two answers hold together. A mismatch
    decides nothing -- it is a data-quality warning about the captured row, never a
    question about the currency.

    An *assumed* value currency -- one defaulted from the account or the review rather
    than printed by the source or attested by the owner -- needs corroboration, and it
    has it whenever the exchange names the same currency the value is read in. When the
    exchange names a different one the row either was converted by the source or was not,
    and only this arithmetic can say which; so whenever the check is needed and cannot
    run -- no dated rate, no price, no quantity -- it is reported rather than passed
    over, and the caller withholds the amount instead of counting an unverified
    assumption into the presented total.
    """
    value = _number(position.get("market_value"))
    if value is None or currency is None or _major(currency) != currency:
        return False
    if quote_major is None or _major(quote_major) != quote_major:
        # A listing that states no currency is already one open exception in its own
        # right, and that is the only question it raises: a second warning here would
        # ask the owner about a value currency their account is not in doubt about.
        return False
    assumed = basis in ASSUMED_BASES
    quantity = _number(position.get("quantity"))
    # The exchange naming the same currency the value is read in is corroboration enough;
    # only a differing one leaves the assumption resting on this arithmetic.
    needed = assumed and quote_major != currency
    blocked = _uncheckable(quantity, price_major)
    if blocked:
        why = _quoted_against(quote_major, currency, blocked)
        return needed and _unchecked_currency(position, why, STATE_CURRENCY, context)
    rate = _check_rate(quote_major, currency, on_date, context)
    if rate is None:
        why = _no_rate_why(quote_major, currency, on_date)
        return _unchecked_currency(position, why, REFRESH_FX, context)
    with localcontext() as decimal_context:
        decimal_context.prec = PRECISION
        # Signs are kept so a short reconciles against its own negative value rather than
        # being skipped; only the tolerance is scaled by magnitude.
        expected = quantity * price_major * rate
        scale = abs(expected)
        if scale == 0:
            why = _quoted_against(quote_major, currency, NO_PRODUCT)
            return needed and _unchecked_currency(position, why, STATE_CURRENCY, context)
        if abs(value - expected) / scale <= RECONCILE_TOLERANCE:
            return False
    context["issues"].append(_mismatch(position, currency, quote_major))
    return False


def _valuation(position, account, context):
    if account.get("valuation_date"):
        return account["valuation_date"], "attested"
    if context["current"] and context["receipt_day"]:
        return context["receipt_day"], "collection_receipt"
    return position.get("valuation_date"), None
