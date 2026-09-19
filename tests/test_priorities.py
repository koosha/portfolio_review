"""Evidence prioritisation: every reason is earned by an input this run actually has.

The rules are deterministic and independent. Each test withholds one rule's inputs and
asserts silence, then supplies them and asserts the single reason that follows, so a
reason can never appear from an assumption. Items are plain dicts shaped like a saved
run result; nothing here touches providers, files or the network.
"""

import unittest
from copy import deepcopy

from portfolio_lab.config import DEFAULTS
from portfolio_research.priorities import METHOD_VERSION, review_priorities

OWNED = "OWN"
CANDIDATE = "NEW"


def _config(**over):
    config = deepcopy(DEFAULTS)
    for group, values in over.items():
        config[group].update(values)
    return config


def _signal(security_id, **over):
    row = {
        "security_id": security_id,
        "ticker": security_id,
        "issuer_id": f"I-{security_id}",
        "sector": "Technology",
        "market_cap": 1_000_000_000.0,
        "price": 100.0,
        "quality": 0.5,
        "value": 0.5,
        "momentum": 0.5,
        "score": 0.5,
        "eligible": True,
        "data_status": "complete",
        "reasons": [],
    }
    row.update(over)
    return row


def _brief(security_id, facts=(), invalidation=()):
    return {
        "security_id": security_id,
        "name": f"{security_id} Corporation",
        "facts": [
            {
                "id": f"{security_id}:{field}",
                "field": field,
                "value": value,
                "units": "USD",
                "source_id": f"src-{security_id}",
                "as_of": "2026-08-31",
            }
            for field, value in facts
        ],
        "invalidation_conditions": [
            {"text": text, "source_ids": [f"src-{security_id}"]} for text in invalidation
        ],
    }


def _eps_proposal(security_id, central_total_return=0.12, scenarios=None):
    return {
        "security_id": security_id,
        "starting_price": 100.0,
        "currency": "USD",
        "horizon_months": 12,
        "scenarios": scenarios
        or [
            {"label": "adverse", "eps": 4.0, "pe": 20.0, "distributions_per_starting_share": 1.0},
            {"label": "central", "eps": 5.0, "pe": 20.0, "distributions_per_starting_share": 1.0},
            {"label": "favorable", "eps": 6.0, "pe": 20.0, "distributions_per_starting_share": 1.0},
        ],
        "proposal_meta": {
            "basis": "proposed",
            "evidence": {"eps_central": f"src-{security_id}"},
            "verification": {
                "status": "ready",
                "central_total_return": central_total_return,
                "issues": [],
            },
        },
    }


def _result(**over):
    result = {
        "summary": {"total_value": 100_000.0, "currency": "USD", "complete": True},
        "holdings": [
            {
                "security_id": OWNED,
                "account_id": "A1",
                "market_value": 10_000.0,
                "currency": "USD",
            }
        ],
        "signals": [_signal(OWNED), _signal(CANDIDATE)],
        "issuer_exposure": [
            {
                "issuer_id": f"I-{OWNED}",
                "sector": "Technology",
                "market_value": 10_000.0,
                "weight": 0.10,
            }
        ],
        "risk": {"status": "complete", "risk_contributions": [], "stresses": []},
        "coverage": {"capabilities": {}, "by_security": {}},
        "research": {},
        "allocation": {"selected_candidate": "no_change", "candidates": [], "proposals": []},
        "forecast_inputs": [],
        "issues": [],
    }
    result.update(over)
    return result


def _bundle():
    return {
        "as_of": "2026-08-31",
        "securities": [
            {"security_id": OWNED, "name": "OWN Corporation", "issuer_id": f"I-{OWNED}"},
            {"security_id": CANDIDATE, "name": "NEW Corporation", "issuer_id": f"I-{CANDIDATE}"},
        ],
    }


def _rules(items, security_id):
    item = next((row for row in items if row["security_id"] == security_id), None)
    return [reason["rule"] for reason in item["reasons"]] if item else []


