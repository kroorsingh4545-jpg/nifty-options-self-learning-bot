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
