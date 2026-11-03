"""SYNTHETIC market data for tests only — NOT real NIFTY prices.

Deterministic generators for candles and ticks with known, hand-designed
shapes (opening range + breakout, trends, chop, low volume) so strategy and
engine behaviour can be asserted exactly.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import datetime, timedelta

from core.clock import IST
from core.models import Candle, Tick

DAY = (2026, 11, 3)
INDEX = "NSE_INDEX|Nifty 50"
FUT = "NSE_FO|NIFTYFUT-SYNTH"


def at(h: int, m: int, s: int = 0, day: tuple[int, int, int] = DAY) -> datetime:
    return datetime(*day, h, m, s, tzinfo=IST)


def candle(key: str, tf: int, start: datetime, o: float, h: float, low: float, c: float, v: int = 0) -> Candle:
    return Candle(instrument_key=key, timeframe_minutes=tf, start=start, end=start + timedelta(minutes=tf), open=o,
                  high=h, low=low, close=c, volume=v)


def opening_range_candles(key: str = INDEX, high: float = 24520.0, low: float = 24480.0,
                          day: tuple[int, int, int] = DAY) -> list[Candle]:
    """Fifteen 1-minute candles 09:15–09:30 whose extremes are exactly high/low."""
    out = []
    mid = (high + low) / 2
    for i in range(15):
        start = at(9, 15, day=day) + timedelta(minutes=i)
        # extremes exactly at high/low; every other candle strictly inside them
        hi = high if i == 4 else min(mid + 5, high)
        lo = low if i == 9 else max(mid - 5, low)
        close = min(max(mid + (1 if i % 2 else -1), lo), hi)
        out.append(candle(key, 1, start, mid, hi, lo, close))
    return out


def five_min(key: str, start: datetime, closes: Sequence[float], vols: Sequence[int] | None = None) -> list[Candle]:
    """Consecutive 5-minute candles with the given closes (open = previous close)."""
    out = []
    prev = closes[0]
    for i, c in enumerate(closes):
        s = start + timedelta(minutes=5 * i)
        out.append(candle(key, 5, s, prev, max(prev, c) + 2, min(prev, c) - 2, c, vols[i] if vols else 0))
        prev = c
    return out


def tick(key: str, ltp: float, ts: datetime, *, spread: float = 0.1, vtt: int | None = None,
         oi: float | None = None, qty: int = 1500) -> Tick:
    return Tick(instrument_key=key, ltp=ltp, exchange_time=ts, received_at=ts, bid=round(ltp - spread / 2, 2),
                ask=round(ltp + spread / 2, 2), bid_qty=qty, ask_qty=qty, volume_today=vtt, oi=oi)


def index_path(points: Sequence[tuple[datetime, float]], step_seconds: int = 10) -> Iterator[tuple[datetime, float]]:
    """Linear interpolation between (time, price) waypoints at ``step_seconds``."""
    for (t0, p0), (t1, p1) in zip(points, points[1:], strict=False):
        n = int((t1 - t0).total_seconds() // step_seconds)
        for k in range(n):
            yield t0 + timedelta(seconds=k * step_seconds), p0 + (p1 - p0) * k / n
    yield points[-1]
