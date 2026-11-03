"""Payload mapping: protobuf feed decoding, order statuses, instruments."""

from __future__ import annotations

from datetime import date

import pytest

from broker.proto import MarketDataFeedV3_pb2 as pb
from broker.upstox_mapping import (
    decode_feed_response,
    map_order_status,
    parse_expiry,
    parse_instrument,
    tick_size_rupees,
)
from core.enums import OrderStatus
from tests.fakes import T0, feed_index, feed_ltpc, feed_market_info, feed_option, option_contract


def test_decode_full_option_feed() -> None:
    msg_type, ticks, _ = decode_feed_response(feed_option("NSE_FO|1", 120.0, T0, vtt=5000, oi=100000))
    assert msg_type == "live_feed"
    (t,) = ticks
    assert t.instrument_key == "NSE_FO|1"
    assert t.ltp == 120.0
    assert t.exchange_time == T0
    assert (t.bid, t.ask, t.bid_qty, t.ask_qty) == (119.5, 120.5, 1500, 1500)
    assert t.volume_today == 5000 and t.oi == 100000
    assert t.greeks is not None and t.greeks.delta == pytest.approx(0.52)
    assert t.spread == pytest.approx(1.0) and not t.is_snapshot


def test_decode_index_and_ltpc() -> None:
    _, (idx,), _ = decode_feed_response(feed_index("NSE_INDEX|Nifty 50", 24510.25, T0))
    assert idx.ltp == 24510.25 and idx.bid is None
    _, (lt,), _ = decode_feed_response(feed_ltpc("K", 5.5, T0))
    assert lt.ltp == 5.5


def test_initial_feed_marked_as_snapshot() -> None:
    _, (t,), _ = decode_feed_response(feed_index("I", 1.0, T0, msg_type=pb.initial_feed))
    assert t.is_snapshot


def test_market_info_status() -> None:
    msg_type, ticks, seg = decode_feed_response(feed_market_info({"NSE_FO": pb.NORMAL_OPEN}))
    assert msg_type == "market_info" and ticks == []
    assert seg == {"NSE_FO": "NORMAL_OPEN"}


def test_garbage_protobuf_raises() -> None:
    with pytest.raises(Exception):  # noqa: B017 - protobuf DecodeError
        decode_feed_response(b"\xff\xff\xff\xff")


@pytest.mark.parametrize(
    ("raw", "filled", "expected"),
    [
        ("complete", 75, OrderStatus.COMPLETE),
        ("rejected", 0, OrderStatus.REJECTED),
        ("cancelled", 25, OrderStatus.CANCELLED),
        ("open", 0, OrderStatus.OPEN),
        ("open", 25, OrderStatus.PARTIALLY_FILLED),
        ("trigger pending", 0, OrderStatus.OPEN),
        ("put order req received", 0, OrderStatus.SUBMITTED),
        ("validation pending", 0, OrderStatus.SUBMITTED),
        ("modify pending", 25, OrderStatus.PARTIALLY_FILLED),
        ("something new from upstox", 0, OrderStatus.UNKNOWN),
        (None, 0, OrderStatus.UNKNOWN),
    ],
)
def test_order_status_mapping(raw: str | None, filled: int, expected: OrderStatus) -> None:
    assert map_order_status(raw, filled) is expected


def test_instrument_parsing_from_official_example_shape() -> None:
    # Official instrument-file example (IDEA option), expiry in epoch ms.
    rec = {"weekly": False, "segment": "NSE_FO", "name": "VODAFONE IDEA LIMITED", "exchange": "NSE",
           "expiry": 1706207399000, "instrument_type": "CE", "underlying_symbol": "IDEA",
           "instrument_key": "NSE_FO|36708", "lot_size": 80000, "freeze_quantity": 1600000.0,
           "exchange_token": "36708", "minimum_lot": 80000, "underlying_key": "NSE_EQ|INE669E01016",
           "tick_size": 5.0, "underlying_type": "EQUITY", "trading_symbol": "IDEA 22 CE 25 JAN 24",
           "strike_price": 22.0}
    inst = parse_instrument(rec)
    assert inst.expiry == date(2024, 1, 25)
    assert inst.tick_size == pytest.approx(0.05)
    assert inst.freeze_quantity == 1600000
    assert inst.option_type is not None and inst.option_type.value == "CE"


def test_option_contract_parsing() -> None:
    inst = parse_instrument(option_contract(24500, "PE"))
    assert inst.strike == 24500 and inst.lot_size == 75 and inst.expiry == date(2026, 11, 10)


def test_helpers() -> None:
    assert parse_expiry("2026-11-10") == date(2026, 11, 10)
    assert parse_expiry(None) is None
    with pytest.raises(ValueError):
        tick_size_rupees(0)
