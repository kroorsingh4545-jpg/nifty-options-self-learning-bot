"""SQLite connection management and schema initialisation."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from importlib import resources
from pathlib import Path
from typing import Any

from core.clock import now_ist

SCHEMA_VERSION = 1

REQUIRED_TABLES = (
    "strategies",
    "orders",
    "fills",
    "positions",
    "trades",
    "daily_pnl",
    "risk_events",
    "feature_snapshots",
    "model_runs",
    "learning_events",
)


class DatabaseError(RuntimeError):
    pass


def load_schema_sql() -> str:
    return resources.files("data.storage").joinpath("schema.sql").read_text(encoding="utf-8")


class Database:
    """Thin, thread-safe wrapper around one SQLite connection.

    Foreign keys are always on. File databases use WAL journaling so the
    dashboard and read-only MCP inspection can read while the bot writes.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None

    @property
    def is_memory(self) -> bool:
        return self.path == ":memory:"

    def connect(self) -> sqlite3.Connection:
        with self._lock:
            if self._conn is None:
                if not self.is_memory:
                    Path(self.path).parent.mkdir(parents=True, exist_ok=True)
                conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA foreign_keys = ON")
                conn.execute("PRAGMA busy_timeout = 5000")
                if not self.is_memory:
                    conn.execute("PRAGMA journal_mode = WAL")
                    conn.execute("PRAGMA synchronous = NORMAL")
                self._conn = conn
            return self._conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def __enter__(self) -> Database:
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """``BEGIN IMMEDIATE`` ... ``COMMIT`` (or ``ROLLBACK`` on error)."""
        with self._lock:
            conn = self.connect()
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")

    def execute(self, sql: str, params: Any = ()) -> sqlite3.Cursor:
        with self._lock:
            return self.connect().execute(sql, params)

    def query(self, sql: str, params: Any = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.connect().execute(sql, params).fetchall()

    def query_one(self, sql: str, params: Any = ()) -> sqlite3.Row | None:
        with self._lock:
            row: sqlite3.Row | None = self.connect().execute(sql, params).fetchone()
            return row

    # ------------------------------------------------------------------ #
    def current_schema_version(self) -> int | None:
        exists = self.query_one("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'")
        if not exists:
            return None
        row = self.query_one("SELECT MAX(version) AS v FROM schema_version")
        return None if row is None or row["v"] is None else int(row["v"])

    def initialize(self) -> None:
        """Create all tables, indexes, views and triggers (idempotent)."""
        with self._lock:
            existing = self.current_schema_version()
            if existing is not None and existing > SCHEMA_VERSION:
                raise DatabaseError(
                    f"database schema v{existing} is newer than this code (v{SCHEMA_VERSION}); refusing to start"
                )
            conn = self.connect()
            conn.executescript(load_schema_sql())
            conn.execute(
                "INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES (?, ?)",
                (SCHEMA_VERSION, now_ist().isoformat()),
            )
            fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
            if fk != 1:
                raise DatabaseError("SQLite foreign key enforcement is not enabled")
            missing = set(REQUIRED_TABLES) - set(self.tables())
            if missing:
                raise DatabaseError(f"schema initialisation incomplete; missing tables: {sorted(missing)}")

    def tables(self) -> list[str]:
        rows = self.query(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
        return [r["name"] for r in rows]

    def schema_ddl(self) -> str:
        rows = self.query(
            "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' "
            "ORDER BY CASE type WHEN 'table' THEN 0 WHEN 'index' THEN 1 WHEN 'view' THEN 2 ELSE 3 END, name"
        )
        return ";\n\n".join(r["sql"] for r in rows) + ";\n"
