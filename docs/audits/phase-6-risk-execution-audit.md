# Phase 6 Risk & Execution Audits

This file holds the audits of branch `phase-6-backtesting`, in order: the initial audit (PASS WITH WARNINGS), then the re-audit after the fixes for warnings 1 and 2 and the five missing tests, appended at the end.

---

# Risk & Execution Audit Report (Phase 6, initial)

Date: 2026-11-04
Scope (files / PR): branch `phase-6-backtesting` (uncommitted working tree), covering:
- the new `backtest/` package: `backtester.py`, `cost_model.py`, `data.py`, `synthetic_data.py`, `metrics.py`, `report_generator.py`, `walk_forward.py`, `purged_cv.py`, `__main__.py`
- the clock-injection fix: `data/storage/repositories.py`, `data/storage/records.py`, `data/storage/bootstrap.py`, `execution/stack.py`, `runtime/bot.py`, `core/event_bus.py`
- the exit-label change in `execution/position_manager.py`
- test changes: `tests/unit/test_architecture.py`, `tests/unit/test_execution_flow.py`

Mode audited: PAPER and backtest. LIVE order routing does not exist.
Tests: 710 passed (635 existing + 75 new); ruff and mypy (strict) clean.
Verdict: **PASS WITH WARNINGS**

## Results

| # | Area | Status | Evidence (file:line, test) | Notes |
|---|---|---|---|---|
| 1 | Entry logic | OK | Backtests run the real `TradingBot` (`backtest/backtester.py:406`), so every entry still goes signal → `RiskEntryGateway` → `RiskManager.evaluate_entry` → `PositionManager.open_trade`. Test: `test_backtest_trades_both_strategies_through_the_real_pipeline` (approved decisions = trades created) | The backtester has no entry path of its own. |
| 2 | Exit logic | OK | Unchanged exit code, exercised on simulated time. Tests: `test_backtest_trades_both_strategies_through_the_real_pipeline` (TARGET, TRAILING_STOP and SQUARE_OFF exits all occur), `test_no_entries_after_cutoff_and_flat_by_square_off` | See warning 1: exit retries all happen at one simulated instant. |
| 3 | Stop-loss logic | OK | Stop placement and tightening unchanged. Backtest overrides are re-validated (`backtester.py:233-253`). `StopLossConfig` requires value > 0 and at most 50 % (`config/settings.py:403-409`), so a stop can't be removed. The report checks that every trade had a stop (`backtest/metrics.py`). Tests: `test_overrides_are_revalidated`, `test_capital_and_risk_overrides_are_refused` | |
| 4 | Take-profit logic | OK | Unchanged; TARGET exits occur in the backtest | |
| 5 | Trailing-stop logic | OK | `execution/position_manager.py:236-242` changes only the label: a resting SL-M fill after the stop was trailed is now recorded as TRAILING_STOP. No consumer branches on the `exit_reason` string (only the repositories store it). Tighten-only logic unchanged. Tests: `test_trailing_stop_tightens_moves_exchange_stop_and_exits` (now asserts TRAILING_STOP), `test_untrailed_exchange_stop_fill_is_a_stop_loss` | |
| 6 | Position sizing | WARN | Unchanged `size_position`. Backtest lot sizes come from the dataset's instrument list. Overrides may change only the reduce-only `size_multiplier` (`backtester.py:70`, `:245`; field `le=1.0` at `config/settings.py:400`). | Warning 2: the synthetic generator holds a default contract size of 65 (`backtest/synthetic_data.py:47`) that the lot-literal architecture check doesn't catch. |
| 7 | Daily loss limits | OK | `DailyLossGuard` unchanged; it rolls each simulated day through the real `start_of_day` job. The report flags per-strategy daily overshoots (`metrics.py:230`). Test: `test_compliance_flags_breaches_and_overshoots` | Overshoot through gaps is reported as WARN, not hidden. |
| 8 | Global loss limits | OK | Unchanged. Global daily overshoot check at `metrics.py:235`. Days the kill switch was active at the close are recorded (`backtester.py:445`) | |
| 9 | Margin checks | OK | The paper broker's funds check and the gateway's `get_funds` are unchanged and run on every backtest entry | |
| 10 | Order lifecycle handling | OK | `OrderManager` unchanged. Timestamps now come from the injected clock (`repositories.py:185`, `:241`). Test: `test_updates_and_closes_use_the_repository_clock` | |
| 11 | Partial fills | OK | Paper broker accounting unchanged. Backtest depth is 20 lots per side (`cost_model.py` `SpreadModel.depth_lots`) | Partial fills aren't exercised by a backtest test (missing test 4). |
| 12 | Order rejection handling | OK | Unchanged; rejections go through the same path and are counted in the report's pipeline section | |
| 13 | Reconciliation | OK | Startup, start-of-day and periodic reconciliation run on simulated time through the bot's own jobs (`backtester.py:435`) | The backtest sets `reconcile_confirm_delay=0` (`backtester.py:408`), so a transient mismatch kills immediately. That errs on the safe side and affects backtests only. |
| 14 | Square-off logic | OK | The real square-off job runs on simulated time. Trades still open after the close are recorded (`backtester.py:443`) and reported as a BREACH. Tests: `test_no_entries_after_cutoff_and_flat_by_square_off`; a SQUARE_OFF exit occurs in the fixture | |
| 15 | Kill-switch behavior | OK | Unchanged; there is no ML/RL path. A kill persists across simulated days like in PAPER. Superseding resets and named reset unchanged | No backtest test triggers a kill (missing test 5). |
| 16 | API rate-limit compliance | WARN | Backtests make no Upstox calls: `HistoricalSource` (`backtester.py`); architecture test `test_backtest_never_uses_the_upstox_adapters` forbids `broker.upstox_rest`, `broker.upstox_websocket` and `broker.factory`. The control-plane job is skipped (`backtester.py:63`) | Carried over: in PAPER, funds are still read every second (`runtime/control.py:183`). |
| 17 | PAPER/LIVE isolation | OK | `Backtester` refuses non-PAPER modes (`backtester.py:351`), and `TradingBot` refuses them again (`runtime/bot.py:119-120`). Only `PaperBroker` is used (`:397`). The trading database and existing files are refused (`:380`, `:382`). Tests: `test_trading_database_and_existing_files_are_refused`, `test_backtest_never_uses_the_upstox_adapters`, `test_only_entry_points_import_runtime` (backtest allowed as a composition root) | There is no path from a backtest to a real order. |
| 18 | Restart recovery | OK | With the default clock (`now_ist`) production behaviour is unchanged: the bot rebinds repositories to its `now_fn` (`runtime/bot.py:123`, `execution/stack.py:81`), which is the wall clock in PAPER. Kill-switch and guard restore read the same timestamps as before. Existing recovery tests pass (`test_day_rollover_and_recovery.py`, `test_paper_restart_restores_book_so_reconciliation_passes`) | |
| 19 | Logging and audit trail | WARN | The carried-over real-clock warning is **fixed**. Every record timestamp left at its default is stamped with the repository clock (`repositories.py:103-119`, applied in every insert and upsert). Updates and closes use the clock (`:241`, `:304`, `:310`, `:345`, `:380`). `sync_strategies` uses it too (`bootstrap.py:29`), and so does the event bus (`core/event_bus.py:47-59`). Tests: `test_backtest_persists_only_simulated_time` (scans every timestamp column in orders, fills, positions, trades, risk_events, daily_pnl and strategies), `test_defaulted_record_timestamps_are_stamped_with_the_repository_clock`, `test_explicit_timestamps_are_kept`, `test_sync_strategies_uses_the_repository_clock`, `test_event_bus_uses_the_injected_clock` | Warnings 3 and 4: log lines keep wall-clock times, and an on-disk backtest database looks like a PAPER session. |

