"""Manual provider smoke check for the market-data adapter.

This is not a unit test: it makes real requests to the public Yahoo endpoints through
``portfolio_research.market_data`` for the symbols given on the command line and prints
the coverage report the adapter produced. Run it before claiming coverage, so the claim
describes a live answer rather than a fixture.

It never opens the holdings database, never reads a credential and writes only the
provider cache directory it is given.

    uv run python scripts/provider_smoke.py AAPL XIC.TO VOD.L
    uv run python scripts/provider_smoke.py --cache-dir /tmp/smoke --as-of 2026-09-11 AAPL
    uv run python scripts/provider_smoke.py --universe --acquire-size 250

``--universe`` measures the other half of the adapter: it pages the live candidate
screen, resolves each acquired row's identity and qualifies it, then prints how many
issuers were acquired, how many qualify and why the rest did not. It enriches nothing,
so it says what a review's candidate coverage would be, not what one would cost.

``--record`` is the other manual job this script does: it replaces the recorded provider
responses under ``tests/fixtures/providers`` with a fresh, trimmed copy of what the live
providers answer today, so the contract tests can be re-pinned when a provider ships a
new shape. It writes into the repository and it makes live requests, so it is never run
by a test or a hook — a person runs it, reads the diff and decides what the change means.
Only public tickers, the public Valet series and the public SEC endpoints are asked for,
so nothing private can reach a fixture; the recorded values are whatever those public
endpoints return.

    uv run python scripts/provider_smoke.py --record AAPL --fund XIC.TO --cik 320193
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

from portfolio_lab.config import DEFAULTS, validate_config  # noqa: E402
from portfolio_research.enrichment import acquire_candidates, enrich_market  # noqa: E402

NEW_YORK = ZoneInfo("America/New_York")
DEFAULT_CACHE = ROOT / "data" / "provider_smoke"


def smoke_config(cache_dir: Path, *, timeout: float, interval: float) -> dict:
    """A live, refreshing configuration whose cache and research path are throwaway."""
    config = deepcopy(DEFAULTS)
    cache_dir.mkdir(parents=True, exist_ok=True)
    config["research"] = {
        "path": str(cache_dir / "smoke.sqlite3"),
        "output_dir": str(cache_dir / "reports"),
    }
    config["data"].update(
        mode="live",
        refresh_network=True,
        price_provider="yahoo",
        sec_enabled=False,
        fred_enabled=False,
        lookback_years=1,
        provider_timeout_seconds=timeout,
        provider_min_interval_seconds=interval,
    )
    return validate_config(config)


def securities(symbols: list[str], funds: set[str]) -> pd.DataFrame:
    """One securities row per requested symbol, typed so fund calls are asked for."""
    return pd.DataFrame(
        [
            {
                "security_id": symbol,
                "ticker": symbol,
                "name": symbol,
                "instrument_type": "etf" if symbol in funds else "equity",
                "currency": None,
                "cik": None,
                "resolution_status": "resolved",
            }
            for symbol in symbols
        ]
    )


def run(arguments: argparse.Namespace) -> dict:
    symbols = [symbol.strip().upper() for symbol in arguments.symbols if symbol.strip()]
    funds = {symbol.strip().upper() for symbol in arguments.fund}
    as_of = arguments.as_of or datetime.now(NEW_YORK).date().isoformat()
    config = smoke_config(
        Path(arguments.cache_dir), timeout=arguments.timeout, interval=arguments.interval
    )
    bundle = {
        "as_of": as_of,
        "issues": [],
        "sources": [],
        "securities": securities(symbols, funds),
    }
    enrich_market(bundle, config, as_of)
    return bundle


def universe_run(arguments: argparse.Namespace) -> dict:
    """Acquire and qualify the live candidate screen; no security is enriched."""
    as_of = arguments.as_of or datetime.now(NEW_YORK).date().isoformat()
    config = smoke_config(
        Path(arguments.cache_dir), timeout=arguments.timeout, interval=arguments.interval
    )
    size = max(int(arguments.acquire_size), 1)
    config["data"].update(universe_acquire_size=size, universe_size=min(size, 1000))
    # Discovery is opt-in for a review; a smoke run says plainly that it is asking.
    config["mandate"]["account_candidate_policy"] = {"provider-smoke": "eligible_universe"}
    config = validate_config(config)
    bundle = {
        "as_of": as_of,
        "issues": [],
        "sources": [],
        "securities": pd.DataFrame(columns=["security_id", "ticker", "instrument_type"]),
    }
    acquire_candidates(bundle, config, as_of, refresh=True)
    return bundle


def summarise_universe(bundle: dict) -> dict:
    """How much of the live screen qualifies, and the reasons the rest did not."""
    universe = bundle.get("universe") or {}
    reasons: dict[str, int] = {}
    for row in bundle.get("exclusions") or []:
        for reason in row.get("reasons") or []:
            reasons[reason] = reasons.get(reason, 0) + 1
    securities = bundle.get("securities")
    candidates = list(securities["security_id"]) if len(securities) else []
    return {
        "as_of": bundle["as_of"],
        "scope_label": universe.get("scope_label"),
        "meta": universe.get("meta"),
        "counts": universe.get("counts"),
        "exclusions_by_reason": reasons,
        "identity_unresolved": len(universe.get("identity_unresolved") or []),
        "candidates": candidates[:10],
        "issues": bundle.get("issues"),
    }


def summarise(bundle: dict) -> dict:
    """What arrived, per frame and per security, beside the adapter's own report."""
    frames = {
        name: int(len(bundle.get(name, [])))
        for name in ("prices", "fundamentals", "fund_holdings", "fund_sectors", "events")
    }
    research = bundle.get("research") or {}
    proposals = {
        sid: {
            "eps": bool((record.get("proposals") or {}).get("eps")),
            "dcf": bool((record.get("proposals") or {}).get("dcf")),
            "reasons": [reason.get("detail") for reason in (record["proposals"]["reasons"])],
        }
        for sid, record in research.items()
    }
    return {
        "as_of": bundle["as_of"],
        "rows": frames,
        "coverage": bundle.get("coverage"),
        "proposals": proposals,
        "issues": bundle.get("issues"),
        "sources": [source["source_id"] for source in bundle.get("sources", [])],
    }


