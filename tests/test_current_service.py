import json
import tempfile
import threading
import unittest
from datetime import UTC, datetime
from decimal import Decimal
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import MagicMock, patch

from portfolio.server import make_handler
from portfolio.storage import Store
from portfolio_lab.config import dashboard_patch, load_config
from portfolio_lab.demo import create_demo
from portfolio_research.service import ResearchService, default_config
from tests.support.normalization import A_ROWS, B_ROWS, LISTINGS, cad_table, listing, usd_table
from tests.test_current import cached_providers

SUNDAY_RECEIPT = "2026-09-13T20:00:00+00:00"
FRIDAY = "2026-09-11"
FRIDAY_CUTOFF = "2026-09-11T20:00:00+00:00"
SUNDAY_GENERATED = datetime(2026, 9, 13, 21, 0, tzinfo=UTC)


class SundayClock(datetime):
    """Pins the review calendar's wall clock to Sunday evening after the Sunday capture."""

    @classmethod
    def now(cls, tz=None):
        return SUNDAY_GENERATED if tz is None else SUNDAY_GENERATED.astimezone(tz)


def table(symbol="DEMO", value="10", currency="USD"):
    headers = ["Symbol", "Shares", "Last Price", "Market Value ($)"]
    rows = [[symbol, "1", value, value], ["Total Cash", "", "2.03", ""]]
    if currency:
        headers = [*headers, "Currency"]
        rows = [[*row, currency] for row in rows]
    return {
        "method": "yahoo-holdings-table-v1",
        "headers": headers,
        "rows": rows,
        "page_count": 1,
        "expected_count": len(rows),
        "completeness": "count-verified",
    }


class CurrentServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.a = self.store.add_source(
            "Labelled account", url="https://finance.yahoo.com/portfolio/p_fixture_service_a"
        )
        self.b = self.store.add_source(
            "Unlabelled account", url="https://finance.yahoo.com/portfolio/p_fixture_service_b"
        )
        with patch("portfolio.storage.now", return_value=SUNDAY_RECEIPT):
            self.batch = self.store.begin_batch([self.a, self.b])
            self.store.ingest_table(self.a, table("DEMO"), batch_id=self.batch)
            self.store.ingest_table(self.b, table("OTHER", currency=None), batch_id=self.batch)
        self.config = default_config(self.temp.name)
        self.assertEqual(Path(self.config["source"]["path"]), self.store.path.resolve())
        self.service = ResearchService(self.config)
        self.addCleanup(self.service.close)

    def monthly(self, request_key, **fields):
        job = self.service.submit({"kind": "monthly", "request_key": request_key, **fields})
        finished = self.service.wait(job["job_id"])
        self.assertIn(finished["status"], {"complete", "failed"}, finished)
        return finished

    def test_current_holdings_do_not_need_a_saved_review(self):
        response = self.service.current()
        self.assertTrue(response["supported"])
        self.assertIsNone(response["latest_run"])
        self.assertIn("dated separately", response["note"])
        current = response["current"]
        self.assertEqual(current["review_kind"], "current")
        self.assertEqual(current["collection"]["status"], "published")
        self.assertEqual(current["collection"]["batch_id"], self.batch)
        self.assertEqual(current["dates"]["collection_received_at"], SUNDAY_RECEIPT)
        self.assertIsNone(current["dates"]["source_valuation_time"])
        self.assertEqual(len(current["accounts"]), 2)
        self.assertEqual(len(current["positions"]), 2)
        self.assertFalse(current["totals"]["reconciled_nav"])
        serialized = json.dumps(response, allow_nan=False)
        self.assertNotIn("finance.yahoo.com", serialized)
        self.assertNotIn(str(self.store.path), serialized)
        self.assertNotIn(self.temp.name, serialized)

    def test_status_reports_latest_collection_and_review_kinds(self):
        status = self.service.status()
        self.assertEqual(status["schema_version"], 1)
        self.assertEqual(status["latest_collection"]["status"], "published")
        self.assertEqual(status["latest_collection"]["collection_received_at"], SUNDAY_RECEIPT)
        self.assertEqual(status["latest_collection"]["account_count"], 2)
        self.assertEqual(status["review_kinds"], ["current", "historical"])
        self.assertEqual(status["default_review_kind"], "current")
        for key in (
            "product",
            "mode",
            "dataset",
            "config",
            "runs",
            "jobs",
            "latest_run_id",
            "default_as_of",
            "providers",
            "collector_busy",
        ):
            self.assertIn(key, status)
        self.assertEqual(status["dataset"], "Yahoo holdings")
        self.assertIsNone(status["latest_run_id"])

    def test_status_survives_an_unreadable_collection(self):
        failure = ValueError("synthetic collector problem")
        with (
            patch("portfolio_research.adapter.load_collector", side_effect=failure),
            patch("portfolio_research.current.current_snapshot", side_effect=failure),
            patch("portfolio_research.service.current_snapshot", side_effect=failure, create=True),
        ):
            status = self.service.status()
        self.assertEqual(status["latest_collection"]["status"], "unavailable")
        self.assertIn("synthetic collector problem", status["latest_collection"]["error"])
        self.assertEqual(status["schema_version"], 1)

    @patch("portfolio_research.calendar.datetime", SundayClock)
    def test_saved_historical_review_is_dated_separately_and_unchanged(self):
        finished = self.monthly(
            "current-service-historical-review", as_of=FRIDAY, review_kind="historical"
        )
        runs = self.service.store.list_runs()
        if finished["status"] != "complete":
            self.assertEqual(runs, [])
            self.assertIsNone(self.service.current()["latest_run"])
            return
        run_id = finished["output"]["result"]["run_id"]
        self.assertEqual(finished["output"]["result"]["metadata"]["review_kind"], "historical")
        before = self.service.run(run_id)
        response = self.service.current()
        self.assertTrue(response["supported"])
        latest = response["latest_run"]
        self.assertEqual(latest["run_id"], run_id)
        self.assertEqual(latest["as_of"], FRIDAY)
        self.assertEqual(latest["created_at"], runs[0]["created_at"])
        self.assertEqual(latest["review_kind"], "historical")
        self.assertIsNone(latest["valuation_date"])
        self.assertIsNone(latest["collection_received_at"])
        self.assertEqual(latest["information_cutoff"], FRIDAY_CUTOFF)
        self.assertEqual(response["current"]["dates"]["collection_received_at"], SUNDAY_RECEIPT)
        self.assertEqual(response["current"]["dates"]["market_observation_date"], FRIDAY)
        self.assertEqual(self.service.run(run_id), before)
        self.assertEqual(self.service.status()["latest_run_id"], run_id)

    def test_default_review_kind_for_submitted_jobs_stays_historical(self):
        finished = self.monthly("current-service-default-kind", as_of=FRIDAY)
        if finished["status"] != "complete":
            self.assertIsNone(self.service.current()["latest_run"])
            return
        metadata = finished["output"]["result"]["metadata"]
        self.assertEqual(metadata["review_kind"], "historical")
        self.assertEqual(metadata["as_of"], FRIDAY)
        self.assertEqual(metadata["information_cutoff"], FRIDAY_CUTOFF)
        self.assertEqual(self.service.current()["latest_run"]["review_kind"], "historical")

    def test_current_review_job_uses_the_sunday_collection(self):
        finished = self.monthly("current-service-current-review", review_kind="current")
        if finished["status"] != "complete":
            self.assertIsNone(self.service.current()["latest_run"])
            return
        result = finished["output"]["result"]
        metadata = result["metadata"]
        self.assertEqual(metadata["review_kind"], "current")
        self.assertEqual(metadata["market_observation_date"], metadata["as_of"])
        self.assertEqual(metadata["information_cutoff"], result["timeline"]["generated_at"])
        self.assertEqual(metadata["collection_received_at"], SUNDAY_RECEIPT)
        self.assertEqual(result["timeline"]["review_kind"], "current")
        latest = self.service.current()["latest_run"]
        self.assertEqual(latest["run_id"], result["run_id"])
        self.assertEqual(latest["review_kind"], "current")
        self.assertEqual(latest["collection_received_at"], SUNDAY_RECEIPT)

    def test_current_review_requests_reject_explicit_dates_and_unknown_kinds(self):
        with self.assertRaises(ValueError):
            self.service.submit(
                {
                    "kind": "monthly",
                    "review_kind": "current",
                    "as_of": FRIDAY,
                    "request_key": "current-service-current-with-date",
                }
            )
        with self.assertRaises(ValueError):
            self.service.submit(
                {
                    "kind": "monthly",
                    "review_kind": "weekly",
                    "request_key": "current-service-weekly-kind",
                }
            )
        with self.assertRaises(ValueError):
            self.service.submit(
                {
                    "kind": "monthly",
                    "review_kind": None,
                    "request_key": "current-service-null-kind",
                }
            )
        self.assertEqual(self.service.status()["jobs"], [])
        self.assertTrue(self.service.current()["supported"])

    def test_monthly_request_key_created_before_review_kinds_is_returned_on_retry(self):
        """A stable scheduler key (e.g. monthly-YYYY-MM-DD) must survive the upgrade."""
        key = "monthly-2026-09-11"
        legacy_request = {
            "as_of": FRIDAY,
            "resolved_config": dashboard_patch(self.service.resolved_config(), {}),
            "supplemental": self.service.store.latest("supplemental"),
        }
        job_id, created = self.service.store.create_job("monthly", key, legacy_request)
        self.assertTrue(created)
        retried = self.service.submit({"kind": "monthly", "as_of": FRIDAY, "request_key": key})
        self.assertEqual(retried["job_id"], job_id)
        explicit = self.service.submit(
            {"kind": "monthly", "as_of": FRIDAY, "review_kind": "historical", "request_key": key}
        )
        self.assertEqual(explicit["job_id"], job_id)
        self.assertNotIn("review_kind", self.service.store.job(job_id)["payload"])
        with self.assertRaisesRegex(ValueError, "different inputs"):
            self.service.submit({"kind": "monthly", "review_kind": "current", "request_key": key})

    def test_status_reads_the_collection_without_presenting_it(self):
        """Status only needs receipt facts; it must not run identity and FX on every poll."""
        with patch(
            "portfolio_lab.pipeline._normalize_collector",
            side_effect=AssertionError("status normalized the collection"),
        ):
            status = self.service.status()
        collection = status["latest_collection"]
        self.assertEqual(collection["status"], "published")
        self.assertEqual(collection["account_count"], 2)
        self.assertEqual(collection["collection_received_at"], SUNDAY_RECEIPT)

    def test_a_current_view_without_a_base_currency_is_still_served(self):
        self.service.save_settings({"mandate": {"base_currency": None}})
        response = self.service.current()
        self.assertTrue(response["supported"])
        usd = response["current"]["totals"]["usd"]
        self.assertIsNone(usd["covered_total"])
        self.assertIsNone(usd["currency"])
        self.assertEqual(len(response["current"]["positions"]), 2)
        codes = {row["code"] for row in self.service.exceptions()["exceptions"]}
        self.assertLessEqual(codes, {"UNRESOLVED_LISTING"})
        self.assertEqual(self.service.status()["latest_collection"]["status"], "published")

    def test_current_review_kind_is_stored_with_the_request(self):
        with patch.object(self.service.executor, "submit"):
            job = self.service.submit(
                {"kind": "monthly", "review_kind": "current", "request_key": "current-kind-stored"}
            )
        self.assertEqual(self.service.store.job(job["job_id"])["payload"]["review_kind"], "current")


