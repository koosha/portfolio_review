"""Dated FX observations: exact conversion, subunits, inversion, staleness and price series."""

import json
import unittest
from decimal import Decimal, localcontext

import pandas as pd

from portfolio_research.fx import (
    MINOR_UNITS,
    FxTable,
    convert_price_series,
    normalize_unit,
    usd_scenario_return,
    validate_observation,
)

RECEIVED = "2026-09-12T12:00:00+00:00"


def obs(pair, rate, date, provider="bank_of_canada", source_id=None, **extra):
    return {
        "pair": pair,
        "base": pair[:3],
        "quote": pair[3:],
        "rate": rate,
        "date": date,
        "source_id": source_id or f"{provider}:{pair}:{date}",
        "provider": provider,
        "received_at": RECEIVED,
        **extra,
    }


def inverse(rate):
    with localcontext() as context:
        context.prec = 34
        return format(Decimal(1) / Decimal(rate), "f")


class NormalizeUnitTests(unittest.TestCase):
    def test_pence_and_cents_become_major_units(self):
        self.assertEqual(normalize_unit("128.75", "GBp"), ("1.2875", "GBP", 100))
        self.assertEqual(normalize_unit("128.75", "GBX"), ("1.2875", "GBP", 100))
        self.assertEqual(normalize_unit("250", "ZAc"), ("2.5", "ZAR", 100))
        self.assertEqual(normalize_unit("1234.5", "ILA"), ("12.345", "ILS", 100))
        self.assertEqual(MINOR_UNITS["GBp"], ("GBP", 100))

    def test_major_units_pass_through_with_factor_one(self):
        self.assertEqual(normalize_unit("100.10", "USD"), ("100.10", "USD", 1))
        self.assertEqual(normalize_unit("285.20", "CAD"), ("285.20", "CAD", 1))

    def test_missing_amount_passes_through(self):
        self.assertEqual(normalize_unit(None, "GBp"), (None, "GBP", 100))
        self.assertEqual(normalize_unit(None, "USD"), (None, "USD", 1))

    def test_invalid_values_are_rejected(self):
        for amount, currency in [
            ("1", None),
            ("1", "US"),
            ("1", "usd"),
            ("abc", "USD"),
            ("NaN", "USD"),
            (True, "USD"),
        ]:
            with self.assertRaises(ValueError, msg=(amount, currency)):
                normalize_unit(amount, currency)


class ValidateObservationTests(unittest.TestCase):
    def test_valid_record_is_canonical(self):
        record = validate_observation(
            obs("USDCAD", "1.3840", "2026-09-04", published_at="2026-09-04T16:30:00-04:00")
        )
        self.assertEqual(record["pair"], "USDCAD")
        self.assertEqual(record["base"], "USD")
        self.assertEqual(record["quote"], "CAD")
        self.assertEqual(record["rate"], "1.3840")
        self.assertEqual(record["date"], "2026-09-04")
        self.assertEqual(record["published_at"], "2026-09-04T16:30:00-04:00")
        self.assertEqual(record["received_at"], RECEIVED)

    def test_invalid_records_are_rejected(self):
        bad = [
            {**obs("USDCAD", "1.38", "2026-09-04"), "pair": "CADUSD"},
            obs("USDCAD", "0", "2026-09-04"),
            obs("USDCAD", "-1.38", "2026-09-04"),
            obs("USDCAD", "NaN", "2026-09-04"),
            obs("USDCAD", "Infinity", "2026-09-04"),
            obs("USDCAD", "abc", "2026-09-04"),
            obs("USDCAD", "1.38", "2026-9-4"),
            obs("USDCAD", "1.38", "2026-02-30"),
            {**obs("USDCAD", "1.38", "2026-09-04"), "received_at": "2026-09-04T12:00:00"},
            {**obs("USDCAD", "1.38", "2026-09-04"), "published_at": "2026-09-04T16:30:00"},
            {**obs("USDCAD", "1.38", "2026-09-04"), "source_id": ""},
            {**obs("USDCAD", "1.38", "2026-09-04"), "provider": None},
            {**obs("GBPUSD", "1.25", "2026-09-04"), "base": "GBp", "pair": "GBpUSD"},
            obs("USDUSD", "1", "2026-09-04"),
            # Provider rates are bounded data: an unbounded exponent or digit count
            # would otherwise build a gigabyte-long string when the rate is formatted.
            obs("USDCAD", "9e9999999", "2026-09-04"),
            obs("USDCAD", "1e-999999999", "2026-09-04"),
            obs("USDCAD", "1" * 200, "2026-09-04"),
            "not a record",
        ]
        for record in bad:
            with self.assertRaises(ValueError, msg=record):
                validate_observation(record)


