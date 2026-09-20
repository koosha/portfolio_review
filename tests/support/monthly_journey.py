"""One synthetic monthly review the owner can drive end to end, with no network.

A collector holding two Yahoo-shaped accounts, a fake paired Chrome that answers the
collection, and a provider patch that injects dated synthetic research into the bundle
the review freezes. Account A labels its values in USD; account B carries no currency
column at all, and still raises no question: both hold a bare ticker, which is a US
listing quoted in USD, so the exchange states the currency the source page left out.

Every number here is simulated. This is not market data, and nothing in this module
reaches a provider: creating ``providers-offline`` in the data directory makes the same
patch point raise, so a recalculation that reached for one would fail loudly.
"""

import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from portfolio.storage import Store
from portfolio_lab import pipeline, providers
from portfolio_lab.config import load_config
from portfolio_lab.demo import create_demo
from portfolio_research import enrichment
from portfolio_research.adapter import account_id, validate_supplemental
from portfolio_research.briefs import build_brief
from portfolio_research.enrichment import coverage_report
from portfolio_research.proposals import propose_dcf_inputs, propose_eps_model
from portfolio_research.server import make_server
from portfolio_research.service import default_config
from tests.test_workflow import FakeChrome

# The two collected symbols, mapped onto the simulated securities the demo inputs price.
HELD = ("SIM01", "SIM03")
ACCOUNTS = (
    ("Synthetic labelled account", "p_fixture_monthly_labelled", "SIM01", "USD"),
    ("Synthetic unlabelled account", "p_fixture_monthly_unlabelled", "SIM03", None),
)
OFFLINE_FLAG = "providers-offline"
# The dated snapshot evidence the owner supplies. A collection captured today is valued
# on the day it was captured, so this follows the clock rather than pinning a date the
# review would then have to call stale.
VALUATION_DATE = datetime.now(UTC).date().isoformat()
# The simulated screen: every company the demo inputs price, so the fund look-through
# can name a sector for each issuer it reaches. Two of the unowned ones are on the
# owner's watchlist, which is the stated reason they appear as new candidates.
UNIVERSE = tuple(f"SIM{index:02d}" for index in range(1, 25))
CANDIDATES = ("SIM07", "SIM11")
# Only the simulated securities this review actually knows are dealable: naming one the
# bundle never carries would be an unresolved security, not a permission.
TRADABLE = UNIVERSE
# The simulated fund the mandate names as its approved benchmark.
BENCHMARK = "SIMETF"
# Snapshots are numbered as they are captured; this journey never makes more than this
# many per account, so every one of them carries the owner's reconciliation evidence.
ATTESTED_SNAPSHOTS = 12
# One captured position per account, plus the cash line beneath it.
SHARES = 20
CASH = 325.00


def table(symbol, currency=None, price=105.00):
    """A count-verified Yahoo capture; ``currency=None`` omits the column entirely.

    The captured price is the simulated close the review will value this position at, so
    quantity times price reconciles against the captured value.
    """
    headers = ["Symbol", "Shares", "Last Price", "Market Value ($)", "AC/Share", "Total Cost ($)"]
    value = round(price * SHARES, 2)
    rows = [
        [
            symbol,
            str(SHARES),
            f"{price:.2f}",
            f"{value:.2f}",
            f"{price * 0.8:.2f}",
            f"{value * 0.8:.2f}",
        ],
        ["Total Cash", "", f"{CASH:.2f}", "", "", ""],
    ]
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


class PerAccountChrome(FakeChrome):
    """The shared fake helper, answering each account with its own captured table."""

    def __init__(self, companion, tables, **fields):
        super().__init__(companion, **fields)
        self.tables = tables

    def run(self):
        while not self.stopped.wait(0.05):
            job = self.companion.take("1.2.0")
            if job is None:
                continue
            if self.delay and self.stopped.wait(self.delay):
                return
            try:
                self.companion.complete(
                    {
                        "id": job["id"],
                        "url": job["url"],
                        "ok": True,
                        "table": self.tables[job["source_id"]],
                        "error": "",
                    }
                )
            except ValueError:  # An expired job never stops the helper.
                continue
            self.completed.append(job["source_id"])


