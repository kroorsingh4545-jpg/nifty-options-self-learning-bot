"""Pydantic configuration models.

All models are frozen (read-only after load) and forbid unknown keys, so a
typo in YAML fails loudly instead of being silently ignored. Risk values are
checked against ``config.hard_limits`` and may only be made stricter.
"""

from __future__ import annotations

import math
from datetime import datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    NonNegativeFloat,
    PositiveFloat,
    PositiveInt,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

from config import hard_limits as hl
from core.enums import AutonomyLevel, TradingMode
from core.ids import STRATEGY_ID_PATTERN

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _parse_hhmm(value: Any) -> time:
    if isinstance(value, time):
        return value
    if not isinstance(value, str):
        raise ValueError("time must be a quoted 'HH:MM' string (unquoted YAML times parse as numbers)")
    try:
        return datetime.strptime(value.strip(), "%H:%M").time()
    except ValueError as exc:
        raise ValueError(f"invalid time {value!r}; expected 'HH:MM'") from exc


HHMM = Annotated[time, BeforeValidator(_parse_hhmm)]


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --------------------------------------------------------------------------- #
# settings.yaml
# --------------------------------------------------------------------------- #


class AppSection(_Frozen):
    name: str = "nifty-options-self-learning-bot"
    timezone: Literal["Asia/Kolkata"] = "Asia/Kolkata"
    autonomy_level: AutonomyLevel = AutonomyLevel.PAPER_AUTO_IMPROVE


class MarketSession(_Frozen):
    market_open: HHMM = time(9, 15)
    market_close: HHMM = time(15, 30)

    @model_validator(mode="after")
    def _order(self) -> MarketSession:
        if not self.market_open < self.market_close:
            raise ValueError("market_open must be before market_close")
        return self


class DatabaseSection(_Frozen):
    path: Path = Path("data/trading.db")


class LoggingSection(_Frozen):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    directory: Path = Path("logs")
    file_name: str = Field(default="bot.jsonl", min_length=1)
    console: bool = True
    max_bytes: PositiveInt = 10 * 1024 * 1024
    backup_count: int = Field(default=10, ge=0)


class CircuitBreakerSection(_Frozen):
    base_cooldown_seconds: PositiveFloat = 1.0
    max_cooldown_seconds: PositiveFloat = 60.0
    max_consecutive_429: PositiveInt = 5


class BrokerSection(_Frozen):
    rest_base_url: Literal["https://api.upstox.com"] = "https://api.upstox.com"
    connect_timeout_seconds: PositiveFloat = 5.0
    request_timeout_seconds: PositiveFloat = 10.0
    max_retries: int = Field(default=3, ge=0, le=5)
    backoff_base_seconds: PositiveFloat = 0.5
    backoff_max_seconds: PositiveFloat = 30.0
    queue_timeout_seconds: PositiveFloat = 30.0
    debounce_seconds: NonNegativeFloat = 1.0
    token_expiry_time: HHMM = time(3, 30)
    circuit_breaker: CircuitBreakerSection = CircuitBreakerSection()


MarketDataMode = Literal["ltpc", "full", "option_greeks", "full_d30"]
PortfolioUpdateType = Literal["order", "position", "holding", "gtt_order"]


class WebSocketSection(_Frozen):
    reconnect_base_seconds: PositiveFloat = 1.0
    reconnect_max_seconds: PositiveFloat = 60.0
    max_reconnect_attempts: PositiveInt = 20
    heartbeat_interval_seconds: PositiveFloat = 15.0
    heartbeat_timeout_seconds: PositiveFloat = 10.0
    stale_after_seconds: PositiveFloat = 10.0
    subscribe_chunk_size: int = Field(default=100, ge=1, le=500)
    market_data_mode: MarketDataMode = "full"
    portfolio_update_types: tuple[PortfolioUpdateType, ...] = ("order", "position")


class PaperBrokerSection(_Frozen):
    starting_funds: PositiveFloat = 200_000.0
    slippage_ticks: int = Field(default=1, ge=0, le=20)
    max_quote_age_seconds: PositiveFloat = 5.0
    simulate_partial_fills: bool = True
    allow_short_options: Literal[False] = False


