"""Configuration loading and risk-limit validation."""

from __future__ import annotations

from datetime import time
from pathlib import Path
from typing import Any

import pytest

from config import hard_limits as hl
from config.loader import ConfigError, load_config
from core.enums import AutonomyLevel, TradingMode


def _load(config_dir: Path) -> Any:
    return load_config(config_dir, use_default_env_file=False)


# --------------------------------------------------------------------------- #
# Happy path — the committed YAML files
# --------------------------------------------------------------------------- #


def test_repository_config_loads_and_defaults_to_paper() -> None:
    cfg = load_config(use_default_env_file=False)
    assert cfg.mode is TradingMode.PAPER
    assert cfg.settings.app.autonomy_level is AutonomyLevel.PAPER_AUTO_IMPROVE
    assert set(cfg.strategies.strategies) == {"orb_options", "vwap_ema"}


def test_spec_risk_values(config_dir: Path) -> None:
    risk = _load(config_dir).risk
    assert risk.capital.total_capital == 200_000
    assert risk.capital.per_strategy_capital == 100_000
    assert risk.per_trade.max_risk_per_trade == 2_000
    assert risk.per_strategy.max_daily_loss == 5_000
    assert risk.global_.max_daily_loss == 10_000
    assert risk.per_strategy.max_open_trades == 2
    assert risk.global_.max_open_trades == 4
    assert risk.per_strategy.max_trades_per_day == 4
    assert risk.per_strategy.loss_cooldown_minutes == 15
    assert risk.per_strategy.max_consecutive_losses == 3
    assert risk.time_rules.no_new_entries_after == time(14, 45)
    assert risk.time_rules.square_off_start == time(15, 10)
    assert risk.time_rules.square_off_end == time(15, 20)
    assert set(risk.global_.on_breach) == hl.REQUIRED_GLOBAL_BREACH_ACTIONS


def test_effective_rate_limits_apply_20pct_buffer(config_dir: Path) -> None:
    eff = _load(config_dir).risk.rate_limits.effective
    assert (eff.order_api.per_second, eff.order_api.per_minute, eff.order_api.per_30_minutes) == (8, 400, 1600)
    assert (eff.standard_api.per_second, eff.standard_api.per_minute, eff.standard_api.per_30_minutes) == (
        40,
        400,
        1600,
    )


def test_strategies_are_isolated_with_own_capital(config_dir: Path) -> None:
    cfg = _load(config_dir)
    for s in cfg.strategies.strategies.values():
        assert s.allocated_capital == 100_000
        assert s.sizing.risk_per_trade <= 2_000
        assert s.ml.confidence_threshold == pytest.approx(0.60)
    assert sum(s.allocated_capital for s in cfg.strategies.strategies.values()) <= 200_000


def test_config_is_frozen(config_dir: Path) -> None:
    cfg = _load(config_dir)
    with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError on frozen model
        cfg.risk.per_trade.max_risk_per_trade = 5_000  # type: ignore[misc]


def test_relative_paths_resolve_against_project_root(config_dir: Path) -> None:
    cfg = _load(config_dir)
    assert cfg.database_path == config_dir.parent / "data" / "trading.db"
    assert cfg.log_directory == config_dir.parent / "logs"


# --------------------------------------------------------------------------- #
# Weakening risk controls must be refused
# --------------------------------------------------------------------------- #

WEAKENING_CASES = [
    ("risk_limits.yaml", ["per_trade", "max_risk_per_trade"], 2_500),
    ("risk_limits.yaml", ["per_strategy", "max_daily_loss"], 6_000),
    ("risk_limits.yaml", ["global", "max_daily_loss"], 15_000),
    ("risk_limits.yaml", ["per_strategy", "max_open_trades"], 3),
    ("risk_limits.yaml", ["global", "max_open_trades"], 5),
    ("risk_limits.yaml", ["per_strategy", "max_trades_per_day"], 10),
    ("risk_limits.yaml", ["per_strategy", "loss_cooldown_minutes"], 5),
    ("risk_limits.yaml", ["per_strategy", "max_consecutive_losses"], 5),
    ("risk_limits.yaml", ["time_rules", "no_new_entries_after"], "15:00"),
    ("risk_limits.yaml", ["time_rules", "square_off_start"], "15:15"),
    ("risk_limits.yaml", ["time_rules", "square_off_end"], "15:25"),
    ("risk_limits.yaml", ["rate_limits", "safety_buffer"], 0.10),
    ("risk_limits.yaml", ["rate_limits", "order_api", "per_second"], 20),
    ("risk_limits.yaml", ["rate_limits", "standard_api", "per_minute"], 1_000),
    ("strategy_defaults.yaml", ["strategies", "orb_options", "sizing", "risk_per_trade"], 3_000),
    ("strategy_defaults.yaml", ["strategies", "orb_options", "sizing", "size_multiplier"], 1.5),
    ("strategy_defaults.yaml", ["strategies", "orb_options", "risk", "max_daily_loss"], 8_000),
    ("strategy_defaults.yaml", ["strategies", "vwap_ema", "risk", "max_trades_per_day"], 6),
    ("strategy_defaults.yaml", ["strategies", "vwap_ema", "risk", "loss_cooldown_minutes"], 0),
    ("strategy_defaults.yaml", ["strategies", "vwap_ema", "entry_window", "end"], "15:00"),
    ("strategy_defaults.yaml", ["strategies", "vwap_ema", "allocated_capital"], 150_000),
    ("strategy_defaults.yaml", ["strategies", "orb_options", "stop_loss", "value"], 80),
]


