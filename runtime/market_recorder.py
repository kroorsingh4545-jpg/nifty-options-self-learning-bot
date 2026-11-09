"""Saves candles and sampled option quotes so a session can be reviewed later.

Write-only towards the database and never read by trading. Rows are buffered in
memory and written by a timer, so nothing here slows the tick path, and any
failure is logged and dropped: recording can never affect trading.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import date, datetime, timedelta

from config.settings import MarketRecordingSection
from core.models import Candle, Tick
from data.storage.repositories import MarketHistoryRepository
from monitoring.json_logger import get_logger

log = get_logger(__name__)

#: rows kept in memory if the database cannot be written
MAX_BUFFERED = 100_000


class MarketRecorder:
    def __init__(self, repo: MarketHistoryRepository, settings: MarketRecordingSection, *,
                 is_option: Callable[[str], bool], held_keys: Callable[[], set[str]]) -> None:
        self.repo = repo
        self._retention = timedelta(days=settings.option_quote_retention_days)
        self._quote_interval = timedelta(seconds=settings.quote_interval_seconds)
        self._held_interval = timedelta(seconds=settings.held_quote_interval_seconds)
        self._is_option = is_option
        self._held_keys = held_keys
        self._candles: list[tuple[Candle, str]] = []
        self._quotes: list[tuple[Tick, bool]] = []
        self._last_quote: dict[str, datetime] = {}
        self.failures = 0

    # Listeners: must never raise into the candle builder / market data service
    def on_candle(self, candle: Candle) -> None:
        if len(self._candles) < MAX_BUFFERED:
            self._candles.append((candle, "LIVE"))

    def record_backfill(self, candles: Sequence[Candle]) -> None:
        self._candles.extend((c, "BACKFILL") for c in candles[:max(0, MAX_BUFFERED - len(self._candles))])

    def on_tick(self, tick: Tick) -> None:
        key = tick.instrument_key
        if tick.is_snapshot or not self._is_option(key) or len(self._quotes) >= MAX_BUFFERED:
            return
        held = key in self._held_keys()
        last = self._last_quote.get(key)
        if last is not None and tick.received_at - last < (self._held_interval if held else self._quote_interval):
            return
        self._last_quote[key] = tick.received_at
        self._quotes.append((tick, held))

    def flush(self) -> int:
        """Write everything buffered. Returns the rows written; never raises."""
        candles, quotes = self._candles, self._quotes
        self._candles, self._quotes = [], []
        try:
            return self.repo.save_candles(candles) + self.repo.save_quotes(quotes)
        except Exception:
            self.failures += 1
            log.exception("market recording failed; these rows are lost (trading is unaffected)",
                          extra={"candles": len(candles), "quotes": len(quotes)})
            return 0

    def purge_old_quotes(self, today: date) -> int:
        """Apply the option-quote retention. Returns the rows deleted; never raises."""
        try:
            deleted = self.repo.purge_quotes(today - self._retention)
        except Exception:
            self.failures += 1
            log.exception("option quote retention failed (trading is unaffected)")
            return 0
        if deleted:
            log.info("old option quotes deleted", extra={"rows": deleted, "retention_days": self._retention.days})
        return deleted


__all__ = ["MAX_BUFFERED", "MarketRecorder"]