class PrioritiesShapeTests(unittest.TestCase):
    def test_quiet_run_reports_no_items_but_still_documents_its_rules(self):
        report = review_priorities(_result(), _bundle(), _config())
        self.assertEqual(report["holdings_to_review"], [])
        self.assertEqual(report["new_candidates"], [])
        self.assertEqual(report["method_version"], METHOD_VERSION)
        self.assertEqual(report["method_version"], "priorities-1")
        names = [rule["rule"] for rule in report["rules"]]
        self.assertEqual(len(names), len(set(names)))
        for rule in report["rules"]:
            self.assertEqual(sorted(rule), ["applies_to", "detail", "inputs", "rule"])
            self.assertIn(rule["applies_to"], {"holding", "candidate"})
            self.assertTrue(rule["inputs"])

    def test_inputs_are_never_mutated(self):
        result, bundle, config = _result(), _bundle(), _config(mandate={"issuer_cap": 0.05})
        before = (deepcopy(result), deepcopy(bundle), deepcopy(config))
        review_priorities(result, bundle, config)
        self.assertEqual((result, bundle, config), before)

    def test_an_item_carries_every_documented_field(self):
        report = review_priorities(_result(), _bundle(), _config(mandate={"issuer_cap": 0.05}))
        item = report["holdings_to_review"][0]
        self.assertEqual(
            sorted(item),
            [
                "account_eligibility",
                "costs",
                "current_weight",
                "evidence",
                "funding_source",
                "invalidation",
                "kind",
                "name",
                "priority_score",
                "proposed_weight",
                "rank_basis",
                "reasons",
                "risk_effect",
                "scenario_range",
                "security_id",
                "unresolved_facts",
            ],
        )
        self.assertEqual(item["kind"], "holding")
        self.assertEqual(item["name"], "OWN Corporation")
        self.assertAlmostEqual(item["current_weight"], 0.10)
        self.assertEqual(sorted(item["reasons"][0]), ["detail", "rule", "source_ids"])
        self.assertEqual(sorted(item["risk_effect"]), ["concentration_vs_cap", "contribution"])
        self.assertTrue(item["rank_basis"])

    def test_current_weight_is_withheld_when_the_denominator_is_unreconciled(self):
        result = _result(summary={"total_value": 100_000.0, "currency": "USD", "complete": False})
        report = review_priorities(result, _bundle(), _config(mandate={"issuer_cap": 0.05}))
        item = report["holdings_to_review"][0]
        self.assertIsNone(item["current_weight"])
        self.assertEqual(item["evidence"]["current_weight_basis"], "unreconciled")


