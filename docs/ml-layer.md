# ML layer (Phase 7)

> **No real historical data exists yet.** Every model in this repository has been trained and tested on
> SYNTHETIC random-walk data (`backtest/synthetic_data.py`). Such models are research artefacts: the
> database refuses to validate or promote them, the trading registry refuses to store them, and the
> inference service refuses to use them for trading. ML is **off** for every strategy by default
> (`strategies.<id>.ml.enabled: false`). With ML off, the bot behaves exactly as before Phase 7.

## Modules

| file | what it does |
|---|---|
| `ml/feature_engineering.py` | Feature schema v1 (32 features). One `compute_features` function for training and live inference, plus a live `FeatureEngine` |
| `ml/feature_store.py` | `FeatureFrame` (training table tagged SYNTHETIC/HISTORICAL, schema-hash checked), backward as-of join, live SIGNAL snapshots |
| `ml/labeling.py` | Triple-barrier labels on the option premium; forward regime labels |
| `ml/dataset.py` | Events (replayed real strategies, or a dense grid) -> features + labels -> `TrainingSet` |
| `ml/lgbm.py` | LightGBM seed ensembles (>= 5 seeds, mean prediction, deterministic, sha256-verified text artefacts) |
| `ml/signal_filter.py` | P(target before stop) + the ALLOW / REDUCE / DELAY / REJECT policy |
| `ml/outcome_predictor.py` | P(target), P(loss), expected premium move, expected holding time |
| `ml/regime_classifier.py` | Trend (3 classes) + volatility (3 classes) on forward labels; expiry day from the calendar; rule-based fallback |
| `ml/evaluation.py` | AUC / Brier / log loss, regression and multiclass baselines, cost-adjusted trading metrics, risk compliance |
| `ml/training_pipeline.py` | Holdout, purged + embargoed walk-forward CV, seeds, gates, comparison with production, report |
| `ml/model_registry.py` | Versioned artefacts + `model_runs` rows, manifest, verified load, promotion rules |
| `ml/inference_service.py` | The engine's `SignalFilter`: per-strategy models, fail-closed, feature snapshots |
| `runtime/ml.py` | Wires the inference service into `TradingBot` (candles -> features, live option quote) |
| `options/pricing.py` | Black-Scholes and implied volatility (shared with the synthetic data generator) |

## Feature dictionary (schema v1.0.0)

