"""Research command line: ``python -m research <command>``.

Commands::

    init               create / migrate the research database
    status             row counts, fetch log summary, observations
    smoke              live check of the research data sources (about 7 read-only calls)
    history            download daily + 1-minute NIFTY index and India VIX history
    contracts          save the current NIFTY option contract list (strike/expiry per key)
    chain-snapshot     save option-chain snapshots of the nearest expiries (+ next monthly)
    chain-schedule     take chain snapshots at the research.yaml chain.schedule times today, then exit

Every command that calls Upstox is refused on weekdays inside
``history.blocked_window`` (08:30-15:40 IST), so research never competes
with a running PAPER session for the rate limit. ``contracts`` and
``chain-snapshot`` (a few calls each) may opt in with ``--during-session``;
so may ``chain-schedule`` (about 5 calls per scheduled time);
``history`` and ``smoke`` never can.
    summarize-quotes   copy the bot's recorded option quotes into 1-minute bars (trading.db read-only)
    archive-quotes     copy the bot's raw 5-second option and market quotes (trading.db read-only)
    data-quality       data-quality audit of recorded sessions (trading.db read-only); JSON also stored
    calendar           derived session calendar + weekly expiries for a year
    forecast           build forecast (before each session) and outcome rows from stored history
    report [--json]    forecast-vs-reality and volatility report (markdown, or JSON); JSON also stored

This is the research composition root: the only research module that builds
the Upstox client. It loads the bot configuration read-only (the Upstox
token is read by ``config.loader`` and never printed), refuses anything but
PAPER mode, gives research its own smaller rate limiter, and hands research
code a ``ReadOnlySource`` (no order, account or portfolio methods).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
from collections.abc import Awaitable, Callable, Sequence
from datetime import date, datetime, timedelta

from broker.upstox_rate_limiter import UpstoxRateLimiter
from broker.upstox_rest import UpstoxRestClient
from config.loader import ConfigError, load_config
from config.settings import BotConfig, EffectiveRateLimits, WindowLimits
from core.clock import now_ist
from core.enums import TradingMode
from research.audit.data_quality import QualityThresholds, consecutive_clean, day_quality, render_text
from research.collect.archive import archive_recorded_quotes
from research.collect.chain import SnapshotResult, capture_contracts, run_chain_schedule, snapshot_chains
from research.collect.history import BulkDownloadBlockedError, download_daily, download_minutes, ensure_outside_window
from research.collect.quotes import summarize_recorded_quotes
from research.config import BlockedWindow, ResearchConfig, load_research_config
from research.forecast.analysis import full_report
from research.forecast.run import run_build
from research.reports.forecast_report import render_markdown
from research.sessions import SessionCalendar, weekly_expiries
from research.smoke import run_smoke
from research.sources import ReadOnlySource
from research.store import ResearchStore
from research.trading_readonly import (
    TradingDbUnavailableError,
    open_trading_db_readonly,
    raw_quotes,
    recorded_days,
    recorded_signals,
)

NETWORK_COMMANDS = frozenset({"smoke", "history", "contracts", "chain-snapshot", "chain-schedule"})
SESSION_ALLOWED_COMMANDS = frozenset({"contracts", "chain-snapshot", "chain-schedule"})


def network_guard(command: str, *, during_session: bool, now: datetime, window: BlockedWindow) -> str | None:
    """Why ``command`` may not call Upstox now, or ``None`` when it may."""
    if command not in NETWORK_COMMANDS:
        return None
    if during_session and command in SESSION_ALLOWED_COMMANDS:
        return None
    try:
        ensure_outside_window(now, window)
    except BulkDownloadBlockedError as exc:
        hint = " (or pass --during-session)" if command in SESSION_ALLOWED_COMMANDS else ""
        return f"{exc}{hint}"
    return None


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
                                queue_timeout=rcfg.queue_timeout_seconds)
    rest = UpstoxRestClient.from_config(cfg, limiter)
    try:
        return await work(ReadOnlySource(rest))
    finally:
        await rest.aclose()


def _to_date(value: str) -> date:
    return now_ist().date() if value.strip().lower() == "today" else date.fromisoformat(value)


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m research", description="Isolated research layer (read-only).")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    sub.add_parser("status")
    sub.add_parser("smoke")
    h = sub.add_parser("history")
    h.add_argument("--what", choices=("all", "daily", "minutes"), default="all")
    h.add_argument("--keys", default="nifty,vix", help="comma list of: nifty, vix")
    h.add_argument("--to", type=_to_date, default=None,
                   help="last date: YYYY-MM-DD or 'today' (default: yesterday)")
    for name in sorted(SESSION_ALLOWED_COMMANDS):
        sub.add_parser(name).add_argument("--during-session", action="store_true",
                                          help="allow during the weekday session window (a few calls)")
    q = sub.add_parser("summarize-quotes")
    q.add_argument("--day", type=date.fromisoformat, action="append", default=None)
    a = sub.add_parser("archive-quotes")
    a.add_argument("--day", type=date.fromisoformat, action="append", default=None)
    dq = sub.add_parser("data-quality")
    dq.add_argument("--day", type=date.fromisoformat, action="append", default=None,
                    help="day(s) to audit (default: every recorded day)")
    dq.add_argument("--json", action="store_true", help="print JSON instead of text")
    dq.add_argument("--expected-contracts", type=int, default=None,
                    help="option contracts the bot keeps subscribed (default: from the bot configuration)")
    sub.add_parser("forecast")
    sub.add_parser("report").add_argument("--json", action="store_true", help="print JSON instead of markdown")
    c = sub.add_parser("calendar")
    c.add_argument("--year", type=int, default=None)
    return p


def _status(store: ResearchStore) -> int:
    for table in ("daily_candles", "minute_candles", "contracts", "chain_snapshots", "chain_snapshot_rows",
                  "quote_minute_bars", "quote_archive", "archive_log", "fetch_log", "observations"):
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
                                         chunk_years=rcfg.history.daily_chunk_years, now=now_ist,
                                         final_after=rcfg.forecast.daily_final_after)
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
    if not cal.has_configured_year(year):
        print(f"  the bot config lists no {year} holidays: nothing to compare (the candle-derived list above is used)")
    for kind, days in cal.configured_mismatches(year).items():
        if days:
            print(f"  MISMATCH {kind}: {', '.join(map(str, days))}")
    observed = store.observed_expiries(rcfg.instruments.nifty_index_key)
    for e in weekly_expiries(date(year, 1, 1), date(year, 12, 31), rcfg.weekly_expiry_rules, cal, observed=observed):
        moved = f" (moved from {e.rule_date})" if e.shifted else ""
        print(f"  expiry {e.expiry} [{e.status}]{moved}")
    return 0


def expected_option_contracts(cfg: BotConfig) -> int:
    """Option contracts the bot keeps subscribed: nearest expiry ATM +/- strike_band and,
    when recorded, the next expiry ATM +/- next_expiry_band (CE and PE each)."""
    rec = cfg.settings.market_recording
    n = 2 * (2 * cfg.settings.instruments.strike_band + 1)
    if rec.enabled and rec.next_expiry_band > 0:
        n += 2 * (2 * rec.next_expiry_band + 1)
    return n


def _data_quality(args: argparse.Namespace, cfg: BotConfig, rcfg: ResearchConfig, store: ResearchStore,
                  trading: sqlite3.Connection) -> int:
    q = rcfg.data_quality
    th = QualityThresholds(quote_interval_seconds=q.quote_interval_seconds, min_completeness=q.min_completeness,
                           max_gap_seconds=q.max_gap_seconds, max_missing_share=q.max_missing_share,
                           abnormal_spread_pct=q.abnormal_spread_pct,
                           max_abnormal_spread_share=q.max_abnormal_spread_share,
                           stale_trade_seconds=q.stale_trade_seconds,
                           max_clock_offset_seconds=q.max_clock_offset_seconds,
                           edge_tolerance_seconds=q.edge_tolerance_seconds,
                           session_open=cfg.settings.market_session.market_open,
                           session_close=cfg.settings.market_session.market_close)
    index_key, vix_key = rcfg.instruments.nifty_index_key, rcfg.instruments.india_vix_key
    expected = args.expected_contracts or expected_option_contracts(cfg)
    days = sorted(set(args.day)) if args.day else recorded_days(trading, "option_quotes")
    if not days:
        print("no recorded quotes in the trading database yet")
        return 1
    reports = []
    for d in days:
        market = raw_quotes(trading, d, "market_quotes")
        futures = {str(r["instrument_key"]) for r in market} - {index_key, vix_key}
        rep = day_quality(d, raw_quotes(trading, d, "option_quotes"), market, recorded_signals(trading, d),
                          index_key=index_key, futures_keys=futures, vix_key=vix_key,
                          expected_option_contracts=expected, th=th)
        reports.append(rep)
        if not args.json:
            print("\n".join(render_text(rep)))
    streak = consecutive_clean(reports)
    summary = {"days": reports, "consecutive_clean": streak, "required": q.clean_sessions_required,
               "expected_option_contracts": expected}
    body = json.dumps(summary, indent=1, default=str)
    store.save_report("data_quality", body)
    if args.json:
        print(body)
    else:
        print(f"clean sessions in a row (most recent first): {streak} of {q.clean_sessions_required} required")
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
    blocked = network_guard(args.command, during_session=getattr(args, "during_session", False), now=now_ist(),
                            window=rcfg.history.blocked_window)
    if blocked is not None:
        print(blocked)
        return 3
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
    if cmd == "forecast":
        summary = run_build(store, rcfg, configured_holidays=cfg.settings.market_session.holidays, now=now_ist())
        for line in summary.lines():
            print(line)
        return 0 if summary.forecasts else 1
    if cmd == "report":
        if store.count("vol_forecasts") == 0:
            print("no forecast rows yet: run `python -m research forecast` first")
            return 1
        rep = full_report(store, rcfg.instruments.nifty_index_key, rcfg.instruments.india_vix_key,
                          block=rcfg.forecast.block_bootstrap)
        body = json.dumps(rep, indent=1, default=str)
        store.save_report("forecast_vs_reality", body)
        print(body if args.json else render_markdown(rep))
        return 0
    if cmd in ("summarize-quotes", "archive-quotes", "data-quality"):
        try:
            trading = open_trading_db_readonly(cfg.database_path)
        except TradingDbUnavailableError as exc:
            print(exc)
            return 1
        try:
            if cmd == "summarize-quotes":
                for d, n in summarize_recorded_quotes(trading, store, now=now_ist, days=args.day).items():
                    print(f"{d}: {n} one-minute quote bars")
                return 0
            if cmd == "archive-quotes":
                for (d, kind), n in archive_recorded_quotes(trading, store, now=now_ist, days=args.day).items():
                    print(f"{d} {kind}: {n:,} raw quotes archived")
                return 0
            return _data_quality(args, cfg, rcfg, store, trading)
        finally:
            trading.close()

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

    async def schedule(source: ReadOnlySource) -> int:
        def report(at: datetime, res: list[SnapshotResult] | str) -> None:
            if isinstance(res, str):
                print(f"{at:%H:%M} ERROR {res}", flush=True)
                return
            print(f"{at:%H:%M} " + "; ".join(f"{r.expiry}: " + (f"ERROR {r.error}" if r.error else f"{r.rows} rows")
                                             for r in res), flush=True)

        taken = await run_chain_schedule(
            rcfg.chain.schedule, now=now_ist, sleep=asyncio.sleep, report=report,
            take=lambda: snapshot_chains(source, store, rcfg.instruments.nifty_index_key, rcfg.chain, now_ist))
        print(f"{taken} scheduled snapshot set(s) taken today")
        return 0

    jobs: dict[str, Callable[[ReadOnlySource], Awaitable[int]]] = {
        "smoke": smoke, "contracts": contracts, "chain-snapshot": chains, "chain-schedule": schedule}
    return asyncio.run(_with_source(cfg, rcfg, jobs[cmd]))


if __name__ == "__main__":
    sys.exit(main())
