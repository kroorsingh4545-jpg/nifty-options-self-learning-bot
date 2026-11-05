# RL trade manager (Phase 8)

> **No real historical data exists yet.** Every RL model in this repository has been trained and tested
> on SYNTHETIC random-walk data (`backtest/synthetic_data.py`). Those models are research artefacts.
> The database refuses to validate or promote them, the trading registry refuses to store them, and the
> bot refuses to load them outside `research=True` backtests in PAPER. RL is **off** for every strategy by
> default (`strategies.<id>.rl.enabled: false`). With RL off, the bot behaves exactly as it did before
> Phase 8.

The RL agent **manages** trades that the strategies propose. It does not speculate:

* it can approve (ENTER) or veto (SKIP_SETUP) a valid strategy setup, but it can never create an entry
  or make one larger;
* for an open trade it can HOLD, tighten the stop (three ways), take one partial profit, or exit;
* it can **never** control daily loss limits, maximum exposure / open trades, the kill switch,
  credentials, square-off timing or position size. None of these exists in its action space, its
  config or its code paths.

## Modules

| file | what it does |
|---|---|
| `ml/rl_environment.py` | Gymnasium environment, observation schema v1, frozen risk envelope, bounded action rules, observation normaliser |
| `ml/reward_design.py` | Reward components and weights (versioned) |
| `ml/rl_agent.py` | Stable-Baselines3 PPO, at least 5 seeds, walk-forward + holdout, evaluation, gates, seed-ensemble model, report |
| `ml/inference_service.py` | `RLTradeAdvisor`: entry veto (`SignalFilter`) and trade advice. Fail-closed / fail-to-HOLD |
| `core/interfaces.py` | `TradeAdvice` / `TradeAdviceAction` (the only thing RL can hand to execution), `ChainedFilter` |
| `execution/position_manager.py` | `apply_advice`: re-validates every proposal against the live trade. Partial exits keep protection |
| `runtime/rl.py` | Wires the advisor into `TradingBot`: entry veto chained after the ML filter, advice on each 1-minute bar |
| `ml/model_registry.py` | `rl_trade_manager` kind (`ModelType.RL_PPO`), lazily resolved so torch never loads with the registry |

## Environment specification

| item | value |
|---|---|
| episode | one trading day of one strategy, replayed from completed 1-minute bars |
| setups | the strategy's own signals (`--events strategy`) or a research grid every N minutes inside the strategy's entry window (`--events grid`) |
| setup filter | contract by the strategy's strike rules on the nearest weekly expiry; premium / volume / OI bounds; must be allowed by the risk envelope right now and size to >= 1 lot |
| decision points | a valid setup (ENTER / SKIP_SETUP); every `rl.decision_interval_minutes` (default 1) while a trade is open |
| fills | the OPEN of the option bar that starts at the decision time, plus `paper_broker.slippage_ticks` adverse slippage |
| intrabar path | an up-bar visits its low first, a down-bar its high first (same as the backtester); a bar that opens through a level fills at the open |
| costs | `broker.charges.ChargeCalculator`: brokerage, STT, exchange, SEBI, stamp duty, GST |
| positions | one managed trade at a time (within `max_open_trades`) |
| look-ahead | an observation at `t` uses only bars that ended at or before `t` (tested by rewriting every later bar) |

### Risk envelope (frozen, from `config/risk_limits.yaml` and the strategy config)

| control | value (current config) | who enforces it |
|---|---|---|
| risk per trade | min(2,000, strategy sizing) x size multiplier; whole lots from the stop distance plus exit slippage | envelope sizes every entry |
| initial stop / target | the strategy's `stop_loss` / `take_profit` rules | envelope |
| daily loss limit | 5,000 per strategy: forced exit when realised + open loss reaches it, then no entries for the day | envelope |
| remaining daily budget | a new trade may risk at most what is left of the daily limit (as `DailyLossGuard`) | envelope |
| entry window / cut-off | strategy window; nothing at/after 14:45 | envelope |
| trades per day / cooldown / consecutive losses | 4 / 15 min after a loss / pause after 3 | envelope |
| square-off | every position is closed at the open of the 15:10 bar | envelope |

