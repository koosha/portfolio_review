"""FX providers: Bank of Canada Valet and Yahoo FX behind the shared provider cache.

No network: ``portfolio_lab.providers._fetch_json`` and ``yfinance.Ticker`` are patched.
"""

import json
import tempfile
import unittest
from decimal import Decimal, localcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError

import pandas as pd

from portfolio_lab import providers
from portfolio_lab.config import DEFAULTS, validate_config
from portfolio_research.fx import FxTable, validate_observation
from portfolio_research.fx_providers import (
    bank_of_canada_observations,
    load_fx_table,
    yahoo_fx_observations,
)

RECEIVED = "2026-11-03T21:00:00+00:00"

# Verified Valet shape: seriesDetail + observations keyed by "d"; holidays are absent
# (Labour Day 2026-09-07). 2026-11-02 falls after the end of daylight saving time.
VALET = {
    "terms": {"url": "https://www.bankofcanada.ca/terms/"},
    "seriesDetail": {
        "FXGBPCAD": {
            "label": "GBP/CAD",
            "description": "UK pound sterling to Canadian dollar daily exchange rate",
            "dimension": {"key": "d", "name": "date"},
        },
        "FXUSDCAD": {
            "label": "USD/CAD",
            "description": "US dollar to Canadian dollar daily exchange rate",
            "dimension": {"key": "d", "name": "date"},
        },
    },
    "observations": [
        {"d": "2026-09-04", "FXUSDCAD": {"v": "1.3840"}, "FXGBPCAD": {"v": "1.8200"}},
        {"d": "2026-09-08", "FXUSDCAD": {"v": "1.3868"}, "FXGBPCAD": {"v": "1.8250"}},
        {"d": "2026-11-02", "FXUSDCAD": {"v": "1.4000"}},
    ],
}


def divide(numerator, denominator):
    with localcontext() as context:
        context.prec = 34
        return format(Decimal(numerator) / Decimal(denominator), "f")


def make_config(tmp_path, **data):
    return validate_config(
        {"research": {"path": str(tmp_path / "research.sqlite")}, "data": {"mode": "live", **data}}
    )


class _Case(unittest.TestCase):
    def setUp(self):
        self.tmp_path = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.config = make_config(self.tmp_path)
        self.urls = []

    def fake_fetch(self, payload=VALET):
        def fake(url, user_agent=None):
            self.urls.append((url, user_agent))
            return json.loads(json.dumps(payload)), RECEIVED

        return fake

    def patch_fetch(self, fake):
        self.enterContext(patch.object(providers, "_fetch_json", fake))

    def forbid_fetch(self):
        def fail(*args, **kwargs):
            raise AssertionError("network invoked")

        self.patch_fetch(fail)


