"""Gate: held securities receive source-backed research without manual preparation.

The adapter is patched at ``yfinance.Ticker`` and every provider answer is canned, so
this exercises the wiring — enrichment, coverage, research, readiness and the workflow
stages — without a single network call.
"""

import tempfile
import unittest
from contextlib import ExitStack
from copy import deepcopy
from time import monotonic
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

from portfolio.storage import Store
from portfolio_lab.config import validate_config
from portfolio_lab.pipeline import load_inputs
from portfolio_research import market_data
from portfolio_research.application import analyze_review, validate_workspace
from portfolio_research.calendar import decision_context
from portfolio_research.enrichment import CAPABILITIES, enrich_market
from portfolio_research.fx import FxTable
from portfolio_research.market_data import ADAPTER_VERSION, available_at
from portfolio_research.service import ResearchService, default_config
from portfolio_research.workflow import ReviewWorkflow, provider_status
from tests.support.market_data_adapter import FakeFundsData, FakeTicker, history_frame
from tests.support.normalization import (
    SUNDAY_GENERATED,
    SUNDAY_RECEIPT,
    cad_table,
    listing,
    observation,
    usd_table,
)

SESSIONS = [day.date().isoformat() for day in pd.bdate_range("2025-09-12", "2026-09-11")]
EXCHANGES = {
    "AAPL": "America/New_York",
    "MSFT": "America/New_York",
    "XIC.TO": "America/Toronto",
    "VOD.L": "Europe/London",
}
LAST_CLOSE = {"AAPL": 300.0, "MSFT": 300.0, "XIC.TO": 41.5, "VOD.L": 128.75}
A_ROWS = [["AAPL", "2", "300.00", "600.00"], ["MSFT", "4", "300.00", "1200.00"]]
B_ROWS = [["XIC.TO", "10", "41.50", "415.00"], ["VOD.L", "100", "128.75", "235.00"]]
LISTINGS = {
    "AAPL": listing("AAPL", "USD", "NMS"),
    "MSFT": listing("MSFT", "USD", "NMS"),
    "XIC.TO": listing("XIC.TO", "CAD", "TOR", "etf"),
    "VOD.L": listing("VOD.L", "GBp", "LSE"),
}
RATES = {"CADUSD": "0.7211", "GBPUSD": "1.3150"}
CADUSD = 0.7211
GBPUSD = 1.3150


def closes(symbol):
    """A deterministic series that ends exactly on the captured price."""
    last = LAST_CLOSE[symbol]
    count = len(SESSIONS)
    return [last * (0.8 + 0.2 * (index + 1) / count) for index in range(count)]


class ReviewTicker(FakeTicker):
    """The canned adapter ticker with a full year of sessions and a second US equity."""

    def history(self, start=None, end=None, auto_adjust=False, actions=True, **kwargs):
        self._record("history")
        if self.symbol not in EXCHANGES:
            raise RuntimeError("no history for this symbol")
        dividends = [0.0] * len(SESSIONS)
        dividends[-2] = 0.25
        return history_frame(SESSIONS, EXCHANGES[self.symbol], closes(self.symbol), dividends)

    @property
    def history_metadata(self):
        self._record("history_metadata")
        data = {
            "AAPL": ("USD", "NMS", "EQUITY", "America/New_York"),
            "MSFT": ("USD", "NMS", "EQUITY", "America/New_York"),
            "VOD.L": ("GBp", "LSE", "EQUITY", "Europe/London"),
            "XIC.TO": ("CAD", "TOR", "ETF", "America/Toronto"),
        }[self.symbol]
        return {
            "currency": data[0],
            "exchangeName": data[1],
            "instrumentType": data[2],
            "exchangeTimezoneName": data[3],
            "regularMarketTime": 1789156800,
        }

    @property
    def info(self):
        self._record("info")
        if self.symbol == "MSFT":
            return {
                "financialCurrency": "USD",
                "sector": "Technology",
                "industry": "Software - Infrastructure",
                "sharesOutstanding": 15_000_000_000,
                "marketCap": 3_100_000_000_000,
                "longName": "Microsoft Corporation",
                "country": "United States",
            }
        return FakeTicker.info.fget(self)


