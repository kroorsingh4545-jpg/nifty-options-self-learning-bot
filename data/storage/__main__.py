"""Database CLI.

    python -m data.storage init      # create/upgrade schema, sync strategies
    python -m data.storage schema    # print the live schema DDL
"""

from __future__ import annotations

import argparse
import sys

from config.loader import ConfigError, load_config
from data.storage.bootstrap import initialize_database
from monitoring.json_logger import configure_from_config, get_logger


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m data.storage")
    parser.add_argument("command", choices=["init", "schema"])
    args = parser.parse_args(argv)

    try:
        cfg = load_config()
    except ConfigError as exc:
        print(f"CONFIG ERROR: {exc}", file=sys.stderr)
        return 2
    configure_from_config(cfg)
    log = get_logger("data.storage")

    repos = initialize_database(cfg)
    try:
        if args.command == "init":
            tables = repos.db.tables()
            log.info("database initialised", extra={"path": str(cfg.database_path), "tables": tables})
            print(f"Database ready at {cfg.database_path}")
            print("Tables: " + ", ".join(tables))
            print("Strategies: " + ", ".join(f"{s.strategy_id} ({s.status})" for s in repos.strategies.list_all()))
        else:
            print(repos.db.schema_ddl())
    finally:
        repos.db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