class BankOfCanadaTests(_Case):
    def fetch(self, currencies=("GBP", "CAD", "USD"), *, refresh=True, issues=None):
        return bank_of_canada_observations(
            self.config,
            list(currencies),
            "2026-09-01",
            "2026-11-03",
            refresh=refresh,
            issues=[] if issues is None else issues,
        )

    def test_one_request_for_all_series_with_quote_per_base_direction(self):
        self.patch_fetch(self.fake_fetch())
        issues = []
        records = self.fetch(issues=issues)
        self.assertEqual(issues, [])
        self.assertEqual(len(self.urls), 1)
        url, user_agent = self.urls[0]
        self.assertTrue(url.startswith("https://www.bankofcanada.ca/valet/observations/"))
        self.assertIn("FXGBPCAD", url)
        self.assertIn("FXUSDCAD", url)
        self.assertNotIn("FXCADCAD", url)
        self.assertIn("start_date=2026-09-01", url)
        self.assertIn("end_date=2026-11-03", url)
        self.assertTrue(user_agent)
        usd = {x["date"]: x for x in records if x["pair"] == "USDCAD"}
        self.assertEqual(sorted(usd), ["2026-09-04", "2026-09-08", "2026-11-02"])
        first = usd["2026-09-04"]
        self.assertEqual((first["base"], first["quote"], first["rate"]), ("USD", "CAD", "1.3840"))
        self.assertEqual(first["provider"], "bank_of_canada")
        self.assertEqual(first["received_at"], RECEIVED)
        self.assertTrue(first["source_id"].startswith("bank_of_canada_fx:"))
        self.assertNotIn("derived_via", first)
        gbp = {x["date"]: x for x in records if x["pair"] == "GBPCAD"}
        self.assertEqual(sorted(gbp), ["2026-09-04", "2026-09-08"])
        self.assertEqual(gbp["2026-09-08"]["rate"], "1.8250")
        self.assertFalse(any(x["date"] == "2026-09-07" for x in records))
        for record in records:
            self.assertEqual(validate_observation(record), record)

    def test_published_at_uses_toronto_offset_for_each_date(self):
        self.patch_fetch(self.fake_fetch())
        records = self.fetch()
        usd = {x["date"]: x for x in records if x["pair"] == "USDCAD"}
        self.assertEqual(usd["2026-09-04"]["published_at"], "2026-09-04T16:30:00-04:00")
        self.assertEqual(usd["2026-11-02"]["published_at"], "2026-11-02T16:30:00-05:00")

    def test_derived_usd_cross_divides_through_cad(self):
        self.patch_fetch(self.fake_fetch())
        records = self.fetch()
        crosses = {x["date"]: x for x in records if x["pair"] == "GBPUSD"}
        self.assertEqual(sorted(crosses), ["2026-09-04", "2026-09-08"])
        cross = crosses["2026-09-08"]
        self.assertEqual(cross["rate"], divide("1.8250", "1.3868"))
        self.assertEqual((cross["base"], cross["quote"]), ("GBP", "USD"))
        self.assertEqual(cross["derived_via"], "CAD")
        self.assertEqual(cross["provider"], "bank_of_canada")
        self.assertIn("FXGBPCAD", cross["source_id"])
        self.assertIn("FXUSDCAD", cross["source_id"])
        self.assertEqual(cross["published_at"], "2026-09-08T16:30:00-04:00")
        self.assertFalse(any(x["pair"] in {"USDUSD", "CADUSD"} for x in records))
        table = FxTable(records)
        converted = table.convert("100", "GBP", "USD", "2026-09-09")
        self.assertEqual(converted["status"], "converted")
        self.assertEqual(converted["observation_date"], "2026-09-08")

    def test_archive_records_direction_and_publication(self):
        self.patch_fetch(self.fake_fetch())
        self.fetch()
        sidecars = list((self.tmp_path / "cache" / "bank_of_canada_fx").glob("*.source.json"))
        self.assertEqual(len(sidecars), 1)
        source = json.loads(sidecars[0].read_text())
        self.assertEqual(source["direction"], "CAD per 1 unit of base")
        self.assertEqual(source["published_by"], "16:30 America/Toronto business days")
        self.assertEqual(source["key"], "2026-09-01_2026-11-03_FXGBPCAD-FXUSDCAD")

    def test_http_failure_returns_nothing_and_never_leaks_the_url(self):
        url = "https://www.bankofcanada.ca/valet/observations/FXUSDCAD/json?start_date=2026-09-01"
        failures = [
            HTTPError(url, 503, "Service Unavailable " + url, {}, None),
            RuntimeError("boom " + url),
        ]
        for exc in failures:

            def fail(*args, exc=exc, **kwargs):
                raise exc

            with patch.object(providers, "_fetch_json", fail):
                issues = []
                self.assertEqual(self.fetch(issues=issues), [])
            self.assertEqual([x["code"] for x in issues], ["FX_PROVIDER_FAILED"])
            self.assertEqual(issues[0]["provider"], "bank_of_canada")
            self.assertEqual(
                issues[0]["detail"], "HTTP 503" if isinstance(exc, HTTPError) else "RuntimeError"
            )
            text = json.dumps(issues)
            for fragment in ("bankofcanada", "valet", "start_date", "?"):
                self.assertNotIn(fragment, text)

    def test_malformed_payload_is_a_provider_failure(self):
        for payload in [{"observations": "nope"}, {"observations": [{"d": "2026-99-01"}]}, []]:
            with patch.object(providers, "_fetch_json", lambda *a, p=payload, **k: (p, RECEIVED)):
                issues = []
                self.assertEqual(self.fetch(issues=issues), [])
            self.assertEqual([x["code"] for x in issues], ["FX_PROVIDER_FAILED"])

    def test_cache_round_trip_without_network(self):
        self.patch_fetch(self.fake_fetch())
        live = self.fetch()
        self.forbid_fetch()
        issues = []
        cached = self.fetch(refresh=False, issues=issues)
        self.assertEqual(issues, [])
        self.assertEqual(cached, live)
        # A newly held currency changes the series set; the cached ones are still read.
        added = []
        widened = self.fetch(currencies=["EUR"], refresh=False, issues=added)
        self.assertEqual(added, [])
        self.assertTrue(any(record["pair"] == "USDCAD" for record in widened))
        self.assertFalse(any(record["base"] == "EUR" for record in widened))
        empty = []
        self.assertEqual(
            bank_of_canada_observations(
                self.config, ["USD"], "2019-01-01", "2019-03-01", refresh=False, issues=empty
            ),
            [],
        )
        self.assertEqual([x["code"] for x in empty], ["FX_PROVIDER_FAILED"])

    def test_invalid_arguments_are_rejected(self):
        self.forbid_fetch()
        with self.assertRaises(ValueError):
            bank_of_canada_observations(
                self.config, ["GBP"], "2026/09/01", "2026-09-30", refresh=True, issues=[]
            )
        with self.assertRaises(ValueError):
            bank_of_canada_observations(
                self.config, ["gbp"], "2026-09-01", "2026-09-30", refresh=True, issues=[]
            )


