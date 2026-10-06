"""Adapter security profile: sector, domicile, equity-type and market-capitalization dating."""

import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from portfolio_research import market_data
from portfolio_research.market_data import (
    ADAPTER_VERSION,
    domicile_code,
    equity_type_label,
    sector_label,
    security_profile,
)
from tests.support.market_data_adapter import AS_OF, AdapterCase, FakeTicker


class AdapterClock(datetime):
    """Pins the adapter's wall clock to the fixture's observation date.

    ``market_cap`` is only attached when the review observes today, so the test states
    which day "today" is instead of drifting with the machine clock.
    """

    @classmethod
    def now(cls, tz=None):
        stamp = datetime(2026, 9, 16, 20, tzinfo=timezone.utc)
        return stamp if tz is None else stamp.astimezone(tz)


class ProfileTests(AdapterCase):
    def test_actual_company_collection_preserves_the_resolved_issuer_profile(self):
        from portfolio_research.enrichment import _collect

        security = {
            **self.security("AAPL"),
            "issuer_id": "cik:0000320193",
            "cik": "0000320193",
            "instrument_type": "equity",
        }
        with (
            patch.object(market_data, "datetime", AdapterClock),
            patch("yfinance.Ticker", FakeTicker),
        ):
            record = _collect(
                security,
                self.config,
                AS_OF,
                refresh=True,
                issues=self.issues,
                issuer_lookup=lambda metadata: "cik:0000320193",
            )
        self.assertEqual(record["profile"]["issuer_id"], security["issuer_id"])
        self.assertEqual(record["profile"]["cik"], "0000320193")
        self.assertEqual(record["profile"]["market_cap_currency"], "USD")

    def test_security_profile_maps_sector_domicile_equity_type_and_identifiers(self):
        with patch.object(market_data, "datetime", AdapterClock):
            profile = self.call(
                security_profile, "AAPL", issuer_lookup=lambda meta: "cik:0000320193"
            )
        self.assertEqual(profile["sector"], "Technology")
        self.assertEqual(profile["industry"], "Consumer Electronics")
        self.assertAlmostEqual(profile["market_cap"], 4_500_000_000_000)
        self.assertEqual(profile["market_cap_currency"], "USD")
        self.assertEqual(profile["market_cap_as_of"], AS_OF)
        self.assertAlmostEqual(profile["shares_outstanding"], 15_000_000_000)
        self.assertEqual(profile["cik"], "0000320193")
        self.assertEqual(profile["domicile"], "US")
        self.assertEqual(profile["equity_type"], "ordinary_common")
        self.assertIsNone(profile["equity_type_reason"])
        self.assertEqual(profile["adapter_version"], ADAPTER_VERSION)

    def test_a_historical_as_of_never_takes_todays_market_capitalization(self):
        with patch("yfinance.Ticker", FakeTicker):
            profile = security_profile(
                self.config,
                self.security("AAPL"),
                refresh=True,
                issues=self.issues,
                as_of="2024-06-28",
            )
        self.assertIsNone(profile["market_cap"])
        self.assertIsNone(profile["market_cap_as_of"])
        self.assertEqual(profile["sector"], "Technology")

    def test_current_ny_evening_does_not_lose_cap_when_utc_is_the_next_day(self):
        class EveningClock(AdapterClock):
            @classmethod
            def now(cls, tz=None):
                stamp = datetime(2026, 9, 17, 1, tzinfo=timezone.utc)
                return stamp if tz is None else stamp.astimezone(tz)

        with patch.object(market_data, "datetime", EveningClock):
            profile = self.call(security_profile, "AAPL")
        self.assertIsNotNone(profile["market_cap"])
        self.assertEqual(profile["market_cap_as_of"], "2026-09-16")

    def test_current_profile_uses_actual_receipt_date_separate_from_last_market_session(self):
        with (
            patch.object(market_data, "datetime", AdapterClock),
            patch.object(
                market_data,
                "_obtain",
                return_value=(
                    {
                        "market_cap": 1000,
                        "market_cap_currency": "USD",
                        "metadata": {"instrument_type": "EQUITY"},
                    },
                    "2026-09-16T20:05:00Z",
                    {"source_id": "profile:current"},
                ),
            ),
        ):
            profile = security_profile(
                self.config,
                self.security("AAPL"),
                refresh=True,
                issues=[],
                as_of="2026-09-15",
                timeline={
                    "review_kind": "current",
                    "information_cutoff": "2026-09-16T20:00:00Z",
                },
            )
        self.assertEqual(profile["market_cap"], 1000)
        self.assertEqual(profile["market_cap_as_of"], "2026-09-16")
        self.assertEqual(profile["market_cap_received_at"], "2026-09-16T20:05:00Z")

    def test_native_market_cap_without_quote_currency_is_preserved_but_not_usable(self):
        with (
            patch.object(market_data, "datetime", AdapterClock),
            patch.object(
                market_data,
                "_obtain",
                return_value=(
                    {"market_cap": 1000, "financial_currency": "USD", "metadata": {}},
                    "2026-09-16T20:00:00Z",
                    {"source_id": "profile:unknown-unit"},
                ),
            ),
        ):
            profile = security_profile(
                self.config, self.security("AAPL"), refresh=True, issues=[], as_of=AS_OF
            )
        self.assertIsNone(profile["market_cap"])
        self.assertEqual(profile["reported_market_cap"], 1000)
        self.assertIsNone(profile["market_cap_currency"])

    def test_sector_labels_follow_the_application_vocabulary(self):
        self.assertEqual(sector_label("Financial Services"), "Financials")
        self.assertEqual(sector_label("Real Estate"), "Real Estate")
        self.assertEqual(sector_label("Technology"), "Technology")
        self.assertIsNone(sector_label(None))
        self.assertIsNone(sector_label(""))

    def test_domicile_codes_cover_united_states_and_common_listings(self):
        self.assertEqual(domicile_code("United States"), "US")
        self.assertEqual(domicile_code("Canada"), "CA")
        self.assertEqual(domicile_code("United Kingdom"), "GB")
        self.assertEqual(domicile_code("Switzerland"), "CH")
        self.assertIsNone(domicile_code("Atlantis"))
        self.assertIsNone(domicile_code(None))

    def test_share_class_suffixes_are_not_ordinary_common_stock(self):
        value, reason = equity_type_label("EQUITY", "AAPL")
        self.assertEqual(value, "ordinary_common")
        self.assertIsNone(reason)
        for symbol in ["BRK-P", "ACME-W", "ACME-U", "ACME.PR"]:
            value, reason = equity_type_label("EQUITY", symbol)
            self.assertIsNone(value)
            self.assertIn(symbol, reason)
        value, reason = equity_type_label("ETF", "XIC.TO")
        self.assertIsNone(value)
        self.assertIn("ETF", reason)


if __name__ == "__main__":
    unittest.main()
