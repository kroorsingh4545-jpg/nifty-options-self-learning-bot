"""Research settings (Pydantic), loaded from ``research/research.yaml``.

Separate from ``config/``: research never reads or writes the bot's YAML
files, and nothing here can change a trading setting.
"""

from __future__ import annotations

from datetime import date, time
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

DEFAULT_CONFIG_FILE = Path(__file__).with_name("research.yaml")


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class InstrumentKeys(_Frozen):
    nifty_index_key: str = "NSE_INDEX|Nifty 50"
    india_vix_key: str = "NSE_INDEX|India VIX"


class BlockedWindow(_Frozen):
    start: time = time(8, 30)
    end: time = time(15, 40)

    @model_validator(mode="after")
    def _order(self) -> BlockedWindow:
        if self.end <= self.start:
            raise ValueError("blocked_window.end must be after start")
        return self


class HistorySection(_Frozen):
    daily_from: date = date(2000, 1, 1)
    minute_from: date = date(2022, 1, 1)
    minute_interval: int = Field(default=1, ge=1, le=15)  # Upstox: 1 month per request up to 15 minutes
    daily_chunk_years: int = Field(default=5, ge=1, le=9)  # Upstox: at most 10 years per request
    blocked_window: BlockedWindow = BlockedWindow()


class ChainSection(_Frozen):
    nearest_expiries: int = Field(default=3, ge=1, le=6)
    include_next_monthly: bool = True
    # In-session snapshot times (IST) for `chain-schedule --during-session` (about 5 calls each).
    schedule: tuple[time, ...] = (time(9, 20), time(11, 0), time(13, 30), time(15, 20))

    @model_validator(mode="after")
    def _schedule(self) -> ChainSection:
        if list(self.schedule) != sorted(set(self.schedule)) or len(self.schedule) > 8:
            raise ValueError("chain.schedule must be at most 8 distinct times in increasing order")
        return self


class DataQualitySection(_Frozen):
    """Review thresholds for `data-quality` (never trading rules)."""

    quote_interval_seconds: float = Field(default=5.0, gt=0.0, le=60.0)  # the bot's market_recording interval
    min_completeness: float = Field(default=0.99, gt=0.0, le=1.0)
    max_gap_seconds: float = Field(default=30.0, gt=0.0)
    max_missing_share: float = Field(default=0.01, ge=0.0, le=1.0)
    abnormal_spread_pct: float = Field(default=5.0, gt=0.0)
    max_abnormal_spread_share: float = Field(default=0.01, ge=0.0, le=1.0)
    stale_trade_seconds: float = Field(default=60.0, gt=0.0)
    max_clock_offset_seconds: float = Field(default=2.0, gt=0.0)
    edge_tolerance_seconds: float = Field(default=120.0, gt=0.0)
    clean_sessions_required: int = Field(default=10, ge=1, le=60)  # D1 exit criterion


class SmokeSection(_Frozen):
    iv_check_rate: float = Field(default=0.065, ge=0.0, le=0.2)


class ForecastSection(_Frozen):
    """Forecast-vs-reality settings. Regime cut-offs here are PROVISIONAL
    reporting buckets for R2 (data-driven terciles / a 1-sigma trend test);
    the validated regime engine is R3."""

    method_version: str = "r2.1"
    rv_window: int = Field(default=20, ge=5, le=120)
    ewma_lambda: float = Field(default=0.94, gt=0.5, lt=1.0)
    har_min_train: int = Field(default=500, ge=250)
    har_refit_every: int = Field(default=21, ge=1, le=252)
    regime_lookback: int = Field(default=252, ge=60, le=1260)
    vol_terciles: tuple[float, float] = (1 / 3, 2 / 3)
    trend_window: int = Field(default=20, ge=5, le=120)
    trend_z: float = Field(default=1.0, gt=0.0, le=3.0)
    min_intraday_minutes: int = Field(default=360, ge=60, le=375)
    intraday_step_minutes: int = Field(default=5, ge=1, le=30)
    reliable_open_from: date = date(2011, 1, 1)  # NIFTY daily opens before this often equal the previous close
    block_bootstrap: int = Field(default=20, ge=5, le=250)
    # A next-session forecast counts as recorded LIVE only when it was made at least this many minutes before
    # the 09:15 open, so a PC clock that runs a little fast (this laptop: about 7 s, NTP stopped) can never
    # label a forecast made after the open as a real pre-session prediction.
    live_margin_minutes: int = Field(default=5, ge=1, le=60)
    # Today's daily candle from Upstox is accepted as final only after this time (IST)
    daily_final_after: time = time(15, 40)

    @model_validator(mode="after")
    def _terciles(self) -> ForecastSection:
        lo, hi = self.vol_terciles
        if not 0.0 < lo < hi < 1.0:
            raise ValueError("vol_terciles must be 0 < low < high < 1")
        return self


