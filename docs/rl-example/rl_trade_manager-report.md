> **SYNTHETIC DATA - research artefact only. Trained and evaluated on a seeded random walk, NOT real NIFTY prices. It can never be validated, promoted or used for trading.**

# RL trade manager evaluation - vwap_ema

Data: **SYNTHETIC** (SYNTHETIC random-walk data (seed 7, 60 days from 2026-07-01); NOT real NIFTY prices)  
Development: 2026-07-02 .. 2026-09-03 (33 days); holdout: 2026-09-07 .. 2026-09-23 (8 days); embargo 1 day(s)  
Judged on: realised equity, trade quality, drawdown and risk compliance (reward is reported only)

## Holdout

| policy | trades | net P&L | expectancy | win % | profit factor | max DD | avg hold (min) | compliance |
|---|---|---|---|---|---|---|---|---|
| PPO seed ensemble | 22 | -1,409 | -64.04 | 31.82 | 0.549 | 1,743 | 1.23 | PASS |
| seed 11 | 22 | 3,281 | 149.12 | 50.0 | 2.873 | 954 | 2.36 | PASS |
| seed 23 | 29 | -442 | -15.24 | 41.38 | 0.95 | 6,561 | 5.1 | PASS |
| seed 37 | 23 | 1,268 | 55.11 | 47.83 | 1.386 | 1,479 | 3.91 | PASS |
| seed 41 | 23 | 465 | 20.21 | 47.83 | 1.125 | 1,643 | 4.04 | PASS |
| seed 53 | 23 | -607 | -26.38 | 39.13 | 0.871 | 1,856 | 1.7 | PASS |
| baseline: static stop/target | 25 | -486 | -19.44 | 36.0 | 0.981 | 14,040 | 45.6 | PASS |
| baseline: skip every setup | 0 | 0 | - | - | - | 0 | - | PASS |

Seed spread of holdout net P&L: {'mean': 792.91, 'std': 1581.59, 'min': -606.69, 'max': 3280.72, 'n': 5}

Ensemble actions: {'ENTER': 22, 'EXIT_FULL': 22, 'MOVE_STOP_TO_BREAKEVEN': 2, 'SKIP_SETUP': 18, 'TIGHTEN_STOP': 3}  
Exit reasons: {'RL_EXIT': 22}  
Reward (reported, not judged): {'realized_trade_return': 0.1202, 'transaction_costs': 0.7304, 'slippage_penalty': 0.0942, 'drawdown_penalty': 0.8666, 'excessive_holding_penalty': 0.0, 'risk_limit_violation_penalty': 0.0, 'overtrading_penalty': 1.1, 'total': -2.671}

## Walk-forward (out of sample)

Fold 0: train 2026-07-02..2026-07-21 (11 d), test 2026-07-27..2026-08-17 (10 d); seed net P&L {'mean': -1594.93, 'std': 3152.77, 'min': -4434.8, 'max': 3691.37, 'n': 5}; static baseline 13,398

Fold 1: train 2026-07-02..2026-08-17 (22 d), test 2026-08-19..2026-09-03 (10 d); seed net P&L {'mean': -2253.6, 'std': 3681.76, 'min': -7512.91, 'max': 2181.34, 'n': 5}; static baseline -665

## Risk compliance (holdout ensemble)

- every trade has a stop below entry: **OK**
- stops never widened: **OK**
- entries inside the window and before 14:45: **OK**
- flat by the 15:10 square-off start: **OK**
- at most 4 trades per day: **OK**
- no entry after a daily-loss halt, in a cooldown or after the consecutive-loss limit: **OK**
- size within the per-trade risk budget: **OK**
- day loss within the daily loss limit (gaps can overshoot): **OK**
- loss per trade within the risk budget (gaps can overshoot): **OK**

## Gates

- holdout trades: PASS (value 22)
- holdout expectancy > 0 (net of costs): FAIL (value -64.04)
- holdout net P&L beats the static-stop baseline: FAIL (value [-1408.8, -485.92])
- seeds with positive walk-forward OOS net P&L: FAIL (value 0.2)
- holdout max drawdown: PASS (value 1742.64)
- invalid action rate: PASS (value 0.0)
- risk compliance (every OOS run and the holdout): PASS (value ['PASS'])

All gates passed: **False**

Comparison with production: no production RL model for this strategy

> SYNTHETIC DATA - research artefact only. Trained and evaluated on a seeded random walk, NOT real NIFTY prices. It can never be validated, promoted or used for trading.
