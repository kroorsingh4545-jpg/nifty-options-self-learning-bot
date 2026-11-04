# Risk & Execution Audit Report (Phase 5)

Date: 2026-11-04
Scope (files / PR): branch `phase-5-dashboard`, covering:
- `dashboard/` (UI only)
- `runtime/control.py` (control plane)
- `execution/operator_controls.py` (named kill-switch reset with strategy re-activation, manual exit, operator kill)
- `broker/paper_broker.py` `restore_book` (PAPER restart)
- schema v3 (`control_commands`, `runtime_state`), plus records and repositories
- wiring in `runtime/bot.py` and `execution/stack.py`

Also covers the fixes for Phase 4 audit warnings 2 (named reset must re-activate strategies) and 3 (PAPER restart).
Mode audited: PAPER (LIVE order routing does not exist)
Tests: 607 passed; ruff and mypy (strict) clean; Playwright browser QA against a synthetic PAPER bot passed every check (see `docs/dashboard.md`).
Verdict: **PASS WITH WARNINGS**

## Results

| # | Area | Status | Evidence (file:line, test) | Notes |
|---|---|---|---|---|
| 1 | Entry logic | OK | Entry path unchanged. `ENGINE_START` only sets RUNNING (`runtime/control.py:125-131`). Test: `test_engine_commands_never_stop_protection` | No dashboard command creates an entry. Every entry still passes through the risk manager. |
| 2 | Exit logic | OK | `execution/operator_controls.py:161-175` (manual exit through `PositionManager.exit_trade`). Tests: `test_manual_exit_command`, `test_manual_exit_with_confirmation`; browser: exit reason MANUAL plus `OPERATOR_EXIT` event | A failed manual exit still escalates through the Phase 3 EXIT_FAILURE path. |
| 3 | Stop-loss logic | OK | Unchanged. Restored resting stops keep their current trigger (`execution/order_manager.py:330` persists modifications). Test: `test_paper_restart_restores_book_so_reconciliation_passes` (the restored stop fills) | |
| 4 | Take-profit logic | OK | Unchanged | |
| 5 | Trailing-stop logic | OK | Unchanged; the trailed trigger is persisted and restored | |
| 6 | Position sizing | OK | Unchanged; the dashboard has no sizing input | |
| 7 | Daily loss limits | OK | `operator_controls.py:104` refuses an intraday reset of a daily-loss kill; `:126-137` keeps a breached strategy KILLED. Test: `test_reset_refused_for_same_day_daily_loss_kill` | |
| 8 | Global loss limits | OK | Same rule, plus `guard.global_breached` blocks re-activation (`operator_controls.py:132`) | |
| 9 | Margin checks | OK | Unchanged | |
| 10 | Order lifecycle handling | OK | Unchanged. `restore_book` skips CREATED and UNKNOWN orders and keeps terminal statuses (`broker/paper_broker.py:124-152`) | A skipped UNKNOWN order that did fill would show up as a startup mismatch, which fails safe. |
| 11 | Partial fills | OK | Restore replays each fill through the same `_book_fill` accounting as live fills (`paper_broker.py:448`) | |
| 12 | Order rejection handling | OK | Unchanged | |
| 13 | Reconciliation | OK | A reset reconciles first and refuses on any mismatch (`operator_controls.py:107-114`). Test: `test_reset_refused_when_reconciliation_fails` | In PAPER the book is rebuilt from our own fills. Reconciliation still compares open trades with the book, so it can still catch a mismatch between them. |
| 14 | Square-off logic | OK | Dashboard "Stop" calls only `engine.stop()` (`runtime/control.py:134`). The scheduler and square-off keep running. Test: `test_engine_commands_never_stop_protection` (square-off flattens while STOPPED) | |
| 15 | Kill-switch behavior | OK | Kill never expires (`dashboard/commands.py:48-51`, `runtime/control.py:95`). Named reset, then explicit re-activation with audit (`operator_controls.py:94-159`). Tests: `test_kill_command_halts_and_never_expires`, `test_strategy_kill_command_is_isolated`, `test_named_reset_clears_kill_and_reactivates_strategies`, `test_strategy_reset_keeps_strategy_killed_while_global_kill_active`, `test_kill_switch_click_confirm_reset_and_reactivation`; browser: kill → KILLED → reset → ACTIVE, audited | Phase 4 warning 2 is fixed. The dashboard cannot bypass the kill switch, and ML/RL still have no reference to it. |
| 16 | API rate-limit compliance | WARN | `runtime/control.py:183` calls `broker.get_funds()` every second | Free for the paper broker. Against a real Upstox REST client it would use about 60 of the roughly 400 effective standard calls per minute (warning 5). |
| 17 | PAPER/LIVE isolation | OK | Commands are mode-scoped (repository `_guard`). LIVE is evaluated and refused (`runtime/control.py:141-147`; `live_available` False at `:261`). A wrong phrase is rejected before queueing (`dashboard/commands.py:77-79`). An architecture test forbids broker/execution/risk/runtime imports in the dashboard (`test_dashboard_is_ui_only`). Tests: `test_live_request_is_refused_with_every_reason`, `test_live_rejects_wrong_text_and_backend_refuses_live` | There is no path from the dashboard to a real order. |
| 18 | Restart recovery | OK | `runtime/bot.py:192` restores the paper book before reconciling. Test: `test_paper_restart_restores_book_so_reconciliation_passes` (no phantom mismatch, cash matches, resting stop works) | Phase 4 warning 3 is fixed. |
| 19 | Logging and audit trail | WARN | Commands are final once processed (trigger `data/storage/schema.sql:329`; `test_processed_command_is_final`). Operator name on every command, reset and re-activation. Failure text masked (`runtime/control.py:105`). Auth uses constant-time comparison with a lockout (`dashboard/auth.py:20`, `:37-38`) | See warnings 2 and 3: the server binds to every interface and uses one shared account. |