# How much of each live response a recorded fixture keeps. A fixture is read by a
# contract test, not by a review: it needs the provider's shape, not its whole answer.
RECORD_SESSIONS = 5
RECORD_STORIES = 3
RECORD_FACT_ENTRIES = 6
RECORD_TICKERS = ("AAPL", "MSFT", "BRK-B", "F")
RECORD_SERIES = ("FXEURCAD", "FXGBPCAD", "FXUSDCAD")
RECORD_WINDOW_DAYS = 12
PROFILE_INFO_KEYS = (
    "longName",
    "shortName",
    "sector",
    "industry",
    "country",
    "financialCurrency",
    "currency",
    "exchange",
    "quoteType",
    "marketCap",
    "sharesOutstanding",
)
STORY_KEYS = (
    "contentType",
    "title",
    "pubDate",
    "displayTime",
    "summary",
    "provider",
    "canonicalUrl",
    "clickThroughUrl",
)


# The chart metadata keys worth recording, named explicitly. Never ``dict(metadata)``:
# ``tradingPeriods`` is resolved lazily, so materializing the mapping costs another
# intraday request and answers with a DataFrame that no fixture can hold.
HISTORY_METADATA_KEYS = (
    "currency",
    "symbol",
    "exchangeName",
    "fullExchangeName",
    "instrumentType",
    "firstTradeDate",
    "regularMarketTime",
    "gmtoffset",
    "timezone",
    "exchangeTimezoneName",
    "regularMarketPrice",
    "chartPreviousClose",
    "priceHint",
    "dataGranularity",
    "range",
)


def _record_metadata(fixtures, metadata) -> dict:
    """The chart metadata this project reads, encoded as JSON.

    yfinance parses ``firstTradeDate`` and ``regularMarketTime`` into ``pandas.Timestamp``
    before the property returns, and hands the whole thing over as a Mapping rather than a
    dict, so every value goes through the fixture encoder on its way to the file.
    """
    source = metadata if isinstance(metadata, Mapping) else {}
    return {key: fixtures.scalar(source[key]) for key in HISTORY_METADATA_KEYS if key in source}


