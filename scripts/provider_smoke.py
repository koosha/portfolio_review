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
"""

from __future__ import annotations

import argparse
import json
import sys
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
    parser.add_argument("--as-of", default=None, help="Observation date (YYYY-MM-DD).")
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE))
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--interval", type=float, default=0.5)
    arguments = parser.parse_args(argv)
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
