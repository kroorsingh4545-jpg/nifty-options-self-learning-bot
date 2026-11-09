"""Today's candles from Upstox: strategies and features start from real history.

At start-up (``backfill_today``) every completed candle of today that a
strategy or the feature engine needs is replayed into it. EMA, VWAP, rolling
volume, VWAP crosses and the opening range are then what they would have been
had the bot run since the open. Replay can never produce a signal, and a setup
already running in the history must break and form again before it can trade.

A candle that started mid-bucket is partial and counts for nothing
(``CandleBuilder``). When one closes, the full version is fetched and replayed
(``on_candle``), together with anything else missing since the last candle the
strategy saw, so a restart or a feed drop leaves no hole in the indicators.

Any failure here is logged and never stops the session: the strategies simply
stay in warm-up for longer. Read-only: this module only calls Upstox's candle API.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from core.models import Candle
from ml.feature_engineering import FeatureEngine
from monitoring.json_logger import get_logger

if TYPE_CHECKING:
    from runtime.bot import TradingBot

log = get_logger(__name__)

Target = tuple[str, int]  # (instrument key, timeframe minutes)

REPAIR_ATTEMPTS = 3


class HistoryLoader:
    def __init__(self, bot: TradingBot, *, repair_delay_seconds: float = 3.0,
                 on_candles: Callable[[Sequence[Candle]], None] | None = None,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        self.bot = bot
        self.repair_delay = repair_delay_seconds
        self._on_candles = on_candles
        self._sleep = sleep
        self._backfilled_until: dict[Target, datetime] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    # ------------------------------------------------------------------ #
    def _feature_engines(self) -> list[FeatureEngine]:
        bot = self.bot
        found = [bot.learning.features if bot.learning is not None else None,
                 bot.inference.features if bot.inference is not None else None,
                 bot.rl.advisor.features if bot.rl is not None else None]
        unique: dict[int, FeatureEngine] = {id(f): f for f in found if f is not None}
        return list(unique.values())

    def targets(self) -> set[Target]:
        """Every (instrument, timeframe) a strategy or the feature engine reads."""
        bot = self.bot
        out = {(s.signal_instrument_key, tf) for s in bot.strategies for tf in s.timeframes_used()}
        if self._feature_engines():
            out |= {(key, 1) for key in bot.base_keys()}
        return out

    def _usable(self, c: Candle, now: datetime) -> bool:
        """A complete, session-aligned candle of today that has already ended."""
        session = self.bot.cfg.settings.market_session
        open_dt = datetime.combine(now.date(), session.market_open, tzinfo=now.tzinfo)
        if not c.complete or c.end > now or c.start.date() != now.date() or c.start < open_dt:
            return False
        return (c.start - open_dt) % timedelta(minutes=c.timeframe_minutes) == timedelta(0)

    async def _fetch(self, key: str, tf: int, now: datetime) -> list[Candle]:
        return [c for c in await self.bot.source.get_intraday_candles(key, tf) if self._usable(c, now)]

    def _apply(self, candles: Sequence[Candle]) -> None:
        for strategy in self.bot.strategies:
            strategy.replay(candles)
        ordered = sorted(candles, key=lambda c: (c.end, c.timeframe_minutes))
        for engine in self._feature_engines():
            for c in ordered:
                engine.on_candle(c)
        if self._on_candles is not None and candles:
            self._on_candles(candles)

    # ------------------------------------------------------------------ #
    async def backfill_today(self) -> dict[str, int]:
        """Replay today's completed candles. Returns candles per target."""
        now = self.bot._now()
        if now.time() <= self.bot.cfg.settings.market_session.market_open:
            return {}
        fetched: list[Candle] = []
        counts: dict[str, int] = {}
        for key, tf in sorted(self.targets()):
            try:
                candles = await self._fetch(key, tf, now)
            except Exception as exc:
                log.warning("candle backfill failed: the strategy warms up from live candles only",
                            extra={"instrument": key, "timeframe": tf, "error": f"{type(exc).__name__}: {exc}"[:200]})
                continue
            fetched += candles
            counts[f"{key}|{tf}"] = len(candles)
            if candles:
                self._backfilled_until[(key, tf)] = candles[-1].end
        self._apply(fetched)
        log.info("candle backfill done", extra={
            "candles": counts, "volume": sum(c.volume for c in fetched),
            "strategies": {s.strategy_id: {"reason": s.last_reason, **s.last_context} for s in self.bot.strategies}})
        return counts

    # ------------------------------------------------------------------ #
    def on_candle(self, candle: Candle) -> None:
        """Candle-builder listener: repair partial candles, report holes."""
        target = (candle.instrument_key, candle.timeframe_minutes)
        if target not in self.targets():
            return
        until = self._backfilled_until.pop(target, None)
        if until is not None and candle.start > until:
            log.warning("gap between the backfill and the first live candle",
                        extra={"instrument": target[0], "timeframe": target[1], "backfilled_until": until.isoformat(),
                               "first_live": candle.start.isoformat()})
        if not candle.complete:
            with contextlib.suppress(RuntimeError):  # no running loop: nothing can be repaired
                task = asyncio.get_running_loop().create_task(self._repair(candle))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)

    async def _repair(self, partial: Candle) -> None:
        key, tf = partial.instrument_key, partial.timeframe_minutes
        for attempt in range(REPAIR_ATTEMPTS):
            await self._sleep(self.repair_delay)
            try:
                candles = await self._fetch(key, tf, self.bot._now())
            except Exception as exc:
                log.warning("candle repair failed", extra={"instrument": key, "timeframe": tf, "attempt": attempt + 1,
                                                           "error": f"{type(exc).__name__}: {exc}"[:200]})
                continue
            if any(c.start == partial.start for c in candles):
                self._apply([c for c in candles if c.start <= partial.start])
                return
        log.warning("partial candle could not be repaired: indicators skip it",
                    extra={"instrument": key, "timeframe": tf, "candle_start": partial.start.isoformat()})

    async def close(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)


__all__ = ["REPAIR_ATTEMPTS", "HistoryLoader"]
