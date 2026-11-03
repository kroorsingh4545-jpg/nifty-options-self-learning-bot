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
    "learning", "ml", "monitoring", "options", "risk", "strategies",
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
# The ONLY modules allowed to open network connections (both go through the
# central rate limiter / authorize flow).
NETWORK_ALLOWED = {
    Path("broker/upstox_rest.py"): {"httpx"},
    Path("broker/upstox_websocket.py"): {"websockets"},
}


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
