"""Instrument list as the only source of lot sizes; current-month NIFTY future
selection and roll-over; NSE holiday calendar."""

from __future__ import annotations

import gzip
import json
import logging
from datetime import date
from typing import Any

import httpx
import pytest

from broker.errors import BrokerError
from broker.upstox_mapping import parse_instrument
from broker.upstox_rate_limiter import UpstoxRateLimiter
from broker.upstox_rest import UpstoxRestClient
from config.loader import ConfigError, load_config
from config.settings import BrokerSection, CircuitBreakerSection, MarketSession
from core.enums import OptionType, OrderStatus
from data.instruments import InstrumentMaster, InstrumentMasterError, build_master
from tests.fakes import FakeClock, FakeWallClock, future_contract, option_contract
from tests.synthetic import FUT, at
from tests.unit.test_bot_integration import ORB_DAY, Sim, instrument_list

# A real NIFTY futures record as published in the Upstox NSE instrument list
# (captured 2026-11-03). Lot size 65, tick_size in paise, expiry in epoch ms.
REAL_FUT_RECORD = {
    "weekly": False, "segment": "NSE_FO", "name": "NIFTY", "exchange": "NSE", "expiry": 1795458599000,
    "instrument_type": "FUT", "asset_symbol": "NIFTY", "underlying_symbol": "NIFTY",
    "instrument_key": "NSE_FO|61471", "lot_size": 65, "freeze_quantity": 1755.0, "exchange_token": "61471",
    "minimum_lot": 65, "asset_key": "NSE_INDEX|Nifty 50", "underlying_key": "NSE_INDEX|Nifty 50",
    "tick_size": 10.0, "asset_type": "INDEX", "underlying_type": "INDEX",
    "trading_symbol": "NIFTY FUT 23 NOV 26", "strike_price": 0.0, "qty_multiplier": 1.0,
}
REAL_OPT_RECORD = {
    "weekly": False, "segment": "NSE_FO", "name": "NIFTY", "exchange": "NSE", "expiry": 1798568999000,
    "instrument_type": "CE", "asset_symbol": "NIFTY", "underlying_symbol": "NIFTY",
    "instrument_key": "NSE_FO|50978", "lot_size": 65, "freeze_quantity": 1755.0, "exchange_token": "50978",
    "minimum_lot": 65, "asset_key": "NSE_INDEX|Nifty 50", "underlying_key": "NSE_INDEX|Nifty 50",
    "tick_size": 5.0, "asset_type": "INDEX", "underlying_type": "INDEX",
    "trading_symbol": "NIFTY 27000 CE 29 DEC 26", "strike_price": 27000.0, "qty_multiplier": 1.0,
}

CFG = load_config(use_default_env_file=False)


def master(*records: dict[str, Any], today: date = date(2026, 11, 3)) -> InstrumentMaster:
    return build_master([parse_instrument(r) for r in records], today)


# --------------------------------------------------------------------------- #
# Real record shape
# --------------------------------------------------------------------------- #


def test_real_upstox_records_parse_with_lot_tick_and_expiry() -> None:
    fut = parse_instrument(REAL_FUT_RECORD)
    assert fut.lot_size == 65 and fut.tick_size == pytest.approx(0.10)
    assert fut.expiry == date(2026, 11, 23)  # 24 Nov is a holiday -> expiry moved to Monday 23 Nov
    assert fut.instrument_type == "FUT" and fut.option_type is None
    opt = parse_instrument(REAL_OPT_RECORD)
    assert opt.lot_size == 65 and opt.tick_size == pytest.approx(0.05) and opt.expiry == date(2026, 12, 29)
    assert opt.option_type is OptionType.CE and opt.strike == 27000


# --------------------------------------------------------------------------- #
# Current-month future
# --------------------------------------------------------------------------- #

FUTS = (future_contract("2026-11-23", "NSE_FO|NOV"), future_contract("2026-12-29", "NSE_FO|DEC"),
        future_contract("2027-01-25", "NSE_FO|JAN"))
OPTS = (option_contract(24500, "CE", "2026-11-10"), option_contract(24500, "PE", "2026-12-29"))


