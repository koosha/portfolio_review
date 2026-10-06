"""Publication-aware quarterly accounting from supplied SEC company facts."""

from copy import deepcopy
from unittest import TestCase

from portfolio_research.sec_statements import extract_sec_quarters
from portfolio_research.statements import ttm_from_quarters

SID = "security:test"
RECEIVED = "2025-03-01T12:00:00+00:00"
SOURCE = "source:sec"


def fact(value, start, end, filed, accession, form="10-Q"):
    row = {"val": value, "end": end, "filed": filed, "accn": accession, "form": form}
    if start is not None:
        row["start"] = start
    return row


def payload(**tags):
    return {"facts": {"us-gaap": {tag: {"units": units} for tag, units in tags.items()}}}


def history():
    fiscal_start = "2024-01-01"
    boundaries = [
        ("2024-03-31", "2024-05-01", "Q1", "10-Q"),
        ("2024-06-30", "2024-08-01", "Q2", "10-Q"),
        ("2024-09-30", "2024-11-01", "Q3", "10-Q"),
        ("2024-12-31", "2025-02-20", "FY", "10-K"),
    ]
    cumulative = {
        "Revenues": [100, 230, 380, 600],
        "GrossProfit": [40, 90, 160, 240],
        "OperatingIncomeLoss": [20, 50, 80, 130],
        "NetIncomeLoss": [15, 40, 65, 105],
        "NetIncomeLossAvailableToCommonStockholdersBasic": [12, 34, 53, 83],
        "NetCashProvidedByUsedInOperatingActivities": [30, 80, 140, 220],
        "PaymentsToAcquirePropertyPlantAndEquipment": [5, 15, 30, 50],
    }
    tags = {
        tag: {
            "USD": [
                fact(value, fiscal_start, *boundary) for value, boundary in zip(values, boundaries)
            ]
        }
        for tag, values in cumulative.items()
    }
    for tag, values in {
        "Assets": [400, 430, 450, 500],
        "CashAndCashEquivalentsAtCarryingValue": [50, 60, 70, 100],
        "DebtAndCapitalLeaseObligations": [100, 100, 95, 90],
    }.items():
        tags[tag] = {
            "USD": [fact(value, None, *boundary) for value, boundary in zip(values, boundaries)]
        }
    tags["EarningsPerShareDiluted"] = {
        "USD/shares": [
            fact(value, fiscal_start, *boundary)
            for value, boundary in zip([1.2, 3.4, 5.3, 8.3], boundaries)
        ],
    }
    tags["WeightedAverageNumberOfDilutedSharesOutstanding"] = {
        "shares": [fact(10, fiscal_start, *boundary) for boundary in boundaries],
    }
    return payload(**tags)


def extract(data, as_of="2025-03-01", **kwargs):
    return extract_sec_quarters(data, SID, as_of, RECEIVED, SOURCE, **kwargs)


def by_end(rows):
    return {row["period_end"]: row for row in rows}


