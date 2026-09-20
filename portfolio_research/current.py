"""Current holdings: the newest completed collection, dated separately from saved reviews.

Captured amounts stay exact Decimal strings and are shown as observed. The collection
receipt time is a server receipt time, never a quote time; the source valuation time is
unknown unless supplemental evidence attests it. Subtotals are grouped by the currency the
source itself labelled, without FX conversion, and are never labeled reconciled NAV.

Beside the captured amounts, listing identity and a USD presentation are derived from
cached provider metadata and dated FX observations only (this view never refreshes a
provider). What a holding is quoted in comes from the exchange its listing trades on and
what an account reports its values in is a fact about that account, so neither is guessed
from the amounts and neither is ever asked about; amounts that cannot be presented stay
unconverted and become open exceptions an owner can resolve.
"""

from decimal import Decimal, localcontext
from pathlib import Path

from .adapter import load_collector
from .calendar import review_context
from .public import _clean

SCHEMA_VERSION = 1
LISTED_STATUSES = frozenset({"mapped", "resolved", "resolved_from_display", "resolved_by_search"})
OPEN_STATUSES = frozenset({"ambiguous", "unresolved"})
PRESENTED_POSITION_FIELDS = (
    "quote_symbol",
    "quote_currency",
    "quote_unit_factor",
    "price_major",
    "price_usd",
    "market_value_usd",
    "value_currency_basis",
    "fx_rate",
    "fx_pair",
    "fx_observation_date",
    "fx_source_id",
    "presentation_currency",
)
USD_LABEL = (
    "{currency} presentation of covered amounts using dated FX observations; not reconciled NAV"
)
NO_CURRENCY_LABEL = (
    "No base currency is configured; captured amounts are shown exactly as collected."
)
DECIMAL_PRECISION = 512
CAPTURE_COMPLETENESS = {"count-verified", "end-observed"}
SEVERITY_ORDER = {"error": 0, "warning": 1}
DATE_LABELS = {
    "collection_received_at": "Server receipt time of the newest complete collection; not a quote time.",
    "source_valuation_time": "Valuation date attested by supplemental evidence for every account; unknown means captured values are shown as observed.",
    "generated_at": "When this view was generated.",
    "market_observation_date": "Last completed NYSE session at generation time.",
    "information_cutoff": "Information available through generation time.",
    "review_month": "New York calendar month of generation.",
}
TOTALS_LABEL = "Captured subtotals grouped by the currency the source labelled; unlabeled amounts are listed separately and no FX conversion was applied."


def _decimal_text(value):
    return format(value, "f")


def _currency_order(currency):
    """Explicit currencies alphabetically, unlabeled amounts last."""
    return (currency is None, currency or "")


def _severity_rank(severity):
    return SEVERITY_ORDER.get(severity, len(SEVERITY_ORDER))


def _exceptions(issues):
    """One record per code and message, keeping the most severe variant."""
    unique = {}
    for issue in issues:
        record = {key: issue.get(key) for key in ("code", "message", "severity")}
        key = (record["code"], record["message"])
        previous = unique.get(key)
        if previous is None or _severity_rank(record["severity"]) < _severity_rank(
            previous["severity"]
        ):
            unique[key] = record
    return list(unique.values())


def _provider_config(source_path, config):
    """The given configuration, else the default one beside the collector database."""
    if config is not None:
        return config
    from .service import default_config

    return default_config(Path(source_path).expanduser().resolve().parent)


def _presented(bundle, config, timeline, supplemental):
    """Identity and USD presentation of the collection from cached providers only."""
    from portfolio_lab.pipeline import _normalize_collector

    presented, _ = _normalize_collector(bundle, config, False, "current", timeline, supplemental)
    return presented


def _sum_text(amounts):
    with localcontext() as context:
        context.prec = DECIMAL_PRECISION
        return _decimal_text(sum((Decimal(amount) for amount in amounts), Decimal(0)))