@pytest.mark.parametrize(("file", "path", "value"), WEAKENING_CASES, ids=lambda x: str(x))
def test_weakened_limits_refuse_to_load(
    config_dir: Path, edit_yaml: Any, file: str, path: list[str], value: Any
) -> None:
    def mutate(d: dict[str, Any]) -> None:
        for key in path[:-1]:
            d = d[key]
        d[path[-1]] = value

    edit_yaml(file, mutate)
    with pytest.raises(ConfigError):
        _load(config_dir)


def test_stricter_limits_are_accepted(config_dir: Path, edit_yaml: Any) -> None:
    def stricter(d: dict[str, Any]) -> None:
        d["per_trade"]["max_risk_per_trade"] = 1_000
        d["per_strategy"]["max_daily_loss"] = 3_000
        d["global"]["max_daily_loss"] = 6_000
        d["per_strategy"]["loss_cooldown_minutes"] = 30
        d["time_rules"]["no_new_entries_after"] = "14:00"
        d["rate_limits"]["safety_buffer"] = 0.30

    def strategies_within(d: dict[str, Any]) -> None:
        for s in d["strategies"].values():
            s["sizing"]["risk_per_trade"] = 1_000
            s["risk"]["max_daily_loss"] = 3_000
            s["risk"]["loss_cooldown_minutes"] = 30
            s["entry_window"]["end"] = "13:30"

    edit_yaml("risk_limits.yaml", stricter)
    edit_yaml("strategy_defaults.yaml", strategies_within)
    cfg = _load(config_dir)
    assert cfg.risk.per_trade.max_risk_per_trade == 1_000
    assert cfg.risk.rate_limits.effective.order_api.per_second == 7  # floor(10 * 0.7)


def test_global_breach_actions_cannot_be_dropped(config_dir: Path, edit_yaml: Any) -> None:
    edit_yaml("risk_limits.yaml", lambda d: d["global"].__setitem__("on_breach", ["SEND_ALERTS"]))
    with pytest.raises(ConfigError, match="on_breach"):
        _load(config_dir)


# --------------------------------------------------------------------------- #
# Malformed configuration
# --------------------------------------------------------------------------- #


def test_unknown_keys_are_rejected(config_dir: Path, edit_yaml: Any) -> None:
    edit_yaml("risk_limits.yaml", lambda d: d["per_trade"].__setitem__("max_risk_per_trde", 100))
    with pytest.raises(ConfigError, match="max_risk_per_trde"):
        _load(config_dir)


def test_unquoted_yaml_time_is_rejected(config_dir: Path) -> None:
    path = config_dir / "risk_limits.yaml"
    path.write_text(path.read_text(encoding="utf-8").replace('"14:45"', "14:45"), encoding="utf-8")
    with pytest.raises(ConfigError, match="quoted"):
        _load(config_dir)


def test_missing_file_is_reported(config_dir: Path) -> None:
    (config_dir / "strategy_defaults.yaml").unlink()
    with pytest.raises(ConfigError, match="not found"):
        _load(config_dir)


def test_invalid_yaml_is_reported(config_dir: Path) -> None:
    (config_dir / "settings.yaml").write_text("app: [unclosed\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="parse"):
        _load(config_dir)


def test_non_mapping_yaml_is_reported(config_dir: Path) -> None:
    (config_dir / "settings.yaml").write_text("- a\n- b\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="mapping"):
        _load(config_dir)


def test_invalid_autonomy_level(config_dir: Path, edit_yaml: Any) -> None:
    edit_yaml("settings.yaml", lambda d: d["app"].__setitem__("autonomy_level", "FULL_AUTO_LIVE"))
    with pytest.raises(ConfigError, match="autonomy_level"):
        _load(config_dir)


def test_no_strategies_is_rejected(config_dir: Path, edit_yaml: Any) -> None:
    edit_yaml("strategy_defaults.yaml", lambda d: d.__setitem__("strategies", {}))
    with pytest.raises(ConfigError):
        _load(config_dir)


def test_bad_strategy_id_is_rejected(config_dir: Path, edit_yaml: Any) -> None:
    def rename(d: dict[str, Any]) -> None:
        d["strategies"]["Bad-ID"] = d["strategies"].pop("orb_options")

    edit_yaml("strategy_defaults.yaml", rename)
    with pytest.raises(ConfigError):
        _load(config_dir)
