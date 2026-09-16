import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from portfolio.storage import Store
from portfolio_lab import metrics
from portfolio_lab.analytics import macro_panel
from portfolio_lab.ingestion import ResearchStore
from portfolio_lab.pipeline import load_inputs, replay_analysis, run_analysis
from portfolio_lab.providers import _filter_frames
from portfolio_research.calendar import bundle_cutoff, review_context
from portfolio_research.fx import FxTable
from portfolio_research.service import default_config

SUNDAY_RECEIPT = "2026-09-13T20:00:00+00:00"
SUNDAY_GENERATED = "2026-09-13T21:00:00+00:00"
FRIDAY = "2026-09-11"
FRIDAY_CUTOFF = "2026-09-11T20:00:00+00:00"
MONDAY_BEFORE_CLOSE = "2026-09-14T19:59:00+00:00"
MONDAY_CLOSE = "2026-09-14T20:00:00+00:00"


def table(symbol="DEMO", value="10", currency="USD"):
    headers = ["Symbol", "Shares", "Last Price", "Market Value ($)"]
    rows = [[symbol, "1", value, value], ["Total Cash", "", "2.03", ""]]
    if currency:
        headers = [*headers, "Currency"]
        rows = [[*row, currency] for row in rows]
    return {
        "method": "yahoo-holdings-table-v1",
        "headers": headers,
        "rows": rows,
        "page_count": 1,
        "expected_count": len(rows),
        "completeness": "count-verified",
    }


class PipelineContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.a = self.store.add_source(
            "Labelled account", url="https://finance.yahoo.com/portfolio/p_fixture_pipeline_a"
        )
        self.b = self.store.add_source(
            "Unlabelled account", url="https://finance.yahoo.com/portfolio/p_fixture_pipeline_b"
        )
        with patch("portfolio.storage.now", return_value=SUNDAY_RECEIPT):
            self.batch = self.store.begin_batch([self.a, self.b])
            self.store.ingest_table(self.a, table("DEMO"), batch_id=self.batch)
            self.store.ingest_table(self.b, table("OTHER", currency=None), batch_id=self.batch)
        self.config = default_config(self.temp.name)

    def test_current_review_uses_sunday_collection_and_generation_cutoff(self):
        bundle = load_inputs(self.config, review_kind="current", generated_at=SUNDAY_GENERATED)
        self.assertEqual(bundle["as_of"], FRIDAY)
        collector = bundle["collector"]
        self.assertIsNotNone(collector["batch"])
        self.assertEqual(collector["batch"]["id"], self.batch)
        self.assertEqual(collector["receipt_through"], "2026-09-13")
        self.assertEqual(collector["collection_received_at"], SUNDAY_RECEIPT)
        self.assertFalse(collector["legacy_partial"])
        timeline = bundle["timeline"]
        self.assertEqual(timeline["review_kind"], "current")
        self.assertEqual(timeline["requested_date"], "2026-09-13")
        self.assertEqual(timeline["market_observation_date"], FRIDAY)
        self.assertEqual(timeline["decision_date"], FRIDAY)
        self.assertEqual(timeline["information_cutoff"], SUNDAY_GENERATED)
        self.assertEqual(timeline["decision_cutoff"], SUNDAY_GENERATED)
        self.assertEqual(timeline["generated_at"], SUNDAY_GENERATED)
        self.assertEqual(timeline["collection_received_at"], SUNDAY_RECEIPT)
        self.assertEqual(timeline["earliest_execution_date"], "2026-09-14")
        self.assertEqual(timeline["earliest_execution_at"], "2026-09-14T20:00:00+00:00")
        self.assertEqual(metrics._cutoff(bundle), pd.Timestamp(SUNDAY_GENERATED))
        self.assertEqual(bundle_cutoff(bundle), pd.Timestamp(SUNDAY_GENERATED))
        self.assertFalse(bundle["positions"].empty)
        self.assertEqual(len(bundle["ledger"]["positions"]), 2)

    def test_historical_review_keeps_friday_cutoff_and_excludes_sunday_capture(self):
        bundle = load_inputs(self.config, FRIDAY)
        self.assertEqual(bundle["as_of"], FRIDAY)
        self.assertIsNone(bundle["collector"]["batch"])
        self.assertEqual(bundle["collector"]["receipt_through"], FRIDAY)
        timeline = bundle["timeline"]
        self.assertEqual(timeline["review_kind"], "historical")
        self.assertEqual(timeline["decision_date"], FRIDAY)
        self.assertEqual(timeline["market_observation_date"], FRIDAY)
        self.assertEqual(timeline["decision_cutoff"], FRIDAY_CUTOFF)
        self.assertEqual(timeline["information_cutoff"], FRIDAY_CUTOFF)
        self.assertEqual(timeline["execution_basis"], "after_decision_cutoff")
        self.assertEqual(metrics._cutoff(bundle), pd.Timestamp(FRIDAY_CUTOFF))
        self.assertTrue(bundle["positions"].empty)
        codes = {issue["code"] for issue in bundle["issues"]}
        self.assertIn("MISSING_ACCOUNT_SNAPSHOT", codes)

    def test_explicit_historical_kind_matches_the_default(self):
        default = load_inputs(self.config, FRIDAY)
        explicit = load_inputs(
            self.config, FRIDAY, review_kind="historical", generated_at=SUNDAY_GENERATED
        )
        self.assertEqual(explicit["timeline"]["review_kind"], "historical")
        self.assertEqual(explicit["timeline"]["generated_at"], SUNDAY_GENERATED)
        self.assertEqual(explicit["timeline"]["historical_basis"], "recorded")
        for key in ("decision_date", "decision_cutoff", "information_cutoff", "market_close"):
            self.assertEqual(explicit["timeline"][key], default["timeline"][key], key)

    def test_current_review_rejects_explicit_date_and_unknown_kind(self):
        with self.assertRaises(ValueError):
            load_inputs(self.config, FRIDAY, review_kind="current")
        with self.assertRaises(ValueError):
            load_inputs(self.config, review_kind="weekly")
        with self.assertRaises(ValueError):
            load_inputs(self.config, review_kind="current", generated_at="2026-09-13T21:00:00")

    def test_run_analysis_records_current_review_kind(self):
        result, bundle = run_analysis(
            self.config, save=False, review_kind="current", generated_at=SUNDAY_GENERATED
        )
        metadata = result["metadata"]
        self.assertEqual(metadata["review_kind"], "current")
        self.assertEqual(metadata["as_of"], FRIDAY)
        self.assertEqual(metadata["market_observation_date"], FRIDAY)
        self.assertEqual(metadata["information_cutoff"], SUNDAY_GENERATED)
        self.assertEqual(metadata["collection_received_at"], SUNDAY_RECEIPT)
        self.assertEqual(result["timeline"]["review_kind"], "current")
        self.assertEqual(result["timeline"]["information_cutoff"], SUNDAY_GENERATED)
        self.assertEqual(result["input_status"]["collection_received_at"], SUNDAY_RECEIPT)
        self.assertEqual(bundle["timeline"]["review_kind"], "current")

    def test_run_analysis_defaults_to_historical_metadata(self):
        result, _ = run_analysis(self.config, FRIDAY, save=False)
        metadata = result["metadata"]
        self.assertEqual(metadata["review_kind"], "historical")
        self.assertEqual(metadata["as_of"], FRIDAY)
        self.assertEqual(metadata["market_observation_date"], FRIDAY)
        self.assertEqual(metadata["information_cutoff"], FRIDAY_CUTOFF)
        self.assertIsNone(metadata["collection_received_at"])
        self.assertEqual(result["timeline"]["review_kind"], "historical")

    def test_replay_keeps_frozen_current_cutoff_and_kind(self):
        saved, _ = run_analysis(
            self.config, save=True, review_kind="current", generated_at=SUNDAY_GENERATED
        )
        replayed, frozen, _ = replay_analysis(self.config, saved["run_id"])
        timeline = replayed["timeline"]
        self.assertEqual(timeline["review_kind"], "current")
        self.assertEqual(timeline["information_cutoff"], SUNDAY_GENERATED)
        self.assertEqual(timeline["decision_date"], FRIDAY)
        self.assertEqual(timeline["collection_received_at"], SUNDAY_RECEIPT)
        self.assertTrue(timeline["frozen_input_replay"])
        self.assertEqual(timeline["original_generated_at"], SUNDAY_GENERATED)
        self.assertNotEqual(timeline["generated_at"], SUNDAY_GENERATED)
        self.assertEqual(replayed["metadata"]["review_kind"], "current")
        self.assertEqual(replayed["metadata"]["information_cutoff"], SUNDAY_GENERATED)
        self.assertEqual(replayed["metadata"]["parent_run_id"], saved["run_id"])
        self.assertTrue(replayed["metadata"]["preview"])
        self.assertEqual(metrics._cutoff(frozen), pd.Timestamp(SUNDAY_GENERATED))
        self.assertEqual(frozen["timeline"]["review_kind"], "current")

    def test_load_inputs_delegates_kind_and_date_errors_to_the_calendar(self):
        for kwargs in (
            {"as_of": FRIDAY, "review_kind": "current"},
            {"review_kind": "weekly"},
            {"review_kind": None},
        ):
            with self.subTest(**kwargs):
                with self.assertRaises(ValueError) as expected:
                    review_context(kwargs["review_kind"], as_of=kwargs.get("as_of"))
                with self.assertRaises(ValueError) as actual:
                    load_inputs(self.config, **kwargs)
                self.assertEqual(str(actual.exception), str(expected.exception))

    def test_current_review_ignores_a_collection_received_after_generation(self):
        with patch("portfolio.storage.now", return_value="2026-09-14T19:00:00+00:00"):
            later = self.store.begin_batch([self.a, self.b])
            self.store.ingest_table(self.a, table("LATER"), batch_id=later)
            self.store.ingest_table(self.b, table("LATEST", currency=None), batch_id=later)
        generated = "2026-09-14T15:00:00+00:00"
        bundle = load_inputs(self.config, review_kind="current", generated_at=generated)
        self.assertEqual(bundle["collector"]["batch"]["id"], self.batch)
        self.assertEqual(bundle["timeline"]["collection_received_at"], SUNDAY_RECEIPT)
        self.assertLessEqual(
            pd.Timestamp(bundle["timeline"]["collection_received_at"]),
            pd.Timestamp(bundle["timeline"]["information_cutoff"]),
        )
        # The later batch did not exist at generation, so it is neither failed nor in progress.
        self.assertFalse(bundle["collector"]["newer_collection_in_progress"])
        self.assertEqual(
            {row["raw_symbol"] for row in bundle["ledger"]["positions"]}, {"DEMO", "OTHER"}
        )
        after = load_inputs(
            self.config, review_kind="current", generated_at="2026-09-14T19:30:00+00:00"
        )
        self.assertEqual(after["collector"]["batch"]["id"], later)

    def test_execution_moves_past_a_close_that_falls_between_load_and_save(self):
        with patch("portfolio_lab.pipeline.monotonic", side_effect=[0.0, 240.0]):
            result, bundle = run_analysis(
                self.config, save=True, review_kind="current", generated_at=MONDAY_BEFORE_CLOSE
            )
        store = ResearchStore(self.config["research"]["path"])
        for label, timeline in (
            ("result", result["timeline"]),
            ("bundle", bundle["timeline"]),
            ("saved run", store.load_run(result["run_id"])["timeline"]),
            ("saved bundle", store.load_bundle(result["run_id"])["timeline"]),
        ):
            with self.subTest(label=label):
                self.assertEqual(timeline["information_cutoff"], MONDAY_BEFORE_CLOSE)
                self.assertEqual(timeline["decision_cutoff"], MONDAY_BEFORE_CLOSE)
                self.assertEqual(timeline["generated_at"], "2026-09-14T20:03:00+00:00")
                self.assertEqual(timeline["earliest_execution_date"], "2026-09-15")
                self.assertEqual(timeline["earliest_execution_at"], "2026-09-15T20:00:00+00:00")
                self.assertGreater(
                    pd.Timestamp(timeline["earliest_execution_at"]),
                    pd.Timestamp(timeline["generated_at"]),
                )
        self.assertEqual(result["metadata"]["information_cutoff"], MONDAY_BEFORE_CLOSE)
        self.assertEqual(metrics._cutoff(bundle), pd.Timestamp(MONDAY_BEFORE_CLOSE))

    def test_execution_stays_when_the_result_exists_before_the_close(self):
        with patch("portfolio_lab.pipeline.monotonic", side_effect=[0.0, 30.0]):
            result, bundle = run_analysis(
                self.config, save=False, review_kind="current", generated_at=MONDAY_BEFORE_CLOSE
            )
        for timeline in (result["timeline"], bundle["timeline"]):
            self.assertEqual(timeline["generated_at"], MONDAY_BEFORE_CLOSE)
            self.assertEqual(timeline["earliest_execution_date"], "2026-09-14")
            self.assertEqual(timeline["earliest_execution_at"], MONDAY_CLOSE)

    def test_historical_execution_is_not_moved_by_a_long_run(self):
        generated = "2026-09-14T19:59:00+00:00"
        with patch("portfolio_lab.pipeline.monotonic", side_effect=[0.0, 3600.0]):
            result, _ = run_analysis(self.config, FRIDAY, save=False, generated_at=generated)
        self.assertEqual(result["timeline"]["generated_at"], generated)
        self.assertEqual(result["timeline"]["earliest_execution_at"], MONDAY_CLOSE)


