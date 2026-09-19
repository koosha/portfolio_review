"""Gate: an unowned screened candidate reaches the dashboard through the whole flow.

Everything is canned. ``yfinance.screen`` answers one synthetic Technology screen,
``yfinance.Ticker`` answers prices, statements and estimates for every symbol in it,
and the listing, FX and issuer providers answer from fixtures, so this exercises the
wiring — acquisition, qualification, bounded enrichment, scoring, priorities and the
candidate comparison — without a single network call.

The screen is honest about size: twenty-four issuers carry a full three years of
sessions and four quarters of statements, which is more than ``min_sector_size``, so
the Technology cross-section is a real cross-section rather than a threshold that was
lowered to fit a fixture.
"""

import tempfile
import time
import unittest
from contextlib import ExitStack
from copy import deepcopy
from unittest.mock import patch

import pandas as pd

from portfolio.storage import Store
from portfolio_lab.config import validate_config
from portfolio_research.adapter import account_id
from portfolio_research.calendar import trailing_sessions
from portfolio_research.enrichment import acquire_candidates, enrich_market
from portfolio_research.fx import FxTable
from portfolio_research.scenarios import DEFAULT_SHARED_STATE
from portfolio_research.service import ResearchService, default_config
from tests.test_market_data import (
    AAPL_ANNUAL_CASHFLOW,
    AAPL_ANNUAL_INCOME,
    AAPL_QUARTERLY_BALANCE,
    AAPL_QUARTERLY_CASHFLOW,
    AAPL_QUARTERLY_INCOME,
    ANNUAL_ENDS,
    QUARTER_ENDS,
    FakeTicker,
    history_frame,
    statement_frame,
)
from tests.test_normalize import SUNDAY_GENERATED, SUNDAY_RECEIPT, listing, observation

AS_OF = "2026-09-11"
SESSIONS = trailing_sessions(AS_OF, 760)
BILLION = 1_000_000_000
HELD = "HELDCO"
WITHHELD = "NOPRICE"
CANDIDATES = tuple(f"TEC{number:02d}" for number in range(1, 25))
SCREENED = (*CANDIDATES, WITHHELD)
EXCLUDED_ROWS = ("PFDCO-P", "BANKCO")
INDEX = {symbol: number for number, symbol in enumerate(SCREENED, start=1)}
WATCHED = CANDIDATES[-1]
# Money lines scale per issuer so quality, value and momentum are real cross-sections
# instead of twenty-four identical rows that would tie at the median.
SCALED_LINES = {
    "Total Revenue",
    "Gross Profit",
    "Operating Income",
    "Net Income",
    "Net Income Common Stockholders",
    "Pretax Income",
    "Tax Provision",
    "Operating Cash Flow",
}


def company(symbol):
    return f"Synthetic {symbol} Corporation"


def market_cap(symbol):
    return (80 - INDEX.get(symbol, 30)) * BILLION


def factor(symbol):
    return 1.0 + 0.04 * INDEX.get(symbol, 0)


def closes(symbol):
    """A deterministic price path whose drift rises with the issuer's number."""
    drift = 0.10 + 0.01 * INDEX.get(symbol, 0)
    count = len(SESSIONS)
    return [40.0 * (1 + drift * (step + 1) / count) for step in range(count)]


def scaled(rows, symbol):
    value = factor(symbol)
    return {
        name: [item * value if name in SCALED_LINES else item for item in values]
        for name, values in rows.items()
    }


def quote(symbol, *, quote_type="EQUITY", currency="USD", exchange="NMS"):
    return {
        "symbol": symbol,
        "quoteType": quote_type,
        "currency": currency,
        "exchange": exchange,
        "fullExchangeName": "NasdaqGS",
        "longName": company(symbol),
        "marketCap": market_cap(symbol),
        "sharesOutstanding": 1_000_000_000,
    }


SCREEN_QUOTES = [
    *(quote(symbol) for symbol in SCREENED),
    quote(EXCLUDED_ROWS[0]),
    quote(EXCLUDED_ROWS[1]),
]