class HoldingRuleTests(unittest.TestCase):
    """Each holding rule stays silent without its inputs and fires with them."""

    def test_below_retention_rank_needs_a_scored_issuer_ranking(self):
        unscored = _result(
            signals=[
                _signal(OWNED, score=None, data_status="incomplete"),
                _signal(CANDIDATE, score=0.9),
            ]
        )
        self.assertNotIn(
            "below_retention_rank",
            _rules(review_priorities(unscored, _bundle(), _config())["holdings_to_review"], OWNED),
        )
        ranked = _result(
            signals=[_signal(OWNED, score=0.1), _signal(CANDIDATE, score=0.9)],
        )
        report = review_priorities(ranked, _bundle(), _config())
        self.assertEqual(_rules(report["holdings_to_review"], OWNED), ["below_retention_rank"])

    def test_concentration_over_cap_needs_a_configured_cap_and_an_issuer_weight(self):
        self.assertEqual(
            _rules(review_priorities(_result(), _bundle(), _config())["holdings_to_review"], OWNED),
            [],
        )
        report = review_priorities(_result(), _bundle(), _config(mandate={"issuer_cap": 0.05}))
        self.assertEqual(_rules(report["holdings_to_review"], OWNED), ["concentration_over_cap"])
        under = review_priorities(_result(), _bundle(), _config(mandate={"issuer_cap": 0.5}))
        self.assertEqual(_rules(under["holdings_to_review"], OWNED), [])

    def test_stale_or_missing_data_needs_a_per_security_coverage_status(self):
        covered = _result(
            coverage={
                "capabilities": {},
                "by_security": {OWNED: {"prices": "ok", "statements": "ok"}},
            }
        )
        self.assertEqual(
            _rules(review_priorities(covered, _bundle(), _config())["holdings_to_review"], OWNED),
            [],
        )
        gap = _result(
            coverage={
                "capabilities": {},
                "by_security": {OWNED: {"prices": "missing", "statements": "ok"}},
            }
        )
        report = review_priorities(gap, _bundle(), _config())
        self.assertEqual(_rules(report["holdings_to_review"], OWNED), ["stale_or_missing_data"])
        reason = report["holdings_to_review"][0]["reasons"][0]
        self.assertIn("prices", reason["detail"])

    def test_large_estimate_revision_needs_a_previous_run_with_the_same_estimate(self):
        current = _result(
            research={OWNED: {"brief": _brief(OWNED, facts=[("eps_next_year_avg", 5.0)])}}
        )
        self.assertEqual(
            _rules(review_priorities(current, _bundle(), _config())["holdings_to_review"], OWNED),
            [],
        )
        previous = _result(
            research={OWNED: {"brief": _brief(OWNED, facts=[("eps_next_year_avg", 4.0)])}}
        )
        report = review_priorities(current, _bundle(), _config(), previous=previous)
        self.assertEqual(_rules(report["holdings_to_review"], OWNED), ["large_estimate_revision"])
        unchanged = _result(
            research={OWNED: {"brief": _brief(OWNED, facts=[("eps_next_year_avg", 5.02)])}}
        )
        baseline = _result(
            research={OWNED: {"brief": _brief(OWNED, facts=[("eps_next_year_avg", 5.0)])}}
        )
        quiet = review_priorities(unchanged, _bundle(), _config(), previous=baseline)
        self.assertEqual(_rules(quiet["holdings_to_review"], OWNED), [])

    def test_proposed_central_return_negative_needs_a_verified_eps_proposal(self):
        positive = _result(
            research={OWNED: {"proposals": {"eps": _eps_proposal(OWNED, 0.10), "dcf": None}}}
        )
        self.assertEqual(
            _rules(review_priorities(positive, _bundle(), _config())["holdings_to_review"], OWNED),
            [],
        )
        negative = _result(
            research={OWNED: {"proposals": {"eps": _eps_proposal(OWNED, -0.08), "dcf": None}}}
        )
        report = review_priorities(negative, _bundle(), _config())
        self.assertEqual(
            _rules(report["holdings_to_review"], OWNED), ["proposed_central_return_negative"]
        )

    def test_drawdown_warning_needs_a_stress_threshold_and_a_top_contribution(self):
        risk = {
            "status": "complete",
            "risk_contributions": [{"security_id": OWNED, "contribution": 0.04}],
            "stresses": [{"scenario": "broad_equity", "return": -0.35, "status": "complete"}],
        }
        self.assertEqual(
            _rules(
                review_priorities(_result(risk=risk), _bundle(), _config())["holdings_to_review"],
                OWNED,
            ),
            [],
        )
        config = _config(mandate={"max_stress_loss": 0.25})
        report = review_priorities(_result(risk=risk), _bundle(), config)
        self.assertEqual(_rules(report["holdings_to_review"], OWNED), ["drawdown_warning"])
        outside = {
            "status": "complete",
            "risk_contributions": [
                {"security_id": f"X{i}", "contribution": 0.09 - i * 0.01} for i in range(3)
            ]
            + [{"security_id": OWNED, "contribution": 0.001}],
            "stresses": [{"scenario": "broad_equity", "return": -0.35, "status": "complete"}],
        }
        quiet = review_priorities(_result(risk=outside), _bundle(), config)
        self.assertEqual(_rules(quiet["holdings_to_review"], OWNED), [])

    def test_unresolved_identity_or_currency_needs_a_named_ledger_gap(self):
        coverage = {
            "capabilities": {
                "fx": {"requested": 1, "available": 0, "stale": 0, "missing": [OWNED]},
                "listings": {"requested": 1, "available": 1, "stale": 0, "missing": []},
            },
            "by_security": {},
        }
        report = review_priorities(_result(coverage=coverage), _bundle(), _config())
        self.assertEqual(
            _rules(report["holdings_to_review"], OWNED), ["unresolved_identity_or_currency"]
        )
        clean = deepcopy(coverage)
        clean["capabilities"]["fx"]["missing"] = []
        self.assertEqual(
            _rules(
                review_priorities(_result(coverage=clean), _bundle(), _config())[
                    "holdings_to_review"
                ],
                OWNED,
            ),
            [],
        )

    def test_candidate_rules_never_fire_for_a_holding(self):
        result = _result(
            signals=[_signal(OWNED, score=0.97), _signal(CANDIDATE, score=0.5)],
            research={
                OWNED: {
                    "brief": _brief(OWNED, facts=[("eps_0y_avg", 2.0), ("eps_next_year_avg", 4.0)])
                }
            },
        )
        report = review_priorities(result, _bundle(), _config(signals={"watchlist": [OWNED]}))
        self.assertEqual(report["holdings_to_review"], [])


