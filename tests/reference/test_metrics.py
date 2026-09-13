import unittest
from copy import deepcopy

import numpy as np
import pandas as pd

from portfolio_lab.config import DEFAULTS
from portfolio_lab.metrics import (
    exposures,
    portfolio_covariance,
    reconcile,
    risk_analysis,
    scenario_analysis,
    score_securities,
    stress_returns,
)


def sample():
    config = deepcopy(DEFAULTS)
    config["mandate"]["base_currency"] = "USD"
    config["allocation"]["cash_return"] = 0.0
    config["signals"]["min_sector_size"] = 2
    config["mandate"]["benchmark_id"] = "ETF"
    dates = pd.bdate_range("2022-01-03", "2026-08-31")
    rng = np.random.default_rng(25)
    common = rng.normal(0.0001, 0.008, len(dates))
    prices, securities, fundamentals, forecasts = [], [], [], []
    for index, sid in enumerate(["A", "A2", "B", "C", "ETF"]):
        price = 100 * np.exp(np.cumsum(common + rng.normal(0, 0.003, len(dates))))
        prices.append(
            pd.DataFrame(
                {
                    "date": dates.strftime("%Y-%m-%d"),
                    "security_id": sid,
                    "close": price,
                    "adjusted_close": price,
                    "volume": (3_000_000 if sid == "A" else 2_000_000),
                    "currency": "USD",
                    "available_at": dates.strftime("%Y-%m-%dT20:00:00Z"),
                    "received_at": dates.strftime("%Y-%m-%dT20:00:00Z"),
                    "source_id": "fixture",
                }
            )
        )
        securities.append(
            {
                "security_id": sid,
                "ticker": sid,
                "issuer_id": "A" if sid == "A2" else sid,
                "sector": "Technology",
                "instrument_type": "etf" if sid == "ETF" else "equity",
                "currency": "USD",
                "domicile": "US",
                "equity_type": "ordinary_common",
                "market_cap": 10_000_000_000,
                "market_cap_as_of": "2026-08-31",
                "market_cap_available_at": "2026-08-31T20:00:00Z",
                "market_cap_received_at": "2026-08-31T20:00:00Z",
                "eligible": True,
            }
        )
        fundamentals.append(
            {
                "security_id": sid,
                "period_end": "2026-06-30",
                "available_at": "2026-08-01",
                "received_at": "2026-08-01",
                "currency": "USD",
                "gross_profit": 3e9 + index * 1e8,
                "operating_income": 1e9,
                "net_income": 8e8,
                "income_common": 8e8,
                "earnings_definition": "common_shareholders",
                "operating_cash_flow": 1.1e9,
                "capex": 2e8,
                "assets": 10e9,
                "assets_begin": 9e9,
            }
        )
        for label, ret, probability in [
            ("Adverse", -0.25, 0.25),
            ("Central", 0.1, 0.5),
            ("Favorable", 0.3, 0.25),
        ]:
            forecasts.append(
                {
                    "security_id": sid,
                    "scenario": label,
                    "horizon_months": 12,
                    "return_value": ret,
                    "probability": probability,
                    "basis": "subjective",
                    "source": "test",
                    "forecast_date": "2026-08-31",
                    "calibration_id": None,
                }
            )
    bundle = {
        "as_of": "2026-08-31",
        "mode": "offline",
        "issues": [],
        "sources": [],
        "accounts": pd.DataFrame(
            [
                {
                    "account_id": "X",
                    "account_type": "retirement",
                    "currency": "USD",
                    "total_value": 1000.0,
                    "cash": 100.0,
                    "complete": True,
                }
            ]
        ),
        "positions": pd.DataFrame(
            [
                {
                    "account_id": "X",
                    "security_id": sid,
                    "market_value": value,
                    "currency": "USD",
                    "valuation_date": "2026-08-31",
                    "quantity": None,
                    "price": None,
                    "reported_weight": value / 1000,
                }
                for sid, value in [("A", 300.0), ("A2", 100.0), ("ETF", 500.0)]
            ]
        ),
        "securities": pd.DataFrame(securities),
        "prices": pd.concat(prices, ignore_index=True),
        "fundamentals": pd.DataFrame(fundamentals),
        "forecasts": pd.DataFrame(forecasts),
        "fund_holdings": pd.DataFrame(
            [
                {
                    "fund_id": "ETF",
                    "issuer_id": sid,
                    "weight": weight,
                    "holdings_date": "2026-08-01",
                    "available_at": "2026-08-05",
                }
                for sid, weight in [("A", 0.4), ("B", 0.3), ("C", 0.3)]
            ]
        ),
    }
    return bundle, config


