"""Upstox REST adapter (official API, read paths only in this phase).

Guarantees:
* Every request passes ``UpstoxRateLimiter.acquire`` first — there is no other
  code path to the network.
* Only idempotent GETs are retried (timeouts, network errors, 5xx, 429).
* 401 / invalid-token responses and the daily token expiry mark the client as
  unauthenticated: further calls fail fast without touching the network and
  ``on_auth_failure`` fires. The client never loops on failed auth.
* The bearer token is only ever placed in the request header; it is never
  logged and error messages are passed through the secret masker.
* Order placement / modification / cancellation is NOT implemented: those
  methods refuse unconditionally (and would also require TRADING_MODE=LIVE).
"""

from __future__ import annotations

import random
from collections.abc import Callable, Sequence
from datetime import date, datetime, time, timedelta
from typing import Any
from urllib.parse import quote

import httpx

from broker.broker_interface import MarketDataSource
from broker.errors import (
    BrokerAPIError,
    BrokerError,
    BrokerTimeoutError,
    LiveTradingDisabledError,
    MaintenanceWindowError,
    RateLimitError,
    TokenExpiredError,
)
from broker.upstox_mapping import parse_candles, parse_funds, parse_instrument, parse_position
from broker.upstox_rate_limiter import ApiCategory, Clock, RealClock, UpstoxRateLimiter, backoff_delay
from config import hard_limits
from config.settings import BotConfig, BrokerSection
from core.clock import IST, now_ist
from core.enums import TradingMode
from core.masking import DEFAULT_MASKER, SecretMasker
from core.models import BrokerPosition, Candle, Funds, Instrument
from monitoring.json_logger import get_logger

log = get_logger(__name__)

AUTH_ERROR_CODES = frozenset({"UDAPI100050", "UDAPI100016"})
MAINTENANCE_ERROR_CODE = "UDAPI100072"
RETRYABLE_STATUS = frozenset({500, 502, 503, 504})
LTP_MAX_KEYS = 500

TokenProvider = Callable[[], str | None]


def next_token_expiry(loaded_at: datetime, expiry_time: time) -> datetime:
    """First occurrence of ``expiry_time`` (IST) strictly after ``loaded_at``."""
    local = loaded_at.astimezone(IST)
    candidate = local.replace(hour=expiry_time.hour, minute=expiry_time.minute, second=0, microsecond=0)
    if candidate <= local:
        candidate += timedelta(days=1)
    return candidate


def _retry_after_seconds(resp: httpx.Response) -> float | None:
    value = resp.headers.get("Retry-After")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


def _first_error(body: Any) -> tuple[str | None, str]:
    if isinstance(body, dict):
        errors = body.get("errors") or []
        if errors and isinstance(errors[0], dict):
            e = errors[0]
            return e.get("error_code") or e.get("errorCode"), str(e.get("message") or "error")
        if body.get("message"):
            return None, str(body["message"])
    return None, "unexpected error response"


