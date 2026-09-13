"""Conservative, optional US sale-tax *reserve* from identified ordinary lots.

This module is not a tax-return calculator.  It uses user-confirmed marginal
rates, ignores capital-loss deductions/offsets, and requires adjusted USD basis.
It does not infer state tax, NIIT, carryovers, wash-sale adjustments, inherited
holding periods, or tax rates.  An estimated reserve can exceed the eventual
incremental liability.  The caller is responsible for checking account type,
instrument eligibility, and whether specific-lot identification is available.

Authority checked 2026-09-13: IRS Publication 550 (2025), Holding Period and
Wash Sales, https://www.irs.gov/publications/p550 .  Ordinary purchased stock is
long-term only after the calendar-year anniversary of its acquisition date;
365 elapsed days is not an adequate rule.  Specific-lot selection is a proposed
identification for broker review; this module never instructs a broker.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

import pandas as pd

SOURCE_URL = "https://www.irs.gov/publications/p550"
REQUIRED_COLUMNS = {
    "account_id",
    "security_id",
    "lot_id",
    "acquired_date",
    "quantity",
    "basis_per_share",
    "currency",
}
_ZERO = Decimal("0")


def _issue(code: str, message: str, severity: str = "warning") -> dict:
    return {"severity": severity, "code": code, "message": message}


def _result(status: str, issues: list[dict], **values: Any) -> dict:
    return {
        "status": status,
        "estimated_tax": None,
        "realized_gain": None,
        "gross_proceeds": None,
        "net_proceeds_after_tax": None,
        "quantity_sold": None,
        "lot_plan": [],
        "issues": issues,
        "currency": "USD",
        "estimate_basis": "Conditional reserve using configured marginal rates; not final tax liability.",
        "loss_credit_applied": False,
        "wash_sale_review_required": False,
        "source_url": SOURCE_URL,
        **values,
    }


def _decimal(value: Any) -> Decimal:
    # bool is a number in Python but not a valid financial input here.
    if value is None or isinstance(value, bool):
        raise ValueError("must be a finite number")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("must be a finite number") from None
    if not parsed.is_finite():
        raise ValueError("must be a finite number")
    return parsed


def _date(value: Any) -> date:
    if value is None or pd.isna(value):
        raise ValueError("date is missing")
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("use an actual calendar date in YYYY-MM-DD format")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError("date is not a valid calendar date") from None


def _anniversary(acquired: date) -> date:
    try:
        return acquired.replace(year=acquired.year + 1)
    except ValueError:
        # A Feb 29 purchase reaches its one-year boundary on Feb 28 in the
        # following non-leap year.  Mar 1 is the first long-term disposal day.
        if acquired.month == 2 and acquired.day == 29:
            return date(acquired.year + 1, 2, 28)
        raise


def estimate_sale(
    tax_lots: pd.DataFrame,
    account_id: str,
    security_id: str,
    quantity: float,
    price: float,
    as_of: str | date,
    config: dict,
) -> dict:
    """Estimate a nonnegative US tax reserve for a specific proposed sale.

    ``tax_lots`` must contain the columns in ``REQUIRED_COLUMNS``. ``quantity``
    in those rows is remaining open quantity, and ``basis_per_share`` is the
    adjusted cost basis, not aggregate basis. All matching lots are validated,
    even if they would not be chosen. Duplicate IDs or insufficient coverage
    return ``unavailable`` without a misleading partial estimate.

    ``config`` may be the application configuration or its ``tax`` group.
    ``enabled`` defaults false. Both marginal rates must be supplied in [0, 1],
    ``jurisdiction`` must be US, ``lot_method`` must be min_tax, and
    ``loss_credit_rate`` must be zero. Rates are assumptions, not inferred law.
    The min_tax plan sorts on nonnegative tax per share, with acquisition date
    and lot ID as stable tie-breakers. It does not optimize future taxes.

    Coverage is verified for the requested quantity only. To verify coverage
    of an entire holding, the caller must call with that holding's full
    quantity before optimizing a partial sale. The function has no position
    ledger from which to discover unreported lots or transfers.

    Amounts are unrounded USD numbers for downstream funding calculations;
    round for display only. Trading costs are not deducted here and must be
    charged exactly once by the allocator. Losses never reduce this reserve,
    including when the caller has asserted a verified wash-sale window.
    """
    if not isinstance(config, dict):
        return _result(
            "unavailable", [_issue("TAX_CONFIG_INVALID", "Tax configuration must be an object.")]
        )
    cfg = config.get("tax", config)
    if not isinstance(cfg, dict):
        return _result(
            "unavailable", [_issue("TAX_CONFIG_INVALID", "Tax configuration must be an object.")]
        )
    if cfg.get("enabled", False) is False:
        return _result(
            "disabled",
            [_issue("TAX_ESTIMATOR_DISABLED", "Optional tax estimation is disabled.", "info")],
        )
    if cfg.get("enabled") is not True:
        return _result(
            "unavailable", [_issue("TAX_CONFIG_INVALID", "tax.enabled must be a boolean.")]
        )

    problems = []
    if cfg.get("jurisdiction", "US") != "US":
        problems.append(
            _issue(
                "TAX_JURISDICTION_UNSUPPORTED",
                "Only ordinary US taxable-account sale estimates are supported.",
            )
        )
    if cfg.get("lot_method", "min_tax") != "min_tax":
        problems.append(
            _issue(
                "TAX_LOT_METHOD_UNSUPPORTED", "Only explicit min_tax lot selection is implemented."
            )
        )
    if not isinstance(cfg.get("wash_sale_window_verified", False), bool):
        problems.append(
            _issue("TAX_CONFIG_INVALID", "wash_sale_window_verified must be a boolean.")
        )
    rates = {}
    for key in ("short_term_rate", "long_term_rate"):
        try:
            rate = _decimal(cfg.get(key))
            if not _ZERO <= rate <= 1:
                raise ValueError("must be between zero and one")
            rates[key] = rate
        except ValueError as exc:
            problems.append(
                _issue(
                    "TAX_RATE_UNCONFIRMED",
                    f"tax.{key} {exc}; supply a confirmed marginal-rate assumption.",
                )
            )
    try:
        if _decimal(cfg.get("loss_credit_rate", 0)) != 0:
            raise ValueError("must be zero")
    except ValueError:
        problems.append(
            _issue(
                "TAX_LOSS_CREDIT_UNSUPPORTED",
                "Loss credits and gain offsets require tax context not represented here; loss_credit_rate must be zero.",
            )
        )
    try:
        sale_date = _date(as_of)
        sale_qty, sale_price = _decimal(quantity), _decimal(price)
        if sale_qty < 0 or sale_price <= 0:
            raise ValueError("Sale quantity must be nonnegative and price positive.")
        if not account_id or not security_id:
            raise ValueError("Account and security IDs are required.")
    except (ValueError, TypeError) as exc:
        problems.append(_issue("TAX_SALE_INPUT_INVALID", str(exc)))
    if problems:
        return _result("unavailable", problems)
    if sale_qty == 0:
        return _result(
            "estimated",
            [],
            estimated_tax=0.0,
            realized_gain=0.0,
            gross_proceeds=0.0,
            net_proceeds_after_tax=0.0,
            quantity_sold=0.0,
        )
    if not isinstance(tax_lots, pd.DataFrame) or not REQUIRED_COLUMNS <= set(tax_lots.columns):
        missing = (
            sorted(REQUIRED_COLUMNS - set(tax_lots.columns))
            if isinstance(tax_lots, pd.DataFrame)
            else sorted(REQUIRED_COLUMNS)
        )
        return _result(
            "unavailable",
            [
                _issue(
                    "TAX_LOT_COLUMNS_MISSING",
                    f"Required lot columns are missing: {', '.join(missing)}.",
                )
            ],
        )

    rows = tax_lots.loc[
        (tax_lots["account_id"].astype(str) == str(account_id))
        & (tax_lots["security_id"].astype(str) == str(security_id))
    ]
    if rows.empty:
        return _result(
            "unavailable",
            [
                _issue(
                    "TAX_LOTS_MISSING",
                    f"No tax lots for account {account_id}, security {security_id}.",
                )
            ],
        )
    prepared, seen = [], set()
    for index, lot in rows.iterrows():
        try:
            raw_id = lot["lot_id"]
            if pd.isna(raw_id) or not str(raw_id).strip():
                raise ValueError("lot_id is missing")
            lot_id = str(raw_id)
            if lot_id in seen:
                raise ValueError(f"duplicate lot_id {lot_id}")
            seen.add(lot_id)
            acquired = _date(lot["acquired_date"])
            if acquired > sale_date:
                raise ValueError("acquired_date is after the proposed sale date")
            lot_qty = _decimal(lot["quantity"])
            basis = _decimal(lot["basis_per_share"])
            if lot_qty <= 0:
                raise ValueError("remaining quantity must be positive")
            if basis < 0:
                raise ValueError("adjusted basis_per_share must be nonnegative")
            if str(lot["currency"]).upper() != "USD":
                raise ValueError("adjusted tax basis must be denominated in USD")
            term = "long_term" if sale_date > _anniversary(acquired) else "short_term"
            rate = rates[f"{term}_rate"]
            gain_per_share = sale_price - basis
            tax_per_share = max(gain_per_share, _ZERO) * rate
            prepared.append(
                {
                    "lot_id": lot_id,
                    "acquired": acquired,
                    "quantity": lot_qty,
                    "basis": basis,
                    "term": term,
                    "rate": rate,
                    "gain_per_share": gain_per_share,
                    "tax_per_share": tax_per_share,
                }
            )
        except (ValueError, TypeError, OverflowError) as exc:
            problems.append(_issue("TAX_LOT_INVALID", f"Lot row {index}: {exc}."))
    if problems:
        return _result("unavailable", problems)

    available_qty = sum((lot["quantity"] for lot in prepared), _ZERO)
    if available_qty < sale_qty:
        return _result(
            "unavailable",
            [
                _issue(
                    "TAX_LOT_COVERAGE_INCOMPLETE",
                    f"Known lots cover {available_qty} shares but the proposed sale requires {sale_qty}; no partial tax estimate was used.",
                )
            ],
            covered_quantity=float(available_qty),
            requested_quantity=float(sale_qty),
        )

    prepared.sort(key=lambda lot: (lot["tax_per_share"], lot["acquired"], lot["lot_id"]))
    remaining, total_tax, total_gain = sale_qty, _ZERO, _ZERO
    plan, has_losses = [], False
    for lot in prepared:
        if remaining == 0:
            break
        sold = min(remaining, lot["quantity"])
        gain, tax = sold * lot["gain_per_share"], sold * lot["tax_per_share"]
        total_gain += gain
        total_tax += tax
        remaining -= sold
        has_losses = has_losses or gain < 0
        plan.append(
            {
                "lot_id": lot["lot_id"],
                "acquired_date": lot["acquired"].isoformat(),
                "quantity": float(sold),
                "remaining_quantity": float(lot["quantity"] - sold),
                "basis_per_share": float(lot["basis"]),
                "sale_price": float(sale_price),
                "proceeds": float(sold * sale_price),
                "realized_gain": float(gain),
                "holding_period": lot["term"],
                "marginal_rate": float(lot["rate"]),
                "estimated_tax": float(tax),
                "loss_credit": 0.0,
            }
        )

    issues = [
        _issue(
            "TAX_ESTIMATE_CONDITIONAL",
            "Tax reserve uses configured marginal rates and positive lot gains only. It excludes gain/loss netting, carryovers, other income, unconfigured additional taxes, and special holding-period rules.",
            "info",
        ),
        _issue(
            "TAX_LOT_IDENTIFICATION_REQUIRED",
            "The min_tax lot plan requires broker-supported specific-lot identification and confirmation; it is not an order.",
            "info",
        ),
    ]
    if has_losses:
        issues.append(
            _issue(
                "TAX_LOSS_BENEFIT_EXCLUDED",
                "Loss lots create no credit, refund, or offset against gain-lot reserves.",
            )
        )
        window_end = (sale_date + timedelta(days=30)).isoformat()
        if not cfg.get("wash_sale_window_verified", False):
            issues.append(
                _issue(
                    "TAX_WASH_SALE_WINDOW_UNVERIFIED",
                    f"Review substantially identical purchases across relevant accounts in the 30 days before and after the sale, through {window_end}. Future-window purchases are unresolved; no wash-sale clearance is inferred.",
                )
            )
        else:
            issues.append(
                _issue(
                    "TAX_WASH_SALE_ASSERTION_ONLY",
                    f"The configuration asserts a verified wash-sale window through {window_end}; this lot-only engine cannot independently verify transactions or future purchases. No loss benefit is applied.",
                )
            )
    proceeds = sale_qty * sale_price
    return _result(
        "estimated",
        issues,
        estimated_tax=float(total_tax),
        realized_gain=float(total_gain),
        gross_proceeds=float(proceeds),
        net_proceeds_after_tax=float(proceeds - total_tax),
        quantity_sold=float(sale_qty),
        lot_plan=plan,
        wash_sale_review_required=has_losses,
        short_term_rate=float(rates["short_term_rate"]),
        long_term_rate=float(rates["long_term_rate"]),
    )
