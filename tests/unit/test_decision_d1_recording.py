"""Decision-engine D1: the data recorder (SYNTHETIC data only).

Recording must never raise and never block trading. It records Greeks, the
resting total buy / sell quantity, last traded quantity and average traded
price; NIFTY index, future and India VIX quotes; every signal with its entry
decision; and the next weekly expiry. None of what is recorded only for
research can ever be traded or gate an entry.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from broker.proto import MarketDataFeedV3_pb2 as pb
from broker.upstox_mapping import decode_feed_response, parse_instrument
from config.loader import load_config
from config.settings import MarketRecordingSection
from core.enums import OptionType, TradingMode
from core.event_bus import Event, Topic
from core.models import Greeks, Signal, Tick
from data.storage.database import SCHEMA_VERSION, Database
from data.storage.repositories import Repositories, SignalRow
from runtime import market_recorder as mr
from runtime.market_recorder import MarketRecorder
from tests.fakes import T0, FakeWallClock, ms, option_contract
from tests.paper_support import FakeConnections, drive, paper_cfg, session
from tests.synthetic import FUT, INDEX, at, five_min, tick
from tests.unit.test_bot_integration import ORB_DAY, NoSource, Sim, instrument_list

CFG = load_config(use_default_env_file=False)
VIX = CFG.settings.instruments.india_vix_key
OPT = "NSE_FO|24500CE"


def _db() -> tuple[Database, Repositories]:
    db = Database(":memory:")
    db.initialize()
    return db, Repositories(db, TradingMode.PAPER)


def _settings(**kw: Any) -> MarketRecordingSection:
    return CFG.settings.market_recording.model_copy(update=kw)


def _recorder(repo: Any, **kw: Any) -> MarketRecorder:
    opts: dict[str, Any] = {"is_option": lambda k: k.endswith(("CE", "PE")), "held_keys": set,
                            "is_market": lambda k: k in {INDEX, FUT, VIX}}
    settings = kw.pop("settings", CFG.settings.market_recording)
    opts.update(kw)
    return MarketRecorder(repo, settings, **opts)


def rich_tick(key: str, ltp: float, t: datetime) -> Tick:
    return tick(key, ltp, t, vtt=1000, oi=5000).model_copy(update={
        "ltq": 65, "atp": ltp - 0.2, "total_buy_qty": 120_000.0, "total_sell_qty": 95_000.0, "iv": 0.14,
        "greeks": Greeks(delta=0.51, gamma=0.0012, theta=-9.5, vega=11.2)})


def signal(sid: str = "SIG-1", at_: datetime | None = None) -> Signal:
    t = at_ or at(9, 50)
    return Signal(signal_id=sid, strategy_id="orb_options", strategy_version="1.0.0", option_type=OptionType.CE,
                  generated_at=t, candle_end=t, underlying_key=INDEX, underlying_price=24560.0,
                  reason="ORB breakout", features={"or_high": 24518.0})


def decision(sid: str = "SIG-1", approved: bool = False) -> Event:
    return Event(Topic.ENTRY_DECISION, {"signal_id": sid, "strategy_id": "orb_options", "approved": approved,
                                        "reasons": [] if approved else ["position size zero"],
                                        "trade_uid": "PAPER-T1" if approved else None,
                                        "quantity": 65 if approved else None, "ml_score": None})


class SpyRepo:
    """Counts every database call; ``fail`` makes the named saves raise."""

    def __init__(self, real: Any, fail: set[str] | None = None) -> None:
        self.real, self.fail, self.calls = real, fail or set(), 0

    def __getattr__(self, name: str) -> Any:
        target = getattr(self.real, name)

        def call(*a: Any, **k: Any) -> Any:
            self.calls += 1
            if name in self.fail:
                raise sqlite3.OperationalError(f"simulated failure in {name}")
            return target(*a, **k)

        return call


# --------------------------------------------------------------------------- #
# Schema v7
# --------------------------------------------------------------------------- #


def test_schema_v7_adds_the_recording_tables_and_columns() -> None:
    db, _ = _db()
    assert SCHEMA_VERSION == 7
    assert {"market_quotes", "signals"} <= set(db.tables())
    cols = {r[1] for r in db.query("PRAGMA table_info(option_quotes)")}
    assert {"delta", "gamma", "theta", "vega", "tbq", "tsq", "ltq", "atp"} <= cols


def test_a_v6_database_migrates_and_keeps_its_quotes(tmp_path: Path) -> None:
    path = tmp_path / "v6.db"
    raw = sqlite3.connect(path)
    raw.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
    raw.execute("INSERT INTO schema_version VALUES (6, '2026-11-13T15:30:00+05:30')")
    raw.execute("""CREATE TABLE option_quotes (id INTEGER PRIMARY KEY AUTOINCREMENT, instrument_key TEXT NOT NULL,
        exchange_time TEXT NOT NULL, received_at TEXT NOT NULL, ltp REAL NOT NULL, bid REAL, ask REAL,
        bid_qty INTEGER, ask_qty INTEGER, volume_today INTEGER, oi REAL, iv REAL,
        held INTEGER NOT NULL DEFAULT 0 CHECK (held IN (0,1)))""")
    raw.execute("INSERT INTO option_quotes(instrument_key, exchange_time, received_at, ltp) "
                "VALUES ('NSE_FO|1', '2026-11-13T10:00:00+05:30', '2026-11-13T10:00:00+05:30', 100.0)")
    raw.commit()
    raw.close()
    db = Database(path)
    db.initialize()
    assert db.current_schema_version() == 7
    (row,) = db.query("SELECT * FROM option_quotes")
    assert row["ltp"] == 100.0 and row["delta"] is None and row["tbq"] is None
    db.initialize()  # idempotent
    db.close()


def test_recorded_signals_are_append_only() -> None:
    db, repos = _db()
    repos.market_history.save_signals([SignalRow(signal(), TradingMode.PAPER, decided=False)])
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute("UPDATE signals SET approved = 1")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute("DELETE FROM signals")
    # writing the same signal again is harmless (kept as first recorded)
    repos.market_history.save_signals([SignalRow(signal(), TradingMode.PAPER, decided=True, approved=True)])
    (row,) = repos.market_history.signals()
    assert row["decided"] == 0 and row["approved"] is None


def test_an_undecided_signal_cannot_claim_an_outcome() -> None:
    db, _ = _db()
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO signals(signal_id, mode, strategy_id, strategy_version, option_type, generated_at,"
                   " candle_end, underlying_key, underlying_price, reason, decided, approved, recorded_at)"
                   " VALUES ('S', 'PAPER', 's', '1', 'CE', 't', 't', 'k', 1.0, 'r', 0, 1, 't')")


# --------------------------------------------------------------------------- #
# Feed mapping
# --------------------------------------------------------------------------- #


def test_the_full_feed_total_buy_and_sell_quantity_is_decoded() -> None:
    r = pb.FeedResponse(type=pb.live_feed, currentTs=ms(T0))
    m = r.feeds["NSE_FO|1"].fullFeed.marketFF
    m.ltpc.ltp, m.ltpc.ltt, m.ltpc.ltq = 120.0, ms(T0), 65
    q = m.marketLevel.bidAskQuote.add()
    q.bidP, q.askP, q.bidQ, q.askQ = 119.5, 120.5, 650, 1300
    m.tbq, m.tsq, m.atp, m.iv = 250_000.0, 180_000.0, 118.4, 0.142
    m.optionGreeks.delta, m.optionGreeks.vega = 0.5, 11.0
    _, (t,), _ = decode_feed_response(r.SerializeToString())
    assert (t.total_buy_qty, t.total_sell_qty, t.ltq, t.atp) == (250_000.0, 180_000.0, 65, pytest.approx(118.4))
    assert t.greeks is not None and t.greeks.vega == 11.0


def test_an_index_feed_has_no_resting_quantity() -> None:
    r = pb.FeedResponse(type=pb.live_feed, currentTs=ms(T0))
    r.feeds[VIX].fullFeed.indexFF.ltpc.ltp = 13.2
    r.feeds[VIX].fullFeed.indexFF.ltpc.ltt = ms(T0)
    _, (t,), _ = decode_feed_response(r.SerializeToString())
    assert t.ltp == 13.2 and t.total_buy_qty is None and t.total_sell_qty is None


# --------------------------------------------------------------------------- #
# What is recorded
# --------------------------------------------------------------------------- #


def test_option_quotes_keep_greeks_resting_quantity_ltq_and_atp() -> None:
    _, repos = _db()
    rec = _recorder(repos.market_history)
    rec.on_tick(rich_tick(OPT, 100.0, at(10, 0)))
    rec.flush()
    (row,) = repos.market_history.quotes(OPT)
    assert (row["delta"], row["gamma"], row["theta"], row["vega"]) == (0.51, 0.0012, -9.5, 11.2)
    assert (row["tbq"], row["tsq"], row["ltq"], row["atp"], row["iv"]) == (120_000.0, 95_000.0, 65, 99.8, 0.14)


def test_index_future_and_vix_quotes_are_sampled_into_market_quotes() -> None:
    _, repos = _db()
    rec = _recorder(repos.market_history)
    for s in range(0, 11):
        for key, px in ((INDEX, 24500.0), (FUT, 24560.0), (VIX, 13.1)):
            rec.on_tick(rich_tick(key, px, at(10, 0, s)))
    rec.on_tick(rich_tick("NSE_EQ|OTHER", 1.0, at(10, 0)))  # neither option nor market key: ignored
    rec.flush()
    for key in (INDEX, FUT, VIX):
        rows = repos.market_history.market_quotes(key)
        assert len(rows) == 3  # 0, 5, 10 s
    assert repos.market_history.market_quotes(FUT)[0]["tbq"] == 120_000.0
    assert repos.market_history.quotes(INDEX) == []  # never in option_quotes


def test_market_quotes_can_be_switched_off() -> None:
    _, repos = _db()
    rec = _recorder(repos.market_history, settings=_settings(record_market_quotes=False))
    rec.on_tick(rich_tick(INDEX, 24500.0, at(10, 0)))
    assert rec.flush() == 0


def test_retention_applies_to_market_quotes_too() -> None:
    _, repos = _db()
    rec = _recorder(repos.market_history)
    repos.market_history.save_market_quotes([tick(INDEX, 1.0, at(10, 0, day=(2026, 10, 1))),
                                             tick(INDEX, 2.0, at(10, 0))])
    assert rec.purge_old_quotes(at(10, 0).date()) == 1
    assert [r["ltp"] for r in repos.market_history.market_quotes(INDEX)] == [2.0]


def test_a_signal_is_recorded_with_its_entry_decision() -> None:
    _, repos = _db()
    rec = _recorder(repos.market_history)
    rec.on_signal(Event(Topic.SIGNAL, signal("SIG-A")))
    rec.on_decision(decision("SIG-A", approved=False))
    rec.on_signal(Event(Topic.SIGNAL, signal("SIG-B")))
    rec.on_decision(decision("SIG-B", approved=True))
    rec.on_decision(decision("SIG-UNKNOWN"))  # a decision without a signal is ignored
    rec.flush()
    rows = {r["signal_id"]: r for r in repos.market_history.signals()}
    assert set(rows) == {"SIG-A", "SIG-B"}
    a, b = rows["SIG-A"], rows["SIG-B"]
    assert (a["decided"], a["approved"], json.loads(a["decision_reasons_json"])) == (1, 0, ["position size zero"])
    assert (b["approved"], b["trade_uid"], b["quantity"], b["mode"]) == (1, "PAPER-T1", 65, "PAPER")
    assert json.loads(a["features_json"]) == {"or_high": 24518.0} and a["reason"] == "ORB breakout"


def test_a_signal_without_a_decision_is_kept_as_undecided_at_close() -> None:
    _, repos = _db()
    rec = _recorder(repos.market_history)
    rec.on_signal(Event(Topic.SIGNAL, signal("SIG-LATE")))
    rec.close()
    (row,) = repos.market_history.signals()
    assert (row["decided"], row["approved"]) == (0, None)


def test_too_many_waiting_signals_are_written_undecided(monkeypatch: pytest.MonkeyPatch) -> None:
    _, repos = _db()
    monkeypatch.setattr(mr, "MAX_PENDING_SIGNALS", 2)
    rec = _recorder(repos.market_history)
    for i in range(3):
        rec.on_signal(Event(Topic.SIGNAL, signal(f"SIG-{i}")))
    rec.flush()
    assert [r["signal_id"] for r in repos.market_history.signals()] == ["SIG-0"]


# --------------------------------------------------------------------------- #
# Never raises
# --------------------------------------------------------------------------- #


def _raise(*_: Any) -> Any:
    raise RuntimeError("boom")


@pytest.mark.parametrize("broken", ["is_option", "held_keys", "is_market"])
def test_a_failing_callback_never_raises_from_on_tick(broken: str, caplog: pytest.LogCaptureFixture) -> None:
    _, repos = _db()
    rec = _recorder(repos.market_history, **{broken: _raise})
    with caplog.at_level(logging.ERROR):
        for s in range(3):
            rec.on_tick(rich_tick(OPT if broken != "is_market" else INDEX, 100.0, at(10, 0, s * 5)))
    assert rec.listener_failures == 3
    assert len([r for r in caplog.records if "listener failed" in r.getMessage()]) == 1  # not one line per tick


def test_garbage_bus_payloads_never_raise() -> None:
    _, repos = _db()
    rec = _recorder(repos.market_history)
    rec.on_signal(Event(Topic.SIGNAL, {"not": "a signal"}))
    rec.on_signal(Event(Topic.SIGNAL, None))
    rec.on_decision(Event(Topic.ENTRY_DECISION, None))
    rec.on_decision(Event(Topic.ENTRY_DECISION, 42))
    rec.on_signal(Event(Topic.SIGNAL, signal("SIG-X")))
    rec.on_decision(Event(Topic.ENTRY_DECISION, {"signal_id": "SIG-X", "quantity": "not a number"}))
    assert rec.listener_failures == 3  # None and 42 have no .get, the quantity is not a number
    assert rec.flush() == 0


@pytest.mark.parametrize("table", ["save_signals", "save_candles", "save_quotes", "save_market_quotes"])
def test_one_failing_table_never_raises_and_the_others_are_still_written(table: str,
                                                                         caplog: pytest.LogCaptureFixture) -> None:
    _, repos = _db()
    spy = SpyRepo(repos.market_history, fail={table})
    rec = _recorder(spy)
    rec.on_signal(Event(Topic.SIGNAL, signal()))
    rec.on_decision(decision())
    rec.on_candle(five_min(FUT, at(9, 15), [24500.0])[0])
    rec.on_tick(rich_tick(OPT, 100.0, at(10, 0)))
    rec.on_tick(rich_tick(INDEX, 24500.0, at(10, 0)))
    with caplog.at_level(logging.ERROR):
        assert rec.flush() == 3
    assert rec.failures == 1 and rec.buffered == 0
    h = repos.market_history
    counts = {"save_signals": len(h.signals()), "save_candles": len(h.candles(FUT, 5, at(9, 15).date())),
              "save_quotes": len(h.quotes(OPT)), "save_market_quotes": len(h.market_quotes(INDEX))}
    assert counts.pop(table) == 0 and set(counts.values()) == {1}


def test_close_never_raises_when_the_database_is_gone() -> None:
    db, repos = _db()
    rec = _recorder(repos.market_history)
    rec.on_signal(Event(Topic.SIGNAL, signal()))
    rec.on_tick(rich_tick(OPT, 100.0, at(10, 0)))
    db.close()
    db.execute = _raise  # type: ignore[method-assign]
    db.transaction = _raise  # type: ignore[method-assign]
    assert rec.close() == 0
    assert rec.failures == 2 and rec.buffered == 0


# --------------------------------------------------------------------------- #
# Never blocks
# --------------------------------------------------------------------------- #


def test_listeners_never_touch_the_database() -> None:
    _, repos = _db()
    spy = SpyRepo(repos.market_history)
    rec = _recorder(spy)
    for s in range(0, 600):
        t = at(10, 0) + timedelta(seconds=s)
        rec.on_tick(rich_tick(OPT, 100.0, t))
        rec.on_tick(rich_tick(INDEX, 24500.0, t))
    rec.on_candle(five_min(FUT, at(9, 15), [24500.0])[0])
    rec.record_backfill(five_min(FUT, at(9, 20), [24500.0, 24501.0]))
    rec.on_signal(Event(Topic.SIGNAL, signal()))
    rec.on_decision(decision())
    assert spy.calls == 0  # only the timer job writes
    assert rec.buffered > 0


def test_a_backlog_is_written_over_several_bounded_flushes() -> None:
    _, repos = _db()
    spy = SpyRepo(repos.market_history)
    rec = _recorder(spy, settings=_settings(max_rows_per_flush=100))
    for s in range(250):
        rec.on_tick(rich_tick(f"NSE_FO|{s}CE", 100.0, at(10, 0)))  # 250 contracts, one quote each
    assert [rec.flush(), rec.flush(), rec.flush(), rec.flush()] == [100, 100, 50, 0]
    assert sum(len(repos.market_history.quotes(f"NSE_FO|{s}CE")) for s in range(250)) == 250


def test_signals_are_written_before_a_quote_backlog() -> None:
    _, repos = _db()
    rec = _recorder(repos.market_history, settings=_settings(max_rows_per_flush=100))
    for s in range(150):
        rec.on_tick(rich_tick(f"NSE_FO|{s}CE", 100.0, at(10, 0)))
    rec.on_signal(Event(Topic.SIGNAL, signal()))
    rec.on_decision(decision())
    rec.flush()
    assert len(repos.market_history.signals()) == 1


def test_full_buffers_drop_rows_instead_of_growing(monkeypatch: pytest.MonkeyPatch,
                                                  caplog: pytest.LogCaptureFixture) -> None:
    _, repos = _db()
    monkeypatch.setattr(mr, "MAX_BUFFERED", 5)
    rec = _recorder(repos.market_history)
    with caplog.at_level(logging.WARNING):
        for s in range(8):
            rec.on_tick(rich_tick(f"NSE_FO|{s}CE", 100.0, at(10, 0)))
    assert rec.buffered == 5 and rec.dropped == 3
    assert len([r for r in caplog.records if "buffer full" in r.getMessage()]) == 1


def test_the_flush_cap_is_bounded() -> None:
    for bad in (99, 50_001):
        with pytest.raises(ValueError):
            MarketRecordingSection(max_rows_per_flush=bad)
    for bad in (-1, 11):
        with pytest.raises(ValueError):
            MarketRecordingSection(next_expiry_band=bad)


# --------------------------------------------------------------------------- #
# Subscriptions: wider band, next expiry, VIX (recording only)
# --------------------------------------------------------------------------- #

NEXT = "2026-11-17"


def two_expiry_list() -> list[Any]:
    """SYNTHETIC list: nearest expiry 24200-24800 (covers ATM 24500 +/- 5), the next
    expiry (keys NSE_FO|N...), and the futures of ``instrument_list``."""
    near = [parse_instrument(option_contract(k, o, "2026-11-10", key=f"NSE_FO|{k}{o}"))
            for k in range(24200, 24850, 50) for o in ("CE", "PE")]
    nxt = [parse_instrument(option_contract(k, o, NEXT, key=f"NSE_FO|N{k}{o}"))
           for k in range(24200, 24850, 50) for o in ("CE", "PE")]
    futures = [i for i in instrument_list() if i.option_type is None]
    return near + nxt + futures


async def _prepared(start: datetime | None = None) -> Sim:
    sim = Sim(start or at(9, 14), instruments=two_expiry_list())
    await sim.bot.prepare()
    return sim


async def test_the_configured_band_is_five_strikes_and_the_next_expiry_three() -> None:
    assert CFG.settings.instruments.strike_band == 5
    assert CFG.settings.market_recording.next_expiry_band == 3
    sim = await _prepared()
    bot = sim.bot
    assert bot.universe is not None and bot.recording_universe is not None
    assert bot.recording_universe.expiry.isoformat() == NEXT
    await sim.step(at(9, 15), 24500.0)
    await bot.recenter_universe()
    trading, recording = bot.universe.subscribed, bot.recording_universe.subscribed
    assert len(trading) == 22 and len(recording) == 14 and not trading & recording
    assert all(k.startswith("NSE_FO|N") for k in recording)
    subscribed = set().union(*(add for add, _ in sim.subs)) - set().union(*(rem for _, rem in sim.subs))
    assert trading | recording | {INDEX, FUT, VIX} <= subscribed
    assert len(bot.subscribed_keys()) == 22 + 14 + 3


async def test_vix_is_subscribed_but_never_a_base_key_of_the_entry_gates() -> None:
    sim = await _prepared()
    assert VIX in sim.subs[0][0]
    assert VIX not in sim.bot.base_keys() and VIX in sim.bot.market_keys()


async def test_a_frozen_vix_never_blocks_entries() -> None:
    sim = await _prepared(at(10, 0))
    for s in range(0, 120, 2):  # VIX ticks at one price for two minutes; NIFTY and the future move
        t = at(10, 0, 0) + timedelta(seconds=s)
        sim.wall.now = t
        sim.md.on_tick(tick(VIX, 13.0, t, spread=0.0))
        sim.md.on_tick(tick(INDEX, 24500.0 + s, t, spread=0.0))
        sim.md.on_tick(tick(FUT, 24560.0 + s, t))
    assert sim.md.frozen_seconds(VIX) is not None and sim.md.frozen_seconds(VIX) > 60  # type: ignore[operator]
    assert sim.bot.frozen_price_block_reason() is None


async def test_a_next_expiry_contract_is_never_selected_for_a_trade() -> None:
    sim = await _prepared()
    bot = sim.bot
    assert bot.universe is not None and bot.recording_universe is not None
    for opt in (OptionType.CE, OptionType.PE):
        for mode, offset in (("ATM", 0), ("ITM", 3), ("OTM", 5)):
            inst = bot.universe.select(24500.0, opt, mode, offset)
            assert inst is not None and inst.expiry is not None and inst.expiry.isoformat() == "2026-11-10"
    assert not set(bot.recording_universe.by_key) & set(bot.universe.by_key)


async def test_a_key_both_universes_want_is_never_unsubscribed() -> None:
    sim = await _prepared()
    bot = sim.bot
    assert bot.universe is not None and bot.recording_universe is not None
    await sim.step(at(9, 15), 24500.0)
    await bot.recenter_universe()
    shared = "NSE_FO|24500CE"  # ATM: the trading universe keeps wanting it
    assert shared in bot.universe.subscribed
    # as on the day the next expiry becomes the nearest: the recording universe held it before
    bot.recording_universe.subscribed.add(shared)
    await sim.step(at(9, 16), 24510.0)
    await bot.recenter_universe()  # the recording universe no longer wants it ...
    removed = set().union(*(rem for _, rem in sim.subs))
    assert shared not in removed  # ... but trading does, so it stays on the feed
    assert not removed & bot.subscribed_keys()


async def test_without_a_next_expiry_nothing_extra_is_recorded() -> None:
    sim = Sim(at(9, 14))  # one expiry in the list
    await sim.bot.prepare()
    assert sim.bot.recording_universe is None


async def test_recording_off_subscribes_nothing_extra(tmp_path: Path) -> None:
    cfg = CFG.model_copy(update={"settings": CFG.settings.model_copy(update={
        "market_recording": _settings(enabled=False)})})
    sim = Sim(at(9, 14), cfg=cfg, instruments=two_expiry_list())
    await sim.bot.prepare()
    assert sim.bot.recording_universe is None and sim.bot.recording_keys() == set()
    assert VIX not in sim.subs[0][0]


# --------------------------------------------------------------------------- #
# A whole PAPER session
# --------------------------------------------------------------------------- #


async def test_a_session_records_signals_decisions_and_market_quotes(tmp_path: Path) -> None:
    wall = FakeWallClock(at(9, 14))
    conns = FakeConnections(wall, NoSource(two_expiry_list()))
    s = session(paper_cfg(tmp_path), wall, conns)
    bot = await s.start()
    try:
        await drive(s, conns, ORB_DAY)
        rec = s.market_recorder
        assert rec is not None
        while rec.buffered:  # a synthetic half hour is more than one bounded flush
            rec.flush()
        signals = bot.repos.market_history.signals()
        assert signals and all(r["decided"] == 1 for r in signals)
        approved = [r for r in signals if r["approved"] == 1]
        (trade,) = bot.stack.positions.open_trades("orb_options")
        assert [r["trade_uid"] for r in approved] == [trade.trade_uid]
        assert bot.repos.market_history.market_quotes(INDEX)
        assert bot.recording_universe is not None and bot.recording_universe.subscribed
        assert conns.feed.subscribed >= bot.recording_universe.subscribed | {VIX}
    finally:
        await s.shutdown()


async def test_trading_is_unaffected_when_every_recording_write_fails(tmp_path: Path,
                                                                     caplog: pytest.LogCaptureFixture) -> None:
    wall = FakeWallClock(at(9, 14))
    conns = FakeConnections(wall, NoSource(two_expiry_list()))
    s = session(paper_cfg(tmp_path), wall, conns)
    bot = await s.start()
    h = bot.repos.market_history
    for name in ("save_candles", "save_quotes", "save_market_quotes", "save_signals"):
        setattr(h, name, _raise)
    try:
        with caplog.at_level(logging.ERROR):
            await drive(s, conns, ORB_DAY)
            assert s.market_recorder is not None
            s.market_recorder.flush()
        assert len(bot.stack.positions.open_trades("orb_options")) == 1  # the ORB entry still happened
        assert s.market_recorder.failures >= 1
    finally:
        await s.shutdown()


# --------------------------------------------------------------------------- #
# Audit follow-ups: shutdown bound, expiry roll, purge at production volume
# --------------------------------------------------------------------------- #

#: generous wall-clock bounds for this laptop (measured: about 3 s and about 1 s);
#: they catch an accidental O(n^2) or a per-row commit, not small slowdowns
CLOSE_BOUND_SECONDS = 30.0
PURGE_BOUND_SECONDS = 10.0
ROWS_PER_DAY = 170_000  # 36 contracts + 3 market keys, 5-second samples, one session


def test_close_drains_a_full_backlog_in_bounded_time(tmp_path: Path) -> None:
    """Shutdown writes everything buffered, even a full buffer after a database
    outage, in bounded flushes and within a bounded time."""
    db = Database(tmp_path / "trading.db")
    db.initialize()
    repos = Repositories(db, TradingMode.PAPER)
    spy = SpyRepo(repos.market_history)
    rec = _recorder(spy)
    base = rich_tick(OPT, 100.0, at(10, 0))
    for i in range(mr.MAX_BUFFERED):  # the largest backlog the recorder ever holds
        rec._quotes.append((base.model_copy(update={"instrument_key": f"NSE_FO|{i % 36}CE",
                                                    "received_at": at(10, 0) + timedelta(milliseconds=i)}), False))
    rec.on_signal(Event(Topic.SIGNAL, signal("SIG-PENDING")))
    started = time.perf_counter()
    written = rec.close()
    elapsed = time.perf_counter() - started
    assert written == mr.MAX_BUFFERED + 1 and rec.buffered == 0
    assert spy.calls <= mr.MAX_BUFFERED // CFG.settings.market_recording.max_rows_per_flush + 3  # batches
    assert elapsed < CLOSE_BOUND_SECONDS, f"close took {elapsed:.1f} s"
    assert db.query_one("SELECT COUNT(*) AS n FROM option_quotes")["n"] == mr.MAX_BUFFERED  # type: ignore[index]
    db.close()


def _replay(subs: list[tuple[set[str], set[str]]]) -> set[str]:
    """The feed's subscriptions after the bot's (add, remove) calls (removes go first)."""
    feed: set[str] = set()
    for add, remove in subs:
        assert not add & remove
        feed = (feed - remove) | add
    return feed


async def test_an_expiry_roll_moves_contracts_between_universes_without_dropping_any() -> None:
    """Day 1: nearest expiry E1 traded (+/- 5), next expiry E2 recorded (+/- 3).
    After E1 expires, start_of_day makes E2 the traded expiry and E3 the
    recorded one. The E2 contracts already on the feed stay subscribed the
    whole time; every E1 contract leaves; E3 joins."""
    e1, e2, e3 = "2026-11-10", "2026-11-17", "2026-11-24"
    futures = [i for i in instrument_list() if i.option_type is None]

    def options(expiry: str, prefix: str) -> list[Any]:
        return [parse_instrument(option_contract(k, o, expiry, key=f"NSE_FO|{prefix}{k}{o}"))
                for k in range(24200, 24850, 50) for o in ("CE", "PE")]

    sim = Sim(at(9, 14), instruments=options(e1, "") + options(e2, "N") + futures)
    bot = sim.bot
    await bot.prepare()
    await sim.step(at(9, 15), 24500.0)
    await bot.recenter_universe()
    assert bot.universe is not None and bot.recording_universe is not None
    day1_e2 = set(bot.recording_universe.subscribed)
    assert len(day1_e2) == 14 and all(k.startswith("NSE_FO|N") for k in day1_e2)
    day1_e1 = set(bot.universe.subscribed)

    day2 = datetime(2026, 11, 11, 9, 0, tzinfo=at(9, 0).tzinfo)
    sim.wall.now = day2
    calls_before = len(sim.subs)
    await bot.start_of_day(day2.date(), instruments=options(e2, "N") + options(e3, "M") + futures)
    assert bot.universe.expiry.isoformat() == e2 and bot.recording_universe.expiry.isoformat() == e3
    await sim.step(day2.replace(minute=15), 24500.0)
    await bot.recenter_universe()

    removed = set().union(*(rem for _, rem in sim.subs[calls_before:]))
    assert not removed & day1_e2  # the E2 contracts never left the feed
    feed = _replay(sim.subs)
    assert not feed & day1_e1  # every expired E1 contract left
    trading, recording = bot.universe.subscribed, bot.recording_universe.subscribed
    assert len(trading) == 22 and all(k.startswith("NSE_FO|N") for k in trading)
    assert len(recording) == 14 and all(k.startswith("NSE_FO|M") for k in recording)
    assert day1_e2 <= trading
    assert feed == trading | recording | bot.base_keys() | bot.recording_keys()


def test_the_daily_retention_purge_at_production_volume_is_bounded(tmp_path: Path) -> None:
    """One trading day of quotes at the D1 volume (about 170,000 rows) is deleted
    in bounded time, and nothing newer is touched."""
    db = Database(tmp_path / "trading.db")
    db.initialize()
    old, new = "2026-10-02T10:00:00+05:30", "2026-11-03T10:00:00+05:30"
    opt = [(f"NSE_FO|{i % 36}CE", ts, ts, 100.0, 99.9, 100.1, 650, 650, 1000, 5000.0, 0.14, 0, 0.5, 0.001, -9.0,
            11.0, 120_000.0, 95_000.0, 65, 99.8) for ts in (old, new) for i in range(ROWS_PER_DAY - 13_000)]
    mkt = [(f"NSE_INDEX|K{i % 3}", ts, ts, 24500.0, None, None, None, None, None, None, None, None, None, None)
           for ts in (old, new) for i in range(13_000)]
    with db.transaction() as conn:
        conn.executemany("INSERT INTO option_quotes(instrument_key, exchange_time, received_at, ltp, bid, ask, "
                         "bid_qty, ask_qty, volume_today, oi, iv, held, delta, gamma, theta, vega, tbq, tsq, ltq, "
                         "atp) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", opt)
        conn.executemany("INSERT INTO market_quotes(instrument_key, exchange_time, received_at, ltp, bid, ask, "
                         "bid_qty, ask_qty, volume_today, oi, tbq, tsq, ltq, atp) "
                         "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", mkt)
    rec = _recorder(Repositories(db, TradingMode.PAPER).market_history)
    started = time.perf_counter()
    deleted = rec.purge_old_quotes(at(10, 0).date())  # 2026-11-03, retention 30 days
    elapsed = time.perf_counter() - started
    assert deleted == ROWS_PER_DAY and rec.failures == 0
    assert elapsed < PURGE_BOUND_SECONDS, f"purge took {elapsed:.1f} s"
    left = db.query_one("SELECT (SELECT COUNT(*) FROM option_quotes) + (SELECT COUNT(*) FROM market_quotes) AS n")
    assert left["n"] == ROWS_PER_DAY  # type: ignore[index]
    db.close()
