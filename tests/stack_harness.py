"""Full PAPER risk+execution stack on in-memory SQLite with a fake clock."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from broker.paper_broker import PaperBroker
from broker.upstox_mapping import parse_instrument
from config.loader import load_config
from config.settings import BotConfig
from core.clock import IST
from core.enums import OptionType, TradingMode
from core.models import Instrument, Tick
from data.market_data import MarketDataService
from data.storage.bootstrap import sync_strategies
from data.storage.database import Database
from data.storage.repositories import Repositories
from execution.position_manager import ManagedTrade
from execution.stack import ExecutionStack, build_stack
from risk.risk_manager import EntryProposal, RiskDecision
from tests.fakes import FakeWallClock, option_contract

CE_KEY = "NSE_FO|24500CE"
PE_KEY = "NSE_FO|24500PE"
CE = parse_instrument(option_contract(24500, "CE", key=CE_KEY))
PE = parse_instrument(option_contract(24500, "PE", key=PE_KEY))


def at(h: int, m: int, s: int = 0) -> datetime:
    return datetime(2026, 11, 3, h, m, s, tzinfo=IST)


async def no_sleep(_: float) -> None:
    await asyncio.sleep(0)


class Harness:
    def __init__(self, now: datetime | None = None, cfg: BotConfig | None = None, db: Database | None = None,
                 **paper: Any) -> None:
        self.cfg = cfg or load_config(use_default_env_file=False)
        self.wall = FakeWallClock(now or at(10, 0))
        self.db = db or Database(":memory:")
        self.db.initialize()
        self.repos = Repositories(self.db, TradingMode.PAPER)
        sync_strategies(self.repos.strategies, self.cfg.strategies.strategies)
        self.md = MarketDataService(now_fn=self.wall)
        settings = self.cfg.settings.paper_broker.model_copy(update=paper)
        self.broker = PaperBroker(quotes=self.md.get, settings=settings, costs=self.cfg.settings.costs,
                                  instruments=[CE, PE], now_fn=self.wall)
        self.s: ExecutionStack = build_stack(self.cfg, repos=self.repos, broker=self.broker, market_data=self.md,
                                             now_fn=self.wall, sleep=no_sleep, reconcile_confirm_delay=0)

    def tick(self, key: str, ltp: float, spread: float = 0.1, qty: int = 1500) -> Tick:
        t = Tick(instrument_key=key, ltp=ltp, bid=round(ltp - spread / 2, 2), ask=round(ltp + spread / 2, 2),
                 bid_qty=qty, ask_qty=qty, exchange_time=self.wall.now, received_at=self.wall.now)
        self.md.on_tick(t)
        return t

    def proposal(self, sid: str = "orb_options", inst: Instrument = CE, **kw: Any) -> EntryProposal:
        opt = OptionType.CE if inst is CE else OptionType.PE
        return EntryProposal(strategy_id=sid, instrument=inst, option_type=opt, reason="test", **kw)

    async def decide(self, sid: str = "orb_options", inst: Instrument = CE, **kw: Any) -> RiskDecision:
        funds = await self.broker.get_funds()
        return self.s.risk.evaluate_entry(self.proposal(sid, inst, **kw), available_margin=funds.available_margin)

    async def enter(self, sid: str = "orb_options", inst: Instrument = CE, ltp: float = 100.0,
                    spread: float = 0.1) -> ManagedTrade:
        self.tick(inst.instrument_key, ltp, spread)
        d = await self.decide(sid, inst)
        assert d.approved, d.reasons
        trade = await self.s.positions.open_trade(d, strategy_version="0.1.0")
        await self.s.settle()
        return trade

    async def settle(self) -> None:
        await self.s.settle()