def _record_filings(fixtures, filings) -> list:
    """Filing rows as JSON: the filing date is a ``datetime.date`` the encoder converts."""
    rows = []
    for filing in filings or []:
        if not isinstance(filing, Mapping):
            continue
        row = {}
        for key, value in filing.items():
            if key == "exhibits" and isinstance(value, Mapping):
                row[key] = {str(name): fixtures.scalar(link) for name, link in value.items()}
            elif key == "exhibits":
                row[key] = [fixtures.scalar(item) for item in value or []]
            else:
                row[key] = fixtures.scalar(value)
        rows.append(row)
    return rows


def _kept(mapping: dict | None, keys) -> dict:
    """The named keys of a provider mapping, in the order this project reads them."""
    source = mapping if isinstance(mapping, dict) else {}
    return {key: source.get(key) for key in keys if key in source}


def _record_calendar(fixtures, calendar, keys=None) -> dict:
    """The provider calendar as JSON: its dates as ISO text, its lists still lists."""
    result = {}
    for key, value in (calendar if isinstance(calendar, dict) else {}).items():
        if keys is not None and key not in keys:
            continue
        if isinstance(value, list):
            result[key] = [fixtures.scalar(item) for item in value]
        else:
            result[key] = fixtures.scalar(value)
    return result


def _record_prices(fixtures, ticker, as_of: str) -> dict:
    start = (pd.Timestamp(as_of) - pd.Timedelta(days=RECORD_WINDOW_DAYS)).date().isoformat()
    end = (pd.Timestamp(as_of) + pd.Timedelta(days=1)).date().isoformat()
    history = ticker.history(start=start, end=end, auto_adjust=False, actions=True)
    return {
        "symbol": ticker.ticker,
        "history": fixtures.encode_history(history.tail(RECORD_SESSIONS)),
        "history_metadata": _record_metadata(fixtures, ticker.history_metadata),
    }


def _record_statements(fixtures, ticker) -> dict:
    payload = {
        "symbol": ticker.ticker,
        "info": _kept(ticker.info, ("financialCurrency",)),
        "sec_filings": _record_filings(fixtures, ticker.sec_filings),
    }
    frames = {
        "quarterly_income_stmt": ticker.quarterly_income_stmt,
        "quarterly_balance_sheet": ticker.quarterly_balance_sheet,
        "quarterly_cashflow": ticker.quarterly_cashflow,
        "income_stmt": ticker.income_stmt,
        "balance_sheet": ticker.balance_sheet,
        "cashflow": ticker.cashflow,
    }
    for name, frame in frames.items():
        payload[name] = fixtures.encode_statement(frame)
    return payload


def _record_estimates(fixtures, ticker) -> dict:
    return {
        "symbol": ticker.ticker,
        "earnings_estimate": fixtures.encode_frame(ticker.earnings_estimate),
        "revenue_estimate": fixtures.encode_frame(ticker.revenue_estimate),
        "analyst_price_targets": dict(ticker.analyst_price_targets or {}),
        "recommendations": fixtures.encode_frame(ticker.recommendations),
        "calendar": _record_calendar(fixtures, ticker.calendar),
    }


def _record_funds(fixtures, ticker) -> dict:
    funds = ticker.funds_data
    return {
        "symbol": ticker.ticker,
        "funds_data": {
            "top_holdings": fixtures.encode_frame(funds.top_holdings),
            "sector_weightings": dict(funds.sector_weightings or {}),
            "asset_classes": dict(funds.asset_classes or {}),
            "fund_overview": _kept(funds.fund_overview, ("categoryName", "family", "legalType")),
        },
    }


