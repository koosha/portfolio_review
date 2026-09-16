"""Yahoo listing metadata, listing search and SEC issuer lookup behind the provider cache."""

import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

import pandas as pd

from portfolio_research.issuers import sec_issuer_lookup
from portfolio_research.market_listings import listing_metadata, search_listings

SEC_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_PAYLOAD = {
    "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
    "1": {"cik_str": 1067983, "ticker": "BRK-B", "title": "Berkshire Hathaway Inc."},
    "2": {"cik_str": 1000275, "ticker": "RY", "title": "Royal Bank of Canada"},
    "3": {"cik_str": "bad", "ticker": "BAD"},
    "4": "not a record",
}

LISTINGS = {
    "AAPL": {
        "history_metadata": {
            "currency": "USD",
            "exchangeName": "NMS",
            "instrumentType": "EQUITY",
            "longName": "Apple Inc.",
            "symbol": "AAPL",
            "regularMarketPrice": 300.5,
            "regularMarketTime": pd.Timestamp("2026-09-11 16:00", tz="America/New_York"),
        },
        "fast_info": {
            "currency": "USD",
            "exchange": "NMS",
            "quoteType": "EQUITY",
            "lastPrice": 300.5,
            "marketCap": 4500000000000,
            "shares": 15000000000,
        },
    },
    "VOD.L": {
        "history_metadata": {
            "currency": "GBp",
            "exchangeName": "LSE",
            "instrumentType": "EQUITY",
            "longName": "Vodafone Group Public Limited Company",
            "symbol": "VOD.L",
            "regularMarketPrice": 128.75,
            "regularMarketTime": 1789140600,
        },
        "fast_info": None,
    },
    "XIC.TO": {
        "history_metadata": None,
        "fast_info": {
            "currency": "CAD",
            "exchange": "TOR",
            "quoteType": "ETF",
            "lastPrice": 41.2,
            "marketCap": float("nan"),
            "shares": None,
        },
    },
    "EMPTY": {"history_metadata": {}, "fast_info": {}},
}


class FakeTicker:
    created = []

    def __init__(self, symbol):
        FakeTicker.created.append(symbol)
        if symbol == "BROKEN":
            raise RuntimeError("request failed https://query.example/v8?token=secret")
        self.symbol = symbol
        self._data = LISTINGS.get(symbol, {"history_metadata": {}, "fast_info": {}})

    @property
    def history_metadata(self):
        value = self._data["history_metadata"]
        if value is None:
            raise KeyError("chart")
        return dict(value)

    @property
    def fast_info(self):
        value = self._data["fast_info"]
        if value is None:
            raise ValueError("no quote summary")
        return dict(value)


def refuse_network(*args, **kwargs):
    raise AssertionError("network invoked")


class ListingCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = {
            "research": {"path": str(self.root / "research.sqlite")},
            "data": {"mode": "live", "max_listing_age_days": 30},
        }
        FakeTicker.created = []

    def tearDown(self):
        self.temp.cleanup()

    def refresh(self, symbol, issues=None):
        issues = [] if issues is None else issues
        with patch("yfinance.Ticker", FakeTicker):
            return listing_metadata(self.config, symbol, refresh=True, issues=issues)


