"""PaperBroker: simulated fills, slippage, charges, partial fills, validation."""

from __future__ import annotations

from typing import Any

import pytest

from broker.errors import BrokerError
from broker.paper_broker import PaperBroker
from broker.upstox_mapping import parse_instrument
from config.settings import PaperBrokerSection
from core.enums import OrderSide, OrderStatus, OrderType, TradingMode
from core.ids import new_client_order_id
from core.models import OrderRequest, OrderUpdate, Tick
from tests.fakes import T0, FakeWallClock, option_contract

KEY = "NSE_FO|24500CE"
INST = parse_instrument(option_contract(24500, "CE", key=KEY))


class Env:
    def __init__(self, **settings: Any) -> None:
        self.wall = FakeWallClock(T0)
        self.quotes: dict[str, Tick] = {}
        self.updates: list[OrderUpdate] = []
        self.broker = PaperBroker(quotes=self.quotes.get, settings=PaperBrokerSection(**settings),
                                  instruments=[INST], now_fn=self.wall)
        self.broker.add_order_listener(self.updates.append)

    def quote(self, ltp: float, bid: float | None = None, ask: float | None = None, bid_qty: int = 1500,
              ask_qty: int = 1500, snapshot: bool = False) -> Tick:
        t = Tick(instrument_key=KEY, ltp=ltp, bid=bid if bid is not None else ltp - 0.5,
                 ask=ask if ask is not None else ltp + 0.5, bid_qty=bid_qty, ask_qty=ask_qty,
                 exchange_time=self.wall.now, received_at=self.wall.now, is_snapshot=snapshot)
        self.quotes[KEY] = t
        self.broker.on_tick(t)
        return t


def req(side: OrderSide = OrderSide.BUY, qty: int = 75, otype: OrderType = OrderType.MARKET, **kw: Any) -> OrderRequest:
    return OrderRequest(client_order_id=new_client_order_id(TradingMode.PAPER, "orb_options"), mode=TradingMode.PAPER,
                        strategy_id="orb_options", instrument_key=KEY, side=side, quantity=qty, order_type=otype, **kw)


# --------------------------------------------------------------------------- #
# Market orders
# --------------------------------------------------------------------------- #


async def test_market_buy_fills_at_ask_plus_slippage_with_charges() -> None:
    e = Env(slippage_ticks=1)
    e.quote(100.0)  # bid 99.5 / ask 100.5
    ack = await e.broker.place_order(req())
    assert ack.status is OrderStatus.COMPLETE and ack.paper_order_id and ack.paper_order_id.startswith("PO-")
    fill = e.updates[-1]
    assert fill.last_fill_price == pytest.approx(100.55)  # ask + 1 tick (0.05)
    assert fill.last_fill_quantity == 75
    assert fill.last_fill_charges > 20  # brokerage + fees
    assert fill.last_fill_id and fill.last_fill_id.startswith("PF-")
    (pos,) = await e.broker.get_positions()
    assert pos.quantity == 75 and pos.average_price == pytest.approx(100.55)
    funds = await e.broker.get_funds()
    assert funds.available_margin == pytest.approx(200_000 - 75 * 100.55 - fill.last_fill_charges, abs=0.01)


async def test_round_trip_realised_pnl_and_costs() -> None:
    e = Env(slippage_ticks=0)
    e.quote(100.0)
    await e.broker.place_order(req())
    e.quote(120.0)
    ack = await e.broker.place_order(req(OrderSide.SELL))
    assert ack.status is OrderStatus.COMPLETE
    (pos,) = await e.broker.get_positions()
    assert pos.quantity == 0
    assert pos.realised == pytest.approx((119.5 - 100.5) * 75)  # sold at bid, bought at ask
    funds = await e.broker.get_funds()
    assert funds.available_margin == pytest.approx(200_000 + pos.realised - e.broker.charges_paid, abs=0.01)


