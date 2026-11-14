"""History download: daily and minute candles (NIFTY index, India VIX) into
the research database.

* Requests are split to Upstox's per-request limits (daily: ``chunk_years``
  per request, at most 10; minutes up to 15: one calendar month).
* Resumable: a range already fetched OK after it had ended is skipped.
* Refused inside the configured weekday window (``ensure_outside_window``,
  applied by ``research.__main__`` to every network command), so it never
  competes with a PAPER session for the rate-limit budget.
* An auth, scope or rate-limit failure stops the run (never retried in a
  loop); any other API error is logged for that range and the run goes on.
* Invalid candles from Upstox (e.g. high below close) are never repaired or
  kept: the bot's ``Candle`` validation rejects the whole response, so the
  range is split in halves until the bad day(s) are isolated. Those days are
  skipped and named in the fetch log; every valid candle around them is kept.
  ``MAX_BISECT_REQUESTS`` bounds the extra calls per range.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from broker.errors import AuthError, BrokerError, RateLimitError, TokenScopeError
from core.models import Candle
from research.config import BlockedWindow
from research.sources import ResearchSource
from research.store import ResearchStore

Range = tuple[date, date]
_FATAL = (AuthError, TokenScopeError, RateLimitError)
#: extra requests one range may spend isolating invalid candles
MAX_BISECT_REQUESTS = 64


class BulkDownloadBlockedError(RuntimeError):
    pass


def ensure_outside_window(now: datetime, window: BlockedWindow) -> None:
    if now.weekday() < 5 and window.start <= now.time() < window.end:
        raise BulkDownloadBlockedError(
            f"research Upstox calls are blocked on weekdays {window.start:%H:%M}-{window.end:%H:%M} IST "
            "(the PAPER bot may be using the Upstox rate limit); run after the close or at the weekend")


def daily_chunks(start: date, end: date, years: int) -> list[Range]:
    """Fixed calendar-aligned blocks of ``years`` years (first block from
    ``start``), the last one cut at ``end``."""
    if start > end:
        return []
    out: list[Range] = []
    a = start
    while a <= end:
        block_end = date(a.year - (a.year - start.year) % years + years - 1, 12, 31)
        b = min(block_end, end)
        out.append((a, b))
        a = b + timedelta(days=1)
    return out


def month_chunks(start: date, end: date) -> list[Range]:
    out: list[Range] = []
    a = start
    while a <= end:
        nxt = date(a.year + (a.month == 12), a.month % 12 + 1, 1)
        b = min(nxt - timedelta(days=1), end)
        out.append((a, b))
        a = nxt
    return out


@dataclass
class FetchSummary:
    instrument_key: str
    unit: str
    interval: int
    chunks: int = 0
    skipped: int = 0
    rows: int = 0
    errors: list[str] = field(default_factory=list)
    invalid_days: list[date] = field(default_factory=list)
    first: date | None = None
    last: date | None = None

    def line(self) -> str:
        span = f"{self.first} .. {self.last}" if self.first else "no data"
        err = f", {len(self.errors)} error(s)" if self.errors else ""
        bad = (f", {len(self.invalid_days)} day(s) skipped for invalid Upstox candles: "
               f"{', '.join(map(str, self.invalid_days))}") if self.invalid_days else ""
        return (f"{self.instrument_key} {self.unit}/{self.interval}: {self.rows} rows from {self.chunks} request(s), "
                f"{self.skipped} range(s) already complete ({span}){err}{bad}")


class _Budget:
    def __init__(self, n: int) -> None:
        self.left = n


async def _fetch_valid(fetch: Callable[[date, date], Awaitable[list[Candle]]], a: date, b: date,
                       invalid: list[date], budget: _Budget) -> list[Candle]:
    """``fetch(a, b)``; on a validation error, split the range until the
    invalid day(s) are isolated (skipped and appended to ``invalid``)."""
    try:
        return await fetch(a, b)
    except ValueError:  # pydantic rejected a candle Upstox returned (e.g. inconsistent OHLC)
        if a == b:
            invalid.append(a)
            return []
        if budget.left < 2:
            raise
        budget.left -= 2
        mid = a + (b - a) // 2
        return (await _fetch_valid(fetch, a, mid, invalid, budget)
                + await _fetch_valid(fetch, mid + timedelta(days=1), b, invalid, budget))


async def _run(store: ResearchStore, summary: FetchSummary, chunks: Sequence[Range],
               fetch: Callable[[date, date], Awaitable[list[Candle]]],
               save: Callable[[list[Candle]], int], now: Callable[[], datetime]) -> FetchSummary:
    done = store.completed_ranges(summary.instrument_key, summary.unit, summary.interval)
    for a, b in chunks:
        if (a, b) in done:
            summary.skipped += 1
            continue
        summary.chunks += 1
        invalid: list[date] = []
        try:
            candles = [c for c in await _fetch_valid(fetch, a, b, invalid, _Budget(MAX_BISECT_REQUESTS))
                       if c.complete]
        except _FATAL as exc:
            store.log_fetch(summary.instrument_key, summary.unit, summary.interval, a, b, 0, "ERROR",
                            f"{type(exc).__name__}: {exc}", at=now())
            raise
        except (BrokerError, ValueError) as exc:
            msg = f"{a}..{b}: {type(exc).__name__}: {' '.join(str(exc).split())[:300]}"
            summary.errors.append(msg)
            store.log_fetch(summary.instrument_key, summary.unit, summary.interval, a, b, 0, "ERROR", msg, at=now())
            continue
        n = save(candles)
        summary.rows += n
        if candles:
            first, last = candles[0].start.date(), candles[-1].start.date()
            summary.first = min(summary.first or first, first)
            summary.last = max(summary.last or last, last)
        note = (f"skipped invalid Upstox candle(s) on: {', '.join(map(str, invalid))}") if invalid else None
        summary.invalid_days += invalid
        store.log_fetch(summary.instrument_key, summary.unit, summary.interval, a, b, n, "OK", note, at=now())
    return summary


async def download_daily(source: ResearchSource, store: ResearchStore, instrument_key: str, *, start: date,
                         end: date, chunk_years: int, now: Callable[[], datetime]) -> FetchSummary:
    async def fetch(a: date, b: date) -> list[Candle]:
        return await source.get_daily_candles(instrument_key, a, b)

    return await _run(store, FetchSummary(instrument_key, "days", 1), daily_chunks(start, end, chunk_years), fetch,
                      lambda cs: store.upsert_daily(cs, at=now()), now)


async def download_minutes(source: ResearchSource, store: ResearchStore, instrument_key: str, *, start: date,
                           end: date, interval: int, now: Callable[[], datetime]) -> FetchSummary:
    if not 1 <= interval <= 15:
        raise ValueError("minute history is fetched one month per request, so interval must be 1..15")

    async def fetch(a: date, b: date) -> list[Candle]:
        return await source.get_historical_candles(instrument_key, interval, a, b)

    return await _run(store, FetchSummary(instrument_key, "minutes", interval), month_chunks(start, end), fetch,
                      lambda cs: store.upsert_minutes(cs, at=now()), now)


__all__ = ["BulkDownloadBlockedError", "FetchSummary", "daily_chunks", "download_daily", "download_minutes",
           "ensure_outside_window", "month_chunks"]
