"""Exact prospective endpoints and explicitly observed decimal comparison ledgers."""

import re
from copy import deepcopy
from decimal import Decimal, InvalidOperation, localcontext

import pandas as pd

from portfolio_lab.analytics import json_safe
from portfolio_lab.evaluation import _stats, evaluate_forecasts

from .calendar import _calendar, decision_context, new_york_dates

CONVENTIONS = {
    "prices": "Unadjusted terminal prices plus explicit cash distributions and corporate actions",
    "costs": "Explicit event costs, charged once to the originating account",
    "taxes": "Explicit conditional reserves only; no final after-tax return is asserted",
    "flows": "Identical dated external flows in each account across comparison arms",
    "returns": "Dollar investment gain; no rate without a chosen cash-flow timing convention",
}
POLICY_ARMS = {"approved_benchmark", "simple_policy", "model_policy"}
ARMS = POLICY_ARMS | {"no_discretionary_change", "actual_decision"}


def _aware(value, name):
    try:
        if not isinstance(value, str) or "T" not in value:
            raise ValueError
        stamp = pd.Timestamp(value)
        if pd.isna(stamp) or stamp.tzinfo is None:
            raise ValueError
        return stamp.tz_convert("UTC")
    except (ValueError, TypeError, OverflowError):
        raise ValueError(f"{name} requires a full timestamp with an explicit timezone.") from None


def _day(value, name):
    try:
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError
        stamp = pd.Timestamp(value)
        if pd.isna(stamp):
            raise ValueError
        return stamp
    except (ValueError, TypeError, OverflowError):
        raise ValueError(f"{name} must be a valid YYYY-MM-DD calendar date.") from None


def _cutoff(value):
    if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return (
            _day(value, "evaluation_date").tz_localize("UTC")
            + pd.Timedelta(days=1)
            - pd.Timedelta(nanoseconds=1)
        )
    return _aware(value, "evaluation_date")


def _issue(code, message):
    return {"code": code, "severity": "warning", "message": message}


def _decimal(value, name):
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{name} requires an explicit decimal amount.")
    try:
        text = str(value)
        number = Decimal(text)
        if (
            len(text) > 260
            or not number.is_finite()
            or abs(number.as_tuple().exponent) > 256
            or number.adjusted() > 256
        ):
            raise InvalidOperation
    except InvalidOperation:
        raise ValueError(f"{name} requires a finite bounded decimal amount.") from None
    return number


def _endpoint(prices, sid, session, cutoff, start=False):
    required = {"security_id", "date", "adjusted_close", "available_at", "received_at"}
    if not isinstance(prices, pd.DataFrame) or not required <= set(prices):
        return (
            None,
            "Endpoint prices require identity, exact date, adjusted close, publication and receipt timestamps.",
        )
    close = _calendar(session.year).session_close(session)
    candidates = []
    for row in prices.loc[prices["security_id"] == sid].to_dict("records"):
        try:
            if _day(row["date"], "price date") != session:
                continue
            available = _aware(row["available_at"], "available_at")
            received = _aware(row["received_at"], "received_at")
            if not close <= available <= received <= cutoff:
                continue
            value = _decimal(row["adjusted_close"], "adjusted close")
            if value < 0 or (value == 0 and (start or row.get("worthless_confirmed") is not True)):
                continue
            candidates.append((available, received, value))
        except (ValueError, TypeError):
            continue
    if not candidates:
        return (
            None,
            "The exact close is missing, unverified, not published/received yet, or zero without confirmed worthless treatment.",
        )
    newest = max(row[0] for row in candidates)
    current = [row for row in candidates if row[0] == newest]
    if len({row[2] for row in current}) != 1:
        return None, "Conflicting closes share the latest eligible publication timestamp."
    return {
        "date": session.date().isoformat(),
        "price": float(current[0][2]),
        "available_at": newest.isoformat(),
        "received_at": max(row[1] for row in current).isoformat(),
    }, None


