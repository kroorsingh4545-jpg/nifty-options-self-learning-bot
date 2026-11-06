"""Shared pytest fixtures.

Tests never read the developer's real ``.env`` and never touch
``data/trading.db``: the environment is scrubbed and every database is
in-memory or under ``tmp_path``.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from config.loader import DEFAULT_CONFIG_DIR
from core.enums import TradingMode
from core.masking import DEFAULT_MASKER
from data.storage.database import Database
from data.storage.records import StrategyRecord
from data.storage.repositories import Repositories

ENV_VARS = (
    "TRADING_MODE",
    "UPSTOX_API_KEY",
    "UPSTOX_API_SECRET",
    "UPSTOX_ACCESS_TOKEN",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "DASHBOARD_USERNAME",
    "DASHBOARD_PASSWORD",
    "DATABASE_PATH",
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for var in ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    DEFAULT_MASKER.clear()
    yield
    DEFAULT_MASKER.clear()


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    """A writable copy of the repository's config YAML files."""
    dest = tmp_path / "config"
    dest.mkdir()
    for f in DEFAULT_CONFIG_DIR.glob("*.yaml"):
        shutil.copy(f, dest / f.name)
    return dest


@pytest.fixture
def edit_yaml(config_dir: Path) -> Callable[[str, Callable[[dict[str, Any]], None]], None]:
    """Mutate one YAML file in the temp config dir: edit_yaml(name, fn)."""

    def _edit(name: str, fn: Callable[[dict[str, Any]], None]) -> None:
        path = config_dir / name
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        fn(data)
        path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    return _edit


@pytest.fixture
def db() -> Iterator[Database]:
    database = Database(":memory:")
    database.initialize()
    yield database
    database.close()


def _seed(database: Database) -> None:
    repos = Repositories(database, TradingMode.PAPER)
    for sid in ("orb_options", "vwap_ema"):
        repos.strategies.upsert(
            StrategyRecord(strategy_id=sid, name=sid, version="0.1.0", allocated_capital=100_000, enabled=True)
        )


@pytest.fixture
def seeded_db(db: Database) -> Database:
    _seed(db)
    return db


@pytest.fixture
def paper(seeded_db: Database) -> Repositories:
    return Repositories(seeded_db, TradingMode.PAPER)


@pytest.fixture
def live(seeded_db: Database) -> Repositories:
    return Repositories(seeded_db, TradingMode.LIVE)
