"""Live smoke check of the research data sources (about 7 read-only calls).

1. India VIX daily candles, last 30 days
2. India VIX: first available daily candle on Upstox
3. India VIX: 1-minute candles of the first configured minute month
4. NIFTY index daily candles, last 30 days
5. Option chain IV unit: the chain's raw IV against Black-Scholes IV solved
   from the same chain's prices (``options.pricing``), near the money.
   The inferred unit is recorded in the research database.

Nothing here is written except the ``chain_iv_unit`` observation.
"""

from __future__ import annotations

import statistics
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from core.clock import IST
from options.pricing import YEAR_SECONDS, implied_volatility
from research.collect.chain import chain_rows, choose_expiries
from research.config import ChainSection, ResearchConfig
from research.sources import ResearchSource
from research.store import ResearchStore

EXPIRY_CLOSE = time(15, 30)
IV_UNIT_OBSERVATION = "chain_iv_unit"


@dataclass(frozen=True)
class SmokeLine:
    name: str
    ok: bool
    detail: str

    def render(self) -> str:
        return f"[{'PASS' if self.ok else 'FAIL'}] {self.name}: {self.detail}"


def infer_iv_unit(ratios: list[float]) -> str:
    """Median of raw_iv / solved_iv: ~1 -> decimal, ~100 -> percent."""
    if not ratios:
        return "unknown"
    m = statistics.median(ratios)
    if 0.5 <= m <= 2.0:
        return "decimal"
    if 50.0 <= m <= 200.0:
        return "percent"
    return "unknown"


async def _daily(source: ResearchSource, key: str, a: date, b: date, name: str) -> SmokeLine:
    try:
        cs = await source.get_daily_candles(key, a, b)
    except Exception as exc:  # a check reports, never raises
        return SmokeLine(name, False, f"{type(exc).__name__}: {exc}")
    if not cs:
        return SmokeLine(name, False, f"no candles {a}..{b}")
    return SmokeLine(name, True, f"{len(cs)} candles, last {cs[-1].start.date()} close {cs[-1].close:,.2f}")


async def _first_daily(source: ResearchSource, key: str, start: date, today: date) -> SmokeLine:
    name = "India VIX first daily candle"
    a = start
    try:
        while a <= today:
            b = min(date(a.year + 9, 12, 31), today)
            cs = await source.get_daily_candles(key, a, b)
            if cs:
                return SmokeLine(name, True, f"{cs[0].start.date()} (searched from {start})")
            a = b + timedelta(days=1)
    except Exception as exc:
        return SmokeLine(name, False, f"{type(exc).__name__}: {exc}")
    return SmokeLine(name, False, f"no daily candles since {start}")


async def _minutes(source: ResearchSource, key: str, month_start: date, interval: int) -> SmokeLine:
    name = f"India VIX {interval}-minute candles {month_start:%Y-%m}"
    nxt = date(month_start.year + (month_start.month == 12), month_start.month % 12 + 1, 1)
    try:
        cs = await source.get_historical_candles(key, interval, month_start, nxt - timedelta(days=1))
    except Exception as exc:
        return SmokeLine(name, False, f"{type(exc).__name__}: {exc}")
    if not cs:
        return SmokeLine(name, False, "no candles")
    return SmokeLine(name, True, f"{len(cs)} candles, first {cs[0].start.isoformat()}")


async def _iv_unit(source: ResearchSource, store: ResearchStore, underlying: str, rate: float,
                   now: datetime) -> SmokeLine:
    name = "Option chain IV unit"
    try:
        contracts = await source.get_option_contracts(underlying)
        expiries = choose_expiries(contracts, now.date(), ChainSection(nearest_expiries=1, include_next_monthly=False))
        if not expiries:
            return SmokeLine(name, False, "no upcoming expiry")
        expiry = expiries[0]
        spot, rows = chain_rows(await source.get_option_chain(underlying, expiry))
    except Exception as exc:
        return SmokeLine(name, False, f"{type(exc).__name__}: {exc}")
    if not spot or not rows:
        return SmokeLine(name, False, "empty chain")
    years = (datetime.combine(expiry, EXPIRY_CLOSE, tzinfo=IST) - now).total_seconds() / YEAR_SECONDS
    if years <= 0:
        return SmokeLine(name, False, "expiry already passed")
    near = sorted(rows, key=lambda r: abs(r.strike - spot))[:10]
    ratios: list[float] = []
    for r in near:
        if not r.ltp or not r.iv_raw or r.iv_raw <= 0:
            continue
        solved = implied_volatility(r.ltp, spot, r.strike, years, rate, r.option_type == "CE")
        if solved:
            ratios.append(r.iv_raw / solved)
    unit = infer_iv_unit(ratios)
    detail = (f"{unit} (median raw/solved {statistics.median(ratios):.2f} over {len(ratios)} near-ATM quotes, "
              f"expiry {expiry}, spot {spot:,.2f})") if ratios else "no usable near-ATM quotes"
    if unit != "unknown":
        store.record_observation(IV_UNIT_OBSERVATION, unit, detail, at=now)
    return SmokeLine(name, unit != "unknown", detail)


async def run_smoke(source: ResearchSource, store: ResearchStore, cfg: ResearchConfig,
                    now: Callable[[], datetime]) -> list[SmokeLine]:
    t = now()
    today, month_ago = t.date(), t.date() - timedelta(days=30)
    vix, nifty = cfg.instruments.india_vix_key, cfg.instruments.nifty_index_key
    return [
        await _daily(source, vix, month_ago, today, "India VIX daily (30 days)"),
        await _first_daily(source, vix, cfg.history.daily_from, today),
        await _minutes(source, vix, cfg.history.minute_from.replace(day=1), cfg.history.minute_interval),
        await _daily(source, nifty, month_ago, today, "NIFTY index daily (30 days)"),
        await _iv_unit(source, store, nifty, cfg.smoke.iv_check_rate, t),
    ]


__all__ = ["IV_UNIT_OBSERVATION", "SmokeLine", "infer_iv_unit", "run_smoke"]
