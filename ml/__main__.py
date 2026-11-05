"""ML command line. Never connects to Upstox. Never deploys anything.

    # SYNTHETIC research run (random-walk data, NOT real prices). Always goes
    # to the research registry (settings ml.research_dir), never the trading DB.
    python -m ml train --synthetic --lot-units 65 --days 30 --strategy orb_options --kind all

    # A HISTORICAL dataset directory (backtest/data.py format). Its meta.json
    # must state "source" explicitly (a dataset without one is refused), and a
    # HISTORICAL dataset with SYNTH-tagged instruments is refused. Registers into
    # the trading database (the bot must have created it) as TRAINED, or
    # VALIDATED when every gate passes. Promotion is a separate, human step.
    python -m ml train --data path/to/dataset --strategy vwap_ema --kind signal_filter --events strategy

    # List registered runs
    python -m ml list [--research]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from backtest.data import BacktestDataError, HistoricalDataset, load_dataset
from backtest.synthetic_data import SyntheticSpec, generate_synthetic_dataset
from config.loader import load_config
from config.settings import BotConfig
from core.enums import TradingMode
from data.storage.bootstrap import sync_strategies
from data.storage.database import Database
from data.storage.repositories import Repositories
from ml.dataset import build_training_set, grid_events, strategy_events
from ml.model_registry import ModelRegistry
from ml.training_pipeline import TrainingConfig, TrainingPipeline

KIND_CHOICES = ("signal_filter", "outcome_predictor", "regime_classifier", "all")


def research_registry(cfg: BotConfig) -> ModelRegistry:
    """The SYNTHETIC / research registry: its own database and artefact folder."""
    root = cfg.resolve_path(cfg.settings.ml.research_dir)
    path = root / "research_registry.db"
    if path.resolve() == cfg.database_path.resolve():
        raise SystemExit("refusing to use the trading database as the research registry")
    db = Database(path)
    db.initialize()  # our own research database, not the bot's
    repos = Repositories(db, TradingMode.PAPER)
    sync_strategies(repos.strategies, cfg.strategies.strategies)
    return ModelRegistry(repos.model_runs, root / "models", allow_synthetic=True)


def trading_registry(cfg: BotConfig) -> ModelRegistry:
    """HISTORICAL models only, in the bot's database (never created or migrated here)."""
    db = Database.open_existing(cfg.database_path)
    return ModelRegistry(Repositories(db, TradingMode.PAPER).model_runs, cfg.resolve_path(cfg.settings.ml.artifact_dir))


def _dataset(args: argparse.Namespace, cfg: BotConfig) -> HistoricalDataset:
    if args.synthetic:
        if not args.lot_units:
            raise SystemExit("synthetic data needs --lot-units (the contract size for the SYNTHETIC instrument list)")
        return generate_synthetic_dataset(SyntheticSpec(lot_units=args.lot_units, start=date.fromisoformat(args.start),
                                                        days=args.days, seed=args.seed),
                                          cfg.settings.market_session)
    if not args.data:
        raise SystemExit("give --data <dataset dir> or --synthetic")
    try:
        # The source label decides which registry a model goes to, so it must be
        # stated in meta.json (never defaulted), and HISTORICAL data must not
        # contain SYNTH-tagged instruments (enforced by HistoricalDataset).
        return load_dataset(Path(args.data), require_source=True)
    except BacktestDataError as exc:
        raise SystemExit(f"refusing dataset {args.data}: {exc}") from exc


def _train(args: argparse.Namespace) -> int:
    cfg = load_config(Path(args.config_dir) if args.config_dir else None, use_default_env_file=False)
    ds = _dataset(args, cfg)
    registry = research_registry(cfg) if ds.is_synthetic else trading_registry(cfg)
    events = strategy_events(cfg, ds, args.strategy) if args.events == "strategy" else \
        grid_events(ds.days(), strategy_id=args.strategy, every_minutes=args.every)
    ts = build_training_set(cfg, ds, events, strategy_id=args.strategy, max_hold_minutes=args.max_hold)
    print(f"{ts.data_source} training set: {json.dumps(ts.summary(), default=str)}")
    kinds = ("signal_filter", "outcome_predictor", "regime_classifier") if args.kind == "all" else (args.kind,)
    pipe = TrainingPipeline(cfg, registry)
    for kind in kinds:
        r = pipe.run(ts, TrainingConfig(kind=kind, n_splits=args.folds,  # type: ignore[arg-type]
                                        embargo_minutes=args.embargo_minutes, holdout_fraction=args.holdout))
        reg = r.registered
        print(f"\n{kind}: {r.status.value} (gates passed: {r.gates_passed})")
        print(f"  headline: {json.dumps(r.evaluation['headline'], default=str)}")
        if reg is not None:
            print(f"  registered {reg.record.model_version} [{reg.record.data_source.value}] -> {reg.directory}")
            print(f"  report: {reg.directory / 'evaluation_report.md'}")
    if ds.is_synthetic:
        print("\nSYNTHETIC data: these models are research artefacts and can never be validated or promoted.")
    return 0


def _list(args: argparse.Namespace) -> int:
    cfg = load_config(Path(args.config_dir) if args.config_dir else None, use_default_env_file=False)
    reg = research_registry(cfg) if args.research else trading_registry(cfg)
    for r in reg.runs.list():
        print(f"{r.run_id}  {r.model_type.value:<18} {r.strategy_id or '-':<12} {r.status.value:<14} "
              f"{r.data_source.value:<10} {r.model_version}")
    return 0


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m ml", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)
    t = sub.add_parser("train", help="build a training set and train / evaluate / register models")
    t.add_argument("--config-dir")
    t.add_argument("--data", help="HISTORICAL dataset directory")
    t.add_argument("--synthetic", action="store_true", help="generate SYNTHETIC random-walk data instead")
    t.add_argument("--lot-units", type=int, help="contract size for the SYNTHETIC instrument list")
    t.add_argument("--days", type=int, default=30)
    t.add_argument("--seed", type=int, default=7)
    t.add_argument("--start", default="2026-07-01")
    t.add_argument("--strategy", required=True)
    t.add_argument("--kind", choices=KIND_CHOICES, default="all")
    t.add_argument("--events", choices=("grid", "strategy"), default="grid",
                   help="grid: candidate setups every --every minutes; strategy: the strategy's own signals")
    t.add_argument("--every", type=int, default=5)
    t.add_argument("--max-hold", type=int, default=60, help="time barrier, minutes")
    t.add_argument("--folds", type=int, default=5)
    t.add_argument("--embargo-minutes", type=int, default=60)
    t.add_argument("--holdout", type=float, default=0.2, help="fraction of days kept as the final holdout")
    ls = sub.add_parser("list", help="list registered model runs")
    ls.add_argument("--config-dir")
    ls.add_argument("--research", action="store_true", help="the SYNTHETIC / research registry")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    return _train(args) if args.command == "train" else _list(args)


if __name__ == "__main__":
    sys.exit(main())