class CandidateRuleTests(unittest.TestCase):
    def test_top_decile_composite_needs_a_complete_composite_score(self):
        incomplete = _result(
            signals=[_signal(OWNED), _signal(CANDIDATE, score=0.95, data_status="incomplete")]
        )
        self.assertEqual(
            _rules(
                review_priorities(incomplete, _bundle(), _config())["new_candidates"], CANDIDATE
            ),
            [],
        )
        strong = _result(signals=[_signal(OWNED), _signal(CANDIDATE, score=0.95)])
        report = review_priorities(strong, _bundle(), _config())
        self.assertEqual(_rules(report["new_candidates"], CANDIDATE), ["top_decile_composite"])
        self.assertEqual(report["new_candidates"][0]["kind"], "candidate")
        middling = _result(signals=[_signal(OWNED), _signal(CANDIDATE, score=0.80)])
        self.assertEqual(
            _rules(review_priorities(middling, _bundle(), _config())["new_candidates"], CANDIDATE),
            [],
        )

    def test_watchlist_needs_the_symbol_on_the_configured_watchlist(self):
        self.assertEqual(
            _rules(review_priorities(_result(), _bundle(), _config())["new_candidates"], CANDIDATE),
            [],
        )
        report = review_priorities(
            _result(), _bundle(), _config(signals={"watchlist": [CANDIDATE]})
        )
        self.assertEqual(_rules(report["new_candidates"], CANDIDATE), ["watchlist"])

    def test_estimate_growth_high_needs_both_consensus_periods_and_a_complete_score(self):
        facts = [("eps_0y_avg", 2.0), ("eps_next_year_avg", 3.0)]
        one_period = _result(
            research={CANDIDATE: {"brief": _brief(CANDIDATE, facts=[("eps_0y_avg", 2.0)])}}
        )
        self.assertEqual(
            _rules(
                review_priorities(one_period, _bundle(), _config())["new_candidates"], CANDIDATE
            ),
            [],
        )
        incomplete = _result(
            signals=[_signal(OWNED), _signal(CANDIDATE, score=None, data_status="incomplete")],
            research={CANDIDATE: {"brief": _brief(CANDIDATE, facts=facts)}},
        )
        self.assertEqual(
            _rules(
                review_priorities(incomplete, _bundle(), _config())["new_candidates"], CANDIDATE
            ),
            [],
        )
        growing = _result(research={CANDIDATE: {"brief": _brief(CANDIDATE, facts=facts)}})
        report = review_priorities(growing, _bundle(), _config())
        self.assertEqual(_rules(report["new_candidates"], CANDIDATE), ["estimate_growth_high"])
        self.assertEqual(
            report["new_candidates"][0]["reasons"][0]["source_ids"], [f"src-{CANDIDATE}"]
        )
        flat = _result(
            research={
                CANDIDATE: {
                    "brief": _brief(
                        CANDIDATE, facts=[("eps_0y_avg", 2.0), ("eps_next_year_avg", 2.05)]
                    )
                }
            }
        )
        self.assertEqual(
            _rules(review_priorities(flat, _bundle(), _config())["new_candidates"], CANDIDATE), []
        )

    def test_holding_rules_never_fire_for_an_unowned_candidate(self):
        result = _result(
            signals=[_signal(OWNED), _signal(CANDIDATE, score=0.05)],
            coverage={
                "capabilities": {},
                "by_security": {CANDIDATE: {"prices": "missing", "statements": "missing"}},
            },
            research={CANDIDATE: {"proposals": {"eps": _eps_proposal(CANDIDATE, -0.2)}}},
        )
        report = review_priorities(result, _bundle(), _config(mandate={"issuer_cap": 0.01}))
        self.assertEqual(report["new_candidates"], [])


