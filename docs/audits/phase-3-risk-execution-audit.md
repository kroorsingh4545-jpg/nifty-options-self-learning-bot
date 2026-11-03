# Risk & Execution Audit Report

Date: 2026-11-03
Scope (files / PR): Phase 3. Covers `risk/` (position_sizer, stop_loss_manager, trailing_stop_manager, daily_loss_guard, kill_switch, risk_manager) and `execution/` (order_manager, position_manager, reconciliation, square_off, stack). Also covers the supporting changes to `core/models.py`, `broker/paper_broker.py`, `data/storage/schema.sql` (v2) and `data/storage/repositories.py`.
Mode audited: PAPER (LIVE order routing does not exist; see #17)
Verdict: **PASS WITH WARNINGS**

## Results

| # | Area | Status | Evidence (file, test) | Notes |
|---|---|---|---|---|
| 1 | Entry logic | OK | `risk/risk_manager.py` `evaluate_entry`. Tests: `test_risk_manager.py` (time rules, kill switch, caps, stale quote, spread, premium) and `test_entry_blocked_by_kill_switch_at_order_layer` | Entries only come from an approved `RiskDecision` (`PositionManager.open_trade` raises otherwise). The order layer re-checks the kill switch. |
| 2 | Exit logic | OK | `position_manager._exit`, `order_manager.exit_position`. Tests: SL / TP / trailing / manual / square-off / kill-switch exits, `test_exit_failure_escalates_to_kill_switch` | Every open trade has a resting SL-M plus local tick monitoring. A failed exit puts protection back, sends a CRITICAL alert and triggers the kill switch. |
| 3 | Stop-loss logic | OK | `compute_levels`, `tighten_stop`, DB trigger `trg_trades_stop_tighten_only`. Tests: `test_stop_can_only_tighten`, `test_database_refuses_to_widen_stop`, `test_open_position_requires_stop_loss` | The stop is always set (NOT NULL), rounds up to the tick, and the schema refuses widening. |
| 4 | Take-profit logic | OK | `check_exit`. Tests: `test_exit_triggers`, `test_target_hit_cancels_protective_stop_then_exits` | The resting stop is cancelled before the target exit, so the exit is never double-sold. |
| 5 | Trailing-stop logic | OK | `risk/trailing_stop_manager.py`. Tests: all 4 modes, pullback never loosens, R-mode breakeven floor | The exchange SL-M trigger follows the trailing stop (`test_trailing_stop_tightens_moves_exchange_stop_and_exits`). |
| 6 | Position sizing | OK | `risk/position_sizer.py`. Tests: `test_position_sizer.py` including `test_never_exceeds_budget_property` | Whole lots. Slippage and round-trip charges count against the budget. The ML multiplier can only shrink size. |
| 7 | Daily loss limits | OK | `DailyLossGuard`. Tests: `test_strategy_limit_breach_includes_unrealized`, `test_strategy_loss_breach_flattens_only_that_strategy` | Includes MTM loss. A breach kills only that strategy and flattens it. Per-trade risk is capped by the remaining daily budget. |
| 8 | Global loss limits | OK | `on_global_breach` → `KillSwitch` → `SquareOffManager.on_kill`. Test: `test_global_loss_breach_full_sequence` | Verified: pending orders cancelled, all positions exited, all strategies KILLED, kill switch active, alerts sent, risk event persisted, entries refused. |
| 9 | Margin checks | WARN | `size_position` (margin cap), `PaperBroker._validate` (funds) | PAPER is covered. LIVE broker margin (Upstox funds plus the margin API) is not wired because LIVE does not exist yet. |
| 10 | Order lifecycle handling | OK | `LIFECYCLE` table, `transition_allowed`. Tests: `test_lifecycle_table`, `test_late_regressive_update_is_ignored`, `test_timeout_never_regresses_a_reported_order` | UNKNOWN is first-class. Illegal transitions are ignored, logged as risk events, and trigger a reconciliation. |
| 11 | Partial fills | OK | Tests: `test_partial_entry_fill_then_timeout_cancels_remainder`, `test_partial_exit_fills_complete_over_ticks` | Trade quantity becomes the filled quantity, and the protective stop is sized to the filled quantity. |
| 12 | Order rejection handling | OK | `_on_rejection`. Tests: `test_rejection_logged_alerted_and_not_retried`, `test_repeated_rejections_trigger_kill_switch` | Rejected entries are never retried. Three consecutive rejections trigger the kill switch. |
| 13 | Reconciliation | OK | `execution/reconciliation.py`. Tests: startup, debounced after order updates, periodic, pre/post square-off, mismatch → kill, unreachable broker → API-failure kill, UNKNOWN resolution | A mismatch is re-checked once (`confirm_delay`) before the kill switch fires. |
| 14 | Square-off logic | OK | `SquareOffManager.check`. Tests: phases, no action before 15:10, full flatten 15:10–15:20, retries, escalation at 15:20 | Reconciliation runs before and after. A position still open after 15:20 sends a CRITICAL alert and triggers the kill switch. |
| 15 | Kill-switch behaviour | OK | `risk/kill_switch.py`. Tests: `test_kill_switch.py` (scope, idempotency, handlers, failing handler, named-operator reset, persistence, restore) | ML/RL hold no reference to it. Reset needs a named human and is audited. |
| 16 | API rate-limit compliance | OK | Phase 2 limiter. The rate-limit trip hook maps to `KillReason.RATE_LIMIT_CIRCUIT` (`test_api_failures_and_rate_limit_trip`) | No new network paths in Phase 3 (architecture test). |
| 17 | PAPER/LIVE isolation | OK | `OrderManager` requires broker mode == repo mode. `build_stack` checks cfg/broker/repo mode. The factory refuses LIVE. `UpstoxRestClient` order methods refuse. The schema has mode-prefixed IDs. | There is no code path from PAPER to a real order. |
| 18 | Restart recovery | WARN | `DailyLossGuard.restore`, `KillSwitch.restore`, `PositionManager.restore`. Test: `test_restart_restores_trades_guard_and_kill_state` | Trades, stops, guard and kill state are restored. The PAPER broker's in-memory book is **not** persisted, so a mid-session PAPER restart is detected by startup reconciliation and halts trading. That is safe but disruptive. |
| 19 | Logging and audit trail | OK | Structured JSON logs with strategy and order IDs. `risk_events` is append-only. `orders`, `fills` and `trades` are persisted. | Secrets are masked (Phase 1/2). |

## Blocking issues
None.

## Warnings
1. **Gap risk beyond ₹2,000 per trade.** Sizing assumes the stop fills within 1 tick of slippage. A gap or wide spread through the stop can lose more than the per-trade budget. The daily strategy and global limits remain the backstop. Consider a configurable gap buffer in sizing.
2. **PAPER broker state is not persisted** (#18). A restart mid-session triggers the reconciliation kill. Persist the simulated book (Phase 4/5) if PAPER restarts must be seamless.
3. ~~**Synthesized fills record ₹0 charges.**~~ **RESOLVED.** Fills reconstructed from an order-book snapshot now use the configured cost model, with brokerage only on the order's first fill. They match the paper broker's actual charges exactly (`test_unknown_order_is_resolved_not_resent`, `test_recovered_partial_fill_charges_exclude_repeat_brokerage`, `test_recovered_exit_fill_charges_flow_into_trade_pnl`). For LIVE, these remain model estimates until the broker's own charge breakdown is reconciled.
4. ~~**No next-day reactivation.**~~ **RESOLVED in Phase 4.** `TradingBot.new_day` and `KillSwitch.roll_day` clear only previous-day kill events and statuses, and a mid-session restart keeps today's kills (`test_new_day_reactivates_only_previous_day_blocks`, `test_mid_session_restart_keeps_todays_kill`). Original note: `KILLED` / `PAUSED` strategy status persists across days. `bootstrap.sync_strategies` preserves it, and a new-day reset has not been implemented. It must be added to the Phase 4 scheduler, gated so that a breach day is not auto-cleared mid-session.
5. **Cancelled entries consume the daily trade cap.** This is conservative by design. Revisit if it proves too restrictive.
6. **LIVE-only paths are unexercised.** These include broker margin, mapping portfolio-stream updates to `client_order_id` via the Upstox `tag`, and exchange-side SL-M modify semantics. Re-audit before any LIVE enablement.

## Required tests missing
1. LIVE-adapter order lifecycle against mocked Upstox order endpoints (deferred: LIVE routing not implemented).
2. A long-running soak test with interleaved ticks and order updates across both strategies (Phase 4 engine).