class AppleHoldingFunds(FakeFundsData):
    """A fund whose largest holding is also held directly in the portfolio."""

    def __init__(self):
        super().__init__()
        self.top_holdings = pd.DataFrame(
            {"Name": ["Apple Inc."], "Holding Percent": [0.07]},
            index=pd.Index(["AAPL"], name="Symbol"),
        )


class LookThroughTicker(ReviewTicker):
    @property
    def funds_data(self):
        self._record("funds_data")
        if self.symbol != "XIC.TO":
            raise KeyError("no fund data")
        return AppleHoldingFunds()


def fx_records():
    rows = []
    for day in SESSIONS:
        for pair, rate in RATES.items():
            rows.append(observation(pair[:3], pair[3:], rate, day, "yahoo"))
    rows.append(observation("GBP", "CAD", "1.8250", SESSIONS[-1]))
    rows.append(observation("USD", "CAD", "1.3868", SESSIONS[-1]))
    return rows


def issuer_lookup(meta):
    return {"AAPL": "cik:0000320193", "MSFT": "cik:0000789019"}.get(meta.get("symbol"))


def listing_metadata(config, symbol, *, refresh, issues):
    found = LISTINGS.get(symbol)
    return deepcopy(found) if found else None


class EnrichmentGateTests(unittest.TestCase):
    """A current review of four real holdings, enriched from the patched adapter."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.a = self.store.add_source(
            "USD portfolio", url="https://finance.yahoo.com/portfolio/p_fixture_enrich_a"
        )
        self.b = self.store.add_source(
            "CAD portfolio", url="https://finance.yahoo.com/portfolio/p_fixture_enrich_b"
        )
        with patch("portfolio.storage.now", return_value=SUNDAY_RECEIPT):
            batch = self.store.begin_batch([self.a, self.b])
            self.store.ingest_table(self.a, usd_table(A_ROWS), batch_id=batch)
            self.store.ingest_table(
                self.b, cad_table(B_ROWS), batch_id=batch, extension_version="1.2.0"
            )
        config = default_config(self.temp.name)
        config["data"].update(
            mode="live",
            price_provider="yahoo",
            lookback_years=1,
            provider_min_interval_seconds=0,
            provider_timeout_seconds=5,
        )
        config["risk"].update(min_weekly_observations=26, bootstrap_samples=50)
        self.config = validate_config(config)
        FakeTicker.calls = []

    def load(self, extra=()):
        """The resolved, enriched bundle for this fixture with every provider canned."""
        table = FxTable(fx_records(), max_age_days=7)
        patches = [
            patch("yfinance.Ticker", ReviewTicker),
            patch(
                "portfolio_research.market_listings.listing_metadata", side_effect=listing_metadata
            ),
            patch("portfolio_research.fx_providers.load_fx_table", return_value=table),
            patch("portfolio_research.issuers.sec_issuer_lookup", return_value=issuer_lookup),
            *extra,
        ]
        with ExitStack() as stack:
            for context in patches:
                stack.enter_context(context)
            return load_inputs(
                self.config,
                refresh=True,
                review_kind="current",
                generated_at=SUNDAY_GENERATED,
            )

    def review(self, extra=()):
        bundle = self.load(extra)
        return analyze_review(bundle, self.config), bundle

    # Coverage ---------------------------------------------------------------

    def test_a_current_review_reports_coverage_for_every_held_security(self):
        result, bundle = self.review()
        coverage = result["coverage"]
        self.assertEqual(coverage["adapter_version"], ADAPTER_VERSION)
        self.assertEqual(sorted(coverage["capabilities"]), sorted(CAPABILITIES))
        self.assertTrue(coverage["generated_at"])
        self.assertEqual(len(coverage["limitations"]), 3)
        prices = coverage["capabilities"]["prices"]
        self.assertEqual(prices["requested"], 4)
        self.assertEqual(prices["available"], 4)
        self.assertEqual(prices["missing"], [])
        by_security = coverage["by_security"]
        self.assertEqual(sorted(by_security), ["AAPL", "MSFT", "VOD.L", "XIC.TO"])
        self.assertEqual(by_security["AAPL"]["prices"], "ok")
        self.assertEqual(by_security["AAPL"]["estimates"], "ok")
        self.assertEqual(by_security["AAPL"]["fund_disclosures"], "not_applicable")
        self.assertEqual(by_security["XIC.TO"]["fund_disclosures"], "ok")
        self.assertEqual(bundle["coverage"]["by_security"], by_security)

    def test_research_inputs_and_frames_carry_the_adapter_records(self):
        result, bundle = self.review()
        self.assertEqual(sorted(bundle["research_inputs"]), ["AAPL", "MSFT", "VOD.L", "XIC.TO"])
        self.assertTrue(bundle["research_inputs"]["AAPL"]["estimates"]["eps"])
        self.assertEqual(
            bundle["research_inputs"]["XIC.TO"]["fund_overview"]["legal_type"],
            "Exchange Traded Fund",
        )
        sectors = [row for row in result["fund_sectors"] if row["fund_id"] == "XIC.TO"]
        self.assertIn("Financials", {row["sector"] for row in sectors})
        events = bundle["events"]
        self.assertIn("news", set(events.kind))
        statements = bundle["fundamentals"]
        apple = statements[statements.security_id == "AAPL"]
        # One row per period end: annual periods and the trailing window, never a quarter
        # standing in for a year.
        self.assertEqual(set(apple.period_type), {"annual", "ttm"})
        self.assertEqual(len(apple), len(set(apple.period_end)))
        trailing = apple[apple.period_type == "ttm"].iloc[0]
        self.assertAlmostEqual(float(trailing["revenue"]), 104.0 + 103.0 + 102.0 + 101.0)
        self.assertAlmostEqual(float(trailing["depreciation"]), 4.0 * 4)
        self.assertAlmostEqual(float(trailing["tax_provision"]), 6.0 + 5.8 + 5.6 + 5.4)
        # Yahoo's cash-flow sign negated once, at the adapter seam, then summed.
        self.assertAlmostEqual(float(trailing["change_working_capital"]), 2.0 * 4)
        self.assertTrue(any(source["provider"] == "yahoo_prices" for source in result["sources"]))

    def test_fund_disclosure_rows_carry_cash_and_an_undated_basis(self):
        """The merge itself, before any as-of filter drops a receipt-dated snapshot."""
        bundle = {
            "issues": [],
            "sources": [],
            "securities": pd.DataFrame(
                [
                    {
                        "security_id": "XIC.TO",
                        "ticker": "XIC.TO",
                        "instrument_type": "etf",
                        "currency": "CAD",
                        "resolution_status": "resolved",
                    }
                ]
            ),
        }
        with patch("yfinance.Ticker", ReviewTicker):
            enrich_market(
                bundle,
                {**self.config, "data": {**self.config["data"], "refresh_network": True}},
                "2026-09-11",
            )
        holdings = bundle["fund_holdings"]
        self.assertFalse(holdings.empty)
        self.assertIn("CASH", set(holdings.issuer_id))
        self.assertLessEqual(float(holdings.weight.sum()), 1.0)
        self.assertEqual(set(holdings.disclosure_basis), {"provider_snapshot_undated"})
        self.assertEqual(
            set(bundle["fund_sectors"].sector) & {"Financials", "Technology", "Real Estate"},
            {"Financials", "Technology", "Real Estate"},
        )
        self.assertEqual(bundle["coverage"]["by_security"]["XIC.TO"]["fund_disclosures"], "ok")

    def test_a_freshly_fetched_fund_disclosure_survives_the_review_cutoff(self):
        """Coverage may never claim look-through the as-of filter has already removed."""
        result, bundle = self.review()
        holdings = bundle["fund_holdings"]
        self.assertFalse(holdings.empty)
        self.assertEqual(set(holdings.fund_id), {"XIC.TO"})
        self.assertEqual(result["coverage"]["by_security"]["XIC.TO"]["fund_disclosures"], "ok")
        excluded = [
            issue
            for issue in bundle["issues"]
            if issue.get("code") == "ASOF_ROWS_EXCLUDED"
            and "fund_holdings" in str(issue.get("message"))
        ]
        self.assertEqual(excluded, [])
        readiness = {row["output"]: row for row in result["readiness"]}
        look_through = [
            row
            for row in readiness["exposure"]["requirements"]
            if row["name"] == "fund_look_through"
        ][0]
        self.assertTrue(look_through["met"], look_through["detail"])

    def test_a_backdated_review_says_missing_instead_of_claiming_look_through(self):
        """Coverage must never report a snapshot the review's own cutoff excludes."""
        bundle = {
            "issues": [],
            "sources": [],
            "timeline": {
                "review_kind": "historical",
                "information_cutoff": "2024-06-28T20:00:00+00:00",
            },
            "securities": pd.DataFrame(
                [
                    {
                        "security_id": "XIC.TO",
                        "ticker": "XIC.TO",
                        "instrument_type": "etf",
                        "currency": "CAD",
                        "resolution_status": "resolved",
                    }
                ]
            ),
        }
        with patch("yfinance.Ticker", ReviewTicker):
            enrich_market(
                bundle,
                {**self.config, "data": {**self.config["data"], "refresh_network": True}},
                "2024-06-28",
            )
        self.assertTrue(bundle["fund_holdings"].empty)
        self.assertEqual(bundle["coverage"]["by_security"]["XIC.TO"]["fund_disclosures"], "missing")
        self.assertIn(
            "FUND_DISCLOSURE_AFTER_CUTOFF", {issue.get("code") for issue in bundle["issues"]}
        )

    def test_fund_holdings_resolve_onto_a_directly_held_issuer(self):
        """Look-through only aggregates when a holding carries the issuer it belongs to."""
        bundle = {
            "issues": [],
            "sources": [],
            "securities": pd.DataFrame(
                [
                    {
                        "security_id": "AAPL",
                        "ticker": "AAPL",
                        "instrument_type": "equity",
                        "currency": "USD",
                        "resolution_status": "resolved",
                        "issuer_id": "cik:0000320193",
                    },
                    {
                        "security_id": "XIC.TO",
                        "ticker": "XIC.TO",
                        "instrument_type": "etf",
                        "currency": "CAD",
                        "resolution_status": "resolved",
                        "issuer_id": None,
                    },
                ]
            ),
        }
        with patch("yfinance.Ticker", LookThroughTicker):
            enrich_market(
                bundle,
                {**self.config, "data": {**self.config["data"], "refresh_network": True}},
                "2026-09-11",
            )
        issuers = set(bundle["fund_holdings"].issuer_id)
        self.assertIn("cik:0000320193", issuers)
        self.assertNotIn("listing:AAPL", issuers)

    def test_a_raising_capability_is_confined_to_its_security(self):
        """A pure helper that raises must cost one capability, never the whole run."""
        real = market_data.price_history

        def prices(config, security, **kwargs):
            if security.get("security_id") == "AAPL":
                raise ValueError("A split ratio must be a positive number")
            return real(config, security, **kwargs)

        extra = (patch("portfolio_research.market_data.price_history", side_effect=prices),)
        result, bundle = self.review(extra)
        self.assertEqual(result["coverage"]["by_security"]["AAPL"]["prices"], "missing")
        self.assertEqual(result["coverage"]["by_security"]["MSFT"]["prices"], "ok")
        failures = [
            issue
            for issue in bundle["issues"]
            if issue.get("code") == "RESEARCH_CAPABILITY_FAILED"
            and issue.get("security_id") == "AAPL"
        ]
        self.assertEqual(len(failures), 1)
        self.assertTrue(len(bundle["prices"][bundle["prices"].security_id == "MSFT"]))
        self.assertIsNotNone(result["research"]["MSFT"]["proposals"]["dcf"])

    def test_prices_without_a_quote_currency_are_not_reported_as_covered(self):
        """A close with no unit states no amount, so the coverage line may not say ok.

        Against the pinned yfinance the whole chart metadata arrived in a shape the reader
        dropped, so every live close lost its currency while coverage still read ``ok``.
        The readiness gate reads that line and nothing else, so the one surface that could
        have stopped the review saw a fully covered price series.
        """
        real = market_data.price_history

        def prices(config, security, **kwargs):
            found = real(config, security, **kwargs)
            if security.get("security_id") != "AAPL":
                return found
            unlabelled = dict(found, currency=None)
            unlabelled["prices"] = [dict(row, currency=None) for row in found["prices"]]
            return unlabelled

        extra = (patch("portfolio_research.market_data.price_history", side_effect=prices),)
        result, bundle = self.review(extra)
        coverage = result["coverage"]
        self.assertEqual(coverage["by_security"]["AAPL"]["prices"], "missing")
        self.assertEqual(coverage["by_security"]["MSFT"]["prices"], "ok")
        self.assertEqual(coverage["capabilities"]["prices"]["missing"], ["AAPL"])
        unlabelled = [
            issue
            for issue in bundle["issues"]
            if issue.get("code") == "MISSING_QUOTE_CURRENCY" and issue.get("security_id") == "AAPL"
        ]
        self.assertTrue(unlabelled)
        self.assertIn("quote currency", unlabelled[0]["detail"])

    def test_a_raising_brief_is_confined_to_its_security(self):
        real = market_data.security_profile

        def profile(config, security, **kwargs):
            found = real(config, security, **kwargs)
            if security.get("security_id") == "AAPL":
                raise ValueError("A split ratio must be a positive number")
            return found

        with patch("portfolio_research.briefs.build_brief", side_effect=ValueError("boom")):
            result, bundle = self.review()
        self.assertIsNone(result["research"]["AAPL"]["brief"])
        self.assertIn("RESEARCH_BUILD_FAILED", {issue.get("code") for issue in bundle["issues"]})
        self.assertIsNotNone(result["research"]["AAPL"]["proposals"]["eps"])

    def test_securities_gain_sector_identity_and_domicile_from_the_provider(self):
        bundle = self.load()
        securities = {row["security_id"]: row for row in bundle["securities"].to_dict("records")}
        self.assertEqual(securities["AAPL"]["sector"], "Technology")
        self.assertEqual(securities["AAPL"]["industry"], "Consumer Electronics")
        self.assertEqual(securities["AAPL"]["domicile"], "US")
        self.assertEqual(securities["AAPL"]["equity_type"], "ordinary_common")
        self.assertEqual(securities["AAPL"]["cik"], "0000320193")
        self.assertAlmostEqual(securities["MSFT"]["shares_outstanding"], 15_000_000_000)
        self.assertEqual(securities["XIC.TO"]["instrument_type"], "etf")
        # A historical observation date never takes today's market capitalisation.
        self.assertIsNone(securities["AAPL"]["market_cap_as_of"])

    # Prices and currency ----------------------------------------------------

    def test_foreign_listings_are_converted_once_at_each_row_date(self):
        _, bundle = self.review()
        prices = bundle["prices"]
        self.assertIn("fx_pair", prices.columns)
        etf = prices[prices.security_id == "XIC.TO"].sort_values("date").iloc[-1]
        self.assertEqual(etf["currency"], "USD")
        self.assertEqual(etf["local_currency"], "CAD")
        self.assertEqual(etf["fx_pair"], "CADUSD")
        self.assertAlmostEqual(float(etf["close"]), 41.5 * CADUSD, places=6)
        self.assertEqual(etf["fx_observation_date"], SESSIONS[-1])
        pence = prices[prices.security_id == "VOD.L"].sort_values("date").iloc[-1]
        self.assertEqual(pence["local_currency"], "GBP")
        self.assertAlmostEqual(float(pence["local_close"]), 1.2875, places=6)
        self.assertAlmostEqual(float(pence["close"]), 1.2875 * GBPUSD, places=6)
        usd = prices[prices.security_id == "AAPL"].sort_values("date").iloc[-1]
        self.assertTrue(pd.isna(usd["fx_pair"]) or usd["fx_pair"] is None)
        self.assertAlmostEqual(float(usd["close"]), 300.0, places=6)

    # Research ---------------------------------------------------------------

    def test_equities_receive_a_cited_brief_and_reviewable_proposals(self):
        result, _ = self.review()
        research = result["research"]
        self.assertEqual(sorted(research), ["AAPL", "MSFT", "VOD.L", "XIC.TO"])
        brief = research["AAPL"]["brief"]
        self.assertEqual(brief["method_version"], "brief-template-1")
        self.assertEqual(brief["basis"], "proposed_from_structured_facts")
        self.assertTrue(brief["sources"])
        known = {source["id"] for source in brief["sources"]}
        for bullet in brief["key_financial_developments"] + brief["market_expectations"]:
            self.assertTrue(set(bullet["source_ids"]) <= known)
        eps = research["AAPL"]["proposals"]["eps"]
        self.assertEqual(eps["proposal_meta"]["basis"], "proposed")
        self.assertEqual(eps["currency"], "USD")
        self.assertAlmostEqual(eps["starting_price"], 300.0, places=6)
        dcf = research["AAPL"]["proposals"]["dcf"]
        self.assertEqual(dcf["company_type"], "nonfinancial")
        # The trailing change in working capital reaches the engine as reinvestment,
        # not as the cash effect Yahoo reports.
        first, second = dcf["projections"][0], dcf["projections"][1]
        self.assertGreater(first["change_working_capital"], 0)
        self.assertAlmostEqual(
            second["invested_capital"],
            first["invested_capital"]
            + first["capex"]
            - first["depreciation"]
            + first["change_working_capital"],
            places=6,
        )
        self.assertIn("discount_rate", dcf["proposal_meta"]["assumptions_visible"])
        self.assertEqual(research["AAPL"]["coverage"]["statements"], "ok")

    def test_a_fund_gets_no_company_proposal_and_says_why(self):
        result, _ = self.review()
        fund = result["research"]["XIC.TO"]
        self.assertIsNone(fund["proposals"]["eps"])
        self.assertIsNone(fund["proposals"]["dcf"])
        self.assertTrue(fund["proposals"]["reasons"])

    def test_a_fund_is_never_asked_for_an_income_statement(self):
        """A fund has no income statement by construction, exactly as it has no estimates.

        Asking anyway spends a provider round trip per ETF per review and lands a
        permanent ``missing`` plus an unresolvable warning in the exceptions surface,
        because the provider answers "no fundamentals data found for symbol".
        """
        asked = []
        real = market_data.statements

        def statements(config, security, **kwargs):
            asked.append(security.get("security_id"))
            return real(config, security, **kwargs)

        extra = (patch("portfolio_research.market_data.statements", side_effect=statements),)
        result, bundle = self.review(extra)
        coverage = result["coverage"]
        self.assertEqual(coverage["by_security"]["XIC.TO"]["statements"], "not_applicable")
        self.assertNotIn("XIC.TO", asked)
        self.assertNotIn("XIC.TO", coverage["capabilities"]["statements"]["missing"])
        self.assertFalse(
            [
                issue
                for issue in bundle["issues"]
                if issue.get("security_id") == "XIC.TO"
                and issue.get("code") == "PROVIDER_SHAPE_UNRECOGNIZED"
            ]
        )

    def test_missing_statements_block_only_that_security(self):
        real = market_data.statements

        def statements(config, security, **kwargs):
            if security.get("security_id") == "AAPL":
                return None
            return real(config, security, **kwargs)

        extra = (patch("portfolio_research.market_data.statements", side_effect=statements),)
        result, bundle = self.review(extra)
        self.assertEqual(result["coverage"]["by_security"]["AAPL"]["statements"], "missing")
        self.assertEqual(result["coverage"]["by_security"]["MSFT"]["statements"], "ok")
        self.assertIn("AAPL", result["coverage"]["capabilities"]["statements"]["missing"])
        self.assertIsNone(result["research"]["AAPL"]["proposals"]["dcf"])
        self.assertIsNotNone(result["research"]["AAPL"]["proposals"]["eps"])
        self.assertIsNotNone(result["research"]["MSFT"]["proposals"]["dcf"])
        self.assertFalse(bundle["fundamentals"][bundle["fundamentals"].security_id == "AAPL"].size)
        self.assertTrue(bundle["fundamentals"][bundle["fundamentals"].security_id == "MSFT"].size)

    # Partial results and readiness -----------------------------------------

    def test_unreconciled_accounts_still_produce_covered_holdings_risk(self):
        result, _ = self.review()
        self.assertFalse(result["summary"]["complete"])
        self.assertEqual(result["risk"]["scope"], "covered_holdings")
        self.assertEqual(result["scenarios"]["scope"], "covered_holdings")
        readiness = {row["output"]: row for row in result["readiness"]}
        self.assertEqual(
            sorted(readiness),
            sorted(
                [
                    "baskets",
                    "candidates",
                    "captured_holdings",
                    "company_valuation",
                    "exposure",
                    "risk",
                    "stress",
                    "usd_value",
                ]
            ),
        )
        self.assertEqual(readiness["company_valuation"]["status"], "ready")

    # Workflow wiring --------------------------------------------------------

    def test_provider_status_reports_each_research_capability(self):
        bundle = self.load()
        status = provider_status(bundle, self.config, refresh=True)
        self.assertEqual(status["research_prices"]["status"], "ok")
        self.assertEqual(status["research_prices"]["requested"], 4)
        self.assertEqual(status["research_statements"]["status"], "ok")
        self.assertEqual(status["research_fund_disclosures"]["covered"], 1)
        self.assertIn("yahoo_prices", status)

    def test_a_current_collected_review_records_its_valuation_tolerance(self):
        bundle = self.load()
        stage = SimpleNamespace(
            bundle=bundle,
            config=deepcopy(self.config),
            review_kind="current",
            started=monotonic(),
            workflow_id="workflow-1",
            operation_key="operation-1",
            result=None,
            _begin=lambda *args, **kwargs: None,
            _end=lambda *args, **kwargs: None,
        )
        ReviewWorkflow._analyzing(stage)
        self.assertEqual(stage.result["metadata"]["valuation_tolerance"], 0.02)
        self.assertEqual(self.config["allocation"]["valuation_tolerance"], 0.001)