## Blocking issues

None.

## Warnings

1. **Exit retries in a backtest all happen at one simulated instant.** `sim_sleep` (`backtester.py:98`) yields to the event loop but doesn't advance the clock. If an exit can't fill (for example a missing option candle near square-off), its 5 attempts and 3 rejections all happen against the same stale quote. That escalates straight to REPEATED_REJECTIONS / EXIT_FAILURE kills, where live trading would retry across a few seconds of new ticks. This fails safe and is reported as a breach, but it overstates kills on data with gaps.
2. **The synthetic generator contains a hard-coded contract size.** `SyntheticSpec.lot_units: int = 65` (`synthetic_data.py:47`) only populates SYNTHETIC instrument lists, and the backtester still reads lot sizes from the dataset. The name slips past the `LOT_LITERAL` architecture check, which otherwise exists to catch exactly this kind of constant.
3. **Backtest log lines carry wall-clock times.** The JSON logger stamps `ts` with the real time, while every database record is simulated time. Backtest logs can only be matched to trades through order and trade ids. If a backtest ran inside a process already logging to `logs/bot.jsonl`, its PAPER-mode order lines would mix with real paper-session logs. The CLI logs to the console only.
4. **An on-disk backtest database looks like a PAPER session.** With `database_path`, the backtest writes ordinary PAPER rows (the schema only knows PAPER and LIVE). Pointing the dashboard at that file would show simulated trades as PAPER activity. The default is in memory.
5. **`with_settings_overrides` can change the `execution` and `market_session` sections** (`backtester.py:259`), for example turning off exchange-side stops or removing holidays. It's re-validated and used only by the backtester, but it returns a normal `BotConfig`. Nothing passes it to a live PAPER bot today, and it should stay that way.
6. **Carried over from Phase 5:** a reset waiting on a slow flatten holds up the control loop; funds are read every second; there is one shared dashboard account; a reset can re-activate strategies with open positions; `--allow-remote` relies on the operator to provide TLS; the instrument list is parsed on the event loop; there is no production PAPER runner.

