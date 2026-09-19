"""Per-candidate exclusions: a screened candidate's missing evidence is its own fact.

A candidate admitted by an account-wide policy was never named by the owner, so a gap
in its record excludes that one candidate and is reported as such, instead of
withholding the whole comparison. Securities the owner holds, the benchmark, and
securities named in explicit `account_permissions` keep the existing blocking
behaviour: those are the owner's own instructions, and silence about them would hide a
gap rather than report it.

A candidate's own forecast defect — a blank probability beside stated ones, a
probability that disagrees with the shared set, a return outside the possible range or
one that is not finite — excludes that candidate and leaves the comparison standing.
Admission and the proposal build between them read each security's price once.
"""

import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

from portfolio_lab.allocation import build_proposals
from portfolio_lab.config import validate_config
from tests.reference.test_allocation import fixture

SCENARIOS = [("adverse", 0.3, -0.12), ("central", 0.7, 0.09)]


def security_row(security_id: str, sector: str = "Technology") -> dict:
    return dict(
        security_id=security_id,
        ticker=security_id,
        issuer_id="issuer_" + security_id,
        name=security_id,
        sector=sector,
        instrument_type="equity",
        currency="USD",
        eligible=True,
    )


class CandidateExclusionTests(unittest.TestCase):
    def setUp(self):
        self.bundle, self.config = fixture()
        self.scores = pd.DataFrame({"security_id": ["A", "B", "C"], "composite": [0.3, 0.7, 0.95]})
        covariance = patch(
            "portfolio_lab.metrics.portfolio_covariance",
            side_effect=lambda b, c, ids: {
                "ids": ids,
                "matrix": np.eye(len(ids)) * 0.04,
                "complete": True,
                "observations": 156,
                "issues": [],
            },
        )
        stress = patch(
            "portfolio_lab.metrics.stress_returns",
            side_effect=lambda b, c, ids: {"recession": {sid: -0.3 for sid in ids}},
        )
        covariance.start()
        stress.start()
        self.addCleanup(covariance.stop)
        self.addCleanup(stress.stop)

    def run_proposals(self) -> dict:
        return build_proposals(self.bundle, self.config, self.scores)

    def open_universe(self, *, explicit: bool = False) -> None:
        """Admit every eligible security through the account policy."""
        self.config["mandate"]["account_candidate_policy"] = {"r1": "eligible_universe"}
        if not explicit:
            self.config["mandate"]["account_permissions"] = {}

    def add_candidate(self, security_id: str, *, prices: bool, forecasts: bool) -> None:
        self.bundle["securities"] = pd.concat(
            [self.bundle["securities"], pd.DataFrame([security_row(security_id)])],
            ignore_index=True,
        )
        if prices:
            self.bundle["prices"] = pd.concat(
                [
                    self.bundle["prices"],
                    pd.DataFrame(
                        [
                            dict(
                                security_id=security_id,
                                date="2026-08-31",
                                close=50.0,
                                available_at="2026-08-31T20:00:00Z",
                                received_at="2026-08-31T20:00:00Z",
                            )
                        ]
                    ),
                ],
                ignore_index=True,
            )
        if forecasts:
            self.bundle["forecasts"] = pd.concat(
                [
                    self.bundle["forecasts"],
                    pd.DataFrame(
                        [
                            dict(
                                security_id=security_id,
                                scenario=scenario,
                                horizon_months=12,
                                return_value=value,
                                probability=probability,
                                basis="subjective",
                                source="test",
                                forecast_date="2026-08-31",
                            )
                            for scenario, probability, value in SCENARIOS
                        ]
                    ),
                ],
                ignore_index=True,
            )

    def excluded(self, result: dict) -> dict[str, list[str]]:
        return {row["security_id"]: row["reasons"] for row in result["excluded_candidates"]}

    def test_result_always_reports_a_candidate_partition(self):
        result = self.run_proposals()
        self.assertEqual(result["excluded_candidates"], [])
        self.assertEqual(result["solver"]["candidate_admission"], {"admitted": 0, "excluded": 0})

    def test_screened_candidate_without_prices_is_excluded_not_blocking(self):
        self.open_universe()
        self.add_candidate("D", prices=False, forecasts=True)
        result = self.run_proposals()
        self.assertNotEqual(result["status"], "blocked", result["issues"])
        self.assertIn("missing_price", self.excluded(result)["D"])
        self.assertNotIn("missing_trade_prices", {i["code"] for i in result["issues"]})
        self.assertEqual(result["solver"]["candidate_admission"]["excluded"], 1)

    def test_excluded_candidate_names_every_missing_family(self):
        self.open_universe()
        self.add_candidate("D", prices=False, forecasts=False)
        result = self.run_proposals()
        self.assertNotEqual(result["status"], "blocked", result["issues"])
        self.assertEqual(sorted(self.excluded(result)["D"]), ["missing_forecast", "missing_price"])

    def test_screened_candidate_without_a_sector_is_excluded(self):
        self.open_universe()
        self.add_candidate("D", prices=True, forecasts=True)
        self.bundle["securities"].loc[self.bundle["securities"].security_id == "D", "sector"] = ""
        result = self.run_proposals()
        self.assertNotEqual(result["status"], "blocked", result["issues"])
        self.assertIn("missing_exposure_coefficients", self.excluded(result)["D"])

    def test_owned_security_without_prices_still_blocks(self):
        self.open_universe()
        prices = self.bundle["prices"]
        self.bundle["prices"] = prices[prices.security_id != "A"]
        result = self.run_proposals()
        self.assertEqual(result["status"], "blocked")
        self.assertIn("missing_trade_prices", {i["code"] for i in result["issues"]})
        self.assertNotIn("A", self.excluded(result))

    def test_explicitly_permitted_security_still_blocks(self):
        """An owner-named security is an instruction, not a screened candidate."""
        self.open_universe(explicit=True)
        forecasts = self.bundle["forecasts"]
        self.bundle["forecasts"] = forecasts[forecasts.security_id != "C"]
        result = self.run_proposals()
        self.assertEqual(result["status"], "blocked")
        self.assertIn("missing_forecasts", {i["code"] for i in result["issues"]})
        self.assertNotIn("C", self.excluded(result))

    def test_policy_admits_eligible_unowned_securities_without_permissions(self):
        self.open_universe()
        result = self.run_proposals()
        codes = {i["code"] for i in result["issues"]}
        self.assertNotIn("missing_account_permissions", codes)
        self.assertNotEqual(result["status"], "blocked", result["issues"])
        self.assertEqual(result["solver"]["candidate_admission"], {"admitted": 1, "excluded": 0})
        self.assertIn("C", result["solver"]["covariance_security_ids"])

    def test_missing_permissions_still_block_without_a_policy(self):
        self.config["mandate"]["account_permissions"] = {}
        result = self.run_proposals()
        self.assertEqual(result["status"], "blocked")
        self.assertIn("missing_account_permissions", {i["code"] for i in result["issues"]})

    def test_candidate_no_account_may_hold_is_reported_as_not_permitted(self):
        self.open_universe()
        self.bundle["accounts"].loc[0, "account_type"] = "taxable"
        self.config["mandate"]["allow_taxable_proposals"] = False
        result = self.run_proposals()
        self.assertNotEqual(result["status"], "blocked", result["issues"])
        self.assertEqual(self.excluded(result)["C"], ["not_permitted"])

    def test_a_blocked_run_still_reports_what_it_learned_about_candidates(self):
        self.open_universe()
        self.bundle["accounts"].loc[0, "account_type"] = "taxable"
        self.config["mandate"]["allow_taxable_proposals"] = False
        self.config["mandate"]["benchmark_id"] = "absent"
        result = self.run_proposals()
        self.assertEqual(result["status"], "blocked")
        self.assertIn("missing_benchmark", {i["code"] for i in result["issues"]})
        self.assertEqual(self.excluded(result)["C"], ["not_permitted"])
        self.assertEqual(result["solver"]["candidate_admission"]["excluded"], 1)


