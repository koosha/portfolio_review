"""Refreshing the recorded provider fixtures: what the recorder must be able to write.

``scripts/provider_smoke.py --record`` is the only path that re-pins the contract
fixtures when a provider ships a new shape, so it has to survive the objects the pinned
yfinance actually hands over — a metadata Mapping carrying ``pandas.Timestamp`` values
and a lazy key that costs an extra request to touch, and filing rows carrying
``datetime.date``. None of that is JSON, and a recorder that cannot encode it reports
the fixture as a provider refusal and leaves the stale file in place.

It also checks the other half of a fixture's life: that a recorded file actually
reaches a checkout rather than being swallowed by the repository's privacy patterns.

No network: every provider object here is a stand-in built in the test.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import provider_smoke  # noqa: E402

from tests.support import provider_fixtures as fixtures  # noqa: E402

LAZY = "tradingPeriods"


class LiveMetadata(Mapping):
    """``Ticker.history_metadata`` as yfinance builds it: a Mapping with parsed dates.

    ``format_history_metadata`` converts ``firstTradeDate`` and ``regularMarketTime`` to
    ``pandas.Timestamp`` before the property returns, and ``tradingPeriods`` is resolved
    lazily — reading it costs another intraday request and answers with a DataFrame.
    """

    def __init__(self, values, touched):
        self._values = dict(values)
        self._touched = touched

    def __getitem__(self, key):
        if key == LAZY:
            self._touched.append(key)
            return pd.DataFrame({"start": [pd.Timestamp("2026-09-11T09:30:00-04:00")]})
        return self._values[key]

    def __iter__(self):
        return iter((*self._values, LAZY))

    def __len__(self):
        return len(self._values) + 1


class RecorderTicker:
    """The provider objects the recorder reads, in the types the pinned release returns."""

    def __init__(self, touched):
        self.ticker = "AAPL"
        self._touched = touched

    @property
    def history_metadata(self):
        return LiveMetadata(
            {
                "currency": "USD",
                "symbol": "AAPL",
                "exchangeName": "NMS",
                "fullExchangeName": "NasdaqGS",
                "instrumentType": "EQUITY",
                "firstTradeDate": pd.Timestamp("1980-12-12 09:30:00-05:00"),
                "regularMarketTime": pd.Timestamp("2026-09-11 16:00:00-04:00"),
                "exchangeTimezoneName": "America/New_York",
                "regularMarketPrice": 103.0,
            },
            self._touched,
        )

    @property
    def sec_filings(self):
        return [
            {
                "date": date(2026, 8, 3),
                "epochDate": 1785715200,
                "type": "10-Q",
                "title": "Quarterly report",
                "edgarUrl": "https://www.sec.gov/Archives/edgar/data/1234501/q2.htm",
                "exhibits": {"EX-31.1": "https://www.sec.gov/Archives/ex311.htm"},
            }
        ]

    @property
    def info(self):
        return {"financialCurrency": "USD", "sector": "Technology"}

    @property
    def news(self):
        return []

    @property
    def calendar(self):
        return {"Earnings Date": [date(2026, 10, 29)]}

    def history(self, start=None, end=None, auto_adjust=False, actions=True, **kwargs):
        index = pd.DatetimeIndex([pd.Timestamp("2026-09-10", tz="America/New_York")], name="Date")
        return pd.DataFrame(
            {
                "Open": [100.0],
                "High": [101.0],
                "Low": [99.0],
                "Close": [100.5],
                "Adj Close": [100.5],
                "Volume": [1000],
                "Dividends": [0.0],
                "Stock Splits": [0.0],
            },
            index=index,
        )


def _statement_frame():
    return pd.DataFrame(
        {pd.Timestamp("2025-12-31"): [980.0]},
        index=pd.Index(["Total Revenue"]),
    )


class RecordedShapeTests(unittest.TestCase):
    """Every recorded payload has to survive ``json.dumps`` with no default= escape."""

    def setUp(self):
        self.touched = []
        self.ticker = RecorderTicker(self.touched)

    def test_price_metadata_records_the_fields_the_adapter_reads_as_json(self):
        payload = provider_smoke._record_prices(fixtures, self.ticker, "2026-09-11")
        json.dumps(payload)  # A TypeError here is the recorder failing, not the provider.
        metadata = payload["history_metadata"]
        self.assertEqual(metadata["currency"], "USD")
        self.assertEqual(metadata["exchangeName"], "NMS")
        self.assertEqual(metadata["instrumentType"], "EQUITY")
        self.assertEqual(metadata["exchangeTimezoneName"], "America/New_York")
        self.assertEqual(metadata["firstTradeDate"], "1980-12-12")

    def test_recording_metadata_never_resolves_the_lazy_trading_periods_key(self):
        """Touching it costs an extra intraday request and answers with a DataFrame."""
        provider_smoke._record_prices(fixtures, self.ticker, "2026-09-11")
        provider_smoke._record_profile(fixtures, self.ticker)
        self.assertEqual(self.touched, [])

    def test_profile_metadata_records_as_json(self):
        payload = provider_smoke._record_profile(fixtures, self.ticker)
        json.dumps(payload)
        self.assertEqual(payload["history_metadata"]["exchangeName"], "NMS")

    def test_filing_rows_record_their_dates_and_exhibits_as_json(self):
        frames = (
            "quarterly_income_stmt",
            "quarterly_balance_sheet",
            "quarterly_cashflow",
            "income_stmt",
            "balance_sheet",
            "cashflow",
        )
        for name in frames:
            setattr(type(self.ticker), name, property(lambda self: _statement_frame()))
        payload = provider_smoke._record_statements(fixtures, self.ticker)
        json.dumps(payload)
        [filing] = payload["sec_filings"]
        self.assertEqual(filing["date"], "2026-08-03")
        self.assertIsInstance(filing["exhibits"], dict)

    def test_news_filings_record_as_json(self):
        payload = provider_smoke._record_news(fixtures, self.ticker)
        json.dumps(payload)
        self.assertEqual(payload["sec_filings"][0]["date"], "2026-08-03")


class SkippedFixtureTests(unittest.TestCase):
    """What the operator is told when a fixture is not refreshed."""

    def arguments(self):
        return argparse.Namespace(
            symbols=["AAPL"],
            fund=["XIC.TO"],
            cik="320193",
            as_of="2026-09-11",
            sec_contact="Portfolio Review owner@example.org",
        )

    def record(self, jobs):
        """Run the recorder over ``jobs`` against a throwaway fixture directory."""
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(fixtures, "FIXTURES", directory))
        self.enterContext(patch.object(provider_smoke, "_record_jobs", lambda *a, **k: jobs))
        return provider_smoke.record_run(self.arguments())

    def test_a_refused_provider_is_named_without_its_request(self):
        """yfinance raises on a response whose URL carries the minted session crumb."""
        crumb = "https://query2.finance.yahoo.com/v8/finance/chart/AAPL?crumb=SeCrEtCrUmB"

        def refuse():
            raise HTTPError(crumb, 403, "Forbidden", {}, None)

        report = self.record({fixtures.PRICES: refuse})
        label = report["skipped"][fixtures.PRICES]
        self.assertEqual(label, "HTTP 403")
        self.assertNotIn("crumb", json.dumps(report))
        self.assertNotIn("SeCrEtCrUmB", json.dumps(report))

    def test_a_fixture_the_recorder_cannot_encode_is_not_a_provider_refusal(self):
        """A recorder that cannot write is broken; calling it "skipped" hides that."""

        def unencodable():
            return {"received_at": pd.Timestamp("2026-09-11 16:00:00-04:00")}

        with self.assertRaises(TypeError):
            self.record({fixtures.PRICES: unencodable})


class PublishedFixtureTests(unittest.TestCase):
    """A recorded fixture has to reach a checkout, or the suite fails for everyone else.

    The privacy patterns in .gitignore are deliberately broad and unrooted — ``*.csv``,
    ``*token*.json``, ``*session*.json``, ``*.db`` — because they are the backstop that
    keeps real holdings out of a push. They apply under ``tests/fixtures`` too, so a
    fixture named to collide with one is ignored, shows a green ``git status`` to the
    author who added it, and is simply missing for everybody who clones. This walks the
    fixture tree the way git does rather than trusting the naming convention to hold.
    """

    def test_every_checked_in_fixture_is_publishable(self):
        root = Path(__file__).resolve().parent.parent
        paths = [
            str(path.relative_to(root))
            for path in sorted((root / "tests" / "fixtures").rglob("*"))
            # Operating-system droppings such as .DS_Store are ignored on purpose and are
            # nobody's fixture; only files a person added are checked.
            if path.is_file() and not any(part.startswith(".") for part in path.parts)
        ]
        self.assertTrue(paths, "no fixtures were found to check")
        try:
            result = subprocess.run(
                ["git", "check-ignore", "--stdin"],
                cwd=root,
                input="\n".join(paths),
                capture_output=True,
                text=True,
            )
        except OSError as error:  # No git on this machine: the hazard cannot be checked.
            self.skipTest(f"git is unavailable: {error}")
        ignored = [line for line in result.stdout.splitlines() if line.strip()]
        self.assertEqual(
            ignored,
            [],
            "These fixtures are ignored by .gitignore and would never reach a checkout. "
            "Rename them away from the privacy patterns rather than narrowing a pattern: "
            f"{ignored}",
        )


if __name__ == "__main__":
    unittest.main()