class VolatilityEvidenceSection(_Frozen):
    """Volatility evidence (D3): option IV and Greeks at each signal, the decay and
    costs of a hold against the expected move, and IV vs realised variance on
    matched windows. Research-only settings; nothing here changes how the bot trades."""

    evidence_version: str = Field(default="d3.1", min_length=1)  # bump when the evidence maths changes
    risk_free_rate: float = Field(default=0.065, ge=0.0, le=0.2)  # same as the ML feature pipeline
    dividend_yield: float = Field(default=0.0, ge=0.0, le=0.1)  # NIFTY ~1 %; 0 keeps parity with options/pricing
    horizon_minutes: int = Field(default=30, ge=1, le=375)  # the hold the ex-ante evidence assumes
    default_overnight_share: float = Field(default=0.35, ge=0.0, lt=1.0)  # R2: ~35 % of variance overnight
    overnight_lookback_sessions: int = Field(default=252, ge=20, le=2520)  # trailing, point in time
    weekend_variance_multiple: float = Field(default=1.9, ge=1.0, le=5.0)  # R2: weekend ~1.9 normal days
    quote_max_age_seconds: float = Field(default=10.0, gt=0.0)  # option quote used for IV at entry / exit
    intraday_checkpoints: tuple[time, ...] = (time(10, 0), time(11, 30), time(13, 0), time(14, 0))
    intraday_horizons: tuple[int, ...] = (15, 30, 60)

    @model_validator(mode="after")
    def _lists(self) -> VolatilityEvidenceSection:
        if not self.intraday_checkpoints or not self.intraday_horizons or min(self.intraday_horizons) < 1:
            raise ValueError("intraday_checkpoints and intraday_horizons (minutes >= 1) must not be empty")
        return self


class RegimeSection(_Frozen):
    """Regime (R3) and Momentum Pulse (decision-engine D4). Window lengths and the
    validation rule are fixed in advance; Pulse cutoffs are fitted on training years."""

    regime_version: str = Field(default="d4.1", min_length=1)
    checkpoints: tuple[time, ...] = (time(10, 0), time(10, 30), time(11, 0), time(11, 30), time(12, 0),
                                     time(12, 30), time(13, 0), time(13, 30), time(14, 0), time(14, 30))
    horizon_minutes: int = Field(default=30, ge=5, le=120)
    lookback_bars: int = Field(default=20, ge=5, le=120)
    adx_period: int = Field(default=10, ge=3, le=50)
    atr_short: int = Field(default=10, ge=2, le=60)
    atr_long: int = Field(default=20, ge=3, le=120)
    required_years: int = Field(default=3, ge=1, le=10)  # KEEP: right sign in this many test years ...
    min_group: int = Field(default=30, ge=5)  # ... each with at least this many samples per group
    min_train: int = Field(default=200, ge=50)
    n_boot: int = Field(default=1000, ge=200, le=20000)
    vix_lookback_sessions: int = Field(default=252, ge=60, le=1260)
    trend_sessions: int = Field(default=20, ge=5, le=120)

    @model_validator(mode="after")
    def _check(self) -> RegimeSection:
        if not self.checkpoints or self.atr_short >= self.atr_long:
            raise ValueError("regime.checkpoints must not be empty and atr_short < atr_long")
        return self


class ShadowSection(_Frozen):
    """Shadow simulation (D2). Trade rules come from the bot configuration; these
    are research-only settings."""

    sim_version: str = Field(default="d2.1", min_length=1)  # bump when the simulator changes
    option_tick_size: float = Field(default=0.05, gt=0.0)  # NIFTY options (Upstox instrument list)
    entry_tolerance_ticks: float = Field(default=1.0, ge=0.0)  # calibration against real PAPER trades
    exit_tolerance_ticks: float = Field(default=2.0, ge=0.0)
    spot_max_age_seconds: float = Field(default=60.0, gt=0.0)  # NIFTY quote used for the ATM strike


class ExpiryRule(_Frozen):
    effective_from: date
    weekday: int = Field(ge=0, le=4)
    source: str = Field(min_length=3)


class ResearchConfig(_Frozen):
    store_path: Path = Path("data/research.db")
    instruments: InstrumentKeys = InstrumentKeys()
    # Fraction of the bot's effective limits research may use in its own process. Capped at 0.1 so the bot
    # and research together stay at or below 88-90 % of Upstox's official limits even if both saturate.
    rate_share: float = Field(default=0.1, gt=0.0, le=0.1)
    # How long a research request may wait for a slot of its (small) budget before it fails.
    # Research is not latency-sensitive; the bot keeps its own queue timeout.
    queue_timeout_seconds: float = Field(default=120.0, ge=30.0, le=600.0)
    history: HistorySection = HistorySection()
    chain: ChainSection = ChainSection()
    smoke: SmokeSection = SmokeSection()
    forecast: ForecastSection = ForecastSection()
    data_quality: DataQualitySection = DataQualitySection()
    shadow: ShadowSection = ShadowSection()
    volatility: VolatilityEvidenceSection = VolatilityEvidenceSection()
    regime: RegimeSection = RegimeSection()
    weekly_expiry_rules: tuple[ExpiryRule, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _rules_sorted(self) -> ResearchConfig:
        dates = [r.effective_from for r in self.weekly_expiry_rules]
        if dates != sorted(dates) or len(set(dates)) != len(dates):
            raise ValueError("weekly_expiry_rules must be in strictly increasing effective_from order")
        if self.store_path.suffix != ".db":
            raise ValueError("store_path must be a .db file (kept out of git by .gitignore)")
        return self

    def resolve_store_path(self, project_root: Path) -> Path:
        return self.store_path if self.store_path.is_absolute() else project_root / self.store_path


def load_research_config(path: Path | None = None) -> ResearchConfig:
    raw: Any = yaml.safe_load((path or DEFAULT_CONFIG_FILE).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("research.yaml must be a mapping")
    return ResearchConfig.model_validate(raw)


__all__ = ["DEFAULT_CONFIG_FILE", "ExpiryRule", "ResearchConfig", "load_research_config"]
