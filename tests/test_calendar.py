import unittest

import pandas as pd

from portfolio_research.calendar import (
    CURRENT_METHOD_VERSION,
    METHOD_VERSION,
    bundle_cutoff,
    decision_context,
    information_cutoff,
    new_york_dates,
    review_context,
    settle_execution,
)

SUNDAY_GENERATED = "2026-09-13T21:00:00+00:00"
EXISTING_TIMELINE_KEYS = {
    "decision_date",
    "decision_cutoff",
    "market_close",
    "early_close",
    "earliest_execution_date",
    "earliest_execution_at",
    "generated_at",
    "execution_policy",
    "method_version",
    "calendar",
    "calendar_version",
    "requested_date",
}


class CalendarTests(unittest.TestCase):
    def test_month_end_weekend_and_next_session(self):
        context = decision_context(month="2026-05")
        self.assertEqual(context["decision_date"], "2026-05-29")
        self.assertEqual(context["earliest_execution_date"], "2026-06-01")

    def test_early_close_is_separate_from_information_cutoff(self):
        context = decision_context("2026-11-27")
        self.assertTrue(context["early_close"])
        self.assertEqual(context["market_close"], "2026-11-27T18:00:00+00:00")
        self.assertEqual(context["decision_cutoff"], "2026-11-27T21:00:00+00:00")
        self.assertEqual(context["earliest_execution_at"], "2026-11-30T21:00:00+00:00")

    def test_default_is_previous_month_and_dst_is_explicit(self):
        self.assertEqual(
            decision_context(generated_at="2026-09-13T18:00:00Z")["decision_date"], "2026-08-31"
        )
        self.assertEqual(information_cutoff("2026-08-31").hour, 20)
        self.assertEqual(information_cutoff("2026-01-30").hour, 21)


class CurrentReviewContextTests(unittest.TestCase):
    def test_sunday_generation_observes_friday_and_executes_monday(self):
        context = review_context("current", generated_at=SUNDAY_GENERATED)
        self.assertEqual(context["review_kind"], "current")
        self.assertEqual(context["method_version"], CURRENT_METHOD_VERSION)
        self.assertNotEqual(context["method_version"], METHOD_VERSION)
        self.assertEqual(context["calendar"], "XNYS")
        self.assertEqual(context["requested_date"], "2026-09-13")
        self.assertEqual(context["review_month"], "2026-09")
        self.assertEqual(context["market_observation_date"], "2026-09-11")
        self.assertEqual(context["decision_date"], "2026-09-11")
        self.assertEqual(context["market_close"], "2026-09-11T20:00:00+00:00")
        self.assertFalse(context["early_close"])
        self.assertEqual(context["information_cutoff"], SUNDAY_GENERATED)
        self.assertEqual(context["decision_cutoff"], SUNDAY_GENERATED)
        self.assertEqual(context["generated_at"], SUNDAY_GENERATED)
        self.assertEqual(context["earliest_execution_date"], "2026-09-14")
        self.assertEqual(context["earliest_execution_at"], "2026-09-14T20:00:00+00:00")
        self.assertEqual(context["execution_basis"], "after_generation")
        self.assertIsNone(context["collection_received_at"])
        self.assertIsNone(context["source_valuation_time"])
        self.assertTrue(EXISTING_TIMELINE_KEYS <= set(context))

    def test_friday_morning_observes_thursday_and_executes_at_friday_close(self):
        context = review_context("current", generated_at="2026-09-11T14:00:00+00:00")
        self.assertEqual(context["market_observation_date"], "2026-09-10")
        self.assertEqual(context["decision_date"], "2026-09-10")
        self.assertEqual(context["earliest_execution_date"], "2026-09-11")
        self.assertEqual(context["earliest_execution_at"], "2026-09-11T20:00:00+00:00")

    def test_friday_evening_observes_friday_and_executes_monday(self):
        context = review_context("current", generated_at="2026-09-11T21:00:00+00:00")
        self.assertEqual(context["market_observation_date"], "2026-09-11")
        self.assertEqual(context["earliest_execution_date"], "2026-09-14")
        self.assertEqual(context["earliest_execution_at"], "2026-09-14T20:00:00+00:00")

    def test_early_close_session_is_complete_after_its_close(self):
        context = review_context("current", generated_at="2026-11-27T19:00:00+00:00")
        self.assertEqual(context["market_observation_date"], "2026-11-27")
        self.assertTrue(context["early_close"])
        self.assertEqual(context["market_close"], "2026-11-27T18:00:00+00:00")
        self.assertEqual(context["information_cutoff"], "2026-11-27T19:00:00+00:00")
        self.assertEqual(context["earliest_execution_date"], "2026-11-30")
        self.assertEqual(context["earliest_execution_at"], "2026-11-30T21:00:00+00:00")

    def test_generation_and_receipt_stamps_are_normalized_to_utc(self):
        context = review_context(
            "current",
            generated_at="2026-09-13T17:00:00-04:00",
            collection_received_at="2026-09-13T16:00:00-04:00",
            source_valuation_time="2026-09-11T16:00:00-04:00",
        )
        self.assertEqual(context["generated_at"], SUNDAY_GENERATED)
        self.assertEqual(context["information_cutoff"], SUNDAY_GENERATED)
        self.assertEqual(context["collection_received_at"], "2026-09-13T20:00:00+00:00")
        self.assertEqual(context["source_valuation_time"], "2026-09-11T20:00:00+00:00")

    def test_current_reviews_reject_explicit_dates_unknown_kinds_and_naive_stamps(self):
        with self.assertRaises(ValueError):
            review_context("current", as_of="2026-08-31")
        with self.assertRaises(ValueError):
            review_context("current", month="2026-08")
        with self.assertRaises(ValueError):
            review_context("weekly")
        with self.assertRaises(ValueError):
            review_context("current", generated_at="2026-09-13T21:00:00")
        with self.assertRaises(ValueError):
            review_context(
                "current",
                generated_at=SUNDAY_GENERATED,
                collection_received_at="2026-09-13T20:00:00",
            )
        with self.assertRaises(ValueError):
            review_context(
                "current", generated_at=SUNDAY_GENERATED, source_valuation_time="2026-09-11"
            )


