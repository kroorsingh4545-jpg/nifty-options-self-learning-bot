"""Control plane between the bot and the dashboard (over SQLite).

* ``publish_state`` writes a JSON snapshot of the running bot to
  ``runtime_state`` (heartbeat). The dashboard only reads it.
* ``process_pending`` runs the commands the dashboard queued in
  ``control_commands``, through the risk / execution services, and records
  each result. The dashboard never calls the broker or the services itself.

Both run every second from the bot scheduler (``tick``). The control plane
uses the wall clock (``wall_fn``) for heartbeats and command expiry, so it
works the same while the trading clock is simulated.

Command rules:
* KILL_SWITCH commands run before every other queued command, and a
  KILL_SWITCH never expires (acting late on an emergency stop is still safe).
* EXIT_POSITION runs as a background task, so a slow or failing exit (up to
  ~30 s of retries) never delays a later kill or the heartbeat. While it runs
  the command stays PENDING in the database but is tracked in memory, so it
  is neither started twice nor expired.
* Other commands expire (the dashboard sets ``expires_at``).
* ENGINE_STOP stops NEW entries only. Square-off, stops and reconciliation
  keep running (the scheduler is never stopped from the dashboard).
* REQUEST_LIVE is evaluated against every precondition and refused while
  LIVE order routing does not exist; the reasons are recorded.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any

from core.clock import now_ist
from core.enums import AutonomyLevel, ControlCommandStatus, ControlCommandType
from core.event_bus import Topic
from core.masking import DEFAULT_MASKER
from core.mode_controller import LivePreconditions
from core.models import Candle
from data.storage.records import ControlCommandRecord
from execution.operator_controls import ControlResult
from monitoring.json_logger import get_logger

if TYPE_CHECKING:
    from runtime.bot import TradingBot

log = get_logger(__name__)

StatusProvider = Callable[[], dict[str, Any]]

CANDLE_HISTORY = 400


def _candle_row(c: Candle) -> list[Any]:
    return [c.start.isoformat(), c.open, c.high, c.low, c.close, c.volume]


class ControlService:
    def __init__(
        self,
        bot: TradingBot,
        *,
        wall_fn: Callable[[], datetime] = now_ist,
        connection_status: StatusProvider | None = None,
        rate_limit_usage: StatusProvider | None = None,
        alert_channels: StatusProvider | None = None,
    ) -> None:
        self.bot = bot
        self._wall = wall_fn
        self._connection = connection_status
        self._rate_limits = rate_limit_usage
        self._channels = alert_channels
        self.candles: dict[str, deque[list[Any]]] = {}
        self.last_live_request: dict[str, Any] | None = None
        self.in_flight: set[int] = set()
        self._tasks: set[asyncio.Task[None]] = set()
        bot.candles.add_listener(self._on_candle)

    # ------------------------------------------------------------------ #
    def _on_candle(self, c: Candle) -> None:
        keys = {self.bot.spot_key}
        if self.bot.futures is not None:
            keys.add(self.bot.futures.instrument_key)
        if c.complete and c.instrument_key in keys:
            buf = self.candles.setdefault(f"{c.instrument_key}|{c.timeframe_minutes}", deque(maxlen=CANDLE_HISTORY))
            buf.append(_candle_row(c))

    async def tick(self) -> None:
        await self.process_pending()
        await self.publish_state()

    # ------------------------------------------------------------------ #
    # Commands
    # ------------------------------------------------------------------ #
    async def process_pending(self) -> int:
        repo = self.bot.repos.commands
        queue = [c for c in repo.pending() if c.id not in self.in_flight]
        # Emergency stops first, whatever their position in the queue.
        queue.sort(key=lambda c: (c.command is not ControlCommandType.KILL_SWITCH, c.id or 0))
        started = 0
        for cmd in queue:
            assert cmd.id is not None
            now = self._wall()
            expired = cmd.expires_at is not None and cmd.expires_at < now
            if expired and cmd.command is not ControlCommandType.KILL_SWITCH:
                repo.complete(cmd.id, ControlCommandStatus.EXPIRED, "expired before the bot processed it", at=now)
                continue
            if cmd.command is ControlCommandType.EXIT_POSITION:
                self._run_in_background(cmd)
            else:
                await self._execute(cmd)
            started += 1
        return started

    def _run_in_background(self, cmd: ControlCommandRecord) -> None:
        assert cmd.id is not None
        cid = cmd.id
        self.in_flight.add(cid)

        async def runner() -> None:
            try:
                await self._execute(cmd)
            finally:
                self.in_flight.discard(cid)

        task = asyncio.get_running_loop().create_task(runner())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def wait_idle(self) -> None:
        """Await background commands (tests / orderly shutdown)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def _execute(self, cmd: ControlCommandRecord) -> None:
        assert cmd.id is not None
        try:
            result = await self._run(cmd)
            status = ControlCommandStatus.DONE if result.ok else ControlCommandStatus.REJECTED
        except Exception as exc:
            log.exception("control command failed", extra={"command": cmd.command.value, "id": cmd.id})
            result = ControlResult(False, DEFAULT_MASKER.mask_text(f"{type(exc).__name__}: {exc}"))
            status = ControlCommandStatus.FAILED
        self.bot.repos.commands.complete(cmd.id, status, result.message, at=self._wall(), result=result.details)
        log.warning("control command processed",
                    extra={"command": cmd.command.value, "id": cmd.id, "operator": cmd.operator,
                           "strategy_id": cmd.strategy_id, "status": status.value, "result": result.message})

    async def _run(self, cmd: ControlCommandRecord) -> ControlResult:
        bot = self.bot
        controls = bot.stack.controls
        assert controls is not None
        c = ControlCommandType(cmd.command)
        if c is ControlCommandType.KILL_SWITCH:
            return controls.trigger_kill(cmd.operator, cmd.note, cmd.strategy_id)
        if c is ControlCommandType.RESET_KILL_SWITCH:
            return await controls.reset_kill_switch(cmd.operator, cmd.note, cmd.strategy_id)
        if c is ControlCommandType.EXIT_POSITION:
            if not cmd.target:
                return ControlResult(False, "EXIT_POSITION needs a trade id")
            return await controls.exit_position(cmd.operator, cmd.target, cmd.note)
        if c in (ControlCommandType.ENGINE_START, ControlCommandType.ENGINE_PAUSE, ControlCommandType.ENGINE_STOP):
            if bot.engine is None:
                return ControlResult(False, "the bot is not prepared yet")
            if c is ControlCommandType.ENGINE_START:
                bot.engine.start()
                return ControlResult(True, "engine RUNNING: new entries allowed (risk checks apply)")
            if c is ControlCommandType.ENGINE_PAUSE:
                bot.engine.pause()
                return ControlResult(True, "engine PAUSED: no new entries; open positions stay protected")
            bot.engine.stop()
            return ControlResult(True, "engine STOPPED: no new entries; square-off, stops and reconciliation "
                                       "keep running")
        if c is ControlCommandType.SET_AUTONOMY:
            level = AutonomyLevel(str(cmd.payload_json.get("level", "")))
            bot.mode.set_autonomy(level)
            return ControlResult(True, f"autonomy set to {level.value}")
        if c is ControlCommandType.REQUEST_LIVE:
            pre = await self.live_preconditions(approved_by=cmd.payload_json.get("approved_by") or None)
            res = bot.mode.request_live(str(cmd.payload_json.get("confirmation", "")), pre)
            self.last_live_request = {"approved": res.approved, "reasons": list(res.reasons),
                                      "at": res.at.isoformat(), "operator": cmd.operator}
            if res.approved:  # pragma: no cover - unreachable while LIVE routing does not exist
                return ControlResult(True, "LIVE enabled")
            return ControlResult(False, "LIVE refused: " + "; ".join(res.reasons), {"reasons": list(res.reasons)})
        if c is ControlCommandType.REQUEST_PAPER:
            res = bot.mode.request_paper(pending_orders=len(bot.repos.orders.list_open()))
            return ControlResult(res.approved, "; ".join(res.reasons) or f"mode {res.mode.value}")
        return ControlResult(False, f"unsupported command {c.value}")  # pragma: no cover - enum is exhaustive

    async def live_preconditions(self, approved_by: str | None = None) -> LivePreconditions:
        s = self.bot.stack
        last = s.reconciler.last
        conn = self._connection() if self._connection else {}
        return LivePreconditions(
            reconciliation_ok=bool(last and last.ok),
            kill_switch_armed=s.kill_switch.armed,
            kill_switch_active=s.kill_switch.active,
            loss_limits_configured=True,  # validated at config load; the bot refuses to start otherwise
            paper_validation_passed=False,  # promotion gate arrives in Phase 9
            approved_by=approved_by,
            pending_orders=len(self.bot.repos.orders.list_open()),
            broker_connected=bool(conn.get("upstox_connected", False)),
        )

    # ------------------------------------------------------------------ #
    # State
    # ------------------------------------------------------------------ #
    async def publish_state(self) -> dict[str, Any]:
        state = await self.build_state()
        self.bot.repos.runtime_state.publish(state, at=self._wall())
        return state

    async def build_state(self) -> dict[str, Any]:
        bot = self.bot
        s = bot.stack
        cfg = bot.cfg
        now = bot._now()
        try:
            funds = await bot.broker.get_funds()
            funds_d: dict[str, Any] | None = {"available_margin": funds.available_margin,
                                              "used_margin": funds.used_margin,
                                              "notional_cash": funds.notional_cash}
        except Exception as exc:  # broker unreachable must not stop the heartbeat
            funds_d = None
            log.warning("funds unavailable for state snapshot", extra={"error": type(exc).__name__})
        guard = s.guard.snapshot()
        engine = bot.engine.state.snapshot() if bot.engine else None
        strategies_cfg = cfg.strategies.strategies
        repo_status = {r.strategy_id: r.status.value for r in bot.repos.strategies.list_all()}
        live_strats = {x.strategy_id: x for x in bot.strategies}

        strategies = []
        for sid, scfg in strategies_cfg.items():
            g = guard["strategies"].get(sid, {})  # type: ignore[attr-defined]
            eng = (engine or {}).get("strategies", {}).get(sid, {})
            strategies.append({
                "strategy_id": sid, "name": scfg.name,
                "version": scfg.version, "enabled": scfg.enabled, "status": repo_status.get(sid),
                "allocated_capital": scfg.allocated_capital, "mode": cfg.mode.value,
                "realized": g.get("realized", 0.0), "unrealized": g.get("unrealized", 0.0),
                "daily_loss": g.get("loss", 0.0), "daily_loss_limit": g.get("limit"),
                "trades_today": g.get("trades", 0), "consecutive_losses": g.get("consecutive_losses", 0),
                "breached": g.get("breached", False), "paused": g.get("paused"),
                "open_trades": s.positions.open_trade_count(sid),
                "max_risk_per_trade": min(scfg.sizing.risk_per_trade, cfg.risk.per_trade.max_risk_per_trade),
                "kill": s.kill_switch.block_reason(sid),
                "last_signal": eng.get("last_signal"), "last_decision": eng.get("last_decision"),
                "signals": eng.get("signals", 0), "ml_score": eng.get("ml_score"), "regime": eng.get("regime"),
                "model_version": None,
                "running": sid in live_strats,
                "detail": live_strats[sid].snapshot() if sid in live_strats else None,
            })

        positions = []
        for t in s.positions.open_trades():
            ltp = t.last_price if t.last_price is not None else bot.md.ltp(t.instrument.instrument_key)
            rec = bot.repos.trades.get(t.trade_uid)
            risk_per_unit = t.levels.risk_per_unit
            r_mult = (ltp - t.avg_entry) / risk_per_unit if ltp is not None and risk_per_unit > 0 else None
            entry_time = rec.entry_time if rec else None
            positions.append({
                "trade_uid": t.trade_uid, "strategy_id": t.strategy_id, "symbol": t.instrument.trading_symbol,
                "instrument_key": t.instrument.instrument_key, "side": "BUY", "option_type": t.option_type.value,
                "quantity": t.open_qty, "entry_price": round(t.avg_entry, 2),
                "ltp": round(ltp, 2) if ltp is not None else None,
                "unrealized": round(t.unrealized, 2), "stop_loss": t.stop, "initial_stop": t.levels.stop,
                "target": t.levels.target, "trailing_active": t.trail.active, "trailing_stop": t.trail.stop,
                "r_multiple": round(r_mult, 2) if r_mult is not None else None,
                "entry_time": entry_time.isoformat() if entry_time else None,
                "holding_seconds": (now - entry_time).total_seconds() if entry_time else None,
                "exit_in_progress": t.exit_in_progress,
            })

        ks = s.kill_switch
        kill_events = ([ks.global_event] if ks.global_event else []) + list(ks.strategy_events.values())
        last = s.reconciler.last
        pre = await self.live_preconditions()
        live_reasons = bot.mode.evaluate_live("", pre)
        events: list[dict[str, Any]] = []
        for topic in (Topic.ENTRY_DECISION, Topic.SIGNAL_FILTERED, Topic.ENGINE_STATE, Topic.STRATEGY_ERROR,
                      Topic.DAY_RESET):
            for e in bot.bus.recent(topic, 30):
                payload = e.payload.model_dump(mode="json") if hasattr(e.payload, "model_dump") else e.payload
                events.append({"at": e.at.isoformat(), "topic": topic.value, "payload": payload})
        events.sort(key=lambda x: x["at"], reverse=True)
        alerts = [{"at": a.at.isoformat(), "severity": a.severity.value, "title": a.title, "message": a.message,
                   "strategy_id": a.strategy_id} for a in list(s.alerts.alerts)[-50:]][::-1]
        exposure = sum(t.open_qty * t.avg_entry for t in s.positions.open_trades())
        conn = self._connection() if self._connection else {}

        return {
            "schema": 1,
            "published_at": self._wall().isoformat(),
            "bot_time": now.isoformat(),
            "mode": bot.mode.mode.value,
            "autonomy": bot.mode.autonomy.value,
            "live_available": False,
            "engine_state": (engine or {}).get("engine_state", "NOT_PREPARED"),
            "accepting_signals": bool(bot.engine and bot.engine.accepting_signals),
            "session": {
                "phase": s.square_off.phase().value,
                "trading_day": cfg.settings.market_session.is_trading_day(now.date()),
                "holiday_calendar_loaded": cfg.settings.market_session.holidays_known_for(now.year),
                "seconds_to_square_off": s.square_off.seconds_to_square_off(),
                "square_off_start": cfg.risk.time_rules.square_off_start.strftime("%H:%M"),
                "no_new_entries_after": cfg.risk.time_rules.no_new_entries_after.strftime("%H:%M"),
                "instrument_block": bot.instrument_block_reason(),
            },
            "connections": {
                "upstox": conn.get("upstox", "not connected (no Upstox session in this process)"),
                "upstox_connected": bool(conn.get("upstox_connected", False)),
                "websocket": conn.get("websocket", "not connected"),
                "subscriptions": len(bot.universe.subscribed | bot.base_keys()) if bot.universe else 0,
            },
            "rate_limits": self._rate_limits() if self._rate_limits else None,
            "alert_channels": self._channels() if self._channels else {"dashboard": "on",
                                                                      "telegram": "not configured (Phase 10)"},
            "capital": {"total": cfg.risk.capital.total_capital,
                        "per_strategy": {sid: sc.allocated_capital for sid, sc in strategies_cfg.items()},
                        "deployed": round(exposure, 2)},
            "funds": funds_d,
            "daily": guard,
            "limits": {"global_daily_loss": cfg.risk.global_.max_daily_loss,
                       "per_strategy_daily_loss": cfg.risk.per_strategy.max_daily_loss,
                       "per_trade_risk": cfg.risk.per_trade.max_risk_per_trade,
                       "max_open_trades": cfg.risk.global_.max_open_trades,
                       "max_open_trades_per_strategy": cfg.risk.per_strategy.max_open_trades},
            "kill_switch": {
                **ks.status(),
                "events": [{"scope": ev.strategy_id or "GLOBAL", "reason": ev.reason.value, "message": ev.message,
                            "since": ev.at.isoformat(), "persistent": not ev.day_scoped} for ev in kill_events],
            },
            "strategies": strategies,
            "positions": positions,
            "open_orders": len(bot.repos.orders.list_open()),
            "reconciliation": None if last is None else {
                "trigger": last.trigger, "at": last.at.isoformat(), "ok": last.ok, "reachable": last.reachable,
                "mismatches": [f"{m.kind} {m.key} local={m.local} broker={m.broker}" for m in last.mismatches]},
            "live_preconditions": {"unmet": [r for r in live_reasons if "type exactly" not in r],
                                   "pending_orders": pre.pending_orders,
                                   "reconciliation_ok": pre.reconciliation_ok},
            "last_live_request": self.last_live_request,
            "instruments": bot.instrument_summary,
            "candles": {k: list(v) for k, v in self.candles.items()},
            "chart_keys": {"index": bot.spot_key,
                           "future": bot.futures.instrument_key if bot.futures else None},
            "events": events[:60],
            "alerts": alerts,
        }
