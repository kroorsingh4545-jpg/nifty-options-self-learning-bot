> **SYNTHETIC / RESEARCH RUN - generated data, NOT real NIFTY prices. Nothing from this run can be promoted; candidates that pass stop at VERIFIED.**

# Reflective Loop run LOOP-13ac22b7ba1a458e

Autonomy: `PAPER_AUTO_IMPROVE`. Verification data: `SYNTHETIC`. LIVE always needs explicit human approval and is never deployed by this build.

## orb_options (production 0.1.0)

### Bring the target closer: it is rarely reached -> 0.1.1: **REJECTED**

- Change: `{'take_profit.value': 1.5}` (was `{'take_profit.value': 2.0}`)
- Evidence: only 14% of trades reached the 2R target; 3 closed later at the trail or the square-off
- Expected effect: More trades bank profit at the target instead of giving it back
- Test metric: oos_expectancy_rs, minimum sample 30 OOS trades
- Risk impact: take_profit.value 2.0 -> 1.5. Rupee risk per trade is unchanged: position size still comes from the fixed per-trade risk budget, and every per-strategy / global loss limit, the kill switch and the square-off are untouched.
- Rollback: Keep orb_options 0.1.0 ready: if the candidate is deployed to PAPER and underperforms, the deployment controller rolls back to 0.1.0 (or the YAML baseline) in one step; every deployment is versioned and reversible.

| gate | result | detail |
|---|---|---|
| MIN_SAMPLE | PASS | 7 OOS trades (need 5) |
| RISK_COMPLIANCE | PASS | no risk-limit breach in any candidate run |
| BACKTEST | PASS | MAIN expectancy 277.02 vs production 135.59 |
| WALK_FORWARD | PASS | won or tied 100% of 8 test windows (need 60% of >= 3) |
| OUT_OF_SAMPLE | PASS | holdout net -4424.42 vs production -4424.42 (2 untouched days, evaluated once) |
| STRESS | PASS | no stress scenario worse than allowed |
| MULTI_SEED | FAIL | 68% of 200 seeded resamples favour the candidate (need 80%) |
| PRODUCTION_COMPARISON | PASS | OOS expectancy -434.19 vs production -535.21 (need +50); drawdown 4424.42 vs 4424.42 |
| NO_TRADE_BASELINE | FAIL | OOS net after costs -3039.3 vs 0 for not trading |

Decision: MULTI_SEED: 68% of 200 seeded resamples favour the candidate (need 80%); NO_TRADE_BASELINE: OOS net after costs -3039.3 vs 0 for not trading

Deployed to PAPER: nothing. LIVE candidates nominated: none.

## vwap_ema (production 0.1.0)

### Bring the target closer: it is rarely reached -> 0.1.1: **REJECTED**

- Change: `{'take_profit.value': 1.5}` (was `{'take_profit.value': 2.0}`)
- Evidence: only 0% of trades reached the 2R target; 6 closed later at the trail or the square-off
- Expected effect: More trades bank profit at the target instead of giving it back
- Test metric: oos_expectancy_rs, minimum sample 30 OOS trades
- Risk impact: take_profit.value 2.0 -> 1.5. Rupee risk per trade is unchanged: position size still comes from the fixed per-trade risk budget, and every per-strategy / global loss limit, the kill switch and the square-off are untouched.
- Rollback: Keep vwap_ema 0.1.0 ready: if the candidate is deployed to PAPER and underperforms, the deployment controller rolls back to 0.1.0 (or the YAML baseline) in one step; every deployment is versioned and reversible.

| gate | result | detail |
|---|---|---|
| MIN_SAMPLE | PASS | 13 OOS trades (need 5) |
| RISK_COMPLIANCE | PASS | no risk-limit breach in any candidate run |
| BACKTEST | PASS | MAIN expectancy 714.12 vs production 603.83 |
| WALK_FORWARD | PASS | won or tied 100% of 8 test windows (need 60% of >= 3) |
| OUT_OF_SAMPLE | PASS | holdout net -5070.73 vs production -5070.73 (2 untouched days, evaluated once) |
| STRESS | PASS | no stress scenario worse than allowed |
| MULTI_SEED | FAIL | 64% of 200 seeded resamples favour the candidate (need 80%) |
| PRODUCTION_COMPARISON | PASS | OOS expectancy 159.26 vs production 74.43 (need +50); drawdown 5070.73 vs 5070.73 |
| NO_TRADE_BASELINE | PASS | OOS net after costs 2070.42 vs 0 for not trading |

