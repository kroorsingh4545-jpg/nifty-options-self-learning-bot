"""Research command line: ``python -m research <command>``.

Commands::

    init               create / migrate the research database
    status             row counts, fetch log summary, observations
    smoke              live check of the research data sources (about 7 read-only calls)
    history            download daily + 1-minute NIFTY index and India VIX history (blocked 09:00-15:40 weekdays)
    contracts          save the current NIFTY option contract list (strike/expiry per key)
    chain-snapshot     save option-chain snapshots of the nearest expiries (+ next monthly)
    summarize-quotes   copy the bot's recorded option quotes into 1-minute bars (trading.db read-only)
    calendar           derived session calendar + weekly expiries for a year

This is the research composition root: the only research module that builds
the Upstox client. It loads the bot configuration read-only (the Upstox
token is read by ``config.loader`` and never printed), refuses anything but
PAPER mode, gives research its own smaller rate limiter, and hands research
code a ``ReadOnlySource`` (no order, account or portfolio methods).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Awaitable, Callable, Sequence
from datetime import date, timedelta

from broker.upstox_rate_limiter import UpstoxRateLimiter
from broker.upstox_rest import UpstoxRestClient
from config.loader import ConfigError, load_config
from config.settings import BotConfig, EffectiveRateLimits, WindowLimits
from core.clock import now_ist
from core.enums import TradingMode
from research.collect.chain import capture_contracts, snapshot_chains
from research.collect.history import BulkDownloadBlockedError, download_daily, download_minutes, ensure_outside_window
from research.collect.quotes import summarize_recorded_quotes
from research.config import ResearchConfig, load_research_config
from research.sessions import SessionCalendar, weekly_expiries
from research.smoke import run_smoke
from research.sources import ReadOnlySource
from research.store import ResearchStore
from research.trading_readonly import TradingDbUnavailableError, open_trading_db_readonly


def research_limits(cfg: BotConfig, share: float) -> EffectiveRateLimits:
    """``share`` of the bot's effective (already 20 %-buffered) limits."""
    eff = cfg.risk.rate_limits.effective

    def scale(w: WindowLimits) -> WindowLimits:
        return WindowLimits(per_second=max(1, int(w.per_second * share)),
                            per_minute=max(1, int(w.per_minute * share)),
                            per_30_minutes=max(1, int(w.per_30_minutes * share)))

    return EffectiveRateLimits(order_api=scale(eff.order_api), standard_api=scale(eff.standard_api))


async def _with_source(cfg: BotConfig, rcfg: ResearchConfig,
                       work: Callable[[ReadOnlySource], Awaitable[int]]) -> int:
    if not cfg.env.upstox_credentials_present:
        print("UPSTOX_ACCESS_TOKEN is not configured (set it in .env); nothing was called.")
        return 2
    limiter = UpstoxRateLimiter(research_limits(cfg, rcfg.rate_share), cfg.settings.broker.circuit_breaker,
                                queue_timeout=cfg.settings.broker.queue_timeout_seconds)
    rest = UpstoxRestClient.from_config(cfg, limiter)
    try:
        return await work(ReadOnlySource(rest))
    finally:
        await rest.aclose()


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m research", description="Isolated research layer (read-only).")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    sub.add_parser("status")
    sub.add_parser("smoke")
    h = sub.add_parser("history")
    h.add_argument("--what", choices=("all", "daily", "minutes"), default="all")
    h.add_argument("--keys", default="nifty,vix", help="comma list of: nifty, vix")
    h.add_argument("--to", type=date.fromisoformat, default=None, help="last date (default: yesterday)")
    sub.add_parser("contracts")
    sub.add_parser("chain-snapshot")
    q = sub.add_parser("summarize-quotes")
    q.add_argument("--day", type=date.fromisoformat, action="append", default=None)
    c = sub.add_parser("calendar")
    c.add_argument("--year", type=int, default=None)
    return p


def _status(store: ResearchStore) -> int:
    for table in ("daily_candles", "minute_candles", "contracts", "chain_snapshots", "chain_snapshot_rows",
                  "quote_minute_bars", "fetch_log", "observations"):
        print(f"{table:22s} {store.count(table):>10,}")
    for r in store.con.execute("SELECT instrument_key, unit, interval, COUNT(*) n, SUM(rows) rows,"
                               " SUM(status='ERROR') errors, MAX(fetched_at) last FROM fetch_log"
                               " GROUP BY 1,2,3 ORDER BY 1,2,3"):
        print(f"  fetched {r['instrument_key']} {r['unit']}/{r['interval']}: {r['n']} requests, {r['rows']} rows, "
              f"{r['errors']} errors, last {r['last']}")
    for r in store.con.execute("SELECT name, value, evidence FROM observations ORDER BY name"):
        print(f"  observation {r['name']} = {r['value']} ({r['evidence']})")
    return 0