def evaluate_saved_forecasts(bundle, prices, evaluation_date):
    """Score actual saved forecasts at exact execution and full-horizon closes.

    A calendar evaluation date includes that UTC day; aware timestamps request an
    exact intraday cutoff. Original forecast vintages remain separate. The caller
    supplies the forecast records actually selected in the saved decision run.
    """
    cutoff = _cutoff(evaluation_date)
    frozen = deepcopy(bundle)
    forecasts = frozen.get("forecasts", pd.DataFrame())
    if isinstance(forecasts, list):
        forecasts = pd.DataFrame(forecasts)
    frozen["forecasts"] = forecasts
    # Share forecast/probability validation; discard the reference's approximate
    # price matching and calculate exact exchange-session endpoints below.
    empty_prices = pd.DataFrame(columns=["security_id", "date", "adjusted_close", "available_at"])
    output = evaluate_forecasts(frozen, empty_prices, cutoff.date().isoformat())
    if not isinstance(forecasts, pd.DataFrame) or forecasts.empty:
        return output
    timeline = deepcopy(frozen.get("timeline") or decision_context(frozen["as_of"]))
    execution = _day(timeline["earliest_execution_date"], "earliest_execution_date")
    calendar = _calendar(execution.year)
    if not calendar.is_session(execution):
        raise ValueError("The frozen execution date is not an exchange session.")
    execution_close = calendar.session_close(execution)
    if _aware(timeline["earliest_execution_at"], "earliest_execution_at") != execution_close:
        raise ValueError("Execution policy must refer to the exact eligible session close.")
    decision = _aware(timeline["decision_cutoff"], "decision_cutoff")
    generated = _aware(timeline["generated_at"], "generated_at")
    if execution_close <= decision:
        raise ValueError("Execution must occur after the frozen decision cutoff.")
    for row in output["results"]:
        if row["status"] == "invalid_forecast":
            continue
        target = execution + pd.DateOffset(months=row["horizon_months"])
        endpoint_calendar = _calendar(target.year)
        endpoint = endpoint_calendar.date_to_session(target, direction="next")
        endpoint_close = endpoint_calendar.session_close(endpoint)
        row.update(
            decision_date=timeline["decision_date"],
            required_start_date=execution.date().isoformat(),
            required_end_date=endpoint.date().isoformat(),
            required_end_available_after=endpoint_close.isoformat(),
            target_date=endpoint.date().isoformat(),
            matured=bool(endpoint_close <= cutoff),
            start_date=None,
            end_date=None,
            start_adjusted_close=None,
            end_adjusted_close=None,
            realized_return=None,
            error=None,
            absolute_error=None,
            issues=[],
        )
        originals = forecasts.loc[
            (forecasts["security_id"] == row["security_id"])
            & (
                pd.to_numeric(forecasts["horizon_months"], errors="coerce") == row["horizon_months"]
            ),
            "forecast_date",
        ]
        parsed = pd.to_datetime(originals, errors="coerce", utc=True, format="mixed")
        # A bare forecast date starts its New York day when compared with the frozen cutoff.
        dates = new_york_dates(originals, parsed)
        group_dates = dates[dates.dt.strftime("%Y-%m-%d") == row["forecast_date"]]
        if "joint_validation_status" in forecasts:
            selected = forecasts.loc[group_dates.index, "joint_validation_status"]
            if selected.eq("invalid").any():
                row["status"] = "invalid_forecast"
                row["issues"].append(
                    _issue(
                        "INVALID_SAVED_JOINT_SCENARIOS",
                        "The saved run did not validate this forecast as part of its joint scenario set.",
                    )
                )
                continue
        if group_dates.gt(decision).any():
            row["status"] = "invalid_forecast"
            row["issues"].append(
                _issue(
                    "FORECAST_AFTER_DECISION",
                    "The original forecast is dated after the frozen decision cutoff.",
                )
            )
            continue
        if not row["matured"]:
            row["status"] = "immature"
            row["issues"].append(
                _issue(
                    "FULL_HORIZON_REQUIRED",
                    "No score exists before the full-horizon eligible session close.",
                )
            )
            continue
        start, start_error = _endpoint(prices, row["security_id"], execution, cutoff, start=True)
        end, end_error = _endpoint(prices, row["security_id"], endpoint, cutoff)
        if start_error or end_error:
            row["status"] = "missing_endpoint"
            row["issues"].extend(
                _issue(code, message)
                for code, message in (
                    ("EXACT_START_REQUIRED", start_error),
                    ("EXACT_END_REQUIRED", end_error),
                )
                if message
            )
            continue
        realized = end["price"] / start["price"] - 1
        error = row["predicted_return"] - realized
        row.update(
            status="scored",
            start_date=start["date"],
            end_date=end["date"],
            start_adjusted_close=start["price"],
            end_adjusted_close=end["price"],
            realized_return=realized,
            error=error,
            absolute_error=abs(error),
            endpoint_provenance={"start": start, "end": end},
        )
    output["cutoff_utc"] = cutoff.isoformat()
    output["summary"] = _stats(output["results"])
    output["by_horizon"] = [
        {
            "horizon_months": horizon,
            **_stats([row for row in output["results"] if row.get("horizon_months") == horizon]),
        }
        for horizon in (6, 12, 18)
    ]
    output["status"] = (
        "evaluated"
        if output["summary"]["scored_count"]
        else "no_matured_forecasts"
        if output["results"] and all(row["status"] == "immature" for row in output["results"])
        else "unavailable"
    )
    output["prospective_eligible"] = bool(
        generated <= execution_close
        and bundle.get("mode") != "demo"
        and all(row["status"] != "invalid_forecast" for row in output["results"])
    )
    output["provenance"] = (
        "synthetic demonstration; not prospective evidence"
        if bundle.get("mode") == "demo"
        else "reconstruction; not prospective evidence"
        if generated > execution_close
        else "frozen before eligible execution; source provenance still required"
    )
    output["execution_policy"] = timeline["execution_policy"]
    output["mode"] = bundle.get("mode", "offline")
    return json_safe(output)


