---
name: ml-rl-trading-safety
description: Use when building or reviewing ML signal filters, PPO reinforcement-learning agents, feature pipelines, backtests, walk-forward validation, model versioning, or any self-learning / Reflective Loop component of the NIFTY options bot.
---

# ML / RL Trading Safety

Apply this Skill to anything in `ml/`, `learning/`, or `backtest/`.

## 1. Data integrity

- Use **point-in-time features only** — every feature must be computable from data available at that timestamp.
- Prevent **lookahead bias**: no future bars, no end-of-day values used intraday, no revised data.
- Prevent **target leakage**: labels must not leak into features (check rolling windows, normalizers, and joins).
- Fit scalers/encoders on training windows only.

## 2. Validation

- Use **chronological walk-forward validation** with **purging** and **embargo** between train and test windows.
- **Never randomly shuffle** financial time-series for train/test splits.
- Keep a final untouched out-of-sample period.

## 3. Evaluation metrics (all required)

Report every model and strategy version on:
- Out-of-sample returns
- Maximum drawdown
- Win rate
- Profit factor
- Trade count
- Average trade duration
- Risk-limit compliance (any breach = fail)
- Transaction-cost-adjusted expectancy (brokerage, STT, exchange fees, GST, stamp duty, slippage)

## 4. Reproducibility

- Run every ML and RL experiment with **multiple random seeds** (minimum 5). Report mean and spread, not the best seed.

## 5. Model versioning

Every saved model records:
- Feature schema (names, types, version)
- Training window (start/end dates)
- Hyperparameters
- Environment version (RL env + library versions)
- Evaluation report
- Model artifact path

A model without all six is not deployable.

## 6. RL agent action boundaries

The RL agent **may only** control:
- Entry approval for valid setups (it can veto, not invent, entries)
- Position size **within limits**
- Stop-loss placement **within limits**
- Target placement **within limits**
- Trailing stop
- Partial exit
- Full exit

The RL agent **must never** control:
- Daily loss limits
- Maximum exposure
- Kill switch
- Credentials
- Square-off timing

Enforce by design: the action space contains only allowed actions, values are clipped to configured bounds, and `risk/` re-checks every action before execution.

## 7. Reflective Loop gates

`LOG → REFLECT → HYPOTHESIZE → MUTATE → VERIFY → PROMOTE / REJECT`

Every strategy mutation must pass **all** of these before promotion:
1. Backtest
2. Walk-forward validation
3. Out-of-sample test
4. Stress test (high volatility, gap days, expiry days, low liquidity)
5. Comparison with the current production version

If any gate fails → REJECT and record why.

## 8. Deployment

- Improved versions deploy to **PAPER first**.
- **LIVE deployment requires explicit human approval.** Never auto-promote to LIVE.
- Every promotion is versioned, auditable, and reversible (keep the previous version ready to restore).
