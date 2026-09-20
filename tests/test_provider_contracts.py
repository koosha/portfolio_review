"""Recorded provider responses: the shapes the adapters accept, and how a drift reads.

Every case replays a response checked in under ``tests/fixtures/providers`` — synthetic
values in the provider's own field names — through the adapter that consumes it. Two
contracts are pinned. First, the adapter accepts the recorded shape and produces the
records a review reads. Second, a shape drift — one renamed column or key, the way a
provider release ships one — is read as an absent capability: the adapter returns
without raising, coverage for that capability says ``missing``, and where the response
can no longer be read at all the run carries an issue that says so in words.

No network: ``yfinance.Ticker`` and ``portfolio_lab.providers._fetch_json`` are patched.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from portfolio_lab import providers
from portfolio_lab.config import validate_config
from portfolio_lab.providers import ProviderShapeError, extract_sec_fundamentals
from portfolio_research import market_data
from portfolio_research.enrichment import _collect, coverage_report
from portfolio_research.fx_providers import bank_of_canada_observations
from portfolio_research.issuers import sec_issuer_lookup
from tests.support.provider_fixtures import (
    SEC_COMPANYFACTS,
    SEC_TICKERS,
    VALET,
    load,
    recorded_tickers,
    rename,
)

AS_OF = "2026-09-16"
RECEIVED = "2026-09-16T20:30:00+00:00"
EQUITY = "AAPL"
FUND = "XIC.TO"


def exception_names(issues) -> list[str]:
    """Issue details that name a Python exception class instead of describing anything."""
    return [
        str(issue.get("detail"))
        for issue in issues
        if str(issue.get("detail")) in {"ValueError", "KeyError", "AttributeError", "TypeError"}
    ]


class ContractCase(unittest.TestCase):
    """One throwaway provider cache, a live-refreshing config and a recorded ticker."""

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.config = validate_config(
            {
                "research": {"path": str(self.root / "research.sqlite")},
                "data": {
                    "mode": "live",
                    "lookback_years": 1,
                    "provider_timeout_seconds": 5,
                    "provider_min_interval_seconds": 0,
                    "provider_refresh_hours": 20,
                    "assumed_publication_lag_days": 90,
                    "news_limit": 20,
                    "sec_user_agent": "Portfolio Review owner@example.org",
                },
            }
        )
        self.issues = []

    def replay(self, **drifts):
        """Patch ``yfinance.Ticker`` with the recorded responses; return the call log."""
        factory, calls = recorded_tickers(**drifts)
        self.enterContext(patch("yfinance.Ticker", factory))
        return calls

    def silent_news(self):
        """Every property the events reader consults, withdrawn the way a release does.

        ``news`` lives in the news fixture, but ``sec_filings`` is served from the
        statements response and ``calendar`` from the estimates one, so all three have to
        be renamed for the reader to find nothing at all.
        """
        return {
            "news": rename(load("yahoo_news"), (), "news", "stories"),
            "statements": rename(load("yahoo_statements"), (), "sec_filings", "filings"),
            "estimates": rename(load("yahoo_estimates"), (), "calendar", "schedule"),
        }

    def security(self, symbol=EQUITY, **fields):
        return {
            "security_id": symbol,
            "ticker": symbol,
            "instrument_type": "etf" if symbol == FUND else "equity",
            "currency": None,
            **fields,
        }

    def call(self, function, symbol=EQUITY, **kwargs):
        return function(
            self.config,
            self.security(symbol),
            refresh=True,
            issues=self.issues,
            as_of=AS_OF,
            **kwargs,
        )

    def collect(self, symbol=EQUITY):
        """Every capability for one security, with the per-capability coverage states."""
        return _collect(self.security(symbol), self.config, AS_OF, refresh=True, issues=self.issues)

    def answer_json(self, payload):
        """Serve one recorded JSON payload to every provider that fetches over HTTP."""
        self.urls = []

        def fake(url, user_agent=None):
            self.urls.append(url)
            return payload, RECEIVED

        self.enterContext(patch.object(providers, "_fetch_json", fake))

    def observations(self, payload, issues):
        self.answer_json(payload)
        return bank_of_canada_observations(
            self.config,
            ["USD", "GBP", "EUR", "CAD"],
            "2026-09-01",
            "2026-09-11",
            refresh=True,
            issues=issues,
        )


class RecordedShapeTests(ContractCase):
    """Each adapter reads the response shape that was recorded from the live provider."""

    def test_price_history_reads_the_recorded_history_frame_and_metadata(self):
        self.replay()
        result = self.call(market_data.price_history)
        self.assertEqual(self.issues, [])
        self.assertEqual(
            [row["date"] for row in result["prices"]],
            ["2026-09-04", "2026-09-08", "2026-09-09", "2026-09-10", "2026-09-11"],
        )
        first = result["prices"][0]
        self.assertAlmostEqual(first["close"], 100.0)
        self.assertAlmostEqual(first["adjusted_close"], 99.0)
        self.assertEqual(first["volume"], 1_100_000)
        self.assertEqual((result["currency"], result["quote_currency"]), ("USD", "USD"))
        self.assertEqual(result["exchange"], "NMS")
        self.assertEqual(result["instrument_type"], "EQUITY")
        self.assertEqual(result["timezone"], "America/New_York")
        self.assertEqual(
            {(row["kind"], row["date"]) for row in result["actions"]},
            {("dividend", "2026-09-09"), ("split", "2026-09-10")},
        )

    def test_statements_read_the_recorded_annual_and_quarterly_frames(self):
        self.replay()
        result = self.call(market_data.statements)
        self.assertEqual(self.issues, [])
        self.assertEqual(result["currency"], "USD")
        self.assertEqual([row["period_end"] for row in result["annual"]][:1], ["2025-12-31"])
        quarter = result["quarterly"][0]
        self.assertEqual(quarter["period_end"], "2026-06-30")
        self.assertAlmostEqual(quarter["revenue"], 260.0)
        self.assertAlmostEqual(quarter["operating_cash_flow"], 70.0)
        # Yahoo reports capital expenditure as a negative cash flow; the outflow is stored.
        self.assertAlmostEqual(quarter["capex"], 20.0)
        self.assertEqual(quarter["earnings_definition"], "common_shareholders")
        self.assertAlmostEqual(result["ttm"]["revenue"], 260.0 + 255.0 + 250.0 + 245.0)
        self.assertEqual(result["ttm"]["missing_fields"], "")
        self.assertEqual([filing["type"] for filing in result["filings"]], ["10-Q", "10-K"])

    def test_estimates_read_the_recorded_consensus_frames_and_calendar(self):
        self.replay()
        result = self.call(market_data.estimates)
        self.assertEqual(self.issues, [])
        self.assertEqual(sorted(result["eps"]), sorted(["0q", "+1q", "0y", "+1y"]))
        self.assertAlmostEqual(result["eps"]["0q"]["avg"], 0.58)
        self.assertEqual(result["eps"]["0q"]["analysts"], 24)
        self.assertEqual(result["currency"], "USD")
        self.assertAlmostEqual(result["revenue"]["+1y"]["avg"], 1180.0)
        self.assertAlmostEqual(result["price_targets"]["median"], 112.0)
        self.assertEqual(result["recommendations"]["strong_buy"], 9)
        self.assertEqual(result["recommendations"]["period"], "0m")
        self.assertEqual(result["earnings_dates"], ["2026-10-29", "2026-11-02"])
        self.assertEqual(result["ex_dividend_date"], "2026-11-06")
        self.assertEqual(result["label"], market_data.ESTIMATE_LABEL)

    def test_fund_disclosure_reads_recorded_holdings_sectors_and_asset_classes(self):
        self.replay()
        result = self.call(market_data.fund_disclosure, FUND)
        self.assertEqual(self.issues, [])
        self.assertEqual(
            [row["holding_symbol"] for row in result["holdings"]],
            ["RY.TO", "SHOP.TO", "ENB.TO", "CNR.TO", None],
        )
        self.assertAlmostEqual(result["holdings"][0]["weight"], 0.062)
        self.assertEqual(result["holdings"][0]["name"], "Example Bank of Canada")
        # The recorded asset classes carry the fund's cash position as its own row.
        self.assertEqual(result["holdings"][-1]["issuer_id"], "CASH")
        self.assertAlmostEqual(result["holdings"][-1]["weight"], 0.019)
        sectors = {row["sector"]: row["weight"] for row in result["sectors"]}
        self.assertAlmostEqual(sectors["Financials"], 0.353)
        self.assertAlmostEqual(sectors["Real Estate"], 0.024)
        self.assertAlmostEqual(result["asset_classes"]["stockPosition"], 0.981)
        self.assertEqual(result["fund_overview"]["legal_type"], "Exchange Traded Fund")
        self.assertEqual(result["unmapped_sectors"], [])

    def test_news_and_filings_read_the_recorded_story_envelope(self):
        self.replay()
        result = self.call(market_data.news_and_filings)
        self.assertEqual(self.issues, [])
        by_kind = {}
        for event in result["events"]:
            by_kind.setdefault(event["kind"], []).append(event)
        self.assertEqual(len(by_kind["news"]), 2)
        story = by_kind["news"][0]
        self.assertEqual(story["title"], "Example Devices reports second-quarter results")
        self.assertEqual(story["event_date"], "2026-09-09")
        self.assertEqual(story["provider"], "Example Newswire")
        self.assertEqual(story["classification"], "third_party_opinion")
        self.assertEqual(story["url"], "https://news.example.com/example-devices-q2")
        self.assertEqual(
            sorted(row["event_date"] for row in by_kind["filing"]),
            ["2026-02-10", "2026-08-03"],
        )
        self.assertEqual(
            sorted(row["event_date"] for row in by_kind["earnings_date"]),
            ["2026-10-29", "2026-11-02"],
        )

    def test_security_profile_reads_the_recorded_info_mapping(self):
        self.replay()
        result = self.call(market_data.security_profile)
        self.assertEqual(self.issues, [])
        self.assertEqual(result["name"], "Example Devices Inc.")
        self.assertEqual(result["sector"], "Technology")
        self.assertEqual(result["industry"], "Consumer Electronics")
        self.assertEqual(result["shares_outstanding"], 100_000_000)
        self.assertEqual(result["financial_currency"], "USD")
        self.assertEqual(result["domicile"], "US")
        self.assertEqual(result["equity_type"], "ordinary_common")
        self.assertEqual(result["exchange"], "NMS")

    def test_every_capability_of_a_recorded_security_reports_coverage(self):
        self.replay()
        record = self.collect()
        self.assertEqual(self.issues, [])
        self.assertEqual(
            record["coverage"],
            {
                "prices": "ok",
                "statements": "ok",
                "estimates": "ok",
                "fund_disclosures": "not_applicable",
                "events": "ok",
                "profile": "ok",
            },
        )
        report = coverage_report({"research_inputs": {EQUITY: record}}, self.config)
        for name in ("prices", "statements", "estimates", "events"):
            with self.subTest(capability=name):
                self.assertEqual(report["capabilities"][name]["missing"], [])
                self.assertEqual(report["capabilities"][name]["available"], 1)


class ValetContractTests(ContractCase):
    """The Bank of Canada Valet observations payload, holiday gap included."""

    def test_recorded_observations_are_read_and_a_holiday_leaves_a_gap(self):
        issues = []
        records = self.observations(load(VALET), issues)
        self.assertEqual(issues, [])
        usd = {row["date"]: row for row in records if row["pair"] == "USDCAD"}
        self.assertEqual(
            sorted(usd),
            ["2026-09-03", "2026-09-04", "2026-09-08", "2026-09-09", "2026-09-10", "2026-09-11"],
        )
        self.assertNotIn("2026-09-07", usd)  # Labour Day: Valet simply omits the day.
        self.assertEqual(usd["2026-09-03"]["rate"], "1.4000")
        self.assertEqual(usd["2026-09-03"]["published_at"], "2026-09-03T16:30:00-04:00")
        eur = {row["date"] for row in records if row["pair"] == "EURCAD"}
        self.assertNotIn("2026-09-08", eur)  # A published row with an empty value is absent.
        cross = next(row for row in records if row["pair"] == "GBPUSD")
        self.assertEqual(cross["derived_via"], "CAD")


class SecContractTests(ContractCase):
    """The SEC ticker list and a companyfacts subset in their published shapes."""

    def test_recorded_ticker_list_resolves_us_listings_to_ciks(self):
        self.answer_json(load(SEC_TICKERS))
        issues = []
        lookup = sec_issuer_lookup(self.config, refresh=True, issues=issues)
        self.assertEqual(issues, [])
        self.assertEqual(lookup({"symbol": "AAPL", "exchange": "NMS"}), "cik:0001234501")
        self.assertEqual(lookup({"symbol": "BRK.B", "exchange": "NYQ"}), "cik:0001234503")
        self.assertIsNone(lookup({"symbol": "RY.TO", "exchange": "TOR"}))

    def test_recorded_companyfacts_subset_yields_one_usd_statement_observation(self):
        result = extract_sec_fundamentals(
            load(SEC_COMPANYFACTS), EQUITY, AS_OF, RECEIVED, "sec:recorded"
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["period_end"], "2025-12-31")
        self.assertEqual(result["currency"], "USD")
        self.assertEqual(result["revenue"], 980_000_000.0)
        self.assertEqual(result["operating_cash_flow"], 258_000_000.0)
        self.assertEqual(result["capex"], 70_000_000.0)
        self.assertEqual(result["assets_begin"], 860_000_000.0)
        self.assertEqual(result["earnings_definition"], "common_shareholders")
        self.assertEqual(result["data_warnings"], [])


class UnreadableDriftTests(ContractCase):
    """A drift that leaves nothing readable: coverage says missing and an issue says why."""

    def assert_named_gap(self, code, *words):
        """One issue, of this code, whose text names the gap rather than an exception."""
        [issue] = self.issues
        self.assertEqual(issue["code"], code)
        self.assertEqual(issue["severity"], "warning")
        self.assertEqual(exception_names(self.issues), [])
        for word in words:
            with self.subTest(word=word):
                self.assertIn(word.lower(), str(issue["message"]).lower())

    def test_a_renamed_adjusted_close_column_leaves_prices_missing_and_named(self):
        prices = load("yahoo_prices")
        drifted = rename(prices, ("history", "columns"), "Adj Close", "AdjustedClose")
        self.replay(prices=drifted)
        self.assertIsNone(self.call(market_data.price_history))
        self.assert_named_gap("PROVIDER_SHAPE_UNRECOGNIZED", "closes")
        self.issues.clear()
        self.assertEqual(self.collect()["coverage"]["prices"], "missing")

    def test_a_withdrawn_income_statement_leaves_statements_missing_and_named(self):
        statements = load("yahoo_statements")
        drifted = rename(statements, (), "income_stmt", "annual_income_stmt")
        drifted = rename(drifted, (), "quarterly_income_stmt", "quarterly_income_statement")
        self.replay(statements=drifted)
        self.assertIsNone(self.call(market_data.statements))
        self.assert_named_gap("PROVIDER_SHAPE_UNRECOGNIZED", "income statement")
        self.issues.clear()
        self.assertEqual(self.collect()["coverage"]["statements"], "missing")

    def test_a_renamed_info_mapping_leaves_the_profile_missing_and_named(self):
        drifted = rename(load("yahoo_profile"), (), "info", "summary_profile")
        self.replay(profile=drifted)
        self.assertIsNone(self.call(market_data.security_profile))
        self.assert_named_gap("PROVIDER_SHAPE_UNRECOGNIZED", "company profile")
        self.issues.clear()
        self.assertEqual(self.collect()["coverage"]["profile"], "missing")

    def test_a_fund_response_without_holdings_or_sectors_is_missing_and_named(self):
        funds = load("yahoo_funds")
        drifted = rename(funds, ("funds_data",), "top_holdings", "topHoldings")
        drifted = rename(drifted, ("funds_data",), "sector_weightings", "sectorWeightings")
        self.replay(funds=drifted)
        self.assertIsNone(self.call(market_data.fund_disclosure, FUND))
        self.assert_named_gap("PROVIDER_SHAPE_UNRECOGNIZED", "fund disclosure")
        self.issues.clear()
        self.assertEqual(self.collect(FUND)["coverage"]["fund_disclosures"], "missing")

    def test_a_shape_drift_is_reported_once_and_not_retried(self):
        prices = load("yahoo_prices")
        drifted = rename(prices, ("history", "columns"), "Adj Close", "AdjustedClose")
        calls = self.replay(prices=drifted)
        self.assertIsNone(self.call(market_data.price_history))
        self.assertEqual([symbol for symbol, field in calls if field == "history"], [EQUITY])

    def test_a_withdrawn_news_filings_and_calendar_leave_events_missing_and_named(self):
        """News is the last reader still on a bare ValueError; its gap must read like the rest."""
        self.replay(**self.silent_news())
        self.assertIsNone(self.call(market_data.news_and_filings))
        self.assert_named_gap("PROVIDER_SHAPE_UNRECOGNIZED", "news", "filings")
        self.issues.clear()
        self.assertEqual(self.collect()["coverage"]["events"], "missing")

    def test_a_withdrawn_news_capability_is_reported_once_and_not_retried(self):
        calls = self.replay(**self.silent_news())
        self.assertIsNone(self.call(market_data.news_and_filings))
        self.assertEqual([symbol for symbol, field in calls if field == "news"], [EQUITY])

    def test_a_renamed_yahoo_fx_close_leaves_amounts_unconverted_and_named(self):
        from portfolio_research.fx_providers import _yahoo_records

        with self.assertRaises(ProviderShapeError) as raised:
            _yahoo_records({"quotes": []}, "USD", "CAD", "yahoo:drift", RECEIVED)
        self.assertIn("yahoo", str(raised.exception).lower())
        with self.assertRaises(ProviderShapeError):
            _yahoo_records([["2026-09-03", 1.4]], "USD", "CAD", "yahoo:drift", RECEIVED)

    def test_a_renamed_valet_observations_key_leaves_amounts_unconverted_and_named(self):
        drifted = rename(load(VALET), (), "observations", "series_observations")
        issues = []
        self.assertEqual(self.observations(drifted, issues), [])
        self.issues = issues
        self.assert_named_gap("FX_PROVIDER_FAILED", "Valet", "unconverted")

    def test_a_renamed_cik_field_leaves_the_sec_ticker_list_unusable_and_named(self):
        payload = load(SEC_TICKERS)
        for record in payload.values():
            record["cik"] = record.pop("cik_str")
        self.answer_json(payload)
        self.assertIsNone(sec_issuer_lookup(self.config, refresh=True, issues=self.issues))
        self.assert_named_gap("SEC_TICKERS_UNAVAILABLE", "SEC ticker list", "no usable records")


class TransientOutageTests(ContractCase):
    """An empty or failing response is an outage, not a drift: it is retried and named so.

    yfinance hides its own exceptions by default and answers an empty frame for a failed
    timezone fetch, a missing-prices error, a Yahoo outage and a plain request failure
    alike. Reading "nothing came back" as a shape change spends the bounded retry that
    increment 4 requires and tells the owner the provider changed when it was only down.
    """

    def empty_prices(self):
        """The recorded prices response with an empty history frame, as an outage gives."""
        prices = load("yahoo_prices")
        prices["history"]["index"] = []
        prices["history"]["columns"] = {name: [] for name in prices["history"]["columns"]}
        return prices

    def test_an_empty_price_history_is_retried_and_reported_as_a_fetch_failure(self):
        calls = self.replay(prices=self.empty_prices())
        self.assertIsNone(self.call(market_data.price_history))
        self.assertEqual(
            [symbol for symbol, field in calls if field == "history"], [EQUITY, EQUITY]
        )
        [issue] = self.issues
        self.assertEqual(issue["code"], "PROVIDER_FETCH_FAILED")

    def test_a_renamed_close_column_is_still_a_drift_and_still_not_retried(self):
        """The drift branch keeps its meaning: a non-empty frame with the wrong names."""
        drifted = rename(load("yahoo_prices"), ("history", "columns"), "Adj Close", "Adjusted")
        calls = self.replay(prices=drifted)
        self.assertIsNone(self.call(market_data.price_history))
        self.assertEqual([symbol for symbol, field in calls if field == "history"], [EQUITY])
        self.assertEqual([issue["code"] for issue in self.issues], ["PROVIDER_SHAPE_UNRECOGNIZED"])

    def test_a_transient_info_failure_is_retried_rather_than_read_as_an_empty_profile(self):
        """Only a withdrawn property is a shape signal; every other failure is transient."""
        factory, _ = recorded_tickers()
        reads = []

        class Unavailable:
            def __init__(self, symbol):
                self._inner = factory(symbol)

            def __getattr__(self, name):
                if name == "info":
                    reads.append(name)
                    raise RuntimeError("connection reset by peer")
                return getattr(self._inner, name)

        self.enterContext(patch("yfinance.Ticker", Unavailable))
        self.assertIsNone(self.call(market_data.security_profile))
        self.assertEqual([issue["code"] for issue in self.issues], ["PROVIDER_FETCH_FAILED"])
        self.assertEqual(reads, ["info", "info"])  # The bounded retry, not one silent read.


class DegradedDriftTests(ContractCase):
    """A drift that leaves the response readable: the capability survives without the fact."""

    def test_a_renamed_metadata_currency_keeps_prices_and_leaves_the_currency_unknown(self):
        prices = load("yahoo_prices")
        drifted = rename(prices, ("history_metadata",), "currency", "currencyCode")
        self.replay(prices=drifted)
        result = self.call(market_data.price_history)
        self.assertEqual(self.issues, [])
        self.assertEqual(len(result["prices"]), 5)
        self.assertIsNone(result["currency"])
        self.assertIsNone(result["prices"][0]["currency"])
        self.assertEqual(result["exchange"], "NMS")

    def test_a_renamed_revenue_line_is_named_among_the_trailing_window_gaps(self):
        statements = load("yahoo_statements")
        drifted = rename(statements, ("quarterly_income_stmt", "rows"), "Total Revenue", "Revenue")
        self.replay(statements=drifted)
        result = self.call(market_data.statements)
        self.assertEqual(self.issues, [])
        self.assertIsNone(result["quarterly"][0]["revenue"])
        self.assertIsNone(result["ttm"]["revenue"])
        self.assertIn("revenue", result["ttm"]["missing_fields"].split(","))

    def test_withdrawn_estimate_frames_leave_estimates_missing_with_targets_intact(self):
        estimates = load("yahoo_estimates")
        drifted = rename(estimates, (), "earnings_estimate", "eps_estimate")
        drifted = rename(drifted, (), "revenue_estimate", "sales_estimate")
        self.replay(estimates=drifted)
        result = self.call(market_data.estimates)
        self.assertEqual(self.issues, [])
        self.assertEqual(result["eps"], {})
        self.assertAlmostEqual(result["price_targets"]["median"], 112.0)
        self.assertEqual(self.collect()["coverage"]["estimates"], "missing")

    def test_a_withdrawn_news_property_keeps_filings_and_scheduled_dates(self):
        news = rename(load("yahoo_news"), (), "news", "stories")
        self.replay(news=news)
        result = self.call(market_data.news_and_filings)
        self.assertEqual(self.issues, [])
        kinds = {event["kind"] for event in result["events"]}
        self.assertNotIn("news", kinds)
        self.assertEqual(kinds, {"filing", "earnings_date"})

    def test_a_renamed_companyfacts_taxonomy_is_no_observation_rather_than_a_failure(self):
        for path, old, new in (
            ((), "facts", "xbrl_facts"),
            (("facts",), "us-gaap", "us_gaap"),
            (("facts", "us-gaap", "Revenues"), "units", "measures"),
        ):
            with self.subTest(renamed=old):
                drifted = rename(load(SEC_COMPANYFACTS), path, old, new)
                result = extract_sec_fundamentals(drifted, EQUITY, AS_OF, RECEIVED, "sec:drift")
                # A taxonomy this reader cannot read is no observation at all; a single
                # renamed tag leaves that one field unresolved and says so.
                if result is None:
                    continue
                self.assertIsNone(result["revenue"])
                self.assertIn(
                    "Unresolved comparable USD TTM fields: revenue", result["data_warnings"]
                )

    def test_a_renamed_unit_key_leaves_the_balance_absent_rather_than_guessed(self):
        drifted = rename(
            load(SEC_COMPANYFACTS), ("facts", "us-gaap", "Assets"), "units", "measures"
        )
        result = extract_sec_fundamentals(drifted, EQUITY, AS_OF, RECEIVED, "sec:drift")
        self.assertIsNone(result["assets"])
        self.assertIsNone(result["assets_begin"])
        self.assertEqual(result["revenue"], 980_000_000.0)


if __name__ == "__main__":
    unittest.main()
