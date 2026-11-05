# Example RL evaluation report (SYNTHETIC)

> **SYNTHETIC DATA.** This report comes from a seeded random walk (`backtest/synthetic_data.py`), NOT
> real NIFTY prices. The model went into the research registry only. It ended **REJECTED**, and it can
> never be validated, promoted or used for trading.

Command used (about 9 minutes on a laptop CPU):

```bash
python -m ml train-rl --synthetic --lot-units 65 --days 60 --start 2026-07-01 --strategy vwap_ema --timesteps 20000
```

| file | content |
|---|---|
| `rl_trade_manager-report.md` | human-readable report |
| `rl_trade_manager-report.json` | the full evaluation: walk-forward folds, holdout per seed, ensemble and baselines, compliance, gates |

## How to read it

* **REJECTED is the expected and correct outcome.** A random walk has no edge to learn. The ensemble's
  holdout net P&L (-1,409) loses to the static-stop baseline (-486). Its expectancy is negative. Only 1 of
  5 seeds made money out of sample in walk-forward. Three gates fail.
* **Seed spread is wide.** Holdout net P&L ranges from -607 to +3,281 across seeds. A single lucky seed
  (seed 11) would have looked good on its own. This is why the pipeline reports every seed, gates on seed
  consistency, and deploys only the ensemble.
* **What the agent learned: exit fast.** It exited every trade itself after about 1 minute (`RL_EXIT`
  22/22) and skipped 18 of 40 setups. On a driftless walk, round-trip costs dominate. The reward
  breakdown shows that charges and the overtrading penalty outweighed realised gains.
* **Risk compliance passed everywhere** (every walk-forward run and the holdout). No entry came after
  14:45, every trade was flat by 15:10, no stop was widened, the daily caps and cooldowns held, and size
  stayed within the risk budget. The envelope makes this hold for any policy, including a random one
  (tested in `tests/unit/test_rl_environment.py`).
* The static baseline's 14,040 max drawdown is over the whole 8-day holdout, not one day. Each day still
  stayed within the 5,000 daily limit.

None of this says anything about real markets. Re-run on a HISTORICAL dataset before drawing conclusions.