def fx_history(closes, index_dates, tz="Europe/London"):
    return pd.DataFrame(
        {"Open": closes, "Close": closes},
        index=pd.DatetimeIndex(index_dates, tz=tz, name="Date"),
    )


class YahooFxTests(_Case):
    def patch_yfinance(self, histories):
        self.calls = []

        def ticker(symbol):
            def history(**kwargs):
                self.calls.append((symbol, kwargs))
                value = histories[symbol]
                if isinstance(value, Exception):
                    raise value
                return value

            return SimpleNamespace(history=history)

        module = SimpleNamespace(Ticker=ticker)
        self.enterContext(patch.dict("sys.modules", {"yfinance": module}))

    def test_history_closes_become_dated_observations(self):
        self.patch_yfinance(
            {
                "CADUSD=X": fx_history(
                    [float("nan"), 0.7205, 0.7211], ["2026-09-09", "2026-09-10", "2026-09-11"]
                ),
                "GBPUSD=X": RuntimeError("no data https://query1.finance.yahoo.com/?crumb=x"),
            }
        )
        issues = []
        records = yahoo_fx_observations(
            self.config,
            [("CAD", "USD"), ("GBP", "USD")],
            "2026-09-01",
            "2026-09-11",
            refresh=True,
            issues=issues,
        )
        symbol, kwargs = self.calls[0]
        self.assertEqual(symbol, "CADUSD=X")
        self.assertEqual(kwargs["start"], "2026-09-01")
        self.assertEqual(kwargs["end"], "2026-09-12")
        self.assertIs(kwargs["auto_adjust"], False)
        # The London-midnight index keeps its own calendar date (no UTC shift).
        self.assertEqual([x["date"] for x in records], ["2026-09-10", "2026-09-11"])
        last = records[-1]
        self.assertEqual((last["pair"], last["base"], last["quote"]), ("CADUSD", "CAD", "USD"))
        self.assertEqual(last["rate"], str(Decimal(str(0.7211))))
        self.assertEqual(last["provider"], "yahoo")
        self.assertIsNone(last["published_at"])
        self.assertTrue(last["source_id"].startswith("yahoo_fx:"))
        self.assertTrue(pd.Timestamp(last["received_at"]).tzinfo)
        for record in records:
            self.assertEqual(validate_observation(record), record)
        self.assertEqual([x["code"] for x in issues], ["FX_PROVIDER_FAILED"])
        self.assertEqual(issues[0]["provider"], "yahoo")
        self.assertEqual(issues[0]["pair"], "GBPUSD")
        self.assertNotIn("crumb", json.dumps(issues))
        self.patch_yfinance({})
        cached_issues = []
        cached = yahoo_fx_observations(
            self.config,
            [("CAD", "USD")],
            "2026-09-01",
            "2026-09-11",
            refresh=False,
            issues=cached_issues,
        )
        self.assertEqual(self.calls, [])
        self.assertEqual(cached_issues, [])
        self.assertEqual(cached, records)

    def test_missing_dependency_is_reported(self):
        self.enterContext(patch.dict("sys.modules", {"yfinance": None}))
        issues = []
        records = yahoo_fx_observations(
            self.config, [("CAD", "USD")], "2026-09-01", "2026-09-11", refresh=True, issues=issues
        )
        self.assertEqual(records, [])
        self.assertEqual([x["code"] for x in issues], ["YAHOO_DEPENDENCY_MISSING"])

    def test_invalid_pair_is_rejected(self):
        with self.assertRaises(ValueError):
            yahoo_fx_observations(
                self.config, [("cad", "USD")], "2026-09-01", "2026-09-11", refresh=False, issues=[]
            )