Every feature is computed **as of the decision time** (a signal's candle end). Only 1-minute bars that
ended at or before then are visible. Intraday features use the current session only.

| # | feature | group | unit | definition | uses |
|---|---|---|---|---|---|
| 1 | `direction` | signal | +1/-1 | +1 CE (bullish), -1 PE, 0 undirected | - |
| 2 | `ret_1m` | returns | % | NIFTY log return, last 1 completed minute | 1 bar |
| 3 | `ret_3m` | returns | % | ... last 3 minutes | 3 bars |
| 4 | `ret_5m` | returns | % | ... last 5 minutes | 5 bars |
| 5 | `ret_15m` | returns | % | ... last 15 minutes | 15 bars |
| 6 | `ret_30m` | returns | % | ... last 30 minutes | 30 bars |
| 7 | `gap_pct` | returns | % | session open vs previous session's last close | previous close |
| 8 | `vwap_dist_pct` | trend | % | current NIFTY future's close vs its session VWAP (the index has no volume) | session |
| 9 | `ema20_dist_pct` | trend | % | close vs EMA(20) of 1-minute closes | >= 20 bars |
| 10 | `ema20_slope_pct` | trend | % | change of EMA(20) over 5 minutes, % of EMA | >= 25 bars |
| 11 | `atr14_pct` | volatility | % | mean 1-minute true range over 14 bars, % of close | 14 bars |
| 12 | `rv_15m` | volatility | annualised | realised volatility of 1-minute log returns | 15 returns |
| 13 | `rv_30m` | volatility | annualised | same, 30 returns | 30 returns |
| 14 | `or_position` | structure | ratio | (close - OR low) / (OR high - OR low); NaN before 09:30 | opening range |
| 15 | `or_width_pct` | structure | % | opening-range width, % of OR low | opening range |
| 16 | `minutes_since_open` | time | min | from 09:15 | clock |
| 17 | `minutes_to_square_off` | time | min | to the square-off start (15:10) | clock |
| 18 | `day_of_week` | time | 0-4 | Monday = 0 | clock |
| 19 | `minutes_to_expiry` | expiry | min | to the selected option's expiry close | calendar |
| 20 | `is_expiry_day` | expiry | 0/1 | expiry day of the selected option | calendar |
| 21 | `option_premium` | option | ₹ | last premium of the contract the strike rules select | <= as-of |
| 22 | `option_premium_pct_spot` | option | % | premium / spot | <= as-of |
| 23 | `option_moneyness_pct` | option | % | direction x (spot - strike) / spot; > 0 in the money | <= as-of |
| 24 | `option_iv` | option | annualised | feed IV, else solved from the premium (Black-Scholes) | <= as-of |
| 25 | `option_volume_log` | option | log1p | contract's day volume so far | <= as-of |
| 26 | `option_oi_log` | option | log1p | open interest | <= as-of |
| 27 | `option_spread_pct` | option | % | (ask - bid) / mid; NaN in candle data (no quotes) | <= as-of |
| 28 | `recent_win_rate_5` | outcomes | ratio | win rate of the strategy's last 5 CLOSED trades | closed <= as-of |
| 29 | `recent_avg_r_5` | outcomes | R | mean R-multiple of those trades | closed <= as-of |
| 30 | `consecutive_losses` | outcomes | count | current losing streak | closed <= as-of |
| 31 | `regime_trend` | regime | -1/0/+1 | trailing 30-minute move vs its expected size at the trailing volatility (> 1 sigma = trend) | 30 bars |
| 32 | `regime_vol` | regime | -1/0/+1 | trailing realised vol: < 9% low, > 16% high (annualised) | 30 returns |

Missing inputs are NaN; LightGBM handles them natively. Nothing is scaled with dataset-wide statistics, so
there is no scaler that could leak test data into training.

## Labels

**Triple barrier** (`ml/labeling.py`), per long-option entry decided at `t0`:

* entry: the open of the option bar that starts at `t0`, plus adverse slippage (`paper_broker.slippage_ticks`)
* upper barrier: the strategy's target (e.g. 2R); lower barrier: its stop-loss (e.g. 25% of premium)
* time barrier: `t0 + 60 min`, capped at the 15:10 square-off start
* inside one bar, an up-bar visits its low first (stop first), as in the backtester; a gap through a
  barrier exits at the bar's open
* outputs: hit (TARGET / STOP / TIME), `t1` (when the label became known), gross move %, R-multiple, net
  P&L per lot after brokerage, STT, exchange, SEBI, stamp duty and GST, holding minutes, MFE / MAE

**Forward regime** labels: the trend and volatility class of the next 30 minutes (same thresholds as the
trailing features). These are used only as targets, never as features.

## Training pipeline

```
HistoricalDataset (SYNTHETIC | HISTORICAL)
   │ ml.dataset: strategy_events (real ORB / VWAP+EMA classes replayed) or grid_events
   │             -> contract by strike rules -> liquidity bounds -> features @ as-of -> triple barrier
   ▼
TrainingSet  (rows sorted by t0; label end t1; data source carried through)
   │
   ├─ last 20% of DAYS ─────────────────────────────────────────────► HOLDOUT (untouched)
   ▼                                                                     │
development rows (label must end before the holdout starts: purged)      │
   │                                                                     │
   │ PurgedKFold(n=5, embargo=60 min, forward_only=True)                 │
   │   fold k: train = rows BEFORE test block k                          │
   │           − rows whose [t0, t1] overlaps the block      (purge)     │
   │           − rows starting < 60 min after the block      (embargo)   │
   │   fit SeedEnsemble × 5 seeds → predict block k (out of sample)      │
   ▼                                                                     │
OOS metrics per seed → mean ± std                                        │
trading metrics: filter policy vs no-filter baseline, risk compliance    │
   │                                                                     │
   ▼                                                                     ▼
final fit on all development rows ───────────────────────────► holdout metrics (once)
   │                                                   comparison with the production model
   ▼
validation gates → ModelRegistry.register
     SYNTHETIC : TRAINED or REJECTED, research registry only, never promotable
     HISTORICAL: TRAINED → VALIDATED if every gate passes → (named human) PAPER_READY
```

**Gates** (signal filter / outcome predictor): >= 100 OOS events; mean OOS AUC >= 0.55; AUC std across
seeds <= 0.05; >= 30 filtered OOS trades; filtered expectancy > 0 and > the no-filter baseline (net of all
costs); no risk-compliance BREACH (OOS and holdout); holdout AUC >= 0.50. Regime classifier: OOS log loss
below the class-prior baseline for trend and for volatility.

**Trading metrics** are event-level: each event is sized like `risk.position_sizer` (lots =
floor(₹ risk x multiplier / (stop distance x lot units))) with net P&L per lot from the label. They do not
model overlapping positions, daily caps, cooldowns or the kill switch. The full event-driven backtest
(`BacktestOptions(ml_strategies=..., ml_models=...)`) does, and it is the Phase 9 VERIFY step.

### Leakage checks (recorded in every evaluation report)

* rewriting every bar after as-of leaves every feature bit-identical (tests, 7 decision times)
* labels start at their feature's as-of (`t0 == as_of`, asserted) and end at or after it
* recent-outcome features count only labels known by as-of (`t1 <= as_of`, tested with overlapping labels)
* the forward regime model is never a training feature (the trailing rules are)
* no dataset-wide normalisation
* chronological, forward-only, purged and embargoed folds; each fold asserts its training labels end before
  its test block starts
* final holdout untouched until the single final evaluation

## Signal filter policy

Per strategy, from `strategies.<id>.ml` in `config/strategy_defaults.yaml`:

| score | action | size |
|---|---|---|
| >= `reduce_below` (0.70) | ALLOW | 100% of the risk-sized quantity |
| [`confidence_threshold` (0.60), 0.70) | REDUCE | `min_size_multiplier` (50%) ... 100%, linear |
| [0.60 - `delay_band` (0.05), 0.60) | DELAY | vetoed now; the strategy may signal again on a later candle |
| below | REJECT | 0 |
| missing / NaN / out of range | REJECT | 0 (fail-closed) |

The filter **cannot** enlarge a position (`FilterDecision` refuses a multiplier > 1). It cannot move or
remove a stop or target, because it has no access to them. It cannot bypass risk either: `RiskManager`
re-checks every limit after the filter and sizes the trade itself.

## Model registry schema

`model_runs` (schema v4) plus one artefact directory per run:

| column | meaning |
|---|---|
| `run_id` | unique id (`RUN-...`) |
| `model_type` | SIGNAL_FILTER / REGIME_CLASSIFIER / OUTCOME_PREDICTOR / RL_PPO |
| `model_version` | `<kind>-<strategy>-<YYYYmmdd-HHMMSS>` (`-synthetic` suffix for SYNTHETIC) |
| `strategy_id` | owning strategy (per-strategy model state) |
| `feature_schema_version`, `feature_schema_json` | names, order, units, hash |
| `training_start`, `training_end` | development window (first `t0`, last `t1`) |
| `hyperparameters_json` | seeds, LightGBM params, rounds, folds, embargo, holdout fraction, barrier spec, policy |
| `metrics_json` | headline metrics, gates, comparison |
| `seeds_json` | the seeds (>= 5) |
| `environment_version` | pipeline / feature schema / Python / LightGBM / NumPy / pandas versions |
| `artifact_path` | artefact directory |
| `status` | TRAINED / VALIDATED / REJECTED / PAPER_READY / LIVE_CANDIDATE / LIVE_APPROVED / ARCHIVED |
| `approved_by` | named human (required for LIVE_APPROVED; recorded for PAPER_READY) |
| **`data_source`** (v4) | SYNTHETIC / HISTORICAL / UNKNOWN, immutable |

Database triggers (v4): `VALIDATED`, `PAPER_READY`, `LIVE_CANDIDATE` and `LIVE_APPROVED` require
`data_source = 'HISTORICAL'`, both on insert and on update. `data_source` can never change. Rows from
older databases migrate as `UNKNOWN`, so they can never be promoted again.

Artefact directory `<artifact_dir>/<MODEL_TYPE>/<strategy>/<version>/`: `manifest.json` (all of the above
plus holdout window, data description and the sha256 of every file), `evaluation_report.json`,
`evaluation_report.md`, one LightGBM text model per seed and head. `ModelRegistry.load` re-verifies every
hash and refuses a modified artefact or a manifest that disagrees with the database row.

## Running it

```bash
# SYNTHETIC research run (random walk, NOT real prices) -> data/dev/ml/ (separate research registry)
python -m ml train --synthetic --lot-units 65 --days 30 --strategy orb_options --kind all
python -m ml list --research

# HISTORICAL dataset (backtest/data.py format) -> trading database + models/ (bot must have created the DB)
python -m ml train --data path/to/dataset --strategy vwap_ema --kind signal_filter --events strategy
```

`--data` reads the label from the dataset's `meta.json`, and that label decides the registry. So the ML
layer never guesses it: a dataset with no `meta.json`, or a `meta.json` without an explicit `"source"`, is
refused. A dataset labelled HISTORICAL whose instrument keys or symbols carry the `SYNTH` tag (everything
`backtest/synthetic_data.py` generates) is refused too. A dataset saved from the SYNTHETIC generator keeps
its SYNTHETIC label and goes to the research registry.

A HISTORICAL run that passes every gate ends VALIDATED. Turning it on is a separate, deliberate step:
`ModelRegistry.mark_paper_ready(run_id, approved_by="<name>")`, then `ml.enabled: true` for that strategy,
then `load_strategy_models(registry, cfg)` passed as `TradingBot(ml_models=...)`. LIVE needs Phase 9's
promotion gate and explicit human approval.

An example SYNTHETIC evaluation report is in [`docs/ml-example/`](ml-example/).
