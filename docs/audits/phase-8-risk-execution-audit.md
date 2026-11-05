# Risk & Execution Audit Report

Date: 2026-11-05 (re-audited 2026-11-05 after the resting-SELL invariant fix for warnings 1 and 2)
Scope (files / PR): branch `phase-8-rl` vs `main`
- first audit (02256ce..68f761a): new `ml/rl_environment.py`, `ml/reward_design.py`, `ml/rl_agent.py`, `runtime/rl.py`; changed `ml/inference_service.py`, `ml/model_registry.py`, `ml/feature_engineering.py`, `ml/__main__.py`, `execution/position_manager.py`, `risk/stop_loss_manager.py`, `core/interfaces.py`, `runtime/bot.py`, `runtime/ml.py`, `backtest/backtester.py`, `config/settings.py`, `config/strategy_defaults.yaml`, `options/pricing.py`, `pyproject.toml`
- re-audit (invariant fix): `execution/order_manager.py`, `execution/position_manager.py`, new `tests/unit/test_resting_sell_invariant.py`
- tests: `test_rl_environment.py`, `test_rl_reward.py`, `test_rl_agent.py`, `test_rl_inference.py`, `test_resting_sell_invariant.py`

Mode audited: PAPER and backtest. LIVE order routing does not exist (`runtime/bot.py:122`).
Data: SYNTHETIC only; no RL model has been trained on real data.
Tests: **1035 passed** (925 at the first audit + 110 in `test_resting_sell_invariant.py`). Run against the pre-fix
execution code, **43 of the 110 new tests fail**. ruff and mypy (strict, 115 files) clean.
Verdict: **PASS WITH WARNINGS**

## Results

