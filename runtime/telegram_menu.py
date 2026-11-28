"""Telegram control menu (owner choice "A": the bot never resets itself).

``Menu`` in the configured chat shows inline buttons:

* read-only: Status, Positions, Today's trades, Health, Risk, Recent log,
  Last signals, Shadow (latest research report, if any);
* controls, each behind "Are you sure? Yes / No": Pause entries, Resume
  entries, Reset kill switch, Emergency kill, Exit one position.

Safety rules:

* Only TELEGRAM_CHAT_ID is answered; every other chat or user is ignored
  silently (and logged). Outbound long-polling only: no webhook, no port.
* Button data is never trusted beyond its id: it must match ``BUTTON_ID``
  exactly; a confirmation or a position choice is a random reference to a
  request kept here (single use, expires after ``confirm_ttl_seconds``), and
  the state is re-checked when Yes is pressed.
* Controls are queued as ordinary control commands (``control_commands``,
  source ``telegram``, the operator's name) and run by the bot's control
  plane exactly like the dashboard's, so the existing queue decides when both
  act at once. Only the five commands in ``ALLOWED_COMMANDS`` can be queued:
  LIVE, autonomy, risk limits, config, capital, credentials and restarts are
  not reachable from here (architecture test).
* A kill-switch reset is refused (with the reason) unless the global kill is
  active and the feed has been connected and stable for
  ``stable_feed_seconds``; the control plane re-checks the feed when it runs
  the reset, which then goes through the dashboard's path (named human,
  reconciliation first, strategies re-activated the same way).
* After a WEBSOCKET_DISCONNECT kill, once the feed has been stable for
  ``stable_feed_seconds``, one message says so with a Reset button. Nothing
  is reset automatically.
* Everything runs in its own tasks, reads only published state, and catches
  every error: a slow, failing or unreachable Telegram never blocks, slows or
  crashes trading. Button presses are rate limited.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import secrets
import time as _time
from collections import deque
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from config.settings import TelegramMenuSection
from core.clock import now_ist
from core.enums import ControlCommandStatus, ControlCommandType, TradeStatus
from core.masking import DEFAULT_MASKER
from data.storage.records import ControlCommandRecord
from monitoring.json_logger import get_logger
from monitoring.telegram import Buttons, TelegramBotApi, TelegramDisabled, TelegramUpdate, parse_update
from risk.kill_switch import KillReason

if TYPE_CHECKING:
    from runtime.bot import TradingBot

log = get_logger(__name__)

SOURCE = "telegram"
COMMAND_TTL = timedelta(seconds=60)  # same as the dashboard's
ALLOWED_COMMANDS = frozenset({ControlCommandType.ENGINE_PAUSE, ControlCommandType.ENGINE_START,
                              ControlCommandType.RESET_KILL_SWITCH, ControlCommandType.KILL_SWITCH,
                              ControlCommandType.EXIT_POSITION})
VIEWS = {"status": "Status", "positions": "Positions", "trades": "Today's trades", "health": "Health",
         "risk": "Risk", "log": "Recent log", "signals": "Last signals", "shadow": "Shadow"}
CONTROLS = {"pause": "Pause entries", "resume": "Resume entries", "reset": "Reset kill switch",
            "kill": "Emergency kill", "exit": "Exit one position"}
CONFIRMABLE = frozenset({"pause", "resume", "reset", "kill", "exit"})
BUTTON_ID = re.compile(r"v:(?:menu|" + "|".join(VIEWS) + r")|c:(?:" + "|".join(CONTROLS) + r")|[xyn]:[0-9a-f]{8}")
MENU_WORDS = frozenset({"menu", "start"})
MENU_BUTTON: Buttons = [[("Menu", "v:menu")]]
MIN_EMPTY_POLL_SECONDS = 1.0
LOG_LINES = 15
LOG_TAIL_BYTES = 256 * 1024
IMPORTANT_LEVELS = frozenset({"WARNING", "ERROR", "CRITICAL"})


def menu_buttons() -> Buttons:
    v = [(label, f"v:{k}") for k, label in VIEWS.items()]
    c = [(label, f"c:{k}") for k, label in CONTROLS.items()]
    return [v[0:2], v[2:4], v[4:6], v[6:8], c[0:2], c[2:4], c[4:5]]


@dataclass
class Pending:
    """A request waiting for Yes / No (or a position choice). Kept here only:
    the button carries a random reference to it, never its content."""

    kind: str  # pause | resume | reset | kill | exit | choose_exit
    created: float
    target: str | None = None
    label: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


def _rs(x: Any) -> str:
    try:
        return f"Rs {float(x):,.0f}"
    except (TypeError, ValueError):
        return "Rs -"


def _num(x: Any, fmt: str = ".2f") -> str:
    try:
        return format(float(x), fmt)
    except (TypeError, ValueError):
        return "-"


def _hms(iso: Any) -> str:
    try:
        return datetime.fromisoformat(str(iso)).strftime("%H:%M:%S")
    except ValueError:
        return "-"


def _duration(seconds: Any) -> str:
    try:
        s = int(float(seconds))
    except (TypeError, ValueError):
        return "-"
    return f"{s // 60}m {s % 60:02d}s" if s >= 60 else f"{s}s"


class TelegramMenu:
    def __init__(
        self,
        bot: TradingBot,
        api: TelegramBotApi,
        settings: TelegramMenuSection | None = None,
        *,
        log_path: Path | None = None,
        research_dir: Path | None = None,
        wall_fn: Callable[[], datetime] = now_ist,
        clock: Callable[[], float] = _time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        watch_interval_seconds: float = 5.0,
    ) -> None:
        self.bot = bot
        self.api = api
        self.settings = settings or bot.cfg.settings.alerts.telegram.menu
        logging_cfg = bot.cfg.settings.logging
        self.log_path = log_path or logging_cfg.directory / logging_cfg.file_name
        self.research_dir = research_dir or logging_cfg.directory / "research"
        self._wall = wall_fn
        self._clock = clock
        self._sleep = sleep
        self.watch_interval = watch_interval_seconds
        self._pending: dict[str, Pending] = {}
        self._presses: deque[float] = deque()
        self._recovered: set[str] = set()
        self._tasks: set[asyncio.Task[None]] = set()
        self._closing = False
        self.offset: int | None = None
        self.disabled: str | None = None
        self.ignored = 0
        self.limited = 0
        self.poll_errors = 0

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        self._spawn(self._poll_loop(), "telegram-menu-poll")
        self._spawn(self._watch_loop(), "telegram-menu-watch")
        log.warning("telegram menu started (outbound long-polling; only the configured chat is answered)")

    def _spawn(self, coro: Coroutine[Any, Any, None], name: str) -> None:
        task: asyncio.Task[None] = asyncio.get_running_loop().create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def aclose(self) -> None:
        self._closing = True
        for t in list(self._tasks):
            t.cancel()
        for t in list(self._tasks):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        await self.api.aclose()

    def describe(self) -> str:
        if self.disabled:
            return f"menu disabled ({self.disabled})"
        return f"menu on (ignored {self.ignored} other-chat updates, poll errors {self.poll_errors})"

    # ------------------------------------------------------------------ #
    # Polling (outbound only)
    # ------------------------------------------------------------------ #
    async def drain_backlog(self) -> None:
        """Skip updates sent while the bot was not running: an old press is
        never acted on (its confirmation reference died with the old run)."""
        try:
            old = await self.api.get_updates(None, 0)
        except Exception as exc:
            log.warning("telegram menu: could not skip old updates", extra={"error": type(exc).__name__})
            return
        ids = [int(u["update_id"]) for u in old if isinstance(u.get("update_id"), int)]
        if ids:
            self.offset = max(ids) + 1
            log.info("telegram menu skipped updates sent while the bot was off", extra={"count": len(ids)})

    async def poll_once(self) -> int:
        updates = await self.api.get_updates(self.offset, self.settings.poll_timeout_seconds)
        for raw in updates:
            uid = raw.get("update_id")
            if isinstance(uid, int):
                self.offset = max(self.offset or 0, uid + 1)
            await self.safe_handle(raw)
        return len(updates)

    async def _poll_loop(self) -> None:
        await self.drain_backlog()
        delay = 1.0
        while not self._closing:
            try:
                started = self._clock()
                n = await self.poll_once()
                delay = 1.0
                # Always give the trading loop a turn; a poll that came back at once with nothing (a proxy or
                # server not holding the long poll) must not turn into a busy loop.
                await asyncio.sleep(0)
                if n == 0 and self._clock() - started < MIN_EMPTY_POLL_SECONDS:
                    await self._sleep(MIN_EMPTY_POLL_SECONDS)
            except asyncio.CancelledError:
                raise
            except TelegramDisabled as exc:
                self.disabled = str(exc)
                log.error("telegram menu stopped: Telegram rejected the bot or chat id (check TELEGRAM_CHAT_ID)",
                          extra={"error": str(exc)})
                return
            except Exception as exc:
                self.poll_errors += 1
                log.warning("telegram menu poll failed; retrying (trading is unaffected)",
                            extra={"error": type(exc).__name__, "retry_seconds": delay})
                await self._sleep(delay)
                delay = min(60.0, delay * 2)

    async def _watch_loop(self) -> None:
        while not self._closing:
            try:
                await self.check_recovery()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("telegram menu recovery check failed (trading is unaffected)")
            await self._sleep(self.watch_interval)

    # ------------------------------------------------------------------ #
    # Updates
    # ------------------------------------------------------------------ #
    async def safe_handle(self, raw: dict[str, Any]) -> None:
        try:
            await self.handle(raw)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("telegram menu update failed (trading is unaffected)")

    async def handle(self, raw: dict[str, Any]) -> None:
        u = parse_update(raw)
        if u is None:
            return
        if not self.api.authorised(u):
            self.ignored += 1
            log.warning("telegram update ignored: not the configured chat",
                        extra={"chat_id": u.chat_id, "user_id": u.user_id})
            return
        if not self._allow_press():
            self.limited += 1
            log.info("telegram press rate-limited", extra={"user_id": u.user_id})
            await self.api.answer(u.callback_id, "Too many presses - wait a moment.")
            return
        if u.callback_id is not None:
            await self.api.answer(u.callback_id)
            await self._on_button(u)
        elif u.text is not None:
            word = u.text.strip().lower().lstrip("/").split("@")[0]
            if word in MENU_WORDS:
                await self.send_menu()
            else:
                await self.api.send("Send Menu to open the NIFTY bot menu.", MENU_BUTTON)

    def _allow_press(self) -> bool:
        now = self._clock()
        while self._presses and now - self._presses[0] > 60.0:
            self._presses.popleft()
        if self._presses and now - self._presses[-1] < self.settings.min_press_interval_seconds:
            return False
        if len(self._presses) >= self.settings.max_presses_per_minute:
            return False
        self._presses.append(now)
        return True

    async def send_menu(self) -> None:
        await self.api.send(f"NIFTY bot ({self.bot.cfg.mode.value}) - choose:", menu_buttons())

    async def _on_button(self, u: TelegramUpdate) -> None:
        data = u.callback_data or ""
        if BUTTON_ID.fullmatch(data) is None:
            log.warning("telegram button ignored: unknown id", extra={"data": data[:64]})
            return
        kind, name = data.split(":", 1)
        if kind == "v":
            if name == "menu":
                await self.send_menu()
            else:
                await self.api.send(await self.view(name), MENU_BUTTON)
        elif kind == "c":
            await self._control(name)
        elif kind == "x":
            await self._choose_exit(name)
        elif kind == "y":
            await self._confirmed(name, u)
        elif kind == "n":
            self._pending.pop(name, None)
            await self.api.send("Cancelled. Nothing was changed.", MENU_BUTTON)

    # ------------------------------------------------------------------ #
    # Read-only views
    # ------------------------------------------------------------------ #
    async def view(self, name: str) -> str:
        if name == "log":
            return await asyncio.to_thread(self.recent_log)
        if name == "shadow":
            return await asyncio.to_thread(self.shadow_summary)
        if name == "trades":
            return self.trades_today()
        state = self.bot.control.last_state
        if state is None:
            return "The bot has not published its state yet. Try again in a few seconds."
        build = {"status": self.status, "positions": self.positions, "health": self.health, "risk": self.risk,
                 "signals": self.signals}[name]
        return build(state)

    def status(self, s: dict[str, Any]) -> str:
        c = s.get("connections") or {}
        sess = s.get("session") or {}
        fs = c.get("feed_stability") or {}
        taking = "taking signals" if s.get("accepting_signals") else "not taking signals"
        lines = [f"NIFTY bot · {s.get('mode')} · engine {s.get('engine_state')} ({taking})",
                 f"State published {_hms(s.get('published_at'))}"]
        feed = "connected" if c.get("websocket_connected") else "NOT connected"
        if c.get("websocket_stale"):
            feed += ", STALE"
        lines.append(f"Feed: {feed} · {c.get('websocket_state') or '-'} · {fs.get('detail') or '-'}")
        lines.append(f"Market: {sess.get('phase')} · trading day {'yes' if sess.get('trading_day') else 'no'} · "
                     f"no new entries after {sess.get('no_new_entries_after')} · square-off "
                     f"{sess.get('square_off_start')}")
        block = sess.get("entry_block")
        lines.append("Entries: " + (f"BLOCKED - {block}" if block else "allowed (risk checks apply)"))
        lines.append(self._kill_line(s))
        lines.append(f"Open positions: {len(s.get('positions') or [])} · open orders: {s.get('open_orders', 0)}")
        return "\n".join(lines)

    @staticmethod
    def _kill_line(s: dict[str, Any]) -> str:
        events = (s.get("kill_switch") or {}).get("events") or []
        if not events:
            return "Kill switch: not active"
        parts = [f"{e.get('scope')} {e.get('reason')} since {_hms(e.get('since'))}: {e.get('message')}"
                 for e in events]
        return "Kill switch: ACTIVE - " + "; ".join(parts)

    def positions(self, s: dict[str, Any]) -> str:
        rows = s.get("positions") or []
        if not rows:
            return "No open positions."
        lines = [f"Open positions ({len(rows)}):"]
        for p in rows:
            r = f" · {_num(p.get('r_multiple'))}R" if p.get("r_multiple") is not None else ""
            target = _num(p.get("target")) if p.get("target") is not None else "none"
            lines.append(f"{p.get('symbol')} ({p.get('strategy_id')}) qty {p.get('quantity')} · entry "
                         f"{_num(p.get('entry_price'))} · ltp {_num(p.get('ltp'))} · P&L {_rs(p.get('unrealized'))}"
                         f"{r} · stop {_num(p.get('stop_loss'))} · target {target}"
                         + (" · EXITING" if p.get("exit_in_progress") else ""))
        return "\n".join(lines)

    def trades_today(self) -> str:
        day = self.bot._now().date()
        closed = sorted(self.bot.repos.trades.list_closed_on(day), key=lambda t: t.exit_time or t.entry_time or day)
        open_n = self.bot.stack.positions.open_trade_count()
        if not closed:
            return f"No closed trades today ({day}). Open positions: {open_n}."
        total = sum(float(t.net_pnl or 0) for t in closed)
        lines = [f"Today's closed trades ({day}): {len(closed)} · net {_rs(total)} · open now: {open_n}"]
        for t in closed[-20:]:
            when = t.exit_time.strftime("%H:%M") if t.exit_time else "--:--"
            lines.append(f"{when} {t.trading_symbol} ({t.strategy_id}) {_num(t.entry_price)} -> {_num(t.exit_price)}"
                         f" · {t.exit_reason or '-'} · net {_rs(t.net_pnl)}")
        return "\n".join(lines)

    def health(self, s: dict[str, Any]) -> str:
        c = s.get("connections") or {}
        clock = c.get("clock") or {}
        fs = c.get("feed_stability") or {}
        connected = "yes" if c.get("websocket_connected") else "NO"
        lines = ["Health",
                 f"Feed: {c.get('websocket_state') or '-'} · connected {connected}"
                 f" · stale {'YES' if c.get('websocket_stale') else 'no'} · last message "
                 f"{_num(c.get('last_message_age_seconds'), '.1f')}s ago",
                 f"Stable: {fs.get('detail') or '-'}",
                 f"Feed lag: {_num(c.get('feed_lag_seconds'), '+.2f')}s (entry limit "
                 f"±{_num(c.get('entry_feed_lag_limit_seconds'), 'g')}s)",
                 f"PC clock: offset {_num(clock.get('offset_seconds'), '+.2f')}s vs Upstox"
                 + (" · DRIFTED" if clock.get("drifted") else "")
                 + (f" · last check failed ({clock.get('error')})" if clock.get("error") else ""),
                 f"Reconnects: {c.get('reconnects') if c.get('reconnects') is not None else '-'} · late ticks dropped: "
                 f"{c.get('late_ticks_dropped') if c.get('late_ticks_dropped') is not None else '-'}",
                 f"Messages: {c.get('messages') if c.get('messages') is not None else '-'} · errors: "
                 f"{c.get('message_errors') if c.get('message_errors') is not None else '-'}"]
        return "\n".join(lines)

    def risk(self, s: dict[str, Any]) -> str:
        d = s.get("daily") or {}
        lines = ["Daily loss used / left"]
        for sid, g in sorted((d.get("strategies") or {}).items()):
            loss, limit = float(g.get("loss") or 0), float(g.get("limit") or 0)
            flags = (" · LIMIT HIT" if g.get("breached") else "") + (f" · paused: {g['paused']}" if g.get("paused")
                                                                      else "")
            lines.append(f"{sid}: used {_rs(loss)} of {_rs(limit)} · left {_rs(max(0.0, limit - loss))} · trades "
                         f"{g.get('trades', 0)}{flags}")
        gl, glim = float(d.get("global_loss") or 0), float(d.get("global_limit") or 0)
        lines.append(f"GLOBAL: used {_rs(gl)} of {_rs(glim)} · left {_rs(max(0.0, glim - gl))}"
                     + (" · LIMIT HIT" if d.get("global_breached") else ""))
        return "\n".join(lines)

    def signals(self, s: dict[str, Any]) -> str:
        lines = ["Last signal per strategy"]
        for st in s.get("strategies") or []:
            sig = st.get("last_signal")
            if not sig:
                lines.append(f"{st.get('strategy_id')}: no signal since the bot started")
                continue
            lines.append(f"{st.get('strategy_id')}: {sig.get('option_type')} at {_hms(sig.get('generated_at'))} - "
                         f"{sig.get('reason')} -> {st.get('last_decision') or 'pending'}")
        return "\n".join(lines)

    def recent_log(self) -> str:
        path = self.log_path
        if not path.exists():
            return "No log file yet."
        with path.open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - LOG_TAIL_BYTES))
            raw = fh.read().decode("utf-8", errors="replace")
        rows: list[str] = []
        for line in raw.splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not isinstance(rec, dict) or rec.get("level") not in IMPORTANT_LEVELS:
                continue
            sid = f" [{rec['strategy_id']}]" if rec.get("strategy_id") else ""
            rows.append(f"{_hms(rec.get('ts'))} {rec.get('level')} {rec.get('event')}{sid}")
        if not rows:
            return "No warnings or errors in the recent log."
        return DEFAULT_MASKER.mask_text("Recent important log lines:\n" + "\n".join(rows[-LOG_LINES:]))

    def _latest(self, pattern: str) -> Path | None:
        files = sorted(self.research_dir.glob(pattern)) if self.research_dir.exists() else []
        return files[-1] if files else None

    def shadow_summary(self) -> str:
        variants, shadow = self._latest("variants-*.txt"), self._latest("shadow-*.txt")
        if variants is None and shadow is None:
            return "No research report yet (run daily-research after 15:40)."
        lines: list[str] = []
        if variants is not None:
            text = variants.read_text(encoding="utf-8", errors="replace").splitlines()
            lines.append(f"Shadow variants ({variants.stem.removeprefix('variants-')}): trades | net per column")
            lines += [x.strip() for x in text if x.strip().startswith(("session", "counted"))]
            lines += [x.strip() for x in text if x.strip().startswith(("A (", "B ("))]
        if shadow is not None:
            text = shadow.read_text(encoding="utf-8", errors="replace").splitlines()
            head = next((i for i, x in enumerate(text) if x.startswith("Shadow decision engine")), None)
            if head is not None:
                rows = [x.split() for x in text[head + 2:] if re.match(r"\d{4}-\d{2}-\d{2}T", x)]
                counted = [r for r in rows if "yes" in r]
                trade = sum(1 for r in counted if len(r) > 2 and r[2] == "TRADE")
                engine = text[head].split("(")[0].strip()
                lines.append(f"Decision engine ({shadow.stem.removeprefix('shadow-')}): {engine}"
                             f" · counted signals {len(counted)} · TRADE {trade} · NO_TRADE {len(counted) - trade}")
                if rows:
                    lines.append("latest: " + " ".join(rows[-1][:4]))
        lines.append("Research only: never affects orders.")
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # Controls (confirm first, re-check on Yes, then the control queue)
    # ------------------------------------------------------------------ #
    def _new_ref(self) -> str:
        return f"{secrets.randbits(32):08x}"

    def _purge(self) -> None:
        now = self._clock()
        for ref in [r for r, p in self._pending.items() if now - p.created > self.settings.confirm_ttl_seconds]:
            del self._pending[ref]
        while len(self._pending) > 50:
            del self._pending[next(iter(self._pending))]

    def _remember(self, p: Pending) -> str:
        self._purge()
        ref = self._new_ref()
        while ref in self._pending:
            ref = self._new_ref()
        self._pending[ref] = p
        return ref

    def _take(self, ref: str, kinds: frozenset[str]) -> Pending | None:
        p = self._pending.pop(ref, None)
        if p is None or p.kind not in kinds:
            return None
        if self._clock() - p.created > self.settings.confirm_ttl_seconds:
            return None
        return p

    async def _ask(self, p: Pending, prompt: str) -> None:
        ref = self._remember(p)
        await self.api.send(f"{prompt}\n\nAre you sure?", [[("Yes", f"y:{ref}"), ("No", f"n:{ref}")]])

    def reset_check(self) -> tuple[bool, str]:
        """Telegram resets the GLOBAL kill only, and only on a stable feed."""
        ks = self.bot.stack.kill_switch
        ev = ks.global_event
        if ev is None:
            if ks.strategy_events:
                return False, ("only strategy kill switches are active (" + ", ".join(ks.strategy_events)
                               + "): reset those from the dashboard")
            return False, "the kill switch is not active"
        ok, why = self.bot.control.feed_stability.check(self._wall())
        if not ok:
            return False, why
        return True, f"{ev.reason.value} since {ev.at:%H:%M:%S}; {why}"

    async def _control(self, name: str) -> None:
        open_n = self.bot.stack.positions.open_trade_count()
        if name == "pause":
            await self._ask(Pending("pause", self._clock(), label="Pause entries"),
                            "Pause NEW entries? Open positions stay protected (stops, targets, square-off keep "
                            "running).")
        elif name == "resume":
            warn = ""
            if self.bot.stack.kill_switch.active:
                warn = "\nNote: the kill switch is active, so entries stay blocked until it is reset."
            await self._ask(Pending("resume", self._clock(), label="Resume entries"),
                            "Resume new entries? Every risk check still applies." + warn)
        elif name == "kill":
            await self._ask(Pending("kill", self._clock(), label="Emergency kill"),
                            f"EMERGENCY KILL: blocks all new entries and exits every open position ({open_n} open)."
                            " Only a named human can reset it.")
        elif name == "reset":
            ok, why = self.reset_check()
            if not ok:
                await self.api.send(f"Reset refused: {why}.", MENU_BUTTON)
                return
            await self._ask(Pending("reset", self._clock(), label="Reset kill switch"),
                            f"Reset the GLOBAL kill switch ({why})? Positions: {open_n}. Reconciliation runs first;"
                            " strategies are re-activated the same way as from the dashboard.")
        elif name == "exit":
            trades = self.bot.stack.positions.open_trades()
            if not trades:
                await self.api.send("No open positions.", MENU_BUTTON)
                return
            rows: Buttons = []
            for t in trades[:10]:
                ref = self._remember(Pending("choose_exit", self._clock(), target=t.trade_uid))
                rows.append([(f"{t.instrument.trading_symbol} · {t.strategy_id} · qty {t.open_qty}", f"x:{ref}")])
            rows.append([("Menu", "v:menu")])
            await self.api.send("Which position should be exited?", rows)

    async def _choose_exit(self, ref: str) -> None:
        p = self._take(ref, frozenset({"choose_exit"}))
        if p is None or p.target is None:
            await self.api.send("That choice has expired. Open the menu again.", MENU_BUTTON)
            return
        t = self.bot.stack.positions.trades.get(p.target)
        if t is None or t.status is not TradeStatus.OPEN:
            await self.api.send("That position is no longer open.", MENU_BUTTON)
            return
        ltp = t.last_price if t.last_price is not None else self.bot.md.ltp(t.instrument.instrument_key)
        await self._ask(Pending("exit", self._clock(), target=t.trade_uid,
                                label=f"Exit {t.instrument.trading_symbol}"),
                        f"Exit {t.instrument.trading_symbol} ({t.strategy_id}, qty {t.open_qty}, entry "
                        f"{t.avg_entry:.2f}, ltp {_num(ltp)}) at market now?")

    async def _confirmed(self, ref: str, u: TelegramUpdate) -> None:
        p = self._take(ref, CONFIRMABLE)
        if p is None:
            await self.api.send("This confirmation has expired or was already used. Nothing was changed.",
                                MENU_BUTTON)
            return
        operator = self.settings.operator_name or u.user_name
        # Re-check the state now: the prompt may be a minute old.
        if p.kind in ("pause", "resume"):
            if self.bot.engine is None:
                await self.api.send("The bot is not prepared yet. Nothing was changed.", MENU_BUTTON)
                return
            cmd = ControlCommandType.ENGINE_PAUSE if p.kind == "pause" else ControlCommandType.ENGINE_START
            cid = self._enqueue(cmd, operator, f"{p.label} from Telegram")
        elif p.kind == "kill":
            cid = self._enqueue(ControlCommandType.KILL_SWITCH, operator, "emergency kill from Telegram")
        elif p.kind == "reset":
            ok, why = self.reset_check()
            if not ok:
                await self.api.send(f"Reset refused: {why}.", MENU_BUTTON)
                return
            cid = self._enqueue(ControlCommandType.RESET_KILL_SWITCH, operator, f"reset from Telegram ({why})")
        else:  # exit
            t = self.bot.stack.positions.trades.get(p.target or "")
            if t is None or t.status is not TradeStatus.OPEN:
                await self.api.send("That position is no longer open. Nothing was changed.", MENU_BUTTON)
                return
            cid = self._enqueue(ControlCommandType.EXIT_POSITION, operator, "exit from Telegram", target=t.trade_uid)
        self._spawn(self._report_result(cid, p.label), f"telegram-result-{cid}")

    def _enqueue(self, command: ControlCommandType, operator: str, note: str, target: str | None = None) -> int:
        if command not in ALLOWED_COMMANDS:  # pragma: no cover - the menu builds only allowed commands
            raise PermissionError(f"{command.value} cannot be requested from Telegram")
        now = self._wall()
        cid = self.bot.repos.commands.enqueue(ControlCommandRecord(
            mode=self.bot.repos.mode, command=command, operator=operator, note=note, target=target,
            payload_json={"source": SOURCE}, requested_at=now,
            expires_at=None if command is ControlCommandType.KILL_SWITCH else now + COMMAND_TTL))
        log.warning("telegram control requested", extra={"command": command.value, "id": cid, "operator": operator,
                                                         "source": SOURCE, "target": target})
        return cid

    async def _report_result(self, cid: int, label: str) -> None:
        deadline = self._clock() + self.settings.result_timeout_seconds
        while True:
            rec = self.bot.repos.commands.get(cid)
            if rec is not None and rec.status is not ControlCommandStatus.PENDING:
                icon = "✅" if rec.status is ControlCommandStatus.DONE else "❌"
                await self.api.send(f"{icon} {label}: {rec.result_message or rec.status.value}", MENU_BUTTON)
                return
            if self._clock() >= deadline:
                await self.api.send(f"⏳ {label} (#{cid}) is still running. Check Status / Positions.", MENU_BUTTON)
                return
            await self._sleep(0.5)

    async def wait_idle(self) -> None:
        """Await result reports (tests)."""
        pending = [t for t in self._tasks if t.get_name().startswith("telegram-result-")]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    # ------------------------------------------------------------------ #
    # Feed recovery after a WEBSOCKET_DISCONNECT kill (never resets)
    # ------------------------------------------------------------------ #
    async def check_recovery(self) -> bool:
        ev = self.bot.stack.kill_switch.global_event
        if ev is None or ev.reason is not KillReason.WEBSOCKET_DISCONNECT:
            return False
        key = ev.at.isoformat()
        if key in self._recovered:
            return False
        ok, _ = self.bot.control.feed_stability.check(self._wall())
        if not ok:
            return False
        n = self.bot.stack.positions.open_trade_count()
        sent = await self.api.send(f"Feed is back and stable. Positions: {n}. Tap Reset to resume",
                                   [[("Reset kill switch", "c:reset")], [("Menu", "v:menu")]])
        if sent:
            self._recovered.add(key)
            log.warning("telegram: feed recovery message sent (the bot does not reset itself)",
                        extra={"kill_since": key, "positions": n})
        return sent


__all__ = ["ALLOWED_COMMANDS", "BUTTON_ID", "CONTROLS", "VIEWS", "TelegramMenu", "menu_buttons"]