class FakeScreen:
    """One page of synthetic quotes, recording how it was asked."""

    def __init__(self):
        self.calls = []

    def __call__(self, query, offset=None, size=None, sortField=None, sortAsc=None, **kwargs):
        self.calls.append({"offset": offset, "size": size, "sortField": sortField})
        start = int(offset or 0)
        rows = SCREEN_QUOTES[start : start + int(size or 250)]
        return {
            "start": start,
            "count": len(rows),
            "total": len(SCREEN_QUOTES),
            "quotes": [dict(row) for row in rows],
        }


class SyntheticTicker(FakeTicker):
    """Three years of sessions and four quarters of statements for every symbol.

    ``NOPRICE`` answers every question except price history, which is how a candidate
    reaches the comparison with a gap that belongs to it alone.
    """

    def history(self, start=None, end=None, auto_adjust=False, actions=True, **kwargs):
        self._record("history")
        if self.symbol == WITHHELD:
            raise RuntimeError("no price history for this symbol")
        return history_frame(SESSIONS, "America/New_York", closes(self.symbol))

    @property
    def info(self):
        self._record("info")
        return {
            "financialCurrency": "USD",
            "sector": "Financial Services" if self.symbol == EXCLUDED_ROWS[1] else "Technology",
            "industry": "Software - Infrastructure",
            "sharesOutstanding": 1_000_000_000,
            "marketCap": market_cap(self.symbol),
            "longName": company(self.symbol),
            "country": "United States",
        }

    @property
    def quarterly_income_stmt(self):
        self._record("quarterly_income_stmt")
        return statement_frame(QUARTER_ENDS, scaled(AAPL_QUARTERLY_INCOME, self.symbol))

    @property
    def income_stmt(self):
        self._record("income_stmt")
        return statement_frame(ANNUAL_ENDS, scaled(AAPL_ANNUAL_INCOME, self.symbol))

    @property
    def quarterly_cashflow(self):
        self._record("quarterly_cashflow")
        return statement_frame(QUARTER_ENDS, scaled(AAPL_QUARTERLY_CASHFLOW, self.symbol))

    @property
    def cashflow(self):
        self._record("cashflow")
        return statement_frame(ANNUAL_ENDS, scaled(AAPL_ANNUAL_CASHFLOW, self.symbol))

    @property
    def quarterly_balance_sheet(self):
        self._record("quarterly_balance_sheet")
        return statement_frame(QUARTER_ENDS, AAPL_QUARTERLY_BALANCE)

    @property
    def funds_data(self):
        self._record("funds_data")
        raise KeyError("no fund data")


def holdings_table(symbol=HELD, shares="200", price="44.00", cash="50000"):
    """A count-verified Yahoo capture of one holding and the account's cash."""
    return {
        "method": "yahoo-holdings-table-v1",
        "headers": ["Symbol", "Shares", "Last Price", "Market Value ($)"],
        "rows": [
            [symbol, shares, price, str(float(shares) * float(price))],
            ["Total Cash", "", cash, ""],
        ],
        "page_count": 1,
        "expected_count": 2,
        "completeness": "count-verified",
    }


def listing_metadata(config, symbol, *, refresh, issues):
    known = {HELD, *SCREENED, *EXCLUDED_ROWS}
    return dict(listing(symbol, "USD", "NMS"), name=company(symbol)) if symbol in known else None


def fx_table():
    return FxTable([observation("CAD", "USD", "0.7211", AS_OF, "yahoo")], max_age_days=7)


def candidate_securities(symbols):
    """A securities frame of screened candidates in the shape the qualifier returns."""
    return pd.DataFrame(
        [
            {
                "security_id": symbol,
                "ticker": symbol,
                "issuer_id": f"name:{symbol}",
                "name": company(symbol),
                "sector": "Technology",
                "instrument_type": "equity",
                "currency": "USD",
                "cik": None,
                "eligible": True,
                "market_cap": market_cap(symbol),
                "market_cap_as_of": AS_OF,
                "market_cap_available_at": SUNDAY_RECEIPT,
                "market_cap_received_at": SUNDAY_RECEIPT,
                "exchange": "NMS",
                "domicile": "US",
                "equity_type": "ordinary_common",
                "resolution_status": "resolved",
                "owned": False,
                "in_scope": True,
                "candidate": True,
            }
            for symbol in symbols
        ]
    )


