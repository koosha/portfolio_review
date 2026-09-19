"""Proposed valuation inputs must verify against the engines before they are offered."""

import math
import unittest

from portfolio_research.model_adapter import NullModelAdapter, load_model_adapter
from portfolio_research.proposals import propose_dcf_inputs, propose_eps_model
from portfolio_research.valuation import calculate_dcf, calculate_eps

ESTIMATE_SOURCE = "yahoo_estimates:" + "a" * 64
STATEMENT_SOURCE = "yahoo_statements:" + "b" * 64


def equity_security():
    return {
        "security_id": "SEC-1",
        "name": "Example Manufacturing",
        "instrument_type": "equity",
        "equity_type": "ordinary_common",
        "sector": "Industrials",
        "currency": "USD",
    }


def estimates_payload():
    return {
        "security_id": "SEC-1",
        "currency": "USD",
        "eps": {
            "0q": {
                "avg": 1.1,
                "low": 1.0,
                "high": 1.2,
                "year_ago": 1.0,
                "analysts": 9,
                "growth": 0.10,
            },
            "+1q": {
                "avg": 1.2,
                "low": 1.1,
                "high": 1.3,
                "year_ago": 1.1,
                "analysts": 9,
                "growth": 0.09,
            },
            "0y": {
                "avg": 4.5,
                "low": 4.2,
                "high": 4.8,
                "year_ago": 4.1,
                "analysts": 11,
                "growth": 0.10,
            },
            "+1y": {
                "avg": 5.0,
                "low": 4.0,
                "high": 6.5,
                "year_ago": 4.5,
                "analysts": 12,
                "growth": 0.11,
            },
        },
        "revenue": {
            "0y": {
                "avg": 1_040_000_000.0,
                "low": 1_020_000_000.0,
                "high": 1_060_000_000.0,
                "year_ago": 1_000_000_000.0,
                "analysts": 10,
                "growth": 0.04,
            },
            "+1y": {
                "avg": 1_123_200_000.0,
                "low": 1_080_000_000.0,
                "high": 1_160_000_000.0,
                "year_ago": 1_040_000_000.0,
                "analysts": 10,
                "growth": 0.08,
            },
        },
        "price_targets": {
            "current": 100.0,
            "low": 80.0,
            "high": 140.0,
            "mean": 115.0,
            "median": 112.0,
        },
        "recommendations": {
            "period": "0m",
            "strong_buy": 5,
            "buy": 7,
            "hold": 3,
            "sell": 1,
            "strong_sell": 0,
        },
        "earnings_dates": ["2025-11-04"],
        "ex_dividend_date": "2025-10-10",
        "received_at": "2025-09-14T12:00:00+00:00",
        "source_id": ESTIMATE_SOURCE,
        "label": "External consensus estimate; not an established outcome",
    }


def ttm_payload(**overrides):
    row = {
        "security_id": "SEC-1",
        "period_end": "2025-06-30",
        "currency": "USD",
        "revenue": 1_000_000_000.0,
        "gross_profit": 400_000_000.0,
        "operating_income": 200_000_000.0,
        "net_income": 150_000_000.0,
        "income_common": 150_000_000.0,
        "operating_cash_flow": 220_000_000.0,
        "capex": 60_000_000.0,
        "depreciation": 40_000_000.0,
        "change_working_capital": 10_000_000.0,
        "tax_provision": 40_000_000.0,
        "pretax_income": 160_000_000.0,
        "stock_compensation": 20_000_000.0,
        "assets": 1_500_000_000.0,
        "assets_begin": 1_400_000_000.0,
        "cash": 300_000_000.0,
        "debt": 400_000_000.0,
        "diluted_shares": 100_000.0,
        "source_id": STATEMENT_SOURCE,
        "received_at": "2025-08-02T00:00:00+00:00",
    }
    row.update(overrides)
    return row


def annual_rows():
    return [
        {
            "security_id": "SEC-1",
            "period_end": "2024-12-31",
            "currency": "USD",
            "revenue": 960_000_000.0,
            "operating_income": 185_000_000.0,
            "net_income": 140_000_000.0,
            "depreciation": 38_000_000.0,
            "capex": 57_000_000.0,
            "change_working_capital": 9_000_000.0,
            "tax_provision": 37_000_000.0,
            "pretax_income": 150_000_000.0,
            "stock_compensation": 18_000_000.0,
            "assets": 1_400_000_000.0,
            "cash": 280_000_000.0,
            "debt": 390_000_000.0,
            "source_id": STATEMENT_SOURCE,
            "received_at": "2025-02-14T00:00:00+00:00",
        }
    ]


