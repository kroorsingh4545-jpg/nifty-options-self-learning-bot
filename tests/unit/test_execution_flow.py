"""End-to-end PAPER execution: entries, protective stops, SL/TP/trailing exits,
partial fills, rejections, order lifecycle, isolation, global breach."""

from __future__ import annotations

import pytest

from core.enums import OrderPurpose, OrderStatus, StrategyStatus, TradeStatus
from execution.order_manager import LIFECYCLE, EntryBlockedError, OrderIntent, transition_allowed
from risk.kill_switch import KillReason
from risk.stop_loss_manager import ExitReason
from tests.stack_harness import CE, PE, Harness


def orders_of(h: Harness, trade_uid: str) -> list:  # type: ignore[type-arg]
    return h.repos.orders.list_for_trade(trade_uid)


# --------------------------------------------------------------------------- #
# Entry + protective stop
# --------------------------------------------------------------------------- #


async def test_entry_fills_and_places_exchange_stop() -> None:
    h = Harness()
    t = await h.enter()
    assert t.filled_qty == 75 and t.entry_complete
    assert t.avg_entry == pytest.approx(100.10)  # ask 100.05 + 1 tick slippage, within LIMIT 100.15
    os_ = orders_of(h, t.trade_uid)
    entry = next(o for o in os_ if o.purpose is OrderPurpose.ENTRY)
    stop = next(o for o in os_ if o.purpose is OrderPurpose.STOP_LOSS)
    assert entry.status is OrderStatus.COMPLETE and entry.order_type.value == "LIMIT"
    assert stop.status is OrderStatus.OPEN and stop.order_type.value == "SL-M"
    assert stop.trigger_price == t.stop and stop.quantity == 75
    rec = h.repos.trades.get(t.trade_uid)
    assert rec is not None and rec.entry_price == pytest.approx(100.10) and rec.stop_loss == t.stop
    (pos,) = h.repos.positions.list_open("orb_options")
    assert pos.quantity == 75 and pos.stop_loss == t.stop
    fills = h.repos.fills.list_for_order(entry.id)  # type: ignore[arg-type]
    assert len(fills) == 1 and fills[0].paper_fill_id and fills[0].charges > 0


async def test_stop_loss_hit_closes_trade_and_updates_guard() -> None:
    h = Harness()
    t = await h.enter()
    h.tick(CE.instrument_key, t.stop)  # exchange-side SL-M triggers
    await h.settle()
    rec = h.repos.trades.get(t.trade_uid)
    assert rec is not None and rec.status is TradeStatus.CLOSED
    assert rec.exit_reason == ExitReason.STOP_LOSS.value
    assert rec.net_pnl is not None and -2000 <= rec.net_pnl < 0
    assert rec.r_multiple is not None and rec.r_multiple < -0.9
    s = h.s.guard.state["orb_options"]
    assert s.losses == 1 and s.realized == pytest.approx(rec.net_pnl)
    assert h.repos.positions.list_open() == []
    assert not h.s.guard.can_enter("orb_options")[0]  # cooldown


async def test_local_stop_monitor_without_exchange_stop() -> None:
    from config.loader import load_config

    base = load_config(use_default_env_file=False)
    no_exchange = base.settings.model_copy(update={"execution": base.settings.execution.model_copy(
        update={"use_exchange_stop_orders": False})})
    h = Harness(cfg=base.model_copy(update={"settings": no_exchange}))
    t = await h.enter()
    assert not any(o.purpose is OrderPurpose.STOP_LOSS for o in orders_of(h, t.trade_uid))
    h.tick(CE.instrument_key, t.stop - 0.5)  # local tick monitor fires a market exit
    await h.settle()
    rec = h.repos.trades.get(t.trade_uid)
    assert rec is not None and rec.status is TradeStatus.CLOSED and rec.exit_reason == "STOP_LOSS"