def test_reconciliation_preserves_unknown_residual_and_missing_cash(sample):
    bundle, config = sample
    bundle["positions"] = bundle["positions"].iloc[:2]
    result = reconcile(bundle, config)
    assert result["summary"]["coverage"] == 0.5
    assert result["summary"]["known_cash"] == 100
    assert result["summary"]["unclassified_value"] == 500
    assert not result["summary"]["complete"]
    bundle["accounts"].loc[0, "cash"] = np.nan
    result = reconcile(bundle, config)
    assert result["summary"]["unknown_cash_accounts"] == ["X"]
    assert result["summary"]["unclassified_value"] == 600
    assert risk_analysis(bundle, config)["annualized_volatility"] is None


def test_currency_mismatch_never_sums_unconverted_assets(sample):
    bundle, config = sample
    bundle["positions"].loc[0, "currency"] = "CAD"
    result = reconcile(bundle, config)
    assert result["summary"]["total_value"] is None
    assert not result["summary"]["complete"]


def test_dual_classes_and_fund_lookthrough_do_not_doublecount(sample):
    bundle, config = sample
    result = exposures(bundle, config)
    issuers = {row["issuer_id"]: row for row in result["issuer_exposure"]}
    assert issuers["A"]["market_value"] == 600
    assert issuers["A"]["direct_value"] == 400
    assert issuers["A"]["indirect_value"] == 200
    assert "ETF" not in issuers
    assert sum(row["market_value"] for row in result["sector_exposure"]) == 1000
    assert result["status"] == "complete"


def test_partial_fund_holdings_are_unclassified_not_renormalized(sample):
    bundle, config = sample
    bundle["fund_holdings"] = bundle["fund_holdings"].iloc[:1]
    result = exposures(bundle, config)
    assert result["unclassified_value"] == 300
    assert result["status"] == "incomplete"
    shocks = stress_returns(bundle, config, ["ETF"])
    assert shocks["growth_reversal"]["ETF"] is None
    assert shocks["broad_equity"]["ETF"] == -0.3


def test_explicit_fund_cash_is_cash_and_receives_no_equity_shock(sample):
    bundle, config = sample
    bundle["fund_holdings"].loc[0, "issuer_id"] = "CASH"
    result = exposures(bundle, config)
    assert "CASH" not in {row["issuer_id"] for row in result["issuer_exposure"]}
    sectors = {row["sector"]: row["market_value"] for row in result["sector_exposure"]}
    assert sectors["Cash"] == 300
    assert sectors["Technology"] == 700
    assert result["status"] == "complete"
    shocks = stress_returns(bundle, config, ["ETF"])
    np.testing.assert_allclose(shocks["broad_equity"]["ETF"], -0.3 * 0.6)
    np.testing.assert_allclose(shocks["growth_reversal"]["ETF"], -0.4 * 0.6)


def test_universe_scoring_uses_single_issuer_and_missing_families(sample):
    bundle, config = sample
    scores = score_securities(bundle, config).set_index("security_id")
    assert bool(scores.loc["A", "is_representative"])
    assert not bool(scores.loc["A2", "is_representative"])
    assert scores.loc["A", "score"] == scores.loc["A2", "score"]
    # B and C are not held, but participate in the research cross-section.
    assert pd.notna(scores.loc["B", "score"])
    assert pd.notna(scores.loc["C", "score"])
    config["signals"]["min_sector_size"] = 4
    scores = score_securities(bundle, config).set_index("security_id")
    # There are four classes but only three corporate issuers.
    assert pd.isna(scores.loc["A", "quality"])
    assert pd.isna(scores.loc["A", "score"])
    assert pd.notna(scores.loc["A", "momentum"])