def _record_news(fixtures, ticker) -> dict:
    stories = []
    for item in (ticker.news or [])[:RECORD_STORIES]:
        content = item.get("content") if isinstance(item, dict) else None
        if isinstance(content, dict):
            stories.append({"id": item.get("id"), "content": _kept(content, STORY_KEYS)})
    return {
        "symbol": ticker.ticker,
        "news": stories,
        "sec_filings": _record_filings(fixtures, ticker.sec_filings),
        "calendar": _record_calendar(
            fixtures,
            ticker.calendar,
            keys=("Earnings Date", "Ex-Dividend Date", "Dividend Date"),
        ),
    }


def _record_profile(fixtures, ticker) -> dict:
    return {
        "symbol": ticker.ticker,
        "info": _kept(ticker.info, PROFILE_INFO_KEYS),
        "history_metadata": _record_metadata(fixtures, ticker.history_metadata),
    }


def _record_valet(as_of: str) -> dict:
    from portfolio_lab import providers
    from portfolio_research.fx_providers import USER_AGENT, _valet_url

    start = (pd.Timestamp(as_of) - pd.Timedelta(days=RECORD_WINDOW_DAYS)).date().isoformat()
    url = _valet_url(list(RECORD_SERIES), start, as_of)
    payload, _ = providers._fetch_json(url, USER_AGENT)
    return payload


def _record_sec_tickers(agent: str) -> dict:
    from portfolio_lab import providers
    from portfolio_research.issuers import SEC_TICKERS_URL, ticker_key

    arguments = (SEC_TICKERS_URL, agent) if agent.strip() else (SEC_TICKERS_URL,)
    payload, _ = providers._fetch_json(*arguments)
    wanted = {ticker_key(symbol) for symbol in RECORD_TICKERS}
    kept = [
        record
        for record in payload.values()
        if isinstance(record, dict) and ticker_key(record.get("ticker")) in wanted
    ]
    kept.sort(key=lambda record: str(record.get("ticker")))
    return {str(index): record for index, record in enumerate(kept)}


def _record_companyfacts(cik: str, agent: str) -> dict:
    """The tags this project's extractor reads, with their most recent USD entries."""
    from portfolio_lab.providers import FLOW_TAGS, STOCK_TAGS, _fetch_json

    number = f"{int(str(cik).removeprefix('CIK').removeprefix('cik:')):010d}"
    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{number}.json"
    payload, _ = _fetch_json(url, agent)
    wanted = {tag for group in (*FLOW_TAGS.values(), *STOCK_TAGS.values()) for tag in group}
    gaap = ((payload.get("facts") or {}).get("us-gaap")) or {}
    facts = {}
    for tag in sorted(wanted & set(gaap)):
        entries = ((gaap[tag].get("units") or {}).get("USD")) or []
        entries = sorted(entries, key=lambda row: (str(row.get("end")), str(row.get("filed"))))
        facts[tag] = {
            "label": gaap[tag].get("label"),
            "description": gaap[tag].get("description"),
            "units": {"USD": entries[-RECORD_FACT_ENTRIES:]},
        }
    return {
        "cik": payload.get("cik"),
        "entityName": payload.get("entityName"),
        "facts": {"us-gaap": facts},
    }


def record_run(arguments: argparse.Namespace) -> dict:
    """Refresh every checked-in provider fixture; a failed capability names itself.

    Each fixture is written on its own, so one provider that will not answer today
    leaves the other recordings in place instead of aborting the refresh.
    """
    import yfinance as yf

    from portfolio_lab.providers import _error_label
    from tests.support import provider_fixtures as fixtures

    as_of = arguments.as_of or datetime.now(NEW_YORK).date().isoformat()
    written, skipped = {}, {}
    for name, job in _record_jobs(fixtures, yf, arguments, as_of).items():
        try:
            payload = job()
        except Exception as error:  # One provider's refusal is not the whole refresh.
            # Never str(error): a yfinance HTTP failure carries the request URL, and that
            # URL carries the minted Yahoo session crumb. The project's own label names
            # the status or the exception class and quotes no request.
            skipped[name] = _error_label(error)
            continue
        # Encoding is this recorder's own job. A payload it cannot write is a defect here,
        # not a provider that would not answer today, and calling it "skipped" would leave
        # the stale fixture in place while the refresh path stays quietly inoperative.
        written_path = fixtures.write(name, payload)
        written[name] = str(
            written_path.relative_to(ROOT) if written_path.is_relative_to(ROOT) else written_path
        )
    return {"as_of": as_of, "written": written, "skipped": skipped}