class CostsSection(_Frozen):
    brokerage_per_order: NonNegativeFloat = 20.0
    stt_sell_pct: NonNegativeFloat = 0.15
    exchange_txn_pct: NonNegativeFloat = 0.03503
    sebi_fee_per_crore: NonNegativeFloat = 10.0
    stamp_duty_buy_pct: NonNegativeFloat = 0.003
    gst_pct: NonNegativeFloat = 18.0


class AppSettings(_Frozen):
    app: AppSection = AppSection()
    market_session: MarketSession = MarketSession()
    database: DatabaseSection = DatabaseSection()
    broker: BrokerSection = BrokerSection()
    websocket: WebSocketSection = WebSocketSection()
    paper_broker: PaperBrokerSection = PaperBrokerSection()
    costs: CostsSection = CostsSection()
    logging: LoggingSection = LoggingSection()


# --------------------------------------------------------------------------- #
# risk_limits.yaml
# --------------------------------------------------------------------------- #


class CapitalLimits(_Frozen):
    total_capital: PositiveFloat
    per_strategy_capital: PositiveFloat

    @model_validator(mode="after")
    def _check(self) -> CapitalLimits:
        if self.per_strategy_capital > self.total_capital:
            raise ValueError("per_strategy_capital cannot exceed total_capital")
        return self


class PerTradeLimits(_Frozen):
    max_risk_per_trade: PositiveFloat = Field(le=hl.MAX_RISK_PER_TRADE_RS)


class PerStrategyLimits(_Frozen):
    max_daily_loss: PositiveFloat = Field(le=hl.MAX_DAILY_LOSS_PER_STRATEGY_RS)
    max_open_trades: PositiveInt = Field(le=hl.MAX_OPEN_TRADES_PER_STRATEGY)
    max_trades_per_day: PositiveInt = Field(le=hl.MAX_TRADES_PER_STRATEGY_PER_DAY)
    loss_cooldown_minutes: int = Field(ge=hl.MIN_LOSS_COOLDOWN_MINUTES)
    max_consecutive_losses: PositiveInt = Field(le=hl.MAX_CONSECUTIVE_LOSSES)


BreachAction = Literal[
    "CANCEL_PENDING_ORDERS",
    "EXIT_ALL_POSITIONS",
    "DISABLE_ALL_STRATEGIES",
    "TRIGGER_KILL_SWITCH",
    "SEND_ALERTS",
]