class DemoSourceCurrentTests(unittest.TestCase):
    def test_configured_non_collector_source_is_not_supported(self):
        with tempfile.TemporaryDirectory() as directory:
            config = load_config(create_demo(Path(directory) / "demo"))
            config["risk"]["bootstrap_samples"] = 50
            service = ResearchService(config)
            try:
                response = service.current()
                self.assertFalse(response["supported"])
                self.assertIn("saved reviews", response["reason"])
                self.assertNotIn("current", response)
                status = service.status()
                self.assertEqual(status["schema_version"], 1)
                self.assertEqual(status["review_kinds"], ["current", "historical"])
                self.assertEqual(status["default_review_kind"], "current")
                self.assertNotIn(str(Path(config["source"]["path"])), json.dumps(status))
            finally:
                service.close()


SHOP_CANDIDATES = {
    "SHOP": [
        {
            "symbol": "SHOP.TO",
            "name": "Shopify (TSX)",
            "exchange": "TOR",
            "instrument_type": "equity",
        },
        {
            "symbol": "SHOP.NE",
            "name": "Shopify (NEO)",
            "exchange": "NEO",
            "instrument_type": "equity",
        },
    ]
}
EXCEPTION_LISTINGS = {**LISTINGS, "SHOP.TO": listing("SHOP.TO", "CAD", "TOR")}