class CompositeAndOrderingTests(unittest.TestCase):
    def test_no_priority_score_when_a_signal_family_is_missing(self):
        result = _result(
            signals=[
                _signal(OWNED, momentum=None, score=None, data_status="incomplete"),
                _signal(CANDIDATE),
            ]
        )
        report = review_priorities(result, _bundle(), _config(mandate={"issuer_cap": 0.05}))
        item = report["holdings_to_review"][0]
        self.assertIsNone(item["priority_score"])
        self.assertEqual(item["evidence"]["families_present"], ["quality", "value"])

    def test_priority_score_is_the_composite_only_when_every_family_is_present(self):
        result = _result(signals=[_signal(OWNED, score=0.42), _signal(CANDIDATE)])
        report = review_priorities(result, _bundle(), _config(mandate={"issuer_cap": 0.05}))
        item = report["holdings_to_review"][0]
        self.assertAlmostEqual(item["priority_score"], 0.42)
        self.assertEqual(item["evidence"]["families_present"], ["momentum", "quality", "value"])

    def test_items_are_ordered_by_reason_count_then_market_cap(self):
        result = _result(
            holdings=[
                {
                    "security_id": sid,
                    "account_id": "A1",
                    "market_value": 10_000.0,
                    "currency": "USD",
                }
                for sid in ("ONE", "TWO", "THREE")
            ],
            signals=[
                _signal("ONE", issuer_id="I-ONE", market_cap=5.0e10, score=0.1),
                _signal("TWO", issuer_id="I-TWO", market_cap=9.0e10, score=0.1),
                _signal("THREE", issuer_id="I-THREE", market_cap=1.0e10, score=0.9),
            ],
            issuer_exposure=[
                {"issuer_id": "I-ONE", "market_value": 10_000.0, "weight": 0.30},
                {"issuer_id": "I-TWO", "market_value": 10_000.0, "weight": 0.01},
                {"issuer_id": "I-THREE", "market_value": 10_000.0, "weight": 0.30},
            ],
        )
        report = review_priorities(result, _bundle(), _config(mandate={"issuer_cap": 0.05}))
        self.assertEqual(
            [item["security_id"] for item in report["holdings_to_review"]], ["ONE", "TWO", "THREE"]
        )
        self.assertEqual([len(item["reasons"]) for item in report["holdings_to_review"]], [2, 1, 1])