def provider_patches(screen):
    """Every provider this fixture answers for, with the provider clock pinned."""
    return [
        patch("yfinance.Ticker", SyntheticTicker),
        patch("yfinance.screen", screen),
        patch("portfolio_lab.providers._now", return_value=SUNDAY_RECEIPT),
        patch("portfolio_research.market_listings.listing_metadata", side_effect=listing_metadata),
        patch("portfolio_research.fx_providers.load_fx_table", return_value=fx_table()),
        patch("portfolio_research.issuers.sec_issuer_lookup", return_value=None),
        patch("portfolio_lab.providers._fetch_json", side_effect=AssertionError("network")),
    ]


class CandidateFlowGateTests(unittest.TestCase):
    """One published current review that acquired, enriched and compared candidates."""

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.store = Store(cls.temp.name)
        source = cls.store.add_source(
            "Taxable account", url="https://finance.yahoo.com/portfolio/p_fixture_candidates"
        )
        with patch("portfolio.storage.now", return_value=SUNDAY_RECEIPT):
            batch = cls.store.begin_batch([source])
            captured = cls.store.ingest_table(source, holdings_table(), batch_id=batch)
        cls.account = account_id(source)
        config = default_config(cls.temp.name)
        config["data"].update(
            mode="live",
            price_provider="yahoo",
            sec_enabled=False,
            lookback_years=3,
            provider_min_interval_seconds=0,
            provider_timeout_seconds=5,
        )
        config["risk"].update(bootstrap_samples=50)
        config["signals"].update(watchlist=[WATCHED])
        # The benchmark is one of the screened issuers, so the fixture needs no second
        # instrument type; it also keeps the contrast the engine draws, because a named
        # benchmark blocks the comparison when its own inputs are missing while a
        # screened candidate only excludes itself.
        config["mandate"].update(
            confirmed=True,
            benchmark_id=CANDIDATES[0],
            account_candidate_policy={cls.account: "eligible_universe"},
            issuer_cap=0.25,
            sector_cap=0.95,
            min_cash_weight=0.05,
            max_turnover=0.60,
            max_volatility=0.45,
            max_stress_loss=0.45,
        )
        config["allocation"].update(
            active_sleeve_weight=0.2,
            sleeve_budget_basis="account_nav",
            sleeve_membership={cls.account: {}},
            cash_return=0.04,
            shared_state={
                **deepcopy(DEFAULT_SHARED_STATE),
                "probabilities": {"Adverse": 0.25, "Central": 0.5, "Favorable": 0.25},
            },
        )
        cls.config = validate_config(config)
        cls.service = ResearchService(cls.config)
        # The owner's attested balances: a comparison may only propose trades in an
        # account whose holdings, cash and NAV reconcile.
        cls.service.store.append_record(
            "supplemental",
            {
                "version": 1,
                "accounts": [
                    {
                        "source_id": source,
                        "snapshot_id": captured["snapshot_id"],
                        "valuation_date": AS_OF,
                        "cash": "50000",
                        "total_value": "58800",
                        "currency": "USD",
                        "position_currency": "USD",
                        "account_type": "retirement",
                        "complete": True,
                    }
                ],
                "securities": [
                    {
                        "source_id": source,
                        "raw_symbol": HELD,
                        "security_id": HELD,
                        "issuer_id": f"listing:{HELD}",
                        "ticker": HELD,
                        "name": company(HELD),
                        "instrument_type": "equity",
                        "currency": "USD",
                        "exchange": "NMS",
                        "valid_from": AS_OF,
                    }
                ],
                "tax_lots": [],
            },
        )
        cls.screen = FakeScreen()
        with ExitStack() as stack:
            for context in provider_patches(cls.screen):
                stack.enter_context(context)
            stack.enter_context(
                patch("portfolio_research.workflow.now", return_value=SUNDAY_GENERATED)
            )
            started = cls.service.start_workflow(
                {
                    "operation_key": "operation-candidate-flow-1",
                    "review_kind": "current",
                    "collect": False,
                }
            )
            cls.workflow = cls.finish(cls.service, started["workflow_id"])
        if cls.workflow["status"] != "complete":
            raise AssertionError(cls.workflow)
        cls.result = cls.service.run(cls.workflow["run_id"])["result"]

    @classmethod
    def tearDownClass(cls):
        cls.service.close()
        cls.temp.cleanup()

    @staticmethod
    def finish(service, workflow_id, timeout=600):
        deadline = time.monotonic() + timeout
        while True:
            workflow = service.workflow(workflow_id)
            if workflow["status"] in {"complete", "failed", "cancelled"}:
                return workflow
            if time.monotonic() > deadline:
                raise AssertionError(f"Workflow never finished: {workflow}")
            time.sleep(0.05)

    # Acquisition and coverage ----------------------------------------------

    def test_the_review_states_its_candidate_scope_and_what_it_enriched(self):
        universe = self.result["universe"]
        counts = universe["counts"]
        self.assertEqual(counts["acquired"], len(SCREEN_QUOTES))
        self.assertEqual(counts["qualified"], len(SCREENED))
        self.assertEqual(counts["in_scope"], len(SCREENED))
        self.assertEqual(counts["enriched"], len(SCREENED))
        self.assertIn("eligible US ordinary common issuers", universe["scope_label"])
        self.assertIn(AS_OF, universe["scope_label"])
        self.assertEqual(universe["meta"]["as_of"], AS_OF)
        self.assertIn("not proof of a complete universe", universe["meta"]["coverage_claim"])

    def test_live_candidate_coverage_is_reported_beside_every_other_capability(self):
        coverage = self.result["coverage"]["capabilities"]["universe"]
        self.assertEqual(coverage["acquired"], len(SCREEN_QUOTES))
        self.assertEqual(coverage["qualified"], len(SCREENED))
        self.assertEqual(coverage["in_scope"], len(SCREENED))
        self.assertEqual(coverage["enriched"], len(SCREENED))
        self.assertEqual(coverage["not_enriched"], [])
        self.assertFalse(coverage["time_budget_exhausted"])
        self.assertEqual(self.result["coverage"]["capabilities"]["prices"]["missing"], [WITHHELD])

    def test_the_fetching_stage_records_how_far_the_provider_pass_got(self):
        detail = self.workflow["stages"]["fetching"]["detail"]
        asked = len(SCREENED) + 1  # every screened candidate, plus the one holding
        self.assertEqual(detail["progress"], {"done": asked, "total": asked})

    def test_every_acquired_row_is_a_candidate_or_an_inspectable_exclusion(self):
        exclusions = {row["symbol"]: row["reasons"] for row in self.result["exclusions"]}
        self.assertEqual(sorted(exclusions), sorted(EXCLUDED_ROWS))
        self.assertIn("not_ordinary_common", exclusions[EXCLUDED_ROWS[0]])
        self.assertIn("separate_sector_model_required", exclusions[EXCLUDED_ROWS[1]])

    # Signals ----------------------------------------------------------------

    def test_screened_technology_issuers_carry_complete_composite_scores(self):
        signals = {row["security_id"]: row for row in self.result["signals"]}
        complete = [
            sid
            for sid in CANDIDATES
            if signals[sid]["data_status"] == "complete" and signals[sid]["score"] is not None
        ]
        self.assertEqual(len(complete), len(CANDIDATES))
        self.assertGreaterEqual(len(complete), self.config["signals"]["min_sector_size"])
        self.assertEqual(self.config["signals"]["min_sector_size"], 20)
        self.assertEqual(signals[WITHHELD]["data_status"], "incomplete")
        self.assertIn("stale_price", signals[WITHHELD]["reasons"])

    # Priorities -------------------------------------------------------------

    def test_an_unowned_candidate_is_prioritized_with_reasons_and_evidence(self):
        priorities = self.result["priorities"]
        self.assertEqual(priorities["method_version"], "priorities-1")
        candidates = {item["security_id"]: item for item in priorities["new_candidates"]}
        self.assertTrue(candidates)
        self.assertTrue(set(candidates) <= set(CANDIDATES))
        watched = candidates[WATCHED]
        rules = {reason["rule"] for reason in watched["reasons"]}
        self.assertIn("watchlist", rules)
        self.assertIn("top_decile_composite", rules)
        self.assertEqual(watched["kind"], "candidate")
        self.assertEqual(watched["evidence"]["sector"], "Technology")
        self.assertEqual(watched["evidence"]["data_status"], "complete")
        self.assertEqual(sorted(watched["scenario_range"]), ["Adverse", "Central", "Favorable"])
        self.assertIn(self.account, watched["account_eligibility"])

    # Comparison -------------------------------------------------------------

    def sleeve(self):
        candidates = {row["candidate"]: row for row in self.result["allocation"]["candidates"]}
        self.assertIn("simple_equal_issuer_sleeve", candidates)
        return candidates["simple_equal_issuer_sleeve"]

    def test_the_sleeve_proposes_buying_an_unowned_screened_candidate(self):
        sleeve = self.sleeve()
        self.assertNotEqual(sleeve["status"], "unavailable", sleeve.get("reason"))
        bought = {row["security_id"] for row in sleeve["proposals"] if row["trade_value"] > 0}
        self.assertTrue(bought & set(CANDIDATES), sleeve["proposals"])
        self.assertNotIn(WITHHELD, bought)

    def test_account_funding_rows_reconcile_for_the_proposed_sleeve(self):
        for account in self.sleeve()["accounts"]:
            with self.subTest(account=account["account_id"]):
                self.assertAlmostEqual(account["cash_identity_residual"], 0, places=6)

    def test_a_candidate_without_prices_is_excluded_by_name_and_blocks_nothing(self):
        allocation = self.result["allocation"]
        excluded = {row["security_id"]: row["reasons"] for row in allocation["excluded_candidates"]}
        self.assertIn(WITHHELD, excluded)
        self.assertTrue({"missing_price", "insufficient_history"} & set(excluded[WITHHELD]))
        admission = allocation["solver"]["candidate_admission"]
        self.assertEqual(admission["excluded"], len(excluded))
        self.assertGreater(admission["admitted"], 0)
        self.assertNotEqual(allocation["status"], "blocked")

    def test_the_shared_state_supplies_one_joint_scenario_set_for_every_asset(self):
        forecasts = self.result["forecast_inputs"]
        rows = forecasts["rows"] if isinstance(forecasts, dict) else forecasts
        covered = {row["security_id"] for row in rows}
        self.assertTrue({HELD, *CANDIDATES} <= covered)
        self.assertEqual(len({row["forecast_date"] for row in rows}), 1)
        self.assertEqual({row["scenario"] for row in rows}, {"Adverse", "Central", "Favorable"})


