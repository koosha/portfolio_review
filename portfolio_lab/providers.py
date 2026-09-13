"""Optional market-data adapters and conservative, publication-aware SEC extraction.

CSV is the default/offline interface. Live failures leave missing data and issues;
they never create replacement market data. SEC companyfacts cover standard USD
US-GAAP entity-wide concepts only. Custom/IFRS tags need a reviewed CSV adapter.

Official adapter contracts checked 2026-09-13:
https://www.sec.gov/search-filings/edgar-application-programming-interfaces
https://fred.stlouisfed.org/docs/api/fred/series_observations.html
https://github.com/ranaroussi/yfinance
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

import pandas as pd

FRAME_COLUMNS = {
    "prices": "date security_id close adjusted_close volume currency available_at received_at source_id".split(),
    "fundamentals": (
        "security_id period_end available_at received_at revenue gross_profit operating_income "
        "net_income income_common earnings_definition operating_cash_flow capex assets assets_begin debt cash currency source_id"
    ).split(),
    "macro": "series_id date value vintage_date received_at units source_id".split(),
    "fund_holdings": "fund_id issuer_id weight holdings_date available_at source_id".split(),
    "forecasts": (
        "security_id scenario horizon_months return_value probability basis source forecast_date "
        "calibration_id"
    ).split(),
    "events": "security_id event_date kind title url".split(),
}
REQUIRED_COLUMNS = {
    "prices": ["date", "security_id", "close", "adjusted_close"],
    "fundamentals": ["security_id", "period_end", "available_at"],
    "macro": ["series_id", "date", "value", "vintage_date"],
    "fund_holdings": ["fund_id", "issuer_id", "weight", "holdings_date", "available_at"],
    "forecasts": [
        "security_id",
        "scenario",
        "horizon_months",
        "return_value",
        "basis",
        "forecast_date",
    ],
    "events": ["security_id", "event_date", "kind"],
}
FLOW_TAGS = {
    "revenue": [
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
    ],
    "gross_profit": ["GrossProfit"],
    "operating_income": ["OperatingIncomeLoss"],
    "net_income": ["NetIncomeLoss"],
    "income_common": ["NetIncomeLossAvailableToCommonStockholdersBasic"],
    "operating_cash_flow": ["NetCashProvidedByUsedInOperatingActivities"],
    "capex": ["PaymentsToAcquirePropertyPlantAndEquipment"],
}
STOCK_TAGS = {
    "assets": ["Assets"],
    "cash": ["CashAndCashEquivalentsAtCarryingValue"],
    # Total debt must be explicitly reported; liabilities are not a debt proxy.
    "debt": [
        "LongTermDebtAndFinanceLeaseObligationsIncludingCurrentMaturities",
        "DebtAndCapitalLeaseObligations",
    ],
    "liabilities": ["Liabilities"],
    "equity": [
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
        "StockholdersEquity",
    ],
}
ALLOWED_FORMS = {"10-K", "10-K/A", "10-Q", "10-Q/A", "20-F", "20-F/A", "40-F", "40-F/A"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _day(value: Any) -> pd.Timestamp:
    return pd.Timestamp(value).tz_localize(None).normalize()


def _issue(bundle: dict, code: str, message: str, severity: str = "warning") -> None:
    bundle.setdefault("issues", []).append({"severity": severity, "code": code, "message": message})


def _redact_url(url: str) -> str:
    p = urlsplit(url)
    query = [
        (
            k,
            "REDACTED"
            if any(x in k.lower() for x in ("key", "token", "secret", "password"))
            else v,
        )
        for k, v in parse_qsl(p.query, keep_blank_values=True)
    ]
    netloc = p.netloc.rsplit("@", 1)[-1]
    return urlunsplit((p.scheme, netloc, p.path, urlencode(query), ""))


def _archive(
    config: dict, provider: str, key: str, payload: Any, url: str, received_at: str, **metadata: Any
) -> dict:
    raw = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, default=str, allow_nan=False
    ).encode()
    digest = hashlib.sha256(raw).hexdigest()
    root = (
        Path(config.get("research", {}).get("path", "research.sqlite"))
        .expanduser()
        .resolve()
        .parent
        / "cache"
        / provider
    )
    root.mkdir(parents=True, exist_ok=True)
    safe_key = re.sub(r"[^A-Za-z0-9_.-]", "_", str(key))[:100]
    path = root / f"{safe_key}_{digest}.json"
    if not path.exists():
        path.write_bytes(raw)
    source = {
        "source_id": f"{provider}:{digest}",
        "provider": provider,
        "key": str(key),
        "url": _redact_url(url),
        "sha256": digest,
        "received_at": received_at,
        "raw_path": str(path),
        **metadata,
    }
    # Sidecars preserve actual observation/receipt times across offline runs.
    # Re-fetching an identical raw payload creates a separate receipt record.
    receipt = received_at.replace(":", "").replace("/", "_")
    (root / f"{safe_key}_{digest}_{receipt}.source.json").write_text(
        json.dumps(source, ensure_ascii=False, sort_keys=True, default=str), encoding="utf-8"
    )
    return source


def _cached(
    config: dict, provider: str, key: str, as_of: str | None = None
) -> tuple[dict | list, str, dict]:
    root = (
        Path(config.get("research", {}).get("path", "research.sqlite"))
        .expanduser()
        .resolve()
        .parent
        / "cache"
        / provider
    )
    candidates = []
    if root.exists():
        for path in root.glob("*.source.json"):
            if path.is_symlink():
                continue
            try:
                source = json.loads(path.read_text())
                if source.get("key") != str(key):
                    continue
                if (
                    as_of
                    and source.get("vintage_date")
                    and _day(source["vintage_date"]) > _day(as_of)
                ):
                    continue
                candidates.append(source)
            except (OSError, ValueError):
                continue
    if not candidates:
        raise FileNotFoundError("No compatible cached provider response")
    latest = max(candidates, key=lambda s: (s.get("vintage_date", ""), s["received_at"]))
    # Sidecars are data, not permission to read arbitrary files. Resolve a relocated
    # cache payload by its basename inside this provider cache and reject symlinks.
    recorded = Path(latest["raw_path"])
    payload_path = root / recorded.name
    if payload_path.is_symlink() or payload_path.resolve().parent != root.resolve():
        raise ValueError("Cached payload path escapes its provider cache")
    raw = payload_path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != latest["sha256"]:
        raise ValueError("Cached payload failed its content hash")
    return json.loads(raw), latest["received_at"], latest


def _fetch_json(
    url: str, user_agent: str = "PortfolioReview/0.2 personal-research"
) -> tuple[dict, str]:
    req = Request(url, headers={"User-Agent": user_agent, "Accept": "application/json"})
    for attempt in range(3):
        try:
            with urlopen(req, timeout=25) as response:
                raw = response.read(50_000_001)
            if len(raw) > 50_000_000:
                raise ValueError("Provider response exceeds 50 MB limit")
            return json.loads(raw), _now()
        except (HTTPError, URLError, TimeoutError) as exc:
            retryable = not isinstance(exc, HTTPError) or exc.code in {429, 500, 502, 503, 504}
            if not retryable or attempt == 2:
                raise
            # Bounded exponential backoff; never include secret-bearing URLs in diagnostics.
            time.sleep(2**attempt)
    raise RuntimeError("Unreachable provider retry state")


def _error_label(exc: Exception) -> str:
    # Exception bodies and URLs may contain API keys. Never log str(exc).
    return f"HTTP {exc.code}" if isinstance(exc, HTTPError) else type(exc).__name__


def _facts(companyfacts: dict, tag: str, as_of: str) -> list[dict]:
    data = (
        companyfacts.get("facts", {})
        .get("us-gaap", {})
        .get(tag, {})
        .get("units", {})
        .get("USD", [])
    )
    cutoff = _day(as_of)
    rows = []
    for item in data:
        try:
            filed = _day(item["filed"])
            end = _day(item["end"])
            value = float(item["val"])
            # Companyfacts has only a filing date, not a dissemination timestamp.
            # Next-calendar-day availability is deliberately conservative.
            available = filed + pd.Timedelta(days=1)
            if (
                available > cutoff
                or end > cutoff
                or item.get("form") not in ALLOWED_FORMS
                or not math.isfinite(value)
            ):
                continue
            start = _day(item["start"]) if item.get("start") else None
            rows.append(
                {
                    **item,
                    "val": value,
                    "_end": end,
                    "_start": start,
                    "_filed": filed,
                    "_available": available,
                    "_tag": tag,
                }
            )
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
    return rows


def _choose(
    rows: list[dict],
    *,
    end: pd.Timestamp,
    start: pd.Timestamp | None = None,
    accn: str | None = None,
) -> dict | None:
    matching = [
        r
        for r in rows
        if r["_end"] == end and r["_start"] == start and (accn is None or r.get("accn") == accn)
    ]
    if not matching:
        return None
    ordered = sorted(matching, key=lambda r: (r["_filed"], str(r.get("accn", ""))), reverse=True)
    # Conflicting values within one exact accession/period are not resolved by arbitrary row order.
    best = ordered[0]
    conflicts = [
        r for r in ordered if r.get("accn") == best.get("accn") and r["_filed"] == best["_filed"]
    ]
    return best if len({r["val"] for r in conflicts}) == 1 else None


def _component(row: dict) -> dict:
    return {
        "tag": row["_tag"],
        "start": row.get("start"),
        "end": row["end"],
        "filed": row["filed"],
        "accession": row.get("accn"),
        "value": row["val"],
        "unit": "USD",
    }


def extract_sec_fundamentals(
    companyfacts: dict, security_id: str, as_of: str, received_at: str, source_id: str
) -> dict | None:
    """Build one USD statement observation without summing cumulative quarters.

    The latest eligible filing/period anchors every current-period component.
    Annual observations use an exact common 330–400-day fiscal duration.
    Interim TTM = prior annual + current YTD - prior comparable YTD. Both YTD
    components must occur in the current accession with exactly aligned dates.
    Earlier annual components share one annual accession where possible. Missing
    tags/periods remain None and source provenance records each arithmetic input.
    This reconstructs facts public by date; it does not claim live receipt then.
    """
    if not companyfacts.get("facts", {}).get("us-gaap"):
        return None
    tags = {t for group in (*FLOW_TAGS.values(), *STOCK_TAGS.values()) for t in group}
    facts = {tag: _facts(companyfacts, tag, as_of) for tag in tags}
    anchors = [
        r
        for tag in ("Assets", *FLOW_TAGS["operating_cash_flow"], *FLOW_TAGS["revenue"])
        for r in facts.get(tag, [])
    ]
    if not anchors:
        return None
    anchor = max(anchors, key=lambda r: (r["_end"], r["_filed"], str(r.get("accn", ""))))
    end, accn = anchor["_end"], anchor.get("accn")
    if not accn:
        return None
    # Prefer cumulative cash-flow periods because income statements also contain
    # a stand-alone quarter beside a six-/nine-month cumulative period.
    period_rows = [
        r
        for tag in (*FLOW_TAGS["operating_cash_flow"], *FLOW_TAGS["revenue"])
        for r in facts[tag]
        if r["_end"] == end
        and r.get("accn") == accn
        and r["_start"] is not None
        and 50 <= (end - r["_start"]).days <= 400
    ]
    start = min((r["_start"] for r in period_rows), default=None)
    annual = start is not None and 330 <= (end - start).days <= 400
    annual_anchor = None
    if start is not None and not annual:
        prior_end = start - pd.Timedelta(days=1)
        candidates = [
            r
            for tag in (*FLOW_TAGS["operating_cash_flow"], *FLOW_TAGS["revenue"])
            for r in facts[tag]
            if r["_end"] == prior_end
            and r["_start"] is not None
            and 330 <= (r["_end"] - r["_start"]).days <= 400
        ]
        if candidates:
            annual_anchor = max(candidates, key=lambda r: (r["_filed"], str(r.get("accn", ""))))
    out = {
        "security_id": security_id,
        "period_end": end.date().isoformat(),
        "available_at": anchor["_available"].date().isoformat(),
        "received_at": received_at,
        "source_id": source_id,
        "currency": "USD",
        "provenance": {},
        "data_warnings": [],
        "extraction_basis": "original_filing_date_plus_one_day; not historical live-receipt replay",
    }
    for field, alternatives in FLOW_TAGS.items():
        out[field] = None
        for tag in alternatives:
            current = (
                _choose(facts[tag], end=end, start=start, accn=accn) if start is not None else None
            )
            if current is None:
                continue
            if annual:
                result, parts = current["val"], [current]
            elif annual_anchor is not None:
                a_start, a_end = annual_anchor["_start"], annual_anchor["_end"]
                # Exact calendar-year boundaries support leap years. Noncalendar
                # 52/53-week comparatives require a reviewed normalization adapter.
                comparable_end = end - pd.DateOffset(years=1)
                previous = _choose(facts[tag], end=comparable_end, start=a_start, accn=accn)
                prior_annual = _choose(
                    facts[tag], end=a_end, start=a_start, accn=annual_anchor.get("accn")
                )
                if previous is None or prior_annual is None:
                    continue
                result = prior_annual["val"] + current["val"] - previous["val"]
                parts = [prior_annual, current, previous]
            else:
                continue
            if field == "capex" and result < 0:
                out["data_warnings"].append(
                    "Negative gross capex is not converted into positive spending; manual review required."
                )
                continue
            out[field] = result
            out["provenance"][field] = {
                "method": "annual" if annual else "annual_plus_current_ytd_minus_prior_ytd",
                "components": [_component(x) for x in parts],
            }
            out["available_at"] = max(
                out["available_at"], *(x["_available"].date().isoformat() for x in parts)
            )
            break
    net_provenance = out["provenance"].get("income_common", {}).get("components", [])
    qualified = bool(net_provenance) and all(
        part["tag"] == "NetIncomeLossAvailableToCommonStockholdersBasic" for part in net_provenance
    )
    out["earnings_definition"] = "common_shareholders" if qualified else "consolidated_unqualified"
    out["income_common"] = out.get("income_common") if qualified else None
    if not qualified:
        out["data_warnings"].append(
            "Common-share earnings are unavailable; consolidated net income cannot unlock earnings yield."
        )
    for field, alternatives in STOCK_TAGS.items():
        out[field] = None
        for tag in alternatives:
            record = _choose(facts[tag], end=end, accn=accn)
            if record is not None:
                out[field] = record["val"]
                out["provenance"][field] = {"method": "instant", "components": [_component(record)]}
                break
    out["assets_begin"] = None
    if start is not None:
        target = (
            start - pd.Timedelta(days=1)
            if annual
            else (end - pd.DateOffset(years=1) if annual_anchor else None)
        )
        if target is not None:
            record = _choose(facts["Assets"], end=target, accn=accn) or _choose(
                facts["Assets"], end=target
            )
            if record is not None:
                out["assets_begin"] = record["val"]
                out["provenance"]["assets_begin"] = {
                    "method": "instant_ttm_start",
                    "components": [_component(record)],
                }
                out["available_at"] = max(
                    out["available_at"], record["_available"].date().isoformat()
                )
    if out["assets"] is not None and out["assets"] < 0:
        out["data_warnings"].append("Negative total assets; invalid observation.")
        out["assets"] = None
    if all(out[k] is not None for k in ("assets", "liabilities", "equity")):
        residual = out["assets"] - out["liabilities"] - out["equity"]
        if abs(residual) > max(abs(out["assets"]) * 0.01, 1):
            out["data_warnings"].append(
                "Assets do not reconcile to liabilities plus reported equity within 1%; review NCI, mezzanine equity, and tags."
            )
    missing = [k for k in FLOW_TAGS if out[k] is None]
    if missing:
        out["data_warnings"].append("Unresolved comparable USD TTM fields: " + ", ".join(missing))
    return out


def load_csv(
    path: str | None, frame_name: str, bundle: dict | None = None, config: dict | None = None
) -> pd.DataFrame:
    """Load canonical CSVs; absent files are empty, malformed inputs are issues."""
    columns = FRAME_COLUMNS.get(frame_name, [])
    if not path:
        return pd.DataFrame(columns=columns)
    try:
        p = Path(path).expanduser()
        raw = p.read_text(encoding="utf-8-sig")
        from io import StringIO

        frame = pd.read_csv(
            StringIO(raw),
            dtype={
                x: "string"
                for x in (
                    "security_id",
                    "account_id",
                    "issuer_id",
                    "fund_id",
                    "series_id",
                    "cik",
                    "ticker",
                )
            },
        )
        required = REQUIRED_COLUMNS.get(
            frame_name, ["security_id", "ticker", "issuer_id", "instrument_type", "currency"]
        )
        missing = set(required) - set(frame.columns)
        if missing:
            raise ValueError("Missing required columns: " + ", ".join(sorted(missing)))
        for name in columns:
            if name not in frame:
                frame[name] = None
        received_at = _now()
        if bundle is not None:
            if config is not None:
                source = _archive(
                    config,
                    "csv",
                    frame_name,
                    {"content": raw},
                    str(p.resolve()),
                    received_at,
                    point_in_time="user_supplied_timestamps; vendor history not independently verified",
                )
            else:
                digest = hashlib.sha256(raw.encode()).hexdigest()
                source = {
                    "source_id": "csv:" + digest,
                    "provider": "csv",
                    "sha256": digest,
                    "received_at": received_at,
                    "url": str(p.resolve()),
                }
            bundle.setdefault("sources", []).append(source)
            if "source_id" in frame:
                frame["source_id"] = frame["source_id"].fillna(source["source_id"])
        if "received_at" in frame:
            frame["received_at"] = frame["received_at"].fillna(received_at)
        if frame_name == "prices" and frame["available_at"].isna().any():
            fallback = (
                pd.to_datetime(frame["date"], errors="coerce") + pd.Timedelta(days=1)
            ).dt.strftime("%Y-%m-%d")
            frame["available_at"] = frame["available_at"].fillna(fallback)
            if bundle is not None:
                _issue(
                    bundle,
                    "PRICE_AVAILABILITY_ASSUMED",
                    "CSV prices lack some availability times; those rows become available the next calendar day.",
                )
        return frame
    except Exception as exc:
        if bundle is None:
            raise
        # Schema failures have safe local diagnostic text; other exceptions may carry secrets.
        detail = (
            str(exc)
            if isinstance(exc, ValueError) and str(exc).startswith("Missing required columns:")
            else _error_label(exc)
        )
        _issue(
            bundle, "CSV_LOAD_FAILED", f"{frame_name} CSV could not be loaded ({detail}).", "error"
        )
        return pd.DataFrame(columns=columns)


def _merge(bundle: dict, name: str, frame: pd.DataFrame) -> None:
    previous = bundle.get(name, pd.DataFrame(columns=FRAME_COLUMNS.get(name, [])))
    if previous.empty:
        bundle[name] = frame.copy()
    elif not frame.empty:
        bundle[name] = pd.concat([previous, frame], ignore_index=True)


def _filter_frames(bundle: dict, as_of: str, require_received: bool = False) -> None:
    # Match metrics/allocation exactly, including daylight-saving time.
    from portfolio_research.calendar import information_cutoff

    cutoff = information_cutoff(as_of)
    specs = {
        "prices": (
            ["date", "available_at"],
            ["security_id", "date"],
            ["available_at", "received_at"],
        ),
        "fundamentals": (
            ["period_end", "available_at"],
            ["security_id", "period_end"],
            ["available_at", "received_at"],
        ),
        "macro": (["date", "vintage_date"], ["series_id", "date"], ["vintage_date", "received_at"]),
        "fund_holdings": (
            ["holdings_date", "available_at"],
            ["fund_id", "issuer_id", "holdings_date"],
            ["available_at"],
        ),
        "forecasts": (
            ["forecast_date"],
            ["security_id", "scenario", "horizon_months"],
            ["forecast_date"],
        ),
    }
    for name, (date_cols, keys, order) in specs.items():
        frame = bundle.get(name, pd.DataFrame()).copy()
        if frame.empty:
            continue
        valid = pd.Series(True, index=frame.index)
        for column in date_cols:
            dates = pd.to_datetime(frame[column], utc=True, errors="coerce", format="mixed")
            valid &= dates.notna() & (dates <= cutoff)
        if require_received:
            if "received_at" not in frame:
                _issue(
                    bundle,
                    "RECEIPT_TIMESTAMP_REQUIRED",
                    f"Strict historical receipt mode excludes {name}: received_at is missing. Supply actual receipt timestamps or use publication-only reconstruction mode.",
                    "error",
                )
                valid &= False
            else:
                receipt_dates = pd.to_datetime(
                    frame["received_at"], utc=True, errors="coerce", format="mixed"
                )
                valid &= receipt_dates.notna() & (receipt_dates <= cutoff)
        removed = int((~valid).sum())
        if removed:
            _issue(
                bundle,
                "ASOF_ROWS_EXCLUDED",
                f"Excluded {removed} {name} rows with missing/invalid dates or information after {as_of}.",
            )
        frame = frame.loc[valid].copy()
        # Parse dates before ordering: mixed offsets cannot be sorted lexically.
        sort_columns = []
        for column in order:
            if column in frame:
                sort_col = "__sort_" + column
                frame[sort_col] = pd.to_datetime(
                    frame[column], utc=True, errors="coerce", format="mixed"
                )
                sort_columns.append(sort_col)
        if sort_columns:
            frame = frame.sort_values(sort_columns, kind="stable")
        frame = frame.drop_duplicates(keys, keep="last").drop(columns=sort_columns)
        numeric = {
            "prices": ["close", "adjusted_close", "volume"],
            "fundamentals": list(FLOW_TAGS)
            + ["income_common", "assets", "assets_begin", "debt", "cash"],
            "macro": ["value"],
            "fund_holdings": ["weight"],
            "forecasts": ["horizon_months", "return_value", "probability"],
        }[name]
        for column in numeric:
            if column not in frame:
                frame[column] = None
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        if name == "prices":
            bad = (
                (frame["close"] <= 0)
                | (frame["adjusted_close"] <= 0)
                | frame[["close", "adjusted_close"]].isna().any(axis=1)
            )
            if bad.any():
                _issue(
                    bundle,
                    "INVALID_PRICES",
                    f"Excluded {int(bad.sum())} nonpositive or nonnumeric prices.",
                )
                frame = frame.loc[~bad]
        bundle[name] = frame.reset_index(drop=True)


def _enrich_yahoo(bundle: dict, config: dict, as_of: str) -> None:
    if not config["data"].get("refresh_network", False):
        for _, sec in bundle["securities"].iterrows():
            if sec.get("instrument_type") == "plan_fund":
                continue
            try:
                payload, received_at, source = _cached(config, "yahoo", str(sec["security_id"]))
                history = pd.DataFrame(payload)
                date_col = "Date" if "Date" in history else "Datetime"
                dates = (
                    pd.to_datetime(history[date_col], utc=True).dt.tz_localize(None).dt.normalize()
                )
                frame = pd.DataFrame(
                    {
                        "security_id": sec["security_id"],
                        "date": dates.dt.strftime("%Y-%m-%d"),
                        "close": history["Close"],
                        "adjusted_close": history["Adj Close"],
                        "volume": history.get("Volume"),
                        "currency": sec.get("currency"),
                        "available_at": (dates + pd.Timedelta(days=1)).dt.strftime("%Y-%m-%d"),
                        "received_at": received_at,
                        "source_id": source["source_id"],
                    }
                )
                bundle["sources"].append(source)
                _merge(bundle, "prices", frame)
                try:
                    info, _, meta_source = _cached(
                        config, "yahoo_metadata", str(sec["security_id"])
                    )
                    if meta_source.get("observation_date") == as_of and info.get(
                        "currency"
                    ) == sec.get("currency"):
                        mask = bundle["securities"]["security_id"] == sec["security_id"]
                        bundle["securities"].loc[mask, "market_cap"] = info.get("marketCap")
                        bundle["securities"].loc[mask, "market_cap_as_of"] = meta_source[
                            "observation_date"
                        ]
                        bundle["securities"].loc[mask, "market_cap_available_at"] = meta_source[
                            "received_at"
                        ]
                        bundle["securities"].loc[mask, "market_cap_received_at"] = meta_source[
                            "received_at"
                        ]
                        bundle["securities"].loc[mask, "market_cap_source_id"] = meta_source[
                            "source_id"
                        ]
                        bundle["sources"].append(meta_source)
                except (OSError, ValueError):
                    pass
            except Exception as exc:
                _issue(
                    bundle,
                    "YAHOO_CACHE_UNAVAILABLE",
                    f"{sec['security_id']}: no compatible cached Yahoo history ({_error_label(exc)}). Run with --refresh to fetch.",
                )
        return
    try:
        import yfinance as yf
    except ImportError:
        _issue(
            bundle,
            "YAHOO_DEPENDENCY_MISSING",
            "Install optional yfinance to use price_provider=yahoo; configured CSV data remain available.",
            "error",
        )
        return
    _issue(
        bundle,
        "YAHOO_RESEARCH_LIMITATION",
        "Yahoo adjusted price histories can be revised; this connector is not a survivorship-free, point-in-time historical universe.",
    )
    start = (
        (_day(as_of) - pd.DateOffset(years=int(config["data"].get("lookback_years", 5))))
        .date()
        .isoformat()
    )
    end = (_day(as_of) + pd.Timedelta(days=1)).date().isoformat()
    today = _day(datetime.now(timezone.utc))
    securities = bundle["securities"].copy()
    rows = []
    for idx, sec in securities.iterrows():
        if (
            sec.get("instrument_type") == "plan_fund"
            or pd.isna(sec.get("ticker"))
            or not str(sec.get("ticker", "")).strip()
        ):
            continue
        try:
            ticker = yf.Ticker(str(sec["ticker"]))
            history = ticker.history(
                start=start, end=end, auto_adjust=False, actions=True, raise_errors=True
            )
            if history.empty or not {"Close", "Adj Close"}.issubset(history.columns):
                raise ValueError("No raw/adjusted history")
            received_at = _now()
            payload = json.loads(history.reset_index().to_json(orient="records", date_format="iso"))
            source = _archive(
                config,
                "yahoo",
                str(sec["security_id"]),
                payload,
                "https://finance.yahoo.com/quote/" + str(sec["ticker"]),
                received_at,
                point_in_time="current_retrieval_of_adjusted_history; not historical vintage",
                adjustment="Yahoo Adj Close; not an independently verified total-return index",
            )
            bundle["sources"].append(source)
            for date, row in history.iterrows():
                day = pd.Timestamp(date).tz_localize(None).normalize()
                rows.append(
                    {
                        "security_id": sec["security_id"],
                        "date": day.date().isoformat(),
                        "close": row["Close"],
                        "adjusted_close": row["Adj Close"],
                        "volume": row.get("Volume"),
                        "currency": sec.get("currency"),
                        "available_at": (day + pd.Timedelta(days=1)).date().isoformat(),
                        "received_at": received_at,
                        "source_id": source["source_id"],
                    }
                )
            # Never introduce today's metadata into a historical snapshot.
            if _day(as_of) >= today:
                info = ticker.info
                metadata_received = _now()
                keep = {
                    k: info.get(k)
                    for k in ("marketCap", "currency", "exchange", "sector", "longName")
                }
                meta = _archive(
                    config,
                    "yahoo_metadata",
                    str(sec["security_id"]),
                    keep,
                    source["url"],
                    metadata_received,
                    point_in_time="current_snapshot_only",
                    observation_date=as_of,
                )
                bundle["sources"].append(meta)
                mcap = keep.get("marketCap")
                if (
                    mcap is not None
                    and float(mcap) > 0
                    and keep.get("currency") == sec.get("currency")
                ):
                    securities.at[idx, "market_cap"] = float(mcap)
                    securities.at[idx, "market_cap_as_of"] = as_of
                    securities.at[idx, "market_cap_available_at"] = metadata_received
                    securities.at[idx, "market_cap_received_at"] = metadata_received
                    securities.at[idx, "market_cap_source_id"] = meta["source_id"]
        except Exception as exc:
            _issue(
                bundle,
                "YAHOO_FETCH_FAILED",
                f"Yahoo request failed for {sec['security_id']} ({_error_label(exc)}); no synthetic replacement.",
            )
    bundle["securities"] = securities
    _merge(bundle, "prices", pd.DataFrame(rows, columns=FRAME_COLUMNS["prices"]))


def _enrich_sec(bundle: dict, config: dict, as_of: str) -> None:
    ua = config["data"].get("sec_user_agent", "")
    refresh = config["data"].get("refresh_network", False)
    if refresh and (
        not ua
        or "@" not in ua
        or any(x in ua.lower() for x in ("example.com", "your_email", "your@email"))
    ):
        _issue(
            bundle,
            "SEC_CONTACT_REQUIRED",
            "Set data.sec_user_agent to an application name and your real contact email before fetching SEC data.",
            "error",
        )
        return
    rows, fetched = [], {}
    for _, sec in bundle["securities"].iterrows():
        if sec.get("instrument_type") != "equity":
            continue
        cik = sec.get("cik")
        try:
            if pd.isna(cik) or not str(cik).strip():
                _issue(
                    bundle,
                    "SEC_CIK_MISSING",
                    f"{sec['security_id']}: no verified CIK; statements remain unavailable from SEC.",
                )
                continue
            cik_str = f"{int(str(cik).removeprefix('CIK')):010d}"
            if cik_str not in fetched:
                if refresh:
                    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik_str}.json"
                    payload, received_at = _fetch_json(url, ua)
                    source = _archive(
                        config,
                        "sec",
                        cik_str,
                        payload,
                        url,
                        received_at,
                        point_in_time="filing_date_plus_one_calendar_day",
                        reporting_currency="USD tags only",
                    )
                    time.sleep(0.12)  # Below SEC's aggregate 10 requests/second limit.
                else:
                    payload, received_at, source = _cached(config, "sec", cik_str)
                bundle["sources"].append(source)
                fetched[cik_str] = payload, received_at, source
            payload, received_at, source = fetched[cik_str]
            result = extract_sec_fundamentals(
                payload, sec["security_id"], as_of, received_at, source["source_id"]
            )
            if result is None:
                _issue(
                    bundle,
                    "SEC_UNSUPPORTED_FACTS",
                    f"{sec['security_id']}: no usable standard USD US-GAAP statements by {as_of}; use a reviewed statement CSV adapter.",
                )
            else:
                rows.append(result)
                for message in result["data_warnings"]:
                    _issue(bundle, "SEC_DATA_REVIEW", f"{sec['security_id']}: {message}")
        except Exception as exc:
            _issue(
                bundle,
                "SEC_FETCH_FAILED",
                f"SEC request failed for {sec['security_id']} ({_error_label(exc)}); configured CSV data remain available.",
            )
    _merge(bundle, "fundamentals", pd.DataFrame(rows))


def _enrich_fred(bundle: dict, config: dict, as_of: str) -> None:
    refresh = config["data"].get("refresh_network", False)
    key = os.getenv(config["data"].get("fred_api_key_env", "FRED_API_KEY"))
    if refresh and not key:
        _issue(
            bundle,
            "FRED_KEY_MISSING",
            "FRED is enabled but the configured environment variable has no API key.",
            "error",
        )
        return
    start = (
        (_day(as_of) - pd.DateOffset(years=int(config["data"].get("lookback_years", 5))))
        .date()
        .isoformat()
    )
    mode = (
        "historical_vintage"
        if _day(as_of) < _day(datetime.now(timezone.utc))
        else "current_daily_vintage"
    )
    _issue(
        bundle,
        "FRED_INTRADAY_TIMING_UNVERIFIED",
        "FRED supplies daily vintages; the adapter cannot certify that every same-day release was public by 16:00 New York. Use release-timestamped macro CSVs for strict intraday reconstruction.",
    )
    rows = []
    for item in config["data"].get("fred_series", []):
        series_id = str(item.get("series_id")) if isinstance(item, dict) else str(item)
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", series_id):
            _issue(
                bundle,
                "FRED_SERIES_INVALID",
                "Configured FRED series ID contains unsupported characters.",
            )
            continue
        params = {
            "series_id": series_id,
            "api_key": key,
            "file_type": "json",
            "realtime_start": as_of,
            "realtime_end": as_of,
        }
        try:
            if not refresh:
                payload, received_at, source = _cached(config, "fred", series_id, as_of)
                bundle["sources"].append(source)
                for obs in payload.get("observations", []):
                    try:
                        value = float(obs["value"])
                        if not math.isfinite(value):
                            continue
                    except (ValueError, TypeError, KeyError):
                        continue
                    rows.append(
                        {
                            "series_id": series_id,
                            "date": obs["date"],
                            "value": value,
                            "vintage_date": source["vintage_date"],
                            "received_at": received_at,
                            "units": source.get("units", "unknown"),
                            "source_id": source["source_id"],
                        }
                    )
                continue
            meta_url = "https://api.stlouisfed.org/fred/series?" + urlencode(params)
            meta, meta_received = _fetch_json(meta_url)
            meta_source = _archive(
                config,
                "fred_metadata",
                series_id,
                meta,
                meta_url,
                meta_received,
                point_in_time=mode,
                vintage_date=as_of,
            )
            bundle["sources"].append(meta_source)
            series_meta = meta.get("seriess", [{}])[0]
            units = series_meta.get("units", "unknown")
            params.update(
                {
                    "observation_start": start,
                    "observation_end": as_of,
                    "units": "lin",
                    "output_type": 1,
                    "limit": 100000,
                    "sort_order": "asc",
                }
            )
            url = "https://api.stlouisfed.org/fred/series/observations?" + urlencode(params)
            payload, received_at = _fetch_json(url)
            if "error_code" in payload:
                raise ValueError("FRED rejected request")
            source = _archive(
                config,
                "fred",
                series_id,
                payload,
                url,
                received_at,
                point_in_time=mode,
                vintage_date=as_of,
                units=units,
                caveat="daily as-of vintage; does not establish intraday publication or historical system receipt",
            )
            bundle["sources"].append(source)
            if int(payload.get("count", 0)) > len(payload.get("observations", [])):
                _issue(
                    bundle,
                    "FRED_TRUNCATED",
                    f"{series_id}: response did not contain all observations; narrow lookback.",
                )
            for obs in payload.get("observations", []):
                try:
                    value = float(obs["value"])
                    if not math.isfinite(value):
                        continue
                except (ValueError, TypeError, KeyError):
                    continue
                rows.append(
                    {
                        "series_id": series_id,
                        "date": obs["date"],
                        "value": value,
                        "vintage_date": as_of,
                        "received_at": received_at,
                        "units": units,
                        "source_id": source["source_id"],
                    }
                )
        except Exception as exc:
            _issue(
                bundle,
                "FRED_FETCH_FAILED",
                f"FRED request failed for {series_id} ({_error_label(exc)}); no replacement values inserted.",
            )
    _merge(bundle, "macro", pd.DataFrame(rows, columns=FRAME_COLUMNS["macro"]))


def enrich_bundle(bundle: dict, config: dict, as_of: str) -> dict:
    """Merge configured CSVs and optional live connectors into a portfolio bundle.

    ``demo`` and ``offline`` are network-free even when connector flags are set.
    The universe is the user's configured cross-section; this adapter never
    claims today's membership or market capitalization are historical facts.
    """
    result = {
        k: v.copy() if isinstance(v, (pd.DataFrame, list, dict)) else v for k, v in bundle.items()
    }
    result.setdefault("issues", [])
    result.setdefault("sources", [])
    result["as_of"] = as_of
    data = config.get("data", {})
    result["mode"] = data.get("mode", "offline")
    result["scope"] = "configured_universe"
    for name, cols in FRAME_COLUMNS.items():
        result.setdefault(name, pd.DataFrame(columns=cols))
    universe = load_csv(data.get("universe_csv"), "securities", result, config)
    if not universe.empty:
        current = result.get("securities", pd.DataFrame())
        # Existing stable identifiers/issuer mappings remain authoritative;
        # supplemental universe rows add research candidates, not holdings.
        combined = pd.concat([current, universe], ignore_index=True)
        result["securities"] = combined.drop_duplicates("security_id", keep="first").reset_index(
            drop=True
        )
        if "eligible" in result["securities"]:
            result["securities"]["eligible"] = result["securities"]["eligible"].map(
                lambda x: str(x).strip().lower() in ("true", "1", "yes") if pd.notna(x) else False
            )
    for name in ("prices", "fundamentals", "macro", "fund_holdings", "forecasts"):
        _merge(result, name, load_csv(data.get(name + "_csv"), name, result, config))
    if result["mode"] == "live":
        if data.get("price_provider", "csv") == "yahoo":
            _enrich_yahoo(result, config, as_of)
        if data.get("sec_enabled", False):
            _enrich_sec(result, config, as_of)
        if data.get("fred_enabled", False):
            _enrich_fred(result, config, as_of)
    elif (
        data.get("sec_enabled") or data.get("fred_enabled") or data.get("price_provider") == "yahoo"
    ):
        _issue(
            result,
            "OFFLINE_CONNECTORS_SKIPPED",
            "Network connectors were skipped because data.mode is demo/offline.",
            "info",
        )
    _filter_frames(result, as_of, bool(data.get("require_received_by_cutoff", False)))
    if result["prices"].empty:
        _issue(
            result,
            "PRICES_UNAVAILABLE",
            "No usable price history; momentum and covariance estimates are unavailable.",
        )
    if result["fundamentals"].empty:
        _issue(
            result,
            "FUNDAMENTALS_UNAVAILABLE",
            "No usable fundamental statements; quality/value fields remain missing.",
        )
    if result["macro"].empty:
        _issue(
            result,
            "MACRO_UNAVAILABLE",
            "No macro observations were loaded; macro panels remain empty.",
            "info",
        )
    return result
