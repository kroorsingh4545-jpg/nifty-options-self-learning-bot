"""Run the Reflective Loop.

    python -m learning synthetic --lot-units 65 --paper-days 8 --verify-days 10 --out reports/learning-synthetic
    python -m learning run --data path/to/historical-dataset     # on the trading database

``run`` reflects on the trading database's journal and verifies candidates
on a HISTORICAL dataset (refused otherwise). Its autonomy level is the
RUNNING bot's, read from the bot's heartbeat at every promotion step, so a
dashboard switch to OBSERVE_ONLY stops it at once; with no fresh heartbeat
it uses the level the bot would start with. It refuses to run while the
market is open. Deployments apply at the next PAPER session start.

``synthetic`` (research / demo only):

1. Generates a SYNTHETIC dataset (NOT real NIFTY prices).
2. Runs the real PAPER bot over the first ``--paper-days`` into a fresh
   research database under ``learning.research_dir`` (never the trading
   database). The trade journal labels every row SYNTHETIC.
3. Runs the loop: reflect on that journal, hypothesize, mutate (whitelisted
   only), verify on the LATER ``--verify-days`` (no overlap with what the
   hypotheses were derived from), and decide.
4. Writes ``learning_report.md`` / ``.json`` (both say SYNTHETIC on top).

Because the evidence is SYNTHETIC, nothing can be promoted: candidates that
pass every gate stop at VERIFIED. LIVE is never reachable.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path

from backtest.backtester import Backtester, BacktestOptions
from backtest.synthetic_data import SyntheticSpec, generate_synthetic_dataset
from config.loader import load_config
from core.clock import IST, now_ist
from core.enums import AutonomyLevel, TradingMode
from data.storage.database import Database
from data.storage.repositories import Repositories
from learning.autonomy_source import PublishedAutonomy
from learning.loop import LoopRefused, ReflectiveLoop, load_verification_data, render_loop_markdown
from learning.validation_engine import ValidationEngine, ValidationPlan
from monitoring.json_logger import configure_logging


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m learning", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("synthetic", help="run the loop on SYNTHETIC data (research only)")
    s.add_argument("--lot-units", type=int, required=True,
                   help="contract size for the SYNTHETIC instrument list (no default)")
    s.add_argument("--start", default="2026-09-01")
    s.add_argument("--paper-days", type=int, default=8, help="days the PAPER bot trades (the journal)")
    s.add_argument("--verify-days", type=int, default=10, help="later days used only to VERIFY candidates")
    s.add_argument("--seed", type=int, default=7)
    s.add_argument("--holdout-days", type=int, default=2)
    s.add_argument("--min-trades", type=int, help="override the minimum OOS sample (synthetic demos are short)")
    s.add_argument("--autonomy", choices=[a.value for a in AutonomyLevel],
                   help="autonomy level for this run (default: app.autonomy_level)")
    s.add_argument("--out", required=True, type=Path)
    s.add_argument("--config-dir", type=Path)
    s.add_argument("--log-level", default="ERROR")
    r = sub.add_parser("run", help="run the loop on the TRADING database (HISTORICAL verification data only)")
    r.add_argument("--data", required=True, type=Path, help="HISTORICAL dataset directory used to VERIFY")
    r.add_argument("--end", help="last journal day to reflect on (default: today)")
    r.add_argument("--out", type=Path, help="report directory (default: learning.loop.reports_dir)")
    r.add_argument("--heartbeat-max-age", type=float, default=10.0,
                   help="seconds; an older bot heartbeat means the bot is not running")
    r.add_argument("--config-dir", type=Path)
    r.add_argument("--log-level", default="WARNING")
    return p


async def run_trading(args: argparse.Namespace) -> Path:
    """The loop on the trading database, next to (or without) a running bot.
    The autonomy level is the RUNNING bot's, read from its heartbeat at every
    promotion step (persisted level when the bot is not running)."""
    cfg = load_config(args.config_dir)
    if cfg.mode is not TradingMode.PAPER:
        raise SystemExit("the learning loop runs in PAPER mode only")
    now = now_ist()
    if cfg.settings.market_session.is_open(now):
        raise SystemExit("refusing to run the learning loop while the market is open")
    try:
        ds = load_verification_data(args.data)
    except LoopRefused as exc:
        raise SystemExit(str(exc)) from None
    end = date.fromisoformat(args.end) if args.end else now.date()
    start = end - timedelta(days=cfg.settings.learning.reflection_window_days - 1)
    db = Database.open_existing(cfg.database_path)
    try:
        repos = Repositories(db, TradingMode.PAPER)
        autonomy = PublishedAutonomy(repos, cfg, max_age_seconds=args.heartbeat_max_age)
        print(f"autonomy now: {autonomy().value} ({autonomy.last_source}); read again at every promotion step")
        loop = ReflectiveLoop(cfg, repos, validation=lambda prod: ValidationEngine(prod, ds), autonomy_fn=autonomy)
        since = datetime.combine(start, time(0, 0), tzinfo=IST)
        until = datetime.combine(end + timedelta(days=1), time(0, 0), tzinfo=IST)
        res = await loop.run(repos.journal.list(since=since, until=until), start, end)
    finally:
        db.close()
    out: Path = args.out or cfg.resolve_path(cfg.settings.learning.loop.reports_dir)
    out.mkdir(parents=True, exist_ok=True)
    stem = f"loop-{end.isoformat()}-{res.run_uid}"
    (out / f"{stem}.md").write_text(render_loop_markdown(res), encoding="utf-8")
    (out / f"{stem}.json").write_text(json.dumps(res.to_dict(), indent=2, default=str), encoding="utf-8")
    return out / f"{stem}.md"


async def run_synthetic(args: argparse.Namespace) -> Path:
    cfg = load_config(args.config_dir, use_default_env_file=False)
    if cfg.mode is not TradingMode.PAPER:
        raise SystemExit("the learning loop runs in PAPER mode only")
    level = AutonomyLevel(args.autonomy or cfg.settings.app.autonomy_level)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")  # noqa: DTZ005 - a directory name only
    run_dir = cfg.resolve_path(cfg.settings.learning.research_dir) / f"synthetic-{stamp}"
    db_path = run_dir / "learning.db"
    if db_path.resolve() == cfg.database_path.resolve():
        raise SystemExit("refusing to use the trading database for a research run")
    spec = SyntheticSpec(lot_units=args.lot_units, start=date.fromisoformat(args.start),
                         days=args.paper_days + args.verify_days, seed=args.seed)
    ds = generate_synthetic_dataset(spec, cfg.settings.market_session)
    days = ds.days()
    paper, verify = ds.slice(days[:args.paper_days]), ds.slice(days[args.paper_days:])
    run_dir.mkdir(parents=True, exist_ok=True)
    await Backtester(cfg, paper, BacktestOptions(label="synthetic-paper-journal", database_path=db_path)).run()
    db = Database.open_existing(db_path)
    try:
        repos = Repositories(db, TradingMode.PAPER)
        plan = ValidationPlan(holdout_days=args.holdout_days, min_trades=args.min_trades)
        loop = ReflectiveLoop(cfg, repos, validation=lambda prod: ValidationEngine(prod, verify, plan=plan),
                              autonomy_fn=lambda: level)
        res = await loop.run(repos.journal.list(), days[0], days[args.paper_days - 1])
    finally:
        db.close()
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    (out / "learning_report.md").write_text(render_loop_markdown(res), encoding="utf-8")
    (out / "learning_report.json").write_text(json.dumps(res.to_dict(), indent=2, default=str), encoding="utf-8")
    return out


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    configure_logging(level=args.log_level, console=True)
    if args.cmd == "run":
        report = asyncio.run(run_trading(args))
        print(f"learning-loop report: {report} (deployments take effect at the next PAPER session start)")
        return 0
    out = asyncio.run(run_synthetic(args))
    print(f"SYNTHETIC learning-loop report: {out / 'learning_report.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