class DateOnlyCutoffTests(unittest.TestCase):
    """A bare calendar date is available from the start of its New York day."""

    SUNDAY_EVENING = "2026-09-14T00:30:00+00:00"  # Sunday 20:30 in New York.
    CONFIG = {"data": {"require_received_by_cutoff": False}}

    def current(self, generated_at=SUNDAY_EVENING):
        return {
            "as_of": FRIDAY,
            "timeline": review_context("current", generated_at=generated_at),
            "issues": [],
        }

    @staticmethod
    def forecasts(*days):
        return pd.DataFrame(
            [
                {
                    "security_id": "DEMO",
                    "scenario": f"scenario-{day}",
                    "horizon_months": 12,
                    "forecast_date": day,
                    "return_value": 0.1,
                    "probability": 0.5,
                }
                for day in days
            ]
        )

    def test_next_day_forecast_is_excluded_on_sunday_evening(self):
        for generated in (self.SUNDAY_EVENING, "2026-09-13T23:30:00+00:00"):
            with self.subTest(generated=generated):
                bundle = self.current(generated)
                frame = self.forecasts("2026-09-13", "2026-09-14")
                observed = metrics._observed(frame, bundle, "forecast_date", None)
                self.assertEqual(observed["forecast_date"].tolist(), ["2026-09-13"])
                bundle["forecasts"] = frame
                _filter_frames(bundle, FRIDAY)
                self.assertEqual(bundle["forecasts"]["forecast_date"].tolist(), ["2026-09-13"])
                codes = {issue["code"] for issue in bundle["issues"]}
                self.assertIn("ASOF_ROWS_EXCLUDED", codes)

    def test_next_day_macro_vintage_is_excluded_on_sunday_evening(self):
        bundle = self.current()
        bundle["macro"] = pd.DataFrame(
            [
                {
                    "series_id": "DGS10",
                    "date": FRIDAY,
                    "vintage_date": vintage,
                    "value": value,
                    "units": "percent",
                }
                for vintage, value in (("2026-09-13", 4.0), ("2026-09-14", 5.0))
            ]
        )
        [series] = macro_panel(bundle, self.CONFIG)
        self.assertEqual(series["latest_value"], 4.0)
        self.assertEqual(series["vintage_date"], "2026-09-13")

    def test_historical_cutoff_selection_is_unchanged(self):
        bundle = {"as_of": FRIDAY, "issues": []}
        frame = self.forecasts("2026-09-10", FRIDAY, "2026-09-12")
        observed = metrics._observed(frame, bundle, "forecast_date", None)
        self.assertEqual(observed["forecast_date"].tolist(), ["2026-09-10", FRIDAY])
        bundle["forecasts"] = frame
        _filter_frames(bundle, FRIDAY)
        self.assertEqual(bundle["forecasts"]["forecast_date"].tolist(), ["2026-09-10", FRIDAY])