def _estimates(security_id, eps):
    return {
        "currency": "USD",
        "source_id": "estimates:" + security_id,
        "received_at": "2026-08-31T20:00:00Z",
        "eps": {"+1y": {"avg": eps, "low": eps * 0.8, "high": eps * 1.2}},
        "revenue": {"+1y": {"avg": 5400.0, "low": 5100.0, "high": 5700.0}},
    }


def _trailing(security_id):
    return {
        "period_end": "2026-06-30",
        "currency": "USD",
        "source_id": "statements:" + security_id,
        "revenue": 5000.0,
        "operating_income": 900.0,
        "net_income": 600.0,
        "operating_cash_flow": 800.0,
        "capex": 200.0,
        "depreciation": 150.0,
        "change_working_capital": 20.0,
        "tax_provision": 200.0,
        "pretax_income": 800.0,
        "assets": 9000.0,
        "debt": 1200.0,
        "cash": 700.0,
        "diluted_shares": 500.0,
        "stock_compensation": 50.0,
    }


def sector_of(security_id):
    """The demo's own sector split, so a fund's look-through can classify every issuer."""
    return "Technology" if int(security_id.removeprefix("SIM")) <= 12 else "Industrials"


def acquire_synthetic_candidates(bundle, config, as_of, *, refresh=False):
    """Stand in for the dated candidate screen a discovery pass would have acquired.

    Same contract as ``enrichment.acquire_candidates``: the qualifying unowned rows are
    added to the bundle's securities with their scope recorded beside them. Nothing is
    acquired from a provider, so the screen is available on every run.
    """
    securities = bundle.get("securities")
    if securities is None or not hasattr(securities, "columns"):
        return
    known = set(securities.get("security_id", pd.Series(dtype="string")).astype(str))
    rows = (
        [
            {
                "security_id": BENCHMARK,
                "ticker": BENCHMARK,
                "issuer_id": "SIM_FUND",
                "name": "Simulated Broad Equity Fund",
                "sector": "Fund",
                "instrument_type": "etf",
                "currency": "USD",
                "quote_currency": "USD",
                "quote_unit_factor": 1,
                "eligible": True,
                "resolution_status": "mapped",
                "resolution_basis": "approved_benchmark",
            }
        ]
        if BENCHMARK not in known
        else []
    )
    rows += [
        {
            "security_id": security_id,
            "ticker": security_id,
            "issuer_id": security_id,
            "name": "Simulated Company " + security_id,
            "sector": sector_of(security_id),
            "instrument_type": "equity",
            "equity_type": "ordinary_common",
            "currency": "USD",
            "quote_currency": "USD",
            "quote_unit_factor": 1,
            "eligible": True,
            "candidate": True,
            "resolution_status": "mapped",
            "resolution_basis": "screened_candidate",
        }
        for security_id in UNIVERSE
        if security_id not in known
    ]
    if rows:
        bundle["securities"] = pd.concat(
            [securities, pd.DataFrame(rows)], ignore_index=True
        ).reset_index(drop=True)
    acquired = len(UNIVERSE)
    bundle["universe"] = {
        "meta": {"source": "Simulated screened universe", "as_of": as_of},
        "counts": {
            "acquired": acquired,
            "qualified": acquired,
            "in_scope": acquired,
            "enriched": acquired,
        },
        "scope_label": f"Simulated screened universe of {acquired} companies",
        "scope_base": f"Simulated screened universe of {acquired} companies",
        "method_version": "monthly-journey-fixture-1",
        "notes": ["Every row in this screen is simulated; it is not a market screen."],
        "identity_unresolved": [],
        "discovery": {
            "stopped": False,
            "time_budget_exhausted": False,
            "identity_limited": False,
        },
    }
    bundle["exclusions"] = []