class CandidatePolicyConfigTests(unittest.TestCase):
    def test_policy_defaults_to_no_screened_admission(self):
        config = validate_config({})
        self.assertEqual(config["mandate"]["account_candidate_policy"], {})

    def test_policy_accepts_the_two_documented_values(self):
        config = validate_config(
            {"mandate": {"account_candidate_policy": {"r1": "eligible_universe", "r2": "none"}}}
        )
        self.assertEqual(
            config["mandate"]["account_candidate_policy"],
            {"r1": "eligible_universe", "r2": "none"},
        )

    def test_unknown_policy_value_is_rejected(self):
        with self.assertRaises(ValueError):
            validate_config({"mandate": {"account_candidate_policy": {"r1": "everything"}}})

    def test_policy_must_be_a_mapping(self):
        with self.assertRaises(ValueError):
            validate_config({"mandate": {"account_candidate_policy": ["r1"]}})


class CandidateForecastAdmissionTests(unittest.TestCase):
    """A screened candidate's own forecast defect excludes that candidate, not the run."""

    def setUp(self):
        import numpy as np

        from tests.reference.test_allocation import fixture

        self.bundle, self.config = fixture()
        self.scores = pd.DataFrame({"security_id": ["A", "B", "C"], "composite": [0.3, 0.7, 0.95]})
        covariance = patch(
            "portfolio_lab.metrics.portfolio_covariance",
            side_effect=lambda b, c, ids: {
                "ids": ids,
                "matrix": np.eye(len(ids)) * 0.04,
                "complete": True,
                "observations": 156,
                "issues": [],
            },
        )
        stress = patch(
            "portfolio_lab.metrics.stress_returns",
            side_effect=lambda b, c, ids: {"recession": {sid: -0.3 for sid in ids}},
        )
        covariance.start()
        stress.start()
        self.addCleanup(covariance.stop)
        self.addCleanup(stress.stop)
        self.config["mandate"]["account_candidate_policy"] = {"r1": "eligible_universe"}
        self.config["mandate"]["account_permissions"] = {}

    def _append(self, frame, rows):
        self.bundle[frame] = pd.concat([self.bundle[frame], pd.DataFrame(rows)], ignore_index=True)

    def add_candidate(self, sid, *, probability, return_value=0.05):
        self._append(
            "securities",
            [
                dict(
                    security_id=sid,
                    ticker=sid,
                    issuer_id="issuer_" + sid,
                    name=sid,
                    sector="Technology",
                    instrument_type="equity",
                    currency="USD",
                    eligible=True,
                )
            ],
        )
        self._append(
            "prices",
            [
                dict(
                    security_id=sid,
                    date="2026-08-31",
                    close=50.0,
                    available_at="2026-08-31T20:00:00Z",
                    received_at="2026-08-31T20:00:00Z",
                )
            ],
        )
        reference = self.bundle["forecasts"]
        labels = reference[reference.security_id == "A"]
        self._append(
            "forecasts",
            [
                dict(
                    security_id=sid,
                    scenario=row.scenario,
                    horizon_months=row.horizon_months,
                    return_value=return_value,
                    probability=float("nan") if probability is None else probability,
                    basis="subjective",
                    source="shared state",
                    forecast_date=row.forecast_date,
                )
                for row in labels.itertuples()
            ],
        )

    def run_proposals(self):
        from portfolio_lab.allocation import build_proposals

        return build_proposals(self.bundle, self.config, self.scores)

    def excluded(self, result):
        return {row["security_id"]: row["reasons"] for row in result["excluded_candidates"]}

    def test_a_blank_probability_beside_stated_ones_excludes_that_candidate(self):
        self.add_candidate("D", probability=None)
        result = self.run_proposals()
        self.assertNotEqual(result["status"], "blocked", result["issues"])
        self.assertNotIn("inconsistent_joint_probability", {i["code"] for i in result["issues"]})
        self.assertIn("missing_forecast", self.excluded(result)["D"])

    def test_a_disagreeing_probability_excludes_that_candidate(self):
        self.add_candidate("D", probability=0.9)
        result = self.run_proposals()
        self.assertNotEqual(result["status"], "blocked", result["issues"])
        self.assertIn("missing_forecast", self.excluded(result)["D"])

    def test_an_out_of_range_return_excludes_that_candidate(self):
        self.add_candidate("D", probability=None, return_value=-1.5)
        result = self.run_proposals()
        self.assertNotEqual(result["status"], "blocked", result["issues"])
        self.assertNotIn("invalid_forecast_return", {i["code"] for i in result["issues"]})
        self.assertIn("missing_forecast", self.excluded(result)["D"])

    def test_a_non_finite_return_excludes_that_candidate(self):
        import numpy as np

        self.add_candidate("D", probability=None, return_value=np.nan)
        result = self.run_proposals()
        self.assertNotEqual(result["status"], "blocked", result["issues"])
        self.assertIn("missing_forecast", self.excluded(result)["D"])

    def test_a_matching_candidate_is_still_admitted(self):
        reference = self.bundle["forecasts"]
        probabilities = {
            row.scenario: row.probability
            for row in reference[reference.security_id == "A"].itertuples()
        }
        self.bundle["securities"] = self.bundle["securities"]
        self.add_candidate("D", probability=None)
        frame = self.bundle["forecasts"]
        mask = frame.security_id == "D"
        frame.loc[mask, "probability"] = frame.loc[mask, "scenario"].map(probabilities)
        result = self.run_proposals()
        self.assertEqual(self.excluded(result), {})
        self.assertEqual(result["solver"]["candidate_admission"]["excluded"], 0)


class SinglePricingTests(CandidateForecastAdmissionTests):
    """Admission and the proposal build read each security's price once between them."""

    def test_no_security_is_priced_twice_in_one_run(self):
        from portfolio_lab import allocation

        self.add_candidate("D", probability=None)
        reference = self.bundle["forecasts"]
        mask = reference.security_id == "D"
        stated = {
            row.scenario: row.probability
            for row in reference[reference.security_id == "A"].itertuples()
        }
        reference.loc[mask, "probability"] = reference.loc[mask, "scenario"].map(stated)
        priced, original = [], allocation._known_price

        def counting(*args, **kwargs):
            priced.append(args[3])
            return original(*args, **kwargs)

        with patch.object(allocation, "_known_price", counting):
            result = self.run_proposals()
        self.assertNotEqual(result["status"], "blocked", result["issues"])
        self.assertEqual(sorted(priced), sorted(set(priced)))
        self.assertIn("D", priced)


if __name__ == "__main__":
    unittest.main()