def _history(args: argparse.Namespace, cfg: BotConfig, rcfg: ResearchConfig, store: ResearchStore) -> int:
    try:
        ensure_outside_window(now_ist(), rcfg.history.blocked_window)
    except BulkDownloadBlockedError as exc:
        print(exc)
        return 3
    names = {"nifty": rcfg.instruments.nifty_index_key, "vix": rcfg.instruments.india_vix_key}
    keys = [k.strip() for k in args.keys.split(",") if k.strip()]
    if not keys or set(keys) - set(names):
        print(f"--keys must be a comma list of: {', '.join(names)}")
        return 2
    end = args.to or now_ist().date() - timedelta(days=1)

    async def work(source: ReadOnlySource) -> int:
        for k in keys:
            if args.what in ("all", "daily"):
                s = await download_daily(source, store, names[k], start=rcfg.history.daily_from, end=end,
                                         chunk_years=rcfg.history.daily_chunk_years, now=now_ist)
                print(s.line())
            if args.what in ("all", "minutes"):
                s = await download_minutes(source, store, names[k], start=rcfg.history.minute_from, end=end,
                                           interval=rcfg.history.minute_interval, now=now_ist)
                print(s.line())
        return 0

    return asyncio.run(_with_source(cfg, rcfg, work))


def _calendar(args: argparse.Namespace, cfg: BotConfig, rcfg: ResearchConfig, store: ResearchStore) -> int:
    sessions = store.daily_sessions(rcfg.instruments.nifty_index_key)
    if not sessions:
        print("no NIFTY daily candles in the research database yet: run `python -m research history --what daily`")
        return 1
    cal = SessionCalendar.from_sessions(sessions, configured_holidays=cfg.settings.market_session.holidays)
    year = args.year or now_ist().year
    print(f"observed sessions {cal.first} .. {cal.last}: {len(cal.trading_days)} regular, "
          f"{len(cal.special_sessions)} special, {len(cal.holidays)} weekday holidays")
    print(f"{year} holidays (from candles): {', '.join(map(str, cal.holidays_in(year))) or 'none observed'}")
    special = sorted(d for d in cal.special_sessions if d.year == year)
    print(f"{year} special sessions: {', '.join(map(str, special)) or 'none'}")
    for kind, days in cal.configured_mismatches(year).items():
        if days:
            print(f"  MISMATCH {kind}: {', '.join(map(str, days))}")
    observed = store.observed_expiries(rcfg.instruments.nifty_index_key)
    for e in weekly_expiries(date(year, 1, 1), date(year, 12, 31), rcfg.weekly_expiry_rules, cal, observed=observed):
        moved = f" (moved from {e.rule_date})" if e.shifted else ""
        print(f"  expiry {e.expiry} [{e.status}]{moved}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        rcfg = load_research_config()
        cfg = load_config()
    except (ConfigError, ValueError) as exc:
        print(f"configuration error: {exc}")
        return 2
    if cfg.mode is not TradingMode.PAPER:
        print("research runs only with TRADING_MODE=PAPER")
        return 2
    store = ResearchStore.open(rcfg.resolve_store_path(cfg.project_root), forbidden=[cfg.database_path])
    try:
        return _dispatch(args, cfg, rcfg, store)
    finally:
        store.close()


def _dispatch(args: argparse.Namespace, cfg: BotConfig, rcfg: ResearchConfig, store: ResearchStore) -> int:
    cmd: str = args.command
    if cmd == "init":
        print(f"research database ready: {store.path}")
        return 0
    if cmd == "status":
        return _status(store)
    if cmd == "history":
        return _history(args, cfg, rcfg, store)
    if cmd == "calendar":
        return _calendar(args, cfg, rcfg, store)
    if cmd == "summarize-quotes":
        try:
            trading = open_trading_db_readonly(cfg.database_path)
        except TradingDbUnavailableError as exc:
            print(exc)
            return 1
        try:
            done = summarize_recorded_quotes(trading, store, now=now_ist, days=args.day)
        finally:
            trading.close()
        for d, n in done.items():
            print(f"{d}: {n} one-minute quote bars")
        return 0

    async def smoke(source: ReadOnlySource) -> int:
        lines = await run_smoke(source, store, rcfg, now_ist)
        for line in lines:
            print(line.render())
        return 0 if all(line.ok for line in lines) else 1

    async def contracts(source: ReadOnlySource) -> int:
        got = await capture_contracts(source, store, rcfg.instruments.nifty_index_key, now_ist)
        print(f"{len(got)} NIFTY option contracts saved")
        return 0

    async def chains(source: ReadOnlySource) -> int:
        for r in await snapshot_chains(source, store, rcfg.instruments.nifty_index_key, rcfg.chain, now_ist):
            print(f"{r.expiry}: " + (f"ERROR {r.error}" if r.error else f"{r.rows} rows, spot {r.spot}"))
        return 0

    jobs: dict[str, Callable[[ReadOnlySource], Awaitable[int]]] = {
        "smoke": smoke, "contracts": contracts, "chain-snapshot": chains}
    return asyncio.run(_with_source(cfg, rcfg, jobs[cmd]))


if __name__ == "__main__":
    sys.exit(main())