def _extend_prices(bundle, as_of):
    """Carry the simulated closes forward to the review date.

    The demo series stops on the day it was generated. A review run later than that has
    no total-return history through its own valuation date, so the risk model refuses
    every asset. Extending the series keeps the fixture usable on any day the suite runs.

    The continuation is flat on purpose: the collected holdings are captured at the last
    simulated close, and a position whose quantity times the latest price disagreed with
    its captured value would be an unreconciled snapshot, not a review. These are not
    simulated market moves; the measured history is the demo series behind them.
    """
    prices = bundle.get("prices")
    if prices is None or prices.empty:
        return
    last = str(prices.date.max())
    future = pd.bdate_range(
        pd.Timestamp(last) + pd.Timedelta(days=1), pd.Timestamp(as_of), inclusive="both"
    )
    if len(future) == 0:
        return
    latest = prices[prices.date == last]
    added = []
    for row in latest.to_dict("records"):
        for day in future:
            stamp = day.strftime("%Y-%m-%d")
            added.append(
                {
                    **row,
                    "date": stamp,
                    "available_at": stamp + "T20:00:00Z",
                    "received_at": stamp + "T20:00:00Z",
                }
            )
    bundle["prices"] = pd.concat([prices, pd.DataFrame(added)], ignore_index=True)


def _inject(bundle, config, as_of):
    """The dated research a live provider would answer with, injected as fixture data."""
    _extend_prices(bundle, as_of)
    research, inputs = {}, {}
    prices = bundle.get("prices")
    for security_id in (*HELD, *CANDIDATES):
        rows = prices[prices.security_id == security_id]
        if rows.empty:
            continue
        close = round(float(rows.close.iloc[-1]), 2)
        security = {
            "security_id": security_id,
            "name": "Simulated Company " + security_id,
            "sector": "Technology",
            "instrument_type": "equity",
            "equity_type": "ordinary_common",
            "currency": "USD",
            "price_source_id": "prices:" + security_id,
        }
        estimates = _estimates(security_id, close / 18.0)
        ttm, reasons = _trailing(security_id), []
        proposals = {
            "eps": propose_eps_model(
                security,
                price_major=close,
                quote_currency="USD",
                estimates=estimates,
                ttm=ttm,
                dividends_ttm_per_share=1.25,
                horizon_months=12,
                dividends_source_id="prices:" + security_id,
                issues=reasons,
            ),
            "dcf": propose_dcf_inputs(
                security,
                ttm=ttm,
                annual_statements=[ttm],
                shares_outstanding=500.0,
                price_major=close,
                currency="USD",
                quote_currency="USD",
                estimates=estimates,
                defaults={
                    "discount_rate": 0.09,
                    "terminal_growth_rate": 0.025,
                    "projection_years": 5,
                },
                issues=reasons,
            ),
            "reasons": reasons,
        }
        brief = build_brief(
            security,
            prices=[
                {
                    "security_id": security_id,
                    "date": as_of,
                    "close": close,
                    "currency": "USD",
                    "source_id": "prices:" + security_id,
                    "received_at": "2026-08-31T20:00:00Z",
                }
            ],
            estimates=estimates,
            ttm=ttm,
            events=[
                {
                    "security_id": security_id,
                    "kind": "filing",
                    "event_date": "2026-08-14",
                    "title": "Quarterly report",
                    "source_id": "filings:" + security_id,
                    "received_at": "2026-08-15T12:00:00Z",
                }
            ],
            previous={
                "as_of": "2026-07-31",
                "close": close * 0.9,
                "currency": "USD",
                "source_id": "prices:" + security_id,
                "estimates": _estimates(security_id, close / 20.0),
            },
            fx_note=None,
            as_of=as_of,
            generated_at="2026-08-31T21:00:00Z",
            horizon_months=12,
        )
        coverage = {
            "prices": "ok",
            "statements": "ok",
            "estimates": "ok",
            "fund_disclosures": "not_applicable",
            "events": "ok",
            "profile": "ok",
        }
        research[security_id] = {"brief": brief, "proposals": proposals, "coverage": coverage}
        inputs[security_id] = {
            "symbol": security_id,
            "currency": "USD",
            "quote_currency": "USD",
            "estimates": estimates,
            "fund_overview": {},
            "coverage": coverage,
        }
    bundle["research_inputs"] = inputs
    bundle["coverage"] = coverage_report(bundle, config)
    bundle["research"] = research
    if os.environ.get("MONTHLY_JOURNEY_DEBUG"):
        priced = sorted(set(prices.security_id)) if prices is not None else []
        print(
            json.dumps(
                {
                    "debug": "bundle",
                    "priced": priced[:30],
                    "securities": sorted(set(bundle["securities"].security_id)),
                    "researched": sorted(research),
                }
            ),
            flush=True,
        )
    bundle["fund_sectors"] = pd.DataFrame(
        [
            {
                "fund_id": "SIMETF",
                "sector": sector,
                "weight": weight,
                "as_of": as_of,
                "source_id": "fund:SIMETF",
                "received_at": "2026-08-31T20:00:00Z",
            }
            for sector, weight in (("Technology", 0.6), ("Industrials", 0.3))
        ]
    )


