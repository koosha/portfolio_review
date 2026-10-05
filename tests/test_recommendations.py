"""Buy / sell / hold per security, checked against numbers worked out by hand.

The portfolio: a reconciled NAV of 100,000 USD holding A (30,000), B (15,000) and C
(5,000), plus two unowned screened companies, N1 and N2.

- A has an adopted EPS valuation returning +12% at the central scenario.
- B's proposed EPS model returns -8%. With a 0% floor and a full sale at -20%, that sells
  (0 - (-8)) / (0 - (-20)) = 40% of the position, 6,000.
- C has no valuation, so it holds.
- N1's proposed DCF implies +15% with a 0.95 score, so it is initiated.
- N2 implies +20% but scores 0.40, below the 0.50 gate, so it is not listed.

Excess-return weighting splits cash 0.04 : 0.07 between A and N1: 0.3636 and 0.6364.
"""

import random
import unittest
from copy import deepcopy

from portfolio_lab.config import DEFAULTS, validate_config
from portfolio_research.recommendations import review_recommendations


def eps(total_return, status="ready"):
    return {
        "status": status,
        "scenarios": [{"label": "central", "total_return": total_return, "status": status}],
    }


def proposed_eps(total_return):
    return {
        "proposals": {
            "eps": {
                "proposal_meta": {
                    "verification": {"status": "ready", "central_total_return": total_return},
                    "evidence": {"eps": "estimates:X"},
                }
            }
        }
    }


def proposed_dcf(change):
    return {"proposals": {"dcf": {"proposal_meta": {"implied_change_vs_price": change}}}}


def holding(sid, value, account="acct-1", currency="USD"):
    return {
        "account_id": account,
        "security_id": sid,
        "market_value": value,
        "currency": currency,
        "name": f"Company {sid}",
        "ticker": sid,
    }


def signal(sid, score, eligible=True):
    return {
        "security_id": sid,
        "score": score,
        "eligible": eligible,
        "data_status": "complete",
        "ticker": sid,
    }


def fixture():
    result = {
        "summary": {"total_value": 100_000.0, "complete": True, "currency": "USD"},
        "holdings": [holding("A", 30_000.0), holding("B", 15_000.0), holding("C", 5_000.0)],
        "signals": [signal("A", 0.8), signal("N1", 0.95), signal("N2", 0.40)],
        "company_research": {"A": {"eps": eps(0.12)}},
        "research": {"B": proposed_eps(-0.08), "N1": proposed_dcf(0.15), "N2": proposed_dcf(0.20)},
    }
    config = deepcopy(DEFAULTS)
    config["review"]["max_position_weight"] = 0.40
    return result, {}, config


def by_id(recommendations):
    return {row["security_id"]: row for row in recommendations["rows"]}


class HandCheckedRecommendationTests(unittest.TestCase):
    def setUp(self):
        self.result, self.bundle, self.config = fixture()
        self.out = review_recommendations(self.result, self.bundle, self.config)
        self.rows = by_id(self.out)

    def test_every_holding_and_the_initiated_candidate_get_one_row(self):
        self.assertEqual(set(self.rows), {"A", "B", "C", "N1"})

    def test_an_attractive_holding_is_bought_with_its_share_of_cash(self):
        row = self.rows["A"]
        self.assertEqual(row["action"], "buy")
        self.assertEqual(row["expected_return_basis"], "adopted_eps_central")
        self.assertEqual(row["confidence"], "high")
        self.assertAlmostEqual(row["current_weight"], 0.30)
        self.assertEqual(row["buy_share"], 0.3636)

    def test_an_overpriced_holding_sells_in_proportion_to_its_shortfall(self):
        row = self.rows["B"]
        self.assertEqual(row["action"], "sell")
        self.assertEqual(row["sell_fraction"], 0.4)
        self.assertEqual(row["sell_amount"], 6000.0)
        self.assertIsNone(row["buy_share"])
        self.assertEqual(row["reasons"][0]["rule"], "expected_return_below_floor")
        self.assertEqual(self.out["sale_proceeds"], 6000.0)

    def test_a_holding_without_valuation_evidence_holds_and_says_why(self):
        row = self.rows["C"]
        self.assertEqual(row["action"], "hold")
        self.assertEqual(row["reasons"][0]["rule"], "no_expected_return_evidence")
        self.assertEqual(row["confidence"], "low")

    def test_a_new_candidate_is_initiated_and_shares_sum_to_one(self):
        row = self.rows["N1"]
        self.assertFalse(row["held"])
        self.assertEqual(row["action"], "buy")
        self.assertEqual(row["buy_share"], 0.6364)
        self.assertEqual(self.out["buy_share_sum"], 1.0)
        self.assertEqual(self.out["counts"]["candidates_considered"], 2)
        self.assertEqual(self.out["counts"]["candidates_qualified"], 1)

    def test_rows_are_ordered_sells_then_buys_then_holds(self):
        self.assertEqual([row["security_id"] for row in self.out["rows"]], ["B", "N1", "A", "C"])


