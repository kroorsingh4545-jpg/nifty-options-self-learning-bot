"""The Reflective Loop next to a running PAPER bot (Phase 10).

``LearningLoopRunner.run_once`` is scheduled daily at ``learning.loop.run_at``
(after the close) by the PAPER launcher when ``learning.loop.enabled`` is on:

* it refuses to run while the market is open or any PAPER trade is open;
* it VERIFIES candidates only on the HISTORICAL dataset in
  ``learning.loop.verify_data`` (a SYNTHETIC or unlabelled dataset is
  refused: it could never promote anything and would only fill the trading
  database with research rows);
* its ``autonomy_fn`` is the running bot's live level (``bot.mode.autonomy``),
  read again at every promotion step, so a dashboard switch to OBSERVE_ONLY
  stops the next PAPER_READY decision or deployment immediately;
* it runs in a worker thread with its own database connection and event
  loop, so the bot's heartbeat and control plane keep running;
* everything it logs is at most WARNING and tagged ``research_run``
  (``monitoring.json_logger.research_logging``): the simulated bots in its
  backtests never raise ERROR / CRITICAL lines, Telegram or dashboard alerts;
* a deployment it makes takes effect at the NEXT bot start (the launcher
  applies deployments at start-up only), never inside the running session.

It never touches orders, positions, risk state or the kill switch: it writes
only learning tables (strategy versions, deployments, learning events) and a
report file.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from config.settings import BotConfig
from core.clock import IST
from core.enums import AutonomyLevel, TradingMode
from data.storage.database import Database
from data.storage.repositories import Repositories
from learning.loop import LoopRefused, LoopResult, ReflectiveLoop, load_verification_data, render_loop_markdown
from learning.validation_engine import ValidationEngine, ValidationPlan
from monitoring.json_logger import get_logger, research_logging

if TYPE_CHECKING:
    from runtime.bot import TradingBot

log = get_logger(__name__)

ValidationFactory = Callable[[BotConfig], ValidationEngine]


class LearningLoopRunner:
    def __init__(self, bot: TradingBot, yaml_cfg: BotConfig, *, plan: ValidationPlan | None = None) -> None:
        if yaml_cfg.mode is not TradingMode.PAPER:
            raise LoopRefused("the learning loop runs in PAPER mode only")
        self.bot = bot
        self.cfg = yaml_cfg  # the YAML config (production = YAML + PAPER deployments, resolved by the loop)
        self.plan = plan or ValidationPlan()
        self.last_result: LoopResult | None = None
        self.last_report: Path | None = None
        self._running = False

    def autonomy(self) -> AutonomyLevel:
        """The running bot's level, read at the moment of each decision."""
        return AutonomyLevel(self.bot.mode.autonomy)

    def build_loop(self, repos: Repositories, validation: ValidationFactory) -> ReflectiveLoop:
        return ReflectiveLoop(self.cfg, repos, validation=validation, autonomy_fn=self.autonomy)

    def window(self, today: date) -> tuple[date, date]:
        days = self.cfg.settings.learning.reflection_window_days
        return today - timedelta(days=days - 1), today

    def refusal(self) -> str | None:
        now = self.bot._now()
        if self.bot.market_hours(now):
            return "the market is open"
        n = self.bot.stack.positions.open_trade_count()
        if n:
            return f"{n} PAPER trade(s) still open"
        if self._running:
            return "a loop run is already in progress"
        return None

    async def run_once(self) -> LoopResult | None:
        why = self.refusal()
        if why is not None:
            log.warning("learning loop skipped", extra={"reason": why})
            return None
        loop_cfg = self.cfg.settings.learning.loop
        if loop_cfg.verify_data is None:
            log.error("learning loop skipped: learning.loop.verify_data is not set")
            return None
        self._running = True
        try:
            start, end = self.window(self.bot._now().astimezone(IST).date())
            res = await asyncio.to_thread(self._run_in_worker, self.cfg.resolve_path(loop_cfg.verify_data),
                                          start, end)
        except LoopRefused as exc:
            log.error("learning loop refused", extra={"reason": str(exc)})
            return None
        except Exception:
            log.exception("learning loop run failed (trading is unaffected)")
            return None
        finally:
            self._running = False
        self.last_result = res
        self.last_report = self.write_report(res, end)
        log.warning("learning loop run finished", extra={
            "run": res.run_uid, "autonomy": res.autonomy.value, "report": str(self.last_report),
            "deployed": {sid: r.deployed for sid, r in res.strategies.items() if r.deployed},
            "note": "deployments take effect at the next PAPER session start"})
        return res

    def _run_in_worker(self, data_dir: Path, start: date, end: date) -> LoopResult:
        # The VERIFY backtests build simulated bots whose kills, exit failures and CRITICALs are not the
        # running bot's: nothing logged in here is ERROR / CRITICAL or becomes an alert (Telegram, dashboard).
        # A failure of the loop itself still propagates and is logged at ERROR by run_once (main thread).
        with research_logging(f"learning-loop {start}..{end}"):
            ds = load_verification_data(data_dir)
            db = Database.open_existing(self.cfg.database_path)  # this thread's own connection
            try:
                repos = Repositories(db, TradingMode.PAPER)
                loop = self.build_loop(repos, lambda prod: ValidationEngine(prod, ds, plan=self.plan))
                since = datetime.combine(start, time(0, 0), tzinfo=IST)
                until = datetime.combine(end + timedelta(days=1), time(0, 0), tzinfo=IST)
                return asyncio.run(loop.run(repos.journal.list(since=since, until=until), start, end))
            finally:
                db.close()

    def write_report(self, res: LoopResult, day: date) -> Path:
        out = self.cfg.resolve_path(self.cfg.settings.learning.loop.reports_dir)
        out.mkdir(parents=True, exist_ok=True)
        stem = f"loop-{day.isoformat()}-{res.run_uid}"
        (out / f"{stem}.md").write_text(render_loop_markdown(res), encoding="utf-8")
        (out / f"{stem}.json").write_text(json.dumps(res.to_dict(), indent=2, default=str), encoding="utf-8")
        return out / f"{stem}.md"


__all__ = ["LearningLoopRunner", "LoopRefused", "load_verification_data"]