class GlobalLimits(_Frozen):
    max_daily_loss: PositiveFloat = Field(le=hl.MAX_GLOBAL_DAILY_LOSS_RS)
    max_open_trades: PositiveInt = Field(le=hl.MAX_TOTAL_OPEN_TRADES)
    on_breach: tuple[BreachAction, ...]

    @field_validator("on_breach")
    @classmethod
    def _all_actions(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        missing = hl.REQUIRED_GLOBAL_BREACH_ACTIONS - set(v)
        if missing:
            raise ValueError(f"on_breach is missing required actions: {sorted(missing)}")
        return v


class TimeRules(_Frozen):
    no_new_entries_after: HHMM
    square_off_start: HHMM
    square_off_end: HHMM

    @model_validator(mode="after")
    def _check(self) -> TimeRules:
        if self.no_new_entries_after > hl.LATEST_NEW_ENTRY_TIME:
            raise ValueError(f"no_new_entries_after cannot be later than {hl.LATEST_NEW_ENTRY_TIME:%H:%M}")
        if self.square_off_start > hl.LATEST_SQUARE_OFF_START:
            raise ValueError(f"square_off_start cannot be later than {hl.LATEST_SQUARE_OFF_START:%H:%M}")
        if self.square_off_end > hl.LATEST_SQUARE_OFF_END:
            raise ValueError(f"square_off_end cannot be later than {hl.LATEST_SQUARE_OFF_END:%H:%M}")
        if not self.no_new_entries_after <= self.square_off_start < self.square_off_end:
            raise ValueError("require no_new_entries_after <= square_off_start < square_off_end")
        return self


class WindowLimits(_Frozen):
    per_second: PositiveInt
    per_minute: PositiveInt
    per_30_minutes: PositiveInt


class EffectiveRateLimits(_Frozen):
    order_api: WindowLimits
    standard_api: WindowLimits


class RateLimits(_Frozen):
    safety_buffer: float = Field(ge=hl.MIN_RATE_LIMIT_SAFETY_BUFFER, lt=1.0)
    order_api: WindowLimits
    standard_api: WindowLimits

    @model_validator(mode="after")
    def _not_above_official(self) -> RateLimits:
        for category in ("order_api", "standard_api"):
            configured: WindowLimits = getattr(self, category)
            official = hl.UPSTOX_OFFICIAL_LIMITS[category]
            for window, ceiling in official.items():
                if getattr(configured, window) > ceiling:
                    raise ValueError(f"{category}.{window} exceeds the official Upstox limit {ceiling}")
        return self

    def _apply(self, limits: WindowLimits) -> WindowLimits:
        factor = Decimal(1) - Decimal(str(self.safety_buffer))

        def eff(x: int) -> int:
            return max(1, math.floor(Decimal(x) * factor))

        return WindowLimits(
            per_second=eff(limits.per_second),
            per_minute=eff(limits.per_minute),
            per_30_minutes=eff(limits.per_30_minutes),
        )

    @property
    def effective(self) -> EffectiveRateLimits:
        """Limits the central rate limiter must enforce (official minus buffer)."""
        return EffectiveRateLimits(order_api=self._apply(self.order_api), standard_api=self._apply(self.standard_api))


class RiskLimits(_Frozen):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    capital: CapitalLimits
    per_trade: PerTradeLimits
    per_strategy: PerStrategyLimits
    global_: GlobalLimits = Field(alias="global")
    time_rules: TimeRules
    rate_limits: RateLimits

    @model_validator(mode="after")
    def _relative_limits(self) -> RiskLimits:
        cap = self.capital
        if self.per_trade.max_risk_per_trade > cap.per_strategy_capital * hl.MAX_RISK_PER_TRADE_PCT:
            raise ValueError("max_risk_per_trade exceeds 2% of per_strategy_capital")
        if self.per_strategy.max_daily_loss > cap.per_strategy_capital * hl.MAX_DAILY_LOSS_PER_STRATEGY_PCT:
            raise ValueError("per_strategy.max_daily_loss exceeds 5% of per_strategy_capital")
        if self.global_.max_daily_loss > cap.total_capital * hl.MAX_GLOBAL_DAILY_LOSS_PCT:
            raise ValueError("global.max_daily_loss exceeds 5% of total_capital")
        if self.per_trade.max_risk_per_trade > self.per_strategy.max_daily_loss:
            raise ValueError("max_risk_per_trade cannot exceed per_strategy.max_daily_loss")
        if self.per_strategy.max_open_trades > self.global_.max_open_trades:
            raise ValueError("per_strategy.max_open_trades cannot exceed global.max_open_trades")
        return self


# --------------------------------------------------------------------------- #
# strategy_defaults.yaml
# --------------------------------------------------------------------------- #


class EntryWindow(_Frozen):
    start: HHMM
    end: HHMM

    @model_validator(mode="after")
    def _order(self) -> EntryWindow:
        if not self.start < self.end:
            raise ValueError("entry_window.start must be before entry_window.end")
        return self


class LiquidityFilter(_Frozen):
    min_volume: int = Field(ge=0)
    min_oi: int = Field(ge=0)
    max_spread_pct: PositiveFloat = Field(le=10)


class StrikeSelection(_Frozen):
    mode: Literal["ATM", "ITM", "OTM"] = "ATM"
    offset_steps: int = Field(default=0, ge=0, le=5)
    strike_step: PositiveInt = 50
    min_premium: NonNegativeFloat = 0
    max_premium: PositiveFloat = 1000
    liquidity: LiquidityFilter

    @model_validator(mode="after")
    def _check(self) -> StrikeSelection:
        if self.mode == "ATM" and self.offset_steps != 0:
            raise ValueError("ATM strike selection requires offset_steps = 0")
        if self.min_premium >= self.max_premium:
            raise ValueError("min_premium must be below max_premium")
        return self


class SizingConfig(_Frozen):
    risk_per_trade: PositiveFloat
    # Can only reduce size relative to the risk budget, never increase it.
    size_multiplier: float = Field(default=1.0, gt=0, le=1.0)


class StopLossConfig(_Frozen):
    mode: Literal["PREMIUM_PERCENT", "POINTS"]
    value: PositiveFloat

    @model_validator(mode="after")
    def _check(self) -> StopLossConfig:
        if self.mode == "PREMIUM_PERCENT" and self.value > 50:
            raise ValueError("stop_loss PREMIUM_PERCENT cannot exceed 50")
        return self


class TakeProfitConfig(_Frozen):
    mode: Literal["R_MULTIPLE", "PREMIUM_PERCENT", "POINTS"]
    value: PositiveFloat


class TrailingConfig(_Frozen):
    enabled: bool = True
    mode: Literal["PREMIUM_PERCENT", "POINTS", "R_MULTIPLE", "STRUCTURAL"]
    activation: NonNegativeFloat
    step: PositiveFloat


class StrategyRiskConfig(_Frozen):
    max_daily_loss: PositiveFloat
    max_open_trades: PositiveInt
    max_trades_per_day: PositiveInt
    loss_cooldown_minutes: int = Field(ge=0)
    max_consecutive_losses: PositiveInt


class MLConfig(_Frozen):
    enabled: bool = False
    confidence_threshold: float = Field(default=0.60, ge=0.0, le=1.0)


class StrategyConfig(_Frozen):
    name: str = Field(min_length=1)
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    enabled: bool = False
    allocated_capital: PositiveFloat
    timeframe_minutes: Literal[1, 3, 5, 15]
    entry_window: EntryWindow
    params: dict[str, Any] = Field(default_factory=dict)
    strike_selection: StrikeSelection
    sizing: SizingConfig
    stop_loss: StopLossConfig
    take_profit: TakeProfitConfig
    trailing: TrailingConfig
    risk: StrategyRiskConfig
    ml: MLConfig = MLConfig()


class StrategyDefaults(_Frozen):
    strategies: dict[Annotated[str, Field(pattern=STRATEGY_ID_PATTERN)], StrategyConfig]

    @field_validator("strategies")
    @classmethod
    def _non_empty(cls, v: dict[str, StrategyConfig]) -> dict[str, StrategyConfig]:
        if not v:
            raise ValueError("at least one strategy must be configured")
        return v


# --------------------------------------------------------------------------- #
# .env
# --------------------------------------------------------------------------- #


def _normalise_mode(value: Any) -> Any:
    if isinstance(value, str):
        return value.strip().upper()
    return value


class EnvSettings(BaseSettings):
    """Secrets and mode from environment variables / ``.env``.

    Real environment variables override ``.env`` values. Empty values
    (``KEY=``) are treated as unset.
    """

    model_config = SettingsConfigDict(
        env_file=None,
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
        case_sensitive=False,
        frozen=True,
    )

    trading_mode: Annotated[TradingMode, BeforeValidator(_normalise_mode)] = TradingMode.PAPER
    upstox_api_key: SecretStr | None = None
    upstox_api_secret: SecretStr | None = None
    upstox_access_token: SecretStr | None = None
    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: str | None = None
    dashboard_username: str | None = None
    dashboard_password: SecretStr | None = None

    @model_validator(mode="after")
    def _dashboard_pair(self) -> EnvSettings:
        has_user = bool(self.dashboard_username)
        has_pw = self.dashboard_password is not None
        if has_user != has_pw:
            raise ValueError("DASHBOARD_USERNAME and DASHBOARD_PASSWORD must be set together")
        if self.dashboard_password is not None and len(self.dashboard_password.get_secret_value()) < 8:
            raise ValueError("DASHBOARD_PASSWORD must be at least 8 characters")
        return self

    @model_validator(mode="after")
    def _telegram_pair(self) -> EnvSettings:
        if (self.telegram_bot_token is None) != (not self.telegram_chat_id):
            raise ValueError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set together")
        return self

    def secret_values(self) -> list[str]:
        out: list[str] = []
        for field in (
            self.upstox_api_key,
            self.upstox_api_secret,
            self.upstox_access_token,
            self.telegram_bot_token,
            self.dashboard_password,
        ):
            if field is not None:
                out.append(field.get_secret_value())
        return out

    @property
    def upstox_credentials_present(self) -> bool:
        return self.upstox_api_key is not None and self.upstox_access_token is not None


# --------------------------------------------------------------------------- #
# Aggregate
# --------------------------------------------------------------------------- #


class BotConfig(_Frozen):
    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    project_root: Path
    settings: AppSettings
    risk: RiskLimits
    strategies: StrategyDefaults
    env: EnvSettings

    @property
    def mode(self) -> TradingMode:
        return self.env.trading_mode

    def resolve_path(self, path: Path) -> Path:
        return path if path.is_absolute() else (self.project_root / path)

    @property
    def database_path(self) -> Path:
        return self.resolve_path(self.settings.database.path)

    @property
    def log_directory(self) -> Path:
        return self.resolve_path(self.settings.logging.directory)

    @model_validator(mode="after")
    def _cross_validate(self) -> BotConfig:
        risk = self.risk
        session = self.settings.market_session
        total_allocated = 0.0
        for sid, s in self.strategies.strategies.items():
            p = f"strategies.{sid}"
            total_allocated += s.allocated_capital
            if s.allocated_capital > risk.capital.per_strategy_capital:
                raise ValueError(f"{p}.allocated_capital exceeds per_strategy_capital")
            if s.sizing.risk_per_trade > risk.per_trade.max_risk_per_trade:
                raise ValueError(f"{p}.sizing.risk_per_trade exceeds max_risk_per_trade")
            if s.sizing.risk_per_trade > s.allocated_capital * hl.MAX_RISK_PER_TRADE_PCT:
                raise ValueError(f"{p}.sizing.risk_per_trade exceeds 2% of allocated_capital")
            r, lim = s.risk, risk.per_strategy
            if r.max_daily_loss > lim.max_daily_loss:
                raise ValueError(f"{p}.risk.max_daily_loss exceeds per_strategy.max_daily_loss")
            if r.max_daily_loss > s.allocated_capital * hl.MAX_DAILY_LOSS_PER_STRATEGY_PCT:
                raise ValueError(f"{p}.risk.max_daily_loss exceeds 5% of allocated_capital")
            if r.max_open_trades > lim.max_open_trades:
                raise ValueError(f"{p}.risk.max_open_trades exceeds per_strategy.max_open_trades")
            if r.max_trades_per_day > lim.max_trades_per_day:
                raise ValueError(f"{p}.risk.max_trades_per_day exceeds per_strategy.max_trades_per_day")
            if r.loss_cooldown_minutes < lim.loss_cooldown_minutes:
                raise ValueError(f"{p}.risk.loss_cooldown_minutes is below per_strategy.loss_cooldown_minutes")
            if r.max_consecutive_losses > lim.max_consecutive_losses:
                raise ValueError(f"{p}.risk.max_consecutive_losses exceeds per_strategy.max_consecutive_losses")
            if s.entry_window.start < session.market_open:
                raise ValueError(f"{p}.entry_window.start is before market_open")
            if s.entry_window.end > risk.time_rules.no_new_entries_after:
                raise ValueError(f"{p}.entry_window.end is after no_new_entries_after")
        if total_allocated > risk.capital.total_capital:
            raise ValueError("sum of strategy allocated_capital exceeds total_capital")
        if risk.time_rules.square_off_end > session.market_close:
            raise ValueError("square_off_end is after market_close")
        return self
