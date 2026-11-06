"""Core: engine pipeline, event bus, mode controller, scheduler."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import date, datetime, time
from typing import Any

import pytest

from core.engine import TradingEngine
from core.enums import AutonomyLevel, OptionType, TradingMode, UpstoxTokenType
from core.event_bus import EventBus, Topic
from core.interfaces import ContractChoice, EntryResult, FilterDecision, PassThroughFilter
from core.mode_controller import LIVE_CONFIRMATION_PHRASE, LivePreconditions, ModeController
from core.models import Candle, Instrument, Signal, Tick
from core.scheduler import TradingScheduler
from core.state_manager import EngineState
from tests.fakes import FakeWallClock
from tests.synthetic import INDEX, at, candle

INST = Instrument(instrument_key="NSE_FO|1", trading_symbol="NIFTY 24500 CE", segment="NSE_FO",
                  instrument_type="CE", lot_size=75, tick_size=0.05, strike=24500, expiry=date(2026, 11, 10))


def sig(sid: str = "s1", opt: OptionType = OptionType.CE) -> Signal:
    return Signal(signal_id=f"SIG-{sid}", strategy_id=sid, strategy_version="0.1.0", option_type=opt,
                  generated_at=at(10, 0), candle_end=at(10, 0), underlying_key=INDEX, underlying_price=24500,
                  reason="test")


class FakeStrategy:
    def __init__(self, sid: str, *, emit: int = 1, raises: bool = False) -> None:
        self.strategy_id = sid
        self.version = "0.1.0"
        self.emit = emit
        self.raises = raises
        self.results: list[bool] = []
        self.reset_days: list[date] = []

    @property
    def enabled(self) -> bool:
        return True

    def wants(self, candle: Candle) -> bool:
        return True

    def on_candle(self, candle: Candle) -> Sequence[Signal]:
        if self.raises:
            raise RuntimeError("bug in strategy")
        return [sig(self.strategy_id) for _ in range(self.emit)]

    def on_tick(self, tick: Tick) -> Sequence[Signal]:
        return ()

    def on_entry_result(self, signal: Signal, approved: bool) -> None:
        self.results.append(approved)

    def reset_day(self, day: date) -> None:
        self.reset_days.append(day)

    def snapshot(self) -> dict[str, Any]:
        return {}


class Resolver:
    def __init__(self, inst: Instrument | None = INST, raises: bool = False) -> None:
        self.inst, self.raises = inst, raises

    def resolve(self, signal: Signal) -> ContractChoice:
        if self.raises:
            raise RuntimeError("chain down")
        return ContractChoice(self.inst, () if self.inst else ("no strike",))


class Gateway:
    def __init__(self, approve: bool = True) -> None:
        self.approve = approve
        self.calls: list[tuple[Signal, FilterDecision]] = []
        self.active = 0
        self.max_active = 0

    async def submit_entry(self, signal: Signal, instrument: Instrument, decision: FilterDecision) -> EntryResult:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0.001)
        self.active -= 1
        self.calls.append((signal, decision))
        return EntryResult(self.approve, () if self.approve else ("risk says no",), "T-1" if self.approve else None)


class Veto:
    def evaluate(self, signal: Signal) -> FilterDecision:
        return FilterDecision(allow=False, score=0.31, reason="score below 0.60")


class Boom:
    def evaluate(self, signal: Signal) -> FilterDecision:
        raise RuntimeError("model file missing")


def make(strategies: list[FakeStrategy], *, resolver: Resolver | None = None, gateway: Gateway | None = None,
         filt: Any = None, open_: bool = True, wall: FakeWallClock | None = None) -> TradingEngine:
    return TradingEngine(strategies=strategies, resolver=resolver or Resolver(), gateway=gateway or Gateway(),
                         signal_filter=filt, market_hours=lambda _t: open_, now_fn=wall or FakeWallClock(at(10, 0)))


C = candle(INDEX, 5, at(9, 55), 1, 2, 1, 2)


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #


async def test_full_pipeline_approved() -> None:
    s = FakeStrategy("s1")
    gw = Gateway()
    e = make([s], gateway=gw)
    e.start()
    e.on_candle(C)
    await e.drain()
    assert len(gw.calls) == 1 and gw.calls[0][1].size_multiplier == 1.0
    assert s.results == [True]
    snap = e.state.snapshot()["strategies"]["s1"]
    assert snap["signals"] == 1 and snap["approved_entries"] == 1 and snap["last_decision"] == "APPROVED"
    topics = [ev.payload["approved"] for ev in e.bus.recent(Topic.ENTRY_DECISION)]
    assert topics == [True]


async def test_not_running_or_market_closed_ignores_data() -> None:
    gw = Gateway()
    e = make([FakeStrategy("s1")], gateway=gw)
    e.on_candle(C)  # STOPPED
    e.start()
    e.pause()
    e.on_candle(C)  # PAUSED
    closed = make([FakeStrategy("s2")], gateway=gw, open_=False)
    closed.start()
    closed.on_candle(C)
    await e.drain()
    await closed.drain()
    assert gw.calls == []
    assert e.state.engine_state is EngineState.PAUSED


async def test_filter_veto_records_and_informs_strategy() -> None:
    s = FakeStrategy("s1")
    gw = Gateway()
    e = make([s], gateway=gw, filt=Veto())
    e.start()
    e.on_candle(C)
    await e.drain()
    assert gw.calls == [] and s.results == [False]
    rt = e.state.strategy("s1")
    assert rt.filtered_out == 1 and rt.last_ml_score == pytest.approx(0.31)
    assert e.bus.recent(Topic.SIGNAL_FILTERED)


async def test_filter_error_fails_closed() -> None:
    gw = Gateway()
    e = make([FakeStrategy("s1")], gateway=gw, filt=Boom())
    e.start()
    e.on_candle(C)
    await e.drain()
    assert gw.calls == []
    assert "signal filter error" in (e.state.strategy("s1").last_decision or "")


@pytest.mark.parametrize("resolver", [Resolver(inst=None), Resolver(raises=True)])
async def test_no_contract_means_no_entry(resolver: Resolver) -> None:
    gw = Gateway()
    e = make([FakeStrategy("s1")], gateway=gw, resolver=resolver)
    e.start()
    e.on_candle(C)
    await e.drain()
    assert gw.calls == []
    assert e.state.strategy("s1").rejected_entries == 1


async def test_risk_rejection_flows_back() -> None:
    s = FakeStrategy("s1")
    e = make([s], gateway=Gateway(approve=False))
    e.start()
    e.on_candle(C)
    await e.drain()
    assert s.results == [False] and "risk says no" in (e.state.strategy("s1").last_decision or "")


async def test_strategy_exception_isolated_and_disabled_for_day() -> None:
    bad, good = FakeStrategy("bad", raises=True), FakeStrategy("good")
    gw = Gateway()
    e = make([bad, good], gateway=gw)
    e.start()
    e.on_candle(C)
    e.on_candle(C)
    await e.drain()
    assert e.state.strategy("bad").errored == "exception in on_candle"
    assert [c[0].strategy_id for c in gw.calls] == ["good", "good"]
    assert e.bus.recent(Topic.STRATEGY_ERROR)
    e.new_day(date(2026, 11, 4))
    assert e.state.strategy("bad").errored is None  # re-enabled next day
    assert bad.reset_days == [date(2026, 11, 4)]


async def test_entries_are_serialised() -> None:
    gw = Gateway()
    e = make([FakeStrategy("a", emit=3), FakeStrategy("b", emit=3)], gateway=gw)
    e.start()
    e.on_candle(C)
    await e.drain()
    assert len(gw.calls) == 6 and gw.max_active == 1


async def test_pause_mid_queue_stops_remaining_entries() -> None:
    gw = Gateway()
    e = make([FakeStrategy("a", emit=3)], gateway=gw)
    e.start()
    e.on_candle(C)
    e.pause()
    await e.drain()
    assert gw.calls == []


async def test_signal_for_other_strategy_rejected() -> None:
    e = make([FakeStrategy("a")])
    with pytest.raises(ValueError):
        await e.process_signal(FakeStrategy("a"), sig("b"))


def test_duplicate_strategy_ids() -> None:
    with pytest.raises(ValueError):
        make([FakeStrategy("a"), FakeStrategy("a")])


def test_filter_decision_bounds() -> None:
    with pytest.raises(ValueError):
        FilterDecision(allow=True, size_multiplier=1.5)  # ML can never enlarge size
    with pytest.raises(ValueError):
        FilterDecision(allow=True, score=1.2)
    assert PassThroughFilter().evaluate(sig()).allow


# --------------------------------------------------------------------------- #
# Event bus
# --------------------------------------------------------------------------- #


async def test_event_bus_sync_async_and_error_isolation() -> None:
    bus = EventBus()
    got: list[Any] = []

    async def async_handler(ev: Any) -> None:
        got.append(("async", ev.payload))

    def bad(ev: Any) -> None:
        raise RuntimeError("x")

    bus.subscribe(Topic.SIGNAL, bad)
    bus.subscribe(Topic.SIGNAL, lambda ev: got.append(("sync", ev.payload)))
    unsub = bus.subscribe(Topic.SIGNAL, async_handler)
    bus.publish(Topic.SIGNAL, 1)
    await bus.drain()
    assert ("sync", 1) in got and ("async", 1) in got and bus.errors == 1
    unsub()
    bus.publish(Topic.SIGNAL, 2)
    await bus.drain()
    assert ("async", 2) not in got
    assert [e.payload for e in bus.recent(Topic.SIGNAL)] == [1, 2]


# --------------------------------------------------------------------------- #
# Mode controller
# --------------------------------------------------------------------------- #

ALL_OK = LivePreconditions(reconciliation_ok=True, kill_switch_armed=True, kill_switch_active=False,
                           loss_limits_configured=True, paper_validation_passed=True, approved_by="ops.lead",
                           pending_orders=0, broker_connected=True)


def test_default_paper_and_live_always_refused_in_this_build() -> None:
    m = ModeController()
    assert m.mode is TradingMode.PAPER and m.autonomy is AutonomyLevel.PAPER_AUTO_IMPROVE
    r = m.request_live(LIVE_CONFIRMATION_PHRASE, ALL_OK)
    assert not r.approved and m.mode is TradingMode.PAPER
    assert r.reasons == ("LIVE order routing is not available in this build",
                         "the Upstox Analytics Token is read-only and cannot place orders")  # default token type
    daily = ModeController(token_type=UpstoxTokenType.DAILY).request_live(LIVE_CONFIRMATION_PHRASE, ALL_OK)
    assert daily.reasons == ("LIVE order routing is not available in this build",)


def test_live_request_lists_every_unmet_precondition() -> None:
    m = ModeController()
    bad = LivePreconditions(reconciliation_ok=False, kill_switch_armed=False, kill_switch_active=True,
                            loss_limits_configured=False, paper_validation_passed=False, approved_by=None,
                            pending_orders=2, broker_connected=False)
    r = m.request_live("enable live trading", bad)  # wrong case
    text = " | ".join(r.reasons)
    for needle in ("type exactly", "broker not connected", "reconciliation", "2 pending", "not armed",
                   "kill switch is active", "loss limits", "PAPER validation", "human approval", "not available"):
        assert needle in text
    assert m.mode is TradingMode.PAPER


def test_paper_request_and_autonomy() -> None:
    m = ModeController()
    assert m.request_paper().approved
    m.set_autonomy(AutonomyLevel.OBSERVE_ONLY)
    assert m.autonomy is AutonomyLevel.OBSERVE_ONLY


# --------------------------------------------------------------------------- #
# Scheduler
# --------------------------------------------------------------------------- #


async def test_scheduler_registers_and_runs_jobs() -> None:
    sch = TradingScheduler()
    calls: list[str] = []

    async def job_async() -> None:
        calls.append("async")

    def boom() -> None:
        raise RuntimeError("job failed")

    sch.every("flush", 1, lambda: calls.append("sync"))
    sch.every("reconcile", 300, job_async)
    sch.daily("new_day", time(9, 0), boom)
    with pytest.raises(ValueError):
        sch.every("flush", 5, lambda: None)
    await sch.run_now("flush")
    await sch.run_now("reconcile")
    await sch.run_now("new_day")  # failure is logged, not raised
    assert calls == ["sync", "async"]
    assert sch.jobs["reconcile"].every_seconds == 300 and sch.jobs["new_day"].at == time(9, 0)
    sch.start()
    assert sch.running
    sch.shutdown()
    await asyncio.sleep(0)


def test_market_session_holidays_and_weekends() -> None:
    from config.settings import MarketSession

    s = MarketSession(holidays=(date(2026, 11, 9),))
    assert s.is_open(datetime(2026, 11, 3, 10, 0, tzinfo=at(10, 0).tzinfo))
    assert not s.is_open(datetime(2026, 11, 3, 15, 30, tzinfo=at(10, 0).tzinfo))
    assert not s.is_open(datetime(2026, 11, 7, 10, 0, tzinfo=at(10, 0).tzinfo))  # Saturday
    assert not s.is_open(datetime(2026, 11, 9, 10, 0, tzinfo=at(10, 0).tzinfo))  # holiday
