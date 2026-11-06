# Self-learning loop (Phase 9)

> **No real historical data exists yet.** Every learning-loop run in this repository so far used SYNTHETIC
> data (`backtest/synthetic_data.py`), and every report says so on its first line. A candidate verified on
> SYNTHETIC data stops at VERIFIED: the promotion gate and the database both refuse to promote it. Nothing
> in this phase can enable LIVE trading.

The loop improves strategies by changing a short, fixed list of parameters, and only after the change has
beaten the current version out of sample. It never changes strategy logic, capital, risk limits, the kill
switch, square-off times or credentials.

## Loop

```
            PAPER bot (real feed) / backtest (HISTORICAL or SYNTHETIC)
                                   |
                                   v
  LOG          trade_journal: one row per closed trade, labelled LIVE_FEED / HISTORICAL / SYNTHETIC / UNKNOWN
               (features at entry and exit, slippage, costs, R, execution quality, errors, RL activity)
               rl_decisions: every RL decision, with what execution did with it
                                   |
                                   v
  REFLECT      daily / weekly report per strategy: stats after costs, best / worst trades, breakdowns by
               regime, entry time, strike, stop-target config, exit reason, version; execution issues;
               degradation; predictive vs non-predictive features
                                   |
                                   v
  HYPOTHESIZE  deterministic rules -> at most N hypotheses per strategy, each with change, expected effect,
               evidence, test metric, minimum sample, validation plan, risk impact, rollback plan
               (findings that need a LOGIC change become observations, never mutations)
                                   |
                                   v
  MUTATE       whitelisted parameters only, within bounds and step limits; whole config re-validated against
               every hard limit; leaf-by-leaf diff; new patch version (CANDIDATE)
                                   |
                                   v
  VERIFY       vs the production version of the same strategy, through the real PAPER bot:
               MIN_SAMPLE, RISK_COMPLIANCE, BACKTEST, WALK_FORWARD, OUT_OF_SAMPLE, STRESS, MULTI_SEED,
               PRODUCTION_COMPARISON, NO_TRADE_BASELINE
                                   |
                   +---------------+----------------+
                   v                                v
  REJECT       any gate failed: REJECTED,     PROMOTE   all passed: VERIFIED, then PAPER_READY if the
               archived with the full                   evidence is HISTORICAL and the autonomy level
               evaluation                               (read now) allows it
                                                                    |
                                                                    v
                                              DEPLOY     best PAPER_READY candidate -> PAPER (automatic at
                                                         PAPER_AUTO_IMPROVE); the replaced version stays
                                                         restorable (SUPERSEDED); rollback in one step
                                                                    |
                                                                    v
                                              NOMINATE   after the paper-validation period on the real
                                                         feed: LIVE_CANDIDATE (needs autonomy
                                                         LIVE_CANDIDATE_APPROVAL)
                                                                    |
                                                                    v
                                              LIVE       named human + typed "APPROVE LIVE <strategy>
                                                         <version>" -> LIVE_APPROVED (recorded only;
                                                         this build has no LIVE deployment)
```

Every stage writes a `learning_events` row. Strategies are reflected on, mutated, verified and deployed in
isolation.

## Autonomy

`app.autonomy_level` in `config/settings.yaml` (default `PAPER_AUTO_IMPROVE`; the dashboard can change it).
The level is read again at every promotion step. One table in `core/autonomy.py`, mirrored by database
triggers:

| level | PAPER_READY | auto PAPER deploy | LIVE_CANDIDATE nomination | LIVE |
|---|---|---|---|---|
| OBSERVE_ONLY | no | no | no | never by the loop |
| PAPER_AUTO_IMPROVE (default) | yes | yes | no | never by the loop |
| LIVE_CANDIDATE_APPROVAL | yes | yes | yes | never by the loop |

PAPER deployment at `PAPER_AUTO_IMPROVE` is automatic: this was the operator's decision, and
`learning.auto_deploy_paper: false` turns it off. **LIVE always needs the operator's explicit approval.**
`loop`, `system`, `bot` and similar names are refused as approvers. The same autonomy rule applies to
ML / RL models: `ModelRegistry.mark_paper_ready` needs the level and a human approver, and the schema
re-checks it (`model_runs.promoted_autonomy`).