def test_momentum_uses_adjusted_close_and_does_not_add_dividends(sample):
    bundle, config = sample
    before = score_securities(bundle, config).set_index("security_id")
    bundle["prices"]["dividends"] = 5000.0
    after = score_securities(bundle, config).set_index("security_id")
    assert before.loc["A", "raw_momentum"] == after.loc["A", "raw_momentum"]
    prices = bundle["prices"]
    aa = prices[prices.security_id == "A"]
    start = aa[aa.date.str.startswith("2025-08")].iloc[-1].adjusted_close
    end = aa[aa.date.str.startswith("2026-07")].iloc[-1].adjusted_close
    np.testing.assert_allclose(after.loc["A", "raw_momentum"], end / start - 1)


def test_future_prices_and_future_statement_versions_are_excluded(sample):
    bundle, config = sample
    scores = score_securities(bundle, config).set_index("security_id")
    covariance = portfolio_covariance(bundle, config, ["A", "B"])
    future = bundle["prices"].iloc[[-1]].copy()
    future["security_id"] = "A"
    future["date"] = "2026-09-01"
    future["available_at"] = "2026-09-01T20:00:00Z"
    future["adjusted_close"] = 1e8
    bundle["prices"] = pd.concat([bundle["prices"], future], ignore_index=True)
    amended = bundle["fundamentals"].iloc[[0]].copy()
    amended["available_at"] = "2026-09-01"
    amended["gross_profit"] = 1e20
    bundle["fundamentals"] = pd.concat([bundle["fundamentals"], amended], ignore_index=True)
    revised = score_securities(bundle, config).set_index("security_id")
    assert revised.loc["A", "gross_profitability"] == scores.loc["A", "gross_profitability"]
    np.testing.assert_array_equal(
        portfolio_covariance(bundle, config, ["A", "B"])["matrix"], covariance["matrix"]
    )


def test_unknown_risk_asset_prevents_partial_covariance(sample):
    bundle, config = sample
    covariance = portfolio_covariance(bundle, config, ["A", "DOES_NOT_EXIST"])
    assert covariance["matrix"] is None
    assert not covariance["complete"]
    bundle["prices"] = bundle["prices"][
        ~((bundle["prices"].security_id == "A2") & (bundle["prices"].date < "2026-01-01"))
    ]
    result = risk_analysis(bundle, config)
    assert result["annualized_volatility"] is None
    assert result["status"] == "incomplete"


def test_risk_common_panel_is_psd_and_cash_is_explicit(sample):
    bundle, config = sample
    cov = portfolio_covariance(bundle, config, ["A", "A2", "ETF"])
    assert cov["complete"]
    assert cov["observations"] >= 104
    assert np.linalg.eigvalsh(cov["matrix"]).min() >= -1e-12
    result = risk_analysis(bundle, config)
    w = np.array([0.3, 0.1, 0.5])
    np.testing.assert_allclose(result["annualized_volatility"], np.sqrt(w @ cov["matrix"] @ w))
    assert result["max_drawdown"] <= 0
    assert len(result["history"]) <= 200
    assert "not actual" in result["history_label"]
    assert result["volatility_interval"]["lower"] < result["volatility_interval"]["upper"]
    assert "benchmark" in result["history"][0]


def test_scenario_uses_common_probabilities_and_explicit_cash(sample):
    bundle, config = sample
    result = scenario_analysis(bundle, config)
    expected = 0.9 * (0.25 * -0.25 + 0.5 * 0.1 + 0.25 * 0.3)
    assert result["status"] == "complete"
    np.testing.assert_allclose(result["weighted_return"], expected)
    assert result["basis"] == "subjective"
    bundle["forecasts"].loc[0, "probability"] = 0.35
    result = scenario_analysis(bundle, config)
    assert result["status"] == "invalid"
    assert result["weighted_return"] is None


def test_missing_or_future_benchmark_forecasts_block_comparison(sample):
    bundle, config = sample
    bundle["forecasts"].loc[bundle["forecasts"].security_id == "ETF", "forecast_date"] = (
        "2026-09-01"
    )
    result = scenario_analysis(bundle, config)
    assert result["status"] == "incomplete"
    assert result["weighted_return"] is None
    assert any(issue["code"] == "missing_forecast_assets" for issue in result["issues"])