Decision: MULTI_SEED: 64% of 200 seeded resamples favour the candidate (need 80%)

Deployed to PAPER: nothing. LIVE candidates nominated: none.

---

> **SYNTHETIC / UNVERIFIED EVIDENCE - this reflection includes trades on generated or unlabelled data. It is NOT a judgement of the strategy on real markets and can never justify a promotion.**

# Custom reflection 2026-09-01 to 2026-10-07

Generated 2026-11-05 15:09 IST. Evidence: SYNTHETIC 14

## orb_options

- 7 trades, win rate 57.14%, net -96 after 431 costs, expectancy -13.73 per trade
- regime UNKNOWN: 7 trades losing -13.73 per trade
- strike ATM: 7 trades losing -13.73 per trade
- exit_reason STOP_LOSS: 3 trades losing -1948.67 per trade

| trades | win rate % | net | costs | slippage | expectancy | avg R | PF | max DD |
|---|---|---|---|---|---|---|---|---|
| 7 | 57.14 | -96.14 | 430.89 | 71.50 | -13.73 | -0.08 | 0.98 | 4,904.31 |

Best: PAPER-T-orb_options-13848ce39821 +4171 (TARGET); PAPER-T-orb_options-3d1e6a085a40 +664 (TRAILING_STOP); PAPER-T-orb_options-ed48e6ccc3e9 +637 (TRAILING_STOP)
Worst: PAPER-T-orb_options-38ba9352ad81 -2116 (STOP_LOSS); PAPER-T-orb_options-1ceb93affc5a -2002 (STOP_LOSS); PAPER-T-orb_options-f8e4202f793f -1728 (STOP_LOSS)

### By regime

| bucket | trades | win_rate_pct | net_pnl | expectancy | avg_r | profit_factor |
|---|---|---|---|---|---|---|
| UNKNOWN | 7 | 57.14 | -96.14 | -13.73 | -0.08 | 0.98 |

### By entry time

| bucket | trades | win_rate_pct | net_pnl | expectancy | avg_r | profit_factor |
|---|---|---|---|---|---|---|
| 09:30-10:00 | 5 | 60.00 | 1,742.69 | 348.54 | 0.15 | 1.47 |
| 10:30-11:00 | 2 | 50.00 | -1,838.83 | -919.41 | -0.66 | 0.13 |

### By strike

| bucket | trades | win_rate_pct | net_pnl | expectancy | avg_r | profit_factor |
|---|---|---|---|---|---|---|
| ATM | 7 | 57.14 | -96.14 | -13.73 | -0.08 | 0.98 |

### By option type

| bucket | trades | win_rate_pct | net_pnl | expectancy | avg_r | profit_factor |
|---|---|---|---|---|---|---|
| CE | 3 | 66.67 | -1,060.46 | -353.49 | -0.17 | 0.47 |
| PE | 4 | 50.00 | 964.32 | 241.08 | -0.01 | 1.25 |

### By stop target config

| bucket | trades | win_rate_pct | net_pnl | expectancy | avg_r | profit_factor |
|---|---|---|---|---|---|---|
| SL PREMIUM_PERCENT 25.0 | TP R_MULTIPLE 2.0 | trail R_MULTIPLE 1.0/0.5 | 7 | 57.14 | -96.14 | -13.73 | -0.08 | 0.98 |

### By exit reason

| bucket | trades | win_rate_pct | net_pnl | expectancy | avg_r | profit_factor |
|---|---|---|---|---|---|---|
| STOP_LOSS | 3 | 0.00 | -5,846.00 | -1,948.67 | -1.33 | 0.00 |
| TARGET | 1 | 100.00 | 4,171.05 | 4,171.05 | 2.21 | - |
| TRAILING_STOP | 3 | 100.00 | 1,578.81 | 526.27 | 0.40 | - |

