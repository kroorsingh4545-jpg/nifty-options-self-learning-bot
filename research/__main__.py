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
    shadow             counterfactual outcome + volatility evidence of every recorded signal (D2, D3); JSON stored
    intraday-vol       India VIX vs realised NIFTY variance over 15-60 minute windows, three clocks (D3); JSON stored
    regime-validate    regime + Momentum Pulse hypotheses, purged walk-forward by year on 2022+ history (D4)
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
from datetime import date, datetime, time, timedelta

import pandas as pd

from broker.charges import ChargeCalculator
from broker.upstox_rate_limiter import UpstoxRateLimiter
from broker.upstox_rest import UpstoxRestClient
from config.loader import ConfigError, load_config
from config.settings import BotConfig, EffectiveRateLimits, WindowLimits
from core.clock import IST, now_ist
from core.enums import TradingMode
from research.audit.data_quality import QualityThresholds, consecutive_clean, day_quality, render_text
from research.collect.archive import archive_recorded_quotes
from research.collect.chain import SnapshotResult, capture_contracts, run_chain_schedule, snapshot_chains
from research.collect.history import BulkDownloadBlockedError, download_daily, download_minutes, ensure_outside_window
from research.collect.quotes import summarize_recorded_quotes
from research.config import BlockedWindow, ResearchConfig, load_research_config
from research.forecast.analysis import full_report
from research.forecast.intraday import minute_closes_by_day, trailing_overnight_shares
from research.forecast.intraday import render_text as render_intraday
from research.forecast.intraday import study as intraday_study
from research.forecast.intraday import windows as intraday_windows
from research.forecast.run import run_build
from research.regime.orb import OrbRule
from research.regime.pulse import Cutoffs
from research.regime.pulse import fit as fit_pulse
from research.regime.regime import RegimeSettings
from research.regime.samples import DailyHistory, Sample, SampleSettings, build_samples
from research.regime.shadow import RegimeContext, regime_rows
from research.regime.validate import evaluate as validate_hypotheses
from research.regime.validate import render_text as render_validation
from research.reports.forecast_report import render_markdown
from research.sessions import SessionCalendar, weekly_expiries
from research.shadow.calibrate import ActualTrade
from research.shadow.evidence import VolContext, evidence_rows, gap_weights, weekend_gap_weight
from research.shadow.report import evidence_summary, render_evidence, render_regime, summarize
from research.shadow.report import render_text as render_shadow
from research.shadow.rules import TradeRules
from research.shadow.run import ShadowInputs, evaluate
from research.shadow.signals import from_log_records, from_table_rows, merge, parse_log_lines
from research.shadow.simulate import Contract, Quote
from research.smoke import run_smoke
from research.sources import ReadOnlySource
from research.store import ResearchStore
from research.trading_readonly import (
    TradingDbUnavailableError,
    closed_trades,
    index_close_at,
    index_minute_bars,
    index_minute_closes,
    open_trading_db_readonly,
    raw_quotes,
    recorded_days,
    recorded_signals,
    signal_rows,
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
    sh = sub.add_parser("shadow")
    sh.add_argument("--day", type=date.fromisoformat, action="append", default=None,
                    help="only signals of these day(s) (default: every recorded signal)")
    sh.add_argument("--json", action="store_true", help="print JSON instead of text")
    sh.add_argument("--no-log", action="store_true", help="do not rebuild pre-v7 signals from the bot's JSON log")
    sub.add_parser("intraday-vol").add_argument("--json", action="store_true", help="print JSON instead of text")
    sub.add_parser("regime-validate").add_argument("--json", action="store_true", help="print JSON instead of text")
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


def shadow_rules(cfg: BotConfig, tick_size: float) -> dict[str, TradeRules]:
    """Each strategy's trade rules as plain values (the bot configuration is read
    here, in the composition root, and nowhere else in research). The strike step
    is the bot's universe step (``OptionUniverse`` default, 50), which is what the
    resolver selects on."""
    s = cfg.settings
    out = {}
    for sid, st in cfg.strategies.strategies.items():
        ss = st.strike_selection
        out[sid] = TradeRules(
            stop_mode=st.stop_loss.mode, stop_value=st.stop_loss.value, target_mode=st.take_profit.mode,
            target_value=st.take_profit.value, trailing_enabled=st.trailing.enabled, trailing_mode=st.trailing.mode,
            trailing_activation=st.trailing.activation, trailing_step=st.trailing.step, strike_mode=ss.mode,
            strike_offset=ss.offset_steps, min_premium=ss.min_premium, max_premium=ss.max_premium,
            min_volume=ss.liquidity.min_volume, min_oi=ss.liquidity.min_oi,
            max_spread_pct=ss.liquidity.max_spread_pct, tick_size=tick_size,
            slippage_ticks=s.paper_broker.slippage_ticks, entry_buffer_ticks=s.execution.entry_limit_buffer_ticks,
            entry_timeout_seconds=s.execution.entry_timeout_seconds,
            max_quote_age_seconds=s.paper_broker.max_quote_age_seconds,
            square_off_start=cfg.risk.time_rules.square_off_start)
    return out


def _ms(at: datetime) -> int:
    return int(at.timestamp() * 1000)


def _from_ms(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=IST)


def _log_records(cfg: BotConfig) -> list[dict[str, object]]:
    """The bot's JSON log lines (current file and any rotated ones), read-only, in time order."""
    name = cfg.settings.logging.file_name
    files = sorted(cfg.log_directory.glob(f"{name}*")) if cfg.log_directory.is_dir() else []
    recs = [r for f in files for r in parse_log_lines(f.read_text(encoding="utf-8", errors="replace").splitlines())]
    return sorted(recs, key=lambda r: str(r.get("ts", "")))


def _shadow(args: argparse.Namespace, cfg: BotConfig, rcfg: ResearchConfig, store: ResearchStore,
            trading: sqlite3.Connection) -> int:
    sc = rcfg.shadow
    index_key = rcfg.instruments.nifty_index_key
    table = from_table_rows(signal_rows(trading))
    records = [] if args.no_log else _log_records(cfg)
    log, logged = from_log_records(records)
    signals = merge(table, log, logged)
    known = {x.signal_id for x in signals}
    unrebuilt = sorted({str(r["signal_id"]) for r in records
                        if r.get("event") == "signal processed" and r.get("signal_id")} - known)
    if args.day:
        days = set(args.day)
        signals = [x for x in signals if x.generated_at.date() in days]
    if not signals:
        print("no recorded signals (signals table or bot log) for the chosen day(s)")
        return 1
    contracts = [Contract(k, str(c["instrument_type"]), float(c["strike"]), date.fromisoformat(str(c["expiry"])),
                          int(c["lot_size"]))
                 for k, c in store.contracts().items()
                 if c["instrument_type"] in ("CE", "PE") and c["strike"] is not None and c["expiry"]
                 and c["underlying_key"] == index_key]

    def quotes(key: str, start: datetime, end: datetime) -> list[Quote]:
        return [Quote(_from_ms(r["received_ms"]), _from_ms(r["exchange_ms"]), r["ltp"], r["bid"], r["ask"],
                      r["bid_qty"], r["ask_qty"], r["volume_today"], r["oi"])
                for r in store.archived_quotes(key, _ms(start), _ms(end))]

    def spot(at: datetime) -> tuple[float, str] | None:
        q = store.last_archived_quote(index_key, _ms(at), max_age_ms=int(sc.spot_max_age_seconds * 1000))
        if q is not None:
            return float(q["ltp"]), "INDEX_QUOTE"
        c = index_close_at(trading, index_key, at, timedelta(seconds=sc.spot_max_age_seconds * 2))
        return None if c is None else (c[0], "INDEX_1M_CLOSE")

    inp = ShadowInputs(rules=shadow_rules(cfg, sc.option_tick_size), contracts=contracts, quotes=quotes, spot=spot,
                       cost_fn=ChargeCalculator(cfg.settings.costs).round_trip,
                       trades=[ActualTrade.from_row(r) for r in closed_trades(trading)], sim_version=sc.sim_version,
                       entry_tolerance_ticks=sc.entry_tolerance_ticks, exit_tolerance_ticks=sc.exit_tolerance_ticks)
    rows, calib = evaluate(signals, inp)
    store.upsert_shadow(rows, at=now_ist())
    summary = summarize(rows, calib)
    evidence = evidence_rows(rows, _vol_context(cfg, rcfg, store, trading, inp.cost_fn))
    store.upsert_evidence(evidence, at=now_ist())
    ev_summary = evidence_summary(evidence, rows)
    regimes = regime_rows(rows, _regime_context(cfg, rcfg, store, trading))
    store.upsert_regime(regimes, at=now_ist())
    body = json.dumps({"sim_version": sc.sim_version, "evidence_version": rcfg.volatility.evidence_version,
                       "summary": summary, "calibration": calib, "evidence_summary": ev_summary, "outcomes": rows,
                       "evidence": evidence, "regime": regimes, "unrebuilt_log_signals": unrebuilt}, indent=1,
                      default=str)
    store.save_report("shadow", body)
    print(body if args.json else "\n".join(render_shadow(summary, rows, calib)
                                              + render_evidence(ev_summary, evidence, rows)
                                              + render_regime(regimes, rows)))
    if unrebuilt and not args.json:
        print(f"{len(unrebuilt)} logged signal(s) could not be rebuilt (no per-candle decision line or no "
              f"direction in the log, e.g. a session before per-candle decision logging): {', '.join(unrebuilt)}")
    return 0


def _nifty_daily(store: ResearchStore, key: str) -> pd.DataFrame:
    d = pd.read_sql("SELECT session_date, open AS o, close AS c FROM daily_candles WHERE instrument_key = ?"
                    " AND complete = 1 ORDER BY session_date", store.con, params=(key,))
    d["session_date"] = pd.to_datetime(d["session_date"]).dt.date
    return d


def _vol_context(cfg: BotConfig, rcfg: ResearchConfig, store: ResearchStore, trading: sqlite3.Connection,
                 cost_fn: Callable[[int, float, float], float]) -> VolContext:
    v, index_key = rcfg.volatility, rcfg.instruments.nifty_index_key
    shares = trailing_overnight_shares(_nifty_daily(store, index_key), lookback=v.overnight_lookback_sessions,
                                       reliable_from=rcfg.forecast.reliable_open_from,
                                       default=v.default_overnight_share)

    def share(day: date) -> float:
        return shares.get(day, v.default_overnight_share)

    def weights(day: date, expiry: date) -> list[float]:
        return gap_weights(day, expiry, cfg.settings.market_session.is_trading_day,
                           weekend_gap_weight(v.weekend_variance_multiple, share(day)))

    def option_quote(key: str, at: datetime) -> dict[str, object] | None:
        r = store.last_archived_quote(key, _ms(at), kind="OPT", max_age_ms=int(v.quote_max_age_seconds * 1000))
        return None if r is None else dict(r)

    def closes(start: datetime, end: datetime) -> list[float]:
        bars = index_minute_closes(trading, index_key, start, end) or store.minute_closes(index_key, start, end)
        return [c for _, c in bars]

    return VolContext(r=v.risk_free_rate, q=v.dividend_yield, horizon_minutes=v.horizon_minutes,
                      square_off=cfg.risk.time_rules.square_off_start, tick_size=rcfg.shadow.option_tick_size,
                      slippage_ticks=cfg.settings.paper_broker.slippage_ticks, evidence_version=v.evidence_version,
                      overnight_share=share, gap_weights=weights, option_quote=option_quote, index_closes=closes,
                      cost_fn=cost_fn)


def _intraday_vol(args: argparse.Namespace, rcfg: ResearchConfig, store: ResearchStore) -> int:
    v = rcfg.volatility

    def minutes(key: str) -> pd.DataFrame:
        return pd.read_sql("SELECT start_time, close FROM minute_candles WHERE instrument_key = ? AND"
                           " interval_minutes = 1 AND complete = 1 ORDER BY start_time", store.con, params=(key,))

    nifty, vix = minutes(rcfg.instruments.nifty_index_key), minutes(rcfg.instruments.india_vix_key)
    if nifty.empty or vix.empty:
        print("no NIFTY / India VIX 1-minute history yet: run `python -m research history` first")
        return 1
    shares = trailing_overnight_shares(_nifty_daily(store, rcfg.instruments.nifty_index_key),
                                       lookback=v.overnight_lookback_sessions,
                                       reliable_from=rcfg.forecast.reliable_open_from,
                                       default=v.default_overnight_share)
    df = intraday_windows(minute_closes_by_day(nifty), minute_closes_by_day(vix), shares,
                          checkpoints=v.intraday_checkpoints, horizons=v.intraday_horizons,
                          default_share=v.default_overnight_share)
    rep = intraday_study(df, block=rcfg.forecast.block_bootstrap)
    body = json.dumps(rep, indent=1, default=str)
    store.save_report("intraday_vol", body)
    print(body if args.json else "\n".join(render_intraday(rep)))
    return 0 if rep.get("windows") else 1


def sample_settings(cfg: BotConfig, rcfg: ResearchConfig) -> SampleSettings:
    """Sample windows from research.yaml; the ORB rule from the bot's orb_options configuration."""
    g = rcfg.regime
    orb = cfg.strategies.strategies["orb_options"]
    p = orb.params
    rule = OrbRule(range_start=_hhmm(p.get("opening_range_start", "09:15")),
                   range_end=_hhmm(p.get("opening_range_end", "09:30")), window_start=orb.entry_window.start,
                   window_end=orb.entry_window.end, buffer_points=float(p.get("breakout_buffer_points", 5.0)),
                   min_range_points=float(p.get("min_range_points", 10.0)),
                   max_range_pct=float(p.get("max_range_pct", 1.5)),
                   max_entries=int(p.get("max_entries_per_direction_per_day", 1)),
                   timeframe=orb.timeframe_minutes, session_open=cfg.settings.market_session.market_open)
    return SampleSettings(checkpoints=g.checkpoints, horizon=g.horizon_minutes, lookback=g.lookback_bars,
                          adx_period=g.adx_period, atr_short=g.atr_short, atr_long=g.atr_long, orb=rule,
                          regime=RegimeSettings(lookback=g.vix_lookback_sessions, trend_sessions=g.trend_sessions))


def _hhmm(v: object) -> time:
    if isinstance(v, time):
        return v
    h, m = str(v).split(":")
    return time(int(h), int(m))


def _history_samples(cfg: BotConfig, rcfg: ResearchConfig, store: ResearchStore) -> list[Sample]:
    """Every checkpoint and ORB sample of the stored 1-minute NIFTY history (point in time)."""
    nkey, vkey = rcfg.instruments.nifty_index_key, rcfg.instruments.india_vix_key
    m = pd.read_sql("SELECT start_time, open, high, low, close FROM minute_candles WHERE instrument_key = ? AND"
                    " interval_minutes = 1 AND complete = 1 ORDER BY start_time", store.con, params=(nkey,))
    bars: dict[date, dict[time, tuple[float, float, float, float]]] = {}
    for st, o, h, lo, c in m.itertuples(index=False):
        t = datetime.fromisoformat(str(st))
        bars.setdefault(t.date(), {})[t.time()] = (float(o), float(h), float(lo), float(c))
    vix_m = pd.read_sql("SELECT start_time, close FROM minute_candles WHERE instrument_key = ? AND"
                        " interval_minutes = 1 AND complete = 1 ORDER BY start_time", store.con, params=(vkey,))
    nd = _nifty_daily(store, nkey)
    vd = pd.read_sql("SELECT session_date, close FROM daily_candles WHERE instrument_key = ? AND complete = 1"
                     " ORDER BY session_date", store.con, params=(vkey,))
    daily = DailyHistory([date.fromisoformat(str(d)) for d in nd["session_date"]], nd["o"].tolist(),
                         nd["c"].tolist(), [date.fromisoformat(str(d)) for d in vd["session_date"]],
                         vd["close"].tolist())
    v = rcfg.volatility
    shares = trailing_overnight_shares(nd, lookback=v.overnight_lookback_sessions,
                                       reliable_from=rcfg.forecast.reliable_open_from,
                                       default=v.default_overnight_share)
    sessions = store.daily_sessions(nkey)
    cal = SessionCalendar.from_sessions(sessions, configured_holidays=cfg.settings.market_session.holidays)
    expiries = {e.expiry for e in weekly_expiries(sessions[0], sessions[-1], rcfg.weekly_expiry_rules, cal,
                                                  observed=store.observed_expiries(nkey))} if sessions else set()
    return build_samples(bars, minute_closes_by_day(vix_m), daily, shares, expiries, sample_settings(cfg, rcfg),
                         tz=IST, default_share=v.default_overnight_share)


def _regime_context(cfg: BotConfig, rcfg: ResearchConfig, store: ResearchStore,
                    trading: sqlite3.Connection) -> RegimeContext:
    nkey, vkey = rcfg.instruments.nifty_index_key, rcfg.instruments.india_vix_key
    history = _history_samples(cfg, rcfg, store)
    checkpoints = [s for s in history if s.kind == "CHECKPOINT"]
    cache: dict[date, Cutoffs | None] = {}

    def cutoffs_for(day: date) -> Cutoffs | None:
        if day not in cache:
            try:
                cache[day] = fit_pulse([s.features for s in checkpoints if s.day < day],
                                       min_train=rcfg.regime.min_train)
            except ValueError:
                cache[day] = None
        return cache[day]

    def bars_for(day: date) -> dict[time, tuple[float, float, float, float]]:
        return index_minute_bars(trading, nkey, day) or store.minute_bars(nkey, day)

    def vix_at(at: datetime) -> float | None:
        q = store.last_archived_quote(vkey, _ms(at), kind="MKT", max_age_ms=120_000)
        if q is not None:
            return float(q["ltp"])
        bars = store.minute_closes(vkey, at - timedelta(minutes=5), at)
        return bars[-1][1] if bars else None

    nd = _nifty_daily(store, nkey)
    vd = pd.read_sql("SELECT session_date, close FROM daily_candles WHERE instrument_key = ? AND complete = 1"
                     " ORDER BY session_date", store.con, params=(vkey,))
    daily = DailyHistory([date.fromisoformat(str(d)) for d in nd["session_date"]], nd["o"].tolist(),
                         nd["c"].tolist(), [date.fromisoformat(str(d)) for d in vd["session_date"]],
                         vd["close"].tolist())
    sessions = store.daily_sessions(nkey)
    cal = SessionCalendar.from_sessions(sessions, configured_holidays=cfg.settings.market_session.holidays)
    expiries = {e.expiry for e in weekly_expiries(sessions[0], max(sessions[-1], now_ist().date() + timedelta(days=14)),
                                                  rcfg.weekly_expiry_rules, cal,
                                                  observed=store.observed_expiries(nkey))} if sessions else set()
    latest = store.latest_report("regime_validation")
    kept = json.loads(latest).get("kept", []) if latest else []
    return RegimeContext(sample_settings(cfg, rcfg), rcfg.regime.regime_version, daily, expiries, bars_for, vix_at,
                         cutoffs_for, kept)


def _regime_validate(args: argparse.Namespace, cfg: BotConfig, rcfg: ResearchConfig, store: ResearchStore) -> int:
    samples = _history_samples(cfg, rcfg, store)
    if not samples:
        print("no NIFTY 1-minute history yet: run `python -m research history` first")
        return 1
    g = rcfg.regime
    rep = validate_hypotheses(samples, required_years=g.required_years, min_group=g.min_group,
                              min_train=g.min_train, n_boot=g.n_boot)
    rep["regime_version"] = g.regime_version
    rep["samples"] = {"CHECKPOINT": sum(1 for s in samples if s.kind == "CHECKPOINT"),
                      "ORB": sum(1 for s in samples if s.kind == "ORB"),
                      "from": str(samples[0].day), "to": str(samples[-1].day)}
    body = json.dumps(rep, indent=1, default=str)
    store.save_report("regime_validation", body)
    print(body if args.json else "\n".join(render_validation(rep)))
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
    if cmd == "intraday-vol":
        return _intraday_vol(args, rcfg, store)
    if cmd == "regime-validate":
        return _regime_validate(args, cfg, rcfg, store)
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
    if cmd in ("summarize-quotes", "archive-quotes", "data-quality", "shadow"):
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
            if cmd == "shadow":
                return _shadow(args, cfg, rcfg, store, trading)
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