## Required tests missing

1. A backtest with a data gap at square-off (an option missing candles after 15:10): check the escalation and that the report shows the BREACH and the kill.
2. Exit-retry timing on simulated time (bounds warning 1, or covers a fix that advances simulated time in `sim_sleep`).
3. A backtest crossing a monthly futures expiry: the contract roll and VWAP+EMA re-subscription on simulated time.
4. A backtest with depth smaller than the order size, so partial fills happen in the replay.
5. A backtest day that breaches a daily loss limit: the kill switch acts and carries over correctly to the next simulated day.
6. Carried over: the bot and the dashboard writing to SQLite at the same time under load.

---

# Risk & Execution Audit Report (Phase 6 re-audit after fixes for warnings 1 and 2 and the five missing tests)

Date: 2026-11-04
Scope (files / PR): branch `phase-6-backtesting`, covering:
- warning 1 fix: `backtest/backtester.py` (`SimClock.sleep`, `settle_simulation`, task-based `SimulatedScheduler`, end-of-run `_stop`, day-start observations); `execution/order_manager.py` `wait_terminal` (waits on the injected `sleep`)
- warning 2 fix: `backtest/synthetic_data.py` (`SyntheticSpec.lot_units` required, no default); `backtest/__main__.py` (`--lot-units`); `tests/unit/test_architecture.py` (wider `LOT_LITERAL`)
- the new scenario tests in `tests/unit/test_backtest_scenarios.py`, plus all other Phase 6 code

Mode audited: PAPER and backtest. LIVE order routing does not exist.
Tests: 725 passed; ruff and mypy (strict) clean.
Verdict: **PASS WITH WARNINGS**

## Results

| # | Area | Status | Evidence (file:line, test) | Notes |
|---|---|---|---|---|
| 1 | Entry logic | OK | Unchanged since the initial audit (real `TradingBot`, `backtester.py:486`) | |
| 2 | Exit logic | OK | Exit retries and backoffs now wait on simulated time: the bot's `sleep` is `SimClock.sleep` (`backtester.py:486`, `:125-137`). Jobs run as tasks, so a retrying square-off never blocks the feed (`:178-197`). Tests: `test_exit_retries_run_on_simulated_time` (≥5 attempts at distinct instants from 15:10, spanning ≥1 minute), `test_a_slow_job_is_never_overlapped_and_does_not_block_the_replay`, `test_sim_clock_sleep_parks_until_simulated_time_passes` | Initial warning 1 is fixed. |
| 3 | Stop-loss logic | OK | Unchanged. While a failed exit retries, protection is put back each time (resting STOP_LOSS orders re-placed between attempts in the gap scenario) | |
| 4 | Take-profit logic | OK | Unchanged | |
| 5 | Trailing-stop logic | OK | Unchanged since the initial audit | |
| 6 | Position sizing | OK | `SyntheticSpec` is keyword-only with a required `lot_units` (`synthetic_data.py:42-46`); the CLI refuses synthetic data without `--lot-units` (`__main__.py:47`). `LOT_LITERAL` now flags any lot-named identifier assigned a literal (`test_architecture.py:149`) and the whole codebase passes it. Tests: `test_synthetic_spec_has_no_default_contract_size`, `test_lot_literal_pattern` (new cases), `test_no_hardcoded_lot_sizes_in_production_code`, `test_cli_synthetic_run_writes_a_report` | Initial warning 2 is fixed: no lot-size constant remains in production code. |
| 7 | Daily loss limits | OK | Test: `test_daily_loss_kill_blocks_the_day_and_clears_next_morning`. With ₹1,000 limits, both strategy kills (STRATEGY_DAILY_LOSS) fire on 09-09, nothing new opens for that strategy afterwards, a VWAP signal is rejected by the kill, both kills auto-clear at 09:00 simulated time on 09-10, and both strategies trade again | Re-activation depends on the clock fix (strategy `updated_at` in simulated time). |
| 8 | Global loss limits | OK | Guard and kill switch unchanged. The global-kill path is exercised in a backtest by the persistent kill in the gap scenario | A global daily-loss breach in a backtest is not tested yet (missing test 1). |
| 9 | Margin checks | OK | Unchanged | |
| 10 | Order lifecycle handling | OK | `wait_terminal` now races the terminal event against the injected `sleep` (`order_manager.py:333-350`). In PAPER that sleep is `asyncio.sleep`, so behaviour is the same as the old `asyncio.wait_for`; both waiters are cancelled and collected in `finally`. Tests: `test_wait_terminal_times_out_on_the_injected_clock`, all execution tests (108) | Touches execution code; production semantics are unchanged. |
| 11 | Partial fills | OK | Test: `test_partial_fills_keep_positions_and_pnl_consistent` (depth of 1 lot; orders fill over 2-3 ticks; for every trade, bought = sold = trade quantity, charges = sum of fill charges, net = gross − charges, no fill above the depth) | Initial missing test 4 is added. |
| 12 | Order rejection handling | OK | Gap scenario: every exit rejected on missing data is logged as ORDER_REJECTED with the reason; 3 in a row trigger REPEATED_REJECTIONS (`test_data_gap_at_square_off_escalates_and_is_reported`) | |
| 13 | Reconciliation | OK | The debounce after order updates now waits 2 s of simulated time (next step) instead of 0 | Unchanged: `reconcile_confirm_delay=0` in backtests (`backtester.py:487`, comment at `:483`), so a transient mismatch kills immediately (fail-safe). |
| 14 | Square-off logic | WARN | Test: `test_data_gap_at_square_off_escalates_and_is_reported` (option data stops at 15:00: retries from 15:10, kill, `open_after_close` recorded, report FAIL with "flat after square-off" BREACH); `test_without_the_gap_the_trade_is_squared_off_intraday` | Warning 2: the stranded position is held overnight. The paper broker doesn't model the broker's own intraday auto square-off. |
| 15 | Kill-switch behavior | OK | Persistent kill carried into the next day: all strategies blocked and KILLED at the open, nothing opened (`_observe_day_start`, `backtester.py:551-557`; gap-scenario test). Day-scoped kill cleared next morning (daily-loss test). No ML/RL path | Initial missing test 5 is added. |
| 16 | API rate-limit compliance | WARN | Unchanged: no Upstox calls in backtests (`test_backtest_never_uses_the_upstox_adapters`) | Carried over: funds read every second in PAPER (`runtime/control.py:183`). |
| 17 | PAPER/LIVE isolation | OK | Unchanged since the initial audit | |
| 18 | Restart recovery | OK | Unchanged; all recovery tests pass | |
| 19 | Logging and audit trail | WARN | Every persisted timestamp is simulated time (`test_backtest_persists_only_simulated_time`; kill resets at exactly 09:00 simulated time in the daily-loss test). Tasks cancelled at the end of a run are reported as a warning in the result and report (`backtester.py:528-530`, `:538-549`) | Warnings 3-6. |

