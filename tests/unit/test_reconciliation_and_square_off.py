"""Reconciliation (startup / order update / periodic / square-off), square-off
window behaviour, and restart recovery."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from broker.errors import BrokerError, BrokerTimeoutError
from core.enums import OrderPurpose, OrderSide, OrderStatus, OrderType, StrategyStatus, TradeStatus, TradingMode
from core.ids import new_client_order_id
from core.models import OrderRequest
from data.storage.database import Database
from execution.order_manager import OrderIntent
from execution.square_off import SessionPhase
from risk.kill_switch import KillReason
from tests.stack_harness import CE, PE, Harness, at

# --------------------------------------------------------------------------- #
# Reconciliation
# --------------------------------------------------------------------------- #


async def test_clean_reconciliation() -> None:
    h = Harness()
    await h.enter("orb_options", CE)
    await h.enter("vwap_ema", CE)  # same contract, two strategies -> broker sees the sum
    rep = await h.s.reconciler.reconcile("startup")
    assert rep.ok and rep.mismatches == []
    assert h.s.positions.net_quantities() == {CE.instrument_key: 150}
    assert not h.s.kill_switch.active


async def test_position_mismatch_triggers_kill_switch() -> None:
    h = Harness()
    await h.enter("orb_options", CE)
    # An order placed at the broker outside the bot (e.g. manually in the app).
    h.tick(PE.instrument_key, 100.0)
    await h.broker.place_order(OrderRequest(
        client_order_id=new_client_order_id(TradingMode.PAPER, "manual"), mode=TradingMode.PAPER,
        strategy_id="manual", instrument_key=PE.instrument_key, side=OrderSide.BUY, quantity=75,
        order_type=OrderType.MARKET))
    rep = await h.s.reconciler.reconcile("periodic")
    assert not rep.ok
    kinds = {m.kind for m in rep.mismatches}
    assert "POSITION_QTY" in kinds
    ks = h.s.kill_switch
    assert ks.global_event is not None and ks.global_event.reason is KillReason.RECONCILIATION_MISMATCH
    assert any(e.event_type == "RECONCILIATION_MISMATCH" for e in h.repos.risk_events.recent())
    await h.settle()


async def test_reconcile_requested_after_order_updates_is_debounced() -> None:
    h = Harness()
    await h.enter()
    before = len(h.s.reconciler.history)
    assert before >= 1  # entry fills requested a reconciliation
    h.tick(CE.instrument_key, 101.0)
    await h.settle()
    assert all(r.trigger == "order_update" for r in h.s.reconciler.history)
    # many updates -> far fewer reconciliations
    n_orders = len(h.repos.orders.list_for_strategy("orb_options"))
    assert len(h.s.reconciler.history) <= n_orders + 1


async def test_periodic_reconciliation_loop() -> None:
    h = Harness()
    stop = asyncio.Event()
    task = asyncio.create_task(h.s.reconciler.run_periodic(stop))
    for _ in range(20):
        await asyncio.sleep(0)
    stop.set()
    await asyncio.wait_for(task, 1)
    assert any(r.trigger == "periodic" for r in h.s.reconciler.history)
    assert h.cfg.settings.execution.reconcile_interval_seconds <= 300


class UnreachableBroker:
    def __init__(self, inner: Any) -> None:
        self.inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    async def get_positions(self) -> Any:
        raise BrokerError("connection reset")


async def test_unreachable_broker_counts_api_failures() -> None:
    h = Harness()
    h.s.reconciler.broker = UnreachableBroker(h.broker)  # type: ignore[assignment]
    for _ in range(2):
        rep = await h.s.reconciler.reconcile("periodic")
        assert not rep.reachable
    assert not h.s.kill_switch.active
    await h.s.reconciler.reconcile("periodic")
    assert h.s.kill_switch.global_event is not None
    assert h.s.kill_switch.global_event.reason is KillReason.API_FAILURE
    await h.settle()


async def test_unknown_order_is_resolved_not_resent() -> None:
    h = Harness()
    h.tick(CE.instrument_key, 100.0)
    real_place = h.broker.place_order
    listeners = h.broker._listeners

    async def place_then_timeout(req: OrderRequest) -> Any:
        h.broker._listeners = []  # the response (and its updates) are "lost"
        try:
            await real_place(req)
        finally:
            h.broker._listeners = listeners
        raise BrokerTimeoutError("read timeout")

    h.broker.place_order = place_then_timeout  # type: ignore[method-assign]
    rec = await h.s.orders.submit(OrderIntent("orb_options", CE, OrderSide.BUY, 75, OrderType.MARKET,
                                              OrderPurpose.ENTRY))
    assert rec.status is OrderStatus.UNKNOWN
    h.broker.place_order = real_place  # type: ignore[method-assign]
    rep = await h.s.reconciler.reconcile("startup")
    assert rec.client_order_id in rep.resolved_unknown
    resolved = h.s.orders.get(rec.client_order_id)
    assert resolved is not None and resolved.status is OrderStatus.COMPLETE
    assert resolved.filled_quantity == 75
    fills = h.repos.fills.list_for_order(resolved.id)  # type: ignore[arg-type]
    assert len(fills) == 1 and fills[0].paper_fill_id and fills[0].paper_fill_id.startswith("RECON-")
    # Recovered fill carries the real charges (identical to what the broker charged).
    assert fills[0].charges > 20
    assert fills[0].charges == pytest.approx(h.broker.charges_paid, abs=0.01)
    # exactly one order exists at the broker: it was never re-sent
    assert len(await h.broker.get_orders()) == 1
    await h.settle()


async def test_timeout_never_regresses_a_reported_order() -> None:
    h = Harness()
    h.tick(CE.instrument_key, 100.0)
    real_place = h.broker.place_order

    async def place_then_timeout(req: OrderRequest) -> Any:
        await real_place(req)  # updates DO arrive (COMPLETE) before the timeout
        raise BrokerTimeoutError("late")

    h.broker.place_order = place_then_timeout  # type: ignore[method-assign]
    rec = await h.s.orders.submit(OrderIntent("orb_options", CE, OrderSide.BUY, 75, OrderType.MARKET,
                                              OrderPurpose.ENTRY))
    assert rec.status is OrderStatus.COMPLETE
    await h.settle()


# --------------------------------------------------------------------------- #
# Square-off
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("hm", "phase"), [((9, 0), SessionPhase.PRE_OPEN), ((10, 0), SessionPhase.TRADING),
                                           ((14, 45), SessionPhase.NO_NEW_ENTRIES),
                                           ((15, 10), SessionPhase.SQUARE_OFF), ((15, 19), SessionPhase.SQUARE_OFF),
                                           ((15, 20), SessionPhase.CLOSED)])
def test_session_phases(hm: tuple[int, int], phase: SessionPhase) -> None:
    h = Harness(at(*hm))
    assert h.s.square_off.phase() is phase


async def test_no_square_off_before_window() -> None:
    h = Harness(at(10, 0))
    t = await h.enter()
    h.wall.now = at(15, 9, 59)
    await h.s.square_off.check()
    assert t.status is TradeStatus.OPEN
    assert h.s.square_off.seconds_to_square_off() == 1


async def test_square_off_flattens_everything_with_reconciliation() -> None:
    h = Harness(at(10, 0))
    a = await h.enter("orb_options", CE)
    b = await h.enter("vwap_ema", PE)
    h.tick(CE.instrument_key, 100.0)
    d = await h.decide("vwap_ema", CE)
    h.tick(CE.instrument_key, 101.0)
    resting = await h.s.positions.open_trade(d, strategy_version="0.1.0")
    h.wall.now = at(15, 10)
    h.tick(CE.instrument_key, 101.0)
    h.tick(PE.instrument_key, 99.0)
    phase = await h.s.square_off.check()
    await h.settle()
    assert phase is SessionPhase.SQUARE_OFF
    assert a.status is TradeStatus.CLOSED and b.status is TradeStatus.CLOSED
    assert resting.status is TradeStatus.CANCELLED
    assert h.repos.orders.list_open() == []
    assert h.s.positions.net_quantities() == {}
    triggers = [r.trigger for r in h.s.reconciler.history]
    assert "pre_square_off" in triggers and "post_square_off" in triggers
    assert h.s.square_off.completed_for == at(15, 10).date()
    rec = h.repos.trades.get(a.trade_uid)
    assert rec is not None and rec.exit_reason == "SQUARE_OFF"
    assert not h.s.kill_switch.active


async def test_square_off_incomplete_escalates_at_window_end() -> None:
    h = Harness(at(10, 0))
    await h.enter()
    h.wall.now = at(15, 10)  # quotes now stale -> exits are rejected by the paper broker
    await h.s.square_off.check()
    await h.settle()
    assert h.s.positions.net_quantities()  # still open
    h.wall.now = at(15, 20)
    await h.s.square_off.check()
    await h.settle()
    assert h.s.kill_switch.active
    assert any("SQUARE-OFF INCOMPLETE" in a.title or "EXIT FAILED" in a.title for a in h.s.alerts.alerts)


async def test_square_off_retries_until_flat() -> None:
    h = Harness(at(10, 0))
    t = await h.enter()
    h.wall.now = at(15, 10)
    await h.s.square_off.check()  # stale quote: cannot exit yet
    await h.settle()
    # The failed exits escalated to the kill switch; an operator resets it after checking.
    assert h.s.kill_switch.active
    h.s.kill_switch.reset(operator="test", note="verified; allow retry")
    h.wall.now = at(15, 11)
    h.tick(CE.instrument_key, 101.0)  # fresh market data returns
    await h.s.square_off.check()
    await h.settle()
    assert t.status is TradeStatus.CLOSED


# --------------------------------------------------------------------------- #
# Restart recovery
# --------------------------------------------------------------------------- #


async def test_restart_restores_trades_guard_and_kill_state(tmp_path: Any) -> None:
    path = tmp_path / "paper.db"
    h = Harness(at(10, 0), db=Database(path))
    t = await h.enter()
    h.s.guard.record_trade_close("vwap_ema", -6000)  # vwap breached + killed
    await h.settle()
    h.db.close()

    # --- process restarts ---
    h2 = Harness(at(10, 30), db=Database(path))
    h2.s.guard.restore()
    h2.s.kill_switch.restore()
    missing = h2.s.positions.restore({CE.instrument_key: CE, PE.instrument_key: PE})
    assert missing == []
    restored = h2.s.positions.trades[t.trade_uid]
    assert restored.open_qty == 75 and restored.stop == t.stop
    assert restored.protective_order_id is not None
    assert h2.s.kill_switch.is_blocked("vwap_ema") and not h2.s.kill_switch.is_blocked("orb_options")
    assert h2.s.guard.state["vwap_ema"].breached
    assert h2.repos.strategies.get("vwap_ema").status is StrategyStatus.KILLED  # type: ignore[union-attr]
    # The paper broker's in-memory book did not survive the restart -> startup
    # reconciliation must detect the divergence and halt (never trade blind).
    rep = await h2.s.reconciler.reconcile("startup")
    assert not rep.ok and h2.s.kill_switch.active
    await h2.settle()
    h2.db.close()


async def test_recovered_partial_fill_charges_exclude_repeat_brokerage() -> None:
    from broker.charges import ChargeCalculator
    from core.models import OrderUpdate

    h = Harness()
    h.tick(CE.instrument_key, 100.0)
    real_place = h.broker.place_order

    async def accepted_no_fill(req: OrderRequest) -> Any:
        raise BrokerTimeoutError("timeout")  # broker accepted it, but we never heard back

    h.broker.place_order = accepted_no_fill  # type: ignore[method-assign]
    rec = await h.s.orders.submit(OrderIntent("orb_options", CE, OrderSide.BUY, 150, OrderType.LIMIT,
                                              OrderPurpose.ENTRY, price=100.5))
    h.broker.place_order = real_place  # type: ignore[method-assign]
    coid = rec.client_order_id
    calc = ChargeCalculator(h.cfg.settings.costs)

    # 1st snapshot: 75 filled @100.20 -> first fill of the order pays brokerage
    h.s.orders.on_broker_update(OrderUpdate(client_order_id=coid, instrument_key=CE.instrument_key,
                                            side=OrderSide.BUY, status=OrderStatus.PARTIALLY_FILLED, quantity=150,
                                            filled_quantity=75, average_price=100.20))
    # 2nd snapshot: 150 filled, avg 100.25 -> second 75 @100.30, no brokerage again
    h.s.orders.on_broker_update(OrderUpdate(client_order_id=coid, instrument_key=CE.instrument_key,
                                            side=OrderSide.BUY, status=OrderStatus.COMPLETE, quantity=150,
                                            filled_quantity=150, average_price=100.25))
    o = h.s.orders.get(coid)
    assert o is not None and o.status is OrderStatus.COMPLETE and o.filled_quantity == 150
    fills = h.repos.fills.list_for_order(o.id)  # type: ignore[arg-type]
    assert [f.quantity for f in fills] == [75, 75]
    assert fills[0].price == pytest.approx(100.20) and fills[1].price == pytest.approx(100.30)
    expected_first = calc.compute(OrderSide.BUY, 75, 100.20, include_brokerage=True).total
    expected_second = calc.compute(OrderSide.BUY, 75, 100.30, include_brokerage=False).total
    assert fills[0].charges == pytest.approx(expected_first)
    assert fills[1].charges == pytest.approx(expected_second)
    assert fills[0].charges - fills[1].charges > 20  # brokerage (+GST) charged once only
    await h.settle()


async def test_recovered_exit_fill_charges_flow_into_trade_pnl() -> None:
    from broker.charges import ChargeCalculator

    h = Harness()
    t = await h.enter()
    entry_charges = t.charges
    real_place = h.broker.place_order
    listeners = h.broker._listeners

    async def lose_exit_response(req: OrderRequest) -> Any:
        if req.side.value == "SELL" and req.order_type.value == "MARKET":
            h.broker._listeners = []
            try:
                await real_place(req)
            finally:
                h.broker._listeners = listeners
            raise BrokerTimeoutError("timeout")
        return await real_place(req)

    h.broker.place_order = lose_exit_response  # type: ignore[method-assign]
    from risk.stop_loss_manager import ExitReason

    assert await h.s.positions.exit_trade(t.trade_uid, ExitReason.MANUAL)
    await h.settle()
    rec = h.repos.trades.get(t.trade_uid)
    assert rec is not None and rec.status.value == "CLOSED"
    sell_fill = [f for o in h.repos.orders.list_for_trade(t.trade_uid) if o.side is OrderSide.SELL
                 and o.filled_quantity for f in h.repos.fills.list_for_order(o.id)]  # type: ignore[arg-type]
    assert len(sell_fill) == 1 and sell_fill[0].paper_fill_id.startswith("RECON-")  # type: ignore[union-attr]
    calc = ChargeCalculator(h.cfg.settings.costs)
    exit_charges = calc.compute(OrderSide.SELL, 75, sell_fill[0].price).total
    assert sell_fill[0].charges == pytest.approx(exit_charges)
    assert rec.charges == pytest.approx(entry_charges + exit_charges, abs=0.01)
    assert rec.net_pnl == pytest.approx(rec.gross_pnl - rec.charges, abs=0.01)  # type: ignore[operator]
    # and it matches what the paper broker actually charged in total
    assert rec.charges == pytest.approx(h.broker.charges_paid, abs=0.02)
