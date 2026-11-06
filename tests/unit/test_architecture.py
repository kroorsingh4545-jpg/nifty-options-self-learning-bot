"""Architecture guards from the nifty-bot-architect Skill."""

from __future__ import annotations

import ast
import importlib
import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]

PACKAGES = (
    "backtest", "broker", "config", "core", "dashboard", "data", "data.storage", "execution",
    "learning", "ml", "monitoring", "options", "risk", "runtime", "strategies",
)  # fmt: skip
PROJECT_TOP_LEVEL = {p.split(".")[0] for p in PACKAGES}


def _py_files(pkg: str) -> list[Path]:
    return sorted((PROJECT_ROOT / pkg).rglob("*.py"))


def _imported_top_levels(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            out.add(node.module.split(".")[0])
    return out


@pytest.mark.parametrize("pkg", PACKAGES)
def test_package_imports(pkg: str) -> None:
    importlib.import_module(pkg)


def test_core_imports_no_other_project_module() -> None:
    for path in _py_files("core"):
        bad = (_imported_top_levels(path) & PROJECT_TOP_LEVEL) - {"core"}
        assert not bad, f"{path.relative_to(PROJECT_ROOT)} imports {bad}"


def test_config_depends_only_on_core() -> None:
    for path in _py_files("config"):
        bad = (_imported_top_levels(path) & PROJECT_TOP_LEVEL) - {"core", "config"}
        assert not bad, f"{path.relative_to(PROJECT_ROOT)} imports {bad}"


def test_no_raw_os_environ_outside_config() -> None:
    pattern = re.compile(r"\bos\.(environ|getenv)\b")
    for pkg in PROJECT_TOP_LEVEL - {"config"}:
        for path in _py_files(pkg):
            assert not pattern.search(path.read_text(encoding="utf-8")), (
                f"{path.relative_to(PROJECT_ROOT)} reads os.environ directly; use config.settings"
            )


NETWORK_CLIENTS = {"requests", "httpx", "aiohttp", "websockets", "websocket", "urllib3", "upstox_client"}
# The ONLY modules allowed to open network connections: the two Upstox
# adapters (both go through the central rate limiter / authorize flow) and
# the outbound-only Telegram alert channel (Phase 10).
NETWORK_ALLOWED = {
    Path("broker/upstox_rest.py"): {"httpx"},
    Path("broker/upstox_websocket.py"): {"websockets"},
    Path("monitoring/telegram.py"): {"httpx"},
}


def test_telegram_channel_never_sees_upstox_or_trading() -> None:
    """The alert channel only formats and posts alerts: no broker, execution,
    risk, runtime or Upstox credential access."""
    path = PROJECT_ROOT / "monitoring" / "telegram.py"
    mods = _imported_modules(path)
    assert not {m.split(".")[0] for m in mods} & {"broker", "execution", "risk", "runtime", "strategies", "ml"}
    text = path.read_text(encoding="utf-8")
    assert "upstox_" not in text.lower() and "EnvSettings" not in text  # no Upstox credential access
    assert "api.telegram.org" in text and "follow_redirects=False" in text


def test_launcher_can_only_build_a_paper_broker() -> None:
    """runtime/paper.py builds its broker through the factory (PaperBroker only in
    PAPER) and never calls an order method on the Upstox client."""
    text = (PROJECT_ROOT / "runtime" / "paper.py").read_text(encoding="utf-8")
    assert "create_broker(" in text and "enable_portfolio=False" in text
    assert not re.search(r"\.(place_order|modify_order|cancel_order)\(", text)


def test_network_access_only_from_upstox_adapters() -> None:
    for pkg in PROJECT_TOP_LEVEL:
        for path in _py_files(pkg):
            rel = path.relative_to(PROJECT_ROOT)
            used = _imported_top_levels(path) & NETWORK_CLIENTS
            # websockets is imported lazily inside a function; scan text too.
            if re.search(r"^\s*from websockets|^\s*import websockets", path.read_text(encoding="utf-8"), re.M):
                used.add("websockets")
            allowed = NETWORK_ALLOWED.get(rel, set())
            assert used <= allowed, f"{rel} imports network client(s) {used - allowed}"


def test_unofficial_upstox_sdk_not_used() -> None:
    for pkg in PROJECT_TOP_LEVEL:
        for path in _py_files(pkg):
            assert "upstox_client" not in _imported_top_levels(path)


def test_only_official_upstox_host_configured() -> None:
    from config.settings import BrokerSection

    assert BrokerSection().rest_base_url == "https://api.upstox.com"
    with pytest.raises(ValueError):
        BrokerSection(rest_base_url="https://evil.example.com")  # type: ignore[arg-type]


def test_core_models_do_not_depend_on_broker() -> None:
    for path in _py_files("core"):
        assert "broker" not in _imported_top_levels(path)


SECRET_PATTERNS = [
    re.compile(r"(?i)(api_key|api_secret|access_token|bot_token|password)\s*=\s*['\"][^'\"]{8,}['\"]"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}"),  # JWT-looking
]


def test_no_hardcoded_secrets_in_source_or_config() -> None:
    for pkg in PROJECT_TOP_LEVEL:
        for path in list((PROJECT_ROOT / pkg).rglob("*.py")) + list((PROJECT_ROOT / pkg).rglob("*.yaml")):
            text = path.read_text(encoding="utf-8")
            for pat in SECRET_PATTERNS:
                assert not pat.search(text), f"possible hard-coded secret in {path.relative_to(PROJECT_ROOT)}"


def test_gitignore_protects_secrets_and_databases() -> None:
    gi = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    for required in (".env", "*.db", "logs/"):
        assert required in gi


def test_strategies_never_import_broker_or_execution() -> None:
    for path in _py_files("strategies"):
        bad = _imported_top_levels(path) & {"broker", "execution", "runtime", "data"}
        assert not bad, f"{path.relative_to(PROJECT_ROOT)} imports {bad}"


def test_only_entry_points_import_runtime() -> None:
    # backtest/ is a composition root too: it builds the whole PAPER bot on simulated time.
    for pkg in PROJECT_TOP_LEVEL - {"runtime", "dashboard", "backtest"}:
        for path in _py_files(pkg):
            assert "runtime" not in _imported_top_levels(path), f"{path.relative_to(PROJECT_ROOT)} imports runtime"


def test_backtest_never_uses_the_upstox_adapters() -> None:
    """Backtests replay data through the paper broker only: no REST/WebSocket
    client, no network, and only runtime.bot from the composition root."""
    for path in _py_files("backtest"):
        mods = _imported_modules(path)
        rel = path.relative_to(PROJECT_ROOT)
        assert not {"broker.upstox_rest", "broker.upstox_websocket", "broker.factory"} & mods, rel
        assert {m for m in mods if m.startswith("runtime")} <= {"runtime.bot"}, rel


LOT_LITERAL = re.compile(
    r"\b(?i:lot(?:_size)?)\b\s*(?::\s*int\s*)?[=:]\s*\d+"  # lot_size = 75 / lot: int = 75 / YAML lot_size: 65
    r"|\b(?i:lots)\b\s*[=:]\s*\d{2,}"                      # lots = 75
    r"|\bLOT_SIZE\b\s*=(?!=)"                              # a LOT_SIZE constant (case-sensitive)
    r"|\b[A-Za-z_]*(?i:lot)[A-Za-z_]*\s*(?::\s*int\s*)?[=:]\s*\d{2,}"  # any lot-named field: lot_units: int = 65
)


@pytest.mark.parametrize(("line", "flagged"), [
    ("LOT_SIZE = 65", True), ("lot_size = 75", True), ("def f(lot: int = 75)", True), ("lot_size: 65", True),
    ("Lot_Size=50", True), ("lots = 75", True), ("lot_units: int = 65", True), ("units_per_lot=65", True),
    ("contract_lot: 75", True), ("depth_lots: int = Field(default=20)", False), ("lot_units: int", False),
    ("risk(lot_size=p.instrument.lot_size)", False), ("lot_size=int(rec['lot_size'])", False),
    ("lots = depth // inst.lot_size", False), ("lots = 1", False), ("if LOT_SIZE == x:", False),
])
def test_lot_literal_pattern(line: str, flagged: bool) -> None:
    assert bool(LOT_LITERAL.search(line)) is flagged


def test_no_hardcoded_lot_sizes_in_production_code() -> None:
    """Lot sizes must come from the Upstox instrument list, never a constant."""
    for pkg in PROJECT_TOP_LEVEL:
        for path in list((PROJECT_ROOT / pkg).rglob("*.py")) + list((PROJECT_ROOT / pkg).rglob("*.yaml")):
            if "proto" in path.parts:
                continue
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                code = line.split("#", 1)[0]
                assert not LOT_LITERAL.search(code), f"hard-coded lot size at {path.relative_to(PROJECT_ROOT)}:{n}"


def test_instrument_lot_size_has_no_default() -> None:
    from core.models import Instrument

    field = Instrument.model_fields["lot_size"]
    assert field.is_required()  # must always be supplied from the instrument list


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            out.add(node.module)
    return out


def test_dashboard_is_ui_only() -> None:
    """The dashboard reads state and queues commands; it never imports the
    broker, the execution / risk services or the runtime that acts."""
    forbidden = {"broker", "execution", "risk", "runtime"}
    for path in _py_files("dashboard"):
        mods = _imported_modules(path)
        tops = {m.split(".")[0] for m in mods}
        assert not (tops & forbidden), f"{path.relative_to(PROJECT_ROOT)} imports {tops & forbidden}"
        strategies = {m for m in mods if m.startswith("strategies")}
        assert strategies <= {"strategies.indicators"}, f"{path.relative_to(PROJECT_ROOT)} imports {strategies}"


def test_ml_can_never_reach_orders_or_risk_state() -> None:
    """ML returns decisions; it never imports the execution / risk services,
    the runtime that acts, the dashboard, or any broker client (only the pure
    charge calculator, for cost-adjusted labels)."""
    for path in _py_files("ml"):
        mods = _imported_modules(path)
        rel = path.relative_to(PROJECT_ROOT)
        tops = {m.split(".")[0] for m in mods}
        assert not tops & {"execution", "risk", "runtime", "dashboard"}, f"{rel} imports {tops}"
        assert {m for m in mods if m.startswith("broker")} <= {"broker.charges"}, rel
        assert {m for m in mods if m.startswith("backtest")} <= {"backtest.data", "backtest.purged_cv",
                                                                 "backtest.synthetic_data"}, rel


def test_only_the_bot_migrates_the_schema() -> None:
    """The dashboard opens the bot's database with Database.open_existing; it
    never calls initialize() or the bootstrap module."""
    for path in _py_files("dashboard"):
        text = path.read_text(encoding="utf-8")
        assert ".initialize(" not in text, f"{path.relative_to(PROJECT_ROOT)} migrates the schema"
        assert "data.storage.bootstrap" not in _imported_modules(path)
    assert "Database.open_existing(" in (PROJECT_ROOT / "dashboard" / "data_access.py").read_text(encoding="utf-8")