The live bot does not use the envelope for enforcement. Live enforcement stays with `risk/` and
`execution/`, exactly as before. The envelope exists so that training and evaluation see the same limits
the bot enforces.

## Action schema

| id | action | when | what it can do (bounds from `strategies.<id>.rl`) |
|---|---|---|---|
| 0 | HOLD | always | nothing |
| 1 | TIGHTEN_STOP | in a trade | stop += `tighten_fraction` (25%) of the stop-to-price gap |
| 2 | MOVE_STOP_TO_BREAKEVEN | in a trade, price > entry + 1 tick | stop = entry |
| 3 | TRAIL_STOP | in a trade | stop = price - `trail_distance_r` (0.5) x R |
| 4 | TAKE_PARTIAL_PROFIT | in a trade, >= 2 lots, profit >= `min_profit_r_for_partial` (0.5 R), < `max_partial_exits` (1) | sells floor(`partial_fraction` (50%) x lots), at least 1 lot stays |
| 5 | EXIT_FULL | in a trade | market exit of everything |
| 6 | ENTER | a valid setup is offered | take it at the envelope's size |
| 7 | SKIP_SETUP | a valid setup is offered | let it go |

Stop actions **only tighten**. `proposed_stop` never returns a looser stop or one at or above the market.
An action that would not tighten is a no-op. Invalid actions (ENTER or SKIP_SETUP while in a trade, or a
position action when flat) are refused, treated as HOLD or SKIP_SETUP, and penalised. At inference the
seed ensemble only chooses among the actions valid in the current state, and ties go to HOLD, then
SKIP_SETUP.

## Observation schema (v1.0.0, 52 features, hash recorded in every model)

| group | features |
|---|---|
| market (NIFTY) | `nifty_vs_open_pct`, `ret_5m`, `ret_15m`, `vwap_dist_pct` (current future vs its session VWAP), `ema20_dist_pct`, `ema20_slope_pct`, `atr14_pct`, `rv_15m`, `rv_30m` |
| option (Greeks where available) | `option_premium_pct_spot`, `option_moneyness_pct`, `option_iv` (feed, else solved), `delta`, `gamma_1pct`, `theta_pct` (per day, % of premium), `vega_pct` (per vol point, % of premium), `minutes_to_expiry`, `is_expiry_day` |
| setup | `setup_available`, `setup_direction`, `ml_confidence` (signal filter score when a model is supplied), `ml_available` |
| position | `in_position`, `position_direction`, `entry_premium_pct_spot` (entry price), `premium_vs_entry_pct`, `unrealized_pnl_budget`, `r_multiple`, `stop_distance_r`, `target_distance_r`, `peak_r`, `time_in_trade_min`, `lots_open_frac`, `partials_taken`, `stop_at_or_above_entry` |
| time | `minutes_since_open`, `minutes_to_square_off`, `minutes_to_entry_cutoff` |
| regime probabilities | `p_trend_down`, `p_range`, `p_trend_up`, `p_vol_low`, `p_vol_normal`, `p_vol_high` (regime model if supplied, else the trailing rules as one-hot) |
| account | `open_positions`, `daily_pnl_frac` (realised net / daily limit), `daily_loss_headroom_frac`, `trades_today_frac`, `cooldown_active`, `consecutive_losses`, `recent_win_rate_5`, `recent_avg_r_5` |

The NIFTY price enters the observation as a return from the session open. The raw index level is
non-stationary, so it is not used directly. `build_observation` is the single function used by the
simulator and by live inference. The observation normaliser (per-feature mean / std, NaN -> 0, clip at +/-10)
is fitted on **training-period** observations only, frozen, and saved with the model
(`observation_normalizer.json`).

## Reward

In units of the per-trade risk budget R_budget (2,000):