| # | Area | Status (OK / WARN / FAIL / N/A) | Evidence (file:line, test) | Notes |
|---|---|---|---|---|
| 1 | Entry logic | OK | RL entry decision chained after the ML filter (`runtime/bot.py:270`). `ChainedFilter` (`core/interfaces.py:65-94`): every filter must allow; multipliers multiply and are clamped to [0, 1]; an exception vetoes. `RLTradeAdvisor.evaluate` (`ml/inference_service.py:331-352`) is fail-closed, offers only ENTER / SKIP (`:347`), and keeps multiplier 1.0 on ENTER. Tests: `test_chained_filter_can_only_veto_or_shrink`, `test_entry_approval_can_only_veto`, `test_backtest_with_rl_enabled_but_no_models_refuses_entries`, `test_backtest_skip_everything_takes_no_trades` | RL can only veto. `RiskManager` still sizes and approves every entry. |
| 2 | Exit logic | OK | A full RL exit goes through `request_exit` → `_exit`, labelled `TRADE_MANAGER` (`execution/position_manager.py:76`). A stop / target hit during a partial escalates to a full exit (`request_exit`, `_partial_exit` finally). `exit_trade` exits the rest after a partial. An exit is never sent while the resting stop may be live (`:495`) or while other SELL orders are live (`:501-503`). Tests: `test_full_exit_is_labelled_trade_manager`, `test_a_stop_hit_during_a_partial_exit_exits_the_rest`, `test_full_exit_is_not_sent_when_the_stop_cannot_be_cancelled`, `test_stop_filled_during_the_cancel_is_never_sold_twice` | Refusing to sell when a cancel is unconfirmed is deliberate: the resting stop keeps protecting the long. See warning 1. |
| 3 | Stop-loss logic | OK | `apply_advice` rounds the stop up to the tick, requires it strictly below the LTP, and goes through `tighten_stop` (raises on widening). Same rule in the simulator (`ml/rl_environment.py:371`, assert `:770`). Initial stop always from strategy config. The protective SL-M can never be larger than the position (resting-SELL invariant, area 10). Tests: `test_stop_actions_only_ever_tighten` (~10k cases), `test_unsafe_advice_is_refused`, `test_tighten_moves_stop_and_the_resting_order`, backtest `stop_loss >= initial_stop_loss` | Never removed or widened. |
| 4 | Take-profit logic | OK | Targets unchanged and from config; RL has no target action. Partial profit only in profit (`apply_advice`) | |
| 5 | Trailing-stop logic | OK | `TrailingStopManager` unchanged; it moves only when its candidate exceeds the current stop, so an RL-tightened stop is never lowered | Restart: warning 4. |
| 6 | Position sizing | OK | RL has no size action (`ml/rl_environment.py:97`). Live sizing is still `RiskManager`. A partial exit keeps at least one lot; `keep` is fixed when the partial is requested (`execution/position_manager.py:584`), re-checked when it runs (`:600`), and the sell is clamped to what is held (`:615`). Tests: `test_envelope_sizing`, `test_risk_limits_hold_under_any_policy`, `test_stop_fill_between_partial_request_and_run_sells_nothing` | |
| 7 | Daily loss limits | OK | Untouched. `runtime/rl.py` only reads `guard.state`. Static test `test_rl_runtime_never_touches_risk_state`. The env halts and force-exits at the limit (`ml/rl_environment.py:992`) | Warning 3. |
| 8 | Global loss limits | OK | Untouched (same evidence as 7). The envelope uses the min of the strategy, per-strategy and global limits | |
| 9 | Margin checks | OK | Unchanged: `RiskEntryGateway` / funds | |
| 10 | Order lifecycle handling | OK | **Resting-SELL invariant (warnings 1 + 2 fixed).** For every trade, the unfilled quantity of live SELL orders (protective + exits) is never more than the quantity held. Mechanisms: <br>• protective placement and cancellation are serialised by `ManagedTrade.protect_lock` (`execution/position_manager.py:130`, `:328`, `:356`), so an exit waits for an in-flight placement, then cancels it; <br>• the protective stop is sized to `sellable_qty` (`:318-325`); <br>• a cancel counts only when the broker confirms it is terminal (`OrderManager.cancel_confirmed`, `execution/order_manager.py:427`); <br>• an unconfirmed cancel aborts the full exit and escalates with a CRITICAL alert and the kill switch (`position_manager.py:495`), and the partial is abandoned instead (`_partial_exit`); <br>• exit retries never re-send while the previous attempt may still be live (`order_manager.py:408`); <br>• a stop left on a closed trade escalates (`position_manager.py:384-386`); <br>• an oversell fill escalates (`:281-282`). <br>Tests: `test_resting_sell_invariant.py` (110 tests). An order-event monitor asserts the invariant after every event, and the broker position must never go short. The broker accepts oversells (`allow_short_options=True`, like a real exchange), so only execution keeps the invariant. 100 randomised seeds mix ticks, partials, trails, exits, kills and refused cancels | The paper broker's own oversell guard (`broker/paper_broker.py:252-257`) had masked these bugs in PAPER. Production config pins it to off-limits (`config/settings.py:172`, `Literal[False]`). |
| 11 | Partial fills | OK | `remaining_qty` clamps to `open_qty` and `open_qty - keep`. A working exit order counts against the protective quantity (`sellable_qty`). Tests: `test_partial_profit_keeps_a_lot_and_its_protection`, `test_exit_retry_never_resends_while_the_previous_order_may_be_live` (thin book), `test_protective_stop_is_sized_to_the_unreserved_quantity` | |
| 12 | Order rejection handling | OK | Refused advice is logged with a reason and never retried. Exit retries stop rather than re-send when a cancel is unconfirmed | |
| 13 | Reconciliation | OK | `net_quantities` uses `open_qty`. A mismatch triggers the kill switch (`execution/reconciliation.py:166`) | |
| 14 | Square-off logic | OK | Advice is refused at or after 15:10 in `apply_advice`, the advisor (`ml/inference_service.py:381`) and the runtime (`runtime/rl.py:74`). A square-off during a partial waits, then exits the rest. Tests: `test_advice_is_refused_when_rl_is_off_in_square_off_or_while_exiting`, `test_the_agent_cannot_hold_past_square_off` | RL never controls square-off timing. Warning 1 covers an unconfirmable cancel near 15:10. |
| 15 | Kill-switch behavior | OK | RL is skipped while `ks.is_blocked(sid)` (`runtime/rl.py:79`). There is no RL import of `risk/` or `execution/` (architecture test). A kill during a partial flattens everything. The new escalations use the existing idempotent `KillSwitch.trigger` (handlers run once). Tests: `test_kill_switch_during_a_partial_exit_flattens_everything`, `test_full_exit_is_not_sent_when_the_stop_cannot_be_cancelled`, randomised kills | RL cannot override or reset it. |
| 16 | API rate-limit compliance | OK | Every action goes through `OrderManager` and the limiter. `cancel_confirmed` makes at most 3 cancel calls per order, with backoff | |
| 17 | PAPER/LIVE isolation | OK | `TradingBot` is PAPER only (`runtime/bot.py:122`). Research RL is PAPER only (`ml/inference_service.py:278`). SYNTHETIC models are refused without `research=True` (`:286`). `load_rl_models` returns HISTORICAL deployable models only (`:245`). The trading registry refuses SYNTHETIC, and the database trigger blocks promotion. Tests: `test_synthetic_rl_model_can_never_become_production`, `test_cli_synthetic_rl_run_never_touches_the_trading_database`, `test_backtest_refuses_synthetic_rl_models_without_research_flag`, `test_advisor_model_policy` | A SYNTHETIC-trained RL model can never be production. |
| 18 | Restart recovery | OK | `entry_time` restored. Protective order re-linked from the orders table. Artefacts sha256-verified and loaded with `weights_only=True` (`ml/rl_agent.py:266`). Test: `test_tampered_artifacts_are_refused` | Warning 4. |
| 19 | Logging and audit trail | WARN | Applied and refused advice is logged with the model version. Every invariant escalation writes a CRITICAL alert and an `EXIT_FAILED` risk event (`order_manager.py:444`) | Warning 2: RL decisions are not persisted to the database. |