async def test_target_hit_cancels_protective_stop_then_exits() -> None:
    h = Harness()
    t = await h.enter()
    assert t.levels.target is not None
    h.tick(CE.instrument_key, t.levels.target + 0.5)
    await h.settle()
    rec = h.repos.trades.get(t.trade_uid)
    assert rec is not None and rec.status is TradeStatus.CLOSED and rec.exit_reason == "TARGET"
    assert rec.net_pnl is not None and rec.net_pnl > 0
    stop = next(o for o in orders_of(h, t.trade_uid) if o.purpose is OrderPurpose.STOP_LOSS)
    assert stop.status is OrderStatus.CANCELLED
    assert h.repos.orders.list_open() == []


async def test_trailing_stop_tightens_moves_exchange_stop_and_exits() -> None:
    h = Harness()
    t = await h.enter()  # orb: R_MULTIPLE trailing, activation 1R, step 0.5R
    initial = t.stop
    r = t.levels.risk_per_unit
    h.tick(CE.instrument_key, t.levels.entry + 1.2 * r)
    await h.settle()
    assert t.trail.active and t.stop > initial
    moved = t.stop
    stop_order = next(o for o in orders_of(h, t.trade_uid) if o.purpose is OrderPurpose.STOP_LOSS)
    assert stop_order.trigger_price == moved
    assert h.repos.trades.get(t.trade_uid).stop_loss == moved  # type: ignore[union-attr]
    h.tick(CE.instrument_key, t.levels.entry + 1.0 * r)  # pullback: stop must not loosen
    await h.settle()
    assert t.stop == moved
    h.tick(CE.instrument_key, moved - 0.05)
    await h.settle()
    rec = h.repos.trades.get(t.trade_uid)
    assert rec is not None and rec.status is TradeStatus.CLOSED
    assert rec.net_pnl is not None and rec.net_pnl > 0  # locked-in profit


async def test_database_refuses_to_widen_stop() -> None:
    import sqlite3

    h = Harness()
    t = await h.enter()
    with pytest.raises(sqlite3.IntegrityError, match="tightened"):
        h.repos.trades.update(t.trade_uid, stop_loss=t.stop - 5)


# --------------------------------------------------------------------------- #
# Partial fills
# --------------------------------------------------------------------------- #


async def test_partial_entry_fill_then_timeout_cancels_remainder() -> None:
    h = Harness()
    h.tick(PE.instrument_key, 40.0, spread=0.05, qty=1500)
    d = await h.decide("vwap_ema", PE)
    assert d.approved and d.quantity > 75
    h.tick(PE.instrument_key, 40.0, spread=0.05, qty=75)  # only one lot on offer
    t = await h.s.positions.open_trade(d, strategy_version="0.1.0")
    await h.settle()
    assert t.filled_qty == 75 and not t.entry_complete
    entry = h.s.orders.get(t.entry_order_id)  # type: ignore[arg-type]
    assert entry is not None and entry.status is OrderStatus.PARTIALLY_FILLED
    h.wall.advance(seconds=31)
    await h.s.orders.expire_stale_entries()
    await h.settle()
    assert t.entry_complete and t.filled_qty == 75
    assert h.s.orders.get(t.entry_order_id).status is OrderStatus.CANCELLED  # type: ignore[arg-type,union-attr]
    rec = h.repos.trades.get(t.trade_uid)
    assert rec is not None and rec.quantity == 75  # trade reflects filled quantity
    stop = next(o for o in orders_of(h, t.trade_uid) if o.purpose is OrderPurpose.STOP_LOSS)
    assert stop.quantity == 75


async def test_partial_exit_fills_complete_over_ticks() -> None:
    h = Harness()
    h.tick(PE.instrument_key, 40.0, spread=0.05)
    d = await h.decide("vwap_ema", PE)
    t = await h.s.positions.open_trade(d, strategy_version="0.1.0")
    await h.settle()
    qty = t.filled_qty
    assert qty >= 150
    h.tick(PE.instrument_key, 41.0, spread=0.05, qty=75)  # thin book on exit
    ok = await h.s.positions.exit_trade(t.trade_uid, ExitReason.MANUAL)
    await h.settle()
    assert ok and t.open_qty == 0
    rec = h.repos.trades.get(t.trade_uid)
    assert rec is not None and rec.status is TradeStatus.CLOSED
    sells = [o for o in orders_of(h, t.trade_uid) if o.side.value == "SELL" and o.filled_quantity]
    assert sum(o.filled_quantity for o in sells) == qty


