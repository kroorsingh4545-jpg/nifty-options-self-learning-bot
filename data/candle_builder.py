"""Build 1/3/5/15-minute candles from ticks.

* Buckets are aligned to the session open (09:15 IST), so 3-minute candles
  are 09:15, 09:18, ... and 15-minute candles 09:15, 09:30, ...
* Bucketing uses the tick's **exchange time**, never local receipt time.
* Volume is the increase in the cumulative day volume (``volume_today``); the
  first tick of the day only sets the baseline.
* Late (out-of-order) ticks for an already-closed bucket are dropped and
  counted, never used to rewrite a published candle.
* Ticks outside the session are ignored.
* ``flush(now)`` closes candles whose interval has ended even if no further
  tick arrives (call it from a timer).
* Buckets without any tick produce no candle (no synthetic bars).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from core.clock import IST
from core.models import Candle, Tick

DEFAULT_TIMEFRAMES = (1, 3, 5, 15)


@dataclass
class _Working:
    start: datetime
    end: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int
    oi: float | None
    ticks: int

    def update(self, price: float, vol: int, oi: float | None) -> None:
        self.high = max(self.high, price)
        self.low = min(self.low, price)
        self.close = price
        self.volume += vol
        if oi is not None:
            self.oi = oi
        self.ticks += 1

    def to_candle(self, key: str, tf: int, complete: bool) -> Candle:
        return Candle(
            instrument_key=key, timeframe_minutes=tf, start=self.start, end=self.end, open=self.open,
            high=self.high, low=self.low, close=self.close, volume=self.volume, oi=self.oi, tick_count=self.ticks,
            complete=complete,
        )


class CandleBuilder:
    def __init__(
        self,
        timeframes: Iterable[int] = DEFAULT_TIMEFRAMES,
        *,
        session_open: time = time(9, 15),
        session_close: time = time(15, 30),
    ) -> None:
        self.timeframes = tuple(sorted(set(timeframes)))
        if not self.timeframes or any(tf <= 0 for tf in self.timeframes):
            raise ValueError("timeframes must be positive minutes")
        self.session_open = session_open
        self.session_close = session_close
        self._working: dict[tuple[str, int], _Working] = {}
        self._last_volume: dict[str, tuple[date, int]] = {}
        self._listeners: list[Callable[[Candle], None]] = []
        self.ignored_outside_session = 0
        self.dropped_late = 0

    def add_listener(self, fn: Callable[[Candle], None]) -> None:
        self._listeners.append(fn)

    def bucket(self, ts: datetime, tf: int) -> tuple[datetime, datetime]:
        local = ts.astimezone(IST)
        open_dt = datetime.combine(local.date(), self.session_open, tzinfo=IST)
        close_dt = datetime.combine(local.date(), self.session_close, tzinfo=IST)
        minutes = int((local - open_dt).total_seconds() // 60)
        start = open_dt + timedelta(minutes=(minutes // tf) * tf)
        return start, min(start + timedelta(minutes=tf), close_dt)

    def _in_session(self, ts: datetime) -> bool:
        t = ts.astimezone(IST).time()
        return self.session_open <= t < self.session_close

    def _volume_delta(self, tick: Tick) -> int:
        if tick.volume_today is None:
            return 0
        day = tick.exchange_time.astimezone(IST).date()
        prev = self._last_volume.get(tick.instrument_key)
        self._last_volume[tick.instrument_key] = (day, tick.volume_today)
        if prev is None or prev[0] != day:
            return 0
        return max(0, tick.volume_today - prev[1])

    def on_tick(self, tick: Tick) -> list[Candle]:
        """Process a tick; returns (and publishes) candles it completed."""
        ts = tick.exchange_time
        if not self._in_session(ts):
            self.ignored_outside_session += 1
            return []
        vol = self._volume_delta(tick)
        completed: list[Candle] = []
        late = False
        for tf in self.timeframes:
            key = (tick.instrument_key, tf)
            start, end = self.bucket(ts, tf)
            w = self._working.get(key)
            if w is None:
                self._working[key] = _Working(start, end, tick.ltp, tick.ltp, tick.ltp, tick.ltp, vol, tick.oi, 1)
            elif start < w.start:
                late = True
            elif start > w.start:
                completed.append(w.to_candle(tick.instrument_key, tf, complete=True))
                self._working[key] = _Working(start, end, tick.ltp, tick.ltp, tick.ltp, tick.ltp, vol, tick.oi, 1)
            else:
                w.update(tick.ltp, vol, tick.oi)
        if late:
            self.dropped_late += 1
        self._publish(completed)
        return completed

    def on_ticks(self, ticks: Iterable[Tick]) -> list[Candle]:
        out: list[Candle] = []
        for t in ticks:
            out.extend(self.on_tick(t))
        return out

    def flush(self, now: datetime) -> list[Candle]:
        """Close every working candle whose interval ended at or before ``now``."""
        completed = []
        for (key, tf), w in list(self._working.items()):
            if w.end <= now:
                completed.append(w.to_candle(key, tf, complete=True))
                del self._working[(key, tf)]
        completed.sort(key=lambda c: (c.end, c.timeframe_minutes))
        self._publish(completed)
        return completed

    def current(self, instrument_key: str, tf: int) -> Candle | None:
        """The still-forming candle (``complete=False``), if any."""
        w = self._working.get((instrument_key, tf))
        return w.to_candle(instrument_key, tf, complete=False) if w else None

    def _publish(self, candles: list[Candle]) -> None:
        for c in candles:
            for fn in list(self._listeners):
                fn(c)