def _records(bundle, name):
    exact = bundle.get("ledger", {}).get(name)
    return deepcopy(exact) if isinstance(exact, list) else json_safe(bundle[name])


def baseline_records(result, config, bundle):
    if not result.get("summary", {}).get("complete"):
        return []
    positions, accounts = _records(bundle, "positions"), _records(bundle, "accounts")
    if any(row.get("quantity") is None for row in positions):
        return []
    for rows, fields in (
        (positions, ("quantity", "market_value")),
        (accounts, ("total_value", "cash")),
    ):
        for row in rows:
            for field in fields:
                row[field] = str(_decimal(row.get(field), f"baseline {field}"))
    candidates = result.get("allocation", {}).get("candidates", [])
    records = []
    for arm, candidate_name in [
        ("no_discretionary_change", "no_change"),
        ("approved_benchmark", "approved_benchmark_new_flows"),
        ("simple_policy", "simple_equal_issuer_sleeve"),
        ("model_policy", result.get("allocation", {}).get("selected_candidate")),
        ("actual_decision", None),
    ]:
        candidate = next(
            (row for row in candidates if row.get("candidate") == candidate_name), None
        )
        status = (
            "baseline_only"
            if arm not in POLICY_ARMS
            else "requires_execution"
            if candidate and candidate.get("status") == "review_ready"
            else "blocked"
        )
        records.append(
            {
                "arm": arm,
                "method_version": "prospective-ledger-2",
                "origin_run_id": result["run_id"],
                "valuation_date": bundle.get("valuation_date"),
                "timeline": deepcopy(bundle.get("timeline")),
                "positions": deepcopy(positions),
                "accounts": deepcopy(accounts),
                "status": status,
                "candidate": candidate_name,
                "proposed_basket": deepcopy(candidate.get("proposals", [])) if candidate else [],
                "flow_policy": config["allocation"].get("flow_policy"),
                "reason": "Starting units are frozen. Policy arms require reviewed dated executions; generated proposals are not invested benchmarks or strategies.",
                "precision": "source_decimal_ledger"
                if "ledger" in bundle
                else "decimal_conversion_of_saved_numerical_inputs",
                "mode": bundle.get("mode", "offline"),
                "after_tax_status": "unavailable",
                "conventions": deepcopy(CONVENTIONS),
            }
        )
    return records