## Blocking issues

None.

## Warnings

1. **A slow manual exit can delay a queued kill.** The control job processes commands one at a time (`runtime/control.py:88-110`), and the scheduler skips overlapping runs. An exit that keeps failing retries for about 30 s (5 attempts, up to 5 s backoff plus a 2 s wait each). During that time a queued KILL waits and the heartbeat goes stale, so the dashboard shows OFFLINE. The repeated failure ends in its own EXIT_FAILURE kill anyway. Recommendation: process KILL commands first and run exits as background tasks.
2. **The dashboard is reachable from the network, unencrypted.** With no `.streamlit/config.toml`, Streamlit listens on every network interface over plain HTTP. The login and the kill and reset controls are therefore exposed on the LAN, and the password crosses the network in clear text. Recommendation: bind to `127.0.0.1` by default, and require TLS through a reverse proxy for remote access.
3. **There is only one dashboard account.** The "named operator" is always the single configured username, so you can't tell which person acted. Per-user accounts are needed before LIVE.
4. **The dashboard migrates the database schema.** It runs `db.initialize()` on the trading database at startup (`dashboard/data_access.py:41`), so it could upgrade the schema under an older running bot. The changes are additive only, but migrations belong to the bot; the dashboard should check the version and refuse on a mismatch.
5. **Funds are fetched every second** (area 16). Cache them before any real broker client is wired in.
6. **A reset can re-activate strategies that still have open positions.** That is the operator's deliberate decision after reconciliation, but the reset form doesn't list the open positions in scope.
7. **Carried over from Phase 4:**
   - Trade exit and order update times use the real clock (Phase 4 warning 4). Visible under simulated time only; chart markers outside the plotted window are now left out.
   - The instrument list is parsed on the event loop (Phase 4 warning 1).
   - There is no production PAPER runner wiring real Upstox connection and rate-limit status into the bot.

## Required tests missing

1. A KILL queued behind a slow or failing EXIT: measure and bound the latency (after warning 1 is fixed).
2. The dashboard's bind address, or a startup check for it (after warning 2 is fixed).
3. `restore_book` with a cancelled, partially filled order. The shared fill accounting covers it, but no dedicated test does.
4. Bot and dashboard writing to SQLite at the same time under load. The current tests run sequentially.