class ListingMetadataTests(ListingCase):
    def test_refresh_combines_history_metadata_and_fast_info(self):
        issues = []
        record = self.refresh("aapl", issues)
        self.assertEqual(issues, [])
        self.assertEqual(FakeTicker.created, ["AAPL"])
        expected = {
            "symbol": "AAPL",
            "quote_currency": "USD",
            "quote_unit_factor": 1,
            "major_currency": "USD",
            "exchange": "NMS",
            "instrument_type": "equity",
            "name": "Apple Inc.",
            "last_quote_at": "2026-09-11T20:00:00+00:00",
            "last_price": "300.5",
            "market_cap": "4500000000000",
            "shares_outstanding": "15000000000",
            "provider": "yahoo",
            "stale": False,
        }
        self.assertEqual({key: record[key] for key in expected}, expected)
        self.assertTrue(record["source_id"].startswith("yahoo_listing:"))
        self.assertIsNotNone(datetime.fromisoformat(record["received_at"]).tzinfo)
        self.assertNotIn("url", record)
        self.assertNotIn("raw_path", record)

    def test_gbp_listing_reports_subunit_quote_and_major_currency(self):
        record = self.refresh("VOD.L")
        self.assertEqual(record["quote_currency"], "GBp")
        self.assertEqual(record["quote_unit_factor"], 100)
        self.assertEqual(record["major_currency"], "GBP")
        self.assertEqual(record["exchange"], "LSE")
        self.assertEqual(record["last_price"], "128.75")
        self.assertEqual(record["last_quote_at"], "2026-09-11T15:30:00+00:00")
        self.assertIsNone(record["market_cap"])

    def test_fast_info_alone_is_enough_and_non_finite_numbers_are_missing(self):
        record = self.refresh("XIC.TO")
        self.assertEqual(record["quote_currency"], "CAD")
        self.assertEqual(record["exchange"], "TOR")
        self.assertEqual(record["instrument_type"], "etf")
        self.assertEqual(record["last_price"], "41.2")
        self.assertIsNone(record["name"])
        self.assertIsNone(record["last_quote_at"])
        self.assertIsNone(record["market_cap"])
        self.assertIsNone(record["shares_outstanding"])

    def test_cached_listing_is_reused_without_network(self):
        first = self.refresh("AAPL")
        issues = []
        with patch("yfinance.Ticker", refuse_network):
            cached = listing_metadata(self.config, "AAPL", refresh=False, issues=issues)
        self.assertEqual(cached, first)
        self.assertEqual(issues, [])

    def test_offline_cache_miss_returns_none_without_network(self):
        issues = []
        with patch("yfinance.Ticker", refuse_network):
            self.assertIsNone(listing_metadata(self.config, "AAPL", refresh=False, issues=issues))
        self.assertEqual(issues, [])

    def test_old_cached_listing_is_returned_but_flagged_stale(self):
        self.refresh("AAPL")
        [sidecar] = (self.root / "cache" / "yahoo_listing").glob("*.source.json")
        source = json.loads(sidecar.read_text())
        source["received_at"] = "2020-01-01T00:00:00+00:00"
        sidecar.write_text(json.dumps(source))
        record = listing_metadata(self.config, "AAPL", refresh=False, issues=[])
        self.assertIs(record["stale"], True)
        self.assertEqual(record["received_at"], "2020-01-01T00:00:00+00:00")
        self.assertEqual(record["quote_currency"], "USD")

    def test_invalid_symbols_are_rejected_before_any_lookup(self):
        for symbol in ("", "BAD SYMBOL", "$$$", ".TO", "A" * 21, None, 123):
            with self.subTest(symbol=symbol):
                issues = []
                self.assertIsNone(self.refresh(symbol, issues))
                self.assertEqual([issue["code"] for issue in issues], ["INVALID_LISTING_SYMBOL"])
        self.assertEqual(FakeTicker.created, [])

    def test_lookup_failure_is_isolated_per_symbol(self):
        issues = []
        results = {symbol: self.refresh(symbol, issues) for symbol in ("BROKEN", "EMPTY", "AAPL")}
        self.assertIsNone(results["BROKEN"])
        self.assertIsNone(results["EMPTY"])
        self.assertEqual(results["AAPL"]["quote_currency"], "USD")
        self.assertEqual(
            [(issue["code"], issue["symbol"]) for issue in issues],
            [("LISTING_LOOKUP_FAILED", "BROKEN"), ("LISTING_LOOKUP_FAILED", "EMPTY")],
        )
        self.assertEqual(issues[0]["detail"], "RuntimeError")
        for issue in issues:
            self.assertEqual(issue["severity"], "warning")
            self.assertNotIn("http", json.dumps(issue))
            self.assertNotIn("secret", json.dumps(issue))
        # Empty provider answers are never cached as listing evidence.
        self.assertIsNone(listing_metadata(self.config, "EMPTY", refresh=False, issues=[]))

    def test_refresh_failure_falls_back_to_a_cached_listing(self):
        first = self.refresh("AAPL")
        issues = []
        with patch("yfinance.Ticker", side_effect=URLError("offline")):
            record = listing_metadata(self.config, "AAPL", refresh=True, issues=issues)
        self.assertEqual(record, first)
        self.assertEqual([issue["code"] for issue in issues], ["LISTING_LOOKUP_FAILED"])

    def test_missing_yfinance_is_reported(self):
        issues = []
        with patch.dict(sys.modules, {"yfinance": None}):
            record = listing_metadata(self.config, "AAPL", refresh=True, issues=issues)
        self.assertIsNone(record)
        self.assertEqual([issue["code"] for issue in issues], ["YAHOO_DEPENDENCY_MISSING"])


