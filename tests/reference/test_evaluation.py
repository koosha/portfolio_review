import json
import unittest

import pandas as pd

from portfolio_lab.evaluation import evaluate_forecasts, purged_walk_forward_splits


class ForecastEvaluationTests(unittest.TestCase):
    def forecasts(self, **changes):
        return {
            "forecasts": pd.DataFrame(
                [
                    {
                        "security_id": "ABC",
                        "forecast_date": "2025-01-01",
                        "horizon_months": 12,
                        "scenario": "adverse",
                        "return_value": -0.20,
                        "probability": 0.25,
                        "basis": "subjective",
                        **changes,
                    },
                    {
                        "security_id": "ABC",
                        "forecast_date": "2025-01-01",
                        "horizon_months": 12,
                        "scenario": "central",
                        "return_value": 0.20,
                        "probability": 0.75,
                        "basis": "subjective",
                        **changes,
                    },
                ]
            )
        }

    def prices(self, *rows):
        rows = rows or [("2025-01-02", 100), ("2025-12-31", 110)]
        return pd.DataFrame(
            [
                {
                    "security_id": "ABC",
                    "date": row[0],
                    "adjusted_close": row[1],
                    "available_at": row[2] if len(row) > 2 else row[0] + "T22:00:00Z",
                }
                for row in rows
            ]
        )

    def test_matured_forecast_scores_weighted_scenario_prediction(self):
        result = evaluate_forecasts(self.forecasts(), self.prices(), "2026-01-01")
        row = result["results"][0]
        self.assertEqual(row["status"], "scored")
        self.assertEqual(row["target_date"], "2026-01-01")
        self.assertEqual(row["basis"], "subjective")
        self.assertAlmostEqual(row["predicted_return"], 0.10)
        self.assertAlmostEqual(row["realized_return"], 0.10)
        self.assertAlmostEqual(result["summary"]["mae"], 0)
        self.assertEqual(result["summary"]["matured_coverage"], 1)
        json.dumps(result, allow_nan=False)

    def test_full_horizon_must_mature_even_with_endpoint_price_present(self):
        result = evaluate_forecasts(self.forecasts(), self.prices(), "2025-12-31")
        self.assertEqual(result["results"][0]["status"], "immature")
        self.assertIsNone(result["results"][0]["realized_return"])
        self.assertEqual(result["summary"]["scored_count"], 0)

    def test_future_observation_and_publication_cannot_leak(self):
        prices = self.prices(("2025-01-02", 100), ("2026-01-02", 150))
        result = evaluate_forecasts(self.forecasts(), prices, "2026-01-01")
        self.assertEqual(result["results"][0]["status"], "missing_endpoint")
        self.assertTrue(any(x["code"] == "EVALUATION_PRICE_AFTER_CUTOFF" for x in result["issues"]))

    def test_delayed_publication_becomes_eligible_only_after_publication_date(self):
        prices = self.prices(("2025-01-02", 100), ("2025-12-31", 110, "2026-01-03T01:00:00Z"))
        self.assertEqual(
            evaluate_forecasts(self.forecasts(), prices, "2026-01-01")["results"][0]["status"],
            "missing_endpoint",
        )
        self.assertEqual(
            evaluate_forecasts(self.forecasts(), prices, "2026-01-03")["results"][0]["status"],
            "scored",
        )

    def test_stale_endpoint_and_missing_start_are_explicit(self):
        stale = evaluate_forecasts(
            self.forecasts(), self.prices(("2025-01-02", 100), ("2025-12-24", 110)), "2026-01-01"
        )
        self.assertEqual(stale["results"][0]["status"], "missing_endpoint")
        missing_start = evaluate_forecasts(
            self.forecasts(), self.prices(("2025-01-09", 100), ("2025-12-31", 110)), "2026-01-01"
        )
        self.assertTrue(
            any(
                x["code"] == "EVALUATION_START_MISSING"
                for x in missing_start["results"][0]["issues"]
            )
        )

    def test_endpoint_boundary_inclusive_and_correct_direction(self):
        prices = self.prices(
            ("2024-12-31", 200), ("2025-01-08", 100), ("2025-12-25", 120), ("2026-01-02", 999)
        )
        row = evaluate_forecasts(self.forecasts(), prices, "2026-01-03")["results"][0]
        self.assertEqual(row["start_date"], "2025-01-08")
        self.assertEqual(row["end_date"], "2025-12-25")
        self.assertAlmostEqual(row["realized_return"], 0.20)

    def test_only_original_scenarios_are_used_and_inputs_not_mutated(self):
        forecasts = self.forecasts()
        original = forecasts["forecasts"].copy(deep=True)
        prices = self.prices()
        old_prices = prices.copy(deep=True)
        evaluate_forecasts(forecasts, prices, "2026-01-01")
        pd.testing.assert_frame_equal(forecasts["forecasts"], original)
        pd.testing.assert_frame_equal(prices, old_prices)

    def test_missing_price_availability_never_assumed(self):
        prices = self.prices()
        prices.loc[1, "available_at"] = None
        self.assertEqual(
            evaluate_forecasts(self.forecasts(), prices, "2026-01-01")["results"][0]["status"],
            "missing_endpoint",
        )

    def test_probability_and_duplicate_scenario_fail_closed(self):
        for changes in [
            {"probability": 0.3},
            {"scenario": "duplicate"},
            {"return_value": float("nan")},
            {"basis": "guaranteed"},
        ]:
            with self.subTest(changes=changes):
                row = evaluate_forecasts(self.forecasts(**changes), self.prices(), "2026-01-01")[
                    "results"
                ][0]
                self.assertEqual(row["status"], "invalid_forecast")

    def test_calendar_month_horizons_and_group_summary(self):
        bundle = {
            "forecasts": pd.concat(
                [self.forecasts(horizon_months=h)["forecasts"] for h in (6, 12, 18)],
                ignore_index=True,
            )
        }
        prices = self.prices(("2025-01-02", 100), ("2025-07-01", 105), ("2025-12-31", 110))
        result = evaluate_forecasts(bundle, prices, "2026-01-01")
        self.assertEqual(result["summary"]["scored_count"], 2)
        self.assertEqual(result["summary"]["immature_count"], 1)
        self.assertEqual(len(result["by_horizon"]), 3)

    def test_conflicting_endpoint_same_publication_is_unavailable(self):
        prices = self.prices(("2025-01-02", 100), ("2025-12-31", 110), ("2025-12-31", 120))
        result = evaluate_forecasts(self.forecasts(), prices, "2026-01-01")
        self.assertEqual(result["results"][0]["status"], "missing_endpoint")

    def test_latest_revision_available_by_cutoff_wins(self):
        prices = self.prices(
            ("2025-01-02", 100), ("2025-12-31", 110), ("2025-12-31", 120, "2026-01-02T00:00:00Z")
        )
        before = evaluate_forecasts(self.forecasts(), prices, "2026-01-01")["results"][0]
        after = evaluate_forecasts(self.forecasts(), prices, "2026-01-02")["results"][0]
        self.assertAlmostEqual(before["realized_return"], 0.1)
        self.assertAlmostEqual(after["realized_return"], 0.2)


