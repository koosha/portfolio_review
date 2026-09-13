"""Behavioral checks for conservative sale reserves and account funding."""

import unittest
from copy import deepcopy

import pandas as pd

from portfolio_lab.taxes import estimate_sale


class SaleTaxTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            "tax": {
                "enabled": True,
                "jurisdiction": "US",
                "short_term_rate": 0.40,
                "long_term_rate": 0.20,
                "loss_credit_rate": 0,
                "lot_method": "min_tax",
                "wash_sale_window_verified": False,
            }
        }

    def lots(self, *rows):
        return pd.DataFrame(
            [
                {
                    "account_id": "brokerage",
                    "security_id": "ABC",
                    "lot_id": f"lot-{i}",
                    "acquired_date": "2024-01-01",
                    "quantity": 10,
                    "basis_per_share": 50,
                    "currency": "USD",
                    **row,
                }
                for i, row in enumerate(rows or [{}])
            ]
        )

    def sale(self, lots=None, quantity=10, price=100, as_of="2026-09-13", config=None):
        return estimate_sale(
            self.lots() if lots is None else lots,
            "brokerage",
            "ABC",
            quantity,
            price,
            as_of,
            self.config if config is None else config,
        )

    def test_exact_anniversary_short_next_day_long_in_leap_interval(self):
        lots = self.lots({"acquired_date": "2023-03-01"})
        short = self.sale(lots, as_of="2024-03-01")  # 366 days still exactly one year
        long = self.sale(lots, as_of="2024-03-02")
        self.assertEqual(short["lot_plan"][0]["holding_period"], "short_term")
        self.assertEqual(short["estimated_tax"], 200)
        self.assertEqual(long["lot_plan"][0]["holding_period"], "long_term")
        self.assertEqual(long["estimated_tax"], 100)

    def test_february_29_anniversary(self):
        lots = self.lots({"acquired_date": "2024-02-29"})
        self.assertEqual(
            self.sale(lots, as_of="2025-02-28")["lot_plan"][0]["holding_period"], "short_term"
        )
        self.assertEqual(
            self.sale(lots, as_of="2025-03-01")["lot_plan"][0]["holding_period"], "long_term"
        )

    def test_partial_lot_sale_and_fractional_quantity(self):
        result = self.sale(
            self.lots(
                {"quantity": 2.5, "basis_per_share": 90}, {"quantity": 10, "basis_per_share": 50}
            ),
            quantity=4,
        )
        self.assertEqual(result["status"], "estimated")
        self.assertEqual([lot["quantity"] for lot in result["lot_plan"]], [2.5, 1.5])
        self.assertEqual(result["lot_plan"][1]["remaining_quantity"], 8.5)
        self.assertEqual(result["estimated_tax"], 20)
        self.assertEqual(result["realized_gain"], 100)

    def test_partial_coverage_never_yields_partial_tax_estimate(self):
        result = self.sale(self.lots({"quantity": 9}), quantity=10)
        self.assertEqual(result["status"], "unavailable")
        self.assertIsNone(result["estimated_tax"])
        self.assertEqual(result["lot_plan"], [])

    def test_missing_and_future_acquisition_dates_block(self):
        for value in [None, "", "2026-09-14", "2026-02-30", float("nan"), "09/01/2024"]:
            with self.subTest(value=value):
                result = self.sale(self.lots({"acquired_date": value}))
                self.assertEqual(result["status"], "unavailable")

    def test_invalid_unselected_lot_is_not_silently_ignored(self):
        result = self.sale(
            self.lots({"quantity": 100, "basis_per_share": 99}, {"basis_per_share": -1}), quantity=1
        )
        self.assertEqual(result["status"], "unavailable")
        self.assertIsNone(result["estimated_tax"])

    def test_negative_or_missing_basis_and_quantity_block(self):
        for key, value in [
            ("basis_per_share", -1),
            ("basis_per_share", None),
            ("quantity", 0),
            ("quantity", -2),
            ("quantity", float("inf")),
        ]:
            with self.subTest(key=key, value=value):
                self.assertEqual(self.sale(self.lots({key: value}))["status"], "unavailable")
        self.assertEqual(self.sale(self.lots({"basis_per_share": 0}))["estimated_tax"], 200)

    def test_losses_do_not_offset_gain_reserve_or_refund(self):
        result = self.sale(
            self.lots(
                {"quantity": 5, "basis_per_share": 150}, {"quantity": 5, "basis_per_share": 50}
            )
        )
        self.assertEqual(result["realized_gain"], 0)
        self.assertEqual(result["estimated_tax"], 50)
        self.assertTrue(result["wash_sale_review_required"])
        self.assertTrue(
            any(i["code"] == "TAX_WASH_SALE_WINDOW_UNVERIFIED" for i in result["issues"])
        )
        loss = self.sale(self.lots({"basis_per_share": 150}))
        self.assertEqual(loss["estimated_tax"], 0)
        self.assertEqual(loss["realized_gain"], -500)
        self.assertEqual(loss["net_proceeds_after_tax"], 1000)

    def test_verified_window_does_not_enable_loss_credit(self):
        config = deepcopy(self.config)
        config["tax"]["wash_sale_window_verified"] = True
        result = self.sale(self.lots({"basis_per_share": 150}), config=config)
        self.assertEqual(result["estimated_tax"], 0)
        self.assertTrue(result["wash_sale_review_required"])
        config["tax"]["loss_credit_rate"] = 0.2
        self.assertEqual(self.sale(config=config)["status"], "unavailable")

    def test_min_tax_can_prefer_larger_gain_at_lower_rate(self):
        lots = self.lots(
            {"lot_id": "long", "acquired_date": "2024-01-01", "basis_per_share": 60},
            {"lot_id": "short", "acquired_date": "2026-01-01", "basis_per_share": 70},
        )
        result = self.sale(lots)
        self.assertEqual(result["lot_plan"][0]["lot_id"], "long")
        self.assertEqual(result["estimated_tax"], 80)

    def test_lot_totals_and_net_funding_reconcile(self):
        result = self.sale(
            self.lots(
                {"quantity": 2.3, "basis_per_share": 91.17},
                {"quantity": 7.7, "basis_per_share": 47.33},
            ),
            price=103.91,
        )
        self.assertAlmostEqual(sum(lot["quantity"] for lot in result["lot_plan"]), 10)
        self.assertAlmostEqual(
            sum(lot["estimated_tax"] for lot in result["lot_plan"]), result["estimated_tax"]
        )
        self.assertAlmostEqual(
            sum(lot["realized_gain"] for lot in result["lot_plan"]), result["realized_gain"]
        )
        self.assertAlmostEqual(
            result["gross_proceeds"] - result["estimated_tax"], result["net_proceeds_after_tax"]
        )
        cost = result["gross_proceeds"] * 0.001
        self.assertAlmostEqual(
            result["net_proceeds_after_tax"] - cost,
            result["gross_proceeds"] - result["estimated_tax"] - cost,
        )

    def test_other_account_lots_cannot_cover_requested_sale(self):
        result = self.sale(
            self.lots({"quantity": 5}, {"account_id": "retirement", "quantity": 100})
        )
        self.assertEqual(result["status"], "unavailable")

    def test_other_security_lots_are_ignored(self):
        result = self.sale(self.lots({}, {"security_id": "OTHER", "basis_per_share": -5}))
        self.assertEqual(result["status"], "estimated")
        self.assertEqual(result["estimated_tax"], 100)

    def test_duplicate_lots_and_non_usd_basis_block(self):
        self.assertEqual(
            self.sale(self.lots({"lot_id": "same"}, {"lot_id": "same"}))["status"], "unavailable"
        )
        self.assertEqual(self.sale(self.lots({"currency": "CAD"}))["status"], "unavailable")

    def test_missing_disabled_and_invalid_rates(self):
        self.assertEqual(self.sale(config={})["status"], "disabled")
        for value in [None, -1, 1.01, float("nan"), True]:
            with self.subTest(value=value):
                config = deepcopy(self.config)
                config["tax"]["short_term_rate"] = value
                self.assertEqual(self.sale(config=config)["status"], "unavailable")
        config = deepcopy(self.config)
        config["tax"]["short_term_rate"] = config["tax"]["long_term_rate"] = 0
        self.assertEqual(self.sale(config=config)["estimated_tax"], 0)

    def test_input_frame_is_unchanged(self):
        lots = self.lots({"basis_per_share": 10}, {"basis_per_share": 90})
        original = lots.copy(deep=True)
        self.sale(lots)
        pd.testing.assert_frame_equal(lots, original)


if __name__ == "__main__":
    unittest.main()
