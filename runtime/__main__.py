"""PAPER trading launcher.

    python -m runtime check                 # pre-flight checks + the strategy versions a session would trade
    python -m runtime check --connect       # ... and one Upstox market-data call (NIFTY LTP) to validate the token
    python -m runtime paper                 # start a real-time PAPER session (Ctrl+C to stop)
    python -m runtime paper --paused        # start with the engine PAUSED (start it from the dashboard)
    python -m runtime paper --stop-at 15:40 # end the session at 15:40 IST today
    python -m runtime paper --baseline      # trade the YAML config, ignoring PAPER deployments

PAPER only: real Upstox market data, simulated orders. There is no flag,
setting or environment variable that turns this into LIVE trading.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, time

from broker.upstox_rate_limiter import UpstoxRateLimiter
from broker.upstox_rest import UpstoxRestClient
from config.loader import ConfigError, load_config
from config.settings import BotConfig
from core.clock import now_ist
from core.enums import AutonomyLevel, TradingMode
from data.storage.database import Database, DatabaseError
from data.storage.repositories import Repositories
from monitoring.json_logger import configure_from_config
from runtime.paper import (
    PaperLaunchError,
    PaperSession,
    SessionOptions,
    preflight,
    render_checks,
    render_versions,
    resolve_startup_config,
)


def _hhmm(value: str) -> time:
    try:
        return datetime.strptime(value, "%H:%M").time()
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected HH:MM (IST), got {value!r}") from exc


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m runtime", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="pre-flight checks (no orders, no trading)")
    c.add_argument("--connect", action="store_true",
                   help="also make one Upstox market-data call (NIFTY LTP) to validate the token")
    c.add_argument("--baseline", action="store_true", help="show versions as with paper --baseline")
    r = sub.add_parser("paper", help="run a real-time PAPER session")
    r.add_argument("--paused", action="store_true", help="start with the engine PAUSED")
    r.add_argument("--baseline", action="store_true", help="trade the YAML config, ignore PAPER deployments")
    r.add_argument("--stop-at", type=_hhmm, help="end the session at this IST time today (HH:MM)")
    h = sub.add_parser("health", help="exit 0 if a PAPER bot published a heartbeat recently (Docker healthcheck)")
    h.add_argument("--max-age", type=float, default=30.0, help="seconds")
    return p


def health(cfg: BotConfig, max_age: float) -> tuple[bool, str]:
    if not cfg.database_path.is_file():
        return False, "no trading database yet"
    try:
        db = Database.open_existing(cfg.database_path)
    except DatabaseError as exc:
        return False, str(exc)
    try:
        latest = Repositories(db, TradingMode.PAPER).runtime_state.latest()
    finally:
        db.close()
    if latest is None:
        return False, "the bot has not published a heartbeat"
    state, at = latest
    age = (now_ist() - at).total_seconds()
    if state.get("mode") != TradingMode.PAPER.value:
        return False, f"heartbeat mode is {state.get('mode')!r}, expected PAPER"
    if age > max_age:
        return False, f"heartbeat is {age:.0f}s old"
    return True, f"PAPER bot alive (heartbeat {age:.0f}s old, engine {state.get('engine_state')})"


def _versions_preview(cfg: BotConfig, autonomy: AutonomyLevel, baseline: bool) -> str:
    if not cfg.database_path.is_file():
        return "  (no trading database yet: every strategy runs its YAML version)"
    try:
        db = Database.open_existing(cfg.database_path)
    except DatabaseError as exc:
        return f"  (database not readable yet: {exc})"
    try:
        _, versions = resolve_startup_config(cfg, Repositories(db, TradingMode.PAPER), autonomy=autonomy,
                                             baseline=baseline)
    finally:
        db.close()
    return render_versions(versions)


async def _connect_check(cfg: BotConfig) -> str:
    """One market-data call (REST LTP of the NIFTY index). Works with both an
    analytics and a daily token; never touches user, funds, portfolio or
    order APIs."""
    key = cfg.settings.instruments.nifty_index_key
    kind = cfg.env.upstox_token_type.value
    rest = UpstoxRestClient.from_config(cfg, UpstoxRateLimiter.from_config(cfg))
    try:
        prices = await rest.get_ltp([key])
        if not prices:
            return f"[FAIL] Upstox token ({kind}): market-quote call returned no LTP for {key}"
        ltp = next(iter(prices.values()))
        return f"[PASS] Upstox token ({kind}): valid (market-data LTP call ok; {key} LTP {ltp:,.2f})"
    except Exception as exc:  # report, never raise: this is a check
        return f"[FAIL] Upstox token ({kind}): {type(exc).__name__}: {exc}"
    finally:
        await rest.aclose()


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        cfg = load_config()
    except ConfigError as exc:
        print(f"CONFIG ERROR: {exc}", file=sys.stderr)
        return 2
    if args.cmd == "health":
        ok, detail = health(cfg, args.max_age)
        print(detail)
        return 0 if ok else 1
    configure_from_config(cfg)
    checks = preflight(cfg)
    print("PAPER pre-flight checks:")
    print(render_checks(checks))
    autonomy = AutonomyLevel(cfg.settings.app.autonomy_level)
    print(f"\nStrategy versions (autonomy {autonomy.value}):")
    print(_versions_preview(cfg, autonomy, args.baseline))
    failed = [c for c in checks if not c.ok and c.fatal]
    if args.cmd == "check":
        if args.connect:
            if not cfg.env.upstox_credentials_present:
                print("\n[FAIL] Upstox token: not checked, credentials missing (see the pre-flight item above)")
                return 1
            line = asyncio.run(_connect_check(cfg))
            print("\n" + line)
            if line.startswith("[FAIL]"):
                return 1
        return 1 if failed else 0
    if failed:
        print("\nRefusing to start: fix the FAIL items above.", file=sys.stderr)
        return 2
    session = PaperSession(cfg, options=SessionOptions(paused=args.paused, baseline=args.baseline,
                                                       stop_at=args.stop_at))
    print("\nStarting PAPER session (simulated orders only). Ctrl+C to stop.")
    try:
        result = asyncio.run(session.run())
    except KeyboardInterrupt:
        print("PAPER session interrupted.")
        return 0
    except PaperLaunchError as exc:
        print(f"PAPER session refused: {exc}", file=sys.stderr)
        return 2
    print(f"PAPER session ended ({result.reason or 'stopped'}); open trades left: {result.open_trades}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