def patch_providers(config, offline_flag):
    """Answer every provider from fixture data, or refuse them all once flagged."""
    original = pipeline.enrich_bundle

    def enrich(bundle, configuration, as_of):
        if Path(offline_flag).exists():
            raise RuntimeError("No provider may be reached while this review is offline.")
        enriched = original(bundle, configuration, as_of)
        _inject(enriched, config, as_of)
        return enriched

    def acquire(bundle, configuration, as_of, *, refresh=False):
        if Path(offline_flag).exists():
            raise RuntimeError("No provider may be reached while this review is offline.")
        acquire_synthetic_candidates(bundle, configuration, as_of, refresh=refresh)

    pipeline.enrich_bundle = enrich
    providers.enrich_bundle = enrich
    enrichment.acquire_candidates = acquire


def build_config(directory):
    """Simulated market inputs, with the collector as the holdings source of record."""
    directory = Path(directory)
    demo = directory / "demo"
    config_path = demo / "config.json"
    config = load_config(config_path if config_path.exists() else create_demo(demo))
    base = default_config(directory)
    config["source"] = base["source"]
    config["research"] = base["research"]
    config["data"].update(mode="demo", sec_enabled=False, fred_enabled=False)
    config["risk"]["bootstrap_samples"] = 50
    return config


def simulated_closes(directory):
    """The last simulated close per security, which the captures are priced at."""
    prices = pd.read_csv(Path(directory) / "demo" / "prices.csv")
    latest = prices[prices.date == prices.date.max()]
    return {
        str(row["security_id"]): round(float(row["close"]), 2) for row in latest.to_dict("records")
    }


def seed(directory, closes):
    """The collector, its two accounts and one already published collection."""
    directory = Path(directory)
    store = Store(directory)
    existing = {row["name"]: row["id"] for row in store.sources()}
    tables, source_ids, navs = {}, [], {}
    for name, slug, symbol, currency in ACCOUNTS:
        source_id = existing.get(name) or store.add_source(
            name, url=f"https://finance.yahoo.com/portfolio/{slug}"
        )
        source_ids.append(source_id)
        price = closes[symbol]
        tables[source_id] = table(symbol, currency, price)
        navs[source_id] = round(price * SHARES, 2) + CASH
    if not existing:
        batch = store.begin_batch(source_ids)
        for source_id in source_ids:
            store.ingest_table(source_id, tables[source_id], batch_id=batch)
    return store, source_ids, tables, navs