async def test_market_order_without_fresh_quote_is_rejected() -> None:
    e = Env()
    ack = await e.broker.place_order(req())  # no quote at all
    assert ack.status is OrderStatus.REJECTED and "market data" in (ack.message or "")
    e.quote(100.0)
    e.wall.advance(seconds=10)  # quote now stale
    ack = await e.broker.place_order(req())
    assert ack.status is OrderStatus.REJECTED


async def test_snapshot_quote_never_used_for_fills() -> None:
    e = Env()
    e.quote(100.0, snapshot=True)
    assert (await e.broker.place_order(req())).status is OrderStatus.REJECTED


async def test_partial_fills_follow_top_of_book_depth() -> None:
    e = Env(slippage_ticks=0)
    e.quote(100.0, ask_qty=150)
    ack = await e.broker.place_order(req(qty=300))
    assert ack.status is OrderStatus.PARTIALLY_FILLED
    assert e.updates[-1].filled_quantity == 150
    e.quote(100.5, ask_qty=75)
    assert e.updates[-1].filled_quantity == 225 and e.updates[-1].status is OrderStatus.PARTIALLY_FILLED
    e.quote(101.0, ask_qty=1500)
    last = e.updates[-1]
    assert last.status is OrderStatus.COMPLETE and last.filled_quantity == 300
    assert last.average_price == pytest.approx((150 * 100.5 + 75 * 101.0 + 75 * 101.5) / 300, abs=1e-3)
    # Brokerage only charged on the first fill of the order.
    fills = [u for u in e.updates if u.last_fill_quantity]
    assert fills[0].last_fill_charges > 20 > fills[1].last_fill_charges


async def test_partial_fill_disabled_fills_full_quantity() -> None:
    e = Env(simulate_partial_fills=False)
    e.quote(100.0, ask_qty=75)
    assert (await e.broker.place_order(req(qty=300))).status is OrderStatus.COMPLETE


# --------------------------------------------------------------------------- #
# Limit / stop orders
# --------------------------------------------------------------------------- #


async def test_limit_buy_rests_until_marketable_and_never_worse() -> None:
    e = Env(slippage_ticks=2)
    e.quote(100.0)
    ack = await e.broker.place_order(req(otype=OrderType.LIMIT, price=99.0))
    assert ack.status is OrderStatus.OPEN
    e.quote(98.8)  # ask 99.3 > limit 99.0 -> still open
    assert e.updates[-1].status is OrderStatus.OPEN
    e.quote(98.4)  # ask 98.9 <= 99.0 -> fill, capped at limit
    last = e.updates[-1]
    assert last.status is OrderStatus.COMPLETE
    assert last.last_fill_price is not None and last.last_fill_price <= 99.0


async def test_sl_m_stop_loss_triggers_and_exits() -> None:
    e = Env(slippage_ticks=1)
    e.quote(100.0)
    await e.broker.place_order(req())
    ack = await e.broker.place_order(req(OrderSide.SELL, otype=OrderType.SL_M, trigger_price=80.0))
    assert ack.status is OrderStatus.OPEN
    e.quote(85.0)
    assert e.updates[-1].status is OrderStatus.OPEN
    e.quote(79.9)  # trigger hit -> market sell at bid - slippage
    last = e.updates[-1]
    assert last.status is OrderStatus.COMPLETE
    assert last.last_fill_price == pytest.approx(79.35)  # bid 79.4 - 0.05
    (pos,) = await e.broker.get_positions()
    assert pos.quantity == 0


async def test_sl_limit_triggers_then_respects_limit() -> None:
    e = Env(slippage_ticks=0)
    e.quote(100.0)
    await e.broker.place_order(req())
    await e.broker.place_order(req(OrderSide.SELL, otype=OrderType.SL, trigger_price=80.0, price=79.0))
    e.quote(77.0)  # triggered, but bid 76.5 < limit 79 -> rests
    assert e.updates[-1].status is OrderStatus.OPEN
    e.quote(79.6)  # bid 79.1 >= 79 -> fill
    assert e.updates[-1].status is OrderStatus.COMPLETE