## Blocking issues

None.

## Warnings

1. **NEW (by design): an unconfirmable cancel blocks the exit.** If the broker never confirms a protective-stop cancel, the bot no longer sends the exit (selling could double-sell). Instead it alerts CRITICAL, records `EXIT_FAILED` and triggers the kill switch. The long stays protected by its resting stop, and the loss is bounded by the premium. Square-off retries on each check and escalates `SQUARE_OFF_INCOMPLETE` after 15:20. A human must resolve it at the broker. This trades liveness for never going short; keep it.
2. **RL decisions are logs only.** Advice is not written to `risk_events` / `learning_events`, and the bot snapshot keeps the last 200. Persist decisions for Phase 9 reflection. (Was warning 3.)
3. **Partial-exit realised P&L reaches `DailyLossGuard` only at close.** This is conservative: only profitable partials are allowed. (Was warning 4.)
4. **Restart.** `partial_exits` resets to 0 for restored trades (at least one lot always stays). An RL-tightened stop restores as an active trailing stop (tighten-only). (Was warning 5.)
5. **Under-protection while a sell is stuck.** When a working exit order cannot be cancelled, the protective stop is sized to the unreserved quantity. If that order later cancels, the stop covers less than is held until the next re-protection. Local tick monitoring still covers the full position. This is safe in direction (never over), but can be thinner than the position.
6. **`closed_outcomes` still loads all closed trades, then keeps the last 20** (`runtime/ml.py:66`). (Was warning 6.)
7. **Training/serving gaps** (documented in `docs/rl-layer.md`): one trade at a time in the environment, per-day recent outcomes in simulation, and a static baseline without trailing. (Was warning 7.)
8. **Carried from Phases 5–7**, unchanged.

Resolved since the first audit:
- **Warning 1 (cancel result not checked before the partial sell) — FIXED.** See area 10.
- **Warning 2 (race with a pending protective placement) — FIXED** by `protect_lock`. The partial now waits for the placement and then cancels it. Tests: `test_partial_exit_waits_for_an_inflight_protective_placement`, `test_full_exit_waits_for_a_slow_protective_placement`.
- **Found by the new randomised test and FIXED:** `_partial_exit` computed `keep` when the task started, not when the partial was requested. If the resting stop filled the whole position in between, `keep` went negative and the partial sold one lot per retry on a flat position (a short of up to 5 lots on a broker without an oversell guard). It is now fixed at request time, re-checked and clamped. Test: `test_stop_fill_between_partial_request_and_run_sells_nothing`.

## Required tests missing

1. Restart mid-session with an RL-managed, partially exited trade.
2. A latency bound for `RLRuntime.step` with real 5-seed models and 2 open trades per strategy.
3. The invariant against the real Upstox adapter's asynchronous cancel acknowledgements (cancel pending, then the cancelled order update), before any LIVE work.
4. End-to-end HISTORICAL path (train → VALIDATED → PAPER_READY → `load_rl_models` → bot), once real data exists.

---

### First audit (2026-11-05), for the record

Verdict: PASS WITH WARNINGS. Area 10 was WARN for two issues:
- (1) `_partial_exit` ignored the cancel result before selling;
- (2) a partial could race with an in-flight protective-stop placement.

Either could leave resting SELL quantity above the position. Both are fixed above. Warnings 3–8 of that audit are carried as warnings 2–8 here.
