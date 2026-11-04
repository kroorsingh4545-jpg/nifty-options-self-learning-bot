"""Kill switch: scopes, idempotency, handlers, alerts, audit, reset, restore."""

from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

from core.clock import IST
from core.enums import TradingMode
from data.storage.repositories import Repositories
from monitoring.alerts import MemoryAlertSink
from risk.kill_switch import RESET_EVENT, TRIGGER_EVENT, KillEvent, KillReason, KillSwitch
from tests.fakes import FakeWallClock

NOW = datetime(2026, 11, 3, 11, 0, tzinfo=IST)


def make(paper: Repositories | None = None, wall: FakeWallClock | None = None) -> tuple[KillSwitch, MemoryAlertSink]:
    alerts = MemoryAlertSink()
    ks = KillSwitch(mode=TradingMode.PAPER, repo=paper.risk_events if paper else None, alerts=alerts,
                    now_fn=wall or FakeWallClock(NOW))
    return ks, alerts


def test_global_trigger_blocks_everything() -> None:
    ks, alerts = make()
    assert not ks.is_blocked("orb_options")
    ev = ks.trigger(KillReason.MANUAL, "dashboard button")
    assert ev is not None and ks.active
    assert ks.is_blocked("orb_options") and ks.is_blocked("vwap_ema") and ks.is_blocked(None)
    assert "MANUAL" in (ks.block_reason("vwap_ema") or "")
    assert alerts.alerts[-1].severity.value == "CRITICAL"


def test_strategy_trigger_is_isolated() -> None:
    ks, _ = make()
    ks.trigger(KillReason.STRATEGY_DAILY_LOSS, "loss", strategy_id="orb_options")
    assert ks.is_blocked("orb_options")
    assert not ks.is_blocked("vwap_ema")
    assert not ks.active


def test_idempotent_per_scope() -> None:
    ks, alerts = make()
    assert ks.trigger(KillReason.API_FAILURE, "x") is not None
    assert ks.trigger(KillReason.MANUAL, "y") is None
    assert ks.trigger(KillReason.MANUAL, "z", strategy_id="orb_options") is None  # global already covers it
    assert len(alerts.alerts) == 1


async def test_handlers_run_with_event() -> None:
    ks, _ = make()
    seen: list[KillEvent] = []

    async def handler(ev: KillEvent) -> None:
        await asyncio.sleep(0)
        seen.append(ev)

    ks.add_handler(handler)
    ks.trigger(KillReason.WEBSOCKET_DISCONNECT, "down")
    await ks.wait_idle()
    assert seen and seen[0].reason is KillReason.WEBSOCKET_DISCONNECT


async def test_failing_handler_alerts_but_does_not_unblock() -> None:
    ks, alerts = make()

    async def bad(ev: KillEvent) -> None:
        raise RuntimeError("broker down")

    ks.add_handler(bad)
    ks.trigger(KillReason.MANUAL, "x")
    await ks.wait_idle()
    assert ks.active
    assert any("FAILED" in a.title for a in alerts.alerts)


def test_reset_requires_named_operator(paper: Repositories) -> None:
    ks, _ = make(paper)
    ks.trigger(KillReason.MANUAL, "x")
    with pytest.raises(ValueError):
        ks.reset(operator=" ", note="n")
    assert ks.reset(operator="ops.lead", note="verified flat at broker")
    assert not ks.active
    assert not ks.reset(operator="ops.lead", note="again")
    types = [e.event_type for e in paper.risk_events.recent()]
    assert types[:2] == [RESET_EVENT, TRIGGER_EVENT]


def test_events_are_persisted(paper: Repositories) -> None:
    ks, _ = make(paper)
    ks.trigger(KillReason.RECONCILIATION_MISMATCH, "qty 75 vs 0", strategy_id="vwap_ema")
    (e,) = paper.risk_events.recent()
    assert e.event_type == TRIGGER_EVENT and e.severity.value == "CRITICAL"
    assert e.strategy_id == "vwap_ema" and e.details_json["reason"] == "RECONCILIATION_MISMATCH"


def test_restore_after_restart(paper: Repositories) -> None:
    ks, _ = make(paper)
    ks.trigger(KillReason.GLOBAL_DAILY_LOSS, "loss")
    ks.trigger(KillReason.STRATEGY_DAILY_LOSS, "s", strategy_id="orb_options")  # ignored (global active)
    ks2, _ = make(paper)
    ks2.restore()
    assert ks2.active and ks2.global_event is not None
    assert ks2.global_event.reason is KillReason.GLOBAL_DAILY_LOSS


def test_restore_respects_reset_and_day(paper: Repositories) -> None:
    ks, _ = make(paper)
    ks.trigger(KillReason.MANUAL, "x", strategy_id="vwap_ema")
    ks.reset(operator="ops", note="ok", strategy_id="vwap_ema")
    ks2, _ = make(paper)
    ks2.restore()
    assert not ks2.is_blocked("vwap_ema")
    ks.trigger(KillReason.MANUAL, "y")
    tomorrow, _ = make(paper, FakeWallClock(datetime(2026, 11, 4, 9, 0, tzinfo=IST)))
    tomorrow.restore()
    assert not tomorrow.active  # yesterday's trigger does not carry over


def test_events_are_stamped_with_the_kill_switch_clock(paper: Repositories) -> None:
    clock = FakeWallClock(datetime(2031, 3, 5, 10, 0, tzinfo=IST))  # far from the real date
    ks, _ = make(paper, clock)
    ks.trigger(KillReason.MANUAL, "x", strategy_id="vwap_ema")
    (e,) = paper.risk_events.recent()
    assert e.created_at == clock()
    ks2, _ = make(paper, clock)
    ks2.restore()
    assert ks2.is_blocked("vwap_ema")


def test_trigger_without_loop_still_blocks() -> None:
    ks, _ = make()

    async def handler(ev: KillEvent) -> None:  # pragma: no cover - cannot run without a loop
        pass

    ks.add_handler(handler)
    ks.trigger(KillReason.MANUAL, "sync context")
    assert ks.active