async def test_entry_with_no_fill_cancels_trade() -> None:
    h = Harness()
    h.tick(CE.instrument_key, 100.0)
    d = await h.decide()
    h.tick(CE.instrument_key, 110.0)  # market ran away above the LIMIT
    t = await h.s.positions.open_trade(d, strategy_version="0.1.0")
    h.wall.advance(seconds=31)
    await h.s.orders.expire_stale_entries()
    await h.settle()
    assert t.status is TradeStatus.CANCELLED
    assert h.repos.trades.get(t.trade_uid).status is TradeStatus.CANCELLED  # type: ignore[union-attr]
    assert h.s.positions.open_trade_count() == 0


# --------------------------------------------------------------------------- #
# Rejections / lifecycle
# --------------------------------------------------------------------------- #


async def test_rejection_logged_alerted_and_not_retried() -> None:
    h = Harness()
    h.tick(CE.instrument_key, 100.0)
    d = await h.decide()
    assert d.approved
    h.broker._cash = 1000.0  # funds drop between risk approval and submission -> broker rejects
    t = await h.s.positions.open_trade(d, strategy_version="0.1.0")
    await h.settle()
    entry = h.s.orders.get(t.entry_order_id)  # type: ignore[arg-type]
    assert entry is not None and entry.status is OrderStatus.REJECTED
    assert "insufficient funds" in (entry.status_message or "")
    assert t.status is TradeStatus.CANCELLED
    assert len([o for o in orders_of(h, t.trade_uid) if o.purpose is OrderPurpose.ENTRY]) == 1  # no blind retry
    assert any(e.event_type == "ORDER_REJECTED" for e in h.repos.risk_events.recent())
    assert any(a.title == "Order rejected" for a in h.s.alerts.alerts)


async def test_repeated_rejections_trigger_kill_switch() -> None:
    h = Harness(starting_funds=1000)
    h.tick(CE.instrument_key, 100.0)
    for _ in range(3):
        await h.s.orders.submit(OrderIntent("orb_options", CE, __import__("core.enums", fromlist=["OrderSide"]).
                                            OrderSide.BUY, 75, __import__("core.enums", fromlist=["OrderType"]).
                                            OrderType.MARKET, OrderPurpose.ENTRY))
    assert h.s.kill_switch.global_event is not None
    assert h.s.kill_switch.global_event.reason is KillReason.REPEATED_REJECTIONS
    await h.settle()


def test_lifecycle_table() -> None:
    S = OrderStatus
    assert transition_allowed(S.CREATED, S.SUBMITTED)
    assert transition_allowed(S.SUBMITTED, S.OPEN)
    assert transition_allowed(S.OPEN, S.PARTIALLY_FILLED)
    assert transition_allowed(S.PARTIALLY_FILLED, S.COMPLETE)
    assert transition_allowed(S.PARTIALLY_FILLED, S.CANCELLED)
    assert transition_allowed(S.UNKNOWN, S.COMPLETE)
    for terminal in (S.COMPLETE, S.REJECTED, S.CANCELLED):
        assert LIFECYCLE[terminal] == frozenset()
    assert not transition_allowed(S.PARTIALLY_FILLED, S.OPEN)
    assert not transition_allowed(S.PARTIALLY_FILLED, S.REJECTED)


async def test_late_regressive_update_is_ignored() -> None:
    from core.models import OrderUpdate

    h = Harness()
    t = await h.enter()
    entry = h.s.orders.get(t.entry_order_id)  # type: ignore[arg-type]
    assert entry is not None and entry.status is OrderStatus.COMPLETE
    h.s.orders.on_broker_update(OrderUpdate(client_order_id=entry.client_order_id, instrument_key=CE.instrument_key,
                                            side=entry.side, status=OrderStatus.OPEN, quantity=75, filled_quantity=0))
    assert h.s.orders.get(entry.client_order_id).status is OrderStatus.COMPLETE  # type: ignore[union-attr]
    assert h.s.orders.illegal_transitions == 1
    await h.settle()


