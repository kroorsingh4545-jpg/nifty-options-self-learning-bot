"""Isolation guards for the research layer (research/, volatility/).

Research may read market data and the trading database. It may never reach
order code, the execution / risk / runtime services, the paper broker, the
learning loop's promotion machinery, the trading database's write path, the
bot's config files, or the network except through the Upstox adapter built in
its composition root. The trading bot must never depend on research.
"""

from __future__ import annotations

import ast
import re
import sqlite3
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RESEARCH_PACKAGES = ("research", "volatility")
COMPOSITION_ROOT = Path("research/__main__.py")
BOT_PACKAGES = ("backtest", "broker", "config", "core", "dashboard", "data", "execution", "learning", "ml",
                "monitoring", "options", "risk", "runtime", "strategies")

FORBIDDEN_TOP_LEVELS = {"execution", "risk", "runtime", "dashboard"}
FORBIDDEN_MODULE_PREFIXES = (
    "broker.paper_broker", "broker.factory", "broker.upstox_websocket", "broker.upstox_mapping",
    "learning.mutation_engine", "learning.promotion_gate", "learning.deployment_controller", "learning.loop",
    "data.storage",  # the trading database's repositories / migrations
    "config.hard_limits",
)
BROKER_ALLOWED = {"broker.errors", "broker.charges"}
BROKER_ALLOWED_IN_ROOT = BROKER_ALLOWED | {"broker.upstox_rest", "broker.upstox_rate_limiter"}
NETWORK_CLIENTS = {"requests", "httpx", "aiohttp", "websockets", "websocket", "urllib3", "upstox_client", "socket",
                   "urllib.request", "http.client", "ftplib", "smtplib"}
ORDER_OR_ACCOUNT_CALL = re.compile(
    r"\.(place_order|modify_order|cancel_order|get_funds|get_positions|get_order_book|get_orders|"
    r"authorize_portfolio_feed)\(")
BOT_CONFIG_FILES = ("risk_limits.yaml", "strategy_defaults.yaml", "settings.yaml", "hard_limits")
FILE_MUTATORS = {"write_text", "write_bytes", "unlink", "rmdir", "rename", "chmod", "touch", "symlink_to"}
SQLITE_CONNECT_ALLOWED = {Path("research/store/store.py"), Path("research/trading_readonly.py")}


def _files() -> list[Path]:
    out: list[Path] = []
    for pkg in RESEARCH_PACKAGES:
        if (PROJECT_ROOT / pkg).is_dir():
            out += sorted((PROJECT_ROOT / pkg).rglob("*.py"))
    return out


def _rel(path: Path) -> Path:
    return path.relative_to(PROJECT_ROOT)


def _modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, f"{_rel(path)}: use absolute imports"
            if node.module:
                out.add(node.module)
                out.update(f"{node.module}.{a.name}" for a in node.names)
    return out


def _matches(mod: str, prefix: str) -> bool:
    return mod == prefix or mod.startswith(prefix + ".")


RESEARCH_FILES = _files()


def test_research_package_exists_and_is_scanned() -> None:
    assert RESEARCH_FILES, "no research files found"
    assert PROJECT_ROOT / COMPOSITION_ROOT in RESEARCH_FILES


@pytest.mark.parametrize("path", RESEARCH_FILES, ids=lambda p: _rel(p).as_posix())
def test_research_imports_stay_inside_the_allowed_surface(path: Path) -> None:
    mods = _modules(path)
    rel = _rel(path)
    tops = {m.split(".")[0] for m in mods}
    assert not tops & FORBIDDEN_TOP_LEVELS, f"{rel} imports {tops & FORBIDDEN_TOP_LEVELS}"
    bad = sorted(m for m in mods for p in FORBIDDEN_MODULE_PREFIXES if _matches(m, p))
    assert not bad, f"{rel} imports {bad}"
    allowed_broker = BROKER_ALLOWED_IN_ROOT if rel == COMPOSITION_ROOT else BROKER_ALLOWED
    broker = {m for m in mods if _matches(m, "broker") and m != "broker"}
    assert all(any(_matches(m, a) for a in allowed_broker) for m in broker), f"{rel} imports {broker}"
    strategies = {m for m in mods if _matches(m, "strategies")}
    assert all(_matches(m, "strategies.indicators") for m in strategies), f"{rel} imports {strategies}"
    if rel != COMPOSITION_ROOT:  # only the root reads the bot configuration (read-only)
        assert not {m for m in mods if _matches(m, "config")}, f"{rel} imports the bot config"