class RecommendationRuleTests(unittest.TestCase):
    def run_with(self, mutate=None):
        result, bundle, config = fixture()
        if mutate:
            mutate(result, config)
        return review_recommendations(result, bundle, config)

    def test_equal_weighting_rounds_three_buys_to_exactly_one(self):
        def mutate(result, config):
            config["review"]["buy_weighting"] = "equal"
            config["review"]["max_new_positions"] = 2
            result["signals"][2]["score"] = 0.9

        out = self.run_with(mutate)
        shares = sorted(row["buy_share"] for row in out["rows"] if row["action"] == "buy")
        self.assertEqual(shares, [0.3333, 0.3333, 0.3334])
        self.assertEqual(out["buy_share_sum"], 1.0)

    def test_an_adopted_valuation_outranks_the_proposal(self):
        def mutate(result, config):
            result["research"]["A"] = proposed_eps(-0.5)

        row = by_id(self.run_with(mutate))["A"]
        self.assertEqual(row["expected_return_basis"], "adopted_eps_central")
        self.assertEqual(row["action"], "buy")

    def test_an_adopted_dcf_is_valued_against_the_holding_price(self):
        def mutate(result, config):
            result["holdings"][2]["price"] = 50.0
            result["company_research"]["C"] = {"dcf": {"status": "ready", "value_per_share": 40.0}}

        row = by_id(self.run_with(mutate))["C"]
        self.assertEqual(row["expected_return_basis"], "adopted_dcf")
        self.assertAlmostEqual(row["expected_return"], -0.2)
        self.assertEqual((row["action"], row["sell_fraction"]), ("sell", 1.0))

    def test_a_small_shortfall_still_sells_at_least_one_step(self):
        def mutate(result, config):
            result["research"]["B"] = proposed_eps(-0.001)

        row = by_id(self.run_with(mutate))["B"]
        self.assertEqual(row["sell_fraction"], 0.05)

    def test_a_position_at_its_cap_is_not_bought(self):
        def mutate(result, config):
            config["review"]["max_position_weight"] = 0.25

        out = self.run_with(mutate)
        row = by_id(out)["A"]
        self.assertEqual(row["action"], "hold")
        self.assertEqual(row["reasons"][0]["rule"], "at_position_cap")
        self.assertEqual(by_id(out)["N1"]["buy_share"], 1.0)

    def test_the_mandate_issuer_cap_is_the_default_position_cap(self):
        def mutate(result, config):
            config["review"]["max_position_weight"] = None
            config["mandate"]["issuer_cap"] = 0.2

        out = self.run_with(mutate)
        self.assertEqual(out["parameters"]["max_position_weight"], 0.2)
        self.assertEqual(by_id(out)["A"]["action"], "hold")

    def test_a_locked_security_always_holds(self):
        def mutate(result, config):
            config["mandate"]["locked_security_ids"] = ["B"]

        row = by_id(self.run_with(mutate))["B"]
        self.assertEqual((row["action"], row["reasons"][0]["rule"]), ("hold", "locked"))

    def test_the_score_gate_holds_an_otherwise_attractive_holding(self):
        def mutate(result, config):
            result["signals"][0]["score"] = 0.3

        row = by_id(self.run_with(mutate))["A"]
        self.assertEqual(row["action"], "hold")
        self.assertIn("gate", row["reasons"][0]["detail"])

    def test_unreconciled_nav_weights_by_known_positions_and_says_so(self):
        def mutate(result, config):
            result["summary"]["complete"] = False

        out = self.run_with(mutate)
        self.assertEqual(out["nav"], {"value": 50_000.0, "basis": "known_position_value"})
        self.assertAlmostEqual(by_id(out)["A"]["current_weight"], 0.6)
        self.assertIn("nav_not_reconciled", [issue["code"] for issue in out["issues"]])

    def test_a_position_in_another_currency_is_left_out_of_the_weights(self):
        def mutate(result, config):
            result["holdings"].append(holding("CAD1", 1000.0, currency="CAD"))

        out = self.run_with(mutate)
        self.assertIn("unconverted_positions", [issue["code"] for issue in out["issues"]])
        self.assertEqual(by_id(out)["CAD1"]["current_value"], 0.0)

    def test_new_candidates_can_be_switched_off(self):
        def mutate(result, config):
            config["review"]["max_new_positions"] = 0

        out = self.run_with(mutate)
        self.assertNotIn("N1", by_id(out))
        self.assertEqual(by_id(out)["A"]["buy_share"], 1.0)

    def test_a_watchlisted_candidate_is_listed_even_when_it_does_not_qualify(self):
        def mutate(result, config):
            config["signals"]["watchlist"] = ["N2"]

        row = by_id(self.run_with(mutate))["N2"]
        self.assertEqual(row["action"], "hold")
        self.assertEqual(row["reasons"][0]["rule"], "below_buy_thresholds")
        self.assertEqual(row["reasons"][-1]["rule"], "watchlist")

    def test_stale_prices_lower_confidence(self):
        def mutate(result, config):
            result["coverage"] = {"by_security": {"A": {"prices": "stale"}}}

        row = by_id(self.run_with(mutate))["A"]
        self.assertEqual(row["confidence"], "medium")
        self.assertIn("stale_or_missing_data", [reason["rule"] for reason in row["reasons"]])

    def test_nothing_to_buy_is_stated_rather_than_invented(self):
        def mutate(result, config):
            config["review"]["buy_min_return"] = 0.5

        out = self.run_with(mutate)
        self.assertEqual(out["buy_share_sum"], 0)
        self.assertIn("no_buy_candidates", [issue["code"] for issue in out["issues"]])

    def test_an_empty_run_recommends_nothing_and_does_not_raise(self):
        out = review_recommendations({}, {}, deepcopy(DEFAULTS))
        self.assertEqual(out["rows"], [])
        self.assertEqual(out["buy_share_sum"], 0)

    def test_input_order_does_not_change_the_answer(self):
        baseline = self.run_with()
        for seed in range(5):

            def mutate(result, config, seed=seed):
                rng = random.Random(seed)
                rng.shuffle(result["holdings"])
                rng.shuffle(result["signals"])

            self.assertEqual(self.run_with(mutate), baseline)


class ReviewConfigTests(unittest.TestCase):
    def test_defaults_validate_and_thresholds_must_be_ordered(self):
        validate_config({})
        with self.assertRaisesRegex(ValueError, "sell_return_floor < buy_min_return"):
            validate_config({"review": {"sell_return_floor": 0.1, "buy_min_return": 0.05}})
        with self.assertRaisesRegex(ValueError, "buy_weighting"):
            validate_config({"review": {"buy_weighting": "random"}})
        with self.assertRaisesRegex(ValueError, "max_new_positions must be an integer"):
            validate_config({"review": {"max_new_positions": 1.5}})
        with self.assertRaisesRegex(ValueError, "Unknown review fields"):
            validate_config({"review": {"new_money": 100}})


if __name__ == "__main__":
    unittest.main()
