"""Current holdings: the newest completed collection, dated separately from saved reviews.

Captured amounts stay exact Decimal strings and are shown as observed. The collection
receipt time is a server receipt time, never a quote time; the source valuation time is
unknown unless supplemental evidence attests it. Subtotals are grouped by explicit value
currency without FX conversion and are never labeled reconciled NAV.
"""

from decimal import Decimal, localcontext

from .adapter import load_collector
from .calendar import review_context
from .public import _clean

SCHEMA_VERSION = 1
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
TOTALS_LABEL = "Captured subtotals grouped by explicit value currency; unlabeled amounts are listed separately and no FX conversion was applied."


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


def _account(account, ledger, capture, account_codes):
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
    }


def _position(position, capture, securities):
    security = securities.get(position["security_id"], {})
    return {
        "account_id": position["account_id"],
        "source_id": position["source_id"],
        "snapshot_id": position["snapshot_id"],
        "row_number": position["row_number"],
        "symbol": position["raw_symbol"],
        "name": security.get("name"),
        "quantity": position["quantity"],
        "price": position["price"],
        "market_value": position["market_value"],
        "currency": position["currency"],
        "average_cost": position["average_cost"],
        "total_cost": position["total_cost"],
        "observed_at": capture["captured_at"] if capture else None,
        "value_basis": "captured_as_observed",
        "security_id": position["security_id"],
        "resolution_status": security.get("resolution_status", "unresolved"),
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


def _projection(bundle):
    """Accounts and positions projected from the exact ledger with their capture dates."""
    ledger, collector = bundle["ledger"], bundle["collector"]
    securities = {row["security_id"]: row for row in ledger["securities"]}
    captures = {row["account_id"]: row for row in ledger["captures"]}
    account_codes = collector.get("account_issue_codes") or {}
    accounts = [
        _account(account, ledger, captures.get(account["account_id"]), account_codes)
        for account in ledger["accounts"]
    ]
    positions = [
        _position(row, captures.get(row["account_id"]), securities) for row in ledger["positions"]
    ]
    return accounts, positions


def current_snapshot(source_path, *, supplemental=None, account_ids=None, generated_at=None):
    """Project the newest completed collector publication as current holdings.

    The caller must supply a collector database. Source receipts are accepted through the
    generation instant, so a Sunday capture stays current on Sunday while the market
    observation date remains the last completed session.
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
    exceptions = _exceptions(bundle["issues"])
    accounts, positions = _projection(bundle)
    response = {
        "schema_version": SCHEMA_VERSION,
        "review_kind": "current",
        "timeline": timeline,
        "dates": _dates(timeline, collector, bundle),
        "collection": _collection(collector, accounts, {issue["code"] for issue in exceptions}),
        "accounts": accounts,
        "positions": positions,
        "totals": _totals(accounts),
        "exceptions": exceptions,
        "excluded_rows": bundle["ledger"]["excluded_rows"],
    }
    return _clean(response)