class HistoricalReviewContextTests(unittest.TestCase):
    def test_historical_context_extends_decision_context_without_changing_it(self):
        generated = "2026-09-13T18:00:00Z"
        base = decision_context(month="2026-08", generated_at=generated)
        context = review_context("historical", month="2026-08", generated_at=generated)
        for key, value in base.items():
            with self.subTest(key=key):
                self.assertEqual(context[key], value)
        self.assertEqual(context["review_kind"], "historical")
        self.assertEqual(context["historical_basis"], "reconstruction")
        self.assertEqual(context["information_cutoff"], context["decision_cutoff"])
        self.assertEqual(context["review_month"], "2026-08")
        self.assertEqual(context["market_observation_date"], base["decision_date"])
        self.assertEqual(context["execution_basis"], "after_decision_cutoff")
        self.assertIsNone(context["collection_received_at"])
        self.assertIsNone(context["source_valuation_time"])

    def test_historical_context_generated_before_execution_is_recorded(self):
        context = review_context(
            "historical", as_of="2026-09-11", generated_at="2026-09-11T21:00:00+00:00"
        )
        self.assertEqual(context["decision_date"], "2026-09-11")
        self.assertEqual(context["decision_cutoff"], "2026-09-11T20:00:00+00:00")
        self.assertEqual(context["earliest_execution_at"], "2026-09-14T20:00:00+00:00")
        self.assertEqual(context["historical_basis"], "recorded")

    def test_historical_context_keeps_decision_context_validation(self):
        with self.assertRaises(ValueError):
            review_context("historical", as_of="2026-08-31", month="2026-08")
        with self.assertRaises(ValueError):
            review_context("historical", as_of="2026-08-31T16:00:00")


class BundleCutoffTests(unittest.TestCase):
    def test_bundle_without_explicit_cutoff_matches_information_cutoff(self):
        self.assertEqual(bundle_cutoff({"as_of": "2026-08-31"}), information_cutoff("2026-08-31"))
        historical = {"as_of": "2026-08-31", "timeline": decision_context("2026-08-31")}
        self.assertEqual(bundle_cutoff(historical), information_cutoff("2026-08-31"))
        self.assertEqual(bundle_cutoff({"as_of": "2026-08-31", "timeline": None}).hour, 20)

    def test_explicit_timeline_cutoff_wins_and_must_be_aware(self):
        bundle = {"as_of": "2026-09-11", "timeline": {"information_cutoff": SUNDAY_GENERATED}}
        self.assertEqual(bundle_cutoff(bundle), pd.Timestamp(SUNDAY_GENERATED))
        self.assertEqual(str(bundle_cutoff(bundle).tzinfo), "UTC")
        offset = {
            "as_of": "2026-09-11",
            "timeline": {"information_cutoff": "2026-09-13T17:00:00-04:00"},
        }
        self.assertEqual(bundle_cutoff(offset), pd.Timestamp(SUNDAY_GENERATED))
        current = {
            "as_of": "2026-09-11",
            "timeline": review_context("current", generated_at=SUNDAY_GENERATED),
        }
        self.assertEqual(bundle_cutoff(current), pd.Timestamp(SUNDAY_GENERATED))
        with self.assertRaises(ValueError):
            bundle_cutoff(
                {"as_of": "2026-09-11", "timeline": {"information_cutoff": "2026-09-13T21:00:00"}}
            )


