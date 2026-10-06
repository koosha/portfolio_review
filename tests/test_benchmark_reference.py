"""An explicit/default comparison fund supplies assumptions, never holdings or trades."""

import tempfile
import unittest
from copy import deepcopy
from time import monotonic
from unittest.mock import patch

import pandas as pd

from portfolio_lab.config import DEFAULTS, load_config
from portfolio_lab.demo import create_demo
from portfolio_lab.metrics import portfolio_covariance
from portfolio_lab.pipeline import load_inputs, replay_analysis
from portfolio_research.application import analyze_review
from portfolio_research.benchmark import (
    comparison_metadata,
    enrich_reference_prices,
    prepare_benchmark,
    resolve_benchmark,
)
from portfolio_research.enrichment import _apply_shared_state
from portfolio_research.holding_analysis import build_holding_analysis
from portfolio_research.saved_view import saved_holding_view
from portfolio_research.service import ResearchService
from tests.support.monthly_journey import _inject
from tests.test_holding_analysis import fixture


class BenchmarkReferenceTests(unittest.TestCase):
    def setup(self, selection=None):
        security, bundle, config, result = fixture()
        config["mandate"]["benchmark_id"] = selection
        result["forecast_inputs"] = []
        return security, bundle, config, result

    def test_default_and_legacy_unset_use_voo_without_confirming_the_mandate(self):
        self.assertEqual(DEFAULTS["mandate"]["benchmark_id"], "VOO")
        _, bundle, config, _ = self.setup()
        original = deepcopy(config)
        used, reference = prepare_benchmark(bundle, config)
        self.assertEqual(used["mandate"]["benchmark_id"], "benchmark:VOO")
        self.assertTrue(reference["default_applied"])
        self.assertEqual(reference["ticker"], "VOO")
        self.assertFalse(used["mandate"]["confirmed"])
        self.assertEqual(config, original)
        self.assertEqual(bundle["securities"].security_id.tolist(), ["EQUITY"])

    def test_unique_saved_ticker_resolves_to_existing_id_with_no_new_reference(self):
        _, bundle, config, _ = self.setup("eq")
        used, reference = prepare_benchmark(bundle, config)
        self.assertEqual(used["mandate"]["benchmark_id"], "EQUITY")
        self.assertEqual(reference["selection"], "eq")
        self.assertEqual(reference["identity_source"], "saved_unique_ticker")
        self.assertTrue(reference["existing_security"])

    def test_exact_id_is_authoritative_even_when_it_spells_a_catalog_ticker(self):
        security, bundle, config, _ = self.setup("VOO")
        bundle["securities"] = pd.DataFrame([{**security, "security_id": "VOO", "ticker": "OTHER"}])
        reference = resolve_benchmark(bundle, config)
        self.assertEqual(reference["security_id"], "VOO")
        self.assertEqual(reference["ticker"], "OTHER")
        self.assertFalse(reference["equity_shared_state"])

    def test_ambiguous_ticker_does_not_fall_back_to_the_catalog(self):
        security, bundle, config, _ = self.setup("VOO")
        bundle["securities"] = pd.DataFrame(
            [
                {**security, "security_id": sid, "ticker": "VOO"}
                for sid in ("owned:one", "owned:two")
            ]
        )
        reference = resolve_benchmark(bundle, config)
        self.assertEqual(reference["status"], "ambiguous")
        self.assertIsNone(reference["security_id"])

    def test_default_has_three_unscaled_returns_with_no_invented_history(self):
        _, bundle, config, result = self.setup()
        config["allocation"]["shared_state"]["sector_multipliers"]["Unknown"] = 2
        _apply_shared_state(bundle, config, bundle["as_of"])
        used, reference = prepare_benchmark(bundle, config)
        result["forecast_inputs"] = bundle["forecasts"].to_dict("records")
        row = build_holding_analysis(result, bundle, used, {})[0]
        cases = bundle["forecasts"].query("security_id == 'benchmark:VOO'")
        self.assertEqual(len(cases), 3)
        for actual, expected in zip(cases.return_value.tolist(), [-0.2, 0.06, 0.18], strict=True):
            self.assertAlmostEqual(actual, expected)
        self.assertEqual(row["action"], "Buy")
        self.assertAlmostEqual(row["benchmark_return"], 0.06)
        self.assertFalse(reference["tradable"])
        self.assertNotIn("prices", bundle)
        self.assertFalse(
            any(
                issue.get("code") == "UNKNOWN_SECTOR_MULTIPLIER"
                and issue.get("security_id") == "benchmark:VOO"
                for issue in bundle.get("issues", [])
            )
        )

    def test_imported_benchmark_rows_take_precedence_and_keep_original_identifier(self):
        _, bundle, config, _ = self.setup("VOO")
        bundle["forecasts"] = pd.DataFrame(
            [
                {
                    "security_id": "VOO",
                    "scenario": label,
                    "return_value": value,
                    "horizon_months": 12,
                    "forecast_date": bundle["as_of"],
                    "basis": "subjective",
                    "source": "imported",
                    "probability": probability,
                }
                for label, value, probability in (
                    ("Adverse", -0.3, 0.25),
                    ("Central", 0.01, 0.5),
                    ("Favorable", 0.25, 0.25),
                )
            ]
        )
        _apply_shared_state(bundle, config, bundle["as_of"])
        rows = bundle["forecasts"].query("security_id == 'benchmark:VOO'")
        self.assertEqual(rows.return_value.tolist(), [-0.3, 0.01, 0.25])
        self.assertTrue(rows.source.eq("imported").all())
        self.assertTrue(rows.original_security_id.eq("VOO").all())

    def test_quote_fx_must_be_explicit_for_a_foreign_presentation_currency(self):
        _, bundle, config, _ = self.setup("VOO")
        config["mandate"]["base_currency"] = "CAD"
        _apply_shared_state(bundle, config, bundle["as_of"])
        self.assertTrue(bundle.get("forecasts", pd.DataFrame()).empty)
        self.assertTrue(
            any(
                issue.get("code") == "MISSING_FX_STATE"
                and issue.get("security_id") == "benchmark:VOO"
                for issue in bundle["issues"]
            )
        )

    def test_unknown_symbol_is_not_created_as_a_company_or_equity_index(self):
        _, bundle, config, _ = self.setup("unknown")
        _apply_shared_state(bundle, config, bundle["as_of"])
        self.assertEqual(bundle["benchmark_reference"]["status"], "unresolved")
        self.assertEqual(set(bundle["forecasts"].security_id), {"EQUITY"})

    def test_monthly_native_price_capability_has_provenance_and_obeys_controls(self):
        _, bundle, config, _ = self.setup("VOO")
        config["data"].update(mode="live", price_provider="yahoo", refresh_network=True)
        rows = [
            {
                "security_id": "benchmark:VOO",
                "date": bundle["as_of"],
                "close": 100,
                "adjusted_close": 100,
                "currency": "USD",
                "source_id": "prices:original",
                "received_at": bundle["as_of"],
            }
        ]
        record = {
            "currency": "USD",
            "prices": rows,
            "source_id": "prices:original",
            "received_at": bundle["as_of"],
        }
        with patch("portfolio_research.market_data.price_history", return_value=record) as fetch:
            enrich_reference_prices(bundle, config, bundle["as_of"], deadline=monotonic() + 10)
        fetch.assert_called_once()
        self.assertEqual(fetch.call_args.args[1]["ticker"], "VOO")
        self.assertEqual(bundle["sources"][0]["source_id"], "prices:original")
        self.assertEqual(bundle["securities"].security_id.tolist(), ["EQUITY"])
        with patch("portfolio_research.market_data.price_history") as fetch:
            enrich_reference_prices(bundle, config, bundle["as_of"], deadline=monotonic() - 1)
            enrich_reference_prices(
                bundle, config, bundle["as_of"], deadline=monotonic() + 10, should_stop=lambda: True
            )
        fetch.assert_not_called()

    def test_legacy_existing_review_action_gains_default_comparison_read_only(self):
        for choice in (None, "VOO"):
            _, bundle, config, result = self.setup(choice)
            result["holding_analysis"] = [{"security_id": "EQUITY", "action": "Review"}]
            original, frozen = deepcopy(result), deepcopy(bundle["research_inputs"])
            displayed = saved_holding_view(result, bundle, config)
            self.assertEqual(displayed["holding_analysis"][0]["action"], "Buy")
            self.assertEqual(displayed["benchmark_reference"]["ticker"], "VOO")
            self.assertTrue(
                displayed["metadata"]["computed_views"]["holding_analysis"]["archive_unchanged"]
            )
            self.assertEqual(result, original)
            self.assertEqual(bundle["research_inputs"], frozen)


class BenchmarkPipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.config = load_config(create_demo(cls.temp.name))
        cls.config["risk"]["bootstrap_samples"] = 50
        cls.bundle = load_inputs(cls.config, "2026-08-31")
        _inject(cls.bundle, cls.config, "2026-08-31")

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_real_replay_selects_default_and_retains_archive_and_holdings(self):
        config, bundle = deepcopy(self.config), deepcopy(self.bundle)
        config["mandate"]["benchmark_id"] = None
        legacy = analyze_review(bundle, config)
        legacy.pop("benchmark_reference")
        legacy["forecast_inputs"] = [
            row for row in legacy["forecast_inputs"] if row["security_id"] != "benchmark:VOO"
        ]
        legacy["holding_analysis"] = [
            {"security_id": row["security_id"], "action": "Review"} for row in legacy["holdings"]
        ]
        service = ResearchService(config)
        self.addCleanup(service.close)
        run_id = service.store.save_run(legacy, config, bundle)
        stored = service.store.load_run(run_id)
        with patch(
            "portfolio_research.market_data.price_history", side_effect=AssertionError("network")
        ):
            view = service.run(run_id)["result"]
            result, frozen, used = replay_analysis(config, run_id)
        self.assertEqual(view["benchmark_reference"]["ticker"], "VOO")
        self.assertEqual(result["benchmark_reference"]["ticker"], "VOO")
        self.assertEqual(result["benchmark_reference"]["configured_id"], None)
        self.assertTrue(result["benchmark_reference"]["default_applied"])
        self.assertIsNone(used["mandate"]["benchmark_id"])
        self.assertEqual(service.store.load_run(run_id), stored)
        self.assertNotIn("benchmark:VOO", frozen["securities"].security_id.tolist())
        self.assertNotIn("benchmark:VOO", {row["security_id"] for row in result["holdings"]})
        self.assertEqual(result["scenarios"]["status"], "complete")
        self.assertTrue(
            any(
                row["action"] in {"Buy", "Sell", "Hold"}
                for row in result["holding_analysis"]
                if row["security_id"] in {"SIM01", "SIM03"}
            )
        )
        self.assertIsNone(result["risk"].get("beta"))

    def test_switch_drops_only_inactive_catalog_overrides_from_calculation_copy(self):
        config, bundle = deepcopy(self.config), deepcopy(self.bundle)
        config["mandate"]["benchmark_id"] = "VOO"
        config["allocation"]["return_overrides"] = {
            "benchmark:VTI": {"Central": 0.03},
            "VOO": {"Central": 0.04},
        }
        original = deepcopy(config)
        result = analyze_review(bundle, config)
        self.assertFalse(
            any(issue.get("code") == "allocation_failed" for issue in result["issues"]),
            result["issues"],
        )
        self.assertEqual(result["scenarios"]["status"], "complete")
        self.assertEqual(config, original)
        central = next(
            row
            for row in result["forecast_inputs"]
            if row["security_id"] == "benchmark:VOO" and row["scenario"] == "Central"
        )
        self.assertAlmostEqual(central["return_value"], 0.04)

    def test_observed_comparator_history_supports_covariance_but_never_trades(self):
        config, bundle = deepcopy(self.config), deepcopy(self.bundle)
        config["mandate"]["benchmark_id"] = "VOO"
        imported = bundle["prices"].query("security_id == 'SIMETF'").copy()
        imported["security_id"] = "VOO"
        bundle["prices"] = pd.concat([bundle["prices"], imported], ignore_index=True)
        used, _ = prepare_benchmark(bundle, config)
        covariance = portfolio_covariance(bundle, used, ["SIM01", "benchmark:VOO"])
        self.assertTrue(covariance["complete"], covariance["issues"])
        metadata = comparison_metadata(bundle, bundle["securities"])
        reference = metadata.query("security_id == 'benchmark:VOO'").iloc[0]
        self.assertFalse(reference["eligible"])
        self.assertTrue(reference["comparison_only"])
        for account in config["mandate"]["account_permissions"]:
            config["mandate"]["account_permissions"][account].append("benchmark:VOO")
        result = analyze_review(bundle, config)
        self.assertFalse(
            any(issue.get("code") == "allocation_failed" for issue in result["issues"]),
            result["issues"],
        )
        self.assertNotEqual(
            result["allocation"]["status"], "blocked", result["allocation"].get("issues")
        )
        self.assertFalse(
            any(row.get("security_id") == "benchmark:VOO" for row in result.get("proposals", []))
        )
        self.assertNotIn("benchmark:VOO", {row.get("security_id") for row in result["signals"]})
