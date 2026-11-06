"""Shared pytest fixtures.

Tests never read the developer's real ``.env`` and never touch
``data/trading.db``: the environment is scrubbed, ``load_config()`` has no
default ``.env`` (and refuses the real one even when passed explicitly), and
every database is in-memory or under ``tmp_path``. The CLI's PC-clock
pre-flight is replaced by a fake reading so no test calls Upstox.
"""

from __future__ import annotations

import shutil
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from config import loader
from config.loader import DEFAULT_CONFIG_DIR
from core.clock import ClockOffset, now_ist
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
    "UPSTOX_TOKEN_TYPE",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "DASHBOARD_USERNAME",
    "DASHBOARD_PASSWORD",
    "DATABASE_PATH",
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REAL_ENV_FILE = (PROJECT_ROOT / ".env").resolve()
_real_load_env = loader.load_env


def _guarded_load_env(env_file: Path | None) -> object:
    if env_file is not None and Path(env_file).resolve() == REAL_ENV_FILE:
        raise AssertionError("a test tried to read the developer's real .env; pass a tmp env_file instead")
    return _real_load_env(env_file)


async def _fake_measure_clock(cfg: object) -> ClockOffset:
    return ClockOffset(0.0, 0.5, "test (no network)", now_ist())


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for var in ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(loader, "DEFAULT_ENV_FILE", None)
    monkeypatch.setattr(loader, "load_env", _guarded_load_env)
    cli = sys.modules.get("runtime.__main__")
    if cli is not None:  # imported by the test module: never let its clock pre-flight reach Upstox
        monkeypatch.setattr(cli, "measure_clock", _fake_measure_clock)
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