### By version

| bucket | trades | win_rate_pct | net_pnl | expectancy | avg_r | profit_factor |
|---|---|---|---|---|---|---|
| 0.1.0 | 7 | 57.14 | -96.14 | -13.73 | -0.08 | 0.98 |

## vwap_ema

- 7 trades, win rate 71.43%, net 194 after 466 costs, expectancy 27.75 per trade

| trades | win rate % | net | costs | slippage | expectancy | avg R | PF | max DD |
|---|---|---|---|---|---|---|---|---|
| 7 | 71.43 | 194.23 | 465.52 | 71.50 | 27.75 | 0.10 | 1.06 | 2,544.93 |

Best: PAPER-T-vwap_ema-473aec67784e +1171 (TRAILING_STOP); PAPER-T-vwap_ema-c002a91575e2 +1020 (TRAILING_STOP); PAPER-T-vwap_ema-c261e9aebb46 +566 (TRAILING_STOP)
Worst: PAPER-T-vwap_ema-b800d49f1697 -2249 (STOP_LOSS); PAPER-T-vwap_ema-5867b990a302 -1140 (TRAILING_STOP); PAPER-T-vwap_ema-d0a5c2beeff7 +278 (TRAILING_STOP)

### By regime

| bucket | trades | win_rate_pct | net_pnl | expectancy | avg_r | profit_factor |
|---|---|---|---|---|---|---|
| UNKNOWN | 7 | 71.43 | 194.23 | 27.75 | 0.10 | 1.06 |

### By entry time

| bucket | trades | win_rate_pct | net_pnl | expectancy | avg_r | profit_factor |
|---|---|---|---|---|---|---|
| 09:30-10:00 | 1 | 100.00 | 1,020.09 | 1,020.09 | 0.57 | - |
| 10:00-10:30 | 1 | 100.00 | 278.00 | 278.00 | 0.33 | - |
| 11:30-12:00 | 2 | 50.00 | -591.29 | -295.64 | -0.08 | 0.48 |
| 13:00-13:30 | 1 | 100.00 | 565.99 | 565.99 | 0.46 | - |
| 13:30-14:00 | 1 | 100.00 | 1,170.83 | 1,170.83 | 0.70 | - |
| 14:00-14:30 | 1 | 0.00 | -2,249.39 | -2,249.39 | -1.15 | 0.00 |

### By strike

| bucket | trades | win_rate_pct | net_pnl | expectancy | avg_r | profit_factor |
|---|---|---|---|---|---|---|
| ATM | 7 | 71.43 | 194.23 | 27.75 | 0.10 | 1.06 |

### By option type

| bucket | trades | win_rate_pct | net_pnl | expectancy | avg_r | profit_factor |
|---|---|---|---|---|---|---|
| CE | 2 | 100.00 | 1,586.08 | 793.04 | 0.52 | - |
| PE | 5 | 60.00 | -1,391.85 | -278.37 | -0.06 | 0.59 |

### By stop target config

| bucket | trades | win_rate_pct | net_pnl | expectancy | avg_r | profit_factor |
|---|---|---|---|---|---|---|
| SL PREMIUM_PERCENT 20.0 | TP R_MULTIPLE 2.0 | trail PREMIUM_PERCENT 15.0/5.0 | 7 | 71.43 | 194.23 | 27.75 | 0.10 | 1.06 |

### By exit reason

| bucket | trades | win_rate_pct | net_pnl | expectancy | avg_r | profit_factor |
|---|---|---|---|---|---|---|
| STOP_LOSS | 1 | 0.00 | -2,249.39 | -2,249.39 | -1.15 | 0.00 |
| TRAILING_STOP | 6 | 83.33 | 2,443.62 | 407.27 | 0.32 | 3.14 |

### By version

| bucket | trades | win_rate_pct | net_pnl | expectancy | avg_r | profit_factor |
|---|---|---|---|---|---|---|
| 0.1.0 | 7 | 71.43 | 194.23 | 27.75 | 0.10 | 1.06 |