class EvidenceTests(unittest.TestCase):
    def test_scenario_range_comes_from_the_run_forecasts_when_present(self):
        result = _result(
            forecast_inputs=[
                {
                    "security_id": OWNED,
                    "scenario": label,
                    "return_value": value,
                    "probability": None,
                    "forecast_date": "2026-08-31",
                    "horizon_months": 12,
                }
                for label, value in (("Adverse", -0.2), ("Central", 0.06), ("Favorable", 0.18))
            ]
        )
        report = review_priorities(result, _bundle(), _config(mandate={"issuer_cap": 0.05}))
        self.assertEqual(
            report["holdings_to_review"][0]["scenario_range"],
            {"Adverse": -0.2, "Central": 0.06, "Favorable": 0.18},
        )

    def test_scenario_range_falls_back_to_the_eps_proposal(self):
        result = _result(research={OWNED: {"proposals": {"eps": _eps_proposal(OWNED)}}})
        report = review_priorities(result, _bundle(), _config(mandate={"issuer_cap": 0.05}))
        scenario_range = report["holdings_to_review"][0]["scenario_range"]
        self.assertAlmostEqual(scenario_range["Central"], (5.0 * 20.0 + 1.0) / 100.0 - 1)
        self.assertAlmostEqual(scenario_range["Adverse"], (4.0 * 20.0 + 1.0) / 100.0 - 1)
        self.assertAlmostEqual(scenario_range["Favorable"], (6.0 * 20.0 + 1.0) / 100.0 - 1)

    def test_scenario_range_is_empty_without_forecasts_or_a_proposal(self):
        report = review_priorities(_result(), _bundle(), _config(mandate={"issuer_cap": 0.05}))
        self.assertEqual(report["holdings_to_review"][0]["scenario_range"], {})

    def test_allocation_supplies_proposed_weight_costs_and_funding(self):
        allocation = {
            "selected_candidate": "simple_equal_issuer_sleeve",
            "candidates": [
                {"candidate": "no_change", "proposals": [], "accounts": []},
                {
                    "candidate": "simple_equal_issuer_sleeve",
                    "proposals": [
                        {
                            "candidate": "simple_equal_issuer_sleeve",
                            "account_id": "A1",
                            "security_id": CANDIDATE,
                            "action": "buy",
                            "current_weight": 0.0,
                            "target_weight": 0.05,
                            "trade_value": 5_000.0,
                            "estimated_cost": 5.0,
                            "estimated_tax": 0.0,
                        }
                    ],
                    "accounts": [
                        {
                            "account_id": "A1",
                            "starting_cash": 6_000.0,
                            "sales": 0.0,
                            "purchases": 5_000.0,
                            "execution_cost": 5.0,
                            "conditional_tax_reserve": 0.0,
                            "ending_cash": 995.0,
                            "cash_identity_residual": 0.0,
                        }
                    ],
                },
            ],
        }
        result = _result(
            signals=[_signal(OWNED), _signal(CANDIDATE, score=0.95)], allocation=allocation
        )
        config = _config(
            mandate={"account_candidate_policy": {"A1": "eligible_universe"}},
        )
        item = review_priorities(result, _bundle(), config)["new_candidates"][0]
        self.assertAlmostEqual(item["proposed_weight"], 0.05)
        self.assertEqual(item["costs"], {"execution_cost": 5.0, "tax_reserve": 0.0})
        self.assertEqual(item["funding_source"]["account_id"], "A1")
        self.assertAlmostEqual(item["funding_source"]["starting_cash"], 6_000.0)
        self.assertEqual(item["account_eligibility"], ["A1"])

    def test_account_eligibility_reads_explicit_permissions(self):
        result = _result(signals=[_signal(OWNED), _signal(CANDIDATE, score=0.95)])
        config = _config(mandate={"account_permissions": {"A1": [CANDIDATE], "A2": [OWNED]}})
        item = review_priorities(result, _bundle(), config)["new_candidates"][0]
        self.assertEqual(item["account_eligibility"], ["A1"])
        self.assertIsNone(item["proposed_weight"])
        self.assertIsNone(item["costs"])
        self.assertIsNone(item["funding_source"])

    def test_unresolved_facts_and_invalidation_travel_with_the_item(self):
        result = _result(
            issues=[
                {"code": "MISSING_PRICES", "security_id": OWNED, "severity": "warning"},
                {"code": "stale_price", "message": f"{OWNED}: the last close is stale."},
                {"code": "unrelated", "message": "Something else entirely."},
            ],
            research={
                OWNED: {"brief": _brief(OWNED, invalidation=["Operating income falls below X."])}
            },
        )
        item = review_priorities(result, _bundle(), _config(mandate={"issuer_cap": 0.05}))[
            "holdings_to_review"
        ][0]
        self.assertEqual(item["unresolved_facts"], ["MISSING_PRICES", "stale_price"])
        self.assertEqual(item["invalidation"][0]["text"], "Operating income falls below X.")

    def test_a_pandas_securities_frame_supplies_names_too(self):
        import pandas as pd

        bundle = {"as_of": "2026-08-31", "securities": pd.DataFrame(_bundle()["securities"])}
        item = review_priorities(_result(), bundle, _config(mandate={"issuer_cap": 0.05}))[
            "holdings_to_review"
        ][0]
        self.assertEqual(item["name"], "OWN Corporation")


if __name__ == "__main__":
    unittest.main()