# --------------------------------------------------------------------------- #
# Validation / safety
# --------------------------------------------------------------------------- #


async def test_lot_size_and_freeze_quantity() -> None:
    e = Env()
    e.quote(100.0)
    assert "lot size" in ((await e.broker.place_order(req(qty=50))).message or "")
    assert "freeze" in ((await e.broker.place_order(req(qty=1875))).message or "")


async def test_tick_size_validation() -> None:
    e = Env()
    e.quote(100.0)
    ack = await e.broker.place_order(req(otype=OrderType.LIMIT, price=99.03))
    assert ack.status is OrderStatus.REJECTED and "tick" in (ack.message or "")


async def test_naked_selling_refused() -> None:
    e = Env()
    e.quote(100.0)
    ack = await e.broker.place_order(req(OrderSide.SELL))
    assert ack.status is OrderStatus.REJECTED and "naked" in (ack.message or "")
    await e.broker.place_order(req())
    await e.broker.place_order(req(OrderSide.SELL, otype=OrderType.SL_M, trigger_price=80.0))
    # A second exit order would over-sell the 75 held (75 already pending).
    ack = await e.broker.place_order(req(OrderSide.SELL, otype=OrderType.LIMIT, price=150.0))
    assert ack.status is OrderStatus.REJECTED


async def test_insufficient_funds() -> None:
    e = Env(starting_funds=5_000)
    e.quote(100.0)
    ack = await e.broker.place_order(req())
    assert ack.status is OrderStatus.REJECTED and "insufficient funds" in (ack.message or "")


async def test_unknown_instrument_rejected() -> None:
    e = Env()
    r = OrderRequest(client_order_id=new_client_order_id(TradingMode.PAPER, "orb_options"), mode=TradingMode.PAPER,
                     strategy_id="orb_options", instrument_key="NSE_FO|nope", side=OrderSide.BUY, quantity=75,
                     order_type=OrderType.MARKET)
    assert (await e.broker.place_order(r)).status is OrderStatus.REJECTED


async def test_idempotent_resend_does_not_duplicate() -> None:
    e = Env()
    e.quote(100.0)
    r = req()
    a1 = await e.broker.place_order(r)
    a2 = await e.broker.place_order(r)
    assert a1.paper_order_id == a2.paper_order_id
    (pos,) = await e.broker.get_positions()
    assert pos.quantity == 75


async def test_live_order_refused_by_paper_broker() -> None:
    e = Env()
    live = OrderRequest(client_order_id=new_client_order_id(TradingMode.LIVE, "orb_options"), mode=TradingMode.LIVE,
                        strategy_id="orb_options", instrument_key=KEY, side=OrderSide.BUY, quantity=75,
                        order_type=OrderType.MARKET)
    with pytest.raises(BrokerError):
        await e.broker.place_order(live)


async def test_modify_and_cancel() -> None:
    e = Env()
    e.quote(100.0)
    r = req(otype=OrderType.LIMIT, price=90.0)
    await e.broker.place_order(r)
    ack = await e.broker.modify_order(r.client_order_id, price=95.0)
    assert ack.status is OrderStatus.OPEN
    ack = await e.broker.modify_order(r.client_order_id, price=95.01)
    assert "rejected" in (ack.message or "")
    ack = await e.broker.cancel_order(r.client_order_id)
    assert ack.status is OrderStatus.CANCELLED
    again = await e.broker.cancel_order(r.client_order_id)
    assert again.status is OrderStatus.CANCELLED and "not cancellable" in (again.message or "")
    e.quote(80.0)  # cancelled order must never fill
    assert (await e.broker.get_positions()) == []
    with pytest.raises(BrokerError):
        await e.broker.cancel_order("PAPER-orb_options-unknown")


async def test_order_book_snapshot() -> None:
    e = Env()
    e.quote(100.0)
    await e.broker.place_order(req())
    (o,) = await e.broker.get_orders()
    assert o.status is OrderStatus.COMPLETE and o.paper_order_id
