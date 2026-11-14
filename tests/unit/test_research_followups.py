"""R2 follow-ups: live-forecast safety margin, evening guard, today's daily
candle, ``--to today`` and the daily research script (synthetic fixtures)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pytest

from core.clock import IST
from core.models import Candle
from research.__main__ import _to_date
from research.collect.history import download_daily
from research.config import ForecastSection, load_research_config
from research.forecast.build import live_deadline
from research.forecast.run import run_build
from research.store import ResearchStore
from tests.unit.test_research_forecast import _fill_store, weekdays

RCFG = load_research_config()
VIX = RCFG.instruments.india_vix_key
ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "daily-research.bat"
HISTORY = weekdays(date(2023, 1, 2), date(2026, 11, 13))  # last session: Friday 13 Nov 2026


@pytest.fixture
def store(tmp_path: Path) -> Iterator[ResearchStore]:
    s = ResearchStore.open(tmp_path / "research.db")
    _fill_store(s, HISTORY)
    yield s
    s.close()


def at(d: date, hh: int, mm: int, ss: int = 0) -> datetime:
    return datetime.combine(d, time(hh, mm, ss), tzinfo=IST)


MONDAY = date(2026, 11, 16)


# --------------------------------------------------------------------------- #
# warning 1: safety margin before 09:15
# --------------------------------------------------------------------------- #
def test_margin_config_defaults_and_bounds() -> None:
    assert RCFG.forecast.live_margin_minutes == 5
    assert live_deadline(MONDAY, 5) == at(MONDAY, 9, 10)
    for bad in (0, 61):
        with pytest.raises(ValueError):
            ForecastSection(live_margin_minutes=bad)


@pytest.mark.parametrize(("now", "live"), [
    (at(date(2026, 11, 14), 18, 0), True),   # Saturday evening
    (at(MONDAY, 9, 9, 59), True),            # 5 min 1 s before the open
    (at(MONDAY, 9, 10, 0), False),           # inside the 5-minute margin
    (at(MONDAY, 9, 14, 59), False),          # a clock 7 s fast would have called this "before the open"
    (at(MONDAY, 9, 15, 0), False),
])
def test_live_label_needs_the_safety_margin(store: ResearchStore, now: datetime, live: bool) -> None:
    s = run_build(store, RCFG, configured_holidays=[], now=now)
    assert s.next_session == MONDAY and s.next_recorded_live is live
    row = store.con.execute("SELECT recorded_live FROM vol_forecasts WHERE session_date = ?",
                            (MONDAY.isoformat(),)).fetchone()
    assert row[0] == int(live)


def test_a_wider_margin_is_respected(store: ResearchStore) -> None:
    cfg = RCFG.model_copy(update={"forecast": RCFG.forecast.model_copy(update={"live_margin_minutes": 30})})
    assert not run_build(store, cfg, configured_holidays=[], now=at(MONDAY, 8, 50)).next_recorded_live


# --------------------------------------------------------------------------- #
# evening guard: today's candle missing after the close
# --------------------------------------------------------------------------- #
def test_evening_run_without_todays_candle_makes_no_next_forecast(store: ResearchStore) -> None:
    s = run_build(store, RCFG, configured_holidays=[], now=at(MONDAY, 16, 0))
    assert s.next_session is None
    assert any("not in the history yet" in w for w in s.warnings)
    assert store.con.execute("SELECT COUNT(*) FROM vol_forecasts WHERE session_date >= ?",
                             (MONDAY.isoformat(),)).fetchone()[0] == 0


def test_a_run_during_the_session_still_writes_a_non_live_row(store: ResearchStore) -> None:
    s = run_build(store, RCFG, configured_holidays=[], now=at(MONDAY, 12, 0))
    assert s.next_session == MONDAY and not s.next_recorded_live and not s.warnings


# --------------------------------------------------------------------------- #
# today's daily candle (Upstox marks it incomplete until midnight)
# --------------------------------------------------------------------------- #
class TodaySource:
    def __init__(self, day: date) -> None:
        self.day = day

    async def get_daily_candles(self, key: str, a: date, b: date) -> list[Candle]:
        start = datetime.combine(self.day, time(0), tzinfo=IST)
        return [Candle(instrument_key=key, timeframe_minutes=1440, start=start, end=start + timedelta(days=1),
                       open=15.0, high=16.0, low=14.5, close=15.5, complete=False)]


@pytest.mark.parametrize(("hh", "mm", "kept"), [(15, 39, False), (15, 40, True), (18, 0, True)])
async def test_todays_daily_candle_is_final_only_after_1540(tmp_path: Path, hh: int, mm: int, kept: bool) -> None:
    s = ResearchStore.open(tmp_path / "r.db")
    src = TodaySource(MONDAY)
    summary = await download_daily(src, s, VIX, start=date(2025, 1, 1), end=MONDAY, chunk_years=5,  # type: ignore[arg-type]
                                   now=lambda: at(MONDAY, hh, mm), final_after=RCFG.forecast.daily_final_after)
    assert summary.rows == int(kept)
    assert (MONDAY in s.daily_sessions(VIX)) is kept
    s.close()


async def test_an_old_incomplete_candle_is_never_accepted(tmp_path: Path) -> None:
    s = ResearchStore.open(tmp_path / "r.db")
    src = TodaySource(MONDAY - timedelta(days=3))  # Friday's candle still flagged incomplete
    summary = await download_daily(src, s, VIX, start=date(2025, 1, 1), end=MONDAY, chunk_years=5,  # type: ignore[arg-type]
                                   now=lambda: at(MONDAY, 18, 0), final_after=RCFG.forecast.daily_final_after)
    assert summary.rows == 0
    s.close()


def test_to_date_accepts_today() -> None:
    from core.clock import now_ist

    assert _to_date("today") == now_ist().date() and _to_date(" TODAY ") == now_ist().date()
    assert _to_date("2026-11-13") == date(2026, 11, 13)
    with pytest.raises(ValueError):
        _to_date("tomorrow")


# --------------------------------------------------------------------------- #
# expiry rules: confirmed
# --------------------------------------------------------------------------- #
def test_expiry_rules_cite_the_confirming_circular() -> None:
    assert all("103/2025" in r.source for r in RCFG.weekly_expiry_rules)


# --------------------------------------------------------------------------- #
# scripts/daily-research.bat
# --------------------------------------------------------------------------- #
def test_daily_research_script_runs_only_research_commands() -> None:
    text = SCRIPT.read_bytes().decode("utf-8")
    steps = ["-m research history --to today", "-m research summarize-quotes", "-m research chain-snapshot",
             "-m research forecast", "-m research report"]
    positions = [text.index(s) for s in steps]
    assert positions == sorted(positions)  # snapshot before forecast: tomorrow's forecast can use its ATM IV
    lowered = text.lower()
    for forbidden in ("-m runtime", "place_order", "trading_mode", ".env", "del ", "rmdir", "--during-session"):
        assert forbidden not in lowered, forbidden
    assert "pause" in lowered  # the double-clicked window stays open


def test_daily_research_script_keeps_crlf_line_endings() -> None:
    raw = SCRIPT.read_bytes()
    assert b"\r\n" in raw and raw.count(b"\n") == raw.count(b"\r\n")  # batch labels/goto break with LF