class UpstoxRestClient(MarketDataSource):
    def __init__(
        self,
        *,
        token_provider: TokenProvider,
        limiter: UpstoxRateLimiter,
        mode: TradingMode,
        settings: BrokerSection | None = None,
        http: httpx.AsyncClient | None = None,
        clock: Clock | None = None,
        rng: random.Random | None = None,
        masker: SecretMasker = DEFAULT_MASKER,
        now_fn: Callable[[], datetime] = now_ist,
        on_auth_failure: Callable[[str], None] | None = None,
    ) -> None:
        self.settings = settings or BrokerSection()
        self.mode = TradingMode(mode)
        self.limiter = limiter
        self._token_provider = token_provider
        self._clock: Clock = clock or RealClock()
        self._rng = rng or random.Random()  # noqa: S311 - backoff jitter only
        self._masker = masker
        self._now = now_fn
        self._on_auth_failure = on_auth_failure
        self._owns_http = http is None
        self._http = http or httpx.AsyncClient(
            base_url=self.settings.rest_base_url,
            timeout=httpx.Timeout(self.settings.request_timeout_seconds, connect=self.settings.connect_timeout_seconds),
        )
        self.auth_failed: str | None = None
        self.token_expires_at = next_token_expiry(self._now(), self.settings.token_expiry_time)

    @classmethod
    def from_config(
        cls, cfg: BotConfig, limiter: UpstoxRateLimiter, **kwargs: Any
    ) -> UpstoxRestClient:
        token = cfg.env.upstox_access_token
        return cls(
            token_provider=lambda: token.get_secret_value() if token is not None else None,
            limiter=limiter,
            mode=cfg.mode,
            settings=cfg.settings.broker,
            **kwargs,
        )

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    # ------------------------------------------------------------------ #
    # Auth state
    # ------------------------------------------------------------------ #
    def reset_auth(self) -> None:
        """Call after a fresh access token has been provisioned."""
        self.auth_failed = None
        self.token_expires_at = next_token_expiry(self._now(), self.settings.token_expiry_time)

    def _fail_auth(self, reason: str) -> TokenExpiredError:
        first = self.auth_failed is None
        self.auth_failed = reason
        if first:
            log.error("upstox authentication failed; trading actions must stop", extra={"reason": reason})
            if self._on_auth_failure is not None:
                self._on_auth_failure(reason)
        return TokenExpiredError(f"Upstox authentication unavailable: {reason}. Re-authenticate.")

    def _token(self) -> str:
        if self.auth_failed is not None:
            raise TokenExpiredError(f"Upstox authentication unavailable: {self.auth_failed}. Re-authenticate.")
        if self._now() >= self.token_expires_at:
            raise self._fail_auth("access token past daily expiry")
        token = self._token_provider()
        if not token:
            raise self._fail_auth("UPSTOX_ACCESS_TOKEN not configured")
        return token

    # ------------------------------------------------------------------ #
    # Core request path
    # ------------------------------------------------------------------ #
    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return await self._request("GET", path, params=params, category=ApiCategory.STANDARD, retry=True)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        category: ApiCategory,
        params: dict[str, Any] | None = None,
        retry: bool,
    ) -> Any:
        cfg = self.settings
        attempt = 0
        while True:
            token = self._token()
            await self.limiter.acquire(category)  # raises if no slot -> request is never sent
            try:
                resp = await self._http.request(
                    method,
                    path,
                    params=params,
                    headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                )
            except httpx.TimeoutException:
                if retry and attempt < cfg.max_retries:
                    await self._backoff(attempt, path, "timeout")
                    attempt += 1
                    continue
                raise BrokerTimeoutError(f"{method} {path} timed out") from None
            except httpx.TransportError as exc:
                if retry and attempt < cfg.max_retries:
                    await self._backoff(attempt, path, type(exc).__name__)
                    attempt += 1
                    continue
                raise BrokerError(f"{method} {path} network error: {type(exc).__name__}") from None

            if resp.status_code == 429:
                cooldown = self.limiter.record_429(category, _retry_after_seconds(resp))
                log.warning("upstox HTTP 429", extra={"path": path, "cooldown_s": cooldown, "attempt": attempt})
                if retry and attempt < cfg.max_retries:
                    attempt += 1
                    continue  # limiter enforces the cooldown before the next send
                raise RateLimitError(f"{method} {path}: HTTP 429 after {attempt + 1} attempt(s)")

            self.limiter.record_success(category)
            body = self._json(resp)
            code, message = _first_error(body) if resp.status_code >= 400 or self._is_error(body) else (None, "")

            if resp.status_code == 401 or code in AUTH_ERROR_CODES:
                raise self._fail_auth(f"HTTP {resp.status_code} {code or ''}".strip())
            if code == MAINTENANCE_ERROR_CODE:
                raise MaintenanceWindowError(self._masker.mask_text(message))
            if resp.status_code in RETRYABLE_STATUS and retry and attempt < cfg.max_retries:
                await self._backoff(attempt, path, f"HTTP {resp.status_code}")
                attempt += 1
                continue
            if resp.status_code >= 400 or self._is_error(body):
                raise BrokerAPIError(resp.status_code, code, self._masker.mask_text(message))
            return body.get("data") if isinstance(body, dict) else body

    @staticmethod
    def _is_error(body: Any) -> bool:
        return isinstance(body, dict) and body.get("status") == "error"

    @staticmethod
    def _json(resp: httpx.Response) -> Any:
        try:
            return resp.json()
        except ValueError:
            return {}

    async def _backoff(self, attempt: int, path: str, reason: str) -> None:
        delay = backoff_delay(attempt, self.settings.backoff_base_seconds, self.settings.backoff_max_seconds,
                              rng=self._rng)
        log.warning("retrying upstox request", extra={"path": path, "reason": reason, "delay_s": round(delay, 3)})
        await self._clock.sleep(delay)

    # ------------------------------------------------------------------ #
    # Market data (MarketDataSource)
    # ------------------------------------------------------------------ #
    async def get_option_contracts(self, underlying_key: str, expiry: date | None = None) -> list[Instrument]:
        params: dict[str, Any] = {"instrument_key": underlying_key}
        if expiry is not None:
            params["expiry_date"] = expiry.isoformat()
        data = await self._get("/v2/option/contract", params)
        return [parse_instrument(rec) for rec in data or []]

    async def get_option_chain(self, underlying_key: str, expiry: date) -> dict[str, Any]:
        params = {"instrument_key": underlying_key, "expiry_date": expiry.isoformat()}
        data = await self._get("/v2/option/chain", params)
        return {"underlying_key": underlying_key, "expiry": expiry.isoformat(), "rows": data or []}

    async def get_ltp(self, instrument_keys: Sequence[str]) -> dict[str, float]:
        """REST LTP snapshot — for recovery only; the WebSocket is primary."""
        out: dict[str, float] = {}
        keys = list(dict.fromkeys(instrument_keys))
        for i in range(0, len(keys), LTP_MAX_KEYS):
            chunk = keys[i : i + LTP_MAX_KEYS]
            data = await self._get("/v3/market-quote/ltp", {"instrument_key": ",".join(chunk)})
            for value in (data or {}).values():
                token = value.get("instrument_token")
                if token and value.get("last_price") is not None:
                    out[token] = float(value["last_price"])
        return out

    async def get_intraday_candles(self, instrument_key: str, interval_minutes: int) -> list[Candle]:
        if not 1 <= interval_minutes <= 300:
            raise ValueError("interval_minutes must be 1..300")
        path = f"/v3/historical-candle/intraday/{quote(instrument_key, safe='')}/minutes/{interval_minutes}"
        data = await self._get(path)
        return parse_candles(instrument_key, (data or {}).get("candles", []), interval_minutes, self._now())

    # ------------------------------------------------------------------ #
    # Account (read-only)
    # ------------------------------------------------------------------ #
    async def get_positions(self) -> list[BrokerPosition]:
        data = await self._get("/v2/portfolio/short-term-positions")
        return [parse_position(rec) for rec in data or []]

    async def get_funds(self) -> Funds:
        return parse_funds(await self._get("/v2/user/get-funds-and-margin", {"segment": "SEC"}) or {})

    async def get_order_book(self) -> list[dict[str, Any]]:
        return list(await self._get("/v2/order/retrieve-all") or [])

    # ------------------------------------------------------------------ #
    # WebSocket authorisation (single-use URLs; call on every (re)connect)
    # ------------------------------------------------------------------ #
    async def authorize_market_feed(self) -> str:
        data = await self._get("/v3/feed/market-data-feed/authorize")
        return str(data["authorized_redirect_uri"])

    async def authorize_portfolio_feed(self, update_types: Sequence[str]) -> str:
        data = await self._get("/v2/feed/portfolio-stream-feed/authorize", {"update_types": ",".join(update_types)})
        return str(data["authorized_redirect_uri"])

    # ------------------------------------------------------------------ #
    # Orders — deliberately not implemented in this phase
    # ------------------------------------------------------------------ #
    def _refuse_order_routing(self, action: str) -> LiveTradingDisabledError:
        if self.mode is not TradingMode.LIVE:
            reason = f"TRADING_MODE is {self.mode.value}"
        elif not hard_limits.LIVE_TRADING_AVAILABLE:
            reason = "LIVE order routing is not available in this build"
        else:  # pragma: no cover - unreachable until LIVE routing is implemented
            reason = "LIVE order routing is not implemented"
        log.critical("refused real order action", extra={"action": action, "reason": reason})
        return LiveTradingDisabledError(f"refusing to {action} a real Upstox order: {reason}")

    async def place_order(self, *_: Any, **__: Any) -> None:
        raise self._refuse_order_routing("place")

    async def modify_order(self, *_: Any, **__: Any) -> None:
        raise self._refuse_order_routing("modify")

    async def cancel_order(self, *_: Any, **__: Any) -> None:
        raise self._refuse_order_routing("cancel")
