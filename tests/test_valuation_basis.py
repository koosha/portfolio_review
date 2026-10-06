"""A currency conversion must preserve the exact listing's share denominator."""

import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

from portfolio_research import market_data
from portfolio_research.market_payloads import _estimates_payload
from portfolio_research.proposals import propose_dcf_inputs, propose_eps_model
from portfolio_research.valuation import calculate_dcf
from portfolio_research.valuation_basis import translate_per_share
from tests.support.market_data_adapter import FakeTicker
from tests.test_proposals import defaults, equity_security, estimates_payload, ttm_payload


def ratio_record():
    return {
        "ordinary_shares_per_listed_share": 8,
        "effective_from": "2019-07-15",
        "available_at": "2026-05-13T10:00:00+00:00",
        "received_at": "2026-05-13T11:00:00+00:00",
        "source_id": "issuer:share-ratio",
    }


def fx_record():
    return {
        "base": "CNY",
        "quote": "USD",
        "pair": "CNYUSD",
        "rate": "0.15",
        "date": "2026-08-31",
        "published_at": "2026-08-31T15:00:00+00:00",
        "received_at": "2026-08-31T15:05:00+00:00",
        "source_id": "fx:retained",
        "provider": "verified_fixture",
    }


def context():
    return {
        "as_of": "2026-08-31",
        "cutoff": "2026-08-31T20:00:00+00:00",
        "listing_basis": ratio_record(),
        "fx_observations": [fx_record()],
    }