class PurgedSplitTests(unittest.TestCase):
    def test_overlapping_and_unmatured_training_labels_excluded(self):
        frame = pd.DataFrame(
            {
                "forecast_date": [
                    "2024-01-01",
                    "2024-07-01",
                    "2024-08-01",
                    "2025-01-01",
                    "2025-06-30",
                    "2025-07-01",
                ],
                "label_end": [
                    "2024-12-31",
                    "2025-01-01",
                    "2025-02-01",
                    "2026-01-01",
                    "2026-06-30",
                    "2026-07-01",
                ],
            },
            index=["old", "boundary", "overlap", "first", "last", "future"],
        )
        splits = purged_walk_forward_splits(frame, "2025-01-01", "2025-07-01")
        self.assertEqual(splits["training"], ["old"])
        self.assertEqual(splits["validation"], ["first", "last"])
        self.assertEqual(splits["excluded"], ["boundary", "overlap", "future"])

    def test_invalid_dates_and_reversed_labels_excluded(self):
        frame = pd.DataFrame(
            {
                "forecast_date": [None, "2024-01-01", "2024-01-01"],
                "label_end": ["2024-12-31", None, "2023-12-31"],
            }
        )
        split = purged_walk_forward_splits(frame, "2025-01-01", "2026-01-01")
        self.assertEqual(split, {"training": [], "validation": [], "excluded": [0, 1, 2]})

    def test_duplicate_indices_and_reversed_windows_rejected(self):
        frame = pd.DataFrame(
            {"forecast_date": ["2024-01-01"] * 2, "label_end": ["2024-12-31"] * 2}, index=[0, 0]
        )
        with self.assertRaises(ValueError):
            purged_walk_forward_splits(frame, "2025-01-01", "2026-01-01")
        with self.assertRaises(ValueError):
            purged_walk_forward_splits(frame.reset_index(drop=True), "2026-01-01", "2025-01-01")


if __name__ == "__main__":
    unittest.main()
