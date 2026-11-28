"""Saves candles, sampled quotes and every strategy signal so a session can be
reviewed (and later evaluated in shadow mode) afterwards.

Write-only towards the database and never read by trading. Two guarantees:

* **Never raises.** Every listener (ticks, candles, bus events) and every
  database call is guarded: a failure is counted and logged, the rows of
  that write are dropped, and trading carries on.
* **Never blocks the tick path.** Listeners only append to in-memory buffers
  (no I/O, no lock on the database). Rows are written by a timer job, at most
  ``max_rows_per_flush`` per run, so even a large backlog after a database
  hiccup is written over several short runs rather than one long one.
  Buffers are capped (``MAX_BUFFERED`` rows each); beyond that new rows are
  dropped and counted.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, timedelta
from typing import Any

from config.settings import MarketRecordingSection
from core.enums import TradingMode
from core.event_bus import Event
from core.models import Candle, Signal, Tick
from data.storage.repositories import MarketHistoryRepository, SignalRow
from monitoring.json_logger import get_logger

log = get_logger(__name__)

#: rows kept in memory per buffer if the database cannot be written
MAX_BUFFERED = 100_000
#: signals waiting for their entry decision; the oldest is written undecided beyond this
MAX_PENDING_SIGNALS = 1_000
#: a listener failure is logged the first time and then every this many times
LOG_EVERY = 1_000


class MarketRecorder:
    def __init__(self, repo: MarketHistoryRepository, settings: MarketRecordingSection, *,
                 is_option: Callable[[str], bool], held_keys: Callable[[], set[str]],
                 is_market: Callable[[str], bool] = lambda _key: False,
                 mode: TradingMode = TradingMode.PAPER) -> None:
        self.repo = repo
        self.mode = TradingMode(mode)
        self._retention = timedelta(days=settings.option_quote_retention_days)
        self._quote_interval = timedelta(seconds=settings.quote_interval_seconds)
        self._held_interval = timedelta(seconds=settings.held_quote_interval_seconds)
        self._record_market = settings.record_market_quotes
        self._max_rows = settings.max_rows_per_flush
        self._is_option = is_option
        self._is_market = is_market
        self._held_keys = held_keys
        self._candles: list[tuple[Candle, str]] = []
        self._quotes: list[tuple[Tick, bool]] = []
        self._market: list[Tick] = []
        self._signals: list[SignalRow] = []
        self._pending: OrderedDict[str, Signal] = OrderedDict()
        self._last_slot: dict[str, tuple[timedelta, int]] = {}  # instrument -> (interval, slot) last sampled
        self.failures = 0  # database writes / retention runs that failed
        self.listener_failures = 0  # listener calls that raised internally (swallowed)
        self.dropped = 0  # rows refused because a buffer was full

    # ------------------------------------------------------------------ #
    # Listeners: append to memory only; never raise, never touch the database
    # ------------------------------------------------------------------ #
    def on_candle(self, candle: Candle) -> None:
        try:
            self._append(self._candles, (candle, "LIVE"))
        except Exception:
            self._listener_failed("on_candle")

    def record_backfill(self, candles: Sequence[Candle]) -> None:
        try:
            for c in candles:
                self._append(self._candles, (c, "BACKFILL"))
        except Exception:
            self._listener_failed("record_backfill")

    def on_tick(self, tick: Tick) -> None:
        try:
            self._on_tick(tick)
        except Exception:
            self._listener_failed("on_tick")

    def _on_tick(self, tick: Tick) -> None:
        if tick.is_snapshot:
            return
        key = tick.instrument_key
        if self._is_option(key):
            held = key in self._held_keys()
            if self._due(key, tick.received_at, self._held_interval if held else self._quote_interval):
                self._append(self._quotes, (tick, held))
        elif self._record_market and self._is_market(key) and self._due(key, tick.received_at,
                                                                          self._quote_interval):
            self._append(self._market, tick)

    def _due(self, key: str, at: datetime, every: timedelta) -> bool:
        """The first tick of each fixed clock slot (``every`` long, aligned to
        the epoch, so 5-s slots start at :00, :05, ...; the same slots the
        data-quality audit counts). The old rule ("``every`` since the last
        sample") drifted: with ticks every ~1 s it sampled about every 5-6 s
        and left ~7 % of 5-s slots empty on a perfect day."""
        slot = (every, int(at.timestamp() // every.total_seconds()))
        if self._last_slot.get(key) == slot:
            return False
        self._last_slot[key] = slot
        return True

    def on_signal(self, event: Event) -> None:
        """``Topic.SIGNAL`` handler: remember the signal until its decision arrives."""
        try:
            sig = event.payload
            if not isinstance(sig, Signal):
                return
            self._pending[sig.signal_id] = sig
            while len(self._pending) > MAX_PENDING_SIGNALS:
                _, oldest = self._pending.popitem(last=False)
                self._append(self._signals, SignalRow(oldest, self.mode, decided=False))
        except Exception:
            self._listener_failed("on_signal")

    def on_decision(self, event: Event) -> None:
        """``Topic.ENTRY_DECISION`` handler: the signal with what became of it."""
        try:
            d: Mapping[str, Any] = event.payload
            sig = self._pending.pop(str(d.get("signal_id")), None)
            if sig is None:
                return
            score = d.get("ml_score")
            qty = d.get("quantity")
            self._append(self._signals, SignalRow(
                sig, self.mode, decided=True, approved=bool(d.get("approved")),
                reasons=tuple(str(r) for r in d.get("reasons") or ()), trade_uid=d.get("trade_uid"),
                quantity=None if qty is None else int(qty), ml_score=None if score is None else float(score)))
        except Exception:
            self._listener_failed("on_decision")

    def _append(self, buffer: list[Any], row: Any) -> None:
        if len(buffer) >= MAX_BUFFERED:
            self.dropped += 1
            if self.dropped % LOG_EVERY == 1:
                log.warning("market recording buffer full; rows dropped (trading is unaffected)",
                            extra={"dropped": self.dropped, "limit": MAX_BUFFERED})
            return
        buffer.append(row)

    def _listener_failed(self, where: str) -> None:
        self.listener_failures += 1
        if self.listener_failures % LOG_EVERY == 1:
            log.exception("market recording listener failed; ignored (trading is unaffected)",
                          extra={"listener": where, "failures": self.listener_failures})

    # ------------------------------------------------------------------ #
    # Writing (timer job)
    # ------------------------------------------------------------------ #
    @property
    def buffered(self) -> int:
        return len(self._candles) + len(self._quotes) + len(self._market) + len(self._signals)

    def flush(self) -> int:
        """Write at most ``max_rows_per_flush`` buffered rows (signals first, then
        candles, option quotes, market quotes); the rest wait for the next run.
        Returns the rows written; never raises. A failed write drops that
        write's rows (and is counted), the other tables are still written."""
        budget = self._max_rows
        written = 0
        jobs: tuple[tuple[str, list[Any], Callable[[Any], int]], ...] = (
            ("signals", self._signals, self.repo.save_signals),
            ("candles", self._candles, self.repo.save_candles),
            ("option_quotes", self._quotes, self.repo.save_quotes),
            ("market_quotes", self._market, self.repo.save_market_quotes))
        for name, buffer, save in jobs:
            if budget <= 0:
                break
            batch = buffer[:budget]
            if not batch:
                continue
            del buffer[:len(batch)]
            budget -= len(batch)
            try:
                written += save(batch)
            except Exception:
                self.failures += 1
                log.exception("market recording failed; these rows are lost (trading is unaffected)",
                              extra={"table": name, "rows": len(batch)})
        return written

    def close(self) -> int:
        """End of session: signals still waiting for a decision are recorded as
        undecided, then everything buffered is written. Never raises."""
        try:
            for sig in list(self._pending.values()):
                self._append(self._signals, SignalRow(sig, self.mode, decided=False))
            self._pending.clear()
        except Exception:
            self._listener_failed("close")
        total = 0
        while self.buffered:
            before = self.buffered
            total += self.flush()
            if self.buffered >= before:  # nothing could be taken out: stop rather than loop
                break
        return total

    def purge_old_quotes(self, today: date) -> int:
        """Apply the quote retention (option and market quotes). Returns the
        rows deleted; never raises."""
        try:
            deleted = self.repo.purge_quotes(today - self._retention)
        except Exception:
            self.failures += 1
            log.exception("quote retention failed (trading is unaffected)")
            return 0
        if deleted:
            log.info("old quotes deleted", extra={"rows": deleted, "retention_days": self._retention.days})
        return deleted


__all__ = ["LOG_EVERY", "MAX_BUFFERED", "MAX_PENDING_SIGNALS", "MarketRecorder"]
