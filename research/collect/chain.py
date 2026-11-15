"""Option contracts and option-chain snapshots into the research database.

``capture_contracts`` keeps every NIFTY option contract's strike / expiry /
type after it expires (the bot's recorded quotes only carry instrument keys,
and Upstox drops expired contracts from its list).

``snapshot_chains`` stores the chain of the nearest expiries (plus the next
monthly) exactly as returned: IV is kept raw (``iv_raw``) because the REST
chain's IV unit is not documented; ``research smoke`` records the unit.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any

from broker.errors import BrokerError
from core.models import Instrument
from research.config import ChainSection
from research.sources import ResearchSource
from research.store import ChainRow, ResearchStore


async def capture_contracts(source: ResearchSource, store: ResearchStore, underlying_key: str,
                            now: Callable[[], datetime]) -> list[Instrument]:
    try:
        instruments = await source.get_option_contracts(underlying_key)
    except BrokerError as exc:
        store.log_fetch(underlying_key, "contracts", 0, None, None, 0, "ERROR", f"{type(exc).__name__}: {exc}",
                        at=now())
        raise
    n = store.upsert_contracts(instruments, at=now())
    store.log_fetch(underlying_key, "contracts", 0, None, None, n, "OK", at=now())
    return instruments


def _is_monthly(expiry: date, instruments: Sequence[Instrument], upcoming: Sequence[date]) -> bool:
    flags = {i.weekly for i in instruments if i.expiry == expiry and i.option_type is not None}
    if flags - {None}:
        return False in flags
    # no weekly flag from Upstox: the last listed expiry of its calendar month
    return expiry == max(e for e in upcoming if (e.year, e.month) == (expiry.year, expiry.month))


def choose_expiries(instruments: Sequence[Instrument], today: date, cfg: ChainSection) -> list[date]:
    upcoming = sorted({i.expiry for i in instruments
                       if i.expiry is not None and i.expiry >= today and i.option_type is not None})
    chosen = upcoming[:cfg.nearest_expiries]
    if cfg.include_next_monthly:
        monthly = next((e for e in upcoming if _is_monthly(e, instruments, upcoming)), None)
        if monthly is not None and monthly not in chosen:
            chosen.append(monthly)
    return chosen


def _f(v: Any) -> float | None:
    return None if v is None else float(v)


def _i(v: Any) -> int | None:
    return None if v is None else int(v)


def chain_rows(raw: Mapping[str, Any]) -> tuple[float | None, list[ChainRow]]:
    """Raw Upstox ``/v2/option/chain`` rows -> (spot, rows); values unchanged."""
    spot: float | None = None
    out: list[ChainRow] = []
    for row in raw.get("rows") or []:
        strike = float(row["strike_price"])
        spot = _f(row.get("underlying_spot_price")) or spot
        for side_key, opt in (("call_options", "CE"), ("put_options", "PE")):
            side = row.get(side_key) or {}
            if not side.get("instrument_key"):
                continue
            md = side.get("market_data") or {}
            g = side.get("option_greeks") or {}
            out.append(ChainRow(
                strike=strike, option_type=opt, instrument_key=side["instrument_key"], ltp=_f(md.get("ltp")),
                close_price=_f(md.get("close_price")), bid=_f(md.get("bid_price")), ask=_f(md.get("ask_price")),
                bid_qty=_i(md.get("bid_qty")), ask_qty=_i(md.get("ask_qty")), volume=_i(md.get("volume")),
                oi=_f(md.get("oi")), prev_oi=_f(md.get("prev_oi")), iv_raw=_f(g.get("iv")), delta=_f(g.get("delta")),
                gamma=_f(g.get("gamma")), theta=_f(g.get("theta")), vega=_f(g.get("vega")), pop=_f(g.get("pop")),
                strike_pcr=_f(row.get("pcr"))))
    return spot, out


@dataclass(frozen=True)
class SnapshotResult:
    expiry: date
    snapshot_id: int | None
    rows: int
    spot: float | None
    error: str | None = None


async def snapshot_chains(source: ResearchSource, store: ResearchStore, underlying_key: str, cfg: ChainSection,
                          now: Callable[[], datetime]) -> list[SnapshotResult]:
    instruments = await capture_contracts(source, store, underlying_key, now)
    out: list[SnapshotResult] = []
    for expiry in choose_expiries(instruments, now().date(), cfg):
        try:
            raw = await source.get_option_chain(underlying_key, expiry)
        except BrokerError as exc:
            msg = f"{type(exc).__name__}: {exc}"
            store.log_fetch(underlying_key, "chain", 0, expiry, expiry, 0, "ERROR", msg, at=now())
            out.append(SnapshotResult(expiry, None, 0, None, msg))
            continue
        spot, rows = chain_rows(raw)
        taken = now()
        sid = store.save_chain_snapshot(taken_at=taken, underlying_key=underlying_key, expiry=expiry, spot=spot,
                                        rows=rows)
        store.log_fetch(underlying_key, "chain", 0, expiry, expiry, len(rows), "OK", at=taken)
        out.append(SnapshotResult(expiry, sid, len(rows), spot))
    return out


def due_times(schedule: Sequence[time], now: datetime, *, late_tolerance: timedelta = timedelta(minutes=10)
              ) -> list[datetime]:
    """Today's snapshot times still to take: those ahead of ``now``, plus one
    missed by at most ``late_tolerance`` (a run started a little late)."""
    out = []
    for t in schedule:
        at = datetime.combine(now.date(), t, tzinfo=now.tzinfo)
        if at >= now - late_tolerance:
            out.append(at)
    return out


async def run_chain_schedule(schedule: Sequence[time], *, now: Callable[[], datetime],
                             sleep: Callable[[float], Awaitable[None]],
                             take: Callable[[], Awaitable[list[SnapshotResult]]],
                             report: Callable[[datetime, list[SnapshotResult] | str], None]) -> int:
    """Take one snapshot set at each remaining time today, then return how
    many sets were taken. A failed set is reported and the schedule goes on."""
    taken = 0
    for at in due_times(schedule, now()):
        wait = (at - now()).total_seconds()
        if wait > 0:
            await sleep(wait)
        try:
            report(at, await take())
            taken += 1
        except BrokerError as exc:
            report(at, f"{type(exc).__name__}: {exc}")
    return taken


__all__ = ["SnapshotResult", "capture_contracts", "chain_rows", "choose_expiries", "due_times", "run_chain_schedule",
           "snapshot_chains"]
