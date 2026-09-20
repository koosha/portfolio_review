"""Provider archive and cache: sanitized keys, exact key matching and archive failures."""

import unittest
from pathlib import Path
from unittest.mock import patch

from portfolio_research.market_data import fund_disclosure, price_history
from tests.support.market_data_adapter import AS_OF, AdapterCase, EmptyKeyFunds, FakeTicker


class EmptyKeyTicker(FakeTicker):
    @property
    def funds_data(self):
        self._record("funds_data")
        return EmptyKeyFunds()


class ArchiveTests(AdapterCase):
    def test_a_key_that_sanitizes_to_nothing_never_reaches_the_archive(self):
        """An unserializable key would otherwise end the run for every security."""
        with patch("yfinance.Ticker", EmptyKeyTicker):
            result = fund_disclosure(
                self.config,
                self.security("XIC.TO"),
                refresh=True,
                issues=self.issues,
                as_of=AS_OF,
            )
        self.assertIsNotNone(result)
        self.assertEqual(self.issues, [])
        self.assertEqual({row["sector"] for row in result["sectors"]}, {"Technology"})
        cash = [row for row in result["holdings"] if row["issuer_id"] == "CASH"][0]
        self.assertAlmostEqual(cash["weight"], 0.02)

    def test_a_failing_archive_becomes_an_issue_rather_than_ending_the_run(self):
        def explode(*args, **kwargs):
            raise TypeError("'<' not supported between instances of 'NoneType' and 'str'")

        with patch("portfolio_lab.providers._archive", side_effect=explode):
            self.assertIsNone(self.call(price_history, "AAPL"))
        self.assertEqual([issue["code"] for issue in self.issues], ["PROVIDER_ARCHIVE_FAILED"])
        self.assertEqual(self.issues[0]["security_id"], "AAPL")


class ProviderCacheTests(AdapterCase):
    """A cache lookup costs one key's receipts, not the whole provider directory."""

    def test_a_lookup_reads_only_the_receipts_of_its_own_key(self):
        from portfolio_lab.providers import _archive, _cached

        for index in range(5):
            _archive(
                self.config,
                "yahoo_prices",
                f"SYM{index}",
                {"rows": index},
                "https://finance.yahoo.com/quote/SYM",
                f"2026-09-1{index}T00:00:00+00:00",
            )
        opened = []
        original = Path.read_text

        def counted(path, *args, **kwargs):
            if path.name.endswith(".source.json"):
                opened.append(path.name)
            return original(path, *args, **kwargs)

        with patch.object(Path, "read_text", counted):
            payload, received_at, source = _cached(self.config, "yahoo_prices", "SYM3")
        self.assertEqual(payload, {"rows": 3})
        self.assertEqual(received_at, "2026-09-13T00:00:00+00:00")
        self.assertEqual(source["key"], "SYM3")
        self.assertEqual(len(opened), 1)

    def test_a_key_whose_sanitized_name_collides_is_still_matched_exactly(self):
        from portfolio_lab.providers import _archive, _cached

        stamp = "2026-09-11T00:00:00+00:00"
        _archive(self.config, "yahoo_prices", "A/B", {"rows": "slash"}, "https://x.test", stamp)
        _archive(self.config, "yahoo_prices", "A:B", {"rows": "colon"}, "https://x.test", stamp)
        payload, _, source = _cached(self.config, "yahoo_prices", "A:B")
        self.assertEqual(payload, {"rows": "colon"})
        self.assertEqual(source["key"], "A:B")


if __name__ == "__main__":
    unittest.main()
