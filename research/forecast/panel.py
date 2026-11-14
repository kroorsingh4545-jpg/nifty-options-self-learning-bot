"""Daily panel of NIFTY and India VIX from the research database.

One row per observed NIFTY session (special sessions included, flagged): the
index OHLC, the India VIX close of the same date (NaN when Upstox has none or
the candle was invalid), and the 5-minute intraday realised variance from the
1-minute candles (NaN when a session has too few minutes).
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date

import numpy as np
import pandas as pd

from research.store import ResearchStore
from volatility.realised import intraday_realised_var


def _daily(store: ResearchStore, key: str) -> pd.DataFrame:
    df = pd.read_sql("SELECT session_date, open, high, low, close FROM daily_candles"
                     " WHERE instrument_key = ? AND complete = 1 ORDER BY session_date", store.con, params=(key,))
    df["session_date"] = pd.to_datetime(df["session_date"]).dt.date
    return df


def intraday_rv(store: ResearchStore, key: str, *, step: int, min_minutes: int) -> pd.DataFrame:
    """Per session: 5-minute (``step``) intraday realised variance and the
    number of 1-minute candles."""
    m = pd.read_sql("SELECT substr(start_time, 1, 10) AS d, start_time, open, close FROM minute_candles"
                    " WHERE instrument_key = ? AND interval_minutes = 1 AND complete = 1 ORDER BY start_time",
                    store.con, params=(key,))
    rows = []
    for d, g in m.groupby("d", sort=True):
        n = len(g)
        rv = intraday_realised_var(float(g["open"].iloc[0]), g["close"].to_numpy(), step=step) \
            if n >= min_minutes else np.nan
        rows.append((date.fromisoformat(str(d)), rv, n))
    return pd.DataFrame(rows, columns=["session_date", "rv5", "n_minutes"])


def load_panel(store: ResearchStore, nifty_key: str, vix_key: str, *, special: Iterable[date], step: int,
               min_minutes: int) -> pd.DataFrame:
    nifty = _daily(store, nifty_key).set_axis(["session_date", "o", "h", "l", "c"], axis=1)
    vix = _daily(store, vix_key)[["session_date", "close"]].set_axis(["session_date", "vix"], axis=1)
    rv = intraday_rv(store, nifty_key, step=step, min_minutes=min_minutes)
    panel = nifty.merge(vix, on="session_date", how="left").merge(rv, on="session_date", how="left")
    specials = set(special)
    panel["special"] = panel["session_date"].map(lambda d: d in specials)
    return panel.sort_values("session_date").reset_index(drop=True)


__all__ = ["intraday_rv", "load_panel"]
