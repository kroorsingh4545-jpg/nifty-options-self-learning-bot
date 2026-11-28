"""Telegram alert channel (Phase 10): outbound messages only.

What is sent (``alerts.telegram`` in settings.yaml): entries, exits, errors,
daily-loss breaches, kill-switch events and the end-of-day summary, plus any
other alert at or above ``min_severity``. Kill-switch and daily-loss alerts
are CRITICAL and are always sent.

Guarantees:
* ``send`` never blocks and never raises: alerts go into a bounded queue
  drained by a background task, so a slow or unreachable Telegram can never
  delay a kill switch, an exit or the heartbeat. When the queue is full a new
  non-critical alert is dropped; a CRITICAL one replaces the oldest entry.
* ``send`` is thread-safe (the error-log handler can call it from any thread).
* At most one message per ``min_interval_seconds`` (Telegram allows about
  one per second per chat); HTTP 429 waits ``retry_after``; network errors
  retry with backoff; an invalid token or chat (401 / 403 / 404) disables the
  channel with one ERROR log instead of retrying forever.
* The bot token is only ever in the request URL. Every message is passed
  through the secret masker, and failures log the error type only.
* This is the only module besides the Upstox adapters that may open a network
  connection (architecture test); it never receives Upstox credentials.

``TelegramBotApi`` (below) is the transport of the control menu
(``runtime/telegram_menu.py``): outbound long-polling of ``getUpdates``, never
a webhook or an open port. It knows nothing about trading.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx

from config.settings import TelegramSection
from core.enums import RiskSeverity, TradingMode
from core.masking import DEFAULT_MASKER, SecretMasker
from monitoring.alerts import Alert, AlertKind

log = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"
MAX_TEXT = 4000  # Telegram's limit is 4096 characters
SEVERITY_ORDER = {RiskSeverity.INFO: 0, RiskSeverity.WARNING: 1, RiskSeverity.CRITICAL: 2}
ICONS = {RiskSeverity.INFO: "ℹ️", RiskSeverity.WARNING: "⚠️", RiskSeverity.CRITICAL: "🚨"}
HEADINGS = {AlertKind.ENTRY: "ENTRY", AlertKind.EXIT: "EXIT", AlertKind.ERROR: "ERROR",
            AlertKind.DAILY_LOSS: "DAILY LOSS LIMIT BREACHED", AlertKind.KILL_SWITCH: "KILL SWITCH",
            AlertKind.EOD_SUMMARY: "END OF DAY", AlertKind.GENERAL: "ALERT"}
DISABLING_STATUS = frozenset({401, 403, 404})


def format_message(alert: Alert, mode: TradingMode, masker: SecretMasker = DEFAULT_MASKER) -> str:
    lines = [f"{ICONS.get(alert.severity, '')} [{mode.value}] {HEADINGS[alert.kind]} · {alert.title}".strip(),
             alert.message]
    if alert.strategy_id:
        lines.append(f"strategy: {alert.strategy_id}")
    for k, v in alert.details.items():
        lines.append(f"{k}: {v}")
    lines.append(alert.at.strftime("%Y-%m-%d %H:%M:%S IST"))
    text = masker.mask_text("\n".join(x for x in lines if x))
    return text if len(text) <= MAX_TEXT else text[: MAX_TEXT - 1] + "…"


class TelegramAlertSink:
    channel = "telegram"

    def __init__(self, *, token: str, chat_id: str, settings: TelegramSection | None = None,
                 mode: TradingMode = TradingMode.PAPER, http: httpx.AsyncClient | None = None,
                 masker: SecretMasker = DEFAULT_MASKER,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        if not token or not chat_id:
            raise ValueError("Telegram needs TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")
        self.settings = settings or TelegramSection()
        self._url = f"{TELEGRAM_API}/bot{token}/sendMessage"
        self._chat_id = chat_id
        self.mode = TradingMode(mode)
        self._masker = masker
        masker.register_many([token])
        self._sleep = sleep
        self._owns_http = http is None
        self._http = http or httpx.AsyncClient(timeout=httpx.Timeout(self.settings.request_timeout_seconds),
                                               follow_redirects=False)
        self._queue: deque[Alert] = deque()
        self._lock = threading.Lock()
        self._wake: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[None] | None = None
        self._closing = False
        self.disabled: str | None = None
        self.sent = 0
        self.dropped = 0
        self.failed = 0

    # ------------------------------------------------------------------ #
    def wants(self, alert: Alert) -> bool:
        s = self.settings
        if alert.severity is RiskSeverity.CRITICAL:
            return True
        by_kind = {AlertKind.ENTRY: s.entries, AlertKind.EXIT: s.exits, AlertKind.ERROR: s.errors,
                   AlertKind.EOD_SUMMARY: s.eod_summary, AlertKind.DAILY_LOSS: True, AlertKind.KILL_SWITCH: True}
        if alert.kind in by_kind:
            return by_kind[alert.kind]
        return SEVERITY_ORDER[alert.severity] >= SEVERITY_ORDER[RiskSeverity(s.min_severity)]

    def send(self, alert: Alert) -> None:
        """Queue an alert (never blocks, never raises)."""
        try:
            if self.disabled is not None or self._closing or not self.wants(alert):
                return
            with self._lock:
                if len(self._queue) >= self.settings.queue_size:
                    if alert.severity is not RiskSeverity.CRITICAL:
                        self.dropped += 1
                        return
                    self._queue.popleft()
                    self.dropped += 1
                self._queue.append(alert)
            self._notify()
        except Exception:  # an alert channel must never break the caller
            log.exception("telegram alert could not be queued")

    def _notify(self) -> None:
        loop, wake = self._loop, self._wake
        if loop is None or wake is None or loop.is_closed():
            return  # delivered once start() runs
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            wake.set()
        else:
            loop.call_soon_threadsafe(wake.set)

    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        if self._task is not None:
            return
        self._loop = asyncio.get_running_loop()
        self._wake = asyncio.Event()
        self._task = self._loop.create_task(self._worker(), name="telegram-alerts")
        if self._queue:
            self._wake.set()

    def _pop(self) -> Alert | None:
        with self._lock:
            return self._queue.popleft() if self._queue else None

    async def _worker(self) -> None:
        assert self._wake is not None
        while True:
            alert = self._pop()
            if alert is None:
                if self._closing:
                    return
                self._wake.clear()
                if self._queue:  # raced with send()
                    continue
                await self._wake.wait()
                continue
            if self.disabled is not None:
                continue
            await self._deliver(alert)
            await self._sleep(self.settings.min_interval_seconds)

    async def _deliver(self, alert: Alert) -> None:
        payload = {"chat_id": self._chat_id, "text": format_message(alert, self.mode, self._masker),
                   "disable_web_page_preview": True}
        for attempt in range(self.settings.max_attempts):
            try:
                resp = await self._http.post(self._url, json=payload)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                log.warning("telegram send failed; retrying", extra={"error": type(exc).__name__,
                                                                      "attempt": attempt + 1})
                await self._sleep(min(30.0, 2.0 ** attempt))
                continue
            if resp.status_code == 200:
                self.sent += 1
                return
            if resp.status_code == 429:
                retry_after = 5.0
                with contextlib.suppress(Exception):
                    retry_after = float(resp.json().get("parameters", {}).get("retry_after", 5))
                await self._sleep(min(max(retry_after, 1.0), 60.0))
                continue
            if resp.status_code in DISABLING_STATUS:
                self.disabled = f"HTTP {resp.status_code}"
                log.error("telegram alerts disabled: the bot token or chat id was rejected",
                          extra={"status": resp.status_code})
                return
            if resp.status_code >= 500:
                await self._sleep(min(30.0, 2.0 ** attempt))
                continue
            log.error("telegram rejected an alert", extra={"status": resp.status_code})
            break
        self.failed += 1

    async def aclose(self, timeout: float = 10.0) -> None:
        """Deliver what is queued (within ``timeout``), then stop."""
        self._closing = True
        if self._task is not None:
            self._notify()
            try:
                await asyncio.wait_for(asyncio.shield(self._task), timeout)
            except (TimeoutError, asyncio.CancelledError, Exception):
                self._task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await self._task
        if self._owns_http:
            await self._http.aclose()

    def describe(self) -> str:
        if self.disabled is not None:
            return f"disabled ({self.disabled})"
        return f"on (sent {self.sent}, queued {len(self._queue)}, dropped {self.dropped}, failed {self.failed})"


# --------------------------------------------------------------------------- #
# Control-menu transport (outbound long-polling only)
# --------------------------------------------------------------------------- #
class TelegramApiError(Exception):
    """A Telegram request failed (network, 409 conflict, 5xx): retry later."""


class TelegramDisabled(TelegramApiError):
    """The bot token or chat was rejected (401 / 403 / 404): stop polling."""


@dataclass(frozen=True)
class TelegramUpdate:
    """One incoming update, reduced to what the menu needs. ``callback_data``
    is untrusted text: the menu only accepts its own button ids."""

    update_id: int
    chat_id: str
    user_id: str | None
    user_name: str
    text: str | None = None
    callback_id: str | None = None
    callback_data: str | None = None


def _user_name(user: dict[str, Any]) -> str:
    if user.get("username"):
        return f"@{user['username']}"
    name = " ".join(str(user.get(k) or "").strip() for k in ("first_name", "last_name")).strip()
    return name or (f"telegram user {user['id']}" if user.get("id") is not None else "telegram user")


def parse_update(raw: dict[str, Any]) -> TelegramUpdate | None:
    """A message or button press, or None for anything else / malformed."""
    try:
        uid = int(raw["update_id"])
        cb = raw.get("callback_query")
        if isinstance(cb, dict):
            user = cb.get("from") or {}
            chat = ((cb.get("message") or {}).get("chat") or {})
            if chat.get("id") is None:
                return None
            data = cb.get("data")
            return TelegramUpdate(uid, str(chat["id"]), str(user["id"]) if user.get("id") is not None else None,
                                  _user_name(user), callback_id=str(cb.get("id") or ""),
                                  callback_data=data if isinstance(data, str) else None)
        msg = raw.get("message")
        if isinstance(msg, dict):
            user = msg.get("from") or {}
            chat = msg.get("chat") or {}
            if chat.get("id") is None:
                return None
            text = msg.get("text")
            return TelegramUpdate(uid, str(chat["id"]), str(user["id"]) if user.get("id") is not None else None,
                                  _user_name(user), text=text if isinstance(text, str) else None)
    except (KeyError, TypeError, ValueError):
        return None
    return None


Buttons = list[list[tuple[str, str]]]  # rows of (label, callback id)


class TelegramBotApi:
    """The control menu's Telegram client: ``getUpdates`` long-polling,
    ``sendMessage`` with inline buttons and ``answerCallbackQuery``.

    Outbound HTTPS to api.telegram.org only: no webhook is registered and no
    port is opened. ``send`` / ``answer`` never raise; ``get_updates`` raises
    ``TelegramApiError`` (retry) or ``TelegramDisabled`` (stop). The token is
    only in the URL, registered with the masker, and errors log their type
    only. ``authorised`` is the single chat check: every other chat is ignored."""

    def __init__(self, *, token: str, chat_id: str, settings: TelegramSection | None = None,
                 http: httpx.AsyncClient | None = None, masker: SecretMasker = DEFAULT_MASKER) -> None:
        if not token or not chat_id:
            raise ValueError("the Telegram menu needs TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")
        self.settings = settings or TelegramSection()
        self._base = f"{TELEGRAM_API}/bot{token}"
        self._chat_id = str(chat_id).strip()
        self._masker = masker
        masker.register_many([token])
        self._owns_http = http is None
        self._http = http or httpx.AsyncClient(timeout=httpx.Timeout(self.settings.request_timeout_seconds),
                                               follow_redirects=False)
        self.sent = 0
        self.failed = 0

    def authorised(self, update: TelegramUpdate) -> bool:
        return update.chat_id == self._chat_id

    async def _call(self, method: str, payload: dict[str, Any], timeout: float | None = None) -> Any:
        try:
            resp = await self._http.post(f"{self._base}/{method}", json=payload,
                                         timeout=timeout if timeout is not None else httpx.USE_CLIENT_DEFAULT)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise TelegramApiError(type(exc).__name__) from None
        if resp.status_code in DISABLING_STATUS:
            raise TelegramDisabled(f"HTTP {resp.status_code}")
        if resp.status_code != 200:
            raise TelegramApiError(f"HTTP {resp.status_code}")
        try:
            body = resp.json()
        except ValueError:
            raise TelegramApiError("invalid JSON") from None
        if not isinstance(body, dict) or not body.get("ok"):
            raise TelegramApiError("not ok")
        return body.get("result")

    async def get_updates(self, offset: int | None, timeout: int) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"timeout": int(timeout), "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            payload["offset"] = int(offset)
        result = await self._call("getUpdates", payload, timeout=timeout + self.settings.request_timeout_seconds)
        return [u for u in result if isinstance(u, dict)] if isinstance(result, list) else []

    async def send(self, text: str, buttons: Buttons | None = None) -> bool:
        text = self._masker.mask_text(text)
        if len(text) > MAX_TEXT:
            text = text[: MAX_TEXT - 1] + "…"
        payload: dict[str, Any] = {"chat_id": self._chat_id, "text": text, "disable_web_page_preview": True}
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": [[{"text": label, "callback_data": data}
                                                            for label, data in row] for row in buttons]}
        try:
            await self._call("sendMessage", payload)
        except Exception as exc:  # the menu is best effort; trading never waits for it
            self.failed += 1
            log.warning("telegram menu message not sent", extra={"error": type(exc).__name__})
            return False
        self.sent += 1
        return True

    async def answer(self, callback_id: str | None, text: str = "") -> None:
        if not callback_id:
            return
        with contextlib.suppress(Exception):
            await self._call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text[:190]})

    async def aclose(self) -> None:
        if self._owns_http:
            with contextlib.suppress(Exception):
                await self._http.aclose()


__all__ = ["TELEGRAM_API", "Buttons", "TelegramAlertSink", "TelegramApiError", "TelegramBotApi", "TelegramDisabled",
           "TelegramUpdate", "format_message", "parse_update"]