async def test_entry_blocked_by_kill_switch_at_order_layer() -> None:
    h = Harness()
    h.tick(CE.instrument_key, 100.0)
    d = await h.decide()
    h.s.kill_switch.trigger(KillReason.MANUAL, "stop now", strategy_id="orb_options")
    with pytest.raises(EntryBlockedError):
        await h.s.positions.open_trade(d, strategy_version="0.1.0")
    await h.settle()
    assert h.s.positions.open_trade_count() == 0


# --------------------------------------------------------------------------- #
# Isolation + global breach
# --------------------------------------------------------------------------- #


async def test_strategy_cannot_exit_another_strategys_trade() -> None:
    h = Harness()
    t = await h.enter("orb_options")
    with pytest.raises(PermissionError):
        await h.s.positions.exit_trade(t.trade_uid, ExitReason.STRATEGY, strategy_id="vwap_ema")


async def test_strategy_loss_breach_flattens_only_that_strategy() -> None:
    h = Harness()
    orb = await h.enter("orb_options", CE)
    vwap = await h.enter("vwap_ema", PE)
    h.s.guard.record_trade_close("orb_options", -5000)  # breach -> strategy kill
    await h.settle()
    assert h.s.kill_switch.is_blocked("orb_options") and not h.s.kill_switch.is_blocked("vwap_ema")
    assert orb.status is TradeStatus.CLOSED
    assert vwap.status is TradeStatus.OPEN
    assert h.repos.strategies.get("orb_options").status is StrategyStatus.KILLED  # type: ignore[union-attr]
    assert h.repos.strategies.get("vwap_ema").status is StrategyStatus.ACTIVE  # type: ignore[union-attr]


async def test_global_loss_breach_full_sequence() -> None:
    h = Harness()
    a = await h.enter("orb_options", CE)
    b = await h.enter("vwap_ema", PE)
    # a resting entry order that must be cancelled
    h.tick(CE.instrument_key, 100.0)
    d = await h.decide("vwap_ema", CE)
    h.tick(CE.instrument_key, 101.0)  # ask now above the entry LIMIT -> order rests
    pending = await h.s.positions.open_trade(d, strategy_version="0.1.0")
    assert h.repos.orders.list_open()
    # With 2 x ₹5,000 strategy limits, ₹10,000 global implies a strategy breach too.
    h.s.guard.record_trade_close("orb_options", -4999)
    h.s.guard.record_trade_close("vwap_ema", -5100)
    assert h.s.guard.global_loss >= 10_000
    await h.settle()
    ks = h.s.kill_switch
    assert ks.global_event is not None and ks.global_event.reason is KillReason.GLOBAL_DAILY_LOSS  # trigger kill
    assert h.repos.orders.list_open() == []                                         # pending cancelled
    assert a.status is TradeStatus.CLOSED and b.status is TradeStatus.CLOSED        # positions exited
    assert pending.status is TradeStatus.CANCELLED
    assert all(s.status is StrategyStatus.KILLED for s in h.repos.strategies.list_all())  # disabled for the day
    titles = [x.title for x in h.s.alerts.alerts]
    assert any("Kill switch triggered" in x for x in titles)                        # alerts
    assert any(e.event_type == "KILL_SWITCH_TRIGGERED" for e in h.repos.risk_events.recent())
    assert not (await h.decide("vwap_ema", CE)).approved
    assert all(p.quantity == 0 for p in await h.broker.get_positions())  # flat at the broker too