class FakeSearch:
    calls = []
    quotes = [
        {
            "symbol": "SHOP",
            "shortname": "Shopify",
            "longname": "Shopify Inc.",
            "exchange": "NYQ",
            "exchDisp": "NYSE",
            "quoteType": "EQUITY",
        },
        {"symbol": "SHOP.TO", "shortname": "Shopify", "exchange": "TOR", "quoteType": "EQUITY"},
        {"shortname": "no symbol"},
        "not a quote",
        {"symbol": "SHOP", "shortname": "duplicate", "exchange": "NYQ", "quoteType": "EQUITY"},
    ]

    def __init__(self, query, max_results=8, **kwargs):
        FakeSearch.calls.append((query, max_results))
        if query == "BROKEN":
            raise RuntimeError("https://query.example/search?token=secret")
        self.quotes = [dict(q) if isinstance(q, dict) else q for q in FakeSearch.quotes]


class BoundedProviderNumbersTests(ListingCase):
    """A provider number is bounded data: an oversized one is missing, never an error."""

    OVERSIZED = {
        "history_metadata": {
            "currency": "USD",
            "exchangeName": "NMS",
            "instrumentType": "EQUITY",
            "symbol": "HUGE",
            "regularMarketPrice": "9e9999999",
        },
        "fast_info": {
            "currency": "USD",
            "exchange": "NMS",
            "quoteType": "EQUITY",
            "marketCap": "9e9999999",
            "shares": "1" + "0" * 200,
        },
    }

    def archive(self, symbol, payload):
        from portfolio_lab import providers

        return providers._archive(
            self.config,
            "yahoo_listing",
            symbol,
            payload,
            "https://finance.yahoo.com/quote/" + symbol,
            providers._now(),
            point_in_time="test fixture",
        )

    def test_oversized_cached_numbers_are_missing_facts(self):
        self.archive("HUGE", self.OVERSIZED)
        issues = []
        record = listing_metadata(self.config, "HUGE", refresh=False, issues=issues)
        self.assertEqual(issues, [])
        self.assertEqual(record["quote_currency"], "USD")
        self.assertIsNone(record["market_cap"])
        self.assertIsNone(record["last_price"])
        self.assertIsNone(record["shares_outstanding"])

    def test_an_unexpected_cached_failure_stays_an_isolated_issue(self):
        self.archive("AAPL", {"history_metadata": {"currency": "USD"}, "fast_info": {}})
        issues = []
        with patch(
            "portfolio_research.market_listings._listing_record",
            side_effect=ArithmeticError("boom"),
        ):
            self.assertIsNone(listing_metadata(self.config, "AAPL", refresh=False, issues=issues))
        self.assertEqual({issue["code"] for issue in issues}, {"LISTING_LOOKUP_FAILED"})

    def test_an_unexpected_refresh_failure_stays_an_isolated_issue(self):
        issues = []
        with (
            patch("yfinance.Ticker", FakeTicker),
            patch(
                "portfolio_research.market_listings._listing_record",
                side_effect=ArithmeticError("boom"),
            ),
        ):
            self.assertIsNone(listing_metadata(self.config, "AAPL", refresh=True, issues=issues))
        self.assertTrue(issues)
        self.assertEqual({issue["code"] for issue in issues}, {"LISTING_LOOKUP_FAILED"})