@patch("portfolio_research.calendar.datetime", SundayClock)
class CurrentExceptionServiceTests(unittest.TestCase):
    """Open exceptions from the USD gate fixture: one ambiguous listing, and no more.

    Two of these holdings are quoted outside the United States and one account labels no
    currency at all; none of that is a question, because a listing's exchange states the
    currency it is quoted in.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.a = self.store.add_source(
            "USD portfolio", url="https://finance.yahoo.com/portfolio/p_fixture_exceptions_a"
        )
        self.b = self.store.add_source(
            "CAD portfolio", url="https://finance.yahoo.com/portfolio/p_fixture_exceptions_b"
        )
        with patch("portfolio.storage.now", return_value=SUNDAY_RECEIPT):
            batch = self.store.begin_batch([self.a, self.b])
            first = self.store.ingest_table(
                self.a, usd_table([*A_ROWS, ["SHOP", "1", "100", "100"]]), batch_id=batch
            )
            second = self.store.ingest_table(
                self.b,
                cad_table([B_ROWS[0], ["VOD.L", "100", "128.75", "500.00"]]),
                batch_id=batch,
                extension_version="1.2.0",
            )
        self.snapshots = {self.a: first["snapshot_id"], self.b: second["snapshot_id"]}
        self.service = ResearchService(default_config(self.temp.name))
        self.addCleanup(self.service.close)
        self.providers = cached_providers(listings=EXCEPTION_LISTINGS, search=SHOP_CANDIDATES)
        self.providers.__enter__()
        self.addCleanup(self.providers.__exit__, None, None, None)

    def held(self):
        return {
            (row["source_id"], row["symbol"]): row
            for row in self.service.current()["current"]["positions"]
        }

    def open_exception(self, code, raw_symbol):
        records = self.service.exceptions()["exceptions"]
        return next(r for r in records if r["code"] == code and r["raw_symbol"] == raw_symbol)

    def test_exceptions_list_exactly_the_constructed_ambiguities(self):
        response = self.service.exceptions()
        pairs = sorted(
            (row["code"], row["raw_symbol"])
            for row in response["exceptions"]
            if row["scope"] == "listing"
        )
        # SHOP matches two listings and nothing says which; that is the only question the
        # sources and provider metadata could not answer between them.
        self.assertEqual(pairs, [("AMBIGUOUS_LISTING", "SHOP")])
        self.assertNotIn("UNKNOWN_VALUE_CURRENCY", {row["code"] for row in response["exceptions"]})
        self.assertEqual(response["count"], len(response["exceptions"]))
        self.assertEqual(response["generated_at"], SUNDAY_GENERATED.isoformat())
        for record in response["exceptions"]:
            self.assertRegex(record["key"], r"^[0-9a-f]{40}$")
        ambiguous = self.open_exception("AMBIGUOUS_LISTING", "SHOP")
        self.assertEqual([row["symbol"] for row in ambiguous["candidates"]], ["SHOP.TO", "SHOP.NE"])
        serialized = json.dumps(response, allow_nan=False)
        self.assertNotIn(self.temp.name, serialized)
        self.assertNotIn("finance.yahoo.com", serialized)

    def test_current_exposes_covered_usd_totals(self):
        current = self.service.current()["current"]
        usd = current["totals"]["usd"]
        held = {(row["source_id"], row["symbol"]): row for row in current["positions"]}
        self.assertEqual(len(held), 5)
        # Every captured position is either presented in USD or counted as unconverted,
        # and the covered total is exactly the amounts that were converted, counted once.
        self.assertEqual(usd["covered_position_count"] + usd["unconverted_position_count"], 5)
        self.assertEqual(
            Decimal(usd["covered_total"]),
            sum(
                Decimal(row["market_value_usd"])
                for row in held.values()
                if row["market_value_usd"] is not None
            )
            + sum(
                Decimal(row["usd"]["cash"])
                for row in current["accounts"]
                if row["usd"]["cash"] is not None
            ),
        )
        # A listing nothing can choose between is still a question. The currency of a bare
        # US ticker is not one, even while its listing stays ambiguous.
        self.assertEqual(current["identity"]["ambiguous"], 1)
        self.assertEqual(held[(self.a, "SHOP")]["value_currency"], "USD")
        self.assertEqual(held[(self.a, "SHOP")]["market_value_usd"], "100")
        self.assertEqual(held[(self.b, "RY.TO")]["value_currency"], "CAD")
        self.assertNotIn(
            "UNKNOWN_VALUE_CURRENCY", {row["code"] for row in current["open_exceptions"]}
        )

    def attest_position_currency(self, source_id, currency):
        self.service.import_input(
            {
                "kind": "supplemental",
                "data": {
                    "version": 1,
                    "accounts": [
                        {
                            "source_id": source_id,
                            "snapshot_id": self.snapshots[source_id],
                            "position_currency": currency,
                        }
                    ],
                    "securities": [],
                    "tax_lots": [],
                },
            }
        )

    def test_an_attested_position_currency_overrides_what_is_assumed(self):
        """Nothing asks the owner for a currency, but an explicit answer still wins.

        A page that labels its values is believed, and a page that labels nothing is read
        in the review's own base currency. An owner who knows a source reports in another
        currency can still say so as dated account evidence, and that outranks both.
        """
        before = self.held()
        # The USD account labels nothing, so its values are read as USD; its Toronto
        # holding is still quoted in CAD, which is a fact about the listing, not the page.
        self.assertEqual(before[(self.a, "RY.TO")]["value_currency"], "USD")
        self.assertEqual(before[(self.a, "RY.TO")]["quote_currency"], "CAD")
        self.assertIsNone(before[(self.a, "RY.TO")]["fx_pair"])

        self.attest_position_currency(self.a, "CAD")
        held = self.held()
        for symbol in ("RY.TO", "AAPL"):
            row = held[(self.a, symbol)]
            self.assertEqual(row["value_currency"], "CAD", symbol)
            self.assertEqual(row["value_currency_basis"], "attested", symbol)
            self.assertEqual(row["fx_pair"], "CADUSD", symbol)
        # The attestation says what the account reports, not where a security trades:
        # every listing keeps its own quote currency and its own subunit.
        self.assertEqual(held[(self.a, "RY.TO")]["quote_currency"], "CAD")
        self.assertEqual(held[(self.a, "AAPL")]["quote_currency"], "USD")
        self.assertEqual(held[(self.b, "VOD.L")]["quote_currency"], "GBp")
        self.assertEqual(held[(self.b, "VOD.L")]["quote_unit_factor"], 100)
        self.assertEqual(held[(self.b, "VOD.L")]["price_major"], "1.2875")
        # The account that labels its own rows is unaffected: the row label still wins.
        self.assertEqual(held[(self.b, "RY.TO")]["value_currency"], "CAD")
        self.assertEqual(held[(self.b, "RY.TO")]["value_currency_basis"], "row")
        self.assertNotIn(
            "UNKNOWN_VALUE_CURRENCY",
            {row["code"] for row in self.service.exceptions()["exceptions"]},
        )
        # The owner's evidence is stored as its own version; nothing was edited in place.
        self.assertEqual(len(self.service.store.records("supplemental")), 1)

    def test_a_read_value_currency_reads_differently_from_a_dated_conversion(self):
        """Both accounts end in USD; the page says which one was converted to get there.

        A labels nothing, so its values are read as reported in the presentation currency:
        an identity, with no rate and no FX pair. B labels its rows CAD, so its values are
        converted at a dated observation, which names its pair and its date. The stated
        basis is what tells those two apart, and it replaces the exception the collector
        used to raise about A's unlabelled rows.
        """
        current = self.service.current()["current"]
        accounts = {row["source_id"]: row for row in current["accounts"]}
        read, converted = accounts[self.a]["value_currency"], accounts[self.b]["value_currency"]
        self.assertEqual((read["currency"], read["basis"]), ("USD", "presentation"))
        self.assertIn("read as reported in USD", read["label"])
        self.assertEqual((converted["currency"], converted["basis"]), ("CAD", "row"))
        held = self.held()
        self.assertIsNone(held[(self.a, "AAPL")]["fx_pair"])
        self.assertIsNone(held[(self.a, "AAPL")]["fx_observation_date"])
        self.assertEqual(held[(self.b, "RY.TO")]["fx_pair"], "CADUSD")
        self.assertTrue(held[(self.b, "RY.TO")]["fx_observation_date"])
        self.assertNotIn(
            "MISSING_POSITION_CURRENCY",
            {row["code"] for row in current["exceptions"]},
        )

    def test_security_listing_resolution_maps_the_chosen_listing_from_today(self):
        shop = self.open_exception("AMBIGUOUS_LISTING", "SHOP")
        self.service.save_resolution(
            {
                "key": shop["key"],
                "kind": "security_listing",
                "source_id": self.a,
                "snapshot_id": self.snapshots[self.a],
                "values": {"security_id": "SHOP.TO"},
            }
        )
        [mapping] = self.service.store.latest("supplemental")["securities"]
        self.assertEqual(
            mapping,
            {
                "source_id": self.a,
                "raw_symbol": "SHOP",
                "security_id": "SHOP.TO",
                "issuer_id": "listing:SHOP.TO",
                "ticker": "SHOP.TO",
                "name": "SHOP.TO fixture listing",
                "instrument_type": "equity",
                "currency": "CAD",
                "exchange": "TOR",
                "valid_from": "2026-09-13",
            },
        )
        keys = {row["key"] for row in self.service.exceptions()["exceptions"]}
        self.assertNotIn(shop["key"], keys)
        self.assertEqual(self.service.current()["current"]["identity"]["mapped"], 1)

    def attest_valuation_date(self, day=FRIDAY):
        self.service.import_input(
            {
                "kind": "supplemental",
                "data": {
                    "version": 1,
                    "accounts": [
                        {
                            "source_id": self.a,
                            "snapshot_id": self.snapshots[self.a],
                            "valuation_date": day,
                        }
                    ],
                    "securities": [],
                    "tax_lots": [],
                },
            }
        )

    def test_a_listing_choice_closes_the_exception_of_an_attested_valuation_date(self):
        """The mapping must apply on the date the adapter evaluates for this snapshot."""
        self.attest_valuation_date()
        shop = self.open_exception("AMBIGUOUS_LISTING", "SHOP")
        before = self.service.exceptions()["count"]
        saved = self.service.save_resolution(
            {
                "key": shop["key"],
                "kind": "security_listing",
                "source_id": self.a,
                "snapshot_id": self.snapshots[self.a],
                "values": {"security_id": "SHOP.TO"},
            }
        )
        [mapping] = self.service.store.latest("supplemental")["securities"]
        self.assertEqual(mapping["valid_from"], FRIDAY)
        remaining = self.service.exceptions()
        self.assertNotIn(shop["key"], {row["key"] for row in remaining["exceptions"]})
        self.assertEqual(saved["remaining"], remaining["count"])
        self.assertLess(remaining["count"], before)
        # One supplemental version for the attestation and one for the resolution.
        self.assertEqual(len(self.service.store.records("supplemental")), 2)

    def resolve_shop(self, listing_id="SHOP.TO"):
        shop = self.open_exception("AMBIGUOUS_LISTING", "SHOP")
        self.service.save_resolution(
            {
                "key": shop["key"],
                "kind": "security_listing",
                "source_id": self.a,
                "snapshot_id": self.snapshots[self.a],
                "values": {"security_id": listing_id},
            }
        )
        return self.service.store.latest("supplemental")["securities"]

    def carry_mappings_and_attest(self, mappings, day=FRIDAY):
        """Attest the account's valuation date while keeping the mappings already saved."""
        self.service.import_input(
            {
                "kind": "supplemental",
                "data": {
                    "version": 1,
                    "accounts": [
                        {
                            "source_id": self.a,
                            "snapshot_id": self.snapshots[self.a],
                            "valuation_date": day,
                        }
                    ],
                    "securities": mappings,
                    "tax_lots": [],
                },
            }
        )

    def test_a_resolution_dated_before_an_earlier_answer_leaves_one_mapping_per_date(self):
        """A backwards valid_from must not reopen the symbol on the next collection."""
        from portfolio_research.current import current_snapshot

        first = self.resolve_shop()
        self.assertEqual([row["valid_from"] for row in first], ["2026-09-13"])
        self.carry_mappings_and_attest(first)
        mappings = self.resolve_shop()
        windows = sorted((row["valid_from"], row.get("valid_to")) for row in mappings)
        self.assertEqual(windows, [(FRIDAY, "2026-09-12"), ("2026-09-13", None)])

        supplemental = {"version": 1, "accounts": [], "securities": mappings, "tax_lots": []}
        snapshot = current_snapshot(
            self.service.config["source"]["path"],
            supplemental=supplemental,
            config=self.service.resolved_config(),
        )
        codes = {row["code"] for row in snapshot["exceptions"]}
        self.assertNotIn("AMBIGUOUS_SECURITY_ALIAS", codes)
        self.assertNotIn("UNRESOLVED_LISTING", {row["code"] for row in snapshot["open_exceptions"]})
        shop = [row for row in snapshot["positions"] if row["symbol"] == "SHOP"]
        self.assertEqual([row["security_id"] for row in shop], ["SHOP.TO"])
        self.assertEqual([row["resolution_status"] for row in shop], ["resolved"])

    def test_two_listings_dated_backwards_do_not_both_apply(self):
        from portfolio_research import resolutions

        later = {
            "source_id": self.a,
            "raw_symbol": "SHOP",
            "security_id": "SHOP.TO",
            "valid_from": "2026-09-13",
        }
        earlier = {**later, "security_id": "SHOP.NE", "valid_from": FRIDAY}
        merged = resolutions.merged_securities(
            {**resolutions.EMPTY_SUPPLEMENTAL, "securities": [later]}, earlier
        )
        applying = [
            row
            for row in merged["securities"]
            if row["valid_from"] <= "2026-09-13"
            and (not row.get("valid_to") or row["valid_to"] >= "2026-09-13")
        ]
        self.assertEqual([row["security_id"] for row in applying], ["SHOP.TO"])

    def test_a_resolution_that_would_leave_the_exception_open_is_not_written(self):
        from portfolio_research import resolutions

        shop = self.open_exception("AMBIGUOUS_LISTING", "SHOP")
        payload = {
            "key": shop["key"],
            "kind": "security_listing",
            "source_id": self.a,
            "snapshot_id": self.snapshots[self.a],
            "values": {"security_id": "SHOP.TO"},
        }

        mapping = resolutions.listing_mapping

        def future(config, record, values, valid_from, *, issues):
            return mapping(config, record, values, "2099-01-01", issues=issues)

        with (
            patch("portfolio_research.resolutions.listing_mapping", side_effect=future),
            self.assertRaisesRegex(ValueError, "does not close"),
        ):
            self.service.save_resolution(payload)
        self.assertEqual(self.service.store.records("supplemental"), [])
        self.assertEqual(self.service.store.records("resolution"), [])
        self.assertIn(shop["key"], {row["key"] for row in self.service.exceptions()["exceptions"]})

    def test_saving_a_resolution_presents_the_collection_at_most_twice(self):
        from portfolio_research import current as current_module

        shop = self.open_exception("AMBIGUOUS_LISTING", "SHOP")
        calls = []
        original = current_module.current_snapshot

        def counted(*args, **kwargs):
            calls.append(kwargs.get("supplemental"))
            return original(*args, **kwargs)

        with patch("portfolio_research.current.current_snapshot", side_effect=counted):
            self.service.save_resolution(
                {
                    "key": shop["key"],
                    "kind": "security_listing",
                    "source_id": self.a,
                    "snapshot_id": self.snapshots[self.a],
                    "values": {"security_id": "SHOP.TO"},
                }
            )
        self.assertEqual(len(calls), 2, calls)

    def test_invalid_resolutions_are_rejected_without_writing_records(self):
        shop = self.open_exception("AMBIGUOUS_LISTING", "SHOP")
        valid = {
            "key": shop["key"],
            "kind": "security_listing",
            "source_id": self.a,
            "snapshot_id": self.snapshots[self.a],
            "values": {"security_id": "SHOP.TO"},
        }
        cases = {
            "unknown key": ({**valid, "key": "0" * 40}, "no longer open"),
            "wrong kind": ({**valid, "kind": "account_facts"}, "kind"),
            "other account": ({**valid, "source_id": self.b}, "account"),
            "bad symbol": ({**valid, "values": {"security_id": "not a symbol"}}, "exact listing"),
            "no values": ({**valid, "values": {}}, "security_id"),
            "extra field": ({**valid, "note": "x"}, "fields"),
            "unsupported value": ({**valid, "values": {"cash": "5"}}, "fields"),
            "not an object": ([], "object"),
        }
        for label, (payload, message) in cases.items():
            with self.subTest(label=label), self.assertRaisesRegex(ValueError, message):
                self.service.save_resolution(payload)
        self.assertEqual(self.service.store.records("supplemental"), [])
        self.assertEqual(self.service.store.records("resolution"), [])


class ResolutionRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.research = MagicMock()
        self.research.exceptions.return_value = {"exceptions": [], "count": 0, "generated_at": None}
        self.research.save_resolution.return_value = {"record_id": "r1", "remaining": 0}
        browser = MagicMock()
        browser.status.return_value = {"busy": False}
        server = ThreadingHTTPServer(("127.0.0.1", 0), lambda *args: None)
        server.RequestHandlerClass = make_handler(
            Store(self.temp.name), browser, server.server_port, self.research
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.port = server.server_port
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.token = self.request("GET", "/api/state")[1]["token"]

    def request(self, method, path, body=None, headers=None):
        connection = HTTPConnection("127.0.0.1", self.port, timeout=3)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        payload = json.loads(response.read())
        connection.close()
        return response.status, payload

    def test_exceptions_route_returns_the_service_listing(self):
        status, payload = self.request("GET", "/api/research/exceptions")
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"exceptions": [], "count": 0, "generated_at": None})

    def test_resolution_route_is_token_gated_and_reports_invalid_input(self):
        body = json.dumps({"key": "k", "kind": "security_listing", "values": {}})
        status, _ = self.request(
            "POST", "/api/research/resolutions", body, {"Content-Type": "application/json"}
        )
        self.assertEqual(status, 403)
        self.research.save_resolution.assert_not_called()
        headers = {"Content-Type": "application/json", "X-Local-Token": self.token}
        status, payload = self.request("POST", "/api/research/resolutions", body, headers)
        self.assertEqual(status, 201)
        self.assertEqual(payload, {"record_id": "r1", "remaining": 0})
        self.research.save_resolution.assert_called_once_with(json.loads(body))
        self.research.save_resolution.side_effect = ValueError("This exception is no longer open.")
        status, payload = self.request("POST", "/api/research/resolutions", body, headers)
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "This exception is no longer open.")


class ResolutionFunctionSizeTests(unittest.TestCase):
    """Resolving an exception stays readable: no function of this path exceeds 50 lines."""

    LIMIT = 50

    def oversized(self, module, names=None):
        import ast
        import inspect

        tree = ast.parse(Path(inspect.getsourcefile(module)).read_text())
        return {
            node.name: node.end_lineno - node.lineno + 1
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and (names is None or node.name in names)
            and node.end_lineno - node.lineno + 1 >= self.LIMIT
        }

    def test_save_resolution_and_its_helpers_stay_under_fifty_lines(self):
        from portfolio_research import resolutions, service

        self.assertEqual(
            self.oversized(service, {"save_resolution", "_resolved_supplemental", "_mapping_date"}),
            {},
        )
        self.assertEqual(self.oversized(resolutions), {})