def _baseline(baseline, end_date):
    start, end = (
        _day(baseline.get("valuation_date"), "baseline valuation_date"),
        _day(end_date, "end_date"),
    )
    if end < start or baseline.get("arm") not in ARMS:
        raise ValueError("Require a known comparator arm and an end date not before its baseline.")
    quantities, cash, currencies, navs, initial_values = {}, {}, {}, {}, {}
    for row in baseline.get("accounts", []):
        aid, currency = row.get("account_id"), row.get("currency")
        if not isinstance(aid, str) or not aid or aid in cash:
            raise ValueError("Baseline accounts require unique nonempty identities.")
        if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
            raise ValueError("Baseline accounts require explicit currencies.")
        if (
            row.get("complete") is not True
            or row.get("valuation_date", baseline["valuation_date"]) != baseline["valuation_date"]
        ):
            raise ValueError("Baseline accounts must be complete at one common valuation date.")
        cash[aid], navs[aid] = (
            _decimal(row.get("cash"), "starting cash"),
            _decimal(row.get("total_value"), "starting NAV"),
        )
        if cash[aid] < 0 or navs[aid] < 0:
            raise ValueError("Starting cash and NAV cannot be negative.")
        currencies[aid], initial_values[aid] = currency, Decimal(0)
    if not cash or len(set(currencies.values())) != 1:
        raise ValueError("A common-currency scope is required; FX is never inferred.")
    for row in baseline.get("positions", []):
        aid, sid = row.get("account_id"), row.get("security_id")
        key = (aid, sid)
        if aid not in cash or not isinstance(sid, str) or not sid or key in quantities:
            raise ValueError("Baseline positions require unique account/security identities.")
        if row.get("currency") != currencies[aid]:
            raise ValueError("Baseline position currency differs from its account.")
        quantities[key], value = (
            _decimal(row.get("quantity"), "starting quantity"),
            _decimal(row.get("market_value"), "starting market value"),
        )
        if quantities[key] < 0 or value < 0:
            raise ValueError("Starting short positions are unsupported.")
        initial_values[aid] += value
    if any(abs(navs[aid] - cash[aid] - initial_values[aid]) > Decimal("0.01") for aid in cash):
        raise ValueError("Baseline NAV must reconcile to starting holdings and cash.")
    return start, end, quantities, cash, currencies, navs


def _events(events, start, end):
    if not isinstance(events, list) or len(events) > 50000:
        raise ValueError("Supply at most 50,000 explicit ledger events.")
    seen, ordered, sequences = set(), [], set()
    for event in events:
        if not isinstance(event, dict):
            raise ValueError("Each ledger event must be an object.")
        eid = event.get("id")
        if not isinstance(eid, str) or not eid or eid in seen:
            raise ValueError("Ledger events need unique IDs; recurring fees appear once.")
        seen.add(eid)
        when = _day(event.get("date"), "event date")
        if not start < when <= end:
            raise ValueError(
                "Events must be after the baseline and on or before the evaluation end."
            )
        sequence = event.get("sequence", 0)
        if type(sequence) is not int or sequence < 0:
            raise ValueError("Event sequence must be a nonnegative integer.")
        key = (event.get("account_id"), when, sequence)
        if key in sequences:
            raise ValueError("Same-day account events need distinct explicit sequences.")
        sequences.add(key)
        ordered.append((when, sequence, event))
    return [
        event for _, _, event in sorted(ordered, key=lambda row: (row[0], row[1], row[2]["id"]))
    ]