```
reward = realized_trade_return          gross P&L realised this step, at unslipped prices
       - transaction_costs              all charges
       - slippage_penalty               adverse fill slippage
       - drawdown_penalty               0.5  x new intraday equity drawdown / R_budget
       - excessive_holding_penalty      0.002 per minute held beyond 60 minutes
       - risk_limit_violation_penalty   5.0  per refused action or daily-loss forced exit   (heavy)
       - overtrading_penalty            0.05 per entry
```

The first three terms add up exactly to the realised net P&L (tested). The shaping terms are penalties
only (weights must be non-negative), and a violation (>= 1 R_budget, default 5) always costs more than a
full stop-loss. The weights and reward version are saved with every model.

## Training and evaluation plan

```
dataset (SYNTHETIC | HISTORICAL) -> MarketReplay -> days with a setup the envelope would allow
  │ last 20% of days: HOLDOUT (untouched), 1 embargo day before it
  ▼
development days, expanding walk-forward (default 2 folds, 1 embargo day between train and test):
  fold k: train on blocks 0..k-1, test on block k
  per seed (>= 5: 11, 23, 37, 41, 53): fit the normaliser on TRAIN days, train PPO, evaluate deterministically
  ▼
final: one normaliser on all development days, PPO per seed
  holdout once: each seed, the seed ensemble, static-stop baseline (ENTER + HOLD), skip-everything,
  and the current production RL model if one exists
  ▼
gates -> registry (SYNTHETIC: TRAINED / REJECTED in the research registry only)
```

PPO (Stable-Baselines3 `MlpPolicy`, 64x64 Tanh actor and critic): lr 3e-4, n_steps 512, batch 64,
10 epochs, gamma 0.99, GAE lambda 0.95, clip 0.2, entropy 0.01. CPU, one torch thread, deterministic
per seed (tested).

**Models are judged on realised equity, trade quality, drawdown and risk compliance, never on reward
alone.** Every evaluation reports the net P&L after all costs, expectancy, win rate, profit factor, max
drawdown, worst day, average R, average win / loss, holding time, exit reasons, action counts,
invalid-action rate, the reward breakdown (for information only), daily P&L, and an independent
compliance re-check of the trade log:

| check | level |
|---|---|
| every trade has a stop below entry | BREACH |
| stops never widened | BREACH |
| entries inside the window and before 14:45 | BREACH |
| flat by the 15:10 square-off start | BREACH |
| at most 4 trades per day | BREACH |
| no entry after a daily-loss halt, inside a cooldown, or after the consecutive-loss limit | BREACH |
| size within the per-trade risk budget | BREACH |
| day loss within the daily limit (gaps can overshoot) | WARN |
| loss per trade within the risk budget (gaps can overshoot) | WARN |

**Gates** (all must pass): at least 20 holdout trades; holdout expectancy > 0 net of costs; the ensemble
beats the static-stop baseline on holdout net P&L; at least 60% of seeds have positive walk-forward OOS net
P&L; holdout max drawdown <= 2 x the daily loss limit; invalid-action rate <= 2%; no BREACH in any
walk-forward run or the holdout.

### What is saved with every model

`<artifact_dir>/RL_PPO/<strategy>/<version>/`:

| file / manifest key | content |
|---|---|
| `rl_trade_manager_seed<N>.pt` | each seed's policy as plain tensors (`state_dict`), loaded with `weights_only=True`; no pickled code |
| `observation_normalizer.json` | the frozen normaliser plus the observation-schema hash |
| `feature_schema` | the observation schema (names, groups, units, version, hash) |
| `training_window` / `holdout_window` | development and holdout ranges |
| `hyperparameters` | seeds, timesteps, PPO parameters, folds, embargo, holdout fraction, reward weights, gates |
| `environment` / `environment_version` | Python, torch, Stable-Baselines3, Gymnasium, NumPy, env / obs / reward / pipeline versions |
| `environment_spec` | the full environment specification above (envelope values, action rules, execution model, reward) |
| `evaluation` + `evaluation_report.json/.md` | everything in the evaluation section; SYNTHETIC reports say so at the top and the bottom |
| `files` | sha256 of every file; `ModelRegistry.load` refuses any mismatch |

