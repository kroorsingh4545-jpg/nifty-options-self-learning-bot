# Risk & Execution Audit Report
Date: 2026-11-13
Scope (files / PR): branch `paper-risk-3000` (off `main` e3f4da3). Owner risk decision of 2026-11-13 plus fix W5.
`config/hard_limits.py`, `config/settings.py`, `config/risk_limits.yaml`, `config/strategy_defaults.yaml`,
`risk/risk_manager.py`, `risk/daily_loss_guard.py`, `execution/position_manager.py`, `docs/safety-policy.md`,
`README.md`, `docs/rl-layer.md`, tests (`tests/unit/test_risk_3000_and_open_risk.py` new; `tests/fakes.py`
`with_per_trade_risk`; updated expectations in `test_config.py`, `test_daily_loss_guard.py`,
`test_risk_manager.py`, `test_rl_environment.py`; Rs 2,000-calibrated tests pinned to a Rs 2,000 config).
Full suite: 1380 passed; ruff and mypy clean.
Verdict: **PASS WITH WARNINGS**

## Results
| # | Area | Status (OK / WARN / FAIL / N/A) | Evidence (file:line, test) | Notes |
|---|---|---|---|---|
| 1 | Entry logic | OK | `risk/risk_manager.py:237-247` (open stop-risk before sizing; fail closed at `:244`); `runtime/gateway.py:45-50` | Approval and trade registration (`position_manager.open_trade`, `self.trades[uid] = trade` before the first `await`) have no await between them, so a second signal always sees the first trade's risk. Test `test_unknown_open_trade_risk_fails_closed`. |
| 2 | Exit logic | OK | Exits untouched (`execution/` exit paths unchanged) | The new check only gates entries; exits never consult it. |
| 3 | Stop-loss logic | OK | `compute_levels` unchanged; `stop_risk` (`risk_manager.py:93-105`) reads the current (trailed) stop | A stop trailed above entry counts as 0 risk, never negative (`test_stop_risk_is_quantity_times_distance_plus_slippage_and_charges`, `test_a_stop_trailed_into_profit_frees_the_room`). |
| 4 | Take-profit logic | OK | Unchanged | R-multiple targets scale with the stop distance, not the budget. |
| 5 | Trailing-stop logic | OK | `risk/trailing_stop_manager.py` unchanged | — |
| 6 | Position sizing | OK | `config/hard_limits.py:24-34`; `config/settings.py:479-480, 777-780` (3 % of capital); `risk_limits.yaml` 3000; `strategy_defaults.yaml` 3000 x2 | PAPER per-trade risk Rs 3,000 = 3 % of Rs 1,00,000. Sizer unchanged; still whole lots, ML/RL multiplier still only <= 1. `test_paper_per_trade_risk_is_3000_and_the_daily_limits_are_unchanged`; weakening cases now refuse 3,500 (`test_config.py`). |
| 7 | Daily loss limits | OK | `risk/daily_loss_guard.py:200-216`; `risk_manager.py:245-246` | W5 fixed: the stop-risk of all open trades (fills pending included, `position_manager.py:213-228`) is counted as already lost; a worse MTM still wins. Per-strategy limit stays Rs 5,000. Tests `test_open_stop_risk_is_counted_as_already_lost_per_strategy`, `test_a_worse_mark_to_market_still_wins`, `test_a_second_concurrent_trade_cannot_push_the_strategy_past_its_daily_limit`, `test_two_concurrent_trades_that_fit_stay_within_the_daily_limit_together`, `test_an_entry_still_filling_counts_in_full`. |
| 8 | Global loss limits | OK | `daily_loss_guard.py:211-212` | Sum of every strategy's open stop-risk against the global Rs 10,000 (unchanged). Tests `test_open_stop_risk_counts_globally_across_strategies`, `test_concurrent_trades_of_two_strategies_respect_the_global_limit`. |
| 9 | Margin checks | OK | Unchanged (`size_position` margin cap) | — |
| 10 | Order lifecycle | N/A | Not touched | — |
| 11 | Partial fills | OK | `position_manager.py:213-228` | A partially filled entry counts `max(open_qty, requested - exited)` at the worst limit price (entry + buffer ticks). |
| 12 | Order rejection handling | N/A | Not touched | — |
| 13 | Reconciliation | N/A | Not touched | — |
| 14 | Square-off logic | OK | Unchanged | — |
| 15 | Kill switch | OK | Unchanged; ML/RL/learning cannot reach risk state (`test_architecture.py::test_ml_can_never_reach_orders_or_risk_state`) | — |
| 16 | API rate limits | N/A | No new API calls | — |
| 17 | PAPER/LIVE isolation | OK | `config/loader.py:110` (LIVE refused, `LIVE_TRADING_AVAILABLE=False`); `config/settings.py:737-755, 768-769` LIVE ceiling Rs 2,000 / 2 % | Even if LIVE becomes available, a config above Rs 2,000 refuses to load with "must be re-decided by the owner before LIVE". Tests `test_live_refuses_a_per_trade_risk_above_2000`, `test_live_refuses_a_strategy_sizing_above_2000_even_with_the_limit_at_2000`, `test_live_accepts_2000`, `test_the_loader_still_refuses_live_outright`. |
| 18 | Restart recovery | OK | Restored open trades are in `PositionManager.trades`, so `open_exposures` includes them | No new persisted state. |
| 19 | Logging / audit trail | OK | `risk_manager.py` approval log adds `open_stop_risk`; rejection reason names the daily loss room left after open stop-risk; decision recorded in `docs/safety-policy.md` section 8 | Learning loop still cannot change any limit: `learning/mutation_engine.py:147-160` `NEVER_MUTABLE`; tests `test_no_risk_limit_is_on_the_learning_whitelist`, `test_the_learning_loop_cannot_change_the_per_trade_risk`, `test_learning_and_ml_code_never_writes_hard_limits`. |

## Blocking issues
None.

## Warnings
1. **Bigger losses per trade in PAPER.** One stop-out can now cost up to Rs 3,000 (60 % of the Rs 5,000 daily limit, was 40 %). After one full loss the remaining room (about Rs 2,000) is below the cost of a typical ATM lot, so most days allow at most one full-size loser per strategy. This is the owner's decision (recorded); it is PAPER only.
2. **The RL training environment keeps its own budget formula** (`ml/rl_environment.py:454-455`, `max_daily_loss - d.loss`) without the open-trade stop-risk. It is simulation-only (research training, never trading); the real gate is `RiskManager`. Align it in a later change so RL trains under the same rule.
3. **Existing PAPER deployments made under Rs 2,000** would differ from the YAML in `sizing.risk_per_trade` (not whitelisted) and fall back to the YAML config with a CRITICAL log at start-up. That is safe (it never loosens), and the trading database currently has no deployments.
4. **The generic ceiling `MAX_RISK_PER_TRADE_RS` is now the PAPER value (3,000).** The LIVE ceiling is enforced only by `BotConfig._live_per_trade_ceiling`. Any future LIVE code must use `MAX_RISK_PER_TRADE_RS_LIVE`, not the generic name.
5. Stop-risk is conservative: it adds full round-trip charges and counts locked-in profit as 0 rather than as room. This can refuse an entry slightly earlier than strictly needed.

## Required tests missing
1. A restart-with-open-trades test asserting the restored trades' stop-risk is counted before the first new entry. Covered by construction (same `trades` dict), not by a dedicated test.
2. A property test over random sequences of approvals, fills and stops, asserting that the realized loss plus open stop-risk never exceeds the limits.