class CacheScanTests(ListingCase):
    """A presentation reads each provider cache directory once, not once per symbol."""

    def archive(self, symbol):
        from portfolio_lab import providers

        providers._archive(
            self.config,
            "yahoo_listing",
            symbol,
            {"history_metadata": {"currency": "USD", "exchangeName": "NMS"}, "fast_info": {}},
            "https://finance.yahoo.com/quote/" + symbol,
            providers._now(),
            point_in_time="test fixture",
        )

    def test_a_scan_answers_every_symbol_without_re_reading_the_directory(self):
        from portfolio_lab import providers
        from portfolio_research.provider_cache import scanned

        symbols = ["AAPL", "MSFT", "RY.TO"]
        for symbol in symbols:
            self.archive(symbol)
        issues = []
        with (
            patch.object(providers, "_cached", side_effect=AssertionError("per-symbol scan")),
            scanned(self.config, "yahoo_listing", "yahoo_search"),
        ):
            found = [
                listing_metadata(self.config, symbol, refresh=False, issues=issues)
                for symbol in symbols
            ]
            self.assertIsNone(listing_metadata(self.config, "NOPE", refresh=False, issues=issues))
        self.assertEqual([record["symbol"] for record in found], symbols)
        self.assertEqual(issues, [])

    def test_outside_a_scan_the_shared_provider_cache_is_used(self):
        self.archive("AAPL")
        record = listing_metadata(self.config, "AAPL", refresh=False, issues=[])
        self.assertEqual(record["quote_currency"], "USD")


class SearchListingsTests(ListingCase):
    def setUp(self):
        super().setUp()
        FakeSearch.calls = []

    def search(self, text, issues=None, **kwargs):
        issues = [] if issues is None else issues
        with patch("yfinance.Search", FakeSearch):
            return search_listings(self.config, text, refresh=True, issues=issues, **kwargs)

    def test_search_quotes_become_listing_candidates_and_are_cached(self):
        issues = []
        results = self.search("  shop ", issues)
        self.assertEqual(issues, [])
        self.assertEqual(FakeSearch.calls, [("SHOP", 5)])
        self.assertEqual(
            results,
            [
                {
                    "symbol": "SHOP",
                    "name": "Shopify Inc.",
                    "exchange": "NYQ",
                    "instrument_type": "equity",
                },
                {
                    "symbol": "SHOP.TO",
                    "name": "Shopify",
                    "exchange": "TOR",
                    "instrument_type": "equity",
                },
            ],
        )
        with patch("yfinance.Search", refuse_network):
            cached = search_listings(self.config, "Shop", refresh=False, issues=issues)
        self.assertEqual(cached, results)
        self.assertEqual(issues, [])

    def test_limit_bounds_the_candidates(self):
        self.assertEqual([row["symbol"] for row in self.search("SHOP", limit=1)], ["SHOP"])
        self.assertEqual(FakeSearch.calls, [("SHOP", 1)])
        with self.assertRaises(ValueError):
            self.search("SHOP", limit=0)

    def test_offline_search_without_cache_is_empty(self):
        with patch("yfinance.Search", refuse_network):
            self.assertEqual(search_listings(self.config, "SHOP", refresh=False, issues=[]), [])

    def test_search_failure_and_invalid_text_are_issues(self):
        issues = []
        self.assertEqual(self.search("BROKEN", issues), [])
        self.assertEqual(self.search("   ", issues), [])
        self.assertEqual(self.search("x" * 101, issues), [])
        self.assertEqual(
            [issue["code"] for issue in issues],
            ["LISTING_SEARCH_FAILED", "INVALID_LISTING_SEARCH", "INVALID_LISTING_SEARCH"],
        )
        self.assertEqual(issues[0]["detail"], "RuntimeError")
        self.assertNotIn("secret", json.dumps(issues))