class SettleExecutionTests(unittest.TestCase):
    """A current review's execution must follow the moment its result exists."""

    MONDAY_BEFORE_CLOSE = "2026-09-14T19:59:00+00:00"

    def test_completion_before_the_first_close_keeps_the_timeline(self):
        timeline = review_context("current", generated_at=self.MONDAY_BEFORE_CLOSE)
        settled = settle_execution(timeline, "2026-09-14T19:59:59+00:00")
        self.assertEqual(settled, timeline)
        self.assertIsNot(settled, timeline)

    def test_completion_at_or_after_the_close_moves_execution_and_keeps_the_cutoff(self):
        timeline = review_context("current", generated_at=self.MONDAY_BEFORE_CLOSE)
        self.assertEqual(timeline["earliest_execution_at"], "2026-09-14T20:00:00+00:00")
        for completed in ("2026-09-14T20:00:00+00:00", "2026-09-14T16:03:00-04:00"):
            with self.subTest(completed=completed):
                settled = settle_execution(timeline, completed)
                self.assertEqual(settled["earliest_execution_date"], "2026-09-15")
                self.assertEqual(settled["earliest_execution_at"], "2026-09-15T20:00:00+00:00")
                self.assertEqual(
                    settled["generated_at"], pd.Timestamp(completed).tz_convert("UTC").isoformat()
                )
                self.assertEqual(settled["information_cutoff"], self.MONDAY_BEFORE_CLOSE)
                self.assertEqual(settled["decision_cutoff"], self.MONDAY_BEFORE_CLOSE)
                self.assertEqual(settled["decision_date"], timeline["decision_date"])
                self.assertEqual(set(settled), set(timeline))
        self.assertEqual(timeline["earliest_execution_at"], "2026-09-14T20:00:00+00:00")

    def test_friday_completion_after_close_executes_next_session(self):
        timeline = review_context("current", generated_at="2026-09-11T19:58:00+00:00")
        settled = settle_execution(timeline, "2026-09-11T20:05:00+00:00")
        self.assertEqual(settled["earliest_execution_date"], "2026-09-14")
        self.assertEqual(settled["earliest_execution_at"], "2026-09-14T20:00:00+00:00")

    def test_historical_timeline_is_unchanged_and_completion_must_be_aware(self):
        historical = review_context(
            "historical", as_of="2026-09-11", generated_at="2026-09-11T21:00:00+00:00"
        )
        self.assertEqual(settle_execution(historical, "2026-09-15T00:00:00+00:00"), historical)
        current = review_context("current", generated_at=self.MONDAY_BEFORE_CLOSE)
        with self.assertRaises(ValueError):
            settle_execution(current, "2026-09-14T20:03:00")


class NewYorkDatesTests(unittest.TestCase):
    def parse(self, values):
        raw = pd.Series(values)
        return new_york_dates(raw, pd.to_datetime(raw, errors="coerce", utc=True, format="mixed"))

    def test_bare_dates_start_their_new_york_day(self):
        parsed = self.parse(["2026-09-14", "2026-12-14", "2026-09-14T00:30:00Z", None, "bad"])
        self.assertEqual(parsed[0], pd.Timestamp("2026-09-14T04:00:00Z"))
        self.assertEqual(parsed[1], pd.Timestamp("2026-12-14T05:00:00Z"))
        self.assertEqual(parsed[2], pd.Timestamp("2026-09-14T00:30:00Z"))
        self.assertTrue(pd.isna(parsed[3]))
        self.assertTrue(pd.isna(parsed[4]))

    def test_sunday_evening_cutoff_excludes_monday_dates_at_any_utc_hour(self):
        for generated in ("2026-09-13T23:30:00+00:00", "2026-09-14T00:30:00+00:00"):
            with self.subTest(generated=generated):
                cutoff = pd.Timestamp(generated)
                included = self.parse(["2026-09-13", "2026-09-14"]) <= cutoff
                self.assertEqual(included.tolist(), [True, False])

    def test_historical_cutoffs_select_the_same_dates_under_either_reading(self):
        raw = pd.Series(["2026-09-10", "2026-09-11", "2026-09-12", "2026-11-27", "2026-11-28"])
        utc = pd.to_datetime(raw, utc=True, format="mixed")
        for as_of in ("2026-09-11", "2026-11-27"):
            with self.subTest(as_of=as_of):
                cutoff = information_cutoff(as_of)
                self.assertEqual(
                    (new_york_dates(raw, utc) <= cutoff).tolist(), (utc <= cutoff).tolist()
                )