class FxTableTests(unittest.TestCase):
    def table(self, *records, max_age_days=7):
        return FxTable(records, max_age_days=max_age_days)

    def test_latest_picks_newest_on_or_before_date_and_ignores_future(self):
        table = self.table(
            obs("USDCAD", "1.3800", "2026-09-01"),
            obs("USDCAD", "1.3840", "2026-09-04"),
            obs("USDCAD", "1.4500", "2026-09-15"),
        )
        found = table.latest("USD", "CAD", "2026-09-13")
        self.assertEqual(found["rate"], "1.3840")
        self.assertEqual(found["date"], "2026-09-04")
        self.assertFalse(found["inverted"])
        self.assertEqual(table.latest("USD", "CAD", "2026-09-15")["rate"], "1.4500")
        self.assertIsNone(table.latest("USD", "CAD", "2026-08-31"))
        self.assertIsNone(table.latest("EUR", "USD", "2026-09-13"))

    def test_latest_inverts_when_only_reverse_pair_exists(self):
        table = self.table(obs("USDCAD", "1.3868", "2026-09-11"))
        found = table.latest("CAD", "USD", "2026-09-13")
        self.assertTrue(found["inverted"])
        self.assertEqual(found["pair"], "CADUSD")
        self.assertEqual(found["base"], "CAD")
        self.assertEqual(found["quote"], "USD")
        self.assertEqual(found["rate"], inverse("1.3868"))
        self.assertEqual(found["date"], "2026-09-11")
        self.assertEqual(Decimal(found["rate"]).adjusted(), -1)
        self.assertEqual(len(Decimal(found["rate"]).as_tuple().digits), 34)

    def test_direct_pair_wins_ties_and_newer_inverse_beats_older_direct(self):
        table = self.table(
            obs("CADUSD", "0.7211", "2026-09-11", provider="yahoo"),
            obs("USDCAD", "1.3868", "2026-09-11"),
        )
        self.assertEqual(table.latest("CAD", "USD", "2026-09-13")["rate"], "0.7211")
        table = self.table(
            obs("CADUSD", "0.7100", "2026-09-02", provider="yahoo"),
            obs("USDCAD", "1.3868", "2026-09-11"),
        )
        found = table.latest("CAD", "USD", "2026-09-13")
        self.assertTrue(found["inverted"])
        self.assertEqual(found["date"], "2026-09-11")

    def test_first_observation_for_pair_and_date_is_kept(self):
        table = self.table(
            obs("USDCAD", "1.3868", "2026-09-11"),
            obs("USDCAD", "1.3900", "2026-09-11", provider="yahoo"),
        )
        self.assertEqual(table.latest("USD", "CAD", "2026-09-11")["provider"], "bank_of_canada")

    def test_identity_conversion(self):
        result = self.table().convert("100.50", "USD", "USD", "2026-09-13")
        self.assertEqual(result["status"], "identity")
        self.assertEqual(result["amount"], "100.50")
        self.assertEqual(result["rate"], "1")
        self.assertEqual(result["issues"], [])
        self.assertFalse(result["inverted"])

    def test_cad_to_usd_uses_exact_decimal_product(self):
        table = self.table(obs("CADUSD", "0.7211", "2026-09-11", source_id="fx:1"))
        result = table.convert("1426.00", "CAD", "USD", "2026-09-13")
        self.assertEqual(result["status"], "converted")
        self.assertEqual(Decimal(result["amount"]), Decimal("1426.00") * Decimal("0.7211"))
        self.assertEqual(result["amount"], "1028.288600")
        self.assertEqual(result["rate"], "0.7211")
        self.assertEqual(result["pair"], "CADUSD")
        self.assertEqual(result["direction"], "quote per base")
        self.assertEqual(result["observation_date"], "2026-09-11")
        self.assertEqual(result["requested_date"], "2026-09-13")
        self.assertEqual(result["age_days"], 2)
        self.assertEqual(result["source_id"], "fx:1")
        self.assertEqual(result["provider"], "bank_of_canada")
        self.assertEqual(result["from_currency"], "CAD")
        self.assertEqual(result["to_currency"], "USD")
        self.assertEqual(result["issues"], [])

    def test_inverted_conversion_uses_inverse_rate(self):
        table = self.table(obs("USDCAD", "1.25", "2026-09-11"))
        result = table.convert("100", "CAD", "USD", "2026-09-11")
        self.assertEqual(result["status"], "converted")
        self.assertTrue(result["inverted"])
        self.assertEqual(Decimal(result["amount"]), Decimal("80"))

    def test_minor_unit_is_normalized_before_conversion(self):
        table = self.table(obs("GBPUSD", "1.25", "2026-09-11"))
        result = table.convert("128.75", "GBp", "USD", "2026-09-11")
        self.assertEqual(result["status"], "converted")
        self.assertEqual(result["unit_factor"], 100)
        self.assertEqual(result["amount"], "1.609375")
        self.assertEqual(result["from_currency"], "GBp")
        normalized = table.convert("128.75", "GBp", "GBP", "2026-09-11")
        self.assertEqual(normalized["status"], "unit_normalized")
        self.assertEqual(normalized["amount"], "1.2875")
        self.assertEqual(normalized["unit_factor"], 100)

    def test_stale_observation_gives_no_amount(self):
        table = self.table(obs("USDCAD", "1.3700", "2026-08-20"), max_age_days=7)
        result = table.convert("100", "CAD", "USD", "2026-09-13")
        self.assertEqual(result["status"], "stale")
        self.assertIsNone(result["amount"])
        self.assertEqual(result["observation_date"], "2026-08-20")
        self.assertEqual(result["age_days"], 24)
        self.assertEqual([x["code"] for x in result["issues"]], ["STALE_FX"])

    def test_missing_observation_gives_no_amount(self):
        result = self.table().convert("100", "CAD", "USD", "2026-09-13")
        self.assertEqual(result["status"], "missing")
        self.assertIsNone(result["amount"])
        self.assertIsNone(result["rate"])
        self.assertEqual([x["code"] for x in result["issues"]], ["MISSING_FX"])

    def test_never_uses_observation_after_requested_date(self):
        table = self.table(obs("CADUSD", "0.7211", "2026-09-14"))
        result = table.convert("100", "CAD", "USD", "2026-09-13")
        self.assertEqual(result["status"], "missing")

    def test_to_records_is_sorted_and_serializable(self):
        table = self.table(
            obs("USDCAD", "1.3868", "2026-09-11"),
            obs("GBPCAD", "1.8250", "2026-09-11"),
            obs("USDCAD", "1.3840", "2026-09-04"),
        )
        records = table.to_records()
        self.assertEqual(
            [(x["pair"], x["date"]) for x in records],
            [("GBPCAD", "2026-09-11"), ("USDCAD", "2026-09-04"), ("USDCAD", "2026-09-11")],
        )
        json.dumps(records)
        records[0]["rate"] = "999"
        self.assertEqual(table.to_records()[0]["rate"], "1.8250")

    def test_invalid_arguments_are_rejected(self):
        with self.assertRaises(ValueError):
            FxTable(max_age_days=-1)
        with self.assertRaises(ValueError):
            self.table().convert("100", "CAD", "USD", "13/09/2026")
        with self.assertRaises(ValueError):
            self.table().add(obs("USDCAD", "0", "2026-09-04"))


