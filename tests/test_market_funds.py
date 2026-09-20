"""Adapter fund disclosure: top holdings, their snapshot basis, sectors and fund overview."""

import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from portfolio_research.market_data import fund_disclosure
from tests.support.market_data_adapter import AdapterCase


class FundDisclosureTests(AdapterCase):
    def test_top_holdings_carry_an_undated_snapshot_basis_and_a_cash_row(self):
        result = self.call(fund_disclosure, "XIC.TO")
        holdings = {row["issuer_id"]: row for row in result["holdings"]}
        self.assertEqual(set(holdings), {"listing:RY.TO", "listing:SHOP.TO", "CASH"})
        row = holdings["listing:RY.TO"]
        self.assertEqual(row["fund_id"], "XIC.TO")
        self.assertAlmostEqual(row["weight"], 0.06)
        self.assertEqual(row["disclosure_basis"], "provider_snapshot_undated")
        self.assertEqual(
            row["holdings_date"],
            datetime.fromisoformat(row["received_at"])
            .astimezone(ZoneInfo("America/New_York"))
            .date()
            .isoformat(),
        )
        # The snapshot is knowable from its New York day, not from the fetch instant:
        # a receipt stamp always falls after a current review's information cutoff.
        self.assertEqual(row["available_at"], row["holdings_date"])
        self.assertGreater(row["received_at"], row["available_at"])
        self.assertAlmostEqual(holdings["CASH"]["weight"], 0.02)
        self.assertLessEqual(sum(row["weight"] for row in result["holdings"]), 1.0)

    def test_an_issuer_lookup_resolves_holdings_to_issuer_ids(self):
        result = self.call(fund_disclosure, "XIC.TO", issuer_lookup=lambda meta: "cik:0001000275")
        issuers = {row["issuer_id"] for row in result["holdings"]}
        self.assertEqual(issuers, {"cik:0001000275", "CASH"})

    def test_sector_weightings_are_mapped_to_the_application_vocabulary(self):
        result = self.call(fund_disclosure, "XIC.TO")
        sectors = {row["sector"]: row["weight"] for row in result["sectors"]}
        self.assertEqual(
            sectors,
            {"Financials": 0.35, "Technology": 0.10, "Real Estate": 0.03, "Materials": 0.11},
        )
        self.assertEqual({row["fund_id"] for row in result["sectors"]}, {"XIC.TO"})
        self.assertEqual(result["fund_overview"]["legal_type"], "Exchange Traded Fund")
        self.assertEqual(result["fund_overview"]["category"], "Canadian Equity")

    def test_a_security_without_fund_data_reports_one_issue(self):
        self.assertIsNone(self.call(fund_disclosure, "AAPL"))
        self.assertEqual([issue["code"] for issue in self.issues], ["PROVIDER_FETCH_FAILED"])


if __name__ == "__main__":
    unittest.main()