class ValuationBasisTests(unittest.TestCase):
    def bridge(self, value=5.5, **changes):
        args = dict(
            from_currency="CNY", quote_currency="USD", per_share_basis="ordinary_share", **context()
        )
        args.update(changes)
        return translate_per_share(value, **args)

    def test_ordinary_eps_becomes_listed_eps_with_two_distinct_sourced_bridges(self):
        result = self.bridge()
        self.assertAlmostEqual(result["value"], 6.6)
        self.assertEqual(result["share_multiplier"], 8)
        self.assertEqual(result["share_ratio_source_id"], "issuer:share-ratio")
        self.assertEqual(result["fx"]["source_id"], "fx:retained")
        self.assertEqual(result["per_share_basis"], "listed_share")

    def test_already_listed_eps_is_not_multiplied_by_the_ads_ratio_again(self):
        self.assertAlmostEqual(self.bridge(44, per_share_basis="listed_share")["value"], 6.6)

    def test_undeclared_units_missing_ratio_or_expired_ratio_are_not_guessed(self):
        for changed in (
            {"per_share_basis": None},
            {"listing_basis": None},
            {"listing_basis": {**ratio_record(), "effective_to": "2026-08-31"}},
            {"listing_basis": {**ratio_record(), "source_id": None}},
            {"listing_basis": {**ratio_record(), "available_at": "2026-09-01"}},
            {"listing_basis": {**ratio_record(), "received_at": "2030-01-01"}},
        ):
            with self.subTest(changed=changed):
                self.assertIsNone(self.bridge(**changed)["value"])

    def test_missing_stale_future_or_late_received_fx_cannot_enter_the_bridge(self):
        for changed in (
            [],
            [{**fx_record(), "date": "2026-07-01"}],
            [{**fx_record(), "date": "2026-09-01"}],
            [{**fx_record(), "published_at": "2026-08-31T21:00:00+00:00"}],
            [{**fx_record(), "received_at": "2030-01-01T00:00:00+00:00"}],
        ):
            self.assertIsNone(self.bridge(fx_observations=changed)["value"])

    def test_subunit_conversion_precedes_the_listing_and_fx_bridges(self):
        result = self.bridge(
            100, from_currency="GBp", quote_currency="GBP", per_share_basis="listed_share"
        )
        self.assertEqual(result["value"], 1)
        self.assertEqual(result["input_unit_factor"], 100)

    def test_an_explicit_unsupported_basis_cannot_use_the_legacy_listing_contract(self):
        result = self.bridge(
            from_currency="USD",
            per_share_basis="per_thousand_ordinary_share",
            same_currency_listing_contract=True,
        )
        self.assertIsNone(result["value"])

    def test_overflow_is_unavailable_instead_of_an_infinite_forecast(self):
        result = self.bridge(1e308, from_currency="USD", quote_currency="USD")
        self.assertIsNone(result["value"])

    def test_a_frozen_receipt_bound_is_not_extended_by_current_mode(self):
        result = self.bridge(
            current=True,
            receipt_limit="2026-08-31T20:00:00Z",
            fx_observations=[
                {**fx_record(), "received_at": "2026-08-31T20:15:00Z"},
            ],
        )
        self.assertIsNone(result["value"])

    def test_dcf_keeps_enterprise_values_in_reporting_currency_and_denominator_in_ads(self):
        trailing = ttm_payload(
            currency="CNY", diluted_shares=800_000_000, share_count_basis="ordinary_share"
        )
        original = deepcopy(trailing)
        payload = propose_dcf_inputs(
            equity_security(),
            ttm=trailing,
            annual_statements=[],
            shares_outstanding=100_000_000,
            price_major=100,
            currency="CNY",
            quote_currency="USD",
            estimates=estimates_payload(),
            defaults=defaults(),
            valuation_context=context(),
            issues=[],
        )
        self.assertIsNotNone(payload)
        self.assertEqual(payload["currency"], "CNY")
        self.assertEqual(payload["diluted_shares"], 100_000_000)
        value = calculate_dcf(payload)["value_per_share"]
        self.assertAlmostEqual(payload["proposal_meta"]["quote_value_per_share"], value * 0.15)
        self.assertAlmostEqual(
            payload["proposal_meta"]["implied_change_vs_price"], value * 0.15 / 100 - 1
        )
        self.assertEqual(trailing, original)

    def test_dcf_does_not_infer_eightfold_share_ratio_from_counts(self):
        issues = []
        payload = propose_dcf_inputs(
            equity_security(),
            ttm=ttm_payload(currency="CNY", diluted_shares=800_000_000),
            annual_statements=[],
            shares_outstanding=100_000_000,
            price_major=100,
            currency="CNY",
            quote_currency="USD",
            estimates=estimates_payload(),
            defaults=defaults(),
            valuation_context=context(),
            issues=issues,
        )
        self.assertIsNone(payload)
        self.assertIn("scaling", issues[0]["detail"])

    def test_foreign_dcf_with_undeclared_share_basis_has_no_price_comparison(self):
        payload = propose_dcf_inputs(
            equity_security(),
            ttm=ttm_payload(currency="CNY", diluted_shares=100_000_000),
            annual_statements=[],
            shares_outstanding=100_000_000,
            price_major=100,
            currency="CNY",
            quote_currency="USD",
            estimates=estimates_payload(),
            defaults=defaults(),
            valuation_context=context(),
            issues=[],
        )
        self.assertIsNotNone(payload)
        self.assertIsNone(payload["proposal_meta"]["implied_change_vs_price"])

    def test_foreign_usd_dcf_cannot_use_eps_units_as_its_share_count_units(self):
        issues = []
        payload = propose_dcf_inputs(
            {**equity_security(), "domicile": "CN"},
            ttm=ttm_payload(per_share_basis="listed_share"),
            annual_statements=[],
            shares_outstanding=100_000_000,
            price_major=100,
            currency="USD",
            quote_currency="USD",
            estimates=estimates_payload(),
            defaults=defaults(),
            valuation_context=context(),
            issues=issues,
        )
        self.assertIsNone(payload)
        self.assertIn("denominator", issues[0]["detail"])

    def test_consensus_revenue_level_cannot_be_divided_by_a_different_currency(self):
        estimates = estimates_payload()
        estimates["revenue"]["+1y"]["growth"] = None
        estimates["revenue"]["+1y"]["currency"] = "USD"
        issues = []
        payload = propose_dcf_inputs(
            equity_security(),
            ttm=ttm_payload(currency="CNY"),
            annual_statements=[],
            shares_outstanding=100_000_000,
            price_major=100,
            currency="CNY",
            estimates=estimates,
            defaults=defaults(),
            issues=issues,
        )
        self.assertIsNone(payload)
        self.assertIn("currencies", issues[0]["detail"])

    def test_eps_proposal_uses_selected_period_currency_and_declared_share_basis(self):
        estimates = estimates_payload()
        estimates["eps"]["+1y"].update(currency="CNY", per_share_basis="ordinary_share")
        payload = propose_eps_model(
            equity_security(),
            price_major=100,
            quote_currency="USD",
            estimates=estimates,
            ttm={},
            dividends_ttm_per_share=0,
            horizon_months=12,
            valuation_context=context(),
        )
        self.assertAlmostEqual(payload["scenarios"][1]["eps"], 5 * 8 * 0.15)
        self.assertEqual(payload["proposal_meta"]["earnings_bridge"]["share_multiplier"], 8)

    def test_raw_per_period_currency_overrides_yfinance_broadcast_and_keeps_missing_null(self):
        ticker = FakeTicker("AAPL")
        ticker._analysis = SimpleNamespace(
            _earnings_trend=[
                {
                    "period": "0y",
                    "earningsEstimate": {"earningsCurrency": "USD"},
                    "revenueEstimate": {"revenueCurrency": "CNY"},
                },
                {
                    "period": "+1y",
                    "earningsEstimate": {"earningsCurrency": "CNY"},
                    "revenueEstimate": {},
                },
            ]
        )
        payload = _estimates_payload(ticker)
        self.assertEqual(payload["eps"]["0y"]["currency"], "USD")
        self.assertEqual(payload["eps"]["+1y"]["currency"], "CNY")
        self.assertEqual(payload["revenue"]["0y"]["currency"], "CNY")
        self.assertIsNone(payload["revenue"]["+1y"]["currency"])
        self.assertIsNone(payload["eps"]["0q"]["currency"])

    def test_without_the_raw_period_units_a_new_answer_remains_unverified(self):
        ticker = FakeTicker("AAPL")
        del ticker._analysis
        payload = _estimates_payload(ticker)
        self.assertEqual(payload["unit_contract"], "legacy_unverified")
        self.assertEqual(payload["eps"]["0y"]["currency_basis"], "dataframe_currency_unverified")

    def test_update_refreshes_a_young_cache_with_the_legacy_currency_contract(self):
        old = {"eps": {"0y": {"avg": 10, "currency": "USD"}}, "revenue": {}}
        new = {**old, "unit_contract": "period-currencies-2"}
        before = deepcopy(old)
        source = {"source_id": "new:estimate"}
        with (
            patch.object(market_data, "_cache", return_value=(old, "2026-08-31T19:00:00Z", source)),
            patch.object(market_data, "_fresh", return_value=True),
            patch.object(market_data, "_live", return_value=new) as fetch,
            patch("portfolio_lab.providers._archive", return_value=source),
        ):
            result = market_data.estimates(
                {"data": {}},
                {"security_id": "EQ", "ticker": "EQ"},
                refresh=True,
                issues=[],
                as_of="2026-08-31",
            )
        fetch.assert_called_once()
        self.assertEqual(result["unit_contract"], "period-currencies-2")
        self.assertEqual(old, before)

    def test_failed_update_keeps_the_old_cache_explicitly_unverified(self):
        old = {"eps": {"0y": {"avg": 10, "currency": "USD"}}, "revenue": {}}
        with (
            patch.object(
                market_data,
                "_cache",
                return_value=(old, "2026-08-31T19:00:00Z", {"source_id": "old:estimate"}),
            ),
            patch.object(market_data, "_fresh", return_value=True),
            patch.object(market_data, "_live", return_value=None),
        ):
            result = market_data.estimates(
                {"data": {}},
                {"security_id": "EQ", "ticker": "EQ"},
                refresh=True,
                issues=[],
                as_of="2026-08-31",
            )
        self.assertEqual(result["unit_contract"], "legacy_unverified")


if __name__ == "__main__":
    unittest.main()
