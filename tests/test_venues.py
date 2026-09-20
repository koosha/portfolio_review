"""The owner's rule: a holding's quote currency is read from its ticker's venue."""

import unittest

from portfolio_research.identity import resolve_identities
from portfolio_research.normalize import LISTED_STATUSES, fx_currencies
from portfolio_research.service import default_config
from portfolio_research.venues import VENUE_CURRENCY, venue_currency
from tests.test_identity import by_id, ledger, listing, position


class VenueCurrencyTests(unittest.TestCase):
    def test_a_bare_symbol_is_a_us_listing_quoted_in_usd(self):
        for symbol in ("AAPL", "SPY", "IBM", "ABC-B", "XYZ"):
            self.assertEqual(venue_currency(symbol), ("USD", "USD", 1), symbol)

    def test_a_canadian_venue_suffix_is_quoted_in_cad(self):
        for symbol in ("RY.TO", "SHOP.NE", "ABC.V", "XYZ.CN"):
            self.assertEqual(venue_currency(symbol), ("CAD", "CAD", 1), symbol)

    def test_a_london_listing_is_quoted_in_pence_not_pounds(self):
        """``.L`` must keep the subunit, or a London holding is presented 100x too high."""
        self.assertEqual(venue_currency("VOD.L"), ("GBp", "GBP", 100))

    def test_a_swiss_listing_is_quoted_in_chf(self):
        self.assertEqual(venue_currency("NESN.SW"), ("CHF", "CHF", 1))

    def test_an_unknown_suffix_decides_nothing(self):
        """A venue we have not written down stays unanswered rather than guessed."""
        for symbol in ("ABC.DE", "ABC.HK", "ABC.AX", "ABC.T"):
            self.assertEqual(venue_currency(symbol), (None, None, None), symbol)

    def test_a_us_share_class_suffix_is_not_a_venue(self):
        for symbol in ("ABC.A", "ABC.B", "XYZ.B", "ABC.PR"):
            self.assertEqual(venue_currency(symbol), ("USD", "USD", 1), symbol)

    def test_an_index_or_an_fx_pseudo_ticker_is_never_a_holding(self):
        for symbol in ("^GSPC", "^VIX", "CADUSD=X", "GBPUSD=X"):
            self.assertEqual(venue_currency(symbol), (None, None, None), symbol)

    def test_unusable_input_decides_nothing(self):
        for symbol in (None, "", "   ", 42, ".TO", "ABC."):
            self.assertEqual(venue_currency(symbol), (None, None, None), repr(symbol))

    def test_a_symbol_is_read_case_insensitively_and_trimmed(self):
        self.assertEqual(venue_currency("  ry.to  "), ("CAD", "CAD", 1))

    def test_the_last_segment_names_the_venue(self):
        self.assertEqual(venue_currency("RDS.A.L"), ("GBp", "GBP", 100))

    def test_every_table_entry_resolves_to_a_currency(self):
        for suffix in VENUE_CURRENCY:
            quote, major, factor = venue_currency(f"ABC.{suffix}")
            self.assertIsNotNone(quote, suffix)
            self.assertIsNotNone(major, suffix)
            self.assertIn(factor, (1, 100), suffix)


class FxCurrencyLoadingTests(unittest.TestCase):
    """The rates a ticker implies are requested before normalization runs."""

    def bundle(self, *symbols):
        positions = [{"raw_symbol": symbol, "quote_symbol": None} for symbol in symbols]
        return {"ledger": {"positions": positions, "accounts": []}}

    def currencies(self, *symbols, listings=None):
        return fx_currencies(self.bundle(*symbols), default_config("."), listings or {})

    def test_an_unresolved_toronto_holding_still_asks_for_its_rate(self):
        """Without this the venue fallback yields a CAD price and no CAD observation."""
        self.assertEqual(self.currencies("RY.TO", "AAPL"), ["CAD"])

    def test_an_unresolved_london_holding_asks_for_the_major_currency(self):
        self.assertEqual(self.currencies("VOD.L"), ["GBP"])

    def test_a_portfolio_of_bare_symbols_needs_no_rate_at_all(self):
        self.assertEqual(self.currencies("AAPL", "SPY", "ABC-B"), [])

    def test_a_resolved_listing_outranks_the_venue_table(self):
        listings = {"RY.TO": {"quote_currency": "USD"}}
        self.assertEqual(self.currencies("RY.TO", listings=listings), [])

    def test_an_unknown_venue_asks_for_nothing(self):
        self.assertEqual(self.currencies("ABC.DE"), [])


class ResolvedListingFallbackTests(unittest.TestCase):
    """A listing can resolve on its exchange alone; its ticker still states its currency.

    ``market_listings`` accepts a provider payload carrying only an exchange name, so a
    symbol resolves with no quote currency of its own. Without the venue fallback here a
    Toronto listing would reach normalization with no quote currency at all, its CAD
    price would be presented as a USD price, and the reconciliation check -- which needs
    a quote currency to run -- could not catch it.
    """

    def resolve(self, symbol, listings):
        result = resolve_identities(
            ledger(position(raw_symbol=symbol)),
            None,
            listings=listings,
            search=None,
            issuer_lookup=None,
        )
        return by_id(result)[symbol]

    def currencyless(self, symbol, exchange):
        return {symbol: listing(symbol, None, exchange=exchange)}

    def test_a_toronto_listing_without_a_stated_currency_is_still_cad(self):
        security = self.resolve("VFV.TO", self.currencyless("VFV.TO", "TOR"))
        self.assertIn(security["resolution_status"], LISTED_STATUSES)
        self.assertEqual(security["quote_currency"], "CAD")
        self.assertEqual(security["currency"], "CAD")
        self.assertEqual(security["quote_unit_factor"], 1)

    def test_a_london_listing_without_a_stated_currency_keeps_its_pence_factor(self):
        security = self.resolve("VOD.L", self.currencyless("VOD.L", "LSE"))
        self.assertEqual(security["quote_currency"], "GBp")
        self.assertEqual(security["currency"], "GBP")
        self.assertEqual(security["quote_unit_factor"], 100)

    def test_a_bare_symbol_without_a_stated_currency_is_usd(self):
        security = self.resolve("AAPL", self.currencyless("AAPL", "NMS"))
        self.assertEqual(security["quote_currency"], "USD")
        self.assertEqual(security["quote_unit_factor"], 1)

    def test_a_stated_currency_still_outranks_the_venue(self):
        """A US-listed Canadian name is quoted in USD; the listing says so and wins."""
        security = self.resolve("RY.TO", {"RY.TO": listing("RY.TO", "USD", exchange="NYQ")})
        self.assertEqual(security["quote_currency"], "USD")

    def test_an_unknown_venue_without_a_stated_currency_stays_unanswered(self):
        security = self.resolve("ABC.DE", self.currencyless("ABC.DE", "GER"))
        self.assertIsNone(security["quote_currency"])
        self.assertIsNone(security["quote_unit_factor"])


if __name__ == "__main__":
    unittest.main()