class WorkspaceAllowlistTests(unittest.TestCase):
    """Proposals may be copied into the workspace with their origin stated."""

    def workspace(self, **fields):
        return {"valuations": {"AAPL": {"eps": {"starting_price": 100.0}, **fields}}}

    def test_a_prefilled_valuation_keeps_its_proposal_meta(self):
        payload = self.workspace(origin="prefill", proposal_meta={"basis": "proposed"})
        self.assertEqual(validate_workspace(payload), payload)

    def test_an_unknown_origin_is_refused(self):
        with self.assertRaises(ValueError):
            validate_workspace(self.workspace(origin="invented"))


class DividendWindowTests(unittest.TestCase):
    """Trailing distributions are windowed on the review date, not the last payment."""

    def record(self, dates):
        return {
            "security": {"security_id": "ACME"},
            "prices": {
                "actions": [
                    {
                        "security_id": "ACME",
                        "date": day,
                        "kind": "dividend",
                        "value": 1.0,
                        "currency": "USD",
                    }
                    for day in dates
                ]
            },
        }

    def test_a_dividend_series_that_stopped_leaves_no_trailing_distribution(self):
        from portfolio_research.research_build import _dividends

        reasons = []
        stopped = self.record(["2023-03-15", "2023-06-15", "2023-09-15", "2023-12-15"])
        self.assertIsNone(_dividends(stopped, "2025-06-30", reasons))
        self.assertTrue(reasons)
        self.assertIn("2023-12-15", reasons[0]["detail"])

    def test_the_trailing_window_ends_at_the_review_date(self):
        from portfolio_research.research_build import _dividends

        paying = self.record(["2025-09-15", "2025-12-15", "2026-03-15", "2026-06-15", "2026-09-15"])
        self.assertAlmostEqual(_dividends(paying, "2026-06-30", []), 4.0)


