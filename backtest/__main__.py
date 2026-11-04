"""Backtest command line. Never connects to Upstox; never touches the trading DB.

    # SYNTHETIC demo data (clearly labelled in the report). --lot-units is the
    # contract size written into the synthetic instrument list (no default).
    python -m backtest synthetic --lot-units 65 --days 20 --seed 7 --out reports/backtest-synthetic
    python -m backtest synthetic --lot-units 65 --days 20 --save-data data/dev/synthetic --out reports/s

    # Your own historical data (see backtest/data.py for the format)
    python -m backtest run --data path/to/dataset --out reports/run1 [--strategies orb_options]
                           [--slippage-ticks 2]

    # Walk-forward validation (chronological, embargo, untouched holdout)
    python -m backtest walk-forward --data path/to/dataset --train 10 --test 5 --embargo 1 --holdout 5 \\
                           --out reports/wf1
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import fields
from datetime import date
from pathlib import Path

from backtest.backtester import Backtester, BacktestOptions, with_settings_overrides
from backtest.cost_model import SpreadModel
from backtest.data import HistoricalDataset, load_dataset, save_dataset
from backtest.report_generator import build_report, render_walk_forward_markdown, write_report
from backtest.synthetic_data import SyntheticSpec, generate_synthetic_dataset
from backtest.walk_forward import WalkForwardRunner, walk_forward_splits
from config.loader import load_config
from config.settings import BotConfig
from monitoring.json_logger import configure_logging


def _config(args: argparse.Namespace) -> BotConfig:
    cfg = load_config(Path(args.config_dir) if args.config_dir else None, use_default_env_file=False)
    if args.slippage_ticks is not None:
        cfg = with_settings_overrides(cfg, {"paper_broker": {"slippage_ticks": args.slippage_ticks}})
    return cfg


def _dataset(args: argparse.Namespace, cfg: BotConfig) -> HistoricalDataset:
    if getattr(args, "data", None):
        return load_dataset(Path(args.data))
    if not args.lot_units:
        raise SystemExit("synthetic data needs --lot-units (the contract size for the SYNTHETIC instrument list)")
    spec = SyntheticSpec(lot_units=args.lot_units, start=date.fromisoformat(args.start), days=args.days,
                         seed=args.seed)
    ds = generate_synthetic_dataset(spec, cfg.settings.market_session)
    if args.save_data:
        print(f"SYNTHETIC dataset saved to {save_dataset(ds, Path(args.save_data))}")
    return ds


def _options(args: argparse.Namespace, label: str) -> BacktestOptions:
    def progress(d: date, n: int, total: int) -> None:
        print(f"  [{n}/{total}] {d}", file=sys.stderr, flush=True)

    return BacktestOptions(label=label, spread=SpreadModel(half_spread_pct=args.half_spread_pct),
                           strategies=tuple(args.strategies) if args.strategies else None,
                           progress=None if args.quiet else progress)


async def _single(args: argparse.Namespace) -> int:
    cfg = _config(args)
    ds = _dataset(args, cfg)
    result = await Backtester(cfg, ds, _options(args, args.label or args.command)).run()
    report = build_report(result)
    paths = write_report(report, Path(args.out))
    print(f"{report.data_source} backtest {report.first_day} -> {report.last_day}: {report.summary['trades']} trades, "
          f"net ₹{report.summary['net_pnl']:,.2f}, compliance {report.compliance_status}")
    for kind, p in paths.items():
        print(f"  {kind}: {p}")
    return 0 if report.compliance_status != "FAIL" else 2


async def _walk_forward(args: argparse.Namespace) -> int:
    cfg = _config(args)
    ds = _dataset(args, cfg)
    plan = walk_forward_splits(ds.days(), train_days=args.train, test_days=args.test, embargo_days=args.embargo,
                               anchored=args.anchored, holdout_days=args.holdout)
    runner = WalkForwardRunner(cfg, ds, options=_options(args, "walk-forward"),
                               evaluate_holdout=args.evaluate_holdout)
    wf = await runner.run(plan)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    md = out / "walk_forward.md"
    md.write_text(render_walk_forward_markdown(wf), encoding="utf-8")
    print(f"walk-forward: {len(wf.folds)} folds, OOS {wf.out_of_sample.trades} trades, "
          f"net ₹{wf.out_of_sample.net_pnl:,.2f}\n  markdown: {md}")
    return 0


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m backtest", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--out", required=True, help="output directory for the report")
        sp.add_argument("--config-dir", help="config directory (default: config/)")
        sp.add_argument("--strategies", nargs="+", help="run only these strategy ids")
        sp.add_argument("--slippage-ticks", type=int, help="override paper_broker.slippage_ticks")
        sp.add_argument("--half-spread-pct", type=float, default=SpreadModel().half_spread_pct)
        sp.add_argument("--label")
        sp.add_argument("--log-level", default="ERROR", help="bot log level on stderr (default ERROR)")
        sp.add_argument("--quiet", action="store_true")

    def synthetic(sp: argparse.ArgumentParser) -> None:
        defaults = {f.name: f.default for f in fields(SyntheticSpec)}
        sp.add_argument("--lot-units", type=int,
                        help="contract size for the SYNTHETIC instrument list (required for synthetic data)")
        sp.add_argument("--days", type=int, default=defaults["days"])
        sp.add_argument("--seed", type=int, default=defaults["seed"])
        sp.add_argument("--start", default=str(defaults["start"]))
        sp.add_argument("--save-data", help="also write the SYNTHETIC dataset to this directory")

    s = sub.add_parser("synthetic", help="backtest on generated SYNTHETIC data")
    common(s)
    synthetic(s)
    r = sub.add_parser("run", help="backtest a historical dataset directory")
    common(r)
    r.add_argument("--data", required=True)
    w = sub.add_parser("walk-forward", help="walk-forward validation")
    common(w)
    synthetic(w)
    w.add_argument("--data", help="historical dataset directory (default: SYNTHETIC data)")
    w.add_argument("--train", type=int, required=True)
    w.add_argument("--test", type=int, required=True)
    w.add_argument("--embargo", type=int, default=1)
    w.add_argument("--holdout", type=int, default=0)
    w.add_argument("--anchored", action="store_true")
    w.add_argument("--evaluate-holdout", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    configure_logging(level=args.log_level, console=True)
    if args.command == "walk-forward":
        return asyncio.run(_walk_forward(args))
    return asyncio.run(_single(args))


if __name__ == "__main__":
    sys.exit(main())
