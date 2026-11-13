"""Earlier candles from Upstox: strategies and features start from real history.

Start-up, in this order:

* ``seed_prior_session``: the previous trading day's candles seed anything that
  carries across days (the 20 EMA), so it is ready at 09:15. VWAP is not seeded;
  it resets every day. If the seed is unavailable the EMA warms up from today's
  candles instead, and no signal can fire until it has.
* ``backfill_today``: every completed candle of today that a strategy or the
  feature engine needs is replayed into it, so EMA, VWAP, rolling volume, VWAP
  crosses and the opening range are what they would have been had the bot run
  since the open. Replay can never produce a signal, and a setup already running
  in the history must break and form again before it can trade.

A candle that started mid-bucket is partial and counts for nothing
(``CandleBuilder``). When one closes, its full version is fetched and replayed
(``on_candle``), together with anything else missing since the open.

Missed candles are never given up early. A candle Upstox has not published yet
(the minute the bot started in, the newest one at start-up) or one lost to a
feed gap stays *pending*, and is fetched again each time a later candle of that
series closes, until it arrives (at most ``MAX_FILL_ATTEMPTS`` times). A
strategy that has already moved past it is then rebuilt: back to its
start-of-day checkpoint (taken before the day's first candle, so a seeded EMA
is kept) and every candle of today replayed in order, the late one in its
place. So the opening range gets all 15 minutes even if the bot started inside
one of them. A rebuild never signals, and today's approved entries are kept.

The fill never lifts a block: a hole in the series a strategy's indicators run
on still blocks it for the day; the fill only makes the data whole (indicators,
features, the recording).

Never trade on partial data: if a strategy's history cannot be loaded complete
(fetch failed, candles missing, a partial candle cannot be repaired, a gap
between candles, or replaying raises), its new entries are blocked for the rest
of the day (``DailyLossGuard.block_entries_today``). Exits are never affected. A
restart re-checks the data, so one that gets complete history can trade again.
Replay is all or nothing, so indicators are never left part-updated.

Read-only: this module only calls Upstox's candle APIs.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Coroutine, Iterable, Sequence
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any

from core.models import Candle
from ml.feature_engineering import FeatureEngine
from monitoring.json_logger import get_logger
from strategies.base_strategy import BaseStrategy

if TYPE_CHECKING:
    from runtime.bot import TradingBot

log = get_logger(__name__)

Target = tuple[str, int]  # (instrument key, timeframe minutes)

REPAIR_ATTEMPTS = 3
#: fetches of a still-missing candle, one per later candle of the same series
MAX_FILL_ATTEMPTS = 30
#: earlier trading days tried for the seed (an unlisted holiday has no candles)
PRIOR_DAY_ATTEMPTS = 3


class HistoryLoader:
    def __init__(self, bot: TradingBot, *, repair_delay_seconds: float = 3.0,
                 on_candles: Callable[[Sequence[Candle]], None] | None = None,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        self.bot = bot
        self.repair_delay = repair_delay_seconds
        self._on_candles = on_candles
        self._sleep = sleep
        self._last_seen: dict[Target, datetime] = {}  # end of the newest candle of each target
        self._tasks: set[asyncio.Task[None]] = set()
        # today's complete candles per target (backfill, live, repaired), for rebuilds
        self._day: date | None = None
        self._today: dict[Target, dict[datetime, Candle]] = {}
        self._pending: dict[Target, set[datetime]] = {}  # starts of candles still missing
        self._fill_attempts: dict[Target, int] = {}
        self._filling: set[Target] = set()
        self._checkpoints: dict[str, tuple[date, dict[str, Any]]] = {}  # strategy -> start-of-day state

    # ------------------------------------------------------------------ #
    # What is needed, and who depends on it
    # ------------------------------------------------------------------ #
    def _feature_engines(self) -> list[FeatureEngine]:
        bot = self.bot
        found = [bot.learning.features if bot.learning is not None else None,
                 bot.inference.features if bot.inference is not None else None,
                 bot.rl.advisor.features if bot.rl is not None else None]
        unique: dict[int, FeatureEngine] = {id(f): f for f in found if f is not None}
        return list(unique.values())

    @staticmethod
    def _needs(strategy: BaseStrategy) -> set[Target]:
        return {(strategy.signal_instrument_key, tf) for tf in strategy.timeframes_used()}

    def targets(self) -> set[Target]:
        """Every (instrument, timeframe) a strategy or the feature engine reads."""
        bot = self.bot
        out = {t for s in bot.strategies for t in self._needs(s)}
        if self._feature_engines():
            out |= {(key, 1) for key in bot.base_keys()}
        return out

    def _block(self, strategy: BaseStrategy, reason: str) -> None:
        self.bot.stack.guard.block_entries_today(strategy.strategy_id, reason)

    def _block_trading_on(self, target: Target, reason: str) -> None:
        """Block strategies whose indicators run on this exact candle series. (A
        hole in ORB's 1-minute series is handled by its own range check.)"""
        for s in self.bot.strategies:
            if (s.signal_instrument_key, s.timeframe) == target:
                self._block(s, reason)

    # ------------------------------------------------------------------ #
    # Reading and checking candles
    # ------------------------------------------------------------------ #
    def _session_bounds(self, day: date, tz: object) -> tuple[datetime, datetime]:
        session = self.bot.cfg.settings.market_session
        return (datetime.combine(day, session.market_open, tzinfo=tz),  # type: ignore[arg-type]
                datetime.combine(day, session.market_close, tzinfo=tz))  # type: ignore[arg-type]

    def _usable(self, c: Candle, day: date, now: datetime) -> bool:
        """A complete, session-aligned candle of ``day`` that has already ended."""
        open_dt, close_dt = self._session_bounds(day, now.tzinfo)
        if not c.complete or c.end > now or c.start.date() != day or not open_dt <= c.start < close_dt:
            return False
        return (c.start - open_dt) % timedelta(minutes=c.timeframe_minutes) == timedelta(0)

    def _missing(self, candles: Iterable[Candle], tf: int, until: datetime) -> list[datetime]:
        """Starts of the candles that should have ended by ``until`` but are absent."""
        open_dt, close_dt = self._session_bounds(until.date(), until.tzinfo)
        count = int((min(until, close_dt) - open_dt).total_seconds() // (tf * 60))
        have = {c.start for c in candles}
        expected = (open_dt + timedelta(minutes=tf * i) for i in range(max(count, 0)))
        return [start for start in expected if start not in have]

    async def _fetch(self, key: str, tf: int, now: datetime) -> list[Candle]:
        return [c for c in await self.bot.source.get_intraday_candles(key, tf) if self._usable(c, now.date(), now)]

    async def _fetch_complete(self, key: str, tf: int) -> tuple[list[Candle], str | None, bool]:
        """Today's candles, what is wrong with them (None = complete), and
        whether that is only the newest candle not published yet. Retries an
        error or a just-ended candle."""
        candles: list[Candle] = []
        problem: str | None = None
        newest_only = False
        for attempt in range(REPAIR_ATTEMPTS):
            if attempt:
                await self._sleep(self.repair_delay)
            now = self.bot._now()
            newest_only = False
            try:
                candles = await self._fetch(key, tf, now)
            except Exception as exc:
                problem = f"{type(exc).__name__}: {exc}"[:200]
                continue
            missing = self._missing(candles, tf, now)
            if not missing:
                return candles, None, False
            problem = f"{len(missing)} candles missing, first {missing[0]:%H:%M}"
            newest_only = len(missing) == 1 and now - missing[0] < timedelta(minutes=2 * tf)
            if not newest_only:
                break  # a real hole: waiting will not fill it
        return candles, problem, newest_only

    # ------------------------------------------------------------------ #
    # Applying candles
    # ------------------------------------------------------------------ #
    def _replay(self, strategy: BaseStrategy, candles: Sequence[Candle]) -> None:
        try:
            strategy.replay(candles)
        except Exception as exc:  # the strategy rolled itself back
            log.exception("candle replay failed", extra={"strategy_id": strategy.strategy_id})
            self._block(strategy, f"candle replay failed ({type(exc).__name__})")

    def _feed_features(self, candles: Sequence[Candle]) -> None:
        ordered = sorted(candles, key=lambda c: (c.end, c.timeframe_minutes))
        for engine in self._feature_engines():
            try:
                for c in ordered:
                    engine.on_candle(c)
            except Exception:
                log.exception("feature engine replay failed (trading is unaffected)")

    def _record(self, candles: Sequence[Candle]) -> None:
        if self._on_candles is not None and candles:
            self._on_candles(candles)

    # ------------------------------------------------------------------ #
    # Late candles: remember today, rebuild what moved past a hole
    # ------------------------------------------------------------------ #
    def _roll(self, day: date) -> None:
        if self._day != day:
            self._day = day
            self._today.clear()
            self._pending.clear()
            self._fill_attempts.clear()

    def _keep(self, candles: Iterable[Candle]) -> None:
        """Remember today's complete candles; one that was pending no longer is."""
        for c in candles:
            day = c.start.date()
            if not c.complete or (self._day is not None and day < self._day):
                continue
            self._roll(day)
            target = (c.instrument_key, c.timeframe_minutes)
            self._today.setdefault(target, {})[c.start] = c
            waiting = self._pending.get(target)
            if waiting is not None:
                waiting.discard(c.start)
                if not waiting:
                    del self._pending[target]
                    self._fill_attempts.pop(target, None)

    def _mark_missing(self, target: Target, starts: Iterable[datetime]) -> None:
        held = self._today.get(target, {})
        new = {s for s in starts if s not in held}
        if new:
            day = min(new).date()
            if self._day is not None and day < self._day:
                return
            self._roll(day)
            self._pending.setdefault(target, set()).update(new)

    def _checkpoint_day_start(self, day: date) -> None:
        """Remember each strategy's state from before ``day``'s first candle."""
        for s in self.bot.strategies:
            if not s.has_candles_of(day):
                self._checkpoints[s.strategy_id] = (day, s.checkpoint())

    def _rebuild(self, strategy: BaseStrategy) -> None:
        sid = strategy.strategy_id
        saved = self._checkpoints.get(sid)
        if saved is None or saved[0] != self._day:
            log.warning("late candles filled, but the strategy has no start-of-day state to rebuild from",
                        extra={"strategy_id": sid})
            return
        candles = [c for t in sorted(self._needs(strategy)) for c in self._today.get(t, {}).values()]
        try:
            used = strategy.rebuild(saved[1], candles)
        except Exception as exc:  # the strategy rolled itself back
            log.exception("strategy rebuild failed", extra={"strategy_id": sid})
            self._block(strategy, f"rebuild with late candles failed ({type(exc).__name__})")
            return
        log.warning("strategy rebuilt with the late candles", extra={
            "strategy_id": sid, "candles": used, "reason": strategy.last_reason, **strategy.last_context})

    def _fill_features(self, candles: Sequence[Candle]) -> None:
        for engine in self._feature_engines():
            try:
                for c in candles:
                    engine.fill_candle(c)
            except Exception:
                log.exception("feature engine fill failed (trading is unaffected)")

    def _apply_late(self, target: Target, candles: Sequence[Candle]) -> None:
        """Use candles that were missing: replayed where they come next, or the
        strategy is rebuilt where it has already moved past them (or has seen
        none of this series yet, e.g. its backfill was refused: replaying the
        late candles alone would leave out the rest of today)."""
        ordered = sorted(candles, key=lambda c: c.start)
        self._keep(ordered)
        for s in self.bot.strategies:
            if target not in self._needs(s):
                continue
            seen = s.seen_until(target[1])
            if seen is not None and seen < ordered[0].end:
                self._replay(s, ordered)
            else:
                self._rebuild(s)
        self._fill_features(ordered)
        self._record(ordered)

    # ------------------------------------------------------------------ #
    # Previous session
    # ------------------------------------------------------------------ #
    async def _prior_session(self, key: str, tf: int, needed: int, now: datetime) -> list[Candle]:
        session = self.bot.cfg.settings.market_session
        day = now.date()
        tried = 0
        for _ in range(15):
            day -= timedelta(days=1)
            if not session.is_trading_day(day):
                continue
            rows = await self.bot.source.get_historical_candles(key, tf, day, day)
            usable = [c for c in rows if self._usable(c, day, now)]
            if len(usable) >= needed:
                return usable
            tried += 1
            if tried >= PRIOR_DAY_ATTEMPTS:
                break
        return []

    async def seed_prior_session(self) -> dict[str, int]:
        """Seed from the previous trading day whatever needs it. Safe to repeat:
        a strategy that is already seeded asks for nothing. Returns candles per strategy."""
        now = self.bot._now()
        seeded: dict[str, int] = {}
        for s in self.bot.strategies:
            needed = s.prior_session_bars()
            if needed <= 0:
                continue
            try:
                candles = await self._prior_session(s.signal_instrument_key, s.timeframe, needed, now)
            except Exception as exc:
                candles = []
                log.warning("previous-session candles could not be fetched",
                            extra={"strategy_id": s.strategy_id, "error": f"{type(exc).__name__}: {exc}"[:200]})
            if not candles:
                log.warning("no previous-session seed: the strategy warms up from today's candles",
                            extra={"strategy_id": s.strategy_id})
                continue
            try:
                seeded[s.strategy_id] = s.seed_prior_session(candles)
            except Exception as exc:  # the strategy rolled itself back
                log.exception("previous-session seed failed", extra={"strategy_id": s.strategy_id})
                self._block(s, f"previous-session seed failed ({type(exc).__name__})")
        self._checkpoint_day_start(now.date())
        log.info("previous-session seed done", extra={"candles": seeded})
        return seeded

    # ------------------------------------------------------------------ #
    # Today
    # ------------------------------------------------------------------ #
    async def backfill_today(self) -> dict[str, int]:
        """Replay today's completed candles. Returns candles per target."""
        now = self.bot._now()
        session = self.bot.cfg.settings.market_session
        if not session.is_trading_day(now.date()) or not session.market_open < now.time() < session.market_close:
            return {}
        self._checkpoint_day_start(now.date())
        self._roll(now.date())
        fetched: dict[Target, list[Candle]] = {}
        problems: dict[Target, str] = {}
        just_late: set[Target] = set()
        for target in sorted(self.targets()):
            fetched[target], problem, newest_only = await self._fetch_complete(*target)
            self._keep(fetched[target])
            # fetched again when a later candle closes (a block below still lasts the day)
            self._mark_missing(target, self._missing(fetched[target], target[1], self.bot._now()))
            if newest_only:
                just_late.add(target)
            if problem is not None:
                problems[target] = problem
                log.warning("candle backfill incomplete",
                            extra={"instrument": target[0], "timeframe": target[1], "problem": problem})
            if fetched[target]:
                self._last_seen[target] = fetched[target][-1].end
        for s in self.bot.strategies:
            needs = self._needs(s)
            primary = (s.signal_instrument_key, s.timeframe)
            # A late newest candle only matters on the series the indicators run on
            # (ORB's 1-minute series feeds the opening range, which checks itself).
            bad = {t: p for t, p in problems.items() if t in needs and (t == primary or t not in just_late)}
            if bad:  # nothing is replayed: no partial history in the indicators
                detail = "; ".join(f"{k}|{tf}: {p}" for (k, tf), p in sorted(bad.items()))
                self._block(s, f"candle backfill incomplete ({detail})")
            else:
                self._replay(s, [c for t in sorted(needs) for c in fetched[t]])
        everything = [c for candles in fetched.values() for c in candles]
        self._feed_features(everything)
        self._record(everything)
        counts = {f"{k}|{tf}": len(v) for (k, tf), v in fetched.items()}
        log.info("candle backfill done", extra={
            "candles": counts, "volume": sum(c.volume for c in everything),
            "strategies": {s.strategy_id: {"reason": s.last_reason, **s.last_context} for s in self.bot.strategies}})
        return counts

    # ------------------------------------------------------------------ #
    # Live candles: repair partial ones, refuse gaps
    # ------------------------------------------------------------------ #
    def on_candle(self, candle: Candle) -> None:
        target = (candle.instrument_key, candle.timeframe_minutes)
        if target not in self.targets():
            return
        previous = self._last_seen.get(target)
        self._last_seen[target] = candle.end
        if not candle.complete:
            self._spawn(self._repair(candle))
            return
        self._keep([candle])
        if previous is not None and previous.date() == candle.start.date() and candle.start > previous:
            log.warning("gap between candles", extra={"instrument": target[0], "timeframe": target[1],
                                                      "previous_end": previous.isoformat(),
                                                      "next_start": candle.start.isoformat()})
            self._block_trading_on(target, f"candle gap {previous:%H:%M}-{candle.start:%H:%M} "
                                           f"on {target[0]}|{target[1]}")
            step = timedelta(minutes=target[1])
            self._mark_missing(target, (previous + step * i for i in range((candle.start - previous) // step)))
        if self._pending.get(target) and target not in self._filling:
            self._filling.add(target)
            if not self._spawn(self._fill(target)):
                self._filling.discard(target)

    def _spawn(self, work: Coroutine[Any, Any, None]) -> bool:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # no running loop: nothing can be fetched
            work.close()
            return False
        task = loop.create_task(work)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return True

    async def _fill(self, target: Target) -> None:
        """One more fetch of candles of ``target`` that are still missing."""
        key, tf = target
        try:
            attempt = self._fill_attempts.get(target, 0) + 1
            self._fill_attempts[target] = attempt
            try:
                candles = await self._fetch(key, tf, self.bot._now())
            except Exception as exc:
                candles = []
                log.warning("missed candle fetch failed", extra={"instrument": key, "timeframe": tf,
                                                                 "error": f"{type(exc).__name__}: {exc}"[:200]})
            waiting = self._pending.get(target, set())
            found = sorted((c for c in candles if c.start in waiting), key=lambda c: c.start)
            if found:
                self._apply_late(target, found)
            left = sorted(self._pending.get(target, set()))
            if found:
                log.warning("missed candles filled", extra={
                    "instrument": key, "timeframe": tf, "filled": [f"{c.start:%H:%M}" for c in found],
                    "still_missing": [f"{x:%H:%M}" for x in left]})
            if left and attempt >= MAX_FILL_ATTEMPTS:
                log.error("missed candles could not be filled", extra={
                    "instrument": key, "timeframe": tf, "missing": [f"{x:%H:%M}" for x in left],
                    "attempts": attempt})
                self._pending.pop(target, None)
        finally:
            self._filling.discard(target)

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
            if not self._missing(candles, tf, partial.end):
                held = self._today.get((key, tf), {})
                new = [c for c in candles if c.start <= partial.start and c.start not in held]
                if new:
                    self._apply_late((key, tf), new)
                return
        log.warning("partial candle could not be repaired",
                    extra={"instrument": key, "timeframe": tf, "candle_start": partial.start.isoformat(),
                           "next": "fetched again when the next candle closes"})
        self._mark_missing((key, tf), [partial.start])
        self._block_trading_on((key, tf), f"partial candle {partial.start:%H:%M} on {key}|{tf} could not be repaired")

    async def close(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)


__all__ = ["MAX_FILL_ATTEMPTS", "PRIOR_DAY_ATTEMPTS", "REPAIR_ATTEMPTS", "HistoryLoader"]