class PriorForecastTests(unittest.TestCase):
    """A matured prior forecast scores against adapter-shaped price rows."""

    def frozen(self):
        return {
            "as_of": "2026-09-03",
            "mode": "offline",
            "timeline": decision_context("2026-09-03", generated_at="2026-09-03T21:00:00Z"),
        }

    def forecasts(self):
        return [
            {
                "security_id": "AAPL",
                "forecast_date": "2026-09-03T19:00:00Z",
                "horizon_months": 12,
                "scenario": label,
                "return_value": value,
                "probability": probability,
                "basis": "subjective",
            }
            for label, value, probability in [
                ("adverse", -0.2, 0.25),
                ("central", 0.1, 0.5),
                ("favorable", 0.4, 0.25),
            ]
        ]

    def adapter_prices(self):
        """Price rows exactly as the adapter emits them: aware session-close stamps."""
        return pd.DataFrame(
            [
                {
                    "security_id": "AAPL",
                    "date": day,
                    "close": price,
                    "adjusted_close": price,
                    "currency": "USD",
                    "available_at": available_at(day),
                    "received_at": (
                        pd.Timestamp(available_at(day)) + pd.Timedelta(minutes=30)
                    ).isoformat(),
                    "source_id": "yahoo_prices:abc",
                    "adapter_version": ADAPTER_VERSION,
                }
                for day, price in (("2026-09-04", 100.0), ("2027-09-07", 120.0))
            ]
        )

    def test_evaluate_prior_scores_a_matured_forecast(self):
        appended = []
        store = SimpleNamespace(
            list_runs=lambda: [{"run_id": "old", "as_of": "2026-09-03"}],
            load_run=lambda run_id: {"forecast_inputs": self.forecasts()},
            load_bundle=lambda run_id: self.frozen(),
            append_record=lambda kind, payload, run_id=None: appended.append(
                (kind, payload, run_id)
            ),
        )
        service = SimpleNamespace(store=store)
        current = {"run_id": "new", "metadata": {"as_of": "2027-09-07"}}
        observations = {
            "prices": self.adapter_prices(),
            "timeline": {"generated_at": "2027-09-07T21:00:00+00:00"},
        }
        ResearchService._evaluate_prior(service, current, observations)
        self.assertEqual(len(appended), 1)
        kind, payload, run_id = appended[0]
        self.assertEqual((kind, run_id), ("evaluation", "old"))
        self.assertEqual(payload["status"], "evaluated")
        self.assertEqual(len(payload["results"]), 1)
        row = payload["results"][0]
        self.assertEqual(row["status"], "scored")
        self.assertEqual(row["start_date"], "2026-09-04")
        self.assertEqual(row["end_date"], "2027-09-07")
        self.assertAlmostEqual(row["realized_return"], 0.2)


if __name__ == "__main__":
    unittest.main()