def _policy_execution(baseline, events, confirmed):
    trades = [event for event in events if event.get("kind") == "trade"]
    if confirmed is not True or not trades:
        return "Policy arms require reviewed executions; unchanged starting holdings are not an invested benchmark or policy."
    if not baseline.get("proposed_basket") or any(
        event.get("policy_candidate") != baseline.get("candidate") for event in trades
    ):
        return "Executions must identify a frozen reviewed candidate basket; unimplemented policy simulation remains unavailable."
    expected, executed = {}, {}
    for leg in baseline["proposed_basket"]:
        quantity = leg.get("decimal_amounts", {}).get("quantity_change")
        if quantity is None:
            return "The reviewed policy basket lacks exact planned unit changes."
        key = (leg["account_id"], leg["security_id"])
        expected[key] = expected.get(key, Decimal(0)) + _decimal(quantity, "planned quantity")
    for event in trades:
        key = (event.get("account_id"), event.get("security_id"))
        executed[key] = executed.get(key, Decimal(0)) + _decimal(
            event.get("quantity"), "executed quantity"
        )
    if expected != executed:
        return "Executions do not match the frozen unit basket; record a reviewed policy revision before comparison."
    return None


def evaluate_ledger(
    baseline, events, end_prices, *, end_date, coverage_confirmed=False, execution_confirmed=False
):
    """Value explicit events in (baseline date, end date], with no FX guessing."""
    if coverage_confirmed is not True:
        return {
            "status": "blocked",
            "reason": "Confirm full corporate-action, distribution, fee, tax-reserve and external-flow coverage.",
        }
    if baseline.get("status") == "blocked":
        return {
            "status": "blocked",
            "reason": "This policy has no reviewed feasible starting treatment.",
        }
    with localcontext() as context:
        context.prec = 1024
        start, end, quantities, cash, currencies, navs = _baseline(baseline, end_date)
        ordered = _events(events, start, end)
        if baseline["arm"] in POLICY_ARMS:
            problem = _policy_execution(baseline, ordered, execution_confirmed)
            if problem:
                return {"status": "blocked", "reason": problem}
        net_flows, costs, tax_reserve = Decimal(0), Decimal(0), Decimal(0)
        for event in ordered:
            aid, sid = event.get("account_id"), event.get("security_id")
            if aid not in cash or event.get("currency") != currencies[aid]:
                raise ValueError("Events require a known account and matching explicit currency.")
            kind, key = event.get("kind"), (aid, sid)
            if kind in {"split", "distribution", "trade", "cash_out"} and (
                not isinstance(sid, str) or not sid
            ):
                raise ValueError("Security events require a nonempty identity.")
            if kind == "external_flow":
                amount = _decimal(event.get("amount"), "external flow")
                cash[aid] += amount
                net_flows += amount
            elif kind == "split":
                ratio = _decimal(event.get("ratio"), "split ratio")
                if ratio <= 0 or key not in quantities:
                    raise ValueError("Splits require an existing security and positive ratio.")
                quantities[key] = _decimal(quantities[key] * ratio, "resulting quantity")
            elif kind == "distribution":
                amount = _decimal(event.get("per_share"), "distribution per share")
                if amount < 0 or key not in quantities:
                    raise ValueError(
                        "Distributions require an existing security and nonnegative amount."
                    )
                cash[aid] += quantities[key] * amount
            elif kind in {"fee", "tax_reserve"}:
                amount = _decimal(event.get("amount"), "expense")
                if amount < 0:
                    raise ValueError("Expenses cannot be negative.")
                cash[aid] -= amount
                costs += amount if kind == "fee" else Decimal(0)
                tax_reserve += amount if kind == "tax_reserve" else Decimal(0)
            elif kind in {"trade", "cash_out"}:
                if kind == "trade" and baseline["arm"] == "no_discretionary_change":
                    raise ValueError("Discretionary trades cannot enter the no-change ledger.")
                if kind == "cash_out" and (
                    event.get("mandatory") is not True or key not in quantities
                ):
                    raise ValueError(
                        "Cash-outs require confirmed mandatory corporate actions on existing securities."
                    )
                quantity = (
                    -quantities[key]
                    if kind == "cash_out"
                    else _decimal(event.get("quantity"), "signed quantity")
                )
                price, fee, reserve = (
                    _decimal(event.get(field), field) for field in ("price", "cost", "tax_reserve")
                )
                if (
                    price < 0
                    or (
                        price == 0
                        and (kind != "cash_out" or event.get("worthless_confirmed") is not True)
                    )
                    or fee < 0
                    or reserve < 0
                ):
                    raise ValueError(
                        "Require positive prices (or confirmed worthless cash-outs), nonnegative costs and reserves."
                    )
                executed_at = _aware(event.get("executed_at"), "executed_at")
                if executed_at.tz_convert("America/New_York").date().isoformat() != event["date"]:
                    raise ValueError("Execution timestamp differs from its New York event date.")
                timeline = baseline.get("timeline") or {}
                if kind == "trade" and (
                    not timeline.get("earliest_execution_at")
                    or executed_at
                    < _aware(timeline["earliest_execution_at"], "earliest_execution_at")
                ):
                    raise ValueError(
                        "Discretionary execution cannot precede the frozen eligible execution time."
                    )
                quantities[key] = _decimal(
                    quantities.get(key, Decimal(0)) + quantity, "resulting quantity"
                )
                cash[aid] -= quantity * price + fee + reserve
                costs += fee
                tax_reserve += reserve
            else:
                raise ValueError("Unsupported event type; no unmodeled action was ignored.")
            if cash[aid] < 0 or any(value < 0 for value in quantities.values()):
                raise ValueError("Events cannot create account borrowing or short positions.")
        if not isinstance(end_prices, list):
            raise ValueError("Terminal observations must be a list.")
        price_map = {}
        for row in end_prices:
            if not isinstance(row, dict):
                raise ValueError("Terminal observations must be objects.")
            key = (row.get("security_id"), row.get("currency"))
            if key in price_map:
                raise ValueError("Duplicate terminal security/currency observations are ambiguous.")
            price_map[key] = row
        values, missing = {}, []
        for (aid, sid), quantity in quantities.items():
            row = price_map.get((sid, currencies[aid]))
            if quantity == 0:
                values[(aid, sid)] = Decimal(0)
            elif not row or row.get("date") != end_date:
                missing.append(sid)
            else:
                price = _decimal(row.get("price"), "terminal price")
                if price < 0 or price == 0 and row.get("worthless_confirmed") is not True:
                    raise ValueError(
                        "Zero terminal prices require confirmed worthless/delisted treatment."
                    )
                values[(aid, sid)] = quantity * price
        if missing:
            return {
                "status": "blocked",
                "missing_security_ids": sorted(set(missing)),
                "reason": "Missing terminal values remain in scope, including failed/delisted holdings.",
            }
        initial = sum(navs.values(), Decimal(0))
        ending = sum(cash.values(), Decimal(0)) + sum(values.values(), Decimal(0))
        return {
            "status": "evaluated",
            "arm": baseline["arm"],
            "currency": next(iter(currencies.values())),
            "start_date": start.date().isoformat(),
            "end_date": end_date,
            "ending_nav": str(ending),
            "starting_nav": str(initial),
            "net_external_flows": str(net_flows),
            "investment_gain": str(ending - initial - net_flows),
            "costs": str(costs),
            "tax_reserve": str(tax_reserve),
            "cash": {key: str(value) for key, value in cash.items()},
            "positions": [
                {"account_id": aid, "security_id": sid, "quantity": str(quantity)}
                for (aid, sid), quantity in sorted(quantities.items())
            ],
            "after_tax_return": None,
            "return": None,
            "return_reason": CONVENTIONS["returns"],
            "conventions": deepcopy(CONVENTIONS),
        }