## Blocking issues

None.

## Warnings

1. **The quiet-loop check relies on CPython internals and on no real-time waits.** `settle_simulation` (`backtester.py:145-160`) reads the private `loop._ready` queue. It refuses to run if that queue doesn't exist, but it can't detect a task waiting on a real timer. The only such wait left near the execution path is the WebSocket disconnect grace timer (`risk/risk_manager.py:236`), which a backtest never starts. A future `asyncio.sleep(n)` or `wait_for` on a backtested path would make results silently non-deterministic.
2. **A failed square-off is carried overnight in PAPER and in backtests.** In the gap scenario the bot escalated correctly (CRITICAL alerts, kill, "MANUAL INTERVENTION REQUIRED"). The position kept its protection and closed the next morning (09:22). Upstox would auto-square an intraday (MIS) position, and the paper broker doesn't model that. The report marks the day as a BREACH, so a backtest never hides it.
3. **A stranded trade keeps its first exit reason.** The trade above is recorded as SQUARE_OFF, although it actually closed the next morning on a target trigger. Only the label is wrong, not the P&L.
4. **Work still parked at the end of a run is cancelled.** A trade whose exit was mid-retry at the final close stays OPEN in the results. The run reports the cancelled-task count as a warning and the open trade in `open_after_close`.
5. Carried over from the initial audit: backtest log lines keep wall-clock times (initial warning 3); an on-disk backtest database looks like a PAPER session (initial warning 4); `with_settings_overrides` can change the execution and market-session sections (initial warning 5).
6. Carried over from Phase 5: a reset waiting on a slow flatten holds up the control loop; funds are read every second; there is one shared dashboard account; a reset can re-activate strategies with open positions; `--allow-remote` relies on the operator for TLS; the instrument list is parsed on the event loop; there is no production PAPER runner.

## Required tests missing

1. A backtest with a global daily-loss breach: every strategy halts, positions flatten, and everything is cleared the next day.
2. A guard against real-time waits on backtested paths (an architecture test for `asyncio.wait_for` / non-zero `asyncio.sleep` in `execution/`, `risk/` and `runtime/`, or a `settle_simulation` test that fails on a stray real timer). This bounds warning 1.
3. A run that ends while an exit is parked mid-retry: the cancelled task and the open trade both appear in the report.
4. Carried over: the bot and the dashboard writing to SQLite at the same time under load.
