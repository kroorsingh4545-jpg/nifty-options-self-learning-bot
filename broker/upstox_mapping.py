"""Translate Upstox payloads (REST JSON, portfolio-stream JSON, market-feed
Protobuf) into broker-agnostic ``core.models``.

Field names follow the official Upstox docs (snake_case; deprecated camelCase
aliases are not relied on).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date, datetime, timedelta
from typing import Any

from broker.proto import MarketDataFeedV3_pb2 as pb
from core.clock import IST, now_ist
from core.enums import OrderSide, OrderStatus
from core.models import BrokerPosition, Candle, Funds, Greeks, Instrument, OrderUpdate, Tick

# --------------------------------------------------------------------------- #
# Instruments
# --------------------------------------------------------------------------- #


def tick_size_rupees(raw: float) -> float:
    """Upstox instrument data reports ``tick_size`` in paise (e.g. 5.0 for a
    ₹0.05 tick, per the official instrument example). Convert to rupees."""
    if raw <= 0:
        raise ValueError("tick_size must be positive")
    return raw / 100.0


def parse_expiry(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):  # epoch milliseconds (instrument files)
        return datetime.fromtimestamp(value / 1000, tz=IST).date()
    return date.fromisoformat(str(value)[:10])


def parse_instrument(rec: Mapping[str, Any]) -> Instrument:
    freeze = rec.get("freeze_quantity")
    return Instrument(
        instrument_key=rec["instrument_key"],
        trading_symbol=rec.get("trading_symbol") or rec["instrument_key"],
        segment=rec.get("segment", ""),
        underlying_key=rec.get("underlying_key"),
        underlying_symbol=rec.get("underlying_symbol"),
        instrument_type=rec.get("instrument_type", ""),
        expiry=parse_expiry(rec.get("expiry")),
        strike=float(rec["strike_price"]) if rec.get("strike_price") is not None else None,
        lot_size=int(rec["lot_size"]),
        tick_size=tick_size_rupees(float(rec["tick_size"])),
        freeze_quantity=int(freeze) if freeze is not None else None,
        weekly=rec.get("weekly"),
    )


# --------------------------------------------------------------------------- #
# Account
# --------------------------------------------------------------------------- #


def parse_position(rec: Mapping[str, Any]) -> BrokerPosition:
    return BrokerPosition(
        instrument_key=rec.get("instrument_token") or rec["instrument_key"],
        trading_symbol=rec.get("trading_symbol"),
        product=rec.get("product", ""),
        quantity=int(rec.get("quantity", 0)),
        average_price=float(rec.get("average_price") or 0.0),
        last_price=float(rec["last_price"]) if rec.get("last_price") is not None else None,
        realised=float(rec.get("realised") or 0.0),
        unrealised=float(rec.get("unrealised") or 0.0),
        buy_quantity=int(rec.get("day_buy_quantity") or 0) + int(rec.get("overnight_buy_quantity") or 0),
        sell_quantity=int(rec.get("day_sell_quantity") or 0) + int(rec.get("overnight_sell_quantity") or 0),
    )


def parse_funds(data: Mapping[str, Any]) -> Funds:
    eq = data.get("equity") or {}
    return Funds(
        available_margin=float(eq.get("available_margin") or 0.0),
        used_margin=float(eq.get("used_margin") or 0.0),
        payin_amount=float(eq.get("payin_amount") or 0.0),
        notional_cash=float(eq.get("notional_cash") or 0.0),
    )


# --------------------------------------------------------------------------- #
# Orders
# --------------------------------------------------------------------------- #

_TERMINAL = {"complete": OrderStatus.COMPLETE, "rejected": OrderStatus.REJECTED, "cancelled": OrderStatus.CANCELLED}
_OPEN = {"open", "trigger pending"}
_SUBMITTED = {
    "put order req received",
    "validation pending",
    "open pending",
    "modify validation pending",
    "modify pending",
    "cancel pending",
    "after market order req received",
    "modified",
    "not modified",
    "not cancelled",
}


def map_order_status(raw: str | None, filled_quantity: int = 0) -> OrderStatus:
    """Map an Upstox order status string to the local lifecycle.

    Unrecognised statuses map to UNKNOWN (must be reconciled), never to a
    guessed state.
    """
    s = (raw or "").strip().lower()
    if s in _TERMINAL:
        return _TERMINAL[s]
    if s in _OPEN:
        return OrderStatus.PARTIALLY_FILLED if filled_quantity > 0 else OrderStatus.OPEN
    if s in _SUBMITTED:
        return OrderStatus.PARTIALLY_FILLED if filled_quantity > 0 else OrderStatus.SUBMITTED
    return OrderStatus.UNKNOWN


def _parse_ts(value: Any) -> datetime:
    if not value:
        return now_ist()
    try:
        dt = datetime.fromisoformat(str(value).replace(" ", "T"))
    except ValueError:
        return now_ist()
    return dt if dt.tzinfo else dt.replace(tzinfo=IST)


def parse_order_update(rec: Mapping[str, Any]) -> OrderUpdate:
    """Parse a portfolio-stream order update or an order-book entry."""
    filled = int(rec.get("filled_quantity") or 0)
    avg = rec.get("average_price")
    return OrderUpdate(
        client_order_id=rec.get("tag") or None,
        broker_order_id=rec.get("order_id") or None,
        instrument_key=rec.get("instrument_key") or rec.get("instrument_token") or "",
        side=OrderSide(str(rec.get("transaction_type", "BUY")).upper()),
        status=map_order_status(rec.get("status"), filled),
        quantity=int(rec.get("quantity") or 0),
        filled_quantity=filled,
        average_price=float(avg) if avg else None,
        message=rec.get("status_message") or None,
        raw_status=rec.get("status"),
        timestamp=_parse_ts(rec.get("exchange_timestamp") or rec.get("order_timestamp")),
    )


# --------------------------------------------------------------------------- #
# Candles
# --------------------------------------------------------------------------- #


def parse_candles(
    instrument_key: str, rows: Iterable[list[Any]], interval_minutes: int, now: datetime | None = None
) -> list[Candle]:
    """Upstox candle rows are ``[ts, open, high, low, close, volume, oi]``,
    newest first. Returns oldest-first; a candle still forming is marked
    ``complete=False``."""
    now = now or now_ist()
    out: list[Candle] = []
    for row in rows:
        start = _parse_ts(row[0])
        end = start + timedelta(minutes=interval_minutes)
        out.append(
            Candle(
                instrument_key=instrument_key,
                timeframe_minutes=interval_minutes,
                start=start,
                end=end,
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
                volume=int(row[5] or 0),
                oi=float(row[6]) if len(row) > 6 and row[6] is not None else None,
                complete=end <= now,
            )
        )
    out.sort(key=lambda c: c.start)
    return out


# --------------------------------------------------------------------------- #
# Market feed (Protobuf)
# --------------------------------------------------------------------------- #


def _ms_to_ist(ms: int) -> datetime | None:
    return datetime.fromtimestamp(ms / 1000, tz=IST) if ms > 0 else None


def _greeks(g: Any) -> Greeks:
    return Greeks(delta=g.delta, gamma=g.gamma, theta=g.theta, vega=g.vega, rho=g.rho)


def _tick_from_feed(key: str, feed: Any, is_snapshot: bool, fallback_ts: datetime) -> Tick | None:
    union = feed.WhichOneof("FeedUnion")
    bid = ask = None
    bid_qty = ask_qty = None
    vtt = oi = iv = atp = None
    tbq = tsq = None
    greeks = None
    if union == "ltpc":
        ltpc = feed.ltpc
    elif union == "fullFeed":
        ff = feed.fullFeed
        kind = ff.WhichOneof("FullFeedUnion")
        if kind == "marketFF":
            m = ff.marketFF
            ltpc = m.ltpc
            if m.marketLevel.bidAskQuote:
                q = m.marketLevel.bidAskQuote[0]
                bid, ask, bid_qty, ask_qty = q.bidP or None, q.askP or None, int(q.bidQ), int(q.askQ)
            vtt, oi, iv, atp = int(m.vtt), m.oi, m.iv, m.atp
            # resting quantity across all depth levels (not executed volume); never negative
            tbq, tsq = (m.tbq if m.tbq >= 0 else None), (m.tsq if m.tsq >= 0 else None)
            if m.HasField("optionGreeks"):
                greeks = _greeks(m.optionGreeks)
        elif kind == "indexFF":
            ltpc = ff.indexFF.ltpc
        else:
            return None
    elif union == "firstLevelWithGreeks":
        f = feed.firstLevelWithGreeks
        ltpc = f.ltpc
        if f.HasField("firstDepth"):
            q = f.firstDepth
            bid, ask, bid_qty, ask_qty = q.bidP or None, q.askP or None, int(q.bidQ), int(q.askQ)
        vtt, oi, iv = int(f.vtt), f.oi, f.iv
        if f.HasField("optionGreeks"):
            greeks = _greeks(f.optionGreeks)
    else:
        return None
    return Tick(
        instrument_key=key,
        ltp=ltpc.ltp,
        exchange_time=_ms_to_ist(int(ltpc.ltt)) or fallback_ts,
        ltq=int(ltpc.ltq),
        close_price=ltpc.cp or None,
        bid=bid,
        ask=ask,
        bid_qty=bid_qty,
        ask_qty=ask_qty,
        volume_today=vtt,
        oi=oi,
        iv=iv,
        atp=atp,
        total_buy_qty=tbq,
        total_sell_qty=tsq,
        greeks=greeks,
        is_snapshot=is_snapshot,
    )


def decode_feed_response(payload: bytes) -> tuple[str, list[Tick], dict[str, str]]:
    """Decode one binary market-feed message.

    Returns ``(type, ticks, segment_status)`` where type is ``initial_feed``,
    ``live_feed`` or ``market_info``.
    """
    msg = pb.FeedResponse.FromString(payload)
    msg_type = pb.Type.Name(msg.type)
    fallback = _ms_to_ist(int(msg.currentTs)) or now_ist()
    ticks: list[Tick] = []
    for key, feed in msg.feeds.items():
        tick = _tick_from_feed(key, feed, msg_type == "initial_feed", fallback)
        if tick is not None:
            ticks.append(tick)
    segments = {seg: pb.MarketStatus.Name(st) for seg, st in msg.marketInfo.segmentStatus.items()}
    return msg_type, ticks, segments