@pytest.mark.parametrize(("today", "expected"), [
    (date(2026, 11, 3), "NSE_FO|NOV"),
    (date(2026, 11, 23), "NSE_FO|NOV"),   # expiry day: still the current month
    (date(2026, 11, 25), "NSE_FO|DEC"),   # day after expiry: rolled
    (date(2026, 12, 30), "NSE_FO|JAN"),
])
def test_current_month_future(today: date, expected: str) -> None:
    m = master(*FUTS, *OPTS, today=today)
    assert m.current_month_future(today).instrument_key == expected


def test_no_unexpired_future_raises() -> None:
    m = master(future_contract("2026-10-27", "NSE_FO|OCT"), *OPTS)
    with pytest.raises(InstrumentMasterError, match="futures"):
        m.current_month_future(date(2026, 11, 3))


def test_options_expiry_and_lot_size_from_list() -> None:
    m = master(*(option_contract(s, o, "2026-11-10", lot=65) for s in (24400, 24500) for o in ("CE", "PE")),
               option_contract(24500, "CE", "2026-11-17", key="NSE_FO|24500CE-17NOV", lot=65),
               future_contract("2026-11-23", "NSE_FO|F", lot=65))
    assert m.nearest_option_expiry(date(2026, 11, 3)) == date(2026, 11, 10)
    assert m.option_lot_size(date(2026, 11, 10)) == 65
    assert len(m.nifty_options(date(2026, 11, 10))) == 4
    assert all(o.option_type is not None for o in m.nifty_options())
    summary = m.validate_nifty(date(2026, 11, 3))
    assert summary["option_lot_size"] == 65 and summary["future_key"] == "NSE_FO|F"
    assert m.lot_size("NSE_FO|F") == 65
    with pytest.raises(InstrumentMasterError, match="not in the Upstox instrument list"):
        m.lot_size("NSE_FO|unknown")


def test_inconsistent_lot_sizes_rejected() -> None:
    m = master(option_contract(24500, "CE", "2026-11-10", key="A", lot=65),
               option_contract(24600, "CE", "2026-11-10", key="B", lot=75), future_contract("2026-11-23", "F"))
    with pytest.raises(InstrumentMasterError, match="single lot size"):
        m.validate_nifty(date(2026, 11, 3))


def test_list_without_options_rejected() -> None:
    with pytest.raises(InstrumentMasterError, match="no NIFTY options"):
        master(*FUTS)


# --------------------------------------------------------------------------- #
# Instrument list download (mocked CDN)
# --------------------------------------------------------------------------- #

TOKEN = "secret-access-token-0123456789abcdef"


def gz(records: list[dict[str, Any]]) -> bytes:
    return gzip.compress(json.dumps(records).encode())


def client(handler: Any) -> tuple[UpstoxRestClient, UpstoxRateLimiter, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return handler(req)

    limiter = UpstoxRateLimiter(CFG.risk.rate_limits.effective, CircuitBreakerSection(), clock=FakeClock(),
                                queue_timeout=1000)
    c = UpstoxRestClient(token_provider=lambda: TOKEN, limiter=limiter, mode=CFG.mode, settings=BrokerSection(),
                         http=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500))),
                         cdn_http=httpx.AsyncClient(transport=httpx.MockTransport(record)), clock=FakeClock(),
                         now_fn=FakeWallClock())
    return c, limiter, seen


async def test_instrument_download_filters_and_never_sends_token() -> None:
    other = {**REAL_OPT_RECORD, "underlying_symbol": "BANKNIFTY", "instrument_key": "NSE_FO|BN"}
    equity = {"segment": "NSE_EQ", "instrument_key": "NSE_EQ|X", "instrument_type": "EQ"}
    broken = {**REAL_OPT_RECORD, "instrument_key": "NSE_FO|BROKEN", "lot_size": None}
    payload = gz([REAL_FUT_RECORD, REAL_OPT_RECORD, other, equity, broken])
    c, limiter, seen = client(lambda r: httpx.Response(200, content=payload))
    insts = await c.get_instruments(("NIFTY",))
    assert {i.instrument_key for i in insts} == {"NSE_FO|61471", "NSE_FO|50978"}
    assert all(i.lot_size == 65 for i in insts)
    req = seen[0]
    assert str(req.url) == BrokerSection().instruments_url
    assert "authorization" not in {k.lower() for k in req.headers}  # public CDN: token never sent
    assert TOKEN not in str(req.headers)
    assert limiter.usage()["standard_api"]["granted"] == 1  # still rate limited


