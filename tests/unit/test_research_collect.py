"""Research collectors, config, session calendar and the smoke check (fake source, no network)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import pytest

from broker.errors import BrokerAPIError, TokenExpiredError
from broker.upstox_mapping import parse_instrument
from core.clock import IST
from core.models import Candle, Instrument
from options.pricing import YEAR_SECONDS, black_scholes
from research.collect.chain import chain_rows, choose_expiries, snapshot_chains
from research.collect.history import (
    BulkDownloadBlockedError,
    daily_chunks,
    download_daily,
    download_minutes,
    ensure_outside_window,
    month_chunks,
)
from research.config import BlockedWindow, ChainSection, ExpiryRule, ResearchConfig, load_research_config
from research.sessions import SessionCalendar, weekly_expiries
from research.smoke import IV_UNIT_OBSERVATION, infer_iv_unit, run_smoke
from research.store import ResearchStore
from tests.fakes import chain_row, option_contract

NIFTY, VIX = "NSE_INDEX|Nifty 50", "NSE_INDEX|India VIX"
RCFG = load_research_config()


def at(d: date, hh: int = 18, mm: int = 0) -> datetime:
    return datetime.combine(d, time(hh, mm), tzinfo=IST)


def daily(key: str, d: date, close: float = 100.0) -> Candle:
    start = at(d, 0)
    return Candle(instrument_key=key, timeframe_minutes=1440, start=start, end=start + timedelta(days=1),
                  open=close, high=close + 1, low=close - 1, close=close)


def weekdays(a: date, b: date, *, closed: tuple[date, ...] = ()) -> list[date]:
    out, d = [], a
    while d <= b:
        if d.weekday() < 5 and d not in closed:
            out.append(d)
        d += timedelta(days=1)
    return out


class FakeSource:
    def __init__(self, sessions: list[date] | None = None) -> None:
        self.sessions = sessions or []
        self.calls: list[tuple[Any, ...]] = []
        self.fail: dict[tuple[date, date], Exception] = {}
        self.contracts: list[Instrument] = []
        self.chains: dict[date, dict[str, Any]] = {}

    async def get_daily_candles(self, key: str, a: date, b: date) -> list[Candle]:
        self.calls.append(("days", key, a, b))
        if (a, b) in self.fail:
            raise self.fail[(a, b)]
        return [daily(key, d) for d in self.sessions if a <= d <= b]

    async def get_historical_candles(self, key: str, interval: int, a: date, b: date) -> list[Candle]:
        self.calls.append(("minutes", key, a, b))
        if (a, b) in self.fail:
            raise self.fail[(a, b)]
        out = []
        for d in (s for s in self.sessions if a <= s <= b):
            t = at(d, 9, 15)
            out.append(Candle(instrument_key=key, timeframe_minutes=interval, start=t,
                              end=t + timedelta(minutes=interval), open=1, high=2, low=1, close=2))
        return out

    async def get_option_contracts(self, key: str, expiry: date | None = None) -> list[Instrument]:
        self.calls.append(("contracts", key))
        return self.contracts

    async def get_option_chain(self, key: str, expiry: date) -> dict[str, Any]:
        self.calls.append(("chain", key, expiry))
        if expiry not in self.chains:
            raise BrokerAPIError(400, "UDAPI1", "no chain")
        return self.chains[expiry]


@pytest.fixture
def store(tmp_path: Path) -> Iterator[ResearchStore]:
    s = ResearchStore.open(tmp_path / "research.db")
    yield s
    s.close()


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #
def test_default_research_config_is_valid() -> None:
    assert RCFG.store_path == Path("data/research.db")
    assert RCFG.instruments.india_vix_key == VIX
    assert RCFG.rate_share == 0.1 and RCFG.queue_timeout_seconds == 120.0
    assert [r.weekday for r in RCFG.weekly_expiry_rules] == [3, 1]  # Thursday, then Tuesday


def test_research_config_rejects_unsafe_values() -> None:
    base = RCFG.model_dump()
    with pytest.raises(ValueError):
        ResearchConfig.model_validate({**base, "rate_share": 0.9})
    with pytest.raises(ValueError, match=".db"):
        ResearchConfig.model_validate({**base, "store_path": "data/research.csv"})
    with pytest.raises(ValueError, match="increasing"):
        ResearchConfig.model_validate({**base, "weekly_expiry_rules": base["weekly_expiry_rules"][::-1]})
    with pytest.raises(ValueError):
        ResearchConfig.model_validate({**base, "history": {**base["history"], "daily_chunk_years": 10}})
    with pytest.raises(ValueError):
        BlockedWindow(start=time(15, 0), end=time(9, 0))


# --------------------------------------------------------------------------- #
# history
# --------------------------------------------------------------------------- #
def test_chunking_respects_upstox_request_limits() -> None:
    chunks = daily_chunks(date(2000, 1, 1), date(2026, 11, 13), 5)
    assert chunks[0] == (date(2000, 1, 1), date(2004, 12, 31))
    assert chunks[-1] == (date(2025, 1, 1), date(2026, 11, 13))
    assert all((b - a).days < 3652 for a, b in chunks) and len(chunks) == 6
    assert daily_chunks(date(2026, 1, 2), date(2026, 1, 1), 5) == []
    months = month_chunks(date(2022, 1, 15), date(2022, 3, 2))
    assert months == [(date(2022, 1, 15), date(2022, 1, 31)), (date(2022, 2, 1), date(2022, 2, 28)),
                      (date(2022, 3, 1), date(2022, 3, 2))]
    assert len(month_chunks(date(2022, 1, 1), date(2026, 11, 13))) == 59


def test_blocked_window_only_on_weekdays_in_session() -> None:
    w = BlockedWindow()
    with pytest.raises(BulkDownloadBlockedError):
        ensure_outside_window(datetime(2026, 11, 13, 9, 0, tzinfo=IST), w)  # Friday
    ensure_outside_window(datetime(2026, 11, 13, 15, 40, tzinfo=IST), w)
    ensure_outside_window(datetime(2026, 11, 13, 8, 29, tzinfo=IST), w)
    with pytest.raises(BulkDownloadBlockedError):
        ensure_outside_window(datetime(2026, 11, 13, 8, 30, tzinfo=IST), w)
    ensure_outside_window(datetime(2026, 11, 14, 11, 0, tzinfo=IST), w)  # Saturday


async def test_daily_download_saves_resumes_and_skips_complete_ranges(store: ResearchStore) -> None:
    src = FakeSource(weekdays(date(2024, 12, 20), date(2025, 1, 10)))
    s1 = await download_daily(src, store, VIX, start=date(2020, 1, 1), end=date(2025, 1, 10), chunk_years=5,
                              now=lambda: at(date(2025, 1, 11)))
    assert s1.chunks == 2 and s1.skipped == 0 and s1.rows == len(src.sessions)
    assert (s1.first, s1.last) == (date(2024, 12, 20), date(2025, 1, 10))
    src.calls.clear()
    s2 = await download_daily(src, store, VIX, start=date(2020, 1, 1), end=date(2025, 1, 20), chunk_years=5,
                              now=lambda: at(date(2025, 1, 21)))
    assert s2.skipped == 1 and src.calls == [("days", VIX, date(2025, 1, 1), date(2025, 1, 20))]
    assert store.count("daily_candles") == len(src.sessions)


async def test_api_errors_are_logged_and_the_run_continues(store: ResearchStore) -> None:
    src = FakeSource(weekdays(date(2022, 1, 1), date(2022, 2, 28)))
    src.fail[(date(2022, 1, 1), date(2022, 1, 31))] = BrokerAPIError(400, "UDAPI1021", "bad range")
    s = await download_minutes(src, store, VIX, start=date(2022, 1, 1), end=date(2022, 2, 28), interval=1,
                               now=lambda: at(date(2022, 3, 5)))
    assert len(s.errors) == 1 and s.rows == 20  # February only
    statuses = [r["status"] for r in store.fetch_log()]
    assert statuses == ["ERROR", "OK"]
    assert store.completed_ranges(VIX, "minutes", 1) == {(date(2022, 2, 1), date(2022, 2, 28))}


class BadDaySource(FakeSource):
    """Like Upstox: one invalid candle makes the whole response fail validation."""

    def __init__(self, sessions: list[date], bad: set[date]) -> None:
        super().__init__(sessions)
        self.bad = bad

    async def get_daily_candles(self, key: str, a: date, b: date) -> list[Candle]:
        self.calls.append(("days", key, a, b))
        if any(a <= d <= b for d in self.bad):
            raise ValueError("1 validation error for Candle\n  Value error, inconsistent OHLC")
        return [daily(key, d) for d in self.sessions if a <= d <= b]


async def test_invalid_upstox_candles_are_isolated_and_skipped(store: ResearchStore) -> None:
    sessions = weekdays(date(2009, 3, 2), date(2010, 12, 31))
    bad = {date(2009, 7, 14), date(2010, 2, 3)}
    src = BadDaySource(sessions, bad)
    s = await download_daily(src, store, VIX, start=date(2009, 1, 1), end=date(2010, 12, 31), chunk_years=5,
                             now=lambda: at(date(2026, 11, 14)))
    assert sorted(s.invalid_days) == sorted(bad) and not s.errors
    assert s.rows == len(sessions) - 2  # every valid candle kept
    assert not set(store.daily_sessions(VIX)) & bad
    (log,) = store.fetch_log()
    assert log["status"] == "OK" and "2009-07-14" in log["note"] and "2010-02-03" in log["note"]
    assert len(src.calls) <= 1 + 64
    # resumable: the range is complete, so a re-run makes no call
    src.calls.clear()
    await download_daily(src, store, VIX, start=date(2009, 1, 1), end=date(2010, 12, 31), chunk_years=5,
                         now=lambda: at(date(2026, 11, 15)))
    assert src.calls == []


async def test_bisection_budget_turns_into_a_logged_error(store: ResearchStore) -> None:
    sessions = weekdays(date(2009, 1, 1), date(2013, 12, 31))
    src = BadDaySource(sessions, set(sessions[::5]))  # far too many bad days
    s = await download_daily(src, store, VIX, start=date(2009, 1, 1), end=date(2013, 12, 31), chunk_years=5,
                             now=lambda: at(date(2026, 11, 14)))
    assert s.rows == 0 and len(s.errors) == 1 and "inconsistent OHLC" in s.errors[0]
    assert store.fetch_log()[-1]["status"] == "ERROR"
    assert len(src.calls) <= 1 + 64


async def test_auth_failure_stops_the_run(store: ResearchStore) -> None:
    src = FakeSource(weekdays(date(2022, 1, 1), date(2022, 3, 31)))
    src.fail[(date(2022, 2, 1), date(2022, 2, 28))] = TokenExpiredError("401")
    with pytest.raises(TokenExpiredError):
        await download_minutes(src, store, VIX, start=date(2022, 1, 1), end=date(2022, 3, 31), interval=1,
                               now=lambda: at(date(2022, 4, 5)))
    assert [c[2] for c in src.calls] == [date(2022, 1, 1), date(2022, 2, 1)]  # March never requested
    assert store.fetch_log()[-1]["status"] == "ERROR"


async def test_minute_download_refuses_long_intervals(store: ResearchStore) -> None:
    with pytest.raises(ValueError):
        await download_minutes(FakeSource(), store, VIX, start=date(2022, 1, 1), end=date(2022, 1, 2), interval=30,
                               now=lambda: at(date(2022, 1, 3)))


# --------------------------------------------------------------------------- #
# contracts + chain snapshots
# --------------------------------------------------------------------------- #
def _contracts(expiries: dict[str, bool]) -> list[Instrument]:
    out = []
    for exp, weekly in expiries.items():
        for opt in ("CE", "PE"):
            rec = option_contract(25100, opt, exp, key=f"NSE_FO|{exp}{opt}", lot=65)
            rec["weekly"] = weekly
            out.append(parse_instrument(rec))
    return out


def test_choose_expiries_adds_the_next_monthly() -> None:
    inst = _contracts({"2026-11-10": True, "2026-11-17": True, "2026-11-24": True, "2026-12-01": True,
                       "2026-12-29": False, "2026-11-03": True})
    got = choose_expiries(inst, date(2026, 11, 14), ChainSection(nearest_expiries=2))
    assert got == [date(2026, 11, 17), date(2026, 11, 24), date(2026, 12, 29)]
    assert choose_expiries(inst, date(2026, 11, 14), ChainSection(nearest_expiries=2, include_next_monthly=False)) \
        == [date(2026, 11, 17), date(2026, 11, 24)]


def test_choose_expiries_without_weekly_flags_uses_last_of_month() -> None:
    inst = _contracts({"2026-11-17": True, "2026-11-24": True})
    for i in inst:
        object.__setattr__(i, "weekly", None)
    got = choose_expiries(inst, date(2026, 11, 14), ChainSection(nearest_expiries=1))
    assert got == [date(2026, 11, 17), date(2026, 11, 24)]


def test_chain_rows_keep_raw_iv_and_pcr() -> None:
    spot, rows = chain_rows({"rows": [chain_row(25100, 25139.25, "2026-11-17"), {"strike_price": 25150,
                                                                                  "call_options": {}}]})
    assert spot == 25139.25 and len(rows) == 2
    ce = next(r for r in rows if r.option_type == "CE")
    assert (ce.iv_raw, ce.strike_pcr, ce.bid, ce.ask, ce.oi) == (13.5, 1.0, ce.ltp - 0.5, ce.ltp + 0.5, 1200000.0)


async def test_snapshot_chains_saves_each_expiry_and_logs_failures(store: ResearchStore) -> None:
    src = FakeSource()
    src.contracts = _contracts({"2026-11-17": True, "2026-11-24": False})
    src.chains[date(2026, 11, 17)] = {"rows": [chain_row(25100, 25139.25, "2026-11-17")]}
    res = await snapshot_chains(src, store, NIFTY, ChainSection(nearest_expiries=2), lambda: at(date(2026, 11, 14)))
    assert [(r.expiry, r.rows, r.error is None) for r in res] == [(date(2026, 11, 17), 2, True),
                                                                   (date(2026, 11, 24), 0, False)]
    assert store.count("chain_snapshots") == 1 and store.count("contracts") == 4
    assert {r["unit"]: r["status"] for r in store.fetch_log() if r["unit"] == "contracts"} == {"contracts": "OK"}


# --------------------------------------------------------------------------- #
# session calendar + weekly expiries
# --------------------------------------------------------------------------- #
CLOSED_2025 = (date(2025, 8, 15), date(2025, 8, 27))  # Independence Day, Ganesh Chaturthi (test fixture)


def test_calendar_from_sessions() -> None:
    sessions = weekdays(date(2026, 11, 2), date(2026, 11, 13), closed=(date(2026, 11, 10),)) + [date(2026, 11, 8)]
    cal = SessionCalendar.from_sessions(sessions, configured_holidays=[date(2026, 11, 10), date(2026, 11, 24)])
    assert cal.holidays == {date(2026, 11, 10)}
    assert cal.special_sessions == {date(2026, 11, 8)}  # Sunday Muhurat-style session
    assert not cal.is_trading_day(date(2026, 11, 8)) and cal.is_trading_day(date(2026, 11, 9))
    assert not cal.is_trading_day(date(2026, 11, 24))  # future: configured holiday
    assert cal.is_trading_day(date(2026, 11, 23))
    assert cal.previous_trading_day(date(2026, 11, 10)) == date(2026, 11, 9)
    assert cal.configured_mismatches(2026) == {"configured_but_traded": [], "closed_but_not_configured": []}
    wrong = SessionCalendar.from_sessions(sessions, configured_holidays=[date(2026, 11, 4)])
    assert wrong.configured_mismatches(2026) == {"configured_but_traded": [date(2026, 11, 4)],
                                                 "closed_but_not_configured": [date(2026, 11, 10)]}
    assert date(2026, 11, 4) in wrong.special_sessions  # configured holiday that traded
    assert wrong.configured_mismatches(2025) == {} and not wrong.has_configured_year(2025)


def test_holiday_shifted_tuesday_expiries_2026() -> None:
    sessions = weekdays(date(2026, 11, 2), date(2026, 11, 13), closed=(date(2026, 11, 10),))
    cal = SessionCalendar.from_sessions(sessions, configured_holidays=[date(2026, 11, 10), date(2026, 11, 24)])
    got = weekly_expiries(date(2026, 11, 2), date(2026, 11, 30), RCFG.weekly_expiry_rules, cal,
                          observed=[date(2026, 11, 9), date(2026, 11, 17)])
    assert [(e.expiry, e.shifted, e.status) for e in got] == [
        (date(2026, 11, 3), False, "rule"),
        (date(2026, 11, 9), True, "observed"),   # Tue 10 Nov holiday -> Mon 9 Nov (seen in our PAPER trades)
        (date(2026, 11, 17), False, "observed"),
        (date(2026, 11, 23), True, "rule"),      # Tue 24 Nov configured holiday -> Mon 23 Nov
    ]


def test_thursday_to_tuesday_switch_2025() -> None:
    sessions = weekdays(date(2025, 8, 1), date(2025, 9, 30), closed=CLOSED_2025)
    cal = SessionCalendar.from_sessions(sessions)
    got = [e.expiry for e in weekly_expiries(date(2025, 8, 18), date(2025, 9, 10), RCFG.weekly_expiry_rules, cal)]
    assert got == [date(2025, 8, 21), date(2025, 8, 28), date(2025, 9, 2), date(2025, 9, 9)]


def test_rule_starting_mid_week_uses_the_older_rule_that_week() -> None:
    rules = (ExpiryRule(effective_from=date(2025, 1, 1), weekday=3, source="test"),
             ExpiryRule(effective_from=date(2025, 9, 3), weekday=1, source="test"))
    cal = SessionCalendar.from_sessions(weekdays(date(2025, 8, 25), date(2025, 9, 19)))
    got = [e.expiry for e in weekly_expiries(date(2025, 8, 25), date(2025, 9, 19), rules, cal)]
    assert got == [date(2025, 8, 28), date(2025, 9, 4), date(2025, 9, 9), date(2025, 9, 16)]


# --------------------------------------------------------------------------- #
# smoke
# --------------------------------------------------------------------------- #
def test_infer_iv_unit() -> None:
    assert infer_iv_unit([1.01, 0.98, 1.05]) == "decimal"
    assert infer_iv_unit([99.0, 101.5, 100.2]) == "percent"
    assert infer_iv_unit([10.0]) == "unknown" and infer_iv_unit([]) == "unknown"


def _bs_chain(spot: float, expiry: date, now: datetime, vol: float, scale: float) -> dict[str, Any]:
    years = (datetime.combine(expiry, time(15, 30), tzinfo=IST) - now).total_seconds() / YEAR_SECONDS
    rows = []
    for strike in (25000.0, 25050.0, 25100.0, 25150.0, 25200.0):
        row = chain_row(strike, spot, expiry.isoformat())
        for side, call in (("call_options", True), ("put_options", False)):
            price = round(black_scholes(spot, strike, years, vol, RCFG.smoke.iv_check_rate, call), 2)
            row[side]["market_data"]["ltp"] = price
            row[side]["option_greeks"]["iv"] = vol * scale
        rows.append(row)
    return {"rows": rows}


@pytest.mark.parametrize(("scale", "unit"), [(100.0, "percent"), (1.0, "decimal")])
async def test_smoke_detects_the_chain_iv_unit(store: ResearchStore, scale: float, unit: str) -> None:
    now = at(date(2026, 11, 14), 11)
    src = FakeSource(weekdays(date(2026, 10, 1), date(2026, 11, 13)))
    src.contracts = _contracts({"2026-11-17": True})
    src.chains[date(2026, 11, 17)] = _bs_chain(25139.25, date(2026, 11, 17), now, 0.13, scale)
    lines = await run_smoke(src, store, RCFG, lambda: now)
    by = {line.name: line for line in lines}
    assert by["Option chain IV unit"].ok and by["Option chain IV unit"].detail.startswith(unit)
    assert store.observation(IV_UNIT_OBSERVATION) == unit
    assert by["India VIX daily (30 days)"].ok and by["NIFTY index daily (30 days)"].ok
    assert by["India VIX first daily candle"].detail.startswith("2026-10-01")
    assert not by["India VIX 1-minute candles 2022-01"].ok  # fake has no 2022 data
    assert len([c for c in src.calls if c[0] == "days"]) <= 6


async def test_smoke_reports_failures_without_raising(store: ResearchStore) -> None:
    class Broken(FakeSource):
        async def get_daily_candles(self, key: str, a: date, b: date) -> list[Candle]:
            raise TokenExpiredError("401")

    lines = await run_smoke(Broken(), store, RCFG, lambda: at(date(2026, 11, 14), 11))
    assert len(lines) == 5 and not any(line.ok for line in lines)
    assert all(line.render().startswith("[FAIL]") for line in lines)
    assert store.observation(IV_UNIT_OBSERVATION) is None