def supplemental(source_ids, navs):
    """The owner's dated evidence: identified symbols and reconciled account balances.

    Every account states its NAV, cash, currency and valuation date, so each collected
    snapshot reconciles. A completeness attestation binds one exact snapshot, so the
    fixture states the same balances for every snapshot this journey can create rather
    than letting a fresh pull silently inherit the previous one's evidence. An identical
    recapture reuses its snapshot, so the owner's own answers survive a later collection.

    Both accounts state the currency they report values in, which is the owner's own
    account fact on the Data page and not a question anything asks: the unlabelled
    account's page carries no currency column, and nothing prompts for one, because a
    holding's quote currency is settled by the exchange it is listed on. The analytical
    layer still requires each account to say what its reported values are denominated in
    before it will certify a basket, so the fixture states it the way the owner does.
    """
    securities, accounts = [], []
    for source_id, (_, _, symbol, _currency) in zip(source_ids, ACCOUNTS, strict=True):
        securities.append(
            {
                "source_id": source_id,
                "raw_symbol": symbol,
                "security_id": symbol,
                "issuer_id": symbol,
                "ticker": symbol,
                "instrument_type": "equity",
                "currency": "USD",
                "sector": sector_of(symbol),
                "eligible": True,
                "valid_from": "2021-01-01",
            }
        )
        accounts.extend(
            {
                "source_id": source_id,
                "snapshot_id": snapshot_id,
                "account_type": "retirement",
                "currency": "USD",
                "cash": f"{CASH:.2f}",
                "total_value": f"{navs[source_id]:.2f}",
                "complete": True,
                "valuation_date": VALUATION_DATE,
                "position_currency": "USD",
            }
            for snapshot_id in range(1, ATTESTED_SNAPSHOTS + 1)
        )
    return validate_supplemental(
        {"version": 1, "accounts": accounts, "securities": securities, "tax_lots": []}
    )


def serve(directory, *, delay=0.5):
    """Start the fixture application and serve it until the process is stopped.

    ``delay`` holds each claimed collection job open for that long, which is what gives a
    case time to read the progress strip and the cancel control while a month is actually
    running. Declared and not applied, the stage assertions were protected only by the
    analysis phase happening to outlast one poll tick; half a second is twenty of the
    25 ms ticks a waiting case polls on, and every collection in the suite pays it.
    """
    directory = Path(directory)
    config = build_config(directory)
    store, source_ids, tables, navs = seed(directory, simulated_closes(directory))
    accounts = [account_id(source_id) for source_id in source_ids]
    config["mandate"]["account_permissions"] = {name: list(TRADABLE) for name in accounts}
    config["mandate"]["dealing_rules"] = {
        name: {
            security_id: {
                "fractional_shares": True,
                "quantity_increment": 0.000001,
                "dealing_allowed": True,
            }
            for security_id in TRADABLE
        }
        for name in accounts
    }
    config["allocation"]["sleeve_membership"] = {}
    config["signals"]["watchlist"] = list(CANDIDATES)
    offline_flag = directory / OFFLINE_FLAG
    patch_providers(config, offline_flag)
    server = make_server(config, port=0, collector_directory=directory)
    if not server.research.store.records("supplemental"):
        server.research.store.append_record("supplemental", supplemental(source_ids, navs))
    chrome = PerAccountChrome(server.research.companion, tables, delay=delay)
    chrome.start()
    deadline = time.monotonic() + 30
    while not server.research.companion.status()["browser_open"]:
        if time.monotonic() > deadline:
            raise RuntimeError("The fake Chrome helper never polled for work.")
        time.sleep(0.02)
    print(
        json.dumps(
            {
                "port": server.server_address[1],
                "directory": str(directory),
                "offline_flag": str(offline_flag),
                "accounts": accounts,
            }
        ),
        flush=True,
    )
    try:
        server.serve_forever()
    finally:
        chrome.stop()
        server.server_close()
