"""Validate configuration and print a masked summary.

    python -m config
"""

from __future__ import annotations

import sys

from config.loader import ConfigError, load_config
from core.masking import mask_secret


def main() -> int:
    try:
        cfg = load_config()
    except ConfigError as exc:
        print(f"CONFIG ERROR: {exc}", file=sys.stderr)
        return 2

    env = cfg.env
    risk = cfg.risk
    eff = risk.rate_limits.effective

    def secret(v: object) -> str:
        return mask_secret(v.get_secret_value()) if v is not None else "(not set)"  # type: ignore[attr-defined]

    print("Configuration OK")
    print(f"  TRADING_MODE          : {cfg.mode.value}")
    print(f"  autonomy_level        : {cfg.settings.app.autonomy_level.value}")
    print(f"  database              : {cfg.database_path}")
    print(f"  logs                  : {cfg.log_directory / cfg.settings.logging.file_name}")
    print(f"  UPSTOX_API_KEY        : {secret(env.upstox_api_key)}")
    print(f"  UPSTOX_API_SECRET     : {secret(env.upstox_api_secret)}")
    print(f"  UPSTOX_ACCESS_TOKEN   : {secret(env.upstox_access_token)}")
    print(f"  UPSTOX_TOKEN_TYPE     : {env.upstox_token_type.value}")
    print(f"  TELEGRAM_BOT_TOKEN    : {secret(env.telegram_bot_token)}")
    print(f"  DASHBOARD auth        : {'configured' if env.dashboard_username else '(not set)'}")
    print(f"  capital total/strategy: {risk.capital.total_capital:,.0f} / {risk.capital.per_strategy_capital:,.0f}")
    print(f"  max risk/trade        : {risk.per_trade.max_risk_per_trade:,.0f}")
    print(f"  daily loss strat/glob : {risk.per_strategy.max_daily_loss:,.0f} / {risk.global_.max_daily_loss:,.0f}")
    print(
        f"  time rules            : no entries after {risk.time_rules.no_new_entries_after:%H:%M}, "
        f"square-off {risk.time_rules.square_off_start:%H:%M}-{risk.time_rules.square_off_end:%H:%M}"
    )
    print(
        f"  effective rate limits : order {eff.order_api.per_second}/s {eff.order_api.per_minute}/min "
        f"{eff.order_api.per_30_minutes}/30min; standard {eff.standard_api.per_second}/s "
        f"{eff.standard_api.per_minute}/min {eff.standard_api.per_30_minutes}/30min"
    )
    for sid, s in cfg.strategies.strategies.items():
        print(f"  strategy {sid:<12}: v{s.version} enabled={s.enabled} capital={s.allocated_capital:,.0f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
