"""One owner-editable shared state produces one dated joint scenario set.

Every security the review needs — owned, benchmark and candidate — is priced under the
same states, on one forecast date, with identical labels, so the allocation engine's
joint-panel contract holds without a per-security forecast being invented anywhere.
No network: the frame is generated from an explicit dict and the only bundle used is
the local synthetic demo.
"""

import tempfile
import unittest

import pandas as pd

from portfolio_lab import allocation, metrics
from portfolio_lab.config import DEFAULTS, dashboard_patch, validate_config
from portfolio_lab.demo import create_demo
from portfolio_lab.pipeline import load_inputs
from portfolio_research.fx import usd_scenario_return
from portfolio_research.scenarios import (
    DEFAULT_SHARED_STATE,
    SHARED_STATE_VERSION,
    generate_joint_forecasts,
)

AS_OF = "2026-08-31"
LABELS = {"Adverse", "Central", "Favorable"}
FRAME_COLUMNS = [
    "security_id",
    "scenario",
    "horizon_months",
    "return_value",
    "probability",
    "basis",
    "source",
    "forecast_date",
    "calibration_id",
    "received_at",
    "available_at",
]


def securities(*rows) -> pd.DataFrame:
    return pd.DataFrame(list(rows))


def security(sid, sector="Technology", currency="USD", **extra) -> dict:
    return {
        "security_id": sid,
        "ticker": sid,
        "name": f"{sid} Inc",
        "sector": sector,
        "currency": currency,
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


class SharedStateTests(unittest.TestCase):
    def test_default_shared_state_is_versioned_and_complete(self):
        self.assertEqual(DEFAULT_SHARED_STATE["version"], SHARED_STATE_VERSION)
        self.assertEqual(set(DEFAULT_SHARED_STATE["market_returns"]), LABELS)
        self.assertEqual(DEFAULT_SHARED_STATE["probabilities"], None)
        for code, states in DEFAULT_SHARED_STATE["fx_returns"].items():
            self.assertRegex(code, r"^[A-Z]{3}$")
            self.assertEqual(set(states), LABELS)

    def test_the_default_state_is_not_mutated_by_generation(self):
        before = repr(DEFAULT_SHARED_STATE)
        generate(securities(security("AAA"), security("BBB", sector="Unknown")))
        self.assertEqual(repr(DEFAULT_SHARED_STATE), before)


class JointSetTests(unittest.TestCase):
    def test_every_security_is_covered_on_one_date_with_the_same_labels(self):
        frame = generate(
            securities(
                security("AAA"),
                security("BBB", sector="Utilities"),
                security("ETF", sector="Fund"),
            )
        )
        self.assertEqual(list(frame.columns), FRAME_COLUMNS)
        self.assertEqual(set(frame.security_id), {"AAA", "BBB", "ETF"})
        self.assertEqual(set(frame.forecast_date), {AS_OF})
        self.assertEqual(set(frame.horizon_months), {12})
        self.assertEqual(set(frame.basis), {"subjective"})
        self.assertEqual(set(frame.source), {f"{SHARED_STATE_VERSION} mapping; owner-editable"})
        self.assertTrue(frame.calibration_id.isna().all())
        for sid, rows in frame.groupby("security_id"):
            self.assertEqual(set(rows.scenario), LABELS, sid)
            self.assertFalse(rows.scenario.duplicated().any(), sid)

    def test_sector_multiplier_scales_the_market_state_return(self):
        frame = generate(securities(security("AAA", sector="Technology")))
        multiplier = DEFAULT_SHARED_STATE["sector_multipliers"]["Technology"]
        for label, market in DEFAULT_SHARED_STATE["market_returns"].items():
            self.assertAlmostEqual(value(frame, "AAA", label), multiplier * market, places=12)

    def test_horizon_scaling_compounds_the_twelve_month_state_return(self):
        rows = securities(security("AAA", sector="Energy"))
        annual = generate(rows)
        for horizon in (6, 18):
            scaled = generate(rows, horizon_months=horizon)
            self.assertEqual(set(scaled.horizon_months), {horizon})
            for label in LABELS:
                expected = (1 + value(annual, "AAA", label)) ** (horizon / 12) - 1
                self.assertAlmostEqual(value(scaled, "AAA", label), expected, places=12)

    def test_foreign_listing_composes_local_and_currency_returns(self):
        frame = generate(
            securities(security("AAA", sector="Energy"), security("CAA", currency="CAD"))
        )
        horizon = 12
        for label in LABELS:
            local = (
                DEFAULT_SHARED_STATE["sector_multipliers"]["Technology"]
                * DEFAULT_SHARED_STATE["market_returns"][label]
            )
            local = (1 + local) ** (horizon / 12) - 1
            fx = (1 + DEFAULT_SHARED_STATE["fx_returns"]["CAD"][label]) ** (horizon / 12) - 1
            self.assertAlmostEqual(
                value(frame, "CAA", label), usd_scenario_return(local, fx), places=12
            )
            self.assertAlmostEqual(
                value(frame, "CAA", label), (1 + local) * (1 + fx) - 1, places=12
            )

    def test_presentation_currency_decides_which_listing_is_foreign(self):
        rows = securities(security("CAA", currency="CAD"))
        frame = generate(rows, presentation_currency="CAD")
        for label in LABELS:
            local = (
                DEFAULT_SHARED_STATE["sector_multipliers"]["Technology"]
                * DEFAULT_SHARED_STATE["market_returns"][label]
            )
            self.assertAlmostEqual(value(frame, "CAA", label), local, places=12)

    def test_unmapped_sector_keeps_a_neutral_multiplier_and_records_an_issue(self):
        frame = generate(securities(security("AAA", sector="Fund"), security("BBB", sector=None)))
        for sid in ("AAA", "BBB"):
            for label, market in DEFAULT_SHARED_STATE["market_returns"].items():
                self.assertAlmostEqual(value(frame, sid, label), market, places=12)
        unknown = [i for i in frame.attrs["issues"] if i["code"] == "UNKNOWN_SECTOR_MULTIPLIER"]
        self.assertEqual({i["security_id"] for i in unknown}, {"AAA", "BBB"})

    def test_missing_currency_state_excludes_the_asset_with_an_issue(self):
        frame = generate(securities(security("AAA"), security("JPA", currency="JPY")))
        self.assertEqual(set(frame.security_id), {"AAA"})
        missing = [i for i in frame.attrs["issues"] if i["code"] == "MISSING_FX_STATE"]
        self.assertEqual([i["security_id"] for i in missing], ["JPA"])

    def test_cash_is_excluded_because_its_return_stays_an_explicit_setting(self):
        frame = generate(
            securities(
                security("AAA"),
                security("CASH", sector=None, instrument_type="cash"),
                security("USD_CASH", sector=None, instrument_type="cash"),
            )
        )
        self.assertEqual(set(frame.security_id), {"AAA"})
        self.assertNotIn("MISSING_FX_STATE", codes(frame))

    def test_probabilities_stay_blank_until_a_complete_distribution_is_supplied(self):
        frame = generate(securities(security("AAA")))
        self.assertTrue(frame.probability.isna().all())
        state = {
            **DEFAULT_SHARED_STATE,
            "probabilities": {"Adverse": 0.25, "Central": 0.5, "Favorable": 0.25},
        }
        weighted = generate(securities(security("AAA")), state)
        self.assertEqual(
            {(r.scenario, r.probability) for r in weighted.itertuples()},
            {("Adverse", 0.25), ("Central", 0.5), ("Favorable", 0.25)},
        )

    def test_probabilities_that_do_not_sum_to_one_are_never_normalized(self):
        state = {
            **DEFAULT_SHARED_STATE,
            "probabilities": {"Adverse": 0.25, "Central": 0.5, "Favorable": 0.10},
        }
        frame = generate(securities(security("AAA")), state)
        self.assertTrue(frame.probability.isna().all())
        self.assertIn("INCOMPLETE_STATE_PROBABILITIES", codes(frame))

    def test_asset_override_replaces_the_mapped_local_return(self):
        state = {**DEFAULT_SHARED_STATE, "asset_overrides": {"AAA": {"Adverse": -0.5}}}
        frame = generate(securities(security("AAA")), state)
        self.assertAlmostEqual(value(frame, "AAA", "Adverse"), -0.5, places=12)
        multiplier = DEFAULT_SHARED_STATE["sector_multipliers"]["Technology"]
        self.assertAlmostEqual(
            value(frame, "AAA", "Central"),
            multiplier * DEFAULT_SHARED_STATE["market_returns"]["Central"],
            places=12,
        )

    def test_an_empty_securities_frame_produces_an_empty_typed_set(self):
        frame = generate(pd.DataFrame())
        self.assertTrue(frame.empty)
        self.assertEqual(list(frame.columns), FRAME_COLUMNS)

    def test_a_state_of_the_wrong_version_is_refused(self):
        with self.assertRaises(ValueError):
            generate(securities(security("AAA")), {**DEFAULT_SHARED_STATE, "version": "other"})


class EngineAcceptanceTests(unittest.TestCase):
    """The generated set must satisfy the allocation engine's joint-panel contract."""

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        from portfolio_lab.config import load_config

        cls.config = load_config(create_demo(cls.temp.name))
        cls.config["risk"]["bootstrap_samples"] = 50
        cls.bundle = load_inputs(cls.config, AS_OF)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def replaced(self):
        bundle = dict(self.bundle)
        bundle["forecasts"] = generate(bundle["securities"], benchmark_ids=self.benchmarks())
        return bundle

    def benchmarks(self):
        """The mandate's broad equity benchmark, which the market states do describe."""
        return (self.config["mandate"]["benchmark_id"],)

    def test_the_generated_set_covers_every_demo_security(self):
        bundle = self.replaced()
        expected = set(self.bundle["securities"].security_id)
        self.assertEqual(set(bundle["forecasts"].security_id), expected)
        self.assertEqual(bundle["forecasts"].forecast_date.nunique(), 1)

    def test_scenario_analysis_accepts_the_generated_set(self):
        result = metrics.scenario_analysis(self.replaced(), self.config)
        self.assertIn(result["status"], {"complete", "partial"})
        self.assertEqual({s["scenario"] for s in result["scenarios"]}, LABELS)
        self.assertEqual(result["forecast_date"][:10], AS_OF)
        self.assertNotIn("missing_forecast_assets", {issue["code"] for issue in result["issues"]})

    def test_the_allocation_forecast_panel_accepts_the_generated_set(self):
        """The engine's own joint-panel validator, not just the scenario report."""
        bundle = self.replaced()
        ids = sorted(set(bundle["forecasts"].security_id))
        panel, issues = allocation._forecast_panel(
            bundle, self.config, ids, require_probabilities=False
        )
        self.assertIsNotNone(panel, [i["message"] for i in issues])
        self.assertEqual([i for i in issues if i["severity"] == "error"], [])
        self.assertIn("unweighted_scenarios", {i["code"] for i in issues})
        self.assertEqual(panel["ids"], ids)
        self.assertEqual(set(panel["names"]), LABELS)
        self.assertEqual(panel["basis"], "subjective")
        self.assertFalse(panel["weighted_available"])
        self.assertEqual(panel["returns"].shape, (len(LABELS), len(ids)))

    def test_a_stated_distribution_reaches_the_engine_as_one_common_set(self):
        bundle = dict(self.bundle)
        state = {
            **DEFAULT_SHARED_STATE,
            "probabilities": {"Adverse": 0.2, "Central": 0.6, "Favorable": 0.2},
        }
        bundle["forecasts"] = generate(bundle["securities"], state, benchmark_ids=self.benchmarks())
        ids = sorted(set(bundle["forecasts"].security_id))
        panel, issues = allocation._forecast_panel(
            bundle, self.config, ids, require_probabilities=True
        )
        self.assertIsNotNone(panel, [i["message"] for i in issues])
        self.assertTrue(panel["weighted_available"])
        self.assertAlmostEqual(float(panel["probabilities"].sum()), 1.0, places=12)

    def test_a_shared_state_without_probabilities_leaves_means_unavailable(self):
        result = metrics.scenario_analysis(self.replaced(), self.config)
        self.assertEqual(result["probability_status"], "unweighted")
        self.assertIsNone(result["weighted_return"])


class SharedStateConfigTests(unittest.TestCase):
    def patched(self, shared_state):
        return validate_config({"allocation": {"shared_state": shared_state}})

    def test_the_default_config_carries_the_default_shared_state(self):
        self.assertEqual(DEFAULTS["allocation"]["shared_state"], DEFAULT_SHARED_STATE)
        self.assertEqual(
            validate_config({})["allocation"]["shared_state"]["version"], SHARED_STATE_VERSION
        )

    def test_a_complete_replacement_state_is_accepted(self):
        state = {
            **DEFAULT_SHARED_STATE,
            "market_returns": {"Adverse": -0.1, "Central": 0.05, "Favorable": 0.15},
            "probabilities": {"Adverse": 0.3, "Central": 0.5, "Favorable": 0.2},
        }
        self.assertEqual(self.patched(state)["allocation"]["shared_state"], state)

    def test_an_invalid_shared_state_is_refused(self):
        for broken in [
            "not-an-object",
            {**DEFAULT_SHARED_STATE, "version": 1},
            {**DEFAULT_SHARED_STATE, "market_returns": {"Central": 0.06}},
            {**DEFAULT_SHARED_STATE, "market_returns": {"Adverse": None, "Central": 0.06}},
            {**DEFAULT_SHARED_STATE, "sector_multipliers": {"Technology": -1}},
            {**DEFAULT_SHARED_STATE, "sector_multipliers": {"Technology": "high"}},
            {**DEFAULT_SHARED_STATE, "fx_returns": {"CANADA": {"Adverse": 0.0}}},
            {
                **DEFAULT_SHARED_STATE,
                "fx_returns": {"CAD": {"Adverse": -0.06, "Central": 0.0}},
            },
            {
                **DEFAULT_SHARED_STATE,
                "probabilities": {"Adverse": 0.3, "Central": 0.5, "Favorable": 0.1},
            },
            {**DEFAULT_SHARED_STATE, "asset_overrides": {"AAA": {"Rally": 0.2}}},
            {**DEFAULT_SHARED_STATE, "horizon_months": 0},
            dict(DEFAULT_SHARED_STATE, **{"unexpected": 1}),
            {key: v for key, v in DEFAULT_SHARED_STATE.items() if key != "market_returns"},
        ]:
            with self.subTest(broken=broken):
                with self.assertRaises(ValueError):
                    self.patched(broken)

    def test_a_dashboard_edit_replaces_the_whole_state_instead_of_merging(self):
        config = validate_config({})
        state = {
            **DEFAULT_SHARED_STATE,
            "market_returns": {"Adverse": -0.3, "Central": 0.04, "Favorable": 0.2},
            "sector_multipliers": {"Technology": 1.4, "Unknown": 1.0},
        }
        patched = dashboard_patch(config, {"allocation": {"shared_state": state}})
        self.assertEqual(patched["allocation"]["shared_state"], state)
        self.assertEqual(
            set(patched["allocation"]["shared_state"]["sector_multipliers"]),
            {"Technology", "Unknown"},
        )

    def test_a_partial_dashboard_edit_is_refused_rather_than_silently_merged(self):
        config = validate_config({})
        with self.assertRaises(ValueError):
            dashboard_patch(config, {"allocation": {"shared_state": {"horizon_months": 12}}})

    def test_the_configured_state_generates_a_usable_set(self):
        config = validate_config({})
        frame = generate(securities(security("AAA")), config["allocation"]["shared_state"])
        self.assertEqual(len(frame), 3)


if __name__ == "__main__":
    unittest.main()