## Promotion-gate rules

| transition | requires |
|---|---|
| CANDIDATE -> REJECTED | any VERIFY gate failed; the full evaluation is stored on the version |
| CANDIDATE -> VERIFIED | every VERIFY gate passed |
| VERIFIED -> PAPER_READY | HISTORICAL evidence (never SYNTHETIC / UNKNOWN); autonomy PAPER_AUTO_IMPROVE or LIVE_CANDIDATE_APPROVAL; still a valid whitelisted overlay of the YAML config; built on the current production version |
| PAPER_READY -> PAPER_ACTIVE | deployment controller, autonomy allows PAPER deployment; the previous version becomes SUPERSEDED |
| PAPER_ACTIVE -> LIVE_CANDIDATE | autonomy LIVE_CANDIDATE_APPROVAL; on the LIVE_FEED journal for that version: >= `paper_validation_days` days, >= `paper_validation_min_trades` trades, positive net and expectancy |
| LIVE_CANDIDATE -> LIVE_APPROVED | a named human approver and the typed phrase `APPROVE LIVE <strategy> <version>`; recorded only |
| any PAPER state -> ROLLED_BACK / SUPERSEDED | rollback / retire / a newer deployment; always allowed (risk-reducing) |
| anything -> LIVE deployment | impossible: `deploy_live` always refuses, and `strategy_deployments` only accepts PAPER rows |

### VERIFY gates

Thresholds are in `learning.gates` (`config/settings.yaml`).

| gate | passes when |
|---|---|
| MIN_SAMPLE | OOS trades (walk-forward test windows + holdout) >= the hypothesis' minimum sample (`learning.min_sample_trades`, default 30) |
| RISK_COMPLIANCE | no BREACH in `backtest.metrics.compliance` in any candidate run (open trades, trades/day, entry cut-off, entry window, flat after square-off, stop on every trade, closed by 15:20) |
| BACKTEST | full non-holdout period: candidate expectancy after costs >= production |
| WALK_FORWARD | chronological test windows (embargo, no shuffling): wins or ties >= 60% of >= 3 windows |
| OUT_OF_SAMPLE | final untouched holdout, evaluated once: candidate net >= production net |
| STRESS | extra slippage; wide, thin quotes (low liquidity); gap, high-volatility and expiry days: never worse than production beyond `max_stress_loss_ratio` |
| MULTI_SEED | 200 seeded resamples of OOS daily P&L differences: >= 80% favour the candidate; ML / RL in use needs models trained on >= 5 seeds |
| PRODUCTION_COMPARISON | test metric: OOS expectancy gain >= 50 rupees and >= 5% per trade with drawdown at most 10% worse (or, for a risk-reducing change, lower drawdown with expectancy no more than 50 rupees worse) |
| NO_TRADE_BASELINE | OOS net P&L after costs > 0 |

## Mutation whitelist

`learning/mutation_engine.py` (`WHITELIST`, `NEVER_MUTABLE`). One mutation may change a value by at most the
listed step; bounds depend on the mode where the unit does.

| category | parameters | bounds (step) |
|---|---|---|
| entry threshold | ORB `breakout_buffer_points`, `min_range_points`, `max_range_pct`; VWAP `min_volume_ratio`, `max_vwap_crosses` | 0-30 (0.5), 5-60 (1), 0.5-3 (0.1), 0.8-3 (0.05), 1-10 (1) |
| confirmation | VWAP `momentum_bars`, `chop_lookback_bars` | 1-5, 6-30 |
| stop-loss | `stop_loss.value` | 10-50% (1) or 2-100 points (0.5), at most 40% change |
| target R-multiple | `take_profit.value` (R_MULTIPLE only) | 1-5 R (0.25) |
| trailing activation / step | `trailing.activation`, `trailing.step` | per trailing mode |
| size multiplier | `sizing.size_multiplier` | 0.25-1.0 (0.05): can only shrink size |
| time window | `entry_window.start` / `end` | 5-minute steps, at most 60 minutes per change, inside the session and before 14:45 |
| strike selection | `strike_selection.mode`, `offset_steps`, `min_premium` | ATM / ITM / OTM, 0-2, 10-200 |
| ML confidence threshold | `ml.confidence_threshold` (ML on only) | 0.50-0.80 (0.01), <= `ml.reduce_below` |

