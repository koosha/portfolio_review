"""Synthetic security rows shared by the research test modules.

One US-listed ordinary equity per row, complete enough for the scenario, universe and
research builders to accept it, with every field overridable by keyword so a case can
state the one attribute it is about.
"""

import pandas as pd


def securities(*rows) -> pd.DataFrame:
    return pd.DataFrame(list(rows))


def security(sid, **extra) -> dict:
    return {
        "security_id": sid,
        "ticker": sid,
        "name": f"{sid} Inc",
        "sector": "Technology",
        "currency": "USD",
        "instrument_type": "equity",
        **extra,
    }