class LoadFxTableTests(_Case):
    def test_collects_providers_in_order_into_a_table(self):
        self.config = make_config(self.tmp_path, max_fx_age_days=3)
        self.patch_fetch(self.fake_fetch())
        self.enterContext(
            patch.dict(
                "sys.modules",
                {
                    "yfinance": SimpleNamespace(
                        Ticker=lambda symbol: SimpleNamespace(
                            history=lambda **kw: fx_history([0.7211], ["2026-09-08"])
                        )
                    )
                },
            )
        )
        issues = []
        table = load_fx_table(
            self.config, ["CAD", "GBp"], "2026-09-01", "2026-11-03", refresh=True, issues=issues
        )
        self.assertIsInstance(table, FxTable)
        self.assertEqual(table.max_age_days, 3)
        self.assertEqual(issues, [])
        providers_seen = {x["provider"] for x in table.to_records()}
        self.assertEqual(providers_seen, {"bank_of_canada", "yahoo"})
        self.assertIn("FXGBPCAD", self.urls[0][0])
        # Bank of Canada is first, so its same-day USDCAD observation is kept.
        self.assertEqual(table.latest("USD", "CAD", "2026-09-08")["provider"], "bank_of_canada")
        self.assertEqual(table.latest("GBP", "USD", "2026-09-08")["derived_via"], "CAD")
        stale = table.convert("100", "CAD", "USD", "2026-09-30")
        self.assertEqual(stale["status"], "stale")

    def test_offline_mode_and_no_refresh_use_cache_only(self):
        tickers = []

        def ticker(symbol):
            tickers.append(symbol)
            return SimpleNamespace(history=lambda **kw: fx_history([1.25], ["2026-09-09"]))

        self.patch_fetch(self.fake_fetch())
        with patch.dict("sys.modules", {"yfinance": SimpleNamespace(Ticker=ticker)}):
            load_fx_table(self.config, ["GBP"], "2026-09-01", "2026-11-03", refresh=True, issues=[])
        self.assertEqual(tickers, ["GBPUSD=X"])
        self.forbid_fetch()
        self.enterContext(patch.dict("sys.modules", {"yfinance": None}))
        offline = make_config(self.tmp_path, mode="offline")
        for config, refresh in [(offline, True), (self.config, False)]:
            issues = []
            table = load_fx_table(
                config, ["GBP"], "2026-09-01", "2026-11-03", refresh=refresh, issues=issues
            )
            self.assertEqual(issues, [])
            self.assertEqual(table.latest("USD", "CAD", "2026-09-08")["rate"], "1.3868")
            self.assertEqual(
                {x["provider"] for x in table.to_records()}, {"bank_of_canada", "yahoo"}
            )

    def test_only_configured_providers_are_used(self):
        self.config = make_config(self.tmp_path, fx_providers=["yahoo"])
        self.forbid_fetch()
        self.enterContext(
            patch.dict(
                "sys.modules",
                {
                    "yfinance": SimpleNamespace(
                        Ticker=lambda symbol: SimpleNamespace(
                            history=lambda **kw: fx_history([0.7211], ["2026-09-08"])
                        )
                    )
                },
            )
        )
        issues = []
        table = load_fx_table(
            self.config, ["CAD", "USD"], "2026-09-01", "2026-09-11", refresh=True, issues=issues
        )
        self.assertEqual({x["provider"] for x in table.to_records()}, {"yahoo"})
        self.assertEqual(table.latest("CAD", "USD", "2026-09-08")["rate"], "0.7211")


