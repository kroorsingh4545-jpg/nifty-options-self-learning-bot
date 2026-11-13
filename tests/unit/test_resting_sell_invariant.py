"""Resting-SELL invariant (Phase 8 audit warnings 1 and 2).

For every trade, at every order event: the unfilled quantity of its LIVE sell
orders (protective SL-M + exit orders) never exceeds the quantity held, so a
stop, an exit or a retry can never sell more than the position (a short
option). Checked through partial exits, failed cancels, exit retries and an
in-flight protective-stop placement.

The PAPER broker normally refuses a sell beyond the long position itself
(``allow_short_options: false``), which would hide a bug here. A real exchange
does not, so these tests run the paper broker with ``allow_short_options=True``:
the execution layer alone must keep the invariant.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pytest

from broker.errors import BrokerError
from core.enums import OrderPurpose, OrderSide, OrderStatus, OrderType, TradeStatus
from core.interfaces import TradeAdvice, TradeAdviceAction
from data.storage.records import OrderRecord
from execution.order_manager import TERMINAL
from execution.position_manager import ManagedTrade
from risk.kill_switch import KillReason
from risk.stop_loss_manager import ExitReason
from tests.fakes import with_per_trade_risk
from tests.stack_harness import CE, Harness
from tests.unit.test_rl_inference import rl_cfg

LOT = CE.lot_size


def mk(**paper: Any) -> Harness:
    """PAPER stack whose broker, like a real exchange, does NOT block oversells."""
    return Harness(cfg=with_per_trade_risk(rl_cfg("orb_options", stop_pct=10.0), 2_000), allow_short_options=True,
                   **paper)


@dataclass
class Monitor:
    """Asserts the invariant after every order event (and on demand)."""

    h: Harness
    violations: list[str] = field(default_factory=list)
    events: int = 0

    def __post_init__(self) -> None:
        self.h.s.orders.add_listener(self.on_event)

    def on_event(self, rec: OrderRecord, _: Any) -> None:
        self.events += 1
        self.check(f"after {rec.purpose.value} {rec.status.value}")

    def check(self, where: str = "") -> None:
        pm = self.h.s.positions
        for t in pm.trades.values():
            live = pm.resting_sell_qty(t)
            if live > max(0, t.open_qty):
                self.violations.append(f"{where}: live SELL {live} > held {t.open_qty} ({t.trade_uid})")

    async def broker_never_short(self) -> None:
        for p in await self.h.broker.get_positions():
            assert p.quantity >= 0, f"broker position went short: {p}"

    def assert_ok(self) -> None:
        self.check("final")
        assert not self.violations, self.violations


def break_cancel(h: Harness, pred: Callable[[OrderRecord], bool]) -> Callable[[], None]:
    """Make the broker refuse to cancel matching orders (they stay live)."""
    original = h.broker.cancel_order

    async def cancel(coid: str) -> Any:
        rec = h.s.orders.get(coid)
        if rec is not None and pred(rec):
            raise BrokerError("cancel rejected (test)")
        return await original(coid)

    h.broker.cancel_order = cancel  # type: ignore[method-assign]
    return lambda: setattr(h.broker, "cancel_order", original)


def is_stop(rec: OrderRecord) -> bool:
    return rec.purpose is OrderPurpose.STOP_LOSS


def sells(h: Harness, t: ManagedTrade, *, order_type: OrderType | None = None) -> list[OrderRecord]:
    return [o for o in h.repos.orders.list_for_trade(t.trade_uid) if o.side is OrderSide.SELL
            and (order_type is None or o.order_type is order_type)]


def live_stops(h: Harness, t: ManagedTrade) -> list[OrderRecord]:
    return [o for o in sells(h, t, order_type=OrderType.SL_M) if o.status not in TERMINAL]


def partial(t: ManagedTrade, lots: int = 1) -> TradeAdvice:
    return TradeAdvice(t.trade_uid, t.strategy_id, TradeAdviceAction.TAKE_PARTIAL_PROFIT, exit_lots=lots)


async def opened(h: Harness, ltp: float = 100.0) -> ManagedTrade:
    t = await h.enter(ltp=ltp)
    assert t.open_qty == 2 * LOT and t.protective_order_id is not None
    return t


# --------------------------------------------------------------------------- #
# Warning 1: a stop that cannot be cancelled
# --------------------------------------------------------------------------- #


async def test_partial_exit_is_abandoned_when_the_stop_cannot_be_cancelled() -> None:
    h = mk()
    mon = Monitor(h)
    t = await opened(h)
    h.tick(CE.instrument_key, 120.0)
    await h.settle()
    restore = break_cancel(h, is_stop)
    assert h.s.positions.apply_advice(partial(t)).applied
    await h.settle()
    assert sells(h, t, order_type=OrderType.MARKET) == []  # nothing sold
    assert t.open_qty == 2 * LOT and t.partial_exits == 0 and not t.exit_in_progress
    stops = live_stops(h, t)
    assert len(stops) == 1 and stops[0].quantity == 2 * LOT and t.protective_order_id == stops[0].client_order_id
    restore()
    h.tick(CE.instrument_key, t.stop - 1)  # the untouched stop still protects the whole position
    await h.settle()
    assert t.status is TradeStatus.CLOSED and t.open_qty == 0
    mon.assert_ok()
    await mon.broker_never_short()


async def test_full_exit_is_not_sent_when_the_stop_cannot_be_cancelled() -> None:
    h = mk()
    mon = Monitor(h)
    t = await opened(h)
    break_cancel(h, is_stop)
    ok = await h.s.positions.exit_trade(t.trade_uid, ExitReason.MANUAL)
    await h.settle()
    assert ok is False
    assert sells(h, t, order_type=OrderType.MARKET) == []  # an exit now could sell 2x the position
    assert len(live_stops(h, t)) == 1 and t.protective_order_id is not None  # still protected, still tracked
    assert h.s.kill_switch.active and h.s.kill_switch.global_event is not None
    assert h.s.kill_switch.global_event.reason is KillReason.EXIT_FAILURE
    assert any("EXIT FAILED" in a.title for a in h.s.alerts.alerts)
    h.tick(CE.instrument_key, t.stop - 1)  # the resting stop exits the position by itself
    await h.settle()
    assert t.open_qty == 0
    mon.assert_ok()
    await mon.broker_never_short()


async def test_stop_filled_during_the_cancel_is_never_sold_twice() -> None:
    """The cancel races with the stop triggering: the exit must only sell what is left."""
    h = mk()
    mon = Monitor(h)
    t = await opened(h)
    original = h.broker.cancel_order

    async def trigger_then_cancel(coid: str) -> Any:
        if h.s.orders.get(coid) is not None and is_stop(h.s.orders.get(coid)):  # type: ignore[arg-type]
            h.tick(CE.instrument_key, t.stop - 1)  # the stop fills first
        return await original(coid)

    h.broker.cancel_order = trigger_then_cancel  # type: ignore[method-assign]
    await h.s.positions.exit_trade(t.trade_uid, ExitReason.MANUAL)
    await h.settle()
    assert t.open_qty == 0 and t.status is TradeStatus.CLOSED
    sold = sum(o.filled_quantity for o in sells(h, t))
    assert sold == 2 * LOT
    mon.assert_ok()
    await mon.broker_never_short()


async def test_a_stop_left_on_a_closed_trade_escalates() -> None:
    h = mk()
    t = await opened(h)
    stop_id = t.protective_order_id
    assert stop_id is not None
    break_cancel(h, is_stop)
    # A fill closes the trade while the stop is still resting (e.g. a manual exit at the broker).
    h.s.positions._apply_exit_fill(t, t.open_qty, 101.0, 0.0, OrderPurpose.EXIT)
    await h.settle()
    assert t.status is TradeStatus.CLOSED
    assert h.s.kill_switch.active  # the resting stop could short a flat position: escalated
    assert any("EXIT FAILED" in a.title for a in h.s.alerts.alerts)


# --------------------------------------------------------------------------- #
# Warning 2: an in-flight protective-stop placement
# --------------------------------------------------------------------------- #


async def test_partial_exit_waits_for_an_inflight_protective_placement() -> None:
    """Entry fill -> protective placement is spawned but has not run when the
    partial is requested. The partial waits for it, cancels it, sells one lot
    and re-protects the rest."""
    h = mk()
    mon = Monitor(h)
    h.tick(CE.instrument_key, 100.0)
    d = await h.decide()
    t = await h.s.positions.open_trade(d, strategy_version="0.1.0")  # no settle: placement task pending
    assert t.entry_complete and t.protective_order_id is None and t.tasks
    h.tick(CE.instrument_key, 120.0)
    assert h.s.positions.apply_advice(partial(t)).applied
    await h.settle()
    assert t.open_qty == LOT and t.partial_exits == 1 and t.status is TradeStatus.OPEN
    stops = live_stops(h, t)
    assert len(stops) == 1 and stops[0].quantity == LOT
    mon.assert_ok()
    await mon.broker_never_short()


async def test_full_exit_waits_for_a_slow_protective_placement() -> None:
    """The placement is mid-submit (the broker is slow) when an exit starts."""
    h = mk()
    mon = Monitor(h)
    gate = asyncio.Event()
    original = h.broker.place_order

    async def slow(request: Any) -> Any:
        if request.order_type is OrderType.SL_M:
            await gate.wait()
        return await original(request)

    h.broker.place_order = slow  # type: ignore[method-assign]
    h.tick(CE.instrument_key, 100.0)
    d = await h.decide()
    t = await h.s.positions.open_trade(d, strategy_version="0.1.0")
    await asyncio.sleep(0)  # the placement task starts and blocks inside submit
    assert t.protect_lock.locked()
    h.tick(CE.instrument_key, 120.0)
    r = h.s.positions.apply_advice(partial(t))
    assert not r.applied and "busy" in r.reason
    exit_task = asyncio.ensure_future(h.s.positions.exit_trade(t.trade_uid, ExitReason.MANUAL))
    await asyncio.sleep(0)
    assert sells(h, t, order_type=OrderType.MARKET) == []  # the exit waits for the placement
    gate.set()
    assert await exit_task is True
    await h.settle()
    assert t.open_qty == 0 and live_stops(h, t) == []
    mon.assert_ok()
    await mon.broker_never_short()


async def test_stop_fill_between_partial_request_and_run_sells_nothing() -> None:
    """Found by the randomised test: the partial is accepted, then the resting
    stop fills the whole position before the partial task runs. The partial
    must sell nothing (it used to sell 75 repeatedly on a flat position)."""
    h = mk()
    mon = Monitor(h)
    t = await opened(h)
    h.tick(CE.instrument_key, 120.0)
    await h.settle()
    assert h.s.positions.apply_advice(partial(t)).applied  # task spawned, not yet run
    h.tick(CE.instrument_key, t.stop - 1)  # the broker fills the resting SL-M for both lots first
    await h.settle()
    assert t.status is TradeStatus.CLOSED and t.open_qty == 0
    assert sells(h, t, order_type=OrderType.MARKET) == []
    assert not h.s.kill_switch.active
    mon.assert_ok()
    await mon.broker_never_short()


async def test_an_oversell_fill_escalates() -> None:
    h = mk()
    t = await opened(h)
    h.s.positions._apply_exit_fill(t, t.open_qty + LOT, 101.0, 0.0, OrderPurpose.EXIT)
    assert h.s.kill_switch.active
    assert any("SHORT" in a.message for a in h.s.alerts.alerts)


# --------------------------------------------------------------------------- #
# Retries
# --------------------------------------------------------------------------- #


async def test_exit_retry_never_resends_while_the_previous_order_may_be_live() -> None:
    """Thin book: the exit fills one lot and the rest stays working; the
    broker then refuses to cancel it. No second exit order may be sent."""
    h = mk(simulate_partial_fills=True)
    mon = Monitor(h)
    t = await opened(h)
    h.tick(CE.instrument_key, 101.0, qty=LOT)  # one lot of depth on each side
    break_cancel(h, lambda r: r.purpose is not OrderPurpose.STOP_LOSS)
    ok = await h.s.positions.exit_trade(t.trade_uid, ExitReason.MANUAL)
    await h.settle()
    exits = sells(h, t, order_type=OrderType.MARKET)
    assert len(exits) == 1, [(o.quantity, o.status) for o in exits]  # never re-sent
    assert ok is False and h.s.kill_switch.active
    assert exits[0].filled_quantity == LOT and exits[0].status in (OrderStatus.PARTIALLY_FILLED, OrderStatus.OPEN)
    assert t.open_qty == LOT
    assert live_stops(h, t) == []  # the working exit order already covers the lot held: no extra stop
    mon.assert_ok()
    await mon.broker_never_short()


async def test_protective_stop_is_sized_to_the_unreserved_quantity() -> None:
    h = mk(simulate_partial_fills=True)
    mon = Monitor(h)
    t = await opened(h)
    h.tick(CE.instrument_key, 101.0, qty=LOT)
    break_cancel(h, lambda r: r.purpose is not OrderPurpose.STOP_LOSS)
    await h.s.positions.exit_trade(t.trade_uid, ExitReason.MANUAL)  # one lot left working
    await h.settle()
    # a later re-protection must not cover the lot already on the working exit order
    t.exit_in_progress = False
    await h.s.positions._place_protective_stop(t)
    assert h.s.positions.sellable_qty(t) == 0 and live_stops(h, t) == []
    mon.assert_ok()


# --------------------------------------------------------------------------- #
# Randomised: any mix of ticks, advice, kills and refused cancels
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("seed", range(100))
async def test_invariant_under_random_operations(seed: int) -> None:
    rng = random.Random(seed)  # noqa: S311
    h = mk(simulate_partial_fills=rng.random() < 0.5)
    mon = Monitor(h)
    t = await opened(h)
    restore: Callable[[], None] | None = None
    price = 115.0  # start in profit so partial exits are reachable
    h.tick(CE.instrument_key, price)
    await h.settle()
    for _ in range(40):
        op = rng.random()
        if op < 0.35:
            price = max(5.0, round(price + rng.uniform(-4, 6), 1))
            h.tick(CE.instrument_key, price, qty=rng.choice([LOT, 10 * LOT]))
        elif op < 0.50:
            h.s.positions.apply_advice(partial(t))
        elif op < 0.60:
            h.s.positions.apply_advice(TradeAdvice(t.trade_uid, t.strategy_id, TradeAdviceAction.TRAIL_STOP,
                                                   new_stop=max(0.05, round(price - rng.uniform(0.5, 8), 1))))
        elif op < 0.68:
            h.s.positions.apply_advice(TradeAdvice(t.trade_uid, t.strategy_id, TradeAdviceAction.EXIT_FULL))
        elif op < 0.80:
            if restore is None:
                restore = break_cancel(h, (lambda r: True) if rng.random() < 0.5 else is_stop)
            else:
                restore()
                restore = None
        elif op < 0.84:
            h.s.kill_switch.trigger(KillReason.MANUAL, "random")
        if rng.random() < 0.7:
            await h.settle()
        mon.check(f"seed {seed}")
        await mon.broker_never_short()
        if t.status is not TradeStatus.OPEN:
            break
    if restore is not None:
        restore()
    await h.settle()
    mon.assert_ok()
    await mon.broker_never_short()
    assert mon.events > 0
