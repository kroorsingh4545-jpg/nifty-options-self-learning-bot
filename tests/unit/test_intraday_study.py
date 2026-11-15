"""D3: the intraday IV-vs-realised history study (research/forecast/intraday.py)
on SYNTHETIC minute data with a known true variance."""

from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta

import numpy as np
import pandas as pd
import pytest

from core.clock import IST
from research.forecast.intraday import minute_closes_by_day, render_text, study, trailing_overnight_shares, windows

SIGMA = 0.15  # annualised on TRADING time: per-minute variance SIGMA^2 / (252 x 375)


def synthetic(days: int, seed: int = 5) -> tuple[pd.DataFrame, pd.DataFrame]:
    rng = np.random.default_rng(seed)
    sd = SIGMA / math.sqrt(252 * 375)
    rows, vix = [], []
    d, price = date(2024, 1, 1), 20000.0
    while len({r[0][:10] for r in rows}) < days:
        if d.weekday() < 5:
            for m in range(375):
                st = datetime.combine(d, time(9, 15), tzinfo=IST) + timedelta(minutes=m)
                price *= math.exp(rng.normal(0.0, sd))
                rows.append((st.isoformat(), price))
                vix.append((st.isoformat(), SIGMA * 100))
        d += timedelta(days=1)
    return (pd.DataFrame(rows, columns=["start_time", "close"]), pd.DataFrame(vix, columns=["start_time", "close"]))


def test_with_trading_time_variance_the_trading_clock_is_right_and_calendar_understates() -> None:
    nifty, vix = synthetic(220)
    df = windows(minute_closes_by_day(nifty), minute_closes_by_day(vix), {}, checkpoints=[time(10, 0), time(13, 0)],
                 horizons=[30], default_share=0.0)
    rep = study(df)
    m = rep["by_horizon"]["30"]
    assert m["trading"]["variance_ratio"] == pytest.approx(1.0, abs=0.08)
    assert m["business"]["variance_ratio"] == pytest.approx(m["trading"]["variance_ratio"], rel=1e-9)  # w = 0
    assert m["calendar"]["variance_ratio"] == pytest.approx(5.56 * m["trading"]["variance_ratio"], rel=1e-3)
    assert m["trading"]["hit_1sd"]["rate"] == pytest.approx(0.683, abs=0.05)
    assert m["trading"]["qlike"] < m["calendar"]["qlike"]  # QLIKE picks the right clock
    assert "trading" in "\n".join(render_text(rep))


def test_an_overnight_share_scales_the_business_clock() -> None:
    nifty, vix = synthetic(30)
    df = windows(minute_closes_by_day(nifty), minute_closes_by_day(vix), {}, checkpoints=[time(10, 0)],
                 horizons=[15], default_share=0.4)
    assert (df["business"] / df["trading"]).to_numpy() == pytest.approx(0.6)


def test_a_window_with_a_missing_minute_is_skipped() -> None:
    nifty, vix = synthetic(2)
    first = nifty["start_time"].str.startswith(nifty["start_time"].iloc[0][:10])
    gap = nifty[~(first & nifty["start_time"].str.contains("T10:10:"))]
    df = windows(minute_closes_by_day(gap), minute_closes_by_day(vix), {}, checkpoints=[time(10, 0)], horizons=[30],
                 default_share=0.0)
    assert len(df) == 1  # the day with the hole has no 10:00 + 30 min window


def test_checkpoint_price_is_the_close_of_the_minute_ending_at_the_checkpoint() -> None:
    nifty, vix = synthetic(1)
    closes = minute_closes_by_day(nifty)
    (day,) = closes
    df = windows(closes, minute_closes_by_day(vix), {}, checkpoints=[time(10, 0)], horizons=[1], default_share=0.0)
    expect = math.log(closes[day][time(10, 1)] / closes[day][time(10, 0)])
    assert df["move"].iloc[0] == pytest.approx(expect)  # 10:00 close -> 10:01 close: nothing after 10:00 used


def test_trailing_overnight_shares_use_only_earlier_sessions() -> None:
    days = [date(2024, 1, 1) + timedelta(days=i) for i in range(120)]
    rng = np.random.default_rng(1)
    c = 20000 * np.exp(np.cumsum(rng.normal(0, 0.01, len(days))))
    o = c * np.exp(rng.normal(0, 0.006, len(days)))
    daily = pd.DataFrame({"session_date": days, "o": o, "c": c})
    a = trailing_overnight_shares(daily, lookback=60, reliable_from=days[0], default=0.35)
    changed = daily.copy()
    changed.loc[100:, "o"] *= 1.05  # later sessions change ...
    b = trailing_overnight_shares(changed, lookback=60, reliable_from=days[0], default=0.35)
    assert all(a[d] == b[d] for d in days[:101])  # ... earlier shares do not (day 100 uses days before it)
    assert a[days[5]] == 0.35  # too little history: the default
    assert 0.0 < a[days[80]] < 1.0


def test_an_empty_history_reports_nothing() -> None:
    assert study(pd.DataFrame()) == {"windows": 0}
    assert render_text({"windows": 0}) == ["no complete intraday windows in the stored history"]
