"""D2 end to end on a SYNTHETIC PAPER session: the bot trades an ORB breakout
that is stopped out, records everything (D1), and the shadow harness, reading
the trading database read-only, reproduces that trade from the recorded quotes."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from broker.charges import ChargeCalculator
from core.clock import IST
from research.__main__ import shadow_rules
from research.collect.archive import archive_recorded_quotes
from research.shadow.calibrate import ActualTrade
from research.shadow.report import render_text, summarize
from research.shadow.run import ShadowInputs, evaluate
from research.shadow.signals import from_table_rows, merge
from research.shadow.simulate import Contract, Quote
from research.store import ResearchStore
from research.trading_readonly import closed_trades, open_trading_db_readonly, signal_rows
from tests.fakes import FakeWallClock
from tests.paper_support import FakeConnections, paper_cfg, session
from tests.synthetic import INDEX, at, index_path, tick
from tests.unit.test_bot_integration import ORB_DAY, NoSource, instrument_list

#: ORB breakout (entry about 09:50), then the index keeps rising while the CE premium collapses
DAY_PATH = [*ORB_DAY, (at(10, 5), 24570.0), (at(10, 20), 24575.0)]


def premium(t: datetime) -> float:
    start, end = at(9, 52), at(10, 5)
    if t <= start:
        return 100.0
    if t >= end:
        return 60.0
    return round(100.0 - 40.0 * (t - start) / (end - start), 2)


async def drive(s: Any, conns: FakeConnections, points: list[tuple[datetime, float]],
                prem: Callable[[datetime], float]) -> None:
    bot, md = s.bot, conns.md
    first = True
    for t, spot in index_path(points):
        conns.wall.now = t
        if md.ltp(INDEX) is not None:
            for key in bot.universe.wanted(spot) | bot.universe.subscribed:
                md.on_tick(tick(key, prem(t), t, spread=0.1, vtt=250_000, oi=1_200_000))
        md.on_tick(tick(INDEX, spot, t, spread=0.0))
        await bot.settle()
        if first:
            await bot.recenter_universe()
            first = False


def _ms(at_: datetime) -> int:
    return int(at_.timestamp() * 1000)


def _dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=IST)


async def test_the_shadow_harness_reproduces_a_real_paper_trade(tmp_path: Path) -> None:
    cfg = paper_cfg(tmp_path)
    insts = instrument_list()
    wall = FakeWallClock(at(9, 14))
    conns = FakeConnections(wall, NoSource(insts))
    s = session(cfg, wall, conns)
    await s.start()
    try:
        await drive(s, conns, DAY_PATH, premium)
        assert s.market_recorder is not None
        s.market_recorder.close()
    finally:
        await s.shutdown()

    trading_path = cfg.database_path
    before = sqlite3.connect(trading_path).execute("SELECT COUNT(*) FROM option_quotes").fetchone()[0]
    store = ResearchStore.open(tmp_path / "research.db", forbidden=[trading_path])
    store.upsert_contracts(insts)
    con = open_trading_db_readonly(trading_path)
    archive_recorded_quotes(con, store, now=lambda: at(18, 0))
    (closed,) = [ActualTrade.from_row(r) for r in closed_trades(con)]
    assert closed.exit_reason == "STOP_LOSS"

    contracts = [Contract(k, str(c["instrument_type"]), float(c["strike"]), date.fromisoformat(str(c["expiry"])),
                          int(c["lot_size"])) for k, c in store.contracts().items()
                 if c["instrument_type"] in ("CE", "PE")]

    def quotes(key: str, start: datetime, end: datetime) -> list[Quote]:
        rows = store.archived_quotes(key, _ms(start), _ms(end))
        return [Quote(_dt(r["received_ms"]), _dt(r["exchange_ms"]), r["ltp"], r["bid"], r["ask"], r["bid_qty"],
                      r["ask_qty"], r["volume_today"], r["oi"]) for r in rows]

    def spot(at_: datetime) -> tuple[float, str] | None:
        r = store.last_archived_quote(INDEX, _ms(at_))
        return None if r is None else (float(r["ltp"]), "INDEX_QUOTE")

    inp = ShadowInputs(rules=shadow_rules(cfg, 0.05), contracts=contracts, quotes=quotes, spot=spot,
                       cost_fn=ChargeCalculator(cfg.settings.costs).round_trip, trades=[closed])
    signals = merge(from_table_rows(signal_rows(con)), [], {})
    assert [x.source for x in signals] == ["TABLE"] and signals[0].trade_uid == closed.trade_uid
    rows, calib = evaluate(signals, inp)
    (row,) = rows
    (c,) = calib
    assert row["status"] == "FILLED" and row["instrument_key"] == closed.instrument_key
    assert row["exit_reason"] == "STOP_LOSS" and row["entry_price"] == closed.entry_price
    assert c["contract_match"] and c["within_tolerance"] and c["judged_on"] == "point_in_time"
    pit = c["point_in_time"]
    assert pit["entry_ticks"] == 0.0 and pit["exit_outside_band_ticks"] == 0.0 and pit["stop_ticks"] == 0.0
    assert abs(pit["net_per_lot_diff"]) < 1.0  # the bot's own charges, the same fills

    assert store.upsert_shadow(rows) == 1 and store.upsert_shadow(rows) == 1  # recomputed in place
    assert len(store.shadow_rows("d2.1")) == 1
    text = "\n".join(render_text(summarize(rows, calib), rows, calib))
    assert "1 of 1 reproduced" in text and "REPRODUCED" in text
    con.close()
    store.close()
    after = sqlite3.connect(trading_path).execute("SELECT COUNT(*) FROM option_quotes").fetchone()[0]
    assert after == before  # the trading database was only read
    assert timedelta(0) <= closed.created_at - signals[0].generated_at < timedelta(seconds=1)