def _account_usd(presented_account, held, unconverted_accounts):
    """Covered USD holdings and cash of one account; unconverted amounts are counted."""
    covered = [row["market_value_usd"] for row in held if row.get("market_value_usd") is not None]
    cash = presented_account.get("cash_usd")
    unconverted = len(held) - len(covered)
    return {
        "covered_total": _sum_text([*covered, *([cash] if cash is not None else [])]),
        "holdings": _sum_text(covered),
        "cash": cash,
        "covered_position_count": len(covered),
        "unconverted_position_count": unconverted,
        "reported_currency": presented_account.get("reported_currency"),
        "currency_basis": presented_account.get("currency_basis"),
        "fx_pair": presented_account.get("fx_pair"),
        "fx_observation_date": presented_account.get("fx_observation_date"),
        "fully_covered": unconverted == 0
        and presented_account.get("account_id") not in unconverted_accounts,
    }


def _identity_counts(held):
    statuses = [row.get("resolution_status") for row in held]
    return {
        "resolved": sum(status in LISTED_STATUSES for status in statuses),
        "open": sum(status in OPEN_STATUSES for status in statuses),
    }


def _usd_totals(presented):
    normalization = presented["normalization"]
    positions = presented["ledger"]["positions"]
    currency = normalization["presentation_currency"]
    if currency is None:
        return {
            "currency": None,
            "covered_total": None,
            "covered_position_count": 0,
            "unconverted_position_count": len(positions),
            "unconverted_accounts": [],
            "method_version": normalization["method_version"],
            "label": NO_CURRENCY_LABEL,
        }
    return {
        "currency": currency,
        "covered_total": normalization["covered_value_usd"],
        "covered_position_count": sum(row["market_value_usd"] is not None for row in positions),
        "unconverted_position_count": normalization["unconverted_positions"],
        "unconverted_accounts": list(normalization.get("unconverted_account_ids", [])),
        "method_version": normalization["method_version"],
        "label": USD_LABEL.format(currency=currency),
    }


def _public_exceptions(records):
    """Owner-facing exception records; the stable key is a digest, not a credential."""
    return [{"key": record["key"], **_clean(record)} for record in records]


def _empty_subtotal():
    return {"holdings": Decimal(0), "cash": Decimal(0), "count": 0, "missing": 0}


def _subtotals(held, cash, cash_currency):
    """Per-currency captured holdings plus captured cash; ``cash`` is None when unknown."""
    groups = {}
    with localcontext() as context:
        context.prec = DECIMAL_PRECISION
        for position in held:
            group = groups.setdefault(position["currency"], _empty_subtotal())
            group["count"] += 1
            if position["market_value"] is None:
                group["missing"] += 1
            else:
                group["holdings"] += Decimal(position["market_value"])
        if cash is not None:
            groups.setdefault(cash_currency, _empty_subtotal())["cash"] += Decimal(cash)
        return [
            {
                "currency": currency,
                "holdings": _decimal_text(group["holdings"]),
                "cash": _decimal_text(group["cash"]) if cash is not None else None,
                "total": _decimal_text(group["holdings"] + group["cash"]),
                "position_count": group["count"],
                "missing_value_count": group["missing"],
            }
            for currency, group in sorted(groups.items(), key=lambda item: _currency_order(item[0]))
        ]


def _totals(accounts):
    groups = {}
    with localcontext() as context:
        context.prec = DECIMAL_PRECISION
        for account in accounts:
            for row in account["subtotals"]:
                group = groups.setdefault(
                    row["currency"],
                    {
                        "holdings": Decimal(0),
                        "cash": Decimal(0),
                        "accounts": set(),
                        "unknown_cash": set(),
                        "positions": 0,
                        "missing": 0,
                    },
                )
                group["holdings"] += Decimal(row["holdings"])
                if row["cash"] is None:
                    group["unknown_cash"].add(account["account_id"])
                else:
                    group["cash"] += Decimal(row["cash"])
                group["accounts"].add(account["account_id"])
                group["positions"] += row["position_count"]
                group["missing"] += row["missing_value_count"]
        by_currency = [
            {
                "currency": currency,
                "holdings": _decimal_text(group["holdings"]),
                "cash": _decimal_text(group["cash"]),
                "total": _decimal_text(group["holdings"] + group["cash"]),
                "account_count": len(group["accounts"]),
                "position_count": group["positions"],
                "missing_value_count": group["missing"],
                "unknown_cash_account_count": len(group["unknown_cash"]),
            }
            for currency, group in sorted(groups.items(), key=lambda item: _currency_order(item[0]))
        ]
    return {"by_currency": by_currency, "label": TOTALS_LABEL, "reconciled_nav": False}