class FxConfigTests(unittest.TestCase):
    def test_defaults(self):
        data = DEFAULTS["data"]
        self.assertEqual(data["fx_providers"], ["bank_of_canada", "yahoo"])
        self.assertEqual(data["max_fx_age_days"], 7)
        self.assertEqual(data["listing_provider"], "yahoo")
        self.assertEqual(data["max_listing_age_days"], 30)
        self.assertEqual(validate_config({})["data"]["fx_providers"], ["bank_of_canada", "yahoo"])

    def test_valid_overrides(self):
        config = validate_config(
            {
                "data": {
                    "fx_providers": ["yahoo"],
                    "listing_provider": "none",
                    "max_fx_age_days": 0,
                    "max_listing_age_days": 1000,
                }
            }
        )
        self.assertEqual(config["data"]["fx_providers"], ["yahoo"])
        self.assertEqual(config["data"]["listing_provider"], "none")

    def test_invalid_values_are_rejected(self):
        for data in [
            {"fx_providers": []},
            {"fx_providers": "yahoo"},
            {"fx_providers": ["ecb"]},
            {"fx_providers": ["yahoo", "yahoo"]},
            {"fx_providers": [None]},
            {"listing_provider": "google"},
            {"listing_provider": None},
            {"max_fx_age_days": -1},
            {"max_fx_age_days": 1001},
            {"max_fx_age_days": "7"},
            {"max_fx_age_days": True},
            {"max_listing_age_days": -1},
            {"max_listing_age_days": 1001},
        ]:
            with self.assertRaises(ValueError, msg=data):
                validate_config({"data": data})


class ValetSupportedSeriesTests(_Case):
    """Valet publishes a fixed list of daily series; an unlisted one must not lose the rest."""

    def observations(self, currencies, *, refresh=True, issues=None):
        return bank_of_canada_observations(
            self.config,
            list(currencies),
            "2026-09-01",
            "2026-11-03",
            refresh=refresh,
            issues=[] if issues is None else issues,
        )

    def test_a_currency_without_a_valet_series_is_never_requested(self):
        self.patch_fetch(self.fake_fetch())
        issues = []
        records = self.observations(["DKK", "GBP", "USD"], issues=issues)
        url = self.urls[0][0]
        self.assertNotIn("FXDKKCAD", url)
        self.assertIn("FXGBPCAD", url)
        self.assertIn("FXUSDCAD", url)
        self.assertTrue(any(record["pair"] == "GBPCAD" for record in records))
        self.assertTrue(any(record["pair"] == "USDCAD" for record in records))
        self.assertEqual([x["code"] for x in issues], ["FX_CURRENCY_UNSUPPORTED"])
        self.assertEqual(issues[0]["provider"], "bank_of_canada")
        self.assertEqual(issues[0]["currency"], "DKK")
        self.assertEqual(issues[0]["severity"], "warning")

    def test_unsupported_currencies_alone_still_load_the_usd_series(self):
        self.patch_fetch(self.fake_fetch())
        issues = []
        records = self.observations(["DKK", "ILS"], issues=issues)
        self.assertIn("FXUSDCAD", self.urls[0][0])
        self.assertTrue(any(record["pair"] == "USDCAD" for record in records))
        self.assertEqual(sorted(x["currency"] for x in issues), ["DKK", "ILS"])

    def test_a_rejected_series_still_leaves_the_usd_anchor(self):
        """Valet 404s the whole request for one unknown series, so retry the anchor alone."""
        url = "https://www.bankofcanada.ca/valet/observations/FXGBPCAD,FXUSDCAD/json"

        def fake(request_url, user_agent=None):
            self.urls.append((request_url, user_agent))
            if "FXGBPCAD" in request_url:
                raise HTTPError(url, 404, "Series FXGBPCAD not found. " + url, {}, None)
            return json.loads(json.dumps(VALET)), RECEIVED

        self.patch_fetch(fake)
        issues = []
        records = self.observations(["GBP", "USD"], issues=issues)
        self.assertEqual(len(self.urls), 2)
        self.assertNotIn("FXGBPCAD", self.urls[1][0])
        self.assertTrue(any(record["pair"] == "USDCAD" for record in records))
        self.assertFalse(any(record["base"] == "GBP" for record in records))
        self.assertEqual([x["code"] for x in issues], ["FX_CURRENCY_UNSUPPORTED"])
        self.assertEqual(issues[0]["currency"], "GBP")
        self.assertNotIn("bankofcanada", json.dumps(issues))

    def test_an_anchor_that_also_fails_is_one_isolated_provider_failure(self):
        def fail(*args, **kwargs):
            self.urls.append(args)
            raise HTTPError("https://www.bankofcanada.ca/valet/x", 503, "down", {}, None)

        self.patch_fetch(fail)
        issues = []
        self.assertEqual(self.observations(["GBP", "USD"], issues=issues), [])
        self.assertEqual([x["code"] for x in issues], ["FX_PROVIDER_FAILED"])
        self.assertEqual(issues[0]["detail"], "HTTP 503")


