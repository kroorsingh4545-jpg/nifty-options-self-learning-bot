"""volatility/ is pure mathematics: no I/O, no network, no database, no project state."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FILES = sorted((ROOT / "volatility").rglob("*.py"))
ALLOWED_TOP_LEVELS = {"__future__", "math", "dataclasses", "typing", "collections", "datetime", "numpy",
                      "statistics", "volatility"}


def test_volatility_package_exists() -> None:
    assert {p.name for p in FILES} >= {"__init__.py", "realised.py", "expected_move.py", "calibration.py",
                                        "surface.py"}


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_volatility_imports_only_pure_libraries(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    tops: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            tops |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0 and node.module
            tops.add(node.module.split(".")[0])
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in {"open", "eval", "exec", "input"}, f"{path.name} calls {node.func.id}()"
    assert tops <= ALLOWED_TOP_LEVELS, f"{path.name} imports {tops - ALLOWED_TOP_LEVELS}"
