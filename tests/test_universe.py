"""The dated candidate universe: acquisition paging, cache reuse and pure qualification."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from portfolio_lab.config import DEFAULTS, validate_config
from portfolio_research.universe import acquire_universe, qualify_universe

AS_OF = "2026-09-18"


def refuse_network(*args, **kwargs):
    raise AssertionError("network invoked")


class FakeScreen:
    """A Yahoo screen answering pages of synthetic quotes up to ``total``."""

    def __init__(self, total):
        self.total = total
        self.calls = []

    def quote(self, index):
        return {
            "symbol": f"S{index:04d}",
            "quoteType": "EQUITY",
            "currency": "USD",
            "exchange": "NMS",
            "fullExchangeName": "NasdaqGS",
            "longName": f"Synthetic Issuer {index}",
            "marketCap": 1_000_000_000_000 - index * 1_000_000,
            "sharesOutstanding": 1_000_000_000,
        }

    def __call__(self, query, offset=None, size=None, sortField=None, sortAsc=None, **kwargs):
        self.calls.append(
            {"offset": offset, "size": size, "sortField": sortField, "sortAsc": sortAsc}
        )
        start = int(offset or 0)
        count = max(min(int(size or 250), self.total - start), 0)
        return {
            "start": start,
            "count": count,
            "total": self.total,
            "quotes": [self.quote(start + step) for step in range(count)],
        }


class UniverseCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = {
            "research": {"path": str(self.root / "research.sqlite")},
            "data": {
                "mode": "live",
                "provider_min_interval_seconds": 0,
                "provider_timeout_seconds": 5,
                "universe_acquire_size": 1500,
                "universe_size": 1000,
            },
            "signals": dict(DEFAULTS["signals"]),
        }

    def tearDown(self):
        self.temp.cleanup()


class AcquireUniverseTests(UniverseCase):
    def test_paging_stops_at_the_reported_total_and_archives_every_page(self):
        screen, issues = FakeScreen(620), []
        with patch("yfinance.screen", screen):
            result = acquire_universe(self.config, as_of=AS_OF, refresh=True, issues=issues)
        self.assertEqual(issues, [])
        self.assertEqual([call["offset"] for call in screen.calls], [0, 250, 500])
        self.assertEqual({call["size"] for call in screen.calls}, {250})
        self.assertEqual({call["sortField"] for call in screen.calls}, {"intradaymarketcap"})
        self.assertEqual({call["sortAsc"] for call in screen.calls}, {False})
        self.assertEqual(len(result["rows"]), 620)
        self.assertEqual(result["rows"][0]["symbol"], "S0000")
        self.assertEqual(len({row["symbol"] for row in result["rows"]}), 620)
        self.assertEqual(result["meta"]["total_reported"], 620)
        self.assertEqual(result["meta"]["pages"], 3)
        self.assertEqual(result["meta"]["as_of"], AS_OF)
        self.assertEqual(result["meta"]["adapter_version"], DEFAULTS["data"]["market_adapter"])
        self.assertIn("region=us", result["meta"]["scope"])
        self.assertIn("not proof of a complete universe", result["meta"]["coverage_claim"])
        row = result["rows"][0]
        self.assertEqual(row["currency"], "USD")
        self.assertEqual(row["quote_type"], "EQUITY")
        self.assertEqual(row["exchange"], "NMS")
        self.assertEqual(row["full_exchange_name"], "NasdaqGS")
        self.assertEqual(row["market_cap"], 1_000_000_000_000)
        self.assertEqual(row["shares_outstanding"], 1_000_000_000)
        self.assertTrue(row["source_id"].startswith("yahoo_screen:"))
        self.assertTrue(row["received_at"].endswith("+00:00"))
        archived = list((self.root / "cache" / "yahoo_screen").glob("*.source.json"))
        self.assertEqual(len(archived), 4)  # three pages and the acquisition manifest

    def test_acquisition_size_bounds_the_last_page(self):
        screen, issues = FakeScreen(5000), []
        self.config["data"]["universe_acquire_size"] = 300
        with patch("yfinance.screen", screen):
            result = acquire_universe(self.config, as_of=AS_OF, refresh=True, issues=issues)
        self.assertEqual([(c["offset"], c["size"]) for c in screen.calls], [(0, 250), (250, 50)])
        self.assertEqual(len(result["rows"]), 300)
        self.assertEqual(result["meta"]["pages"], 2)
        self.assertEqual(result["meta"]["total_reported"], 5000)
        self.assertEqual(issues, [])

    def test_cached_acquisition_is_reused_without_the_provider(self):
        screen = FakeScreen(620)
        with patch("yfinance.screen", screen):
            first = acquire_universe(self.config, as_of=AS_OF, refresh=True, issues=[])
        issues = []
        with patch("yfinance.screen", refuse_network):
            cached = acquire_universe(self.config, as_of=AS_OF, refresh=False, issues=issues)
        self.assertEqual(issues, [])
        self.assertEqual(cached["rows"], first["rows"])
        self.assertEqual(cached["meta"]["total_reported"], 620)
        self.assertEqual(cached["meta"]["pages"], 3)
        self.assertEqual(len(screen.calls), 3)

    def test_provider_failure_leaves_no_rows_and_one_issue(self):
        def broken(*args, **kwargs):
            raise RuntimeError("screen unavailable https://query.example/v1?token=secret")

        issues = []
        with patch("yfinance.screen", broken):
            result = acquire_universe(self.config, as_of=AS_OF, refresh=True, issues=issues)
        self.assertEqual(result["rows"], [])
        self.assertEqual([issue["code"] for issue in issues], ["UNIVERSE_UNAVAILABLE"])
        self.assertNotIn("secret", issues[0]["message"])

    def test_offline_without_a_cached_acquisition_reports_the_same_issue(self):
        issues = []
        with patch("yfinance.screen", refuse_network):
            result = acquire_universe(self.config, as_of=AS_OF, refresh=False, issues=issues)
        self.assertEqual(result["rows"], [])
        self.assertEqual([issue["code"] for issue in issues], ["UNIVERSE_UNAVAILABLE"])

    def test_a_different_as_of_does_not_read_another_days_acquisition(self):
        with patch("yfinance.screen", FakeScreen(300)):
            acquire_universe(self.config, as_of=AS_OF, refresh=True, issues=[])
        issues = []
        with patch("yfinance.screen", refuse_network):
            result = acquire_universe(self.config, as_of="2026-10-18", refresh=False, issues=issues)
        self.assertEqual(result["rows"], [])
        self.assertEqual([issue["code"] for issue in issues], ["UNIVERSE_UNAVAILABLE"])


BILLION = 1_000_000_000
RECEIVED_AT = "2026-09-18T20:05:00+00:00"
SOURCE_ID = "yahoo_screen:0123456789abcdef"


def screen_row(symbol, cap, *, currency="USD", exchange="NMS", quote_type="EQUITY", name=None):
    """One acquired screen row in the shape ``acquire_universe`` returns."""
    return {
        "symbol": symbol,
        "name": name or f"{symbol} Holdings Inc",
        "exchange": exchange,
        "currency": currency,
        "quote_type": quote_type,
        "market_cap": float(cap),
        "shares_outstanding": 1.0 * BILLION,
        "full_exchange_name": "NasdaqGS",
        "received_at": RECEIVED_AT,
        "source_id": SOURCE_ID,
    }


def synthetic_universe():
    """Thirty acquired rows covering every qualification decision, plus their listings."""
    rows = [
        screen_row("MEGA", 900 * BILLION),
        screen_row("BIGA", 500 * BILLION, name="Big Media Inc Class A"),
        screen_row("BIGB", 499.9 * BILLION, name="Big Media Inc Class B"),
        screen_row("PFDA-P", 300 * BILLION),
        screen_row("ADRCO", 280 * BILLION, name="Adrco Holdings ADR"),
        screen_row("EURO", 270 * BILLION, currency="EUR"),
        screen_row("FINCO", 260 * BILLION),
        screen_row("FUND", 250 * BILLION, quote_type="ETF"),
        screen_row("TSECO.TO", 240 * BILLION, currency="CAD", exchange="TOR"),
        screen_row("NOLIST", 230 * BILLION),
    ]
    rows += [screen_row(f"ORD{n:02d}", (210 - 10 * n) * BILLION) for n in range(1, 21)]
    sectors = {"BIGA": "Communication Services", "BIGB": "Communication Services"}
    listings = {
        row["symbol"]: {
            "country": "United States",
            "sector": sectors.get(
                row["symbol"], "Financials" if row["symbol"] == "FINCO" else "Technology"
            ),
            "instrument_type": "equity",
            "name": row["name"],
        }
        for row in rows
        if row["symbol"] != "NOLIST"  # an unresolved listing cannot establish US domicile
    }
    # Distinct CIKs except for the two classes of one issuer; the ORD rows take the
    # 101-120 range, so no unrelated row may share a key with them.
    ciks = {"MEGA": 1, "BIGA": 11, "BIGB": 11, "PFDA-P": 4, "ADRCO": 5, "EURO": 6}
    ciks.update({"FINCO": 7, "FUND": 8, "NOLIST": 10})
    ciks.update({f"ORD{n:02d}": 100 + n for n in range(1, 21)})

    def issuer_lookup(row):
        cik = ciks.get(row.get("symbol"))
        return None if cik is None else f"cik:{cik:010d}"

    return rows, listings, issuer_lookup


class QualifyUniverseTests(UniverseCase):
    def setUp(self):
        super().setUp()
        self.config["data"]["universe_size"] = 5
        self.rows, self.listings, self.issuer_lookup = synthetic_universe()

    def qualify(self, *, owned=(), watchlist=("ORD20",), rows=None, listings=None):
        return qualify_universe(
            self.rows if rows is None else rows,
            owned_securities=list(owned),
            listings=self.listings if listings is None else listings,
            issuer_lookup=self.issuer_lookup,
            config=self.config,
            watchlist=list(watchlist),
            as_of=AS_OF,
        )

    def test_candidates_are_the_ranked_survivors_plus_the_watchlist(self):
        result = self.qualify()
        self.assertEqual(
            [row["security_id"] for row in result["candidates"]],
            ["MEGA", "BIGA", "ORD01", "ORD02", "ORD03", "ORD20"],
        )
        self.assertEqual(result["method_version"], "universe-1")
        self.assertTrue(all(row["in_scope"] for row in result["candidates"]))
        self.assertTrue(all(row["owned"] is False for row in result["candidates"]))
        notes = {note["symbol"]: note["reasons"] for note in result["notes"]}
        self.assertEqual(notes, {"ORD20": ["watchlist"]})

    def test_every_rejected_row_is_recorded_with_its_reasons(self):
        result = self.qualify()
        reasons = {entry["symbol"]: entry["reasons"] for entry in result["exclusions"]}
        self.assertEqual(reasons["FUND"], ["not_company_equity"])
        self.assertEqual(reasons["TSECO.TO"], ["non_us_listing", "currency_mismatch"])
        self.assertEqual(reasons["EURO"], ["currency_mismatch"])
        self.assertEqual(reasons["PFDA-P"], ["not_ordinary_common"])
        self.assertEqual(reasons["ADRCO"], ["depositary_receipt"])
        self.assertEqual(reasons["NOLIST"], ["us_domicile_unverified"])
        self.assertEqual(reasons["BIGB"], ["duplicate_share_class"])
        self.assertEqual(reasons["FINCO"], ["separate_sector_model_required"])
        self.assertEqual(reasons["ORD04"], ["beyond_size_limit"])
        self.assertNotIn("ORD20", reasons)
        self.assertNotIn("MEGA", reasons)
        duplicate = next(e for e in result["exclusions"] if e["symbol"] == "BIGB")
        self.assertEqual(duplicate["representative_symbol"], "BIGA")
        self.assertEqual(len(result["exclusions"]), 24)

    def test_counts_and_scope_label_report_the_narrower_scope(self):
        result = self.qualify()
        self.assertEqual(
            result["counts"],
            {
                "acquired": 30,
                "qualified": 22,
                "in_scope": 6,
                "excluded": 24,
                "size_limit": 5,
                "watchlist_included": 1,
                "watchlist_not_acquired": 0,
                "owned_retained": 0,
                "by_reason": {
                    "not_company_equity": 1,
                    "non_us_listing": 1,
                    "currency_mismatch": 2,
                    "not_ordinary_common": 1,
                    "depositary_receipt": 1,
                    "us_domicile_unverified": 1,
                    "duplicate_share_class": 1,
                    "separate_sector_model_required": 1,
                    "beyond_size_limit": 16,
                },
            },
        )
        self.assertEqual(
            result["scope_label"],
            "Largest 5 eligible US ordinary common issuers by Yahoo intraday market cap "
            "on 2026-09-18, plus 1 watchlist symbol",
        )

    def test_candidate_rows_carry_the_securities_frame_facts(self):
        [mega] = [row for row in self.qualify()["candidates"] if row["security_id"] == "MEGA"]
        self.assertEqual(
            mega,
            {
                "security_id": "MEGA",
                "ticker": "MEGA",
                "issuer_id": "cik:0000000001",
                "name": "MEGA Holdings Inc",
                "sector": "Technology",
                "instrument_type": "equity",
                "currency": "USD",
                "cik": "0000000001",
                "eligible": True,
                "market_cap": 900.0 * BILLION,
                "market_cap_as_of": AS_OF,
                "market_cap_available_at": RECEIVED_AT,
                "market_cap_received_at": RECEIVED_AT,
                "exchange": "NMS",
                "share_class": None,
                "domicile": "US",
                "equity_type": "ordinary_common",
                "resolution_status": "resolved",
                "owned": False,
                "in_scope": True,
                "source_id": SOURCE_ID,
            },
        )

    def test_owned_securities_are_retained_beyond_the_size_limit(self):
        result = self.qualify(owned=["ORD19"], watchlist=[])
        owned = {row["security_id"]: row for row in result["candidates"]}
        self.assertIn("ORD19", owned)
        self.assertIs(owned["ORD19"]["owned"], True)
        self.assertEqual(result["counts"]["owned_retained"], 1)
        self.assertNotIn("ORD19", {entry["symbol"] for entry in result["exclusions"]})
        self.assertEqual(
            result["scope_label"],
            "Largest 5 eligible US ordinary common issuers by Yahoo intraday market cap "
            "on 2026-09-18, plus 1 owned security",
        )

    def test_liquidity_and_price_are_never_claimed_by_qualification(self):
        self.config["signals"]["minimum_dollar_volume"] = 1e18
        self.config["signals"]["minimum_price"] = 1e6
        result = self.qualify()
        self.assertEqual(
            [row["security_id"] for row in result["candidates"]],
            ["MEGA", "BIGA", "ORD01", "ORD02", "ORD03", "ORD20"],
        )

    def test_inconsistent_issuer_caps_exclude_the_representative_by_default(self):
        rows = [
            screen_row("INCA", 500 * BILLION, name="Inconsistent Inc Class A"),
            screen_row("INCB", 470 * BILLION, name="Inconsistent Inc Class B"),
        ]
        listings = {row["symbol"]: dict(self.listings["MEGA"], name=row["name"]) for row in rows}

        def lookup(row):
            return "cik:0000000222"

        self.issuer_lookup = lookup
        result = self.qualify(rows=rows, listings=listings, watchlist=[])
        reasons = {entry["symbol"]: entry["reasons"] for entry in result["exclusions"]}
        self.assertEqual(reasons["INCA"], ["issuer_cap_inconsistent"])
        self.assertEqual(reasons["INCB"], ["duplicate_share_class"])
        self.assertEqual(result["candidates"], [])

        self.config["signals"]["require_consistent_issuer_cap"] = False
        kept = self.qualify(rows=rows, listings=listings, watchlist=[])
        self.assertEqual([row["security_id"] for row in kept["candidates"]], ["INCA"])
        self.assertEqual(
            [note["reasons"] for note in kept["notes"] if note["symbol"] == "INCA"],
            [["issuer_cap_inconsistent"]],
        )

    def test_issuers_without_a_cik_fall_back_to_the_normalized_name(self):
        rows = [
            screen_row("NAMA", 90 * BILLION, name="Same Issuer Inc."),
            screen_row("NAMB", 80 * BILLION, name="Same Issuer, Inc"),
        ]
        listings = {row["symbol"]: dict(self.listings["MEGA"], name=row["name"]) for row in rows}
        self.issuer_lookup = lambda row: None
        result = self.qualify(rows=rows, listings=listings, watchlist=[])
        self.assertEqual([row["security_id"] for row in result["candidates"]], ["NAMA"])
        self.assertEqual(
            [entry["reasons"] for entry in result["exclusions"] if entry["symbol"] == "NAMB"],
            [["duplicate_share_class"]],
        )
        self.assertIsNone(result["candidates"][0]["cik"])


class UniverseConfigTests(unittest.TestCase):
    def test_defaults_carry_the_documented_sizes_and_budgets(self):
        config = validate_config({})
        self.assertEqual(config["data"]["universe_acquire_size"], 1500)
        self.assertEqual(config["data"]["universe_size"], 1000)
        self.assertEqual(config["data"]["candidate_enrichment_limit"], 1000)
        self.assertEqual(config["data"]["provider_time_budget_seconds"], 1800)
        self.assertEqual(config["signals"]["watchlist"], [])
        self.assertIs(config["signals"]["require_consistent_issuer_cap"], True)
        self.assertEqual(config["signals"]["candidate_brief_limit"], 20)

    def test_watchlist_entries_must_be_listing_symbols(self):
        self.assertEqual(
            validate_config({"signals": {"watchlist": ["AAPL", "BRK-B", "RY.TO"]}})["signals"][
                "watchlist"
            ],
            ["AAPL", "BRK-B", "RY.TO"],
        )
        for entry in ["", "bad symbol", "$$$", ".TO", "A" * 21, 5, None, ["AAPL"]]:
            with self.subTest(entry=entry):
                with self.assertRaises(ValueError):
                    validate_config({"signals": {"watchlist": [entry]}})
        with self.assertRaises(ValueError):
            validate_config({"signals": {"watchlist": "AAPL"}})

    def test_watchlist_is_bounded_at_five_hundred_entries(self):
        symbols = [f"S{index:04d}" for index in range(500)]
        self.assertEqual(
            len(validate_config({"signals": {"watchlist": symbols}})["signals"]["watchlist"]), 500
        )
        with self.assertRaises(ValueError):
            validate_config({"signals": {"watchlist": symbols + ["S0500"]}})

    def test_universe_sizes_budgets_and_flags_are_validated(self):
        for patch_value in [
            {"data": {"universe_acquire_size": 0}},
            {"data": {"universe_acquire_size": 250.5}},
            {"data": {"universe_size": 0}},
            {"data": {"universe_size": 2000}},
            {"data": {"candidate_enrichment_limit": -1}},
            {"data": {"provider_time_budget_seconds": 0}},
            {"data": {"provider_time_budget_seconds": 100000}},
            {"signals": {"candidate_brief_limit": 2.5}},
            {"signals": {"candidate_brief_limit": -1}},
            {"signals": {"require_consistent_issuer_cap": "yes"}},
        ]:
            with self.subTest(patch=patch_value):
                with self.assertRaises(ValueError):
                    validate_config(patch_value)

    def test_the_in_scope_universe_cannot_exceed_what_is_acquired(self):
        with self.assertRaises(ValueError):
            validate_config({"data": {"universe_acquire_size": 500, "universe_size": 1000}})
        self.assertEqual(
            validate_config({"data": {"universe_acquire_size": 500, "universe_size": 500}})["data"][
                "universe_size"
            ],
            500,
        )


if __name__ == "__main__":
    unittest.main()