class CachedWindowTests(_Case):
    """A cache-only read must not depend on the exact window a refresh happened to use."""

    START, END = "2026-09-01", "2026-11-03"
    LATER_START, LATER_END = "2026-09-02", "2026-11-04"

    def observations(self, currencies, start, end, *, refresh, issues):
        return bank_of_canada_observations(
            self.config, list(currencies), start, end, refresh=refresh, issues=issues
        )

    def test_the_next_day_reads_the_archived_window(self):
        self.patch_fetch(self.fake_fetch())
        live = self.observations(["GBP", "USD"], self.START, self.END, refresh=True, issues=[])
        self.forbid_fetch()
        issues = []
        cached = self.observations(
            ["GBP", "USD"], self.LATER_START, self.LATER_END, refresh=False, issues=issues
        )
        self.assertEqual(issues, [])
        self.assertEqual(cached, live)
        table = FxTable(cached, max_age_days=7)
        converted = table.convert("100", "USD", "CAD", "2026-11-04")
        self.assertEqual(converted["status"], "converted")
        self.assertEqual(converted["observation_date"], "2026-11-02")

    def test_a_newly_held_currency_never_discards_the_cached_series(self):
        self.patch_fetch(self.fake_fetch())
        self.observations(["GBP", "USD"], self.START, self.END, refresh=True, issues=[])
        self.forbid_fetch()
        issues = []
        cached = self.observations(
            ["EUR", "GBP", "USD"], self.LATER_START, self.LATER_END, refresh=False, issues=issues
        )
        pairs = {record["pair"] for record in cached}
        self.assertIn("USDCAD", pairs)
        self.assertIn("GBPCAD", pairs)
        self.assertFalse(any(record["base"] == "EUR" for record in cached))
        self.assertEqual(issues, [])

    def test_an_unrelated_window_is_still_a_cache_miss(self):
        self.patch_fetch(self.fake_fetch())
        self.observations(["USD"], self.START, self.END, refresh=True, issues=[])
        self.forbid_fetch()
        issues = []
        self.assertEqual(
            self.observations(["USD"], "2024-01-01", "2024-03-01", refresh=False, issues=issues),
            [],
        )
        self.assertEqual([x["code"] for x in issues], ["FX_PROVIDER_FAILED"])

    def test_observations_outside_the_requested_window_are_left_out(self):
        self.patch_fetch(self.fake_fetch())
        records = self.observations(["USD"], self.START, "2026-09-30", refresh=True, issues=[])
        self.assertEqual(sorted({x["date"] for x in records}), ["2026-09-04", "2026-09-08"])

    def test_a_cached_yahoo_pair_is_reused_for_a_later_window(self):
        histories = {"CADUSD=X": fx_history([0.7205, 0.7211], ["2026-09-10", "2026-09-11"])}
        module = SimpleNamespace(
            Ticker=lambda symbol: SimpleNamespace(history=lambda **kw: histories[symbol])
        )
        with patch.dict("sys.modules", {"yfinance": module}):
            live = yahoo_fx_observations(
                self.config,
                [("CAD", "USD")],
                "2026-09-01",
                "2026-09-13",
                refresh=True,
                issues=[],
            )
        self.assertTrue(live)
        issues = []
        with patch.dict("sys.modules", {"yfinance": None}):
            cached = yahoo_fx_observations(
                self.config,
                [("CAD", "USD")],
                "2026-09-02",
                "2026-09-14",
                refresh=False,
                issues=issues,
            )
        self.assertEqual(issues, [])
        self.assertEqual(cached, live)


if __name__ == "__main__":
    unittest.main()
