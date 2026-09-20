"""Synthetic captures, listings and FX observations shared by the normalization modules.

Two accounts, shaped like the two statements a review actually meets. ``usd_table`` is a
version-1 capture that labels nothing, so the currency it reports values in is the
review's own base currency -- and it holds a Toronto listing quoted in CAD whose value
the source has already converted to USD, which is the shape the owner's real captures
have. ``cad_table`` is a version-2 capture carrying quote symbols and a currency column
that states CAD, so its values are converted once at a dated rate; it holds a London
listing quoted in pence.

Between them they cover every currency question the normalization answers: a USD
listing, a Toronto listing, a pence listing, a source that converted for us and one that
did not, and a rollup of both into USD.
"""

from decimal import Decimal

from portfolio_research.fx import FxTable

SUNDAY_RECEIPT = "2026-09-13T20:00:00+00:00"
LATER_RECEIPT = "2026-09-13T20:30:00+00:00"
SUNDAY_GENERATED = "2026-09-13T21:00:00+00:00"
RECEIPT_DAY = "2026-09-13"
FX_DAY = "2026-09-11"
STALE_DAY = "2026-08-20"
CADUSD = Decimal("0.7211")


def listing(symbol, quote_currency, exchange, instrument_type="equity"):
    factor = 100 if quote_currency == "GBp" else 1
    return {
        "symbol": symbol,
        "quote_currency": quote_currency,
        "quote_unit_factor": factor,
        "major_currency": "GBP" if quote_currency == "GBp" else quote_currency,
        "exchange": exchange,
        "instrument_type": instrument_type,
        "name": f"{symbol} fixture listing",
        "last_quote_at": "2026-09-11T20:00:00+00:00",
        "last_price": None,
        "market_cap": None,
        "shares_outstanding": None,
        "source_id": f"yahoo_listing:{symbol}",
        "received_at": SUNDAY_RECEIPT,
        "provider": "yahoo",
        "stale": False,
    }


LISTINGS = {
    "RY.TO": listing("RY.TO", "CAD", "TOR"),
    "AAPL": listing("AAPL", "USD", "NMS"),
    "VOD.L": listing("VOD.L", "GBp", "LSE"),
}


def observation(base, quote, rate, day=FX_DAY, provider="bank_of_canada"):
    return {
        "pair": base + quote,
        "base": base,
        "quote": quote,
        "rate": rate,
        "date": day,
        "source_id": f"{provider}_fx:{base}{quote}:{day}",
        "provider": provider,
        "received_at": SUNDAY_RECEIPT,
    }


def fx_table(day=FX_DAY):
    return FxTable(
        [
            observation("CAD", "USD", "0.7211", day, "yahoo"),
            observation("GBP", "CAD", "1.8250", day),
            observation("USD", "CAD", "1.3868", day),
        ],
        max_age_days=7,
    )


def usd_table(rows):
    """Yahoo USD portfolio capture: no currency column, version 1."""
    rows = [*rows, ["Total Cash", "", "100", ""]]
    return {
        "method": "yahoo-holdings-table-v1",
        "headers": ["Symbol", "Shares", "Last Price", "Market Value ($)"],
        "rows": rows,
        "page_count": 1,
        "expected_count": len(rows),
        "completeness": "count-verified",
    }


def cad_table(rows, cash_currency="CAD"):
    """Yahoo CAD portfolio capture with quote symbols and a stated currency, version 2.

    ``cash_currency=""`` leaves the cash row unlabelled, so cash has to follow the
    currency its account's holdings were established in.
    """
    rows = [[*row, "CAD", row[0]] for row in rows] + [
        ["Total Cash", "", "50", "", cash_currency, ""]
    ]
    return {
        "method": "yahoo-holdings-table-v1",
        "headers": [
            "Symbol",
            "Shares",
            "Last Price",
            "Market Value",
            "Currency",
            "Yahoo quote symbol",
        ],
        "rows": rows,
        "page_count": 1,
        "expected_count": len(rows),
        "completeness": "count-verified",
        "capture_version": 2,
    }


# A reports in USD: AAPL is quoted and valued in USD, and RY.TO is quoted in CAD but
# valued in USD already -- 10 x 285.20 CAD x 0.7211 = 2056.30. B reports in CAD: RY.TO is
# quoted and valued in CAD, and VOD.L is quoted in pence (100 x 128.75p = 128.75 GBP) and
# valued in CAD at 1.8250.
A_ROWS = [["RY.TO", "10", "285.20", "2056.30"], ["AAPL", "2", "300", "600"]]
B_ROWS = [["RY.TO", "5", "285.20", "1426.00"], ["VOD.L", "100", "128.75", "235.00"]]


def issuer_lookup(meta):
    return "cik:0000320193" if meta.get("symbol") == "AAPL" else None
