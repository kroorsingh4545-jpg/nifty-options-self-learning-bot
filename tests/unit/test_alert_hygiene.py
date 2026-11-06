"""Alert hygiene (Phase 10 audit fixes).

1. Alert text shown on the dashboard is masked for secrets, whatever produced
   the alert (error-log alerts are also masked at the source).
2. The in-session learning loop's backtests never log at ERROR / CRITICAL and
   never raise Telegram or dashboard alerts (``research_logging``).

Market data is SYNTHETIC (tests only). Verification runs are fakes plus one
real one-day SYNTHETIC backtest; the HISTORICAL label on the validation
engine is FABRICATED to exercise the loop. No test talks to Telegram.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest

from backtest.backtester import Backtester, BacktestOptions
from backtest.synthetic_data import SyntheticSpec, generate_synthetic_dataset
from config.settings import BotConfig, TelegramSection
from core.enums import AutonomyLevel, EvidenceSource, RiskSeverity
from core.masking import DEFAULT_MASKER
from learning.loop import ReflectiveLoop
from learning.validation_engine import RunSummary, ValidationEngine, ValidationPlan
from monitoring.alerts import Alert, AlertKind, AlertLogHandler, FanoutAlertSink, LogAlertSink, MemoryAlertSink
from monitoring.json_logger import is_research_record, research_logging
from monitoring.telegram import TelegramAlertSink
from runtime import learning_loop as rl
from tests.fakes import FakeWallClock
from tests.paper_support import SID, FakeConnections, paper_cfg, session
from tests.synthetic import at
from tests.unit.test_learning_verify_promote import _journal as journal_rows
from tests.unit.test_learning_verify_promote import fake_runs

SECRET = "upstox-live-token-value-0123456789"


@pytest.fixture(scope="module")
def dataset():  # type: ignore[no-untyped-def]
    # SYNTHETIC: its calendar for the fake runs, and one day for a real simulated bot
    return generate_synthetic_dataset(SyntheticSpec(lot_units=65, start=date(2026, 9, 7), days=8, seed=3))


# --------------------------------------------------------------------------- #
# 1. Masking
# --------------------------------------------------------------------------- #


def test_error_log_alerts_are_masked_at_the_source() -> None:
    DEFAULT_MASKER.register_many([SECRET])
    mem = MemoryAlertSink()
    handler = AlertLogHandler(mem)
    lg = logging.getLogger("broker.test_component")
    lg.addHandler(handler)
    try:
        lg.error("feed authorize failed for %s", SECRET)
        try:
            raise RuntimeError(f"Authorization: Bearer {SECRET} rejected")
        except RuntimeError:
            lg.exception("request failed access_token=abcdef123456")
    finally:
        lg.removeHandler(handler)
    text = json.dumps([[a.title, a.message] for a in mem.alerts])
    assert len(mem.alerts) == 2
    assert SECRET not in text and "abcdef123456" not in text and "****" in text


async def test_dashboard_state_masks_every_alert(tmp_path: Path) -> None:
    cfg = paper_cfg(tmp_path)
    DEFAULT_MASKER.register_many([SECRET])
    wall = FakeWallClock(at(9, 14))
    s = session(cfg, wall, FakeConnections(wall))
    bot = await s.start()
    try:
        assert bot.stack.notify is not None
        bot.stack.notify.send(Alert(RiskSeverity.CRITICAL, f"token {SECRET} expired",
                                    f"Bearer {SECRET} / password=hunter2hunter2", kind=AlertKind.ERROR))
        logging.getLogger("execution.order_manager").error("order failed with token %s", SECRET)  # via the handler
        state = await bot.control.build_state()
        alerts = json.dumps(state["alerts"])
        assert SECRET not in alerts and "hunter2hunter2" not in alerts
        assert "token **** expired" in alerts.replace(SECRET[:4] + "****" + SECRET[-4:], "****")
        assert sum(1 for a in state["alerts"] if a["kind"] == "ERROR") == 2
        bot.repos.runtime_state.publish(state, at=wall())
        latest = bot.repos.runtime_state.latest()
        assert latest is not None and SECRET not in json.dumps(latest[0])  # what the dashboard reads
    finally:
        await s.shutdown()


# --------------------------------------------------------------------------- #
# 2. research_logging
# --------------------------------------------------------------------------- #


def test_research_logging_downgrades_only_inside_the_context(caplog: pytest.LogCaptureFixture) -> None:
    mem = MemoryAlertSink()
    handler = AlertLogHandler(mem, dedupe_seconds=0)
    root = logging.getLogger()
    root.addHandler(handler)
    seen_in_task: list[logging.LogRecord] = []
    try:
        with caplog.at_level(logging.DEBUG):
            with research_logging("verify"):
                logging.getLogger("execution.square_off").critical("kill switch flatten finished")
                logging.getLogger("runtime.bot").error("ML is enabled but no deployable model")

                async def task_logs() -> None:  # tasks started inside the context inherit it
                    logging.getLogger("execution.order_manager").error("EXIT FAILED in simulation")

                asyncio.run(task_logs())

                outside: list[int] = []
                t = threading.Thread(target=lambda: (logging.getLogger("x.other").error("other thread"),
                                                     outside.append(1)))
                t.start()
                t.join()
            logging.getLogger("execution.order_manager").error("real failure after the run")
        wanted = {"kill switch flatten finished", "ML is enabled but no deployable model", "EXIT FAILED in simulation"}
        tagged = [r for r in caplog.records if is_research_record(r)]  # also asyncio's own DEBUG lines
        research = [r for r in tagged if r.getMessage() in wanted]
        seen_in_task = [r for r in research if r.getMessage() == "EXIT FAILED in simulation"]
        assert {r.getMessage() for r in research} == wanted
        assert all(r.levelno <= logging.WARNING for r in tagged)
        assert all(r.levelno == logging.WARNING for r in research)
        assert "other thread" not in {r.getMessage() for r in tagged}
        assert {r.original_level for r in research} == {"CRITICAL", "ERROR"}  # type: ignore[attr-defined]
        assert seen_in_task and seen_in_task[0].research_run == "verify"  # type: ignore[attr-defined]
    finally:
        root.removeHandler(handler)
    # only the records outside the context became alerts: another thread, and after the run
    assert [a.title for a in mem.alerts] == ["other thread", "real failure after the run"]


def test_a_real_simulated_bot_logs_no_errors_inside_the_context(caplog: pytest.LogCaptureFixture,
                                                                dataset: Any) -> None:
    from tests.ml_support import config

    one_day = dataset.slice(dataset.days()[:1])
    opts = BacktestOptions(ml_strategies=[SID])  # ML on, no model: the simulated bot logs CRITICAL
    with caplog.at_level(logging.DEBUG):
        asyncio.run(Backtester(config(), one_day, opts).run())
    assert any(r.levelno >= logging.ERROR for r in caplog.records)  # the control: it does log CRITICAL
    caplog.clear()
    with caplog.at_level(logging.DEBUG), research_logging("verify"):
        asyncio.run(Backtester(config(), one_day, BacktestOptions(ml_strategies=[SID])).run())
    assert caplog.records and not any(r.levelno >= logging.ERROR for r in caplog.records)
    assert any(getattr(r, "original_level", None) == "CRITICAL" for r in caplog.records)


class Telegram:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.messages.append(json.loads(request.content)["text"])
        return httpx.Response(200, json={"ok": True})


async def test_the_in_session_loop_raises_no_alerts(tmp_path: Path, dataset: Any, monkeypatch: pytest.MonkeyPatch,
                                                    caplog: pytest.LogCaptureFixture) -> None:
    from tests.ml_support import config

    base = paper_cfg(tmp_path)
    learning = base.settings.learning.model_copy(update={"loop": base.settings.learning.loop.model_copy(
        update={"verify_data": tmp_path / "verify-ds", "reports_dir": tmp_path / "reports"})})
    cfg: BotConfig = base.model_copy(update={"settings": base.settings.model_copy(update={"learning": learning})})
    wall = FakeWallClock(at(16, 0))  # after the close
    captured = MemoryAlertSink()
    api = Telegram()
    tg = TelegramAlertSink(token="123456789:AAH-fake-telegram-token-for-tests-only", chat_id="42",
                           settings=TelegramSection(min_severity="INFO"),
                           http=httpx.AsyncClient(transport=httpx.MockTransport(api)),
                           sleep=lambda _s: asyncio.sleep(0))
    s = session(cfg, wall, FakeConnections(wall))
    s._alert_sinks = (captured, tg)
    bot = await s.start()
    inner = fake_runs({"production": 100.0, "candidate": 300.0})
    ran_real = False

    async def run_fn(c: BotConfig, days: Any, label: str) -> RunSummary:
        nonlocal ran_real
        logging.getLogger("execution.square_off").critical("kill switch flatten finished")
        FanoutAlertSink(MemoryAlertSink(), LogAlertSink()).send(  # a simulated stack's own alert
            Alert(RiskSeverity.CRITICAL, "Kill switch triggered (GLOBAL)", "simulated", kind=AlertKind.KILL_SWITCH))
        if not ran_real:  # one real simulated bot (ML on, no model -> CRITICAL)
            ran_real = True
            await Backtester(config(), dataset.slice(dataset.days()[:1]), BacktestOptions(ml_strategies=[SID])).run()
        return await inner(c, days, label)

    def engine(prod: BotConfig, ds: Any, *, plan: ValidationPlan | None = None) -> ValidationEngine:
        eng = ValidationEngine(prod, ds, plan=ValidationPlan(min_trades=5), run_fn=run_fn)
        eng.evidence = EvidenceSource.HISTORICAL  # FABRICATED label (fake runs): exercises the loop
        return eng

    class Runner(rl.LearningLoopRunner):
        def build_loop(self, repos: Any, validation: Any) -> ReflectiveLoop:  # feed the SYNTHETIC journal
            loop = super().build_loop(repos, validation)
            real_run = loop.run

            async def run(*_a: Any, **_k: Any) -> Any:
                return await real_run(journal_rows(), date(2026, 11, 2), date(2026, 11, 2), strategy_ids=[SID])

            loop.run = run  # type: ignore[method-assign]
            return loop

    monkeypatch.setattr(rl, "load_verification_data", lambda _p: dataset)
    monkeypatch.setattr(rl, "ValidationEngine", engine)
    try:
        with caplog.at_level(logging.DEBUG):
            res = await Runner(bot, cfg).run_once()
            assert res is not None and ran_real and bot.mode.autonomy is AutonomyLevel.PAPER_AUTO_IMPROVE
            await asyncio.sleep(0.05)
        worker = [r for r in caplog.records if is_research_record(r)]
        assert worker and not any(r.levelno >= logging.ERROR for r in worker)
        assert {getattr(r, "original_level", None) for r in worker} >= {"CRITICAL"}
        assert not any(a.kind is AlertKind.ERROR for a in captured.alerts)
        assert not any("simulated" in a.message or "flatten" in a.title for a in captured.alerts)
        logging.getLogger("execution.order_manager").error("a real failure after the loop")  # still alerts
        await asyncio.sleep(0.05)
        assert any(a.title == "a real failure after the loop" for a in captured.alerts)
    finally:
        await s.shutdown()
    assert len(api.messages) == 1 and "a real failure after the loop" in api.messages[0]
