import copy
import unittest
from decimal import Decimal

import pandas as pd

from portfolio_research.calendar import decision_context, review_context
from portfolio_research.prospective import (
    baseline_records,
    evaluate_comparators,
    evaluate_ledger,
    evaluate_saved_forecasts,
)


def baseline(arm="no_discretionary_change"):
    return {
        "arm": arm,
        "status": "baseline_only",
        "valuation_date": "2026-09-03",
        "timeline": decision_context("2026-09-03", generated_at="2026-09-03T21:00:00Z"),
        "accounts": [
            {
                "account_id": "account-a",
                "currency": "USD",
                "cash": "10",
                "total_value": "110",
                "complete": True,
                "valuation_date": "2026-09-03",
            }
        ],
        "positions": [
            {
                "account_id": "account-a",
                "security_id": "DEMO",
                "quantity": "10",
                "market_value": "100",
                "currency": "USD",
            }
        ],
    }


def event(kind, when="2026-09-04", **values):
    return {
        "id": "event-1",
        "date": when,
        "account_id": "account-a",
        "currency": "USD",
        "kind": kind,
        **values,
    }


def end_prices(price="12", **changes):
    return [
        {"security_id": "DEMO", "currency": "USD", "date": "2026-09-30", "price": price, **changes}
    ]


