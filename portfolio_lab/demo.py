"""Generate reproducible SYNTHETIC inputs. These are never live market quotes."""

from __future__ import annotations

import sqlite3
from copy import deepcopy
from pathlib import Path

import numpy as np
import pandas as pd

from .config import DEFAULTS, write_config


def create_demo(directory: str | Path) -> Path:
    directory = Path(directory).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    db = directory / "synthetic_holdings.sqlite"
    if db.exists():
        raise FileExistsError(f"Demo database already exists: {db}. Choose a new directory.")
    rng = np.random.default_rng(24)
    as_of = "2026-08-31"
    days = pd.bdate_range("2021-01-01", as_of)
    n = len(days)
    common = rng.normal(0.00028, 0.008, n)
    tech = rng.normal(0.00007, 0.003, n)
    securities, frames, fundamentals, forecasts = [], [], [], []
    symbols = [f"SIM{i:02d}" for i in range(1, 25)]
    price_map = {}
    for i, sid in enumerate(symbols):
        sector = "Technology" if i < 12 else "Industrials"
        dr = common * (0.7 + i / 40) + rng.normal(0.00002 * (i - 10), 0.006 + (i % 4) * 0.001, n)
        if i < 12:
            dr += tech
        price = (40 + 3 * i) * np.exp(np.cumsum(dr))
        price_map[sid] = price
        market_cap = 12e9 + i * 3e9
        securities.append(
            dict(
                security_id=sid,
                ticker=sid,
                issuer_id=sid,
                name=f"Simulated Company {i + 1:02d}",
                sector=sector,
                instrument_type="equity",
                domicile="US",
                equity_type="ordinary_common",
                currency="USD",
                cik=None,
                eligible=True,
                market_cap=market_cap,
                market_cap_as_of=as_of,
                market_cap_available_at="2026-08-31T20:00:00Z",
                market_cap_received_at="2026-08-31T20:00:00Z",
                exchange="SIMULATED",
            )
        )
        frames.append(
            pd.DataFrame(
                dict(
                    date=days.strftime("%Y-%m-%d"),
                    security_id=sid,
                    close=price,
                    adjusted_close=price,
                    volume=np.repeat(2_000_000, n),
                    currency="USD",
                    available_at=days.strftime("%Y-%m-%dT20:00:00Z"),
                    received_at=days.strftime("%Y-%m-%dT20:00:00Z"),
                    source_id="synthetic_prices",
                )
            )
        )
        assets = 15e9 + i * 2e9
        income = assets * (0.04 + i * 0.004)
        cfo = income * (1.05 + (i % 5) * 0.12)
        fundamentals.append(
            dict(
                security_id=sid,
                period_end="2026-06-30",
                available_at="2026-08-10T12:00:00Z",
                received_at="2026-08-10T12:00:00Z",
                revenue=assets * 0.95,
                gross_profit=assets * (0.15 + i * 0.008),
                operating_income=income * 1.3,
                net_income=income,
                income_common=income,
                earnings_definition="common_shareholders",
                operating_cash_flow=cfo,
                capex=cfo * 0.25,
                assets=assets,
                assets_begin=assets / 1.06,
                debt=assets * 0.2,
                cash=assets * 0.12,
                source_id="synthetic_statements",
                currency="USD",
            )
        )
        alpha = (i - 10) * 0.003
        for h in [6, 12, 18]:
            for label, ret, prob in [
                ("Adverse", -0.22 + alpha, 0.25),
                ("Central", 0.09 + alpha, 0.50),
                ("Favorable", 0.28 + alpha, 0.25),
            ]:
                forecasts.append(
                    dict(
                        security_id=sid,
                        scenario=label,
                        horizon_months=h,
                        return_value=(1 + ret) ** (h / 12) - 1,
                        probability=prob,
                        basis="subjective",
                        source="Synthetic joint macro scenario, not a real forecast",
                        forecast_date=as_of,
                        calibration_id=None,
                    )
                )
    # One genuine structural test: two separate share classes of the same simulated issuer.
    dual = dict(
        securities[0], security_id="SIM01B", ticker="SIM01B", name="Simulated Company 01 Class B"
    )
    securities.append(dual)
    dual_prices = frames[0].copy()
    dual_prices["security_id"] = "SIM01B"
    dual_prices["close"] *= 0.997
    dual_prices["adjusted_close"] *= 0.997
    frames.append(dual_prices)
    price_map["SIM01B"] = dual_prices["close"].to_numpy()
    fundamentals.append(dict(fundamentals[0], security_id="SIM01B"))
    forecasts.extend(
        [dict(f, security_id="SIM01B") for f in list(forecasts) if f["security_id"] == "SIM01"]
    )
    benchmark_price = 100 * np.exp(np.cumsum(common))
    securities.append(
        dict(
            security_id="SIMETF",
            ticker="SIMETF",
            issuer_id="SIM_FUND",
            name="Simulated Broad Equity Fund",
            sector="Fund",
            instrument_type="etf",
            currency="USD",
            cik=None,
            eligible=True,
            market_cap=None,
            exchange="SIMULATED",
        )
    )
    frames.append(
        pd.DataFrame(
            dict(
                date=days.strftime("%Y-%m-%d"),
                security_id="SIMETF",
                close=benchmark_price,
                adjusted_close=benchmark_price,
                volume=5_000_000,
                currency="USD",
                available_at=days.strftime("%Y-%m-%dT20:00:00Z"),
                received_at=days.strftime("%Y-%m-%dT20:00:00Z"),
                source_id="synthetic_prices",
            )
        )
    )
    price_map["SIMETF"] = benchmark_price
    for h in [6, 12, 18]:
        for label, ret, prob in [
            ("Adverse", -0.25, 0.25),
            ("Central", 0.08, 0.5),
            ("Favorable", 0.24, 0.25),
        ]:
            forecasts.append(
                dict(
                    security_id="SIMETF",
                    scenario=label,
                    horizon_months=h,
                    return_value=(1 + ret) ** (h / 12) - 1,
                    probability=prob,
                    basis="subjective",
                    source="Synthetic benchmark scenario",
                    forecast_date=as_of,
                    calibration_id=None,
                )
            )
    accounts = pd.DataFrame(
        [
            dict(
                account_id="Retirement_A",
                account_type="retirement",
                currency="USD",
                total_value=100_000.0,
                cash=10_000.0,
                complete=True,
                tax_rate=None,
            ),
            dict(
                account_id="Retirement_B",
                account_type="retirement",
                currency="USD",
                total_value=60_000.0,
                cash=6_000.0,
                complete=True,
                tax_rate=None,
            ),
        ]
    )
    positions = []
    allocations = {
        "Retirement_A": {
            "SIM01": 12000,
            "SIM01B": 3000,
            "SIM06": 14000,
            "SIM12": 16000,
            "SIM18": 18000,
            "SIMETF": 27000,
        },
        "Retirement_B": {"SIM03": 10000, "SIM09": 10000, "SIM16": 14000, "SIMETF": 20000},
    }
    for account, holdings in allocations.items():
        total = float(accounts.loc[accounts.account_id == account, "total_value"].iloc[0])
        for sid, mv in holdings.items():
            price = float(price_map[sid][-1])
            positions.append(
                dict(
                    account_id=account,
                    security_id=sid,
                    quantity=mv / price,
                    price=price,
                    market_value=float(mv),
                    currency="USD",
                    reported_weight=mv / total,
                    valuation_date=as_of,
                )
            )
    with sqlite3.connect(db) as conn:
        pd.DataFrame(positions).to_sql("positions", conn, index=False)
        accounts.to_sql("accounts", conn, index=False)
        pd.DataFrame(securities).to_sql("securities", conn, index=False)
    pd.concat(frames, ignore_index=True).to_csv(directory / "prices.csv", index=False)
    pd.DataFrame(fundamentals).to_csv(directory / "fundamentals.csv", index=False)
    pd.DataFrame(forecasts).to_csv(directory / "forecasts.csv", index=False)
    pd.DataFrame(
        [
            dict(
                fund_id="SIMETF",
                issuer_id=sid,
                weight=1 / 24,
                holdings_date=as_of,
                available_at="2026-08-31T12:00:00Z",
                source_id="synthetic_fund_holdings",
            )
            for sid in symbols
        ]
    ).to_csv(directory / "fund_holdings.csv", index=False)
    macro = []
    months = pd.date_range("2022-01-01", "2026-08-01", freq="MS")
    for name, units, base, wave in [
        ("DGS10", "Percent", 3.5, 0.8),
        ("UNRATE", "Percent", 4.0, 0.35),
        ("CPIAUCSL", "Index 1982-1984=100", 290.0, 8.0),
        ("NFCI", "Index", -0.15, 0.4),
        ("T10Y2Y", "Percentage points", 0.1, 0.7),
    ]:
        for i, day in enumerate(months):
            value = base + wave * np.sin(i / 8) + (i * 0.4 if name == "CPIAUCSL" else 0)
            macro.append(
                dict(
                    series_id=name,
                    date=day.strftime("%Y-%m-%d"),
                    value=float(value),
                    vintage_date=as_of,
                    received_at="2026-08-31T12:00:00Z",
                    units=units,
                    source_id="synthetic_macro",
                )
            )
    pd.DataFrame(macro).to_csv(directory / "macro.csv", index=False)
    c = deepcopy(DEFAULTS)
    c["source"]["path"] = db.name
    c["research"] = {"path": "research.sqlite", "output_dir": "reports"}
    c["data"].update(
        mode="demo",
        sec_enabled=False,
        fred_enabled=False,
        prices_csv="prices.csv",
        fundamentals_csv="fundamentals.csv",
        macro_csv="macro.csv",
        forecasts_csv="forecasts.csv",
        fund_holdings_csv="fund_holdings.csv",
    )
    c["mandate"].update(
        confirmed=True,
        base_currency="USD",
        benchmark_id="SIMETF",
        issuer_cap=0.25,
        sector_cap=0.75,
        min_cash_weight=0.05,
        max_turnover=0.40,
        max_volatility=0.30,
        max_stress_loss=0.35,
        account_permissions={a: list(price_map) for a in allocations},
    )
    c["signals"]["min_sector_size"] = 10
    c["mandate"]["dealing_rules"] = {
        aid: {
            sid: {
                "fractional_shares": True,
                "quantity_increment": 0.000001,
                "dealing_allowed": True,
            }
            for sid in price_map
        }
        for aid in allocations
    }
    c["allocation"].update(
        residual_security_id="SIMETF",
        cash_return=0.0,
        active_sleeve_weight=0.20,
        sleeve_budget_basis="account_nav",
        min_trade_value=100,
        min_trade_weight=0.001,
    )
    # Explicitly assign a simulated sleeve from held positions; this is not inferred for live data.
    c["allocation"]["sleeve_membership"] = {
        aid: {
            sid: value
            / float(accounts.loc[accounts.account_id == aid, "total_value"].iloc[0])
            * 0.25
            for sid, value in holdings.items()
        }
        for aid, holdings in allocations.items()
    }
    write_config(directory / "config.json", c)
    (directory / "SYNTHETIC_DATA.txt").write_text(
        "All positions, prices, fundamentals, macro observations, and forecasts in this demo are simulated. They are not the owner's portfolio or historical market data. Synthetic price series contain no dividends or corporate actions.\n"
    )
    return directory / "config.json"
