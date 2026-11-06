"""Load and validate the full bot configuration.

    from config.loader import load_config
    cfg = load_config()          # config/*.yaml + .env from the project root

The bot must refuse to start if this raises ``ConfigError``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from config import hard_limits
from config.settings import AppSettings, BotConfig, EnvSettings, RiskLimits, StrategyDefaults
from core.enums import TradingMode, UpstoxTokenType
from core.masking import DEFAULT_MASKER, SecretMasker

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_DIR = PROJECT_ROOT / "config"

SETTINGS_FILE = "settings.yaml"
RISK_FILE = "risk_limits.yaml"
STRATEGIES_FILE = "strategy_defaults.yaml"


class ConfigError(RuntimeError):
    """Configuration is missing or invalid. The bot must not start."""


class LiveModeNotPermittedError(ConfigError):
    """TRADING_MODE=LIVE requested but LIVE trading is not available."""


def _format_validation_error(source: str, exc: ValidationError, masker: SecretMasker) -> str:
    # Deliberately omit input values: they may contain secrets.
    lines = [f"invalid configuration in {source}:"]
    for err in exc.errors(include_input=False, include_url=False):
        loc = ".".join(str(p) for p in err.get("loc", ())) or "<root>"
        lines.append(f"  - {loc}: {err.get('msg', 'invalid')}")
    return masker.mask_text("\n".join(lines))


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}")
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        raise ConfigError(f"could not parse YAML in {path.name}: {exc}") from exc
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path.name} must contain a mapping at the top level")
    return data


def load_env(env_file: Path | None) -> EnvSettings:
    """Load secrets/mode from the process environment and optional ``.env``."""
    try:
        if env_file is not None and env_file.is_file():
            return EnvSettings(_env_file=env_file)
        return EnvSettings()
    except ValidationError as exc:
        raise ConfigError(_format_validation_error("environment/.env", exc, DEFAULT_MASKER)) from None


def load_config(
    config_dir: Path | None = None,
    env_file: Path | None = None,
    *,
    use_default_env_file: bool = True,
    masker: SecretMasker = DEFAULT_MASKER,
) -> BotConfig:
    """Load, validate, and freeze the bot configuration.

    Args:
        config_dir: directory with the three YAML files (default ``config/``).
        env_file: explicit ``.env`` path. If omitted and
            ``use_default_env_file`` is true, ``<project_root>/.env`` is used
            when it exists.
        masker: secret masker that loaded secrets are registered with.

    Raises:
        ConfigError: on any missing file or validation failure.
        LiveModeNotPermittedError: if LIVE mode is requested.
    """
    config_dir = Path(config_dir) if config_dir is not None else DEFAULT_CONFIG_DIR
    project_root = config_dir.resolve().parent
    if env_file is None and use_default_env_file:
        env_file = PROJECT_ROOT / ".env"

    env = load_env(env_file)
    # Register secrets before anything else can log or raise with them.
    masker.register_many(env.secret_values())

    if env.trading_mode is TradingMode.LIVE and env.upstox_token_type is UpstoxTokenType.ANALYTICS:
        # Independent of LIVE_TRADING_AVAILABLE: a read-only analytics token can never trade.
        raise LiveModeNotPermittedError(
            "TRADING_MODE=LIVE is not permitted with UPSTOX_TOKEN_TYPE=analytics: the Upstox Analytics "
            "Token is read-only and cannot place orders. Use PAPER."
        )
    if env.trading_mode is TradingMode.LIVE and not hard_limits.LIVE_TRADING_AVAILABLE:
        raise LiveModeNotPermittedError(
            "TRADING_MODE=LIVE is not permitted: LIVE order routing and the LIVE "
            "preconditions in docs/safety-policy.md are not implemented. Use PAPER."
        )

    sources = {
        SETTINGS_FILE: _read_yaml(config_dir / SETTINGS_FILE),
        RISK_FILE: _read_yaml(config_dir / RISK_FILE),
        STRATEGIES_FILE: _read_yaml(config_dir / STRATEGIES_FILE),
    }
    parsed: dict[str, Any] = {}
    for name, model in (
        (SETTINGS_FILE, AppSettings),
        (RISK_FILE, RiskLimits),
        (STRATEGIES_FILE, StrategyDefaults),
    ):
        try:
            parsed[name] = model.model_validate(sources[name])
        except ValidationError as exc:
            raise ConfigError(_format_validation_error(name, exc, masker)) from None

    try:
        return BotConfig(
            project_root=project_root,
            settings=parsed[SETTINGS_FILE],
            risk=parsed[RISK_FILE],
            strategies=parsed[STRATEGIES_FILE],
            env=env,
        )
    except ValidationError as exc:
        raise ConfigError(_format_validation_error("cross-file validation", exc, masker)) from None