async def test_exit_failure_escalates_to_kill_switch() -> None:
    h = Harness()
    t = await h.enter()
    h.wall.advance(seconds=60)  # every quote is stale -> paper broker rejects market exits
    ok = await h.s.positions.exit_trade(t.trade_uid, ExitReason.MANUAL)
    await h.settle()
    assert not ok and t.open_qty == 75
    assert any(a.title == "EXIT FAILED" for a in h.s.alerts.alerts)
    assert h.s.kill_switch.active
    # protection is put back while humans intervene
    assert any(o.purpose is OrderPurpose.STOP_LOSS and o.status is OrderStatus.OPEN
               for o in orders_of(h, t.trade_uid))


# --------------------------------------------------------------------------- #
# UNKNOWN exits / broker errors
# --------------------------------------------------------------------------- #


async def test_exit_timeout_is_resolved_before_any_resend() -> None:
    from typing import Any

    from broker.errors import BrokerTimeoutError
    from core.models import OrderRequest

    h = Harness()
    t = await h.enter()
    real_place = h.broker.place_order
    listeners = h.broker._listeners

    async def lose_response(req: OrderRequest) -> Any:
        if req.side.value == "SELL" and req.order_type.value == "MARKET":
            h.broker._listeners = []
            try:
                await real_place(req)  # the exit DID execute at the broker
            finally:
                h.broker._listeners = listeners
            raise BrokerTimeoutError("timeout")
        return await real_place(req)

    h.broker.place_order = lose_response  # type: ignore[method-assign]
    ok = await h.s.positions.exit_trade(t.trade_uid, ExitReason.MANUAL)
    await h.settle()
    assert ok and t.open_qty == 0
    sells = [o for o in await h.broker.get_orders() if o.side.value == "SELL" and o.status is OrderStatus.COMPLETE]
    assert len(sells) == 1  # resolved via the order book, never sent twice
    assert all(p.quantity == 0 for p in await h.broker.get_positions())


async def test_unresolvable_unknown_exit_escalates_without_resending() -> None:
    from typing import Any

    from broker.errors import BrokerTimeoutError
    from core.models import OrderRequest

    h = Harness()
    t = await h.enter()
    sent: list[str] = []
    real_place = h.broker.place_order

    async def black_hole(req: OrderRequest) -> Any:
        if req.side.value == "SELL" and req.order_type.value == "MARKET":
            sent.append(req.client_order_id)
            raise BrokerTimeoutError("timeout")  # broker never heard of it
        return await real_place(req)

    h.broker.place_order = black_hole  # type: ignore[method-assign]
    ok = await h.s.positions.exit_trade(t.trade_uid, ExitReason.MANUAL)
    await h.settle()
    assert not ok
    assert len(sent) == 1  # UNKNOWN outcome -> no blind retry
    assert h.s.kill_switch.active
    assert any(a.title == "EXIT FAILED" for a in h.s.alerts.alerts)


async def test_broker_error_on_submit_marks_rejected() -> None:
    from typing import Any

    from broker.errors import BrokerError
    from core.enums import OrderSide, OrderType
    from core.models import OrderRequest

    h = Harness()
    h.tick(CE.instrument_key, 100.0)

    async def boom(req: OrderRequest) -> Any:
        raise BrokerError("HTTP 500")

    h.broker.place_order = boom  # type: ignore[method-assign]
    rec = await h.s.orders.submit(OrderIntent("orb_options", CE, OrderSide.BUY, 75, OrderType.MARKET,
                                              OrderPurpose.ENTRY))
    assert rec.status is OrderStatus.REJECTED and "broker error" in (rec.status_message or "")
    assert h.s.orders.rejection_streak == 1


async def test_live_routing_refusal_propagates() -> None:
    from typing import Any

    from broker.errors import LiveTradingDisabledError
    from core.enums import OrderSide, OrderType
    from core.models import OrderRequest

    h = Harness()

    async def refuse(req: OrderRequest) -> Any:
        raise LiveTradingDisabledError("no live")

    h.broker.place_order = refuse  # type: ignore[method-assign]
    with pytest.raises(LiveTradingDisabledError):
        await h.s.orders.submit(OrderIntent("orb_options", CE, OrderSide.BUY, 75, OrderType.MARKET,
                                            OrderPurpose.ENTRY))
