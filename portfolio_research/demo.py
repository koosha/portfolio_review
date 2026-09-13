"""Explicitly synthetic company research for the standalone demo command."""

import hashlib


def demo_workspace():
    eps = {
        "starting_price": 100,
        "currency": "USD",
        "horizon_months": 12,
        "eps_convention": "forward",
        "pe_convention": "forward",
        "scenarios": [
            {"label": "adverse", "eps": 4, "pe": 15, "distributions_per_starting_share": 1},
            {"label": "central", "eps": 5.5, "pe": 20, "distributions_per_starting_share": 1},
            {"label": "favorable", "eps": 6, "pe": 25, "distributions_per_starting_share": 1},
        ],
    }
    dcf = {
        "currency": "USD",
        "monetary_unit": "units",
        "company_type": "nonfinancial",
        "discount_rate": 0.10,
        "terminal_growth_rate": 0.02,
        "terminal_roic": 0.10,
        "sbc_treatment": "expensed_in_ebit",
        "debt": 100,
        "preferred": 20,
        "nci": 10,
        "excess_cash": 40,
        "nonoperating_assets": 5,
        "diluted_shares": 10,
        "projections": [
            {
                "year": year,
                "revenue": 500,
                "ebit": 125,
                "tax_rate": 0.20,
                "depreciation": 10,
                "capex": 30,
                "change_working_capital": 0,
                "invested_capital": capital,
                "stock_compensation": 5,
            }
            for year, capital in ((1, 500), (2, 520))
        ],
    }
    assessment = {
        "version": 1,
        "security_id": "SIM01",
        "author": "Synthetic example",
        "horizon_months": 12,
        "decision_cutoff": "2026-08-31T16:00:00-04:00",
        "generated_at": "2026-08-31T21:00:00Z",
        "review_status": "reviewed",
        "reviewed_by": "Synthetic reviewer",
        "reviewed_at": "2026-08-31T20:30:00Z",
        "market_expectations": "Synthetic starting price of 100 is an educational valuation assumption, independent of the simulated market-price series.",
        "thesis": "Simulated reinvestment sustains operating earnings.",
        "counter_thesis": "Simulated margin pressure lowers profitability.",
        "catalysts": [{"description": "Synthetic product milestone", "target_date": "2027-03-01"}],
        "balance_sheet_risks": "Synthetic debt and minority/preferred claims reduce common equity value.",
        "invalidation_conditions": "Simulated operating profit falls below the adverse case.",
        "sources": [
            {
                "id": "synthetic-report",
                "locator": "https://example.invalid/synthetic-report",
                "version": "synthetic-original",
                "content_hash": hashlib.sha256(b"synthetic evidence").hexdigest(),
                "published_at": "2026-08-15T12:00:00Z",
                "received_at": "2026-08-16T12:00:00Z",
            }
        ],
        "facts": [
            {
                "id": "synthetic-revenue",
                "field": "revenue",
                "value": 500,
                "units": "USD",
                "source_id": "synthetic-report",
                "origin": "manual",
                "review_status": "reviewed",
                "reviewed_by": "Synthetic reviewer",
                "reviewed_at": "2026-08-30T12:00:00Z",
            }
        ],
        "operating_assumptions": [
            {
                "field": "forward_eps",
                "value": 5.5,
                "units": "USD/share",
                "horizon_months": 12,
                "basis": "Synthetic hypothetical assumption",
                "author": "Synthetic example",
                "version": 1,
                "origin": "manual",
            }
        ],
    }
    return {
        "assessments": {"SIM01": assessment},
        "valuations": {
            "SIM01": {
                "eps": eps,
                "dcf": dcf,
                "origin": "manual",
                "source": "Synthetic demonstration only",
                "version": 1,
                "horizon_months": 12,
            }
        },
    }
