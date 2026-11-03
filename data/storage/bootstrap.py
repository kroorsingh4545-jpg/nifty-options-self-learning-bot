"""Create/upgrade the database and sync strategy definitions from config."""

from __future__ import annotations

from collections.abc import Mapping

from config.settings import BotConfig, StrategyConfig
from core.clock import now_ist
from core.enums import StrategyStatus
from data.storage.database import Database
from data.storage.records import StrategyRecord
from data.storage.repositories import Repositories, StrategyRepository


def sync_strategies(repo: StrategyRepository, strategies: Mapping[str, StrategyConfig]) -> None:
    """Upsert each configured strategy.

    New strategies start ACTIVE if enabled, else DISABLED. For existing rows an
    intraday PAUSED/KILLED status is preserved; disabling in config always wins.
    """
    for sid, cfg in strategies.items():
        existing = repo.get(sid)
        if not cfg.enabled:
            status = StrategyStatus.DISABLED
        elif existing is None or existing.status is StrategyStatus.DISABLED:
            status = StrategyStatus.ACTIVE
        else:
            status = existing.status
        now = now_ist()
        repo.upsert(
            StrategyRecord(
                strategy_id=sid,
                name=cfg.name,
                version=cfg.version,
                allocated_capital=cfg.allocated_capital,
                enabled=cfg.enabled,
                status=status,
                config_json=cfg.model_dump(mode="json"),
                created_at=existing.created_at if existing else now,
                updated_at=now,
            )
        )


def initialize_database(cfg: BotConfig) -> Repositories:
    """Open the configured database, apply the schema, sync strategies."""
    db = Database(cfg.database_path)
    db.initialize()
    repos = Repositories(db, cfg.mode)
    sync_strategies(repos.strategies, cfg.strategies.strategies)
    return repos