## Live wiring (PAPER only; off by default)

* `TradingBot(rl_models=..., rl_research=False)` attaches `runtime.rl.RLRuntime` when any strategy
  has `rl.enabled: true`. The production loader `ml.inference_service.load_rl_models` returns HISTORICAL
  deployable models only.
* **Entries**: `ChainedFilter(ml_filter, rl_advisor)`. The RL step can only veto. The risk gate still
  checks and sizes every entry. If RL is enabled but no model is loaded, every entry is refused.
* **Open trades**: on each completed 1-minute NIFTY bar, every idle open trade of an RL-enabled strategy
  whose decision interval has elapsed gets a `TradeAdvice`. RL is not consulted while the kill switch
  blocks the strategy, at or after 15:10, or for a trade that is entering or exiting. Any inference
  problem means HOLD: the trade keeps its configured stop, target and trailing.
* `PositionManager.apply_advice` re-checks every proposal. It refuses another strategy's trade, a trade
  whose strategy does not have RL enabled, a non-idle trade, anything at or after the square-off start,
  a stop that is not strictly tighter or not strictly below the last price (`tighten_stop`), a partial
  exit that would not leave a lot, one when the trade is not in profit, and one beyond `max_partial_exits`.
  A tightened stop also moves the resting SL-M order.
* **Partial exits** cancel the resting stop, sell the partial quantity (with the usual exit retries, and
  the kill switch on failure), then re-place the protective stop for what is left. If the stop or
  target is hit during a partial exit, the rest is exited right after. A kill switch or square-off
  during a partial waits for it, then exits the rest.
* Full RL exits are recorded as `exit_reason = TRADE_MANAGER`.
* **Resting-SELL invariant.** For every trade, the live SELL quantity (protective stop plus exit orders) is
  never more than the quantity held. Protective placement and cancellation are serialised per trade. A
  cancel counts only when the broker confirms it. An exit is never sent while the stop or another sell may
  still be live; the bot escalates instead (CRITICAL alert and kill switch). A partial exit sells down to a
  quantity fixed when it was requested. See `tests/unit/test_resting_sell_invariant.py`.

## Running it

```bash
# SYNTHETIC research run -> data/dev/ml/ (separate research registry; never promotable)
python -m ml train-rl --synthetic --lot-units 65 --days 60 --strategy vwap_ema --timesteps 30000
python -m ml list --research

# HISTORICAL dataset (meta.json must state "source"; bot database must exist)
python -m ml train-rl --data path/to/dataset --strategy vwap_ema --events strategy

# Backtest a research RL model through the real bot (PAPER, research only)
Backtester(cfg, dataset, BacktestOptions(rl_strategies=("vwap_ema",), rl_models={"vwap_ema": loaded},
                                         rl_research=True))
```

Promotion: a HISTORICAL model whose gates all pass becomes VALIDATED. `mark_paper_ready` needs a named
human. LIVE statuses are only reachable through the Phase 9 promotion gate with explicit human approval.

## Known limitations

* One managed trade at a time in the environment. The bot allows 2 per strategy, and the agent manages
  each of them independently, seeing `open_positions`.
* The environment's recent-outcome features only use the episode's own day. Live, they use the
  strategy's last closed trades across days.
* A partial exit's realised P&L reaches `DailyLossGuard` only when the trade closes. Partials happen only
  in profit, so the guard sees less profit than it should, never more.
* After a restart, `partial_exits` restarts at 0 for restored trades (at least one lot always stays). An
  RL-tightened stop above the initial stop is restored as an active trailing stop. The trailing manager
  then follows its configured rule, which only ever tightens.
* Trained and tested on SYNTHETIC data only. Results say nothing about real markets.
