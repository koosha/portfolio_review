"""Regressions for the defects found in the increment-5 review.

Each case here names one way the discovery-and-comparison flow asserted something it
had not established — an equity market state handed to a bond fund, a scope label that
survived an interrupted fetch, a screened candidate withholding the whole comparison —
and pins the corrected behaviour. No network: every provider is a local fake.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from portfolio_research.scenarios import (
    DEFAULT_SHARED_STATE,
    generate_joint_forecasts,
    validate_shared_state,
)

AS_OF = "2026-09-18"
LABELS = {"Adverse", "Central", "Favorable"}


def securities(*rows) -> pd.DataFrame:
    return pd.DataFrame(list(rows))


def security(sid, **extra) -> dict:
    return {
        "security_id": sid,
        "ticker": sid,
        "name": f"{sid} Inc",
        "sector": "Technology",
        "currency": "USD",
        "instrument_type": "equity",
        **extra,
    }


def generate(frame, state=None, *, horizon_months=12, **kwargs):
    return generate_joint_forecasts(
        frame, state or DEFAULT_SHARED_STATE, as_of=AS_OF, horizon_months=horizon_months, **kwargs
    )


def value(frame, sid, label) -> float:
    rows = frame[(frame.security_id == sid) & (frame.scenario == label)]
    return float(rows.return_value.iloc[0])


def codes(frame) -> set:
    return {issue["code"] for issue in frame.attrs["issues"]}


class InstrumentScopeTests(unittest.TestCase):
    """A US equity market state describes equities, and says so about everything else."""

    def test_bond_and_commodity_funds_get_no_equity_state(self):
        frame = generate(
            securities(
                security("AAA"),
                security("AGG", sector=None, instrument_type="fund", name="Core Bond ETF"),
                security("GLD", sector=None, instrument_type="etf", name="Gold Trust"),
            )
        )
        self.assertEqual(set(frame.security_id), {"AAA"})
        refused = [i for i in frame.attrs["issues"] if i["code"] == "NO_STATE_FOR_INSTRUMENT"]
        self.assertEqual({i["security_id"] for i in refused}, {"AGG", "GLD"})
        self.assertNotIn("UNKNOWN_SECTOR_MULTIPLIER", codes(frame))

    def test_a_named_equity_benchmark_is_still_covered(self):
        frame = generate(
            securities(security("AAA"), security("SIMETF", sector=None, instrument_type="etf")),
            benchmark_ids=("SIMETF",),
        )
        self.assertEqual(set(frame.security_id), {"AAA", "SIMETF"})
        self.assertNotIn("NO_STATE_FOR_INSTRUMENT", codes(frame))

    def test_a_security_without_an_instrument_type_is_never_assumed_to_be_equity(self):
        frame = generate(securities(security("AAA", instrument_type=None)))
        self.assertTrue(frame.empty)
        self.assertIn("NO_STATE_FOR_INSTRUMENT", codes(frame))

    def test_cash_is_still_skipped_without_an_instrument_issue(self):
        frame = generate(
            securities(security("AAA"), security("CASH", sector=None, instrument_type="cash"))
        )
        self.assertEqual(set(frame.security_id), {"AAA"})
        self.assertNotIn("NO_STATE_FOR_INSTRUMENT", codes(frame))


class StateHorizonTests(unittest.TestCase):
    """`horizon_months` declares what the stated market returns are returns over."""

    def test_a_six_month_state_states_six_month_returns(self):
        state = {**DEFAULT_SHARED_STATE, "horizon_months": 6, "sector_multipliers": {"Energy": 1.0}}
        frame = generate(securities(security("AAA", sector="Energy")), state, horizon_months=6)
        for label, market in state["market_returns"].items():
            self.assertAlmostEqual(value(frame, "AAA", label), market, places=12)

    def test_a_six_month_state_compounds_onto_a_twelve_month_horizon(self):
        state = {**DEFAULT_SHARED_STATE, "horizon_months": 6, "sector_multipliers": {"Energy": 1.0}}
        frame = generate(securities(security("AAA", sector="Energy")), state, horizon_months=12)
        for label, market in state["market_returns"].items():
            self.assertAlmostEqual(value(frame, "AAA", label), (1 + market) ** 2 - 1, places=12)

    def test_a_rescaled_state_says_so(self):
        state = {**DEFAULT_SHARED_STATE, "horizon_months": 6}
        frame = generate(securities(security("AAA")), state, horizon_months=12)
        self.assertIn("SHARED_STATE_HORIZON_RESCALED", codes(frame))

    def test_a_state_stated_over_the_requested_horizon_is_never_rescaled(self):
        frame = generate(securities(security("AAA")))
        self.assertNotIn("SHARED_STATE_HORIZON_RESCALED", codes(frame))


class EconomicFloorTests(unittest.TestCase):
    """No validated state may state a loss worse than the whole investment."""

    def test_a_multiplier_that_states_worse_than_total_loss_is_refused(self):
        state = {
            **DEFAULT_SHARED_STATE,
            "sector_multipliers": {**DEFAULT_SHARED_STATE["sector_multipliers"], "Technology": 6.0},
        }
        with self.assertRaises(ValueError) as caught:
            validate_shared_state(state)
        self.assertIn("Technology", str(caught.exception))

    def test_the_refusal_reaches_generation_before_any_number_is_produced(self):
        state = {
            **DEFAULT_SHARED_STATE,
            "sector_multipliers": {**DEFAULT_SHARED_STATE["sector_multipliers"], "Technology": 6.0},
        }
        for horizon in (6, 12, 18):
            with self.assertRaises(ValueError):
                generate(securities(security("AAA")), state, horizon_months=horizon)

    def test_no_generated_return_is_ever_complex(self):
        state = {
            **DEFAULT_SHARED_STATE,
            "market_returns": {"Adverse": -1.0, "Central": 0.06, "Favorable": 0.18},
            "sector_multipliers": {"Technology": 1.0},
        }
        for horizon in (6, 12, 18):
            frame = generate(securities(security("AAA")), state, horizon_months=horizon)
            self.assertEqual(frame.return_value.dtype.kind, "f", horizon)
            self.assertAlmostEqual(value(frame, "AAA", "Adverse"), -1.0, places=12)


class GeneratedReceiptTests(unittest.TestCase):
    """A state expansion is knowable on the date it is stated for, and says when."""

    def test_generated_rows_carry_a_receipt_at_the_forecast_date(self):
        frame = generate(securities(security("AAA")))
        self.assertIn("received_at", frame.columns)
        self.assertIn("available_at", frame.columns)
        self.assertEqual(set(frame.received_at), {AS_OF})
        self.assertEqual(set(frame.available_at), {AS_OF})

    def test_strict_receipt_mode_keeps_the_generated_set(self):
        from portfolio_lab.providers import _filter_frames

        bundle = {"forecasts": generate(securities(security("AAA"))), "issues": []}
        _filter_frames(bundle, AS_OF, True)
        self.assertEqual(len(bundle["forecasts"]), 3)
        self.assertEqual(
            [i for i in bundle["issues"] if i["code"] == "RECEIPT_TIMESTAMP_REQUIRED"], []
        )


def forecast_rows(sid, *, date, horizon=12, probability=None) -> list[dict]:
    return [
        {
            "security_id": sid,
            "scenario": label,
            "horizon_months": horizon,
            "return_value": 0.05,
            "probability": probability,
            "basis": "subjective",
            "source": "owner CSV",
            "forecast_date": date,
            "received_at": date,
        }
        for label in sorted(LABELS)
    ]


class SharedStateVintageTests(unittest.TestCase):
    """The shared state fills the vintage this run needs, not merely empty rows."""

    def bundle(self, forecasts) -> dict:
        return {
            "securities": securities(
                security("HOLD1", owned=True),
                security("BENCH", sector=None, instrument_type="etf"),
                security("CAND1", candidate=True),
            ),
            "forecasts": pd.DataFrame(forecasts),
            "issues": [],
        }

    def config(self) -> dict:
        return {
            "allocation": {"horizon_months": 12, "shared_state": DEFAULT_SHARED_STATE},
            "mandate": {"base_currency": "USD", "benchmark_id": "BENCH"},
            "data": {"max_forecast_age_days": 45},
        }

    def applied(self, forecasts) -> dict:
        from portfolio_research.enrichment import _apply_shared_state

        bundle = self.bundle(forecasts)
        _apply_shared_state(bundle, self.config(), AS_OF)
        return bundle

    def dated(self, bundle, sid, horizon=12) -> set:
        frame = bundle["forecasts"]
        rows = frame[(frame.security_id == sid) & (frame.horizon_months == horizon)]
        return set(rows.forecast_date)

    def test_an_older_import_is_superseded_so_the_joint_set_shares_one_date(self):
        imported = forecast_rows("HOLD1", date="2026-09-04") + forecast_rows(
            "BENCH", date="2026-09-04"
        )
        bundle = self.applied(imported)
        for sid in ("HOLD1", "BENCH", "CAND1"):
            self.assertIn(AS_OF, self.dated(bundle, sid), sid)
        superseded = [
            issue for issue in bundle["issues"] if issue["code"] == "IMPORTED_FORECAST_SUPERSEDED"
        ]
        self.assertEqual({issue["security_id"] for issue in superseded}, {"HOLD1", "BENCH"})

    def test_an_import_of_the_wrong_horizon_never_counts_as_coverage(self):
        bundle = self.applied(forecast_rows("HOLD1", date=AS_OF, horizon=6))
        self.assertIn(AS_OF, self.dated(bundle, "HOLD1", horizon=12))
        self.assertIn(AS_OF, self.dated(bundle, "CAND1", horizon=12))

    def test_an_import_already_dated_for_this_run_still_wins(self):
        bundle = self.applied(forecast_rows("HOLD1", date=AS_OF))
        frame = bundle["forecasts"]
        rows = frame[frame.security_id == "HOLD1"]
        self.assertEqual(set(rows.source), {"owner CSV"})
        self.assertEqual(len(rows), 3)
        self.assertEqual(
            [i for i in bundle["issues"] if i["code"] == "IMPORTED_FORECAST_SUPERSEDED"], []
        )

    def test_the_mandate_benchmark_is_covered_even_though_it_is_a_fund(self):
        bundle = self.applied([])
        self.assertIn(AS_OF, self.dated(bundle, "BENCH"))
        self.assertNotIn(
            "BENCH",
            {
                issue.get("security_id")
                for issue in bundle["issues"]
                if issue["code"] == "NO_STATE_FOR_INSTRUMENT"
            },
        )


class RepeatingScreen:
    """A degraded screen: a non-empty page, the same symbols every time, no ``total``."""

    def __init__(self, count=5):
        self.count = count
        self.calls = 0

    def __call__(self, query, offset=None, size=None, **kwargs):
        self.calls += 1
        return {
            "start": 0,
            "count": self.count,
            "quotes": [
                {
                    "symbol": f"R{index:03d}",
                    "quoteType": "EQUITY",
                    "currency": "USD",
                    "exchange": "NMS",
                    "longName": f"Repeating Issuer {index}",
                    "marketCap": 500_000_000_000 - index,
                }
                for index in range(self.count)
            ],
        }


class UniverseCaseMixin:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        from portfolio_lab.config import DEFAULTS

        self.config = {
            "research": {"path": str(self.root / "research.sqlite")},
            "data": {
                "mode": "live",
                "provider_min_interval_seconds": 0,
                "provider_timeout_seconds": 5,
                "universe_acquire_size": 1500,
                "universe_size": 1000,
            },
            "signals": dict(DEFAULTS["signals"]),
        }


class AcquisitionBoundTests(UniverseCaseMixin, unittest.TestCase):
    """Paging is bounded by the review's own arithmetic, never by provider goodwill."""

    def acquire(self, screen, **kwargs):
        from portfolio_research.universe import acquire_universe

        issues = []
        with patch("yfinance.screen", screen):
            result = acquire_universe(
                self.config, as_of=AS_OF, refresh=True, issues=issues, **kwargs
            )
        return result, issues

    def test_a_screen_repeating_one_page_without_a_total_stops(self):
        screen = RepeatingScreen()
        result, issues = self.acquire(screen)
        self.assertLessEqual(screen.calls, 3)
        self.assertEqual(len(result["rows"]), 5)
        self.assertIn("UNIVERSE_ACQUISITION_INCOMPLETE", {i["code"] for i in issues})

    def test_paging_never_exceeds_the_pages_the_requested_size_needs(self):
        from portfolio_research.universe import PAGE_SIZE

        self.config["data"]["universe_acquire_size"] = 500
        screen = RepeatingScreen(count=PAGE_SIZE)

        def growing(query, offset=None, size=None, **kwargs):
            screen.calls += 1
            start = screen.calls * 1000
            return {
                "start": 0,
                "count": PAGE_SIZE,
                "quotes": [
                    {
                        "symbol": f"G{start + index:06d}",
                        "quoteType": "EQUITY",
                        "currency": "USD",
                        "exchange": "NMS",
                        "longName": f"Growing Issuer {start + index}",
                        "marketCap": 1_000_000_000_000 - start - index,
                    }
                    for index in range(PAGE_SIZE)
                ],
            }

        result, _ = self.acquire(growing)
        self.assertLessEqual(screen.calls, -(-500 // PAGE_SIZE) + 1)
        self.assertEqual(len(result["rows"]), 500)

    def test_a_stop_request_reaches_the_acquisition(self):
        screen = RepeatingScreen()
        result, issues = self.acquire(screen, should_stop=lambda: True)
        self.assertEqual(screen.calls, 0)
        self.assertEqual(result["rows"], [])
        self.assertTrue(result["meta"]["stopped"])
        self.assertIn("UNIVERSE_ACQUISITION_INCOMPLETE", {i["code"] for i in issues})

    def test_an_expired_budget_reaches_the_acquisition(self):
        screen = RepeatingScreen()
        result, issues = self.acquire(screen, deadline=-1.0)
        self.assertEqual(screen.calls, 0)
        self.assertTrue(result["meta"]["time_budget_exhausted"])
        self.assertIn("UNIVERSE_ACQUISITION_INCOMPLETE", {i["code"] for i in issues})


class WatchlistReportingTests(UniverseCaseMixin, unittest.TestCase):
    """A symbol the owner named by hand is never dropped without a word."""

    def rows(self):
        return [
            {
                "symbol": f"S{index}",
                "name": f"Synthetic Issuer {index}",
                "exchange": "NMS",
                "currency": "USD",
                "quote_type": "EQUITY",
                "market_cap": 900_000_000_000 - index,
                "received_at": "2026-09-18T20:00:00Z",
                "source_id": "src",
            }
            for index in range(3)
        ]

    def qualify(self, *, watchlist, owned=()):
        from portfolio_research.universe import qualify_universe

        listings = {
            f"S{index}": {"country": "United States", "sector": "Technology"} for index in range(3)
        }
        return qualify_universe(
            self.rows(),
            owned_securities=owned,
            listings=listings,
            issuer_lookup=None,
            config=self.config,
            watchlist=watchlist,
            as_of=AS_OF,
        )

    def test_a_watchlisted_symbol_the_screen_never_returned_is_reported(self):
        result = self.qualify(watchlist=["ZZZZ"])
        excluded = {row["symbol"]: row["reasons"] for row in result["exclusions"]}
        self.assertEqual(excluded.get("ZZZZ"), ["watchlist_not_acquired"])
        self.assertEqual(result["counts"]["by_reason"]["watchlist_not_acquired"], 1)
        self.assertEqual(result["counts"]["watchlist_not_acquired"], 1)

    def test_a_watchlisted_symbol_the_screen_did_return_is_not_reported_missing(self):
        result = self.qualify(watchlist=["S1"])
        self.assertEqual(
            [row for row in result["exclusions"] if "watchlist_not_acquired" in row["reasons"]], []
        )

    def test_an_owned_watchlist_symbol_is_researched_as_a_holding_not_reported_missing(self):
        result = self.qualify(watchlist=["HELD"], owned=["HELD"])
        self.assertEqual(
            [row for row in result["exclusions"] if "watchlist_not_acquired" in row["reasons"]], []
        )


def screen_payload(count):
    quotes = [
        {
            "symbol": f"C{index:03d}",
            "quoteType": "EQUITY",
            "currency": "USD",
            "exchange": "NMS",
            "longName": f"Candidate Issuer {index}",
            "marketCap": 900_000_000_000 - index * 1_000_000,
            "sharesOutstanding": 1_000_000_000,
        }
        for index in range(count)
    ]
    return {"start": 0, "count": count, "total": count, "quotes": quotes}


class DiscoveryBoundTests(unittest.TestCase):
    """Identity resolution is part of the fetch, and obeys the fetch's own bounds."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        from portfolio_lab.config import DEFAULTS

        self.config = {
            "research": {"path": str(Path(self.temp.name) / "research.sqlite")},
            "data": {
                "mode": "live",
                "provider_min_interval_seconds": 0,
                "provider_timeout_seconds": 5,
                "universe_acquire_size": 40,
                "universe_size": 40,
                "candidate_enrichment_limit": 1000,
                "provider_time_budget_seconds": 1800,
                "sec_enabled": False,
            },
            "signals": dict(DEFAULTS["signals"]),
            "mandate": {"account_candidate_policy": {"a1": "eligible_universe"}},
            "allocation": {"horizon_months": 12},
        }

    def bundle(self):
        return {
            "as_of": AS_OF,
            "issues": [],
            "sources": [],
            "securities": securities(security("HELD", owned=True)),
        }

    def acquire(self, *, rows=5, should_stop=None, profile=None):
        from portfolio_research.enrichment import acquire_candidates, fetch_controls

        asked = []

        def security_profile(config, security_row, **kwargs):
            asked.append(str(security_row.get("security_id")))
            return (
                profile(security_row)
                if profile
                else {
                    "domicile": "United States",
                    "sector": "Technology",
                    "instrument_type": "equity",
                    "name": str(security_row.get("security_id")),
                }
            )

        bundle = self.bundle()
        with (
            patch("yfinance.screen", return_value=screen_payload(rows)),
            patch("portfolio_research.market_data.security_profile", security_profile),
        ):
            with fetch_controls(should_stop=should_stop):
                acquire_candidates(bundle, self.config, AS_OF, refresh=True)
        return bundle, asked

    def reasons(self, bundle):
        return {row["symbol"]: row["reasons"] for row in bundle["exclusions"]}

    def test_the_enrichment_limit_bounds_the_identity_stage_too(self):
        self.config["data"]["candidate_enrichment_limit"] = 0
        bundle, asked = self.acquire(rows=5)
        self.assertEqual(asked, [])
        for symbol, reasons in self.reasons(bundle).items():
            self.assertEqual(reasons, ["identity_limit_reached"], symbol)
        self.assertNotIn("us_domicile_unverified", bundle["universe"]["counts"]["by_reason"])

    def test_a_stopped_fetch_never_calls_the_tail_a_foreign_issuer(self):
        bundle, asked = self.acquire(rows=5, should_stop=lambda: True)
        self.assertEqual(asked, [])
        for symbol, reasons in self.reasons(bundle).items():
            self.assertEqual(reasons, ["identity_not_attempted"], symbol)
        self.assertIn("UNIVERSE_DISCOVERY_INTERRUPTED", {i["code"] for i in bundle["issues"]})

    def test_a_stopped_fetch_says_so_in_the_coverage_it_publishes(self):
        from portfolio_research.enrichment import _record_universe

        bundle, _ = self.acquire(rows=5, should_stop=lambda: True)
        _record_universe(
            bundle, in_scope=0, enriched=[], not_enriched=set(), exhausted=False, stopped=False
        )
        coverage = bundle["universe"]["coverage"]
        self.assertTrue(coverage["stopped"])

    def test_a_resolved_fetch_still_qualifies_every_acquired_row(self):
        bundle, asked = self.acquire(rows=5)
        self.assertEqual(len(asked), 5)
        self.assertEqual(bundle["universe"]["counts"]["in_scope"], 5)

    def test_one_fetch_starts_one_provider_time_budget(self):
        from portfolio_research.enrichment import _budget, fetch_controls

        with fetch_controls():
            first, second = _budget(self.config), _budget(self.config)
        self.assertEqual(first, second)
        outside_first, outside_second = _budget(self.config), _budget(self.config)
        self.assertNotEqual(outside_first, outside_second)


class ScopeLabelTests(unittest.TestCase):
    """The one human sentence about scope never overstates what was researched."""

    def record(self, *, enriched, in_scope, **flags):
        from portfolio_research.enrichment import _record_universe

        bundle = {
            "universe": {
                "scope_label": f"Largest {in_scope} eligible US ordinary common issuers by "
                f"Yahoo intraday market cap on {AS_OF}",
                "counts": {"acquired": in_scope, "qualified": in_scope, "in_scope": in_scope},
            }
        }
        _record_universe(
            bundle,
            in_scope=in_scope,
            enriched=[f"C{index}" for index in range(enriched)],
            not_enriched={f"C{index}" for index in range(enriched, in_scope)},
            **{"exhausted": False, "stopped": False, **flags},
        )
        return bundle["universe"]

    def test_the_label_states_how_much_of_the_scope_was_enriched(self):
        universe = self.record(enriched=12, in_scope=1000)
        self.assertTrue(universe["scope_label"].endswith("; 12 of 1000 enriched"))

    def test_an_interrupted_discovery_says_so_instead_of_claiming_a_screen(self):
        universe = self.record(enriched=0, in_scope=0, stopped=True)
        self.assertIn("discovery interrupted", universe["scope_label"])

    def test_an_exhausted_budget_says_so(self):
        universe = self.record(enriched=3, in_scope=1000, exhausted=True)
        self.assertIn("discovery interrupted", universe["scope_label"])

    def test_the_readiness_scope_line_quotes_the_label_the_review_used(self):
        from portfolio_research.readiness import _candidates

        result = {
            "signals": [{"security_id": "C1", "data_status": "complete", "eligible": True}],
            "metadata": {"scope": "configured_universe"},
            "universe": {"scope_label": "Largest 1000 issuers on 2026-09-18; 12 of 1000 enriched"},
        }
        row = _candidates(result, [])
        self.assertIn("12 of 1000 enriched", row["scope"])
        self.assertNotIn("configured_universe", row["scope"])


class CandidateForecastAdmissionTests(unittest.TestCase):
    """A screened candidate's own forecast defect excludes that candidate, not the run."""

    def setUp(self):
        import numpy as np

        from tests.reference.test_allocation import fixture

        self.bundle, self.config = fixture()
        self.scores = pd.DataFrame({"security_id": ["A", "B", "C"], "composite": [0.3, 0.7, 0.95]})
        covariance = patch(
            "portfolio_lab.metrics.portfolio_covariance",
            side_effect=lambda b, c, ids: {
                "ids": ids,
                "matrix": np.eye(len(ids)) * 0.04,
                "complete": True,
                "observations": 156,
                "issues": [],
            },
        )
        stress = patch(
            "portfolio_lab.metrics.stress_returns",
            side_effect=lambda b, c, ids: {"recession": {sid: -0.3 for sid in ids}},
        )
        covariance.start()
        stress.start()
        self.addCleanup(covariance.stop)
        self.addCleanup(stress.stop)
        self.config["mandate"]["account_candidate_policy"] = {"r1": "eligible_universe"}
        self.config["mandate"]["account_permissions"] = {}

    def add_candidate(self, sid, *, probability, return_value=0.05):
        self.bundle["securities"] = pd.concat(
            [
                self.bundle["securities"],
                pd.DataFrame(
                    [
                        dict(
                            security_id=sid,
                            ticker=sid,
                            issuer_id="issuer_" + sid,
                            name=sid,
                            sector="Technology",
                            instrument_type="equity",
                            currency="USD",
                            eligible=True,
                        )
                    ]
                ),
            ],
            ignore_index=True,
        )
        self.bundle["prices"] = pd.concat(
            [
                self.bundle["prices"],
                pd.DataFrame(
                    [
                        dict(
                            security_id=sid,
                            date="2026-08-31",
                            close=50.0,
                            available_at="2026-08-31T20:00:00Z",
                            received_at="2026-08-31T20:00:00Z",
                        )
                    ]
                ),
            ],
            ignore_index=True,
        )
        reference = self.bundle["forecasts"]
        labels = reference[reference.security_id == "A"]
        self.bundle["forecasts"] = pd.concat(
            [
                reference,
                pd.DataFrame(
                    [
                        dict(
                            security_id=sid,
                            scenario=row.scenario,
                            horizon_months=row.horizon_months,
                            return_value=return_value,
                            probability=float("nan") if probability is None else probability,
                            basis="subjective",
                            source="shared state",
                            forecast_date=row.forecast_date,
                        )
                        for row in labels.itertuples()
                    ]
                ),
            ],
            ignore_index=True,
        )

    def run_proposals(self):
        from portfolio_lab.allocation import build_proposals

        return build_proposals(self.bundle, self.config, self.scores)

    def excluded(self, result):
        return {row["security_id"]: row["reasons"] for row in result["excluded_candidates"]}

    def test_a_blank_probability_beside_stated_ones_excludes_that_candidate(self):
        self.add_candidate("D", probability=None)
        result = self.run_proposals()
        self.assertNotEqual(result["status"], "blocked", result["issues"])
        self.assertNotIn("inconsistent_joint_probability", {i["code"] for i in result["issues"]})
        self.assertIn("missing_forecast", self.excluded(result)["D"])

    def test_a_disagreeing_probability_excludes_that_candidate(self):
        self.add_candidate("D", probability=0.9)
        result = self.run_proposals()
        self.assertNotEqual(result["status"], "blocked", result["issues"])
        self.assertIn("missing_forecast", self.excluded(result)["D"])

    def test_an_out_of_range_return_excludes_that_candidate(self):
        self.add_candidate("D", probability=None, return_value=-1.5)
        result = self.run_proposals()
        self.assertNotEqual(result["status"], "blocked", result["issues"])
        self.assertNotIn("invalid_forecast_return", {i["code"] for i in result["issues"]})
        self.assertIn("missing_forecast", self.excluded(result)["D"])

    def test_a_non_finite_return_excludes_that_candidate(self):
        import numpy as np

        self.add_candidate("D", probability=None, return_value=np.nan)
        result = self.run_proposals()
        self.assertNotEqual(result["status"], "blocked", result["issues"])
        self.assertIn("missing_forecast", self.excluded(result)["D"])

    def test_a_matching_candidate_is_still_admitted(self):
        reference = self.bundle["forecasts"]
        probabilities = {
            row.scenario: row.probability
            for row in reference[reference.security_id == "A"].itertuples()
        }
        self.bundle["securities"] = self.bundle["securities"]
        self.add_candidate("D", probability=None)
        frame = self.bundle["forecasts"]
        mask = frame.security_id == "D"
        frame.loc[mask, "probability"] = frame.loc[mask, "scenario"].map(probabilities)
        result = self.run_proposals()
        self.assertEqual(self.excluded(result), {})
        self.assertEqual(result["solver"]["candidate_admission"]["excluded"], 0)


class CancellationLatchTests(unittest.TestCase):
    """A cancellation once seen stays seen; a store that cannot answer says nothing."""

    def probe(self, *, answers):
        from portfolio_research.workflow import PROBE_SECONDS, ReviewWorkflow

        run = ReviewWorkflow.__new__(ReviewWorkflow)
        run.probed = 0.0
        run.stop_seen = False
        run.workflow_id = "w1"
        run._stopping = lambda: False
        calls = iter(answers)

        class Store:
            def workflow(self, workflow_id):
                answer = next(calls)
                if isinstance(answer, Exception):
                    raise answer
                return {"cancel_requested": answer}

        run.store = Store()
        clock = iter([PROBE_SECONDS * step for step in range(1, len(answers) + 2)])
        with patch("portfolio_research.workflow.monotonic", side_effect=lambda: next(clock)):
            return [run._fetch_stopping() for _ in answers]

    def test_a_failing_probe_after_a_cancellation_never_resumes_the_fetch(self):
        self.assertEqual(self.probe(answers=[True, KeyError("gone")]), [True, True])

    def test_a_failing_probe_before_any_cancellation_is_not_a_cancellation(self):
        self.assertEqual(self.probe(answers=[KeyError("gone"), False]), [False, False])

    def test_a_cancellation_is_still_reported_when_the_store_answers(self):
        self.assertEqual(self.probe(answers=[False, True]), [False, True])


class PriorityDenominatorTests(unittest.TestCase):
    """A percentile means nothing until the population it ranks against is named."""

    def report(self, extra_issuers):
        from tests.test_priorities import OWNED, _bundle, _config, _result, _signal

        signals = [_signal(OWNED, score=0.1), _signal("NEW", score=0.9)]
        signals += [
            _signal(f"X{index}", score=0.5 + index / 1000.0) for index in range(extra_issuers)
        ]
        return review_priorities_for(_result(signals=signals), _bundle(), _config()), OWNED

    def holding(self, extra_issuers):
        report, owned = self.report(extra_issuers)
        return next(item for item in report["holdings_to_review"] if item["security_id"] == owned)

    def test_the_reason_names_the_population_the_percentile_ranks_against(self):
        reason = next(r for r in self.holding(18)["reasons"] if r["rule"] == "below_retention_rank")
        self.assertIn("20 scored issuers", reason["detail"])

    def test_the_evidence_carries_the_denominator(self):
        self.assertEqual(self.holding(18)["evidence"]["scored_issuers"], 20)
        self.assertEqual(self.holding(3)["evidence"]["scored_issuers"], 5)

    def test_the_rank_basis_mentions_the_denominator(self):
        from portfolio_research.priorities import RANK_BASIS

        self.assertIn("scored", RANK_BASIS)
        self.assertIn("percentile", RANK_BASIS)


def review_priorities_for(result, bundle, config):
    from portfolio_research.priorities import review_priorities

    return review_priorities(result, bundle, config)


class BriefLimitTests(unittest.TestCase):
    """`candidate_brief_limit` bounds the brief that is built, not only the events fetch."""

    def records(self, *ids):
        return {
            sid: {
                "security": {"security_id": sid, "ticker": sid, "currency": "USD"},
                "profile": {"name": f"{sid} Inc"},
                "prices": {"prices": [], "currency": "USD"},
                "statements": {},
                "estimates": None,
                "events": {"events": []},
                "coverage": {"prices": "ok", "statements": "missing"},
                "sources": [],
            }
            for sid in ids
        }

    def build(self, brief_ids):
        from portfolio_research.research_build import build_research

        asked = []

        def fake_brief(security, **kwargs):
            asked.append(str(security["security_id"]))
            return {
                "security_id": security["security_id"],
                "name": security.get("name"),
                "facts": [],
                "invalidation_conditions": [],
            }

        bundle = {"issues": [], "timeline": {"generated_at": f"{AS_OF}T20:00:00Z"}}
        config = {"allocation": {"horizon_months": 12}}
        with patch("portfolio_research.briefs.build_brief", fake_brief):
            research = build_research(
                self.records("A", "B"), bundle, config, AS_OF, brief_ids=brief_ids
            )
        return research, asked

    def test_only_the_named_securities_get_a_brief(self):
        research, asked = self.build({"A"})
        self.assertEqual(asked, ["A"])
        self.assertIsNotNone(research["A"]["brief"])
        self.assertIsNone(research["B"]["brief"])
        self.assertIsNone(research["B"]["proposals"])

    def test_every_security_still_reports_its_coverage(self):
        research, _ = self.build({"A"})
        for sid in ("A", "B"):
            self.assertEqual(research[sid]["coverage"], {"prices": "ok", "statements": "missing"})

    def test_no_named_set_means_every_enriched_security(self):
        research, asked = self.build(None)
        self.assertEqual(sorted(asked), ["A", "B"])
        self.assertIsNotNone(research["B"]["brief"])

    def test_the_fetch_plan_names_the_holdings_and_the_brief_limit(self):
        from portfolio_lab.config import DEFAULTS
        from portfolio_research.enrichment import _plan

        frame = securities(
            security("HELD", owned=True),
            security("C1", candidate=True, market_cap=900.0),
            security("C2", candidate=True, market_cap=800.0),
            security("C3", candidate=True, market_cap=700.0),
        )
        config = {
            "signals": {**DEFAULTS["signals"], "candidate_brief_limit": 2, "watchlist": []},
            "data": {"candidate_enrichment_limit": 1000},
        }
        plan = _plan(frame, config)
        self.assertEqual(plan["with_events"], {"C1", "C2"})
        self.assertEqual(plan["with_briefs"], {"HELD", "C1", "C2"})


class ScanCostTests(unittest.TestCase):
    """Per-security work must not re-scan the whole panel once per security.

    At the increment's default scope the comparison carries a thousand screened
    candidates with three years of daily bars. A filter written per security turns that
    into a quadratic pass, so these cases count the full-panel boolean masks each call
    makes and require the count to stay flat as the number of securities grows.
    """

    MASK_FLOOR = 200

    def panel(self, count, sessions=60):
        days = pd.bdate_range(end="2026-09-18", periods=sessions).strftime("%Y-%m-%d")
        prices = pd.DataFrame(
            [
                {
                    "security_id": f"S{index:04d}",
                    "date": day,
                    "close": 100.0 + step,
                    "adjusted_close": 100.0 + step,
                    "volume": 5_000_000,
                    "currency": "USD",
                    "available_at": f"{day}T20:00:00Z",
                    "received_at": f"{day}T20:00:00Z",
                }
                for index in range(count)
                for step, day in enumerate(days)
            ]
        )
        securities_frame = pd.DataFrame(
            [
                {
                    "security_id": f"S{index:04d}",
                    "ticker": f"S{index:04d}",
                    "issuer_id": f"issuer_{index:04d}",
                    "name": f"Issuer {index}",
                    "sector": "Technology",
                    "instrument_type": "equity",
                    "currency": "USD",
                    "domicile": "US",
                    "equity_type": "ordinary_common",
                    "eligible": True,
                    "market_cap": 1e11 - index,
                    "market_cap_as_of": "2026-09-18",
                    "market_cap_available_at": "2026-09-18T20:00:00Z",
                    "market_cap_received_at": "2026-09-18T20:00:00Z",
                }
                for index in range(count)
            ]
        )
        return {
            "as_of": "2026-09-18",
            "securities": securities_frame,
            "prices": prices,
            "fundamentals": pd.DataFrame(),
            "issues": [],
        }

    def config(self):
        from copy import deepcopy

        from portfolio_lab.config import DEFAULTS

        return deepcopy(DEFAULTS)

    def masks(self, run):
        """How many whole-panel boolean masks one call takes."""
        original = pd.DataFrame.__getitem__
        seen = []

        def counting(frame, key):
            if isinstance(key, pd.Series) and key.dtype == bool and len(key) >= self.MASK_FLOOR:
                seen.append(len(key))
            return original(frame, key)

        with patch.object(pd.DataFrame, "__getitem__", counting):
            run()
        return len(seen)

    def test_scoring_does_not_rescan_the_price_panel_per_security(self):
        from portfolio_lab.metrics import score_securities

        config = self.config()
        small, large = self.panel(5), self.panel(40)
        few = self.masks(lambda: score_securities(small, config))
        many = self.masks(lambda: score_securities(large, config))
        self.assertLessEqual(many, few + 4, f"{few} masks at n=5, {many} at n=40")

    def test_covariance_does_not_rescan_the_price_panel_per_security(self):
        from portfolio_lab.metrics import portfolio_covariance

        config = self.config()
        small, large = self.panel(5, sessions=200), self.panel(40, sessions=200)
        few = self.masks(
            lambda: portfolio_covariance(small, config, sorted(small["securities"].security_id))
        )
        many = self.masks(
            lambda: portfolio_covariance(large, config, sorted(large["securities"].security_id))
        )
        self.assertLessEqual(many, few + 4, f"{few} masks at n=5, {many} at n=40")

    def test_known_price_reads_only_the_rows_it_is_handed(self):
        from portfolio_lab import allocation

        bundle = self.panel(40)
        prices = bundle["prices"]
        grouped = dict(tuple(prices.groupby("security_id", sort=False)))
        cutoff = pd.Timestamp("2026-09-18T20:00:00Z")
        config = self.config()
        scans = self.masks(
            lambda: allocation._known_price(
                bundle, config, prices, "S0000", cutoff, grouped["S0000"]
            )
        )
        self.assertEqual(scans, 0)


class SinglePricingTests(CandidateForecastAdmissionTests):
    """Admission and the proposal build read each security's price once between them."""

    def test_no_security_is_priced_twice_in_one_run(self):
        from portfolio_lab import allocation

        self.add_candidate("D", probability=None)
        reference = self.bundle["forecasts"]
        mask = reference.security_id == "D"
        stated = {
            row.scenario: row.probability
            for row in reference[reference.security_id == "A"].itertuples()
        }
        reference.loc[mask, "probability"] = reference.loc[mask, "scenario"].map(stated)
        priced, original = [], allocation._known_price

        def counting(*args, **kwargs):
            priced.append(args[3])
            return original(*args, **kwargs)

        with patch.object(allocation, "_known_price", counting):
            result = self.run_proposals()
        self.assertNotEqual(result["status"], "blocked", result["issues"])
        self.assertEqual(sorted(priced), sorted(set(priced)))
        self.assertIn("D", priced)


class PreviousReviewExtractTests(unittest.TestCase):
    """The prior run is read for the one part prioritisation compares against."""

    def store(self, result):
        from portfolio_research.repository import ResearchRepository

        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        store = ResearchRepository(str(Path(temp.name) / "research.sqlite"))
        store.save_run(result, {"data": {}}, {"as_of": AS_OF})
        return store

    def result(self):
        return {
            "as_of": AS_OF,
            "research": {"OWN": {"brief": {"facts": []}, "proposals": None, "coverage": {}}},
            "signals": [{"security_id": "OWN"}],
            "holdings": [],
        }

    def test_only_the_research_the_comparison_reads_is_loaded(self):
        from portfolio_research.workflow import _previous_review

        previous = _previous_review(self.store(self.result()))
        self.assertEqual(sorted(previous), ["research"])
        self.assertIn("OWN", previous["research"])

    def test_no_archive_is_simply_no_comparison(self):
        from portfolio_research.repository import ResearchRepository
        from portfolio_research.workflow import _previous_review

        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        empty = ResearchRepository(str(Path(temp.name) / "research.sqlite"))
        self.assertIsNone(_previous_review(empty))
        self.assertIsNone(_previous_review(None))


if __name__ == "__main__":
    unittest.main()
