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
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from collections import deque
from collections.abc import Awaitable, Callable

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


__all__ = ["TELEGRAM_API", "TelegramAlertSink", "format_message"]