async def test_instrument_download_retries_then_fails_cleanly() -> None:
    responses = [httpx.Response(503), httpx.Response(200, content=gz([REAL_FUT_RECORD]))]
    c, _, seen = client(lambda r: responses.pop(0))
    assert len(await c.get_instruments()) == 1 and len(seen) == 2
    c2, _, _ = client(lambda r: httpx.Response(200, content=b"not gzip"))
    with pytest.raises(BrokerError, match="gzip"):
        await c2.get_instruments()


def test_instruments_url_is_official_only() -> None:
    with pytest.raises(ValueError):
        BrokerSection(instruments_url="https://evil.example.com/NSE.json.gz")  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Lot size flows from the list through sizing, orders and fills
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("lot", [65, 40])
async def test_trade_quantity_follows_instrument_list_lot_size(lot: int) -> None:
    sim = Sim(at(9, 14), instruments=instrument_list(lot=lot))
    await sim.bot.prepare()
    sim.bot.start()
    await sim.run_path(ORB_DAY)
    (t,) = sim.bot.stack.positions.open_trades("orb_options")
    assert t.instrument.lot_size == lot
    assert t.filled_qty == lot  # 1 lot at a 25% stop fits the 2,000 risk budget for both
    entry = next(o for o in sim.repos.orders.list_for_trade(t.trade_uid) if o.purpose.value == "ENTRY")
    assert entry.quantity % lot == 0 and entry.status is OrderStatus.COMPLETE
    assert sim.bot.snapshot()["instruments"]["option_lot_size"] == lot


async def test_quantity_not_matching_listed_lot_is_rejected_by_paper_broker() -> None:
    from core.enums import OrderPurpose, OrderSide, OrderType
    from execution.order_manager import OrderIntent

    sim = Sim(at(9, 14), instruments=instrument_list(lot=65))
    await sim.bot.prepare()
    inst = sim.bot.instruments["NSE_FO|24500CE"]
    await sim.step(at(9, 20), 24500.0)  # index first...
    await sim.step(at(9, 20, 5), 24500.0)  # ...then option quotes are live
    rec = await sim.bot.stack.orders.submit(OrderIntent("orb_options", inst, OrderSide.BUY, 75, OrderType.MARKET,
                                                        OrderPurpose.ENTRY))
    assert rec.status is OrderStatus.REJECTED and "lot size 65" in (rec.status_message or "")


# --------------------------------------------------------------------------- #
# Futures auto-selection and roll-over in the bot
# --------------------------------------------------------------------------- #


def roll_list(lot: int = 65) -> list[Any]:
    opts = [parse_instrument(option_contract(k, o, e, key=f"NSE_FO|{k}{o}{e}", lot=lot))
            for e in ("2026-11-26", "2026-12-01") for k in range(24300, 24850, 50) for o in ("CE", "PE")]
    futs = [parse_instrument(future_contract("2026-11-23", "NSE_FO|NOV", lot)),
            parse_instrument(future_contract("2026-12-29", "NSE_FO|DEC", lot))]
    return opts + futs


async def test_vwap_uses_current_month_future_automatically() -> None:
    sim = Sim(at(9, 14))
    await sim.bot.prepare()
    vwap = next(s for s in sim.bot.strategies if s.strategy_id == "vwap_ema")
    orb = next(s for s in sim.bot.strategies if s.strategy_id == "orb_options")
    assert vwap.signal_instrument_key == FUT  # nearest unexpired NIFTY future in the list
    assert orb.signal_instrument_key == "NSE_INDEX|Nifty 50"
    assert any({"NSE_INDEX|Nifty 50", FUT} <= add for add, _ in sim.subs)  # both feeds subscribed
    assert sim.bot.snapshot()["instruments"]["future_key"] == FUT