class ExactForecastTests(unittest.TestCase):
    def bundle(self):
        return {
            "as_of": "2026-09-03",
            "mode": "offline",
            "timeline": decision_context("2026-09-03", generated_at="2026-09-03T21:00:00Z"),
            "forecasts": pd.DataFrame(
                [
                    {
                        "security_id": "DEMO",
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
            ),
        }

    def prices(self):
        return pd.DataFrame(
            [
                {
                    "security_id": "DEMO",
                    "date": day,
                    "adjusted_close": price,
                    "available_at": day + "T20:01:00Z",
                    "received_at": day + "T20:02:00Z",
                }
                for day, price in [("2026-09-04", 100), ("2027-09-03", 110), ("2027-09-07", 120)]
            ]
        )

    def test_weekend_and_holiday_horizon_scores_exact_next_session(self):
        bundle = self.bundle()
        original = bundle["forecasts"].copy(deep=True)
        result = evaluate_saved_forecasts(bundle, self.prices(), "2027-09-07")
        row = result["results"][0]
        self.assertEqual(row["status"], "scored")
        self.assertEqual(row["start_date"], "2026-09-04")
        self.assertEqual(row["required_end_date"], "2027-09-07")
        self.assertEqual(row["end_date"], "2027-09-07")
        self.assertAlmostEqual(row["realized_return"], 0.2)
        pd.testing.assert_frame_equal(bundle["forecasts"], original)

    def test_full_endpoint_close_and_actual_receipt_are_required(self):
        prices = self.prices()
        for cutoff in ("2027-09-06", "2027-09-07T19:59:00Z"):
            result = evaluate_saved_forecasts(self.bundle(), prices, cutoff)
            self.assertEqual(result["results"][0]["status"], "immature")
        prices.loc[prices.date == "2027-09-07", "received_at"] = "2027-09-08T01:00:00Z"
        result = evaluate_saved_forecasts(self.bundle(), prices, "2027-09-07")
        self.assertEqual(result["results"][0]["status"], "missing_endpoint")
        later = evaluate_saved_forecasts(self.bundle(), prices, "2027-09-08")
        self.assertEqual(later["results"][0]["status"], "scored")

    def test_missing_receipt_and_impossible_preclose_publication_cannot_score(self):
        for prices in (
            self.prices().drop(columns="received_at"),
            self.prices().assign(available_at=lambda x: x.date + "T12:00:00Z"),
        ):
            result = evaluate_saved_forecasts(self.bundle(), prices, "2027-09-08")
            self.assertEqual(result["summary"]["scored_count"], 0)
        prices = self.prices().assign(received_at=lambda x: x.date)
        self.assertEqual(
            evaluate_saved_forecasts(self.bundle(), prices, "2027-09-08")["summary"][
                "scored_count"
            ],
            0,
        )

    def test_conflicting_endpoint_and_missing_delisted_price_remain_unscored(self):
        prices = self.prices()
        conflict = prices.iloc[-1].copy()
        conflict["adjusted_close"] = 130
        conflicting = pd.concat([prices, pd.DataFrame([conflict])], ignore_index=True)
        self.assertEqual(
            evaluate_saved_forecasts(self.bundle(), conflicting, "2027-09-08")["summary"][
                "scored_count"
            ],
            0,
        )
        absent = prices[prices.date != "2027-09-07"]
        self.assertEqual(
            evaluate_saved_forecasts(self.bundle(), absent, "2027-09-08")["summary"][
                "forecast_count"
            ],
            1,
        )
        prices.loc[prices.date == "2027-09-07", "adjusted_close"] = 0
        self.assertEqual(
            evaluate_saved_forecasts(self.bundle(), prices, "2027-09-08")["summary"][
                "scored_count"
            ],
            0,
        )
        prices["worthless_confirmed"] = True
        self.assertEqual(
            evaluate_saved_forecasts(self.bundle(), prices, "2027-09-08")["results"][0][
                "realized_return"
            ],
            -1,
        )

    def test_original_forecast_cutoff_and_reconstruction_label(self):
        bundle = self.bundle()
        bundle["forecasts"]["forecast_date"] = "2026-09-03T20:01:00Z"
        result = evaluate_saved_forecasts(bundle, self.prices(), "2027-09-08")
        self.assertEqual(result["results"][0]["status"], "invalid_forecast")
        bundle = self.bundle()
        bundle["timeline"]["generated_at"] = "2027-09-08T00:00:00Z"
        result = evaluate_saved_forecasts(bundle, self.prices(), "2027-09-08")
        self.assertFalse(result["prospective_eligible"])
        self.assertIn("reconstruction", result["provenance"])

    def test_bare_forecast_date_after_a_current_cutoff_is_not_frozen_before_it(self):
        """Sunday 20:30 New York: a Monday-dated forecast was not made before the cutoff."""
        codes, statuses = {}, {}
        for day in ("2026-09-13", "2026-09-14"):
            bundle = self.bundle()
            bundle["as_of"] = "2026-09-11"
            bundle["timeline"] = review_context("current", generated_at="2026-09-14T00:30:00Z")
            bundle["forecasts"]["forecast_date"] = day
            result = evaluate_saved_forecasts(bundle, self.prices(), "2027-09-20")
            [row] = result["results"]
            codes[day] = {issue["code"] for issue in row["issues"]}
            statuses[day] = row["status"]
        self.assertNotEqual(statuses["2026-09-13"], "invalid_forecast")
        self.assertNotIn("FORECAST_AFTER_DECISION", codes["2026-09-13"])
        self.assertEqual(statuses["2026-09-14"], "invalid_forecast")
        self.assertIn("FORECAST_AFTER_DECISION", codes["2026-09-14"])

    def test_invalid_probabilities_stay_invalid_after_endpoint_matching(self):
        bundle = self.bundle()
        bundle["forecasts"]["probability"] = 0.6
        result = evaluate_saved_forecasts(bundle, self.prices(), "2027-09-08")
        self.assertEqual(result["results"][0]["status"], "invalid_forecast")
        self.assertIsNone(result["results"][0]["realized_return"])

    def test_saved_invalid_joint_forecasts_and_synthetic_mode_do_not_certify_prospective_evidence(
        self,
    ):
        bundle = self.bundle()
        bundle["forecasts"]["joint_validation_status"] = "invalid"
        result = evaluate_saved_forecasts(bundle, self.prices(), "2027-09-08")
        self.assertEqual(result["results"][0]["status"], "invalid_forecast")
        self.assertFalse(result["prospective_eligible"])
        bundle = self.bundle()
        bundle["mode"] = "demo"
        result = evaluate_saved_forecasts(bundle, self.prices(), "2027-09-08")
        self.assertFalse(result["prospective_eligible"])
        self.assertIn("synthetic", result["provenance"])


class DecimalLedgerTests(unittest.TestCase):
    def evaluate(self, b=None, events=None, prices=None, **options):
        return evaluate_ledger(
            b or baseline(),
            events or [],
            end_prices() if prices is None else prices,
            end_date="2026-09-30",
            coverage_confirmed=True,
            **options,
        )

    def test_explicit_distributions_splits_costs_and_flows_reconcile(self):
        events = [
            event("external_flow", amount="20"),
            event("split", "2026-09-05", id="split", security_id="DEMO", ratio="2"),
            event("distribution", "2026-09-06", id="dividend", security_id="DEMO", per_share="0.5"),
            event("fee", "2026-09-07", id="fee", amount="1"),
            event("tax_reserve", "2026-09-08", id="tax", amount="2"),
        ]
        result = self.evaluate(events=events, prices=end_prices("6"))
        self.assertEqual(Decimal(result["ending_nav"]), Decimal("157"))
        self.assertEqual(Decimal(result["investment_gain"]), Decimal("27"))
        self.assertEqual(result["costs"], "1")
        self.assertEqual(result["tax_reserve"], "2")
        self.assertIsNone(result["after_tax_return"])

    def test_events_cannot_precede_baseline_or_exceed_interval(self):
        for day in ("2026-09-02", "2026-09-03", "2026-10-01"):
            with self.subTest(day=day), self.assertRaises(ValueError):
                self.evaluate(events=[event("external_flow", day, amount="20")])

    def test_mixed_unknown_currency_and_duplicate_identities_rejected(self):
        for alteration in (
            "mixed",
            "unknown",
            "duplicate_account",
            "duplicate_position",
            "wrong_position_currency",
        ):
            b = baseline()
            if alteration == "mixed":
                b["accounts"].append(
                    {
                        **b["accounts"][0],
                        "account_id": "account-b",
                        "currency": "EUR",
                        "total_value": "10",
                    }
                )
            elif alteration == "unknown":
                b["accounts"][0]["currency"] = None
                b["positions"][0]["currency"] = None
            elif alteration == "duplicate_account":
                b["accounts"].append(copy.deepcopy(b["accounts"][0]))
            elif alteration == "duplicate_position":
                b["positions"].append(copy.deepcopy(b["positions"][0]))
            else:
                b["positions"][0]["currency"] = "EUR"
            with self.subTest(alteration=alteration), self.assertRaises(ValueError):
                self.evaluate(b)
        with self.assertRaises(ValueError):
            self.evaluate(prices=end_prices() + end_prices("14"))

    def test_missing_values_unknown_cash_and_delisting_not_assumed_zero(self):
        self.assertEqual(self.evaluate(prices=[])["status"], "blocked")
        with self.assertRaises(ValueError):
            self.evaluate(prices=end_prices("0"))
        confirmed = self.evaluate(prices=end_prices("0", worthless_confirmed=True))
        self.assertEqual(confirmed["investment_gain"], "-100")
        b = baseline()
        b["accounts"][0]["cash"] = None
        with self.assertRaises(ValueError):
            self.evaluate(b)

    def test_decimal_precision_is_not_limited_to_default_context(self):
        b = baseline()
        b["accounts"][0].update(
            cash="0.03", total_value="10000000000000000000000000000000000000000.04"
        )
        b["positions"][0].update(
            quantity="1", market_value="10000000000000000000000000000000000000000.01"
        )
        result = self.evaluate(b, prices=end_prices("10000000000000000000000000000000000000000.02"))
        self.assertEqual(result["investment_gain"], "0.01")
        self.assertEqual(result["ending_nav"], "10000000000000000000000000000000000000000.05")

    def test_duplicate_and_ambiguous_same_day_events_rejected(self):
        with self.assertRaises(ValueError):
            self.evaluate(events=[event("fee", amount="1"), event("fee", amount="1")])
        with self.assertRaises(ValueError):
            self.evaluate(events=[event("fee", amount="1"), event("fee", id="other", amount="1")])
        result = self.evaluate(
            events=[event("fee", amount="1"), event("fee", id="other", amount="1", sequence=1)]
        )
        self.assertEqual(result["costs"], "2")

    def test_trades_cannot_borrow_or_precede_eligible_execution(self):
        b = baseline("actual_decision")
        trade = event(
            "trade",
            security_id="DEMO",
            quantity="1",
            price="10",
            cost="0",
            tax_reserve="0",
            executed_at="2026-09-04T20:00:00Z",
        )
        self.assertEqual(self.evaluate(b, [trade])["status"], "evaluated")
        for changes in ({"quantity": "2"}, {"executed_at": "2026-09-04T12:00:00Z"}, {"cost": None}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.evaluate(b, [{**trade, **changes}])
        with self.assertRaises(ValueError):
            self.evaluate(events=[trade])

    def test_generated_policy_is_not_invested_without_reviewed_matching_executions(self):
        b = baseline("model_policy")
        b.update(
            status="requires_execution",
            candidate="model-candidate",
            proposed_basket=[
                {
                    "account_id": "account-a",
                    "security_id": "DEMO",
                    "decimal_amounts": {"quantity_change": "1"},
                }
            ],
        )
        self.assertEqual(self.evaluate(b)["status"], "blocked")
        self.assertEqual(self.evaluate(b, execution_confirmed=True)["status"], "blocked")
        trade = event(
            "trade",
            security_id="DEMO",
            quantity="1",
            price="10",
            cost="0",
            tax_reserve="0",
            executed_at="2026-09-04T20:00:00Z",
            policy_candidate="model-candidate",
        )
        self.assertEqual(self.evaluate(b, [trade], execution_confirmed=True)["status"], "evaluated")
        trade["quantity"] = "0.5"
        self.assertEqual(self.evaluate(b, [trade], execution_confirmed=True)["status"], "blocked")

    def test_comparators_require_identical_external_flows_and_scope(self):
        arms = [baseline(), baseline("actual_decision")]
        same = {arm["arm"]: [event("external_flow", amount="5")] for arm in arms}
        result = evaluate_comparators(
            arms, same, end_prices(), end_date="2026-09-30", coverage_confirmed=True
        )
        self.assertEqual(result["status"], "evaluated")
        self.assertEqual(
            Decimal(result["results"][1]["investment_gain_difference_vs_no_change"]), 0
        )
        different = copy.deepcopy(same)
        different["actual_decision"][0]["amount"] = "6"
        with self.assertRaises(ValueError):
            evaluate_comparators(
                arms, different, end_prices(), end_date="2026-09-30", coverage_confirmed=True
            )
        arms[1]["accounts"][0]["cash"] = "11"
        with self.assertRaises(ValueError):
            evaluate_comparators(
                arms, same, end_prices(), end_date="2026-09-30", coverage_confirmed=True
            )

    def test_baseline_freezes_exact_adapter_ledger_and_policy_status(self):
        b = baseline()
        b["positions"][0]["quantity"] = "10.000000000000000001"
        bundle = {
            "positions": pd.DataFrame(b["positions"]).assign(quantity=10.0),
            "accounts": pd.DataFrame(b["accounts"]),
            "ledger": {"positions": b["positions"], "accounts": b["accounts"]},
            "valuation_date": b["valuation_date"],
            "timeline": b["timeline"],
        }
        result = {
            "summary": {"complete": True},
            "run_id": "synthetic-run",
            "allocation": {
                "candidates": [
                    {
                        "candidate": "approved_benchmark_new_flows",
                        "status": "review_ready",
                        "proposals": [],
                    }
                ]
            },
        }
        records = baseline_records(result, {"allocation": {}}, bundle)
        self.assertEqual(records[0]["positions"][0]["quantity"], "10.000000000000000001")
        benchmark = next(row for row in records if row["arm"] == "approved_benchmark")
        self.assertEqual(benchmark["candidate"], "approved_benchmark_new_flows")
        self.assertEqual(benchmark["status"], "requires_execution")
        self.assertEqual(self.evaluate(benchmark)["status"], "blocked")