class PriceSeriesTests(unittest.TestCase):
    def cad_frame(self):
        dates = ["2026-08-31", "2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04"]
        rows = [
            {
                "security_id": "RY.TO",
                "date": day,
                "close": 100.0,
                "adjusted_close": 95.0,
                "currency": "CAD",
                "volume": 10,
            }
            for day in dates
        ]
        rows.append({**rows[0], "date": "2026-09-07"})  # Labour Day: no Valet observation
        rows.append({**rows[0], "date": "2026-09-25"})  # 14 days after the last observation
        rows.append(
            {
                "security_id": "AAPL",
                "date": "2026-09-04",
                "close": 300.0,
                "adjusted_close": 299.0,
                "currency": "USD",
                "volume": 5,
            }
        )
        return pd.DataFrame(rows)

    def cad_table(self):
        rates = ["0.7000", "0.7125", "0.7250", "0.7375", "0.7500"]
        dates = ["2026-08-31", "2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04"]
        records = [obs("CADUSD", r, d, source_id=f"fx:{d}") for r, d in zip(rates, dates)]
        records.append(obs("CADUSD", "0.8000", "2026-09-11", source_id="fx:today"))
        return FxTable(records)

    def test_flat_local_series_rises_with_dated_fx_and_no_rate_leakage(self):
        frame = self.cad_frame()
        before = frame.copy()
        result, issues = convert_price_series(frame, self.cad_table(), to_currency="USD")
        pd.testing.assert_frame_equal(frame, before)
        cad = result[result["security_id"] == "RY.TO"].reset_index(drop=True)
        first_five = cad.iloc[:5]
        self.assertTrue(first_five["close"].is_monotonic_increasing)
        self.assertTrue((first_five["close"].diff().dropna() > 0).all())
        self.assertEqual(list(first_five["fx_observation_date"]), list(first_five["date"]))
        self.assertAlmostEqual(first_five["close"].iloc[0], 70.0)
        self.assertAlmostEqual(first_five["adjusted_close"].iloc[0], 66.5)
        self.assertAlmostEqual(first_five["close"].iloc[4], 75.0)
        self.assertEqual(list(cad["local_close"]), [100.0] * len(cad))
        self.assertEqual(set(cad["local_currency"]), {"CAD"})
        self.assertEqual(set(cad["currency"]), {"USD"})
        self.assertEqual(set(cad["fx_pair"]), {"CADUSD"})
        self.assertNotIn("fx:today", set(cad["fx_source_id"]))
        for column in [
            "security_id",
            "date",
            "close",
            "adjusted_close",
            "currency",
            "volume",
            "local_close",
            "local_adjusted_close",
            "local_currency",
            "fx_rate",
            "fx_pair",
            "fx_observation_date",
            "fx_source_id",
        ]:
            self.assertIn(column, result.columns)

    def test_holiday_gap_uses_previous_observation_with_its_date(self):
        result, _ = convert_price_series(self.cad_frame(), self.cad_table())
        holiday = result[(result["security_id"] == "RY.TO") & (result["date"] == "2026-09-07")]
        self.assertEqual(len(holiday), 1)
        self.assertEqual(holiday["fx_observation_date"].iloc[0], "2026-09-04")
        self.assertAlmostEqual(holiday["fx_rate"].iloc[0], 0.75)
        self.assertAlmostEqual(holiday["close"].iloc[0], 75.0)

    def test_rows_beyond_gap_are_dropped_with_one_issue_per_security(self):
        result, issues = convert_price_series(self.cad_frame(), self.cad_table(), max_gap_days=5)
        self.assertNotIn("2026-09-25", set(result["date"]))
        gaps = [x for x in issues if x["code"] == "FX_SERIES_GAPS"]
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0]["security_id"], "RY.TO")
        self.assertEqual(gaps[0]["rows"], 1)
        self.assertIn(gaps[0]["severity"], {"warning", "error"})
        self.assertIn("RY.TO", gaps[0]["message"])

    def test_rows_in_target_currency_pass_through(self):
        result, _ = convert_price_series(self.cad_frame(), self.cad_table())
        usd = result[result["security_id"] == "AAPL"].iloc[0]
        self.assertEqual(usd["close"], 300.0)
        self.assertEqual(usd["currency"], "USD")
        self.assertTrue(pd.isna(usd["fx_rate"]))
        self.assertTrue(pd.isna(usd["fx_pair"]))
        self.assertTrue(pd.isna(usd["fx_observation_date"]))
        self.assertTrue(pd.isna(usd["fx_source_id"]))

    def test_inverse_pair_and_subunit_series(self):
        frame = pd.DataFrame(
            [
                {
                    "security_id": "RY.TO",
                    "date": "2026-09-04",
                    "close": 100.0,
                    "adjusted_close": 100.0,
                    "currency": "CAD",
                },
                {
                    "security_id": "VOD.L",
                    "date": "2026-09-04",
                    "close": 128.75,
                    "adjusted_close": 128.75,
                    "currency": "GBp",
                },
            ]
        )
        table = FxTable([obs("USDCAD", "1.25", "2026-09-04"), obs("GBPUSD", "1.25", "2026-09-04")])
        result, issues = convert_price_series(frame, table)
        self.assertEqual(issues, [])
        by_id = result.set_index("security_id")
        self.assertAlmostEqual(by_id.loc["RY.TO", "close"], 80.0)
        self.assertEqual(by_id.loc["RY.TO", "fx_pair"], "CADUSD")
        self.assertAlmostEqual(by_id.loc["VOD.L", "close"], 1.609375)
        self.assertEqual(by_id.loc["VOD.L", "local_close"], 128.75)
        self.assertEqual(by_id.loc["VOD.L", "local_currency"], "GBp")
        self.assertEqual(by_id.loc["VOD.L", "fx_pair"], "GBPUSD")

    def test_empty_frame(self):
        frame = pd.DataFrame(columns=["security_id", "date", "close", "adjusted_close", "currency"])
        result, issues = convert_price_series(frame, FxTable())
        self.assertTrue(result.empty)
        self.assertIn("fx_rate", result.columns)
        self.assertEqual(issues, [])


class ScenarioReturnTests(unittest.TestCase):
    def test_local_and_fx_returns_compound(self):
        self.assertAlmostEqual(usd_scenario_return(0.10, -0.05), 0.045)
        self.assertAlmostEqual(usd_scenario_return(0.0, 0.0), 0.0)
        self.assertIsInstance(usd_scenario_return(0.10, -0.05), float)
        with self.assertRaises(ValueError):
            usd_scenario_return(float("nan"), 0.0)


if __name__ == "__main__":
    unittest.main()
