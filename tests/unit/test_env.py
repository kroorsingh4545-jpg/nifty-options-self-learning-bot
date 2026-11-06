""".env / environment loading, mode validation, and secret handling."""

from __future__ import annotations

from pathlib import Path

import pytest

from config.loader import ConfigError, LiveModeNotPermittedError, load_config
from core.enums import TradingMode
from core.masking import DEFAULT_MASKER

PROJECT_ROOT = Path(__file__).resolve().parents[2]

FAKE_KEY = "fake-api-key-1234567890abcdef"
FAKE_TOKEN = "fake-access-token-ZYXWVUTSRQPONMLKJIHG"


def _env(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "test.env"
    p.write_text(text, encoding="utf-8")
    return p


def test_env_example_loads_as_paper(config_dir: Path) -> None:
    cfg = load_config(config_dir, env_file=PROJECT_ROOT / ".env.example")
    assert cfg.mode is TradingMode.PAPER
    assert cfg.env.upstox_api_key is None  # empty values are treated as unset
    assert not cfg.env.upstox_credentials_present


def test_database_path_can_be_overridden_by_the_environment(config_dir: Path, tmp_path: Path,
                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    default = load_config(config_dir, env_file=_env(tmp_path, ""))
    assert default.database_path == default.resolve_path(Path("data/trading.db"))
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "var" / "trading.db"))  # Docker volume
    cfg = load_config(config_dir, env_file=_env(tmp_path, ""))
    assert cfg.database_path == tmp_path / "var" / "trading.db"


def test_missing_trading_mode_defaults_to_paper(config_dir: Path, tmp_path: Path) -> None:
    cfg = load_config(config_dir, env_file=_env(tmp_path, ""))
    assert cfg.mode is TradingMode.PAPER


@pytest.mark.parametrize("value", ["paper", " PAPER ", "Paper"])
def test_trading_mode_is_normalised(config_dir: Path, tmp_path: Path, value: str) -> None:
    cfg = load_config(config_dir, env_file=_env(tmp_path, f"TRADING_MODE={value}\n"))
    assert cfg.mode is TradingMode.PAPER


@pytest.mark.parametrize("value", ["LIVE", "live"])
def test_live_mode_is_refused(config_dir: Path, tmp_path: Path, value: str) -> None:
    with pytest.raises(LiveModeNotPermittedError, match="not permitted"):
        load_config(config_dir, env_file=_env(tmp_path, f"TRADING_MODE={value}\n"))


def test_live_mode_from_process_env_is_refused(config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRADING_MODE", "LIVE")
    with pytest.raises(LiveModeNotPermittedError):
        load_config(config_dir, use_default_env_file=False)


@pytest.mark.parametrize("value", ["DEMO", "1", "PAPERLIVE"])
def test_invalid_trading_mode_is_refused(config_dir: Path, tmp_path: Path, value: str) -> None:
    with pytest.raises(ConfigError, match="trading_mode"):
        load_config(config_dir, env_file=_env(tmp_path, f"TRADING_MODE={value}\n"))


def test_process_env_overrides_env_file(config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UPSTOX_API_KEY", FAKE_KEY)
    cfg = load_config(config_dir, env_file=_env(tmp_path, "UPSTOX_API_KEY=from-file-value-xxxxxxxx\n"))
    assert cfg.env.upstox_api_key is not None
    assert cfg.env.upstox_api_key.get_secret_value() == FAKE_KEY


def test_secrets_are_secretstr_and_never_in_repr(config_dir: Path, tmp_path: Path) -> None:
    env_file = _env(tmp_path, f"UPSTOX_API_KEY={FAKE_KEY}\nUPSTOX_ACCESS_TOKEN={FAKE_TOKEN}\n")
    cfg = load_config(config_dir, env_file=env_file)
    assert cfg.env.upstox_credentials_present
    for text in (repr(cfg), str(cfg), repr(cfg.env), cfg.env.model_dump_json()):
        assert FAKE_KEY not in text
        assert FAKE_TOKEN not in text


def test_loaded_secrets_are_registered_with_masker(config_dir: Path, tmp_path: Path) -> None:
    load_config(config_dir, env_file=_env(tmp_path, f"UPSTOX_ACCESS_TOKEN={FAKE_TOKEN}\n"))
    masked = DEFAULT_MASKER.mask_text(f"calling upstox with {FAKE_TOKEN}")
    assert FAKE_TOKEN not in masked


def test_dashboard_credentials_must_be_paired(config_dir: Path, tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="DASHBOARD"):
        load_config(config_dir, env_file=_env(tmp_path, "DASHBOARD_USERNAME=admin\n"))


def test_short_dashboard_password_rejected_without_leaking_it(config_dir: Path, tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as info:
        load_config(config_dir, env_file=_env(tmp_path, "DASHBOARD_USERNAME=admin\nDASHBOARD_PASSWORD=pw12\n"))
    assert "pw12" not in str(info.value)


def test_telegram_credentials_must_be_paired(config_dir: Path, tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="TELEGRAM"):
        load_config(config_dir, env_file=_env(tmp_path, "TELEGRAM_CHAT_ID=12345\n"))


def test_validation_errors_do_not_echo_inputs(config_dir: Path, tmp_path: Path) -> None:
    secret_like = "SuperSecretModeValue123456"
    with pytest.raises(ConfigError) as info:
        load_config(config_dir, env_file=_env(tmp_path, f"TRADING_MODE={secret_like}\n"))
    assert secret_like not in str(info.value)


def test_env_example_contains_no_values() -> None:
    lines = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
    for line in lines:
        if not line.strip() or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        if key == "TRADING_MODE":
            assert value == "PAPER"
        else:
            assert value == "", f".env.example must not contain a value for {key}"
