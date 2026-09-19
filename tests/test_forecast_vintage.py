"""The shared state fills the forecast vintage a run needs, not merely its empty rows.

An owner-imported forecast from an earlier vintage is superseded and said to be
superseded, one already dated for this run keeps its own source, and an import stated
over a different horizon never counts as coverage of the horizon being read. The
mandate benchmark is covered on the shared date even when it is a fund. No network: the
frames are built from explicit dicts.
"""

import unittest

import pandas as pd

from portfolio_research.scenarios import DEFAULT_SHARED_STATE
from tests.support.securities import securities, security

AS_OF = "2026-09-18"
LABELS = {"Adverse", "Central", "Favorable"}


def forecast_rows(sid, *, date, horizon=12, probability=None) -> list[dict]:
    return [
        {
            "security_id": sid,
            "scenario": label,
            "horizon_months": horizon,
            "return_value": 0.05,
            "probability": probability,
            "basis": "subjective",
            "source": "owner CSV",
            "forecast_date": date,
            "received_at": date,
        }
        for label in sorted(LABELS)
    ]


class SharedStateVintageTests(unittest.TestCase):
    """The shared state fills the vintage this run needs, not merely empty rows."""

    def bundle(self, forecasts) -> dict:
        return {
            "securities": securities(
                security("HOLD1", owned=True),
                security("BENCH", sector=None, instrument_type="etf"),
                security("CAND1", candidate=True),
            ),
            "forecasts": pd.DataFrame(forecasts),
            "issues": [],
        }

    def config(self) -> dict:
        return {
            "allocation": {"horizon_months": 12, "shared_state": DEFAULT_SHARED_STATE},
            "mandate": {"base_currency": "USD", "benchmark_id": "BENCH"},
            "data": {"max_forecast_age_days": 45},
        }

    def applied(self, forecasts) -> dict:
        from portfolio_research.enrichment import _apply_shared_state

        bundle = self.bundle(forecasts)
        _apply_shared_state(bundle, self.config(), AS_OF)
        return bundle

    def dated(self, bundle, sid, horizon=12) -> set:
        frame = bundle["forecasts"]
        rows = frame[(frame.security_id == sid) & (frame.horizon_months == horizon)]
        return set(rows.forecast_date)

    def test_an_older_import_is_superseded_so_the_joint_set_shares_one_date(self):
        imported = forecast_rows("HOLD1", date="2026-09-04") + forecast_rows(
            "BENCH", date="2026-09-04"
        )
        bundle = self.applied(imported)
        for sid in ("HOLD1", "BENCH", "CAND1"):
            self.assertIn(AS_OF, self.dated(bundle, sid), sid)
        superseded = [
            issue for issue in bundle["issues"] if issue["code"] == "IMPORTED_FORECAST_SUPERSEDED"
        ]
        self.assertEqual({issue["security_id"] for issue in superseded}, {"HOLD1", "BENCH"})

    def test_an_import_of_the_wrong_horizon_never_counts_as_coverage(self):
        bundle = self.applied(forecast_rows("HOLD1", date=AS_OF, horizon=6))
        self.assertIn(AS_OF, self.dated(bundle, "HOLD1", horizon=12))
        self.assertIn(AS_OF, self.dated(bundle, "CAND1", horizon=12))

    def test_an_import_already_dated_for_this_run_still_wins(self):
        bundle = self.applied(forecast_rows("HOLD1", date=AS_OF))
        frame = bundle["forecasts"]
        rows = frame[frame.security_id == "HOLD1"]
        self.assertEqual(set(rows.source), {"owner CSV"})
        self.assertEqual(len(rows), 3)
        self.assertEqual(
            [i for i in bundle["issues"] if i["code"] == "IMPORTED_FORECAST_SUPERSEDED"], []
        )

    def test_the_mandate_benchmark_is_covered_even_though_it_is_a_fund(self):
        bundle = self.applied([])
        self.assertIn(AS_OF, self.dated(bundle, "BENCH"))
        self.assertNotIn(
            "BENCH",
            {
                issue.get("security_id")
                for issue in bundle["issues"]
                if issue["code"] == "NO_STATE_FOR_INSTRUMENT"
            },
        )


if __name__ == "__main__":
    unittest.main()
