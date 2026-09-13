import unittest

from portfolio_research.calendar import decision_context, information_cutoff


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