@pytest.mark.parametrize("path", RESEARCH_FILES, ids=lambda p: _rel(p).as_posix())
def test_research_has_no_network_client(path: Path) -> None:
    mods = _modules(path)
    used = {m for m in mods for c in NETWORK_CLIENTS if _matches(m, c)}
    assert not used, f"{_rel(path)} imports network client(s) {used}"


@pytest.mark.parametrize("path", RESEARCH_FILES, ids=lambda p: _rel(p).as_posix())
def test_research_never_calls_order_or_account_methods(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert not ORDER_OR_ACCOUNT_CALL.search(text), f"{_rel(path)} calls an order/account method"


@pytest.mark.parametrize("path", RESEARCH_FILES, ids=lambda p: _rel(p).as_posix())
def test_research_never_touches_bot_config_files_or_environment(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    for name in BOT_CONFIG_FILES:
        assert name not in text, f"{_rel(path)} references {name}"
    assert not re.search(r"\bos\.(environ|getenv)\b", text), f"{_rel(path)} reads os.environ"


@pytest.mark.parametrize("path", RESEARCH_FILES, ids=lambda p: _rel(p).as_posix())
def test_research_writes_files_only_through_its_own_database(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    rel = _rel(path)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        assert not (isinstance(f, ast.Name) and f.id == "open"), f"{rel} calls open()"
        if isinstance(f, ast.Attribute):
            assert f.attr not in FILE_MUTATORS, f"{rel} calls .{f.attr}()"
            if f.attr == "connect" and isinstance(f.value, ast.Name) and f.value.id == "sqlite3":
                assert rel in SQLITE_CONNECT_ALLOWED, f"{rel} opens a database directly"
    assert "shutil" not in _modules(path)


def test_trading_database_is_opened_read_only_only() -> None:
    text = (PROJECT_ROOT / "research" / "trading_readonly.py").read_text(encoding="utf-8")
    assert "?mode=ro" in text and "PRAGMA query_only = ON" in text and "uri=True" in text


def test_read_only_trading_connection_refuses_writes(tmp_path: Path) -> None:
    from research.trading_readonly import open_trading_db_readonly

    db = tmp_path / "trading.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE option_quotes (id INTEGER PRIMARY KEY, x TEXT)")
    con.commit()
    con.close()
    ro = open_trading_db_readonly(db)
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("INSERT INTO option_quotes(x) VALUES ('y')")
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("CREATE TABLE t (a)")
    ro.close()


def test_research_store_refuses_the_trading_database(tmp_path: Path) -> None:
    from research.store import ResearchStore, ResearchStoreError

    trading = tmp_path / "trading.db"
    with pytest.raises(ResearchStoreError, match="trading database"):
        ResearchStore.open(tmp_path / "sub" / ".." / "trading.db", forbidden=[trading])
    assert not trading.exists()


def test_read_only_source_exposes_only_market_data_reads() -> None:
    from research.sources import ReadOnlySource

    public = {n for n in dir(ReadOnlySource) if not n.startswith("_")}
    assert public == {"get_daily_candles", "get_historical_candles", "get_option_contracts", "get_option_chain"}


def test_research_rate_share_is_below_the_bot_budget() -> None:
    from config.loader import load_config
    from research.__main__ import research_limits
    from research.config import load_research_config

    cfg = load_config(use_default_env_file=False)
    rcfg = load_research_config()
    assert 0 < rcfg.rate_share <= 0.5
    mine, bot = research_limits(cfg, rcfg.rate_share), cfg.risk.rate_limits.effective
    for cat in ("standard_api", "order_api"):
        for window in ("per_second", "per_minute", "per_30_minutes"):
            assert getattr(getattr(mine, cat), window) <= getattr(getattr(bot, cat), window) * 0.5


@pytest.mark.parametrize("pkg", BOT_PACKAGES)
def test_the_trading_bot_never_depends_on_research(pkg: str) -> None:
    for path in sorted((PROJECT_ROOT / pkg).rglob("*.py")):
        tops = {m.split(".")[0] for m in _modules_loose(path)}
        assert not tops & set(RESEARCH_PACKAGES), f"{_rel(path)} imports research code"


def _modules_loose(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            out.add(node.module)
    return out