class PriceSeriesCurrencyTests(unittest.TestCase):
    """The presented price series is converted with FX asked for on the series' own terms."""

    EARLY = "2019-05-01"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.source = self.store.add_source(
            "USD account", url="https://finance.yahoo.com/portfolio/p_fixture_price_currency"
        )
        with patch("portfolio.storage.now", return_value=SUNDAY_RECEIPT):
            batch = self.store.begin_batch([self.source])
            self.store.ingest_table(self.source, table("DEMO"), batch_id=batch)
        self.config = default_config(self.temp.name)
        self.requested = []

    def prices_csv(self, rows):
        path = Path(self.temp.name) / "prices.csv"
        header = "security_id,date,close,adjusted_close,currency,available_at,received_at\n"
        lines = [
            f"{security},{day},{close},{close},{currency},{day}T20:01:00Z,{day}T20:02:00Z\n"
            for security, day, close, currency in rows
        ]
        path.write_text(header + "".join(lines), encoding="utf-8")
        config = deepcopy(self.config)
        config["data"]["prices_csv"] = str(path)
        return config

    def loader(self, rates):
        """Stub provider: records each request and serves ``rates`` for the asked currencies."""

        def load(config, currencies, *, start_date, end_date, refresh, issues):
            self.requested.append(
                {"currencies": sorted(currencies), "start": start_date, "end": end_date}
            )
            served = [
                observation
                for observation in rates
                if observation["base"] in set(currencies)
                and start_date <= observation["date"] <= end_date
            ]
            return FxTable(served, max_age_days=7)

        return patch("portfolio_research.fx_providers.load_fx_table", side_effect=load)

    @staticmethod
    def rate(day, rate="0.7211"):
        return {
            "pair": "CADUSD",
            "base": "CAD",
            "quote": "USD",
            "rate": rate,
            "date": day,
            "source_id": f"yahoo_fx:CADUSD:{day}",
            "provider": "yahoo",
            "received_at": SUNDAY_RECEIPT,
        }

    def load(self, config, rates):
        with self.loader(rates):
            return load_inputs(config, review_kind="current", generated_at=SUNDAY_GENERATED)

    def series(self, bundle, security_id):
        prices = bundle["prices"]
        return prices[prices["security_id"] == security_id]

    def test_a_price_row_in_a_currency_no_holding_uses_is_converted_not_dropped(self):
        config = self.prices_csv(
            [
                ("DEMO", "2026-09-10", "200", "USD"),
                ("RY.TO", "2026-09-10", "180", "CAD"),
                ("RY.TO", FRIDAY, "181", "CAD"),
            ]
        )
        bundle = self.load(config, [self.rate("2026-09-10"), self.rate(FRIDAY)])
        candidate = self.series(bundle, "RY.TO")
        self.assertEqual(len(candidate), 2)
        self.assertEqual(set(candidate["currency"]), {"USD"})
        self.assertEqual(set(candidate["local_currency"]), {"CAD"})
        self.assertAlmostEqual(candidate["close"].tolist()[0], 180 * 0.7211, places=9)
        self.assertEqual(len(self.series(bundle, "DEMO")), 1)
        codes = {issue["code"] for issue in bundle["issues"]}
        self.assertNotIn("FX_SERIES_GAPS", codes)
        self.assertEqual([call["currencies"] for call in self.requested], [["CAD"]])

    def test_price_history_older_than_the_fx_window_widens_the_request(self):
        config = self.prices_csv(
            [("RY.TO", self.EARLY, "150", "CAD"), ("RY.TO", "2026-09-10", "180", "CAD")]
        )
        bundle = self.load(config, [self.rate(self.EARLY, "0.7400"), self.rate("2026-09-10")])
        candidate = self.series(bundle, "RY.TO")
        self.assertEqual(len(candidate), 2)
        self.assertLessEqual(self.requested[0]["start"], self.EARLY)
        self.assertAlmostEqual(candidate["close"].tolist()[0], 150 * 0.74, places=9)
        self.assertNotIn("FX_SERIES_GAPS", {issue["code"] for issue in bundle["issues"]})

    def test_a_fully_presented_series_asks_no_provider(self):
        config = self.prices_csv([("DEMO", "2026-09-10", "200", "USD")])
        bundle = self.load(config, [])
        self.assertEqual(self.requested, [])
        self.assertEqual(len(self.series(bundle, "DEMO")), 1)