def test_scenario_override_invalid_and_subjective_not_calibrated(sample):
    bundle, config = sample
    config["allocation"]["probability_overrides"] = {
        "Adverse": 0.4,
        "Central": 0.5,
        "Favorable": 0.2,
    }
    assert scenario_analysis(bundle, config)["status"] == "invalid"
    config["allocation"]["probability_overrides"] = {
        "Adverse": 0.2,
        "Central": 0.5,
        "Favorable": 0.3,
    }
    bundle["forecasts"]["basis"] = "calibrated"
    bundle["forecasts"]["calibration_id"] = "test_record"
    result = scenario_analysis(bundle, config)
    assert result["status"] == "complete"
    assert result["basis"] == "subjective"


def test_actual_observation_replay_requires_receipt_time(sample):
    bundle, config = sample
    config["data"]["require_received_by_cutoff"] = True
    bundle["prices"]["received_at"] = "2026-09-13"
    result = portfolio_covariance(bundle, config, ["A"])
    assert not result["complete"]


def test_undated_market_cap_and_partial_newer_forecasts_are_rejected(sample):
    bundle, config = sample
    bundle["securities"]["market_cap_as_of"] = None
    assert score_securities(bundle, config).score.isna().all()
    bundle["forecasts"].loc[0, "forecast_date"] = "2026-08-30"
    result = scenario_analysis(bundle, config)
    assert result["status"] == "invalid"


def test_candidate_overrides_and_objective_shrinkage(sample):
    bundle, config = sample
    config["allocation"]["return_overrides"] = {"B": {"Central": 0.5}}
    assert scenario_analysis(bundle, config)["status"] == "complete"
    config["allocation"]["forecast_shrinkage"] = 0.5
    config["allocation"]["prior_returns"] = {"A": 0.02, "A2": 0.02, "ETF": 0.02}
    result = scenario_analysis(bundle, config)
    assert result["status"] == "complete"
    np.testing.assert_allclose(
        result["objective_weighted_return"], 0.5 * result["weighted_return"] + 0.5 * 0.9 * 0.02
    )


def test_statement_currency_cannot_be_combined_with_usd_market_cap(sample):
    bundle, config = sample
    bundle["fundamentals"].loc[bundle["fundamentals"].security_id == "A", "currency"] = "JPY"
    scores = score_securities(bundle, config).set_index("security_id")
    assert pd.isna(scores.loc["A", "score"])
    assert pd.isna(scores.loc["A", "earnings_yield"])


def test_proposed_cost_reserve_and_identical_covariance_universe(sample):
    bundle, config = sample
    weights = pd.DataFrame(
        [
            {"account_id": "X", "security_id": sid, "weight": weight}
            for sid, weight in [("A", 0.3), ("A2", 0.1), ("ETF", 0.5), ("CASH", 0.09)]
        ]
    )
    assert risk_analysis(bundle, config, weights)["status"] == "incomplete"
    weights.attrs["reserved_cost_weight_by_account"] = {"X": 0.01}
    weights.attrs["covariance_security_ids"] = ["A", "A2", "B", "C", "ETF"]
    result = risk_analysis(bundle, config, weights)
    cov = portfolio_covariance(bundle, config, weights.attrs["covariance_security_ids"])
    w = np.array([0.3, 0.1, 0, 0, 0.5])
    np.testing.assert_allclose(result["annualized_volatility"], np.sqrt(w @ cov["matrix"] @ w))
    scenarios = scenario_analysis(bundle, config, weights)
    np.testing.assert_allclose(
        scenarios["weighted_return"] - scenarios["net_weighted_return"], 0.01
    )


class MetricsTests(unittest.TestCase):
    """Fresh deterministic fixtures for each invariant, without pytest dependency."""


for _name, _function in list(globals().items()):
    if _name.startswith("test_") and callable(_function):
        setattr(MetricsTests, _name, lambda self, test=_function: test(sample()))
del _name, _function


if __name__ == "__main__":
    unittest.main()
