"""D4: the research ORB rule (research/regime/orb.py) must give exactly the
signals of the bot's strategies/orb_options.py on the same bars (SYNTHETIC
random sessions, every signal assumed approved)."""

from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta

import numpy as np
import pytest

from config.loader import load_config
from core.clock import IST
from core.models import Candle
from research.__main__ import sample_settings
from research.config import load_research_config
from research.regime.orb import OrbRule, orb_signals
from strategies.orb_options import OrbOptionsStrategy

CFG = load_config(use_default_env_file=False)
DAY = date(2026, 11, 3)


def session(seed: int, drift: float, vol: float, *, drop: set[int] | None = None) -> dict[time, tuple[float, ...]]:
    rng = np.random.default_rng(seed)
    price, bars = 24500.0, {}
    for m in range(375):
        if drop and m in drop:
            continue
        o = price
        path = o * np.exp(np.cumsum(rng.normal(drift, vol, 6)))
        price = float(path[-1])
        t = (datetime.combine(DAY, time(9, 15)) + timedelta(minutes=m)).time()
        bars[t] = (o, float(max(o, path.max())), float(min(o, path.min())), price)
    return bars


def bot_signals(bars: dict[time, tuple[float, ...]]) -> list[tuple[datetime, str]]:
    strat = OrbOptionsStrategy("orb_options", CFG.strategies.strategies["orb_options"], signal_instrument_key="IDX",
                               now_fn=lambda: datetime.combine(DAY, time(9, 14), tzinfo=IST))
    candles = []
    for t, (o, h, lo, c) in bars.items():
        st = datetime.combine(DAY, t, tzinfo=IST)
        candles.append(Candle(instrument_key="IDX", timeframe_minutes=1, start=st, end=st + timedelta(minutes=1),
                              open=o, high=h, low=lo, close=c))
    buckets: dict[datetime, list[Candle]] = {}
    for c in candles:
        k = int((c.start - datetime.combine(DAY, time(9, 15), tzinfo=IST)).total_seconds() // 300)
        buckets.setdefault(datetime.combine(DAY, time(9, 15), tzinfo=IST) + timedelta(minutes=5 * k), []).append(c)
    for st, cs in buckets.items():
        candles.append(Candle(instrument_key="IDX", timeframe_minutes=5, start=st, end=st + timedelta(minutes=5),
                              open=cs[0].open, high=max(x.high for x in cs), low=min(x.low for x in cs),
                              close=max(cs, key=lambda x: x.start).close))
    out = []
    for c in sorted(candles, key=lambda x: (x.end, x.timeframe_minutes)):  # the bot's replay order
        for sig in strat.on_candle(c):
            strat.on_entry_result(sig, True)  # history assumes approval
            out.append((sig.candle_end, sig.option_type.value))
    return out


def rule() -> OrbRule:
    return sample_settings(CFG, load_research_config()).orb


@pytest.mark.parametrize("seed", range(40))
def test_the_research_orb_rule_gives_the_bot_strategys_signals(seed: int) -> None:
    rng = np.random.default_rng(seed)
    bars = session(seed, drift=float(rng.normal(0, 4e-5)), vol=float(rng.uniform(1e-4, 4e-4)))
    mine = [(s.candle_end, s.option_type) for s in orb_signals(DAY, bars, rule(), IST)]
    assert mine == bot_signals(bars)


def test_an_incomplete_opening_range_gives_no_signal_in_both() -> None:
    bars = session(3, drift=2e-4, vol=2e-4, drop={7})  # 09:22 missing
    assert orb_signals(DAY, bars, rule(), IST) == [] == bot_signals(bars)


def test_some_sessions_do_signal() -> None:
    hits = sum(bool(orb_signals(DAY, session(s, 1.5e-4, 2e-4), rule(), IST)) for s in range(20))
    assert hits >= 5 and not math.isnan(hits)


def test_rule_comes_from_the_bot_configuration() -> None:
    r = rule()
    p = CFG.strategies.strategies["orb_options"].params
    assert (r.buffer_points, r.min_range_points, r.max_range_pct) == (
        p["breakout_buffer_points"], p["min_range_points"], p["max_range_pct"])
    assert (r.window_start, r.window_end) == (time(9, 30), time(11, 0))