def _record_jobs(fixtures, yf, arguments: argparse.Namespace, as_of: str) -> dict:
    """One callable per fixture, each fetching exactly what that recording needs."""
    agent = arguments.sec_contact
    equity = yf.Ticker(arguments.symbols[0]) if arguments.symbols else None
    fund = yf.Ticker(arguments.fund[0]) if arguments.fund else None
    return {
        fixtures.PRICES: lambda: _record_prices(fixtures, equity, as_of),
        fixtures.STATEMENTS: lambda: _record_statements(fixtures, equity),
        fixtures.ESTIMATES: lambda: _record_estimates(fixtures, equity),
        fixtures.FUNDS: lambda: _record_funds(fixtures, fund),
        fixtures.NEWS: lambda: _record_news(fixtures, equity),
        fixtures.PROFILE: lambda: _record_profile(fixtures, equity),
        fixtures.VALET: lambda: _record_valet(as_of),
        fixtures.SEC_TICKERS: lambda: _record_sec_tickers(agent),
        fixtures.SEC_COMPANYFACTS: lambda: _record_companyfacts(arguments.cik, agent),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("symbols", nargs="*", help="Exact provider symbols, e.g. AAPL VOD.L")
    parser.add_argument(
        "--universe",
        action="store_true",
        help="Acquire and qualify the live candidate screen instead of enriching symbols.",
    )
    parser.add_argument(
        "--acquire-size",
        type=int,
        default=250,
        help="How many screened symbols --universe acquires (identity is asked per symbol).",
    )
    parser.add_argument(
        "--fund",
        action="append",
        default=[],
        help="Symbol to request as an ETF or mutual fund (repeatable).",
    )
    parser.add_argument(
        "--record",
        action="store_true",
        help=(
            "Manual fixture refresh: replace the recorded provider responses under "
            "tests/fixtures/providers with today's live answers, trimmed to the shape the "
            "contract tests read. Makes live requests and writes into the repository, so "
            "run it by hand and review the diff; never from a test, a hook or a review. "
            "Asks only public endpoints, so no private data can reach a fixture. "
            "Takes the equity symbol positionally, the fund from --fund and the SEC "
            "issuer from --cik."
        ),
    )
    parser.add_argument(
        "--cik",
        default="320193",
        help="CIK whose companyfacts subset --record writes (public SEC identifier).",
    )
    parser.add_argument(
        "--sec-contact",
        default="PortfolioReview/0.2 personal-research",
        help="User-Agent string --record sends to SEC endpoints, as SEC asks callers to.",
    )
    parser.add_argument("--as-of", default=None, help="Observation date (YYYY-MM-DD).")
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE))
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--interval", type=float, default=0.5)
    arguments = parser.parse_args(argv)
    if arguments.record:
        if not arguments.symbols or not arguments.fund:
            parser.error("--record needs one equity symbol and one --fund symbol.")
        report = record_run(arguments)
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
        if report["skipped"]:
            print(
                "Fixtures left unchanged: " + ", ".join(sorted(report["skipped"])), file=sys.stderr
            )
            return 1
        return 0
    if not arguments.universe and not arguments.symbols:
        parser.error("Give at least one provider symbol, or --universe.")
    try:
        bundle = universe_run(arguments) if arguments.universe else run(arguments)
    except (ValueError, KeyError, OSError) as error:  # A smoke run reports, never traces.
        print(f"Provider smoke run failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 2
    if arguments.universe:
        print(json.dumps(summarise_universe(bundle), indent=2, sort_keys=True, default=str))
        return 0
    print(json.dumps(summarise(bundle), indent=2, sort_keys=True, default=str))
    missing = [
        name
        for name, record in ((bundle.get("coverage") or {}).get("capabilities") or {}).items()
        if record.get("requested") and not record.get("available")
    ]
    if missing:
        print("Capabilities with no live answer: " + ", ".join(sorted(missing)), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
