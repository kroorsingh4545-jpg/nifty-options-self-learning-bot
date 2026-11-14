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
