"""Adapter price series: session stamping, minor-unit quotes, isolation and the payload cache."""

import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from portfolio_research import market_data
from portfolio_research.market_data import price_history
from tests.support.market_data_adapter import AS_OF, AdapterCase, FakeTicker, refuse_network


class PriceHistoryTests(AdapterCase):
    def test_price_rows_are_stamped_with_the_exchange_close_of_an_xnys_session(self):
        result = self.call(price_history, "AAPL")
        self.assertEqual(result["security_id"], "AAPL")
        self.assertEqual([row["date"] for row in result["prices"]], ["2026-09-10", "2026-09-11"])
        row = result["prices"][1]
        self.assertEqual(row["available_at"], "2026-09-11T20:00:00+00:00")
        self.assertIsNotNone(datetime.fromisoformat(row["available_at"]).tzinfo)
        self.assertAlmostEqual(row["close"], 302.5)
        self.assertAlmostEqual(row["adjusted_close"], 302.5 * 0.99)
        self.assertEqual(row["volume"], 1_000_001)
        self.assertEqual(row["currency"], "USD")
        self.assertEqual(row["quote_unit_factor"], 1)
        self.assertEqual(row["adjustment"], "yahoo_adj_close")
        self.assertTrue(row["source_id"].startswith("yahoo_prices:"))
        self.assertEqual(row["source_id"], result["source_id"])
        self.assertIsNotNone(datetime.fromisoformat(row["received_at"]).tzinfo)
        self.assertEqual(self.issues, [])

    def test_a_date_that_is_not_an_xnys_session_is_available_at_the_new_york_day_end(self):
        result = self.call(price_history, "VOD.L")
        labour_day = result["prices"][0]
        self.assertEqual(labour_day["date"], "2026-09-07")
        self.assertEqual(labour_day["available_at"], "2026-09-07T23:59:59-04:00")

    def test_minor_unit_quotes_are_normalized_to_the_major_currency(self):
        result = self.call(price_history, "VOD.L")
        self.assertEqual(result["currency"], "GBP")
        self.assertEqual(result["quote_currency"], "GBp")
        self.assertEqual(result["quote_unit_factor"], 100)
        row = result["prices"][0]
        self.assertAlmostEqual(row["close"], 1.2875)
        self.assertAlmostEqual(row["adjusted_close"], 128.75 * 0.99 / 100)
        self.assertEqual(row["currency"], "GBP")
        self.assertEqual(row["quote_currency"], "GBp")
        self.assertEqual(row["quote_unit_factor"], 100)

    def test_dividend_and_split_actions_are_returned_in_major_currency(self):
        result = self.call(price_history, "VOD.L")
        kinds = {(row["kind"], row["date"]): row for row in result["actions"]}
        self.assertEqual(set(kinds), {("dividend", "2026-09-11"), ("split", "2026-09-11")})
        self.assertAlmostEqual(kinds[("dividend", "2026-09-11")]["value"], 0.025)
        self.assertEqual(kinds[("dividend", "2026-09-11")]["currency"], "GBP")
        self.assertAlmostEqual(kinds[("split", "2026-09-11")]["value"], 2.0)
        self.assertIsNone(kinds[("split", "2026-09-11")]["currency"])

    def test_a_non_positive_split_value_is_rejected_instead_of_stored(self):
        """A negative ratio is not a split; storing it would raise inside a pure helper."""
        result = self.call(price_history, "NEGSPLIT")
        self.assertEqual([row["kind"] for row in result["actions"]], ["dividend"])
        self.assertEqual([issue["code"] for issue in self.issues], ["PROVIDER_VALUE_REJECTED"])
        self.assertEqual(self.issues[0]["security_id"], "NEGSPLIT")
        self.assertEqual(len(result["prices"]), 2)

    def test_one_failing_ticker_is_isolated_and_leaks_no_provider_url(self):
        failed = self.call(price_history, "BROKEN")
        self.assertIsNone(failed)
        self.assertEqual([issue["code"] for issue in self.issues], ["PROVIDER_FETCH_FAILED"])
        self.assertEqual(self.issues[0]["security_id"], "BROKEN")
        self.assertEqual(self.issues[0]["detail"], "RuntimeError")
        self.assertNotIn("token", str(self.issues))
        self.assertIsNotNone(self.call(price_history, "AAPL"))
        self.assertEqual(len(self.issues), 1)

    def test_a_provider_call_slower_than_the_timeout_fails_fast(self):
        self.config["data"]["provider_timeout_seconds"] = 0.2
        started = time.monotonic()
        self.assertIsNone(self.call(price_history, "SLEEPY"))
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertEqual([issue["code"] for issue in self.issues], ["PROVIDER_TIMEOUT"])
        self.assertEqual(self.issues[0]["security_id"], "SLEEPY")

    def test_a_timed_out_provider_call_abandons_only_a_daemon_thread(self):
        """Fails fast for the process too: no worker the interpreter joins at exit."""
        from concurrent.futures import thread as futures_thread

        self.config["data"]["provider_timeout_seconds"] = 0.2
        FakeTicker.sleep_seconds = 3.0
        self.addCleanup(setattr, FakeTicker, "sleep_seconds", 0.5)
        pooled = len(futures_thread._threads_queues)
        before = set(threading.enumerate())
        self.assertIsNone(self.call(price_history, "SLEEPY"))
        self.assertEqual(len(futures_thread._threads_queues), pooled)
        abandoned = [worker for worker in threading.enumerate() if worker not in before]
        self.assertTrue(abandoned)
        self.assertTrue(all(worker.daemon for worker in abandoned))


class CacheTests(AdapterCase):
    def test_a_fresh_cached_payload_is_reused_even_when_refresh_is_requested(self):
        first = self.call(price_history, "AAPL")
        calls = len(FakeTicker.calls)
        second = self.call(price_history, "AAPL")
        self.assertEqual(len(FakeTicker.calls), calls)
        self.assertEqual(first["prices"], second["prices"])
        self.assertEqual(self.issues, [])

    def test_force_refetches_inside_the_refresh_window(self):
        self.call(price_history, "AAPL")
        calls = len(FakeTicker.calls)
        self.call(price_history, "AAPL", force=True)
        self.assertGreater(len(FakeTicker.calls), calls)

    def test_a_payload_older_than_the_refresh_window_is_refetched(self):
        self.call(price_history, "AAPL")
        calls = len(FakeTicker.calls)
        later = datetime.now(timezone.utc) + timedelta(hours=30)
        with patch.object(market_data, "_now", lambda: later.isoformat()):
            self.call(price_history, "AAPL")
        self.assertGreater(len(FakeTicker.calls), calls)

    def test_without_refresh_the_cache_answers_and_a_miss_is_an_issue(self):
        self.call(price_history, "AAPL")
        with patch("yfinance.Ticker", refuse_network):
            cached = price_history(
                self.config,
                self.security("AAPL"),
                refresh=False,
                issues=self.issues,
                as_of=AS_OF,
            )
            missing = price_history(
                self.config,
                self.security("MSFT"),
                refresh=False,
                issues=self.issues,
                as_of=AS_OF,
            )
        self.assertEqual(len(cached["prices"]), 2)
        self.assertIsNone(missing)
        self.assertEqual([issue["code"] for issue in self.issues], ["PROVIDER_CACHE_MISSING"])


if __name__ == "__main__":
    unittest.main()