def _account(account, ledger, capture, account_codes, presentation):
    aid = account["account_id"]
    held = [row for row in ledger["positions"] if row["account_id"] == aid]
    cash, cash_currency = account["cash"], account["captured_cash_currency"]
    details = (capture or {}).get("capture") or {}
    completeness = None
    if capture is not None:
        reported = details.get("completeness")
        completeness = reported if reported in CAPTURE_COMPLETENESS else "unverified"
    return {
        "account_id": aid,
        "source_id": account["source_id"],
        "name": account["name"],
        "snapshot_id": account["snapshot_id"],
        "captured_at": capture["captured_at"] if capture else None,
        "received_at": capture["received_at"] if capture else None,
        "completeness": completeness,
        "row_count": details.get("row_count"),
        "position_count": len(held),
        "excluded_count": sum(row["account_id"] == aid for row in ledger["excluded_rows"]),
        "cash": {"amount": cash, "currency": cash_currency} if cash is not None else None,
        "cash_currency_known": cash is not None and cash_currency is not None,
        "subtotals": _subtotals(held, cash, cash_currency),
        "valuation_date": account["valuation_date"],
        "value_basis": "attested" if account["valuation_date"] else "captured_as_observed",
        # The collector tags each account-scoped issue it raised; portfolio-wide codes (for
        # example NO_COMPLETE_BATCH) appear only in the top-level exception list.
        "exceptions": list(account_codes.get(aid, [])),
        "usd": _account_usd(
            presentation["accounts"].get(aid, {}),
            presentation["held"].get(aid, []),
            presentation["unconverted_accounts"],
        ),
        "identity": _identity_counts(presentation["held"].get(aid, [])),
        "open_exception_count": sum(
            record.get("account_id") == aid for record in presentation["exceptions"]
        ),
    }


def _resolution_status(presented, security):
    """Listing status; an owner mapping keeps the collector's own mapping status."""
    status = presented.get("resolution_status")
    if status is None or status == "mapped":
        return security.get("resolution_status", "unresolved")
    return status


def _position(position, capture, securities, presented, presented_securities):
    security = securities.get(position["security_id"], {})
    listing = presented_securities.get(presented["security_id"], {})
    return {
        "account_id": position["account_id"],
        "source_id": position["source_id"],
        "snapshot_id": position["snapshot_id"],
        "row_number": position["row_number"],
        "symbol": position["raw_symbol"],
        "name": listing.get("name") or security.get("name"),
        "quantity": position["quantity"],
        "price": position["price"],
        "market_value": position["market_value"],
        "currency": position["currency"],
        "average_cost": position["average_cost"],
        "total_cost": position["total_cost"],
        "observed_at": capture["captured_at"] if capture else None,
        "value_basis": "captured_as_observed",
        "security_id": presented["security_id"],
        "resolution_status": _resolution_status(presented, security),
        "value_currency": presented.get("reported_currency"),
        **{field: presented.get(field) for field in PRESENTED_POSITION_FIELDS},
    }


def _collection(collector, accounts, codes):
    batch = collector["batch"]
    captured = sum(account["snapshot_id"] is not None for account in accounts)
    if batch:
        status = "published"
    elif captured:
        status = "legacy_partial"
    else:
        status = "none"
    return {
        "batch_id": batch["id"] if batch else None,
        "status": status,
        "created_at": batch["created_at"] if batch else None,
        "completed_at": batch["completed_at"] if batch else None,
        "completeness": batch["completeness"] if batch else None,
        "account_count": captured,
        "newer_collection_failed": "PRIOR_COMPLETE_BATCH" in codes,
        "newer_collection_in_progress": bool(collector.get("newer_collection_in_progress")),
    }