async def test_start_of_day_rolls_future_after_expiry_and_refreshes_lot_sizes() -> None:
    sim = Sim(datetime_at(2026, 11, 23, 8, 50), instruments=roll_list(lot=65))
    await sim.bot.prepare()
    vwap = next(s for s in sim.bot.strategies if s.strategy_id == "vwap_ema")
    assert vwap.signal_instrument_key == "NSE_FO|NOV"  # expiry day: still November
    vwap.ema.update(24500)
    sim.subs.clear()

    # 24 Nov is a holiday; next trading day 25 Nov, and NSE revised the lot size.
    sim.wall.now = datetime_at(2026, 11, 25, 9, 0)
    await sim.bot.start_of_day(sim.wall.now.date(), roll_list(lot=60))
    assert vwap.signal_instrument_key == "NSE_FO|DEC"
    assert vwap.ema.count == 0  # indicators restarted for the new contract
    assert ({"NSE_FO|DEC"}, {"NSE_FO|NOV"}) in sim.subs
    assert sim.bot.instrument_summary["option_lot_size"] == 60
    assert sim.bot.instruments["NSE_FO|24500CE2026-11-26"].lot_size == 60
    assert sim.broker._instruments["NSE_FO|24500CE2026-11-26"].lot_size == 60  # broker validates with new lot


async def test_start_of_day_keeps_contract_when_not_expired() -> None:
    sim = Sim(at(9, 0))
    await sim.bot.prepare()
    sim.subs.clear()
    await sim.bot.start_of_day(date(2026, 11, 4))
    vwap = next(s for s in sim.bot.strategies if s.strategy_id == "vwap_ema")
    assert vwap.signal_instrument_key == FUT and sim.subs == []
    assert sim.bot.source.calls == 2  # type: ignore[attr-defined]  # list re-downloaded every day


def datetime_at(y: int, mo: int, d: int, h: int, mi: int) -> Any:
    from datetime import datetime

    from core.clock import IST

    return datetime(y, mo, d, h, mi, tzinfo=IST)


# --------------------------------------------------------------------------- #
# Holidays
# --------------------------------------------------------------------------- #


def test_2026_nse_holidays_loaded() -> None:
    s = CFG.settings.market_session
    assert len(s.holidays) == 16
    assert all(d.weekday() < 5 for d in s.holidays)
    for d in (date(2026, 1, 15), date(2026, 1, 26), date(2026, 11, 10), date(2026, 11, 24), date(2026, 12, 25)):
        assert d in s.holidays and not s.is_trading_day(d)
    assert s.is_trading_day(date(2026, 11, 23))
    assert not s.is_open(datetime_at(2026, 11, 24, 10, 0))
    assert s.holidays_known_for(2026) and not s.holidays_known_for(2027)


@pytest.mark.parametrize(("value", "msg"), [(["2026-11-07"], "weekdays"), (["2026-11-10", "2026-11-10"], "duplicate"),
                                            (["24-11-2026"], "YYYY-MM-DD")])
def test_invalid_holidays_rejected(value: list[str], msg: str) -> None:
    with pytest.raises(ValueError, match=msg):
        MarketSession(holidays=value)  # type: ignore[arg-type]


def test_invalid_holiday_in_yaml_refuses_to_load(config_dir: Any, edit_yaml: Any) -> None:
    edit_yaml("settings.yaml", lambda d: d["market_session"].__setitem__("holidays", ["2026-11-08"]))  # Sunday
    with pytest.raises(ConfigError, match="weekdays"):
        load_config(config_dir, use_default_env_file=False)


async def test_bot_warns_when_year_has_no_holidays(caplog: pytest.LogCaptureFixture) -> None:
    sim = Sim(datetime_at(2027, 1, 4, 9, 0), instruments=[
        parse_instrument(option_contract(24500, "CE", "2027-01-12")),
        parse_instrument(future_contract("2027-01-25", "NSE_FO|JAN27"))])
    with caplog.at_level(logging.CRITICAL):
        await sim.bot.prepare()
    assert any("no NSE trading holidays configured" in r.getMessage() for r in caplog.records)


async def test_no_trading_on_holiday() -> None:
    sim = Sim(datetime_at(2026, 11, 24, 9, 14), instruments=roll_list())
    await sim.bot.prepare()
    sim.bot.start()
    assert sim.bot.engine is not None
    sim.wall.now = datetime_at(2026, 11, 24, 10, 0)  # mid-"session" on a holiday
    assert not sim.bot.engine.accepting_signals
    assert sim.bot.stack.positions.open_trade_count() == 0