def evaluate_comparators(
    baselines,
    events_by_arm,
    end_prices,
    *,
    end_date,
    coverage_confirmed=False,
    execution_confirmed=None,
):
    """Compare explicit ledgers using identical initial scope and external flows."""
    if not isinstance(baselines, list) or not baselines or not isinstance(events_by_arm, dict):
        raise ValueError("Supply baseline records and event lists keyed by arm.")
    execution_confirmed = {} if execution_confirmed is None else execution_confirmed
    if not isinstance(execution_confirmed, dict) or any(
        type(value) is not bool for value in execution_confirmed.values()
    ):
        raise ValueError("Execution confirmations must be explicit per-arm booleans.")
    arms = [record.get("arm") for record in baselines]
    if (
        len(set(arms)) != len(arms)
        or set(arms) - ARMS
        or set(events_by_arm) - set(arms)
        or "no_discretionary_change" not in arms
    ):
        raise ValueError("Require unique known arms including the no-change baseline.")

    def scope(record):
        accounts = {
            (
                row["account_id"],
                row.get("currency"),
                str(row.get("total_value")),
                str(row.get("cash")),
            )
            for row in record["accounts"]
        }
        positions = {
            (
                row["account_id"],
                row["security_id"],
                str(row.get("quantity")),
                str(row.get("market_value")),
                row.get("currency"),
            )
            for row in record["positions"]
        }
        return record.get("valuation_date"), accounts, positions

    expected_scope, expected_flows = scope(baselines[0]), None
    for baseline in baselines:
        if scope(baseline) != expected_scope:
            raise ValueError("Arms must start from the same dated accounts, units and cash.")
        if baseline["arm"] not in events_by_arm or not isinstance(
            events_by_arm[baseline["arm"]], list
        ):
            raise ValueError(
                "Every arm needs an explicit event list, including an empty list for confirmed no events."
            )
        flows = sorted(
            (
                row.get("account_id"),
                row.get("date"),
                row.get("currency"),
                str(_decimal(row.get("amount"), "external flow")),
            )
            for row in events_by_arm[baseline["arm"]]
            if row.get("kind") == "external_flow"
        )
        if expected_flows is not None and flows != expected_flows:
            raise ValueError(
                "External flows must match by account, date, currency and amount across arms."
            )
        expected_flows = flows
    results = []
    for baseline in baselines:
        try:
            answer = evaluate_ledger(
                baseline,
                events_by_arm[baseline["arm"]],
                end_prices,
                end_date=end_date,
                coverage_confirmed=coverage_confirmed,
                execution_confirmed=execution_confirmed.get(baseline["arm"], False),
            )
        except ValueError as exc:
            answer = {"status": "blocked", "reason": str(exc)}
        results.append({"arm": baseline["arm"], **answer})
    reference = next(row for row in results if row["arm"] == "no_discretionary_change")
    with localcontext() as context:
        context.prec = 1024
        for answer in results:
            answer["investment_gain_difference_vs_no_change"] = (
                str(Decimal(answer["investment_gain"]) - Decimal(reference["investment_gain"]))
                if answer["status"] == reference["status"] == "evaluated"
                else None
            )
    return {
        "status": "evaluated"
        if all(row["status"] == "evaluated" for row in results)
        else "partial"
        if any(row["status"] == "evaluated" for row in results)
        else "blocked",
        "results": results,
        "end_date": end_date,
        "conventions": deepcopy(CONVENTIONS),
        "coverage_confirmed": coverage_confirmed is True,
        "after_tax_performance": "Unavailable; conditional reserves are not final tax liability",
        "statistical_claim": "None. Observed ledger comparisons do not establish predictive superiority.",
    }