def defaults():
    return {"discount_rate": 0.09, "terminal_growth_rate": 0.025, "projection_years": 5}


class EpsProposalTest(unittest.TestCase):
    def test_proposal_is_ready_and_matches_hand_computed_return(self):
        issues = []
        payload = propose_eps_model(
            equity_security(),
            price_major=100.0,
            quote_currency="USD",
            estimates=estimates_payload(),
            ttm=ttm_payload(),
            dividends_ttm_per_share=1.25,
            horizon_months=12,
            issues=issues,
        )
        self.assertIsNotNone(payload)
        self.assertEqual(issues, [])
        result = calculate_eps(payload)
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["eps_convention"], "forward")
        self.assertEqual(result["pe_convention"], "forward")
        self.assertEqual(result["currency"], "USD")
        by_label = {row["label"]: row for row in result["scenarios"]}
        self.assertEqual([row["pe"] for row in result["scenarios"]], [20.0, 20.0, 20.0])
        self.assertEqual(
            [by_label[label]["eps"] for label in ("adverse", "central", "favorable")],
            [4.0, 5.0, 6.5],
        )
        for row in result["scenarios"]:
            expected = (row["eps"] * row["pe"] + 1.25) / 100.0 - 1
            self.assertAlmostEqual(row["total_return"], expected, places=12)
        self.assertAlmostEqual(by_label["central"]["total_return"], 0.0125, places=12)

    def test_six_month_horizon_uses_the_current_year_estimate(self):
        payload = propose_eps_model(
            equity_security(),
            price_major=100.0,
            quote_currency="USD",
            estimates=estimates_payload(),
            ttm=ttm_payload(),
            dividends_ttm_per_share=1.25,
            horizon_months=6,
        )
        self.assertEqual(payload["horizon_months"], 6)
        self.assertEqual(payload["proposal_meta"]["estimate_period"], "0y")
        eps = {row["label"]: row["eps"] for row in payload["scenarios"]}
        self.assertEqual(eps, {"adverse": 4.2, "central": 4.5, "favorable": 4.8})
        self.assertAlmostEqual(payload["scenarios"][0]["pe"], 100.0 / 4.5, places=12)

    def test_proposal_meta_names_the_visible_assumptions(self):
        payload = propose_eps_model(
            equity_security(),
            price_major=100.0,
            quote_currency="USD",
            estimates=estimates_payload(),
            ttm=ttm_payload(),
            dividends_ttm_per_share=1.25,
            horizon_months=12,
        )
        meta = payload["proposal_meta"]
        self.assertEqual(meta["basis"], "proposed")
        self.assertEqual(meta["assumptions_visible"], ["eps_growth", "terminal_multiple"])
        self.assertIn("not a joint state", meta["coherence"])
        self.assertEqual(meta["evidence"]["eps_central"], ESTIMATE_SOURCE)
        self.assertEqual(meta["evidence"]["distributions_per_starting_share"], STATEMENT_SOURCE)

    def test_non_equity_instrument_is_refused_with_a_reason(self):
        issues = []
        fund = dict(equity_security(), instrument_type="etf", equity_type=None)
        payload = propose_eps_model(
            fund,
            price_major=100.0,
            quote_currency="USD",
            estimates=estimates_payload(),
            ttm=ttm_payload(),
            dividends_ttm_per_share=1.25,
            horizon_months=12,
            issues=issues,
        )
        self.assertIsNone(payload)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["security_id"], "SEC-1")
        self.assertIn("equity", issues[0]["detail"].lower())

    def test_non_positive_consensus_is_refused_rather_than_raising(self):
        issues = []
        estimates = estimates_payload()
        estimates["eps"]["+1y"] = dict(estimates["eps"]["+1y"], avg=-0.5, low=-1.5, high=0.4)
        payload = propose_eps_model(
            equity_security(),
            price_major=100.0,
            quote_currency="USD",
            estimates=estimates,
            ttm=ttm_payload(),
            dividends_ttm_per_share=1.25,
            horizon_months=12,
            issues=issues,
        )
        self.assertIsNone(payload)
        self.assertTrue(issues and issues[0]["detail"])

    def test_estimate_currency_mismatch_is_refused(self):
        issues = []
        payload = propose_eps_model(
            equity_security(),
            price_major=100.0,
            quote_currency="CAD",
            estimates=estimates_payload(),
            ttm=ttm_payload(),
            dividends_ttm_per_share=1.25,
            horizon_months=12,
            issues=issues,
        )
        self.assertIsNone(payload)
        self.assertIn("CAD", issues[0]["detail"])

    def test_missing_estimate_returns_none(self):
        self.assertIsNone(
            propose_eps_model(
                equity_security(),
                price_major=100.0,
                quote_currency="USD",
                estimates=None,
                ttm=ttm_payload(),
                dividends_ttm_per_share=1.25,
                horizon_months=12,
            )
        )


