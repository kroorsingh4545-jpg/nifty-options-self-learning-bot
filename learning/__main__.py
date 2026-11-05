"""Run the Reflective Loop on SYNTHETIC data (research / demo only).

    python -m learning synthetic --lot-units 65 --paper-days 8 --verify-days 10 --out reports/learning-synthetic

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
from datetime import date, datetime
from pathlib import Path

from backtest.backtester import Backtester, BacktestOptions
from backtest.synthetic_data import SyntheticSpec, generate_synthetic_dataset
from config.loader import load_config
from core.enums import AutonomyLevel, TradingMode
from data.storage.database import Database
from data.storage.repositories import Repositories
from learning.loop import ReflectiveLoop, render_loop_markdown
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
    return p


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
    out = asyncio.run(run_synthetic(args))
    print(f"SYNTHETIC learning-loop report: {out / 'learning_report.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