class CandidateEnrichmentBoundsTests(unittest.TestCase):
    """The fetching stage's own bounds: a limit, a time budget and a stop request."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        config = default_config(self.temp.name)
        config["data"].update(
            mode="live",
            price_provider="yahoo",
            refresh_network=True,
            sec_enabled=False,
            lookback_years=3,
            provider_min_interval_seconds=0,
            provider_timeout_seconds=5,
        )
        self.config = validate_config(config)

    def bundle(self, symbols):
        return {
            "as_of": AS_OF,
            "issues": [],
            "sources": [],
            "securities": candidate_securities(symbols),
        }

    def enrich(self, bundle, **kwargs):
        with ExitStack() as stack:
            stack.enter_context(patch("yfinance.Ticker", SyntheticTicker))
            stack.enter_context(patch("portfolio_lab.providers._now", return_value=SUNDAY_RECEIPT))
            stack.enter_context(
                patch("portfolio_research.issuers.sec_issuer_lookup", return_value=None)
            )
            enrich_market(bundle, self.config, AS_OF, **kwargs)

    def test_the_enrichment_limit_takes_the_largest_candidates_first(self):
        self.config = deepcopy(self.config)
        self.config["data"]["candidate_enrichment_limit"] = 3
        bundle = self.bundle(CANDIDATES[:6])
        self.enrich(bundle)
        universe = bundle["coverage"]["capabilities"]["universe"]
        self.assertEqual(universe["enriched"], 3)
        self.assertEqual(sorted(bundle["research_inputs"]), sorted(CANDIDATES[:3]))
        self.assertEqual(universe["not_enriched"], sorted(CANDIDATES[3:6]))

    def test_a_stop_request_ends_the_fetch_and_says_what_was_not_reached(self):
        bundle = self.bundle(CANDIDATES[:6])
        calls = []

        def should_stop():
            calls.append(len(calls))
            return len(calls) > 2

        self.enrich(bundle, should_stop=should_stop)
        universe = bundle["coverage"]["capabilities"]["universe"]
        self.assertLess(universe["enriched"], 6)
        self.assertTrue(universe["not_enriched"])
        self.assertTrue(universe["stopped"])

    def test_an_exhausted_time_budget_is_reported_rather_than_hidden(self):
        self.config = deepcopy(self.config)
        self.config["data"]["provider_time_budget_seconds"] = 1
        bundle = self.bundle(CANDIDATES[:4])
        clock = iter([0.0, 0.0, 0.5, 5.0, 5.0, 5.0, 5.0, 5.0])
        with patch("portfolio_research.enrichment.monotonic", side_effect=lambda: next(clock)):
            self.enrich(bundle)
        universe = bundle["coverage"]["capabilities"]["universe"]
        self.assertTrue(universe["time_budget_exhausted"])
        self.assertTrue(universe["not_enriched"])

    def test_progress_is_reported_for_every_security_the_stage_asks_about(self):
        bundle = self.bundle(CANDIDATES[:3])
        seen = []
        self.enrich(bundle, progress=seen.append)
        self.assertTrue(seen)
        self.assertEqual(seen[-1], {"done": 3, "total": 3})
        self.assertEqual([step["total"] for step in seen], [3] * len(seen))

    def test_no_screen_is_asked_for_until_the_owner_admits_candidates(self):
        """Discovery is a decision, not a default: an unasked screen is never called."""
        bundle = self.bundle([])
        bundle["securities"] = pd.DataFrame(
            [
                {
                    "security_id": HELD,
                    "ticker": HELD,
                    "instrument_type": "equity",
                    "currency": "USD",
                    "resolution_status": "resolved",
                }
            ]
        )
        with patch("yfinance.screen", side_effect=AssertionError("the screen was asked")):
            acquire_candidates(bundle, self.config, AS_OF, refresh=True)
        self.assertNotIn("universe", bundle)
        self.assertEqual(bundle["issues"], [])

        asked = []

        def screen(query, offset=None, size=None, **kwargs):
            asked.append(offset)
            return {"start": 0, "count": 0, "total": 0, "quotes": []}

        self.config = deepcopy(self.config)
        self.config["signals"]["watchlist"] = [HELD]
        with patch("yfinance.screen", side_effect=screen):
            acquire_candidates(bundle, self.config, AS_OF, refresh=True)
        self.assertEqual(asked, [0])
        self.assertEqual(bundle["universe"]["counts"]["acquired"], 0)
        self.assertEqual(bundle["exclusions"], [])

    def test_briefs_and_events_are_asked_for_only_where_they_are_read(self):
        self.config = deepcopy(self.config)
        self.config["signals"]["candidate_brief_limit"] = 2
        bundle = self.bundle(CANDIDATES[:4])
        self.enrich(bundle)
        coverage = bundle["coverage"]["by_security"]
        asked = [sid for sid in CANDIDATES[:4] if coverage[sid]["events"] != "not_applicable"]
        self.assertEqual(asked, list(CANDIDATES[:2]))