class DcfProposalTest(unittest.TestCase):
    def build(self, **overrides):
        kwargs = {
            "ttm": ttm_payload(),
            "annual_statements": annual_rows(),
            "shares_outstanding": 100_000_000.0,
            "price_major": 100.0,
            "currency": "USD",
            "estimates": estimates_payload(),
            "defaults": defaults(),
        }
        kwargs.update(overrides)
        security = kwargs.pop("security", equity_security())
        return propose_dcf_inputs(security, **kwargs)

    def test_proposal_passes_the_engine_with_an_exact_capital_roll_forward(self):
        payload = self.build()
        self.assertIsNotNone(payload)
        self.assertEqual(payload["company_type"], "nonfinancial")
        self.assertEqual(payload["monetary_unit"], "units")
        self.assertEqual(payload["sbc_treatment"], "expensed_in_ebit")
        self.assertEqual(len(payload["projections"]), 5)
        rows = payload["projections"]
        for current, following in zip(rows, rows[1:]):
            expected = (
                current["invested_capital"]
                + current["capex"]
                - current["depreciation"]
                + current["change_working_capital"]
            )
            self.assertTrue(
                math.isclose(following["invested_capital"], expected, rel_tol=1e-6, abs_tol=1e-8)
            )
        result = calculate_dcf(payload)
        self.assertEqual(result["status"], "ready")
        self.assertGreater(result["value_per_share"], 0)
        self.assertEqual(result["issues"], [])

    def test_inputs_follow_the_trailing_twelve_month_structure(self):
        payload = self.build()
        first = payload["projections"][0]
        self.assertAlmostEqual(first["revenue"], 1_080_000_000.0, places=6)
        self.assertAlmostEqual(first["ebit"] / first["revenue"], 0.20, places=12)
        self.assertAlmostEqual(first["tax_rate"], 0.25, places=12)
        self.assertAlmostEqual(first["capex"] / first["revenue"], 0.06, places=12)
        self.assertAlmostEqual(first["depreciation"] / first["revenue"], 0.04, places=12)
        self.assertAlmostEqual(first["change_working_capital"] / first["revenue"], 0.01, places=12)
        self.assertAlmostEqual(first["invested_capital"], 1_200_000_000.0, places=6)
        self.assertAlmostEqual(payload["terminal_growth_rate"], 0.025, places=12)
        self.assertAlmostEqual(payload["discount_rate"], 0.09, places=12)
        self.assertAlmostEqual(payload["terminal_roic"], 0.125, places=12)
        self.assertAlmostEqual(payload["diluted_shares"], 100_000_000.0, places=6)

    def test_absent_bridge_lines_become_explicit_zeros_with_evidence(self):
        payload = self.build()
        self.assertEqual(payload["preferred"], 0)
        self.assertEqual(payload["nci"], 0)
        self.assertEqual(payload["nonoperating_assets"], 0)
        self.assertAlmostEqual(payload["debt"], 400_000_000.0, places=6)
        self.assertAlmostEqual(payload["excess_cash"], 300_000_000.0, places=6)
        notes = " ".join(payload["proposal_meta"]["notes"])
        self.assertIn("line absent; explicit zero proposed", notes)
        self.assertIn("all cash treated as excess", notes)

    def test_proposal_meta_names_the_consequential_assumptions(self):
        meta = self.build()["proposal_meta"]
        self.assertEqual(meta["basis"], "proposed")
        self.assertEqual(
            meta["assumptions_visible"],
            ["discount_rate", "terminal_growth_rate", "margins", "reinvestment", "terminal_roic"],
        )
        self.assertIn("convergence assumption", meta["convergence"])
        self.assertEqual(meta["evidence"]["revenue"], STATEMENT_SOURCE)
        self.assertEqual(meta["evidence"]["revenue_growth"], ESTIMATE_SOURCE)
        self.assertEqual(meta["verification"]["status"], "ready")

    def test_a_quote_currency_that_differs_from_the_statements_is_never_compared(self):
        """Statements in USD against a CAD quote: dividing across units is not a change."""
        payload = self.build(quote_currency="CAD", price_major=210.0)
        meta = payload["proposal_meta"]
        self.assertEqual(payload["currency"], "USD")
        self.assertEqual(meta["price_currency"], "CAD")
        self.assertAlmostEqual(meta["price_major"], 210.0)
        self.assertIsNone(meta["implied_change_vs_price"])
        self.assertIn("CAD", " ".join(meta["notes"]))

    def test_a_matching_quote_currency_states_the_implied_change(self):
        payload = self.build(quote_currency="USD")
        meta = payload["proposal_meta"]
        self.assertEqual(meta["price_currency"], "USD")
        self.assertAlmostEqual(
            meta["implied_change_vs_price"],
            meta["verification"]["value_per_share"] / 100.0 - 1,
        )

    def test_an_unstated_quote_currency_is_not_assumed_to_match(self):
        meta = self.build()["proposal_meta"]
        self.assertIsNone(meta["price_currency"])
        self.assertIsNone(meta["implied_change_vs_price"])

    def test_financial_sector_is_refused_with_a_reason(self):
        for sector in ("Financials", "Real Estate"):
            issues = []
            payload = self.build(security=dict(equity_security(), sector=sector), issues=issues)
            self.assertIsNone(payload)
            self.assertIn(sector.lower(), issues[0]["detail"].lower())

    def test_engine_rejection_returns_none_instead_of_raising(self):
        issues = []
        payload = self.build(ttm=ttm_payload(assets=300_000_000.0), issues=issues)
        self.assertIsNone(payload)
        self.assertTrue(issues[0]["detail"])

    def test_negative_capex_is_not_silently_converted(self):
        issues = []
        payload = self.build(ttm=ttm_payload(capex=-60_000_000.0), issues=issues)
        self.assertIsNone(payload)
        self.assertIn("capex", issues[0]["detail"].lower())

    def test_share_scaling_mismatch_is_refused(self):
        issues = []
        payload = self.build(shares_outstanding=137_000.0, issues=issues)
        self.assertIsNone(payload)
        self.assertIn("share", issues[0]["detail"].lower())

    def test_unchecked_share_scale_is_refused_rather_than_assumed(self):
        issues = []
        payload = self.build(shares_outstanding=None, issues=issues)
        self.assertIsNone(payload)
        self.assertIn("share", issues[0]["detail"].lower())


class ModelAdapterTest(unittest.TestCase):
    def test_null_adapter_is_the_default(self):
        for config in ({}, {"data": {}}, {"data": {"model_provider": None}}):
            adapter = load_model_adapter(config)
            self.assertIsInstance(adapter, NullModelAdapter)
            self.assertIsNone(adapter.summarize([], {"symbol": "ABC", "name": "Example"}))

    def test_importable_class_path_is_loaded(self):
        adapter = load_model_adapter(
            {"data": {"model_provider": "portfolio_research.model_adapter.NullModelAdapter"}}
        )
        self.assertIsInstance(adapter, NullModelAdapter)

    def test_non_importable_provider_is_refused(self):
        for name in (
            "no_such_module.Adapter",
            "portfolio_research.model_adapter.MissingAdapter",
            "portfolio_research.model_adapter.load_model_adapter",
            "collections.OrderedDict",
            "NotAPath",
            123,
        ):
            with self.subTest(name=name), self.assertRaises(ValueError):
                load_model_adapter({"data": {"model_provider": name}})


if __name__ == "__main__":
    unittest.main()