def collection_facts(bundle) -> dict:
    """Receipt facts of a collected bundle: what status needs, with nothing presented."""
    collector = bundle["collector"]
    codes = {issue["code"] for issue in bundle.get("issues") or []}
    collection = _collection(collector, bundle["ledger"]["accounts"], codes)
    return {
        "collection_received_at": collector["collection_received_at"],
        "status": collection["status"],
        "account_count": collection["account_count"],
    }


def _dates(timeline, collector, bundle):
    return {
        "collection_received_at": collector["collection_received_at"],
        "source_valuation_time": bundle["valuation_date"],
        "generated_at": timeline["generated_at"],
        "market_observation_date": timeline["market_observation_date"],
        "information_cutoff": timeline["information_cutoff"],
        "review_month": timeline["review_month"],
        "labels": dict(DATE_LABELS),
    }


def _projection(bundle, presented):
    """Accounts and positions projected from the exact ledger with their capture dates.

    Captured fields come from the collector ledger unchanged; identity and USD fields
    come from the presented ledger, which keeps the collector's position order.
    """
    ledger, collector = bundle["ledger"], bundle["collector"]
    shown = presented["ledger"]
    if len(shown["positions"]) != len(ledger["positions"]):
        raise ValueError("The USD presentation does not match the collected positions.")
    securities = {row["security_id"]: row for row in ledger["securities"]}
    presented_securities = {row["security_id"]: row for row in shown["securities"]}
    captures = {row["account_id"]: row for row in ledger["captures"]}
    account_codes = collector.get("account_issue_codes") or {}
    held = {}
    for row in shown["positions"]:
        held.setdefault(row["account_id"], []).append(row)
    presentation = {
        "accounts": {row["account_id"]: row for row in shown["accounts"]},
        "held": held,
        "unconverted_accounts": set(presented["normalization"].get("unconverted_account_ids", [])),
        "exceptions": presented["exceptions"],
    }
    accounts = [
        _account(account, ledger, captures.get(account["account_id"]), account_codes, presentation)
        for account in ledger["accounts"]
    ]
    positions = [
        _position(row, captures.get(row["account_id"]), securities, shown_row, presented_securities)
        for row, shown_row in zip(ledger["positions"], shown["positions"], strict=True)
    ]
    return accounts, positions


def current_snapshot(
    source_path, *, supplemental=None, account_ids=None, generated_at=None, config=None
):
    """Project the newest completed collector publication as current holdings.

    The caller must supply a collector database. Source receipts are accepted through the
    generation instant, so a Sunday capture stays current on Sunday while the market
    observation date remains the last completed session.

    ``config`` locates the provider cache and selects listing and FX providers; without
    it the default configuration beside ``source_path`` is used. Providers are read from
    their cache only. ``exceptions`` lists collection issues; ``open_exceptions`` lists
    the identity and currency exceptions an owner can resolve.
    """
    timeline = review_context("current", generated_at=generated_at)
    bundle = load_collector(
        source_path,
        timeline["decision_date"],
        supplemental,
        account_ids,
        receipt_through=timeline["requested_date"],
        receipt_before=timeline["generated_at"],
    )
    collector = bundle["collector"]
    timeline["collection_received_at"] = collector["collection_received_at"]
    presented = _presented(bundle, _provider_config(source_path, config), timeline, supplemental)
    exceptions = _exceptions(presented["issues"])
    accounts, positions = _projection(bundle, presented)
    response = {
        "schema_version": SCHEMA_VERSION,
        "review_kind": "current",
        "timeline": timeline,
        "dates": _dates(timeline, collector, bundle),
        "collection": _collection(collector, accounts, {issue["code"] for issue in exceptions}),
        "accounts": accounts,
        "positions": positions,
        "totals": {**_totals(accounts), "usd": _usd_totals(presented)},
        "exceptions": exceptions,
        "identity": dict(presented["identity"]),
        "fx_observations": presented["fx_observations"],
        "excluded_rows": bundle["ledger"]["excluded_rows"],
    }
    public = _clean(response)
    public["open_exceptions"] = _public_exceptions(presented["exceptions"])
    return public