class SecIssuerLookupTests(ListingCase):
    def setUp(self):
        super().setUp()
        self.config["data"]["sec_user_agent"] = "Portfolio Review owner@example.org"
        self.calls = []

    def fetch(self, *args):
        self.calls.append(args)
        return SEC_PAYLOAD, "2026-09-13T20:00:00+00:00"

    def test_us_listings_map_to_zero_padded_cik_identifiers(self):
        issues = []
        with patch("portfolio_lab.providers._fetch_json", self.fetch):
            lookup = sec_issuer_lookup(self.config, refresh=True, issues=issues)
        self.assertEqual(issues, [])
        self.assertEqual(self.calls, [(SEC_URL, "Portfolio Review owner@example.org")])
        self.assertEqual(lookup({"symbol": "AAPL", "exchange": "NMS"}), "cik:0000320193")
        self.assertEqual(lookup({"symbol": "aapl"}), "cik:0000320193")
        self.assertEqual(lookup({"symbol": "BRK.B", "exchange": "NYQ"}), "cik:0001067983")
        self.assertEqual(lookup({"symbol": "BRK-B"}), "cik:0001067983")
        self.assertIsNone(lookup({"symbol": "BRK.B"}))
        self.assertIsNone(lookup({"symbol": "RY.TO", "exchange": "TOR"}))
        self.assertIsNone(lookup({"symbol": "SHOP.TO"}))
        self.assertIsNone(lookup({"symbol": "BAD", "exchange": "NYQ"}))
        self.assertIsNone(lookup({"symbol": "ZZZZ", "exchange": "NMS"}))
        self.assertIsNone(lookup({"exchange": "NMS"}))
        self.assertIsNone(lookup("AAPL"))

    def test_cached_ticker_list_is_used_offline(self):
        with patch("portfolio_lab.providers._fetch_json", self.fetch):
            sec_issuer_lookup(self.config, refresh=True, issues=[])
        issues = []
        with patch("portfolio_lab.providers._fetch_json", refuse_network):
            lookup = sec_issuer_lookup(self.config, refresh=False, issues=issues)
        self.assertEqual(issues, [])
        self.assertEqual(lookup({"symbol": "AAPL", "exchange": "NMS"}), "cik:0000320193")

    def test_failed_refresh_falls_back_to_the_cached_ticker_list(self):
        with patch("portfolio_lab.providers._fetch_json", self.fetch):
            sec_issuer_lookup(self.config, refresh=True, issues=[])
        issues = []
        with patch("portfolio_lab.providers._fetch_json", side_effect=URLError("offline")):
            lookup = sec_issuer_lookup(self.config, refresh=True, issues=issues)
        self.assertEqual(lookup({"symbol": "BRK.B", "exchange": "NYQ"}), "cik:0001067983")
        self.assertEqual(
            [(issue["code"], issue["detail"]) for issue in issues],
            [("SEC_TICKERS_REFRESH_FAILED", "URLError")],
        )

    def test_default_user_agent_is_used_when_none_is_configured(self):
        del self.config["data"]["sec_user_agent"]
        with patch("portfolio_lab.providers._fetch_json", self.fetch):
            self.assertIsNotNone(sec_issuer_lookup(self.config, refresh=True, issues=[]))
        self.assertEqual(self.calls, [(SEC_URL,)])

    def test_unavailable_ticker_list_is_a_warning_and_no_lookup(self):
        failures = (
            HTTPError(SEC_URL, 403, "Forbidden", None, None),
            URLError("offline"),
            lambda *args: ({"0": {"ticker": "AAPL"}}, "2026-09-13T20:00:00+00:00"),
        )
        for failure in failures:
            with self.subTest(failure=failure):

                def fetch(*args, failure=failure):
                    if callable(failure):
                        return failure(*args)
                    raise failure

                issues = []
                with patch("portfolio_lab.providers._fetch_json", fetch):
                    self.assertIsNone(sec_issuer_lookup(self.config, refresh=True, issues=issues))
                [issue] = issues
                self.assertEqual(issue["code"], "SEC_TICKERS_UNAVAILABLE")
                self.assertEqual(issue["severity"], "warning")
                self.assertNotIn("sec.gov", json.dumps(issue))
        issues = []
        self.assertIsNone(sec_issuer_lookup(self.config, refresh=False, issues=issues))
        self.assertEqual([issue["code"] for issue in issues], ["SEC_TICKERS_UNAVAILABLE"])


if __name__ == "__main__":
    unittest.main()
