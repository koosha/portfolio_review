"""Fixtures test provider semantics without network access or real financial claims."""

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

from portfolio_lab import providers as p


def fact(value, start, end, filed, accn, form="10-Q"):
    row = {"val": value, "end": end, "filed": filed, "accn": accn, "form": form}
    if start:
        row["start"] = start
    return row


def company():
    """Half-year income and cumulative cashflow plus the preceding annual filing."""
    tags = {}
    values = {
        "Revenues": (1000, 650, 450),
        "GrossProfit": (400, 300, 200),
        "OperatingIncomeLoss": (180, 140, 70),
        "NetIncomeLoss": (120, 90, 50),
        "NetCashProvidedByUsedInOperatingActivities": (200, 160, 90),
        "PaymentsToAcquirePropertyPlantAndEquipment": (60, 50, 20),
    }
    for tag, (annual, current, previous) in values.items():
        rows = [
            fact(annual, "2023-01-01", "2023-12-31", "2024-02-20", "FY", "10-K"),
            fact(current, "2024-01-01", "2024-06-30", "2024-08-01", "Q2"),
            fact(previous, "2023-01-01", "2023-06-30", "2024-08-01", "Q2"),
        ]
        # A separately reported quarter must never get added to the cumulative YTD.
        if tag != "NetCashProvidedByUsedInOperatingActivities":
            rows.append(fact(current / 2, "2024-04-01", "2024-06-30", "2024-08-01", "Q2"))
        tags[tag] = {"units": {"USD": rows}}
    tags["Assets"] = {
        "units": {
            "USD": [
                fact(500, None, "2022-12-31", "2024-02-20", "FY", "10-K"),
                fact(600, None, "2023-12-31", "2024-02-20", "FY", "10-K"),
                fact(570, None, "2023-06-30", "2024-08-01", "Q2"),
                fact(700, None, "2024-06-30", "2024-08-01", "Q2"),
            ]
        }
    }
    return {"cik": 123, "facts": {"us-gaap": tags}}


def extract(data, date="2024-08-02"):
    return p.extract_sec_fundamentals(data, "TEST", date, "2024-08-02T12:00:00+00:00", "fixture")


def config(tmp_path, **data):
    return {
        "research": {"path": str(tmp_path / "research.sqlite")},
        "data": {"mode": "offline", **data},
    }


def bundle():
    return {
        "issues": [],
        "sources": [],
        "securities": pd.DataFrame(
            [
                {
                    "security_id": "TEST",
                    "ticker": "TEST",
                    "issuer_id": "TESTCO",
                    "instrument_type": "equity",
                    "currency": "USD",
                    "cik": "123",
                    "market_cap": None,
                    "eligible": True,
                }
            ]
        ),
    }


class _Patches:
    def __init__(self, case):
        self.case = case

    def setattr(self, obj, name, value):
        self.case.enterContext(patch.object(obj, name, value))

    def setenv(self, key, value):
        self.case.enterContext(patch.dict(os.environ, {key: value}))

    def delenv(self, key):
        self.case.enterContext(patch.dict(os.environ))
        os.environ.pop(key, None)


class ProviderTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.tmp_path = Path(directory)
        self.patches = _Patches(self)

    def test_ttm_uses_annual_plus_ytd_and_not_cumulative_quarter_sum(self):
        row = extract(company())
        assert row["revenue"] == 1200
        assert row["operating_cash_flow"] == 270
        assert row["capex"] == 90
        assert row["net_income"] == 160
        assert row["gross_profit"] == 500
        assert row["assets"] == 700
        assert row["assets_begin"] == 570
        assert row["period_end"] == "2024-06-30"
        assert row["available_at"] == "2024-08-02"
        assert (
            row["provenance"]["operating_cash_flow"]["method"]
            == "annual_plus_current_ytd_minus_prior_ytd"
        )
        assert [x["accession"] for x in row["provenance"]["operating_cash_flow"]["components"]] == [
            "FY",
            "Q2",
            "Q2",
        ]

    def test_filing_date_requires_next_day_not_same_day(self):
        row = extract(company(), "2024-08-01")
        assert row["period_end"] == "2023-12-31"
        assert row["revenue"] == 1000
        assert row["assets_begin"] == 500
        assert extract(company(), "2024-02-20") is None

    def test_restatement_cannot_change_prior_information_set(self):
        data = company()
        tags = data["facts"]["us-gaap"]
        for tag in list(tags):
            for original in list(tags[tag]["units"]["USD"]):
                if original["accn"] == "Q2":
                    amended = {**original, "filed": "2024-09-10", "accn": "Q2A", "form": "10-Q/A"}
                    if tag == "Revenues" and original.get("start") == "2024-01-01":
                        amended["val"] = 750
                    tags[tag]["units"]["USD"].append(amended)
        assert extract(data, "2024-09-10")["revenue"] == 1200
        assert extract(data, "2024-09-11")["revenue"] == 1300
        assert extract(data, "2024-08-02")["revenue"] == extract(company())["revenue"]

    def test_mismatched_ytd_cash_flow_is_missing_not_quarterly_fiction(self):
        data = company()
        for row in data["facts"]["us-gaap"]["NetCashProvidedByUsedInOperatingActivities"]["units"][
            "USD"
        ]:
            if row.get("start") == "2023-01-01" and row["accn"] == "Q2":
                row["end"] = "2023-03-31"
        result = extract(data)
        assert result["operating_cash_flow"] is None
        assert result["revenue"] == 1200

    def test_non_usd_custom_tags_and_shares_do_not_fill_missing_fields(self):
        data = company()
        data["facts"]["us-gaap"].pop("GrossProfit")
        data["facts"]["issuer-custom"] = {
            "GrossProfit": {
                "units": {"USD": [fact(999, "2024-01-01", "2024-06-30", "2024-08-01", "Q2")]}
            }
        }
        data["facts"]["dei"] = {
            "EntityCommonStockSharesOutstanding": {
                "units": {"shares": [fact(100, None, "2024-06-30", "2024-08-01", "Q2")]}
            }
        }
        data["facts"]["us-gaap"]["PaymentsToAcquirePropertyPlantAndEquipment"]["units"] = {
            "EUR": []
        }
        row = extract(data)
        assert row["gross_profit"] is None
        assert row["capex"] is None
        assert row["debt"] is None
        assert "market_cap" not in row
        assert (
            p.extract_sec_fundamentals(
                {"facts": {"ifrs-full": {}}}, "TEST", "2024-08-02", "now", "fixture"
            )
            is None
        )

    def test_conflicting_duplicate_tag_context_is_not_arbitrarily_chosen(self):
        data = company()
        data["facts"]["us-gaap"]["Revenues"]["units"]["USD"].append(
            fact(660, "2024-01-01", "2024-06-30", "2024-08-01", "Q2")
        )
        assert extract(data)["revenue"] is None

    def test_balance_sheet_identity_generates_review_warning(self):
        data = company()
        for tag, value in [
            ("Liabilities", 600),
            ("StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest", 300),
        ]:
            data["facts"]["us-gaap"][tag] = {
                "units": {"USD": [fact(value, None, "2024-06-30", "2024-08-01", "Q2")]}
            }
        assert any("do not reconcile" in x for x in extract(data)["data_warnings"])

    def test_offline_is_network_free_and_no_synthetic_fallback(self):
        tmp_path = self.tmp_path
        monkeypatch = self.patches

        def fail(*args, **kwargs):
            raise AssertionError("network invoked")

        monkeypatch.setattr(p, "_fetch_json", fail)
        result = p.enrich_bundle(
            bundle(),
            config(
                tmp_path,
                sec_enabled=True,
                fred_enabled=True,
                price_provider="yahoo",
                refresh_network=True,
            ),
            "2024-08-02",
        )
        assert result["prices"].empty
        assert result["fundamentals"].empty
        assert any(x["code"] == "OFFLINE_CONNECTORS_SKIPPED" for x in result["issues"])

    def test_csv_prices_need_adjusted_close_and_filter_future_rows(self):
        tmp_path = self.tmp_path
        path = tmp_path / "prices.csv"
        pd.DataFrame(
            [
                {
                    "date": "2024-08-01",
                    "security_id": "TEST",
                    "close": 100,
                    "adjusted_close": 90,
                    "available_at": "2024-08-02",
                },
                {
                    "date": "2024-08-02",
                    "security_id": "TEST",
                    "close": 110,
                    "adjusted_close": 99,
                    "available_at": "2024-08-03",
                },
            ]
        ).to_csv(path, index=False)
        result = p.enrich_bundle(bundle(), config(tmp_path, prices_csv=str(path)), "2024-08-02")
        assert len(result["prices"]) == 1
        assert result["prices"].iloc[0]["close"] == 100
        assert result["prices"].iloc[0]["adjusted_close"] == 90
        assert result["sources"][0]["sha256"]
        pd.DataFrame([{"date": "2024-08-01", "security_id": "TEST", "close": 100}]).to_csv(
            path, index=False
        )
        result = p.enrich_bundle(bundle(), config(tmp_path, prices_csv=str(path)), "2024-08-02")
        assert result["prices"].empty
        assert any(x["code"] == "CSV_LOAD_FAILED" for x in result["issues"])

    def test_strict_historical_receipt_filter(self):
        tmp_path = self.tmp_path
        path = tmp_path / "prices.csv"
        pd.DataFrame(
            [
                {
                    "date": "2024-08-01",
                    "security_id": "TEST",
                    "close": 100,
                    "adjusted_close": 90,
                    "available_at": "2024-08-02",
                    "received_at": "2024-08-04T12:00:00Z",
                }
            ]
        ).to_csv(path, index=False)
        result = p.enrich_bundle(
            bundle(),
            config(tmp_path, prices_csv=str(path), require_received_by_cutoff=True),
            "2024-08-02",
        )
        assert result["prices"].empty

    def test_fred_secret_redaction_vintage_bounds_and_cache(self):
        tmp_path = self.tmp_path
        monkeypatch = self.patches
        monkeypatch.setenv("TEST_FRED_KEY", "very-sensitive-key")
        urls = []

        def fake(url, user_agent=None):
            urls.append(url)
            if "/series?" in url:
                return {"seriess": [{"units": "Percent"}]}, "2024-08-03T12:00:00+00:00"
            return {
                "count": 2,
                "observations": [
                    {"date": "2024-07-01", "value": "4.2"},
                    {"date": "2024-08-01", "value": "."},
                ],
            }, "2024-08-03T12:00:00+00:00"

        monkeypatch.setattr(p, "_fetch_json", fake)
        cfg = config(
            tmp_path,
            mode="live",
            fred_enabled=True,
            fred_api_key_env="TEST_FRED_KEY",
            fred_series=["UNRATE"],
            refresh_network=True,
        )
        result = p.enrich_bundle(bundle(), cfg, "2024-08-02")
        assert len(result["macro"]) == 1
        assert result["macro"].iloc[0]["vintage_date"] == "2024-08-02"
        assert result["macro"].iloc[0]["units"] == "Percent"
        assert all(
            "realtime_start=2024-08-02" in x and "realtime_end=2024-08-02" in x for x in urls
        )
        assert "very-sensitive-key" not in json.dumps(result["sources"])
        assert "very-sensitive-key" not in "".join(
            x.read_text() for x in (tmp_path / "cache").rglob("*.json")
        )
        cfg["data"]["refresh_network"] = False
        monkeypatch.delenv("TEST_FRED_KEY")
        monkeypatch.setattr(
            p, "_fetch_json", lambda *args: self.fail("offline cache issued request")
        )
        cached = p.enrich_bundle(bundle(), cfg, "2024-08-03")
        assert len(cached["macro"]) == 1
        assert cached["macro"].iloc[0]["vintage_date"] == "2024-08-02"

    def test_provider_exception_url_does_not_expose_key(self):
        tmp_path = self.tmp_path
        monkeypatch = self.patches
        monkeypatch.setenv("TEST_FRED_KEY", "very-sensitive-key")

        def fail(*args, **kwargs):
            raise RuntimeError("failed api_key=very-sensitive-key")

        monkeypatch.setattr(p, "_fetch_json", fail)
        result = p.enrich_bundle(
            bundle(),
            config(
                tmp_path,
                mode="live",
                fred_enabled=True,
                fred_api_key_env="TEST_FRED_KEY",
                fred_series=["UNRATE"],
                refresh_network=True,
            ),
            "2024-08-02",
        )
        assert "very-sensitive-key" not in json.dumps(result["issues"])
        assert result["macro"].empty

    def test_cached_sec_reextracts_asof_without_network(self):
        tmp_path = self.tmp_path
        monkeypatch = self.patches
        p._archive(
            config(tmp_path),
            "sec",
            "0000000123",
            company(),
            "https://data.sec.gov/fixture",
            "2024-08-02T12:00:00+00:00",
        )
        monkeypatch.setattr(p, "_fetch_json", lambda *args: self.fail("cache issued request"))
        result = p.enrich_bundle(
            bundle(),
            config(tmp_path, mode="live", sec_enabled=True, refresh_network=False),
            "2024-08-01",
        )
        assert result["fundamentals"].iloc[0]["revenue"] == 1000

    def test_universe_adds_candidate_without_changing_existing_identity(self):
        tmp_path = self.tmp_path
        path = tmp_path / "universe.csv"
        pd.DataFrame(
            [
                {
                    "security_id": "TEST",
                    "ticker": "WRONG",
                    "issuer_id": "WRONG",
                    "instrument_type": "equity",
                    "currency": "USD",
                    "eligible": True,
                },
                {
                    "security_id": "NEW",
                    "ticker": "NEW",
                    "issuer_id": "NEWCO",
                    "instrument_type": "equity",
                    "currency": "USD",
                    "eligible": False,
                },
            ]
        ).to_csv(path, index=False)
        result = p.enrich_bundle(bundle(), config(tmp_path, universe_csv=str(path)), "2024-08-02")
        securities = result["securities"].set_index("security_id")
        assert securities.loc["TEST", "ticker"] == "TEST"
        assert not securities.loc["NEW", "eligible"]
        assert result["scope"] == "configured_universe"

    def test_strict_receipt_requires_fund_and_forecast_timestamps(self):
        for frame, row in [
            (
                "fund_holdings",
                {
                    "fund_id": "F",
                    "issuer_id": "TESTCO",
                    "weight": 1.0,
                    "holdings_date": "2024-08-01",
                    "available_at": "2024-08-02",
                },
            ),
            (
                "forecasts",
                {
                    "security_id": "TEST",
                    "scenario": "central",
                    "horizon_months": 12,
                    "return_value": 0.1,
                    "probability": 1.0,
                    "basis": "subjective",
                    "forecast_date": "2024-08-02",
                },
            ),
        ]:
            path = self.tmp_path / f"{frame}.csv"
            pd.DataFrame([row]).to_csv(path, index=False)
            cfg = config(
                self.tmp_path, require_received_by_cutoff=True, **{frame + "_csv": str(path)}
            )
            result = p.enrich_bundle(bundle(), cfg, "2024-08-02")
            assert result[frame].empty
            assert any(x["code"] == "RECEIPT_TIMESTAMP_REQUIRED" for x in result["issues"])

    def test_yahoo_cap_dates_are_preserved_live_and_cached(self):
        fixed_now = datetime(2024, 8, 2, 12, tzinfo=timezone.utc)
        self.patches.setattr(p, "datetime", SimpleNamespace(now=lambda tz: fixed_now))
        history = pd.DataFrame(
            {"Close": [100.0], "Adj Close": [90.0], "Volume": [1000]},
            index=pd.DatetimeIndex(["2024-08-01"], tz="America/New_York", name="Date"),
        )
        ticker = SimpleNamespace(
            history=lambda **kw: history,
            info={"marketCap": 1000000.0, "currency": "USD", "exchange": "TEST"},
        )
        self.enterContext(
            patch.dict("sys.modules", {"yfinance": SimpleNamespace(Ticker=lambda symbol: ticker)})
        )
        cfg = config(self.tmp_path, mode="live", price_provider="yahoo", refresh_network=True)
        result = p.enrich_bundle(bundle(), cfg, "2024-08-02")
        row = result["securities"].iloc[0]
        assert row["market_cap"] == 1000000.0
        assert row["market_cap_as_of"] == "2024-08-02"
        assert row["market_cap_available_at"] == fixed_now.isoformat()
        assert row["market_cap_received_at"] == fixed_now.isoformat()
        cfg["data"]["refresh_network"] = False
        cached = p.enrich_bundle(bundle(), cfg, "2024-08-02")
        assert cached["securities"].iloc[0]["market_cap_as_of"] == "2024-08-02"
        historical = p.enrich_bundle(bundle(), cfg, "2024-08-01")
        assert pd.isna(historical["securities"].iloc[0]["market_cap"])

    def test_publication_after_new_york_close_is_excluded_with_dst(self):
        for date, close_utc in [("2024-08-02", "20:00:00"), ("2024-12-02", "21:00:00")]:
            exact = pd.Timestamp(date + "T" + close_utc + "Z")
            frame = pd.DataFrame(
                [
                    {
                        "date": date,
                        "security_id": "AT_CLOSE",
                        "close": 100,
                        "adjusted_close": 90,
                        "available_at": exact.isoformat(),
                        "received_at": exact.isoformat(),
                    },
                    {
                        "date": date,
                        "security_id": "AFTER_CLOSE",
                        "close": 200,
                        "adjusted_close": 180,
                        "available_at": (exact + pd.Timedelta(seconds=1)).isoformat(),
                        "received_at": exact.isoformat(),
                    },
                    {
                        "date": date,
                        "security_id": "LATE_RECEIPT",
                        "close": 300,
                        "adjusted_close": 270,
                        "available_at": exact.isoformat(),
                        "received_at": (exact + pd.Timedelta(seconds=1)).isoformat(),
                    },
                ]
            )
            path = self.tmp_path / "close_test.csv"
            frame.to_csv(path, index=False)
            result = p.enrich_bundle(bundle(), config(self.tmp_path, prices_csv=str(path)), date)
            assert set(result["prices"].security_id) == {"AT_CLOSE", "LATE_RECEIPT"}
            strict = p.enrich_bundle(
                bundle(),
                config(self.tmp_path, prices_csv=str(path), require_received_by_cutoff=True),
                date,
            )
            assert set(strict["prices"].security_id) == {"AT_CLOSE"}


if __name__ == "__main__":
    unittest.main()