class SecQuarterHistoryTests(TestCase):
    def test_reported_quarter_stubs_with_overlapping_or_missing_days_do_not_make_ttm(self):
        for start, code in [
            ("2024-03-24", "TTM_QUARTER_OVERLAP"),
            ("2024-04-02", "TTM_QUARTER_FLOW_GAP"),
        ]:
            data = payload(
                Revenues={
                    "USD": [
                        fact(100, "2024-01-01", "2024-03-31", "2024-05-01", "Q1"),
                        fact(100, start, "2024-06-30", "2024-08-01", "Q2"),
                        fact(100, "2024-07-01", "2024-09-30", "2024-11-01", "Q3"),
                        fact(100, "2024-10-01", "2024-12-31", "2025-02-20", "FY", "10-K"),
                    ]
                }
            )
            quarters = extract(data)
            self.assertEqual(len(quarters), 4)
            issues = []
            self.assertIsNone(ttm_from_quarters(quarters, issues=issues))
            self.assertEqual(issues[0]["code"], code)

    def test_reported_first_quarter_and_cumulative_flows_produce_real_quarters(self):
        rows = extract(history())
        self.assertEqual(
            [row["period_end"] for row in rows],
            ["2024-12-31", "2024-09-30", "2024-06-30", "2024-03-31"],
        )
        q = by_end(rows)
        self.assertEqual(q["2024-03-31"]["revenue"], 100)
        self.assertEqual(q["2024-06-30"]["revenue"], 130)
        self.assertEqual(q["2024-06-30"]["operating_cash_flow"], 50)
        self.assertEqual(q["2024-09-30"]["operating_cash_flow"], 60)
        self.assertEqual(q["2024-12-31"]["operating_cash_flow"], 80)
        self.assertEqual(q["2024-12-31"]["income_common"], 30)
        self.assertEqual(
            q["2024-12-31"]["provenance"]["revenue"]["method"], "annual_minus_nine_months"
        )
        self.assertEqual(q["2024-06-30"]["period_start"], "2024-04-01")
        self.assertEqual(q["2024-12-31"]["assets"], 500)
        self.assertEqual(q["2024-12-31"]["debt"], 90)
        self.assertEqual(q["2024-12-31"]["cash"], 100)

    def test_direct_quarter_revenue_takes_precedence_over_ytd_difference(self):
        data = history()
        rows = data["facts"]["us-gaap"]["Revenues"]["units"]["USD"]
        rows.append(fact(125, "2024-04-01", "2024-06-30", "2024-08-01", "Q2"))
        row = by_end(extract(data))["2024-06-30"]
        self.assertEqual(row["revenue"], 125)
        self.assertEqual(row["provenance"]["revenue"]["method"], "reported_quarter")
        self.assertEqual(row["operating_cash_flow"], 50)

    def test_nonadditive_eps_and_weighted_share_counts_are_direct_only(self):
        data = history()
        tags = data["facts"]["us-gaap"]
        tags["EarningsPerShareDiluted"]["units"]["USD/shares"].append(
            fact(2.1, "2024-04-01", "2024-06-30", "2024-08-01", "Q2")
        )
        tags["WeightedAverageNumberOfDilutedSharesOutstanding"]["units"]["shares"].append(
            fact(11, "2024-04-01", "2024-06-30", "2024-08-01", "Q2")
        )
        q = by_end(extract(data))
        self.assertEqual(q["2024-03-31"]["diluted_eps"], 1.2)
        self.assertEqual(q["2024-06-30"]["diluted_eps"], 2.1)
        self.assertEqual(q["2024-06-30"]["diluted_shares"], 11)
        self.assertIsNone(q["2024-09-30"]["diluted_eps"])
        self.assertIsNone(q["2024-12-31"]["diluted_eps"])
        self.assertIsNone(q["2024-12-31"]["diluted_shares"])
        self.assertEqual(
            q["2024-06-30"]["provenance"]["diluted_eps"]["components"][0]["unit"], "USD/shares"
        )
        self.assertEqual(
            q["2024-06-30"]["provenance"]["diluted_shares"]["components"][0]["unit"], "shares"
        )

    def test_filing_date_cutoff_and_future_periods_are_excluded(self):
        data = history()
        self.assertEqual([row["period_end"] for row in extract(data, "2024-08-01")], ["2024-03-31"])
        self.assertEqual(
            [row["period_end"] for row in extract(data, "2024-08-02")], ["2024-06-30", "2024-03-31"]
        )
        data["facts"]["us-gaap"]["Revenues"]["units"]["USD"].append(
            fact(900, "2025-04-01", "2025-06-30", "2024-01-01", "IMPOSSIBLE")
        )
        self.assertNotIn("2025-06-30", by_end(extract(data)))

    def test_restatement_does_not_change_an_earlier_information_set(self):
        data = history()
        original = next(
            row
            for row in data["facts"]["us-gaap"]["Revenues"]["units"]["USD"]
            if row["accn"] == "Q2"
        )
        data["facts"]["us-gaap"]["Revenues"]["units"]["USD"].append(
            {**original, "val": 250, "filed": "2024-09-10", "accn": "Q2A", "form": "10-Q/A"}
        )
        before = by_end(extract(data, "2024-09-10"))["2024-06-30"]
        after = by_end(extract(data, "2024-09-11"))["2024-06-30"]
        self.assertEqual(before["revenue"], 130)
        self.assertEqual(after["revenue"], 150)
        self.assertEqual(after["accession"], "Q2A")
        self.assertEqual(after["available_at"], "2024-09-11")
        self.assertIsNone(after["assets"])

    def test_current_filing_comparative_is_preferred_to_an_older_previous_ytd(self):
        data = history()
        rows = data["facts"]["us-gaap"]["NetCashProvidedByUsedInOperatingActivities"]["units"][
            "USD"
        ]
        rows.append(fact(35, "2024-01-01", "2024-03-31", "2024-08-01", "Q2"))
        q2 = by_end(extract(data))["2024-06-30"]
        self.assertEqual(q2["operating_cash_flow"], 45)
        self.assertEqual(
            [part["accession"] for part in q2["provenance"]["operating_cash_flow"]["components"]],
            ["Q2", "Q2"],
        )

    def test_tag_unit_and_fiscal_start_mismatches_do_not_fill_missing_flows(self):
        for mutation in ("tag", "unit", "start"):
            with self.subTest(mutation=mutation):
                data = history()
                tags = data["facts"]["us-gaap"]
                rows = tags["Revenues"]["units"]["USD"]
                q1 = next(row for row in rows if row["accn"] == "Q1")
                if mutation == "tag":
                    rows.remove(q1)
                    tags["SalesRevenueNet"] = {"units": {"USD": [q1]}}
                elif mutation == "unit":
                    rows.remove(q1)
                    tags["Revenues"]["units"]["EUR"] = [q1]
                else:
                    q1["start"] = "2024-01-02"
                q2 = by_end(extract(data))["2024-06-30"]
                self.assertIsNone(q2["revenue"])
                self.assertEqual(q2["operating_cash_flow"], 50)

    def test_previous_ytd_published_after_current_filing_cannot_be_used(self):
        data = history()
        rows = data["facts"]["us-gaap"]["NetCashProvidedByUsedInOperatingActivities"]["units"][
            "USD"
        ]
        next(row for row in rows if row["accn"] == "Q1")["filed"] = "2024-08-05"
        q2 = by_end(extract(data))["2024-06-30"]
        self.assertIsNone(q2["operating_cash_flow"])

    def test_conflicting_exact_context_and_missing_quarter_are_not_fabricated(self):
        data = history()
        rows = data["facts"]["us-gaap"]["Revenues"]["units"]["USD"]
        rows.append(fact(999, "2024-01-01", "2024-06-30", "2024-08-01", "Q2"))
        self.assertIsNone(by_end(extract(data))["2024-06-30"]["revenue"])
        just_annual = payload(
            Revenues={"USD": [fact(1000, "2024-01-01", "2024-12-31", "2025-02-20", "FY", "10-K")]}
        )
        self.assertEqual(extract(just_annual), [])
        just_balances = payload(
            Assets={"USD": [fact(1000, None, "2024-12-31", "2025-02-20", "FY", "10-K")]}
        )
        self.assertEqual(extract(just_balances), [])

    def test_negative_capex_and_explicit_common_earnings_are_preserved(self):
        data = history()
        tags = data["facts"]["us-gaap"]
        next(
            row
            for row in tags["PaymentsToAcquirePropertyPlantAndEquipment"]["units"]["USD"]
            if row["accn"] == "Q1"
        )["val"] = -5
        del tags["NetIncomeLossAvailableToCommonStockholdersBasic"]
        q1 = by_end(extract(data))["2024-03-31"]
        self.assertEqual(q1["capex"], -5)
        self.assertIsNone(q1["income_common"])
        self.assertEqual(q1["net_income"], 15)
        self.assertEqual(q1["earnings_definition"], "consolidated_unqualified")
        self.assertTrue(any("Negative gross capex" in warning for warning in q1["data_warnings"]))

    def test_source_metadata_and_input_payload_are_preserved(self):
        data = history()
        original = deepcopy(data)
        q2 = by_end(extract(data))["2024-06-30"]
        self.assertEqual(data, original)
        self.assertEqual(q2["source_policy"], "original_filing")
        self.assertEqual(q2["source_id"], SOURCE)
        self.assertEqual(q2["received_at"], RECEIVED)
        for part in q2["provenance"]["operating_cash_flow"]["components"]:
            self.assertEqual(part["source_id"], SOURCE)
            self.assertEqual(part["received_at"], RECEIVED)
            self.assertTrue(part["available_at"] <= q2["available_at"])

    def test_calendar_history_boundary_and_distinct_quarter_cap(self):
        rows = []
        for year in range(2019, 2025):
            for start, end, filed in [
                (f"{year}-01-01", f"{year}-03-31", f"{year}-05-01"),
                (f"{year}-04-01", f"{year}-06-30", f"{year}-08-01"),
                (f"{year}-07-01", f"{year}-09-30", f"{year}-11-01"),
                (f"{year}-10-01", f"{year}-12-31", f"{year + 1}-02-20"),
            ]:
                rows.append(fact(100, start, end, filed, end))
        data = payload(Revenues={"USD": rows})
        selected = extract(data)
        self.assertEqual(len(selected), 12)
        self.assertTrue(all("2022-03-01" < row["period_end"] <= "2025-03-01" for row in selected))
        self.assertEqual(len(extract(data, years=1)), 4)
        with self.assertRaises(ValueError):
            extract(data, years=4)

    def test_annual_minus_nonadjacent_or_incompatible_nine_months_is_refused(self):
        data = payload(
            Revenues={
                "USD": [
                    fact(200, "2024-01-01", "2024-06-30", "2024-08-01", "Q2"),
                    fact(500, "2024-01-01", "2024-12-31", "2025-02-20", "FY", "10-K"),
                ]
            }
        )
        self.assertNotIn("2024-12-31", by_end(extract(data)))
