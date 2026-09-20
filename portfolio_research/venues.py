"""A holding's quote currency read from its ticker, the owner's stated rule.

The owner's instruction, written down once so nothing re-derives it: *know the currency
from the ticker*. ``VOO`` is a bare symbol, so it trades on a US exchange and is quoted
in USD; ``VFV.TO`` carries Toronto's suffix, so it is quoted in CAD. The suffix names the
venue and the venue fixes the currency -- no arithmetic, no comparing a reported value
against quantity x price, no prompt.

Provider listing metadata stays the first authority, because a resolved listing states
its own quote currency (and its subunit, which is how a London listing reports pence).
This table is what answers when no listing could be resolved -- offline, a cache miss, an
unknown symbol -- so an ordinary holding still gets the currency its ticker plainly
states while its listing exception stays open.
"""

from .market_listings import listing_currency

# The venue each ticker suffix names, and the currency that venue quotes in.
VENUE_CURRENCY = {
    "TO": "CAD",  # Toronto Stock Exchange (TSX)
    "V": "CAD",  # TSX Venture Exchange
    "NE": "CAD",  # Cboe Canada, formerly the NEO Exchange
    "CN": "CAD",  # Canadian Securities Exchange (CSE)
    "L": "GBp",  # London Stock Exchange, quoted in pence, not pounds
    "SW": "CHF",  # SIX Swiss Exchange
}
# A bare symbol is a US listing: NYSE, Nasdaq, NYSE American and Arca, Cboe US.
US_CURRENCY = "USD"
# Dotted suffixes that mark a US share class rather than a venue: BRK.A, BRK.B, BF.B and
# the preferred-share marker. These stay US listings; market_values.SHARE_CLASS_SUFFIXES
# is what decides whether they are ordinary common stock.
US_SHARE_CLASS_SUFFIXES = frozenset({"A", "B", "C", "PR"})
# Not holdings at all: ``^GSPC`` is an index and ``CADUSD=X`` an FX pseudo-ticker. Neither
# is owned in an account, so neither is assigned a holding currency.
INDEX_PREFIX = "^"
FX_SUFFIX = "=X"


def venue_currency(symbol):
    """Return ``(quote_currency, major_currency, unit_factor)`` for a ticker's venue.

    Mirrors :func:`portfolio_research.market_listings.listing_currency` so a resolved
    listing and this fallback are interchangeable at the call site. A symbol whose venue
    is not stated here -- an unknown suffix, an index, an FX pseudo-ticker -- returns
    ``(None, None, None)`` and decides nothing.
    """
    if not isinstance(symbol, str):
        return None, None, None
    text = symbol.strip().upper()
    if not text or text.startswith(INDEX_PREFIX) or text.endswith(FX_SUFFIX):
        return None, None, None
    head, dot, suffix = text.rpartition(".")
    if not dot:
        return listing_currency(US_CURRENCY)
    if not head:
        return None, None, None
    if suffix in VENUE_CURRENCY:
        return listing_currency(VENUE_CURRENCY[suffix])
    if suffix in US_SHARE_CLASS_SUFFIXES:
        return listing_currency(US_CURRENCY)
    return None, None, None