**Never mutable:** capital allocation, every risk limit (per-strategy and global daily loss, exposure / open
trades, trades per day, cooldown, consecutive losses), the risk budget per trade, the kill switch, square-off
and entry cut-off times, credentials, enable flags, the ML / RL switches and RL bounds, the signal instrument.
Global settings are outside a strategy config, so a mutation cannot even address them.

## Database schema additions (v5)

| table / column | purpose | enforced in the schema |
|---|---|---|
| `rl_decisions` | every RL entry decision (ENTER / SKIP / VETO) and trade decision (HOLD / tighten / partial / exit) with applied / result, model, confidence | append-only; trade must match mode and strategy; MANAGE needs a trade, ENTRY a signal |
| `trade_journal` | LOG rows (see above) | append-only; only for a CLOSED trade of the same mode and strategy; `data_source` required |
| `strategy_versions` | baseline and candidate versions: config, mutations, hypothesis, evaluation, evidence, autonomy, approver | inserts only as BASELINE / CANDIDATE; fixed transitions; HISTORICAL evidence for PAPER_READY and beyond; autonomy per status; LIVE_APPROVED needs an approver; definition immutable; evidence set once; never deleted |
| `strategy_deployments` | PAPER deployment history (the newest row per strategy is in force) | `mode = 'PAPER'` only; DEPLOY / ROLLBACK must name a HISTORICAL PAPER-state version of the same strategy, under a PAPER autonomy level; append-only |
| `model_runs.promoted_autonomy` | autonomy level of a model promotion | PAPER_READY needs a PAPER level; LIVE_CANDIDATE / LIVE_APPROVED need LIVE_CANDIDATE_APPROVAL |

## RL decisions (Phase 8 audit warning 2)

Every RL decision is persisted. An ENTER that cannot be written becomes a veto. A failed write suspends RL
for that strategy for the session: its entries are refused, and its open trades keep their deterministic
stops, targets and trailing.

## Running it

```
# SYNTHETIC research run (separate database under learning.research_dir; never the trading DB)
python -m learning synthetic --lot-units 65 --paper-days 25 --verify-days 12 --min-trades 5 --out reports/learning
```

```
# On the TRADING database, after the close (HISTORICAL verification data only)
python -m learning run --data path/to/historical-dataset
```

Or inside the PAPER session: set `learning.loop.enabled: true` and `learning.loop.verify_data` in
`config/settings.yaml`; the launcher (`python -m runtime paper`) runs the loop daily at `learning.loop.run_at`
(15:45 IST) in a worker thread with its own database connection, and refuses while the market is open or a
PAPER trade is open. Reports go to `learning.loop.reports_dir`.

**Autonomy is read live.** In the session, `autonomy_fn` is the running bot's level (`bot.mode.autonomy`,
the value the dashboard's SET_AUTONOMY command changes). `python -m learning run` in another process uses
`learning.autonomy_source.PublishedAutonomy`: the level in the bot's heartbeat (`runtime_state`, published
every second), or, when the heartbeat is missing, older than 10 s or unreadable, the level the bot would
start with (`persisted_autonomy`: the most restrictive of YAML and the last applied dashboard change;
anything unreadable resolves to OBSERVE_ONLY). It is read again at every promotion step, so a dashboard
switch to OBSERVE_ONLY stops the next PAPER_READY decision or deployment at once.

The PAPER bot journals automatically (`learning.journal_enabled`). The launcher applies the deployed PAPER
versions at start-up with `DeploymentController(...).resolve()` (unless the autonomy level is OBSERVE_ONLY
or `--baseline` is given); a deployment never changes a running session. Example output:
[`docs/learning-example/`](learning-example/learning_report.md) (SYNTHETIC).

## Limits

* Journal snapshots are taken at the first completed minute after an entry or exit (up to one minute of
  lag, never lookahead).
* Walk-forward windows are computed from one backtest over the whole non-holdout period. The candidate is
  fixed by its hypothesis, so there is no parameter selection inside the windows.
* MULTI_SEED resamples days. The rule-based backtest is deterministic, so seeds matter for models only.
* A PAPER deployment takes effect at the next PAPER session start (`python -m runtime paper`), never
  inside a running session.
