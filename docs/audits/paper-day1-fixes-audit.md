# Risk & Execution Audit Report
Date: 2026-11-09 (re-audit; supersedes the first report on commit fce85dd)
Scope (files / PR): branch `paper-day1-fixes` against `main`, with close attention to the changes since fce85dd:
`risk/daily_loss_guard.py`, `strategies/base_strategy.py`, `strategies/vwap_ema.py`, `broker/paper_broker.py`,
`broker/upstox_rest.py`, `broker/broker_interface.py`, `core/models.py`, `execution/order_manager.py`,
`runtime/history.py`, `runtime/market_recorder.py`, `runtime/paper.py`, `data/storage/`, `config/`, and their tests.
Verdict: PASS WITH WARNINGS

Test evidence: 1360 passed, ruff clean, mypy clean (135 files). 14 deliberate breakages of the new behaviour (rollback,
each block path, the conservative local stop, seeding, retention) were each caught by a test. All tests use synthetic
data; nothing here has run against the live Upstox feed. Audited by the same session that wrote the change, so a
second reviewer is still worthwhile.

## Results
| # | Area | Status (OK / WARN / FAIL / N/A) | Evidence (file:line, test) | Notes |
|---|---|---|---|---|
| 1 | Entry logic | OK | `risk/risk_manager.py:157`; `risk/daily_loss_guard.py:187-188`; `runtime/gateway.py:45-54`; `test_a_blocked_strategy_is_refused_at_the_risk_gate_but_can_still_exit` | A history block is enforced at the risk gate, the only way an entry becomes an order. Replay and seeding cannot signal. |
| 2 | Exit logic | OK | `execution/position_manager.py:457-461`, `:484`; `test_blocking_after_entry_leaves_the_open_trade_protected`, `test_a_frozen_price_never_stops_the_stop_loss` | Blocks and gates never reach the exit path. |
| 3 | Stop-loss logic | OK | `broker/paper_broker.py:407-425`; `core/models.py:161`; `execution/order_manager.py:151`; `test_a_local_monitor_stop_fills_at_the_lower_of_bid_and_last`, `test_exits_that_are_not_stops_still_fill_at_the_quote` | Previous warning fixed: resting SL-M orders and stop exits from the local monitor now both fill at the worse of quote and last. The flag is set only for `STOP_LOSS` purpose orders, which are SL-M or stop-triggered market exits. Stops still cannot widen. |
| 4 | Take-profit logic | OK | `risk/stop_loss_manager.py:58-64`; `test_target_hit_cancels_protective_stop_then_exits` | Unchanged; target exits keep the quote-based fill. |
| 5 | Trailing-stop logic | OK | `execution/position_manager.py:88`, `:455-456`; `test_trailing_stop_tightens_moves_exchange_stop_and_exits` | Unchanged; trailing exits are `STOP_LOSS` purpose and get the conservative fill. |
| 6 | Position sizing | OK | `risk/risk_manager.py:193-201`; `risk/position_sizer.py:60-79`; `test_never_exceeds_budget_property` | Unchanged. |
| 7 | Daily loss limits | OK | `risk/daily_loss_guard.py:111-126`, `_evaluate`; `execution/position_manager.py:400-401`; `test_closing_a_trade_does_not_double_count_the_loss`, `test_stop_outs_inside_the_daily_limit_do_not_kill` | Close-order bug fixed. See Warning 1 for the test change. |
| 8 | Global loss limits | OK | `risk/daily_loss_guard.py` `_evaluate`; `test_global_limit_breach` | Reads the same corrected state. |
| 9 | Margin checks | OK | `risk/risk_manager.py:197`; `risk/position_sizer.py:61`; `test_margin_limited` | Unchanged. |
| 10 | Order lifecycle handling | OK | `execution/position_manager.py:284-286`; `test_unknown_order_is_resolved_not_resent` | Unchanged. |
| 11 | Partial fills | OK | `broker/paper_broker.py` `_try_fill`; `test_partial_exit_fills_complete_over_ticks`, `test_partial_fills_keep_positions_and_pnl_consistent` | The conservative reference price applies to each partial stop fill. |
| 12 | Order rejection handling | OK | `test_rejection_logged_alerted_and_not_retried`, `test_repeated_rejections_trigger_kill_switch` | Unchanged. |
| 13 | Reconciliation | OK | `test_clean_reconciliation`, `test_position_mismatch_triggers_kill_switch` | Unchanged; recording and retention are outside it. |
| 14 | Square-off logic | OK | `test_square_off_flattens_everything_with_reconciliation`, `test_square_off_retries_until_flat` | Unchanged. |
| 15 | Kill-switch behavior | OK | `risk/risk_manager.py:147`; `tests/unit/test_kill_switch.py`; `test_kill_switch_blocks_pipeline_entries` | No kill-switch code changed. History blocks are separate and cannot clear a kill. |
| 16 | API rate-limit compliance | OK | `broker/upstox_rest.py:240`, `:436`; `runtime/history.py:131`, `:285`; `test_historical_candles_use_the_v3_path_with_to_date_first` | The new historical call goes through the central limiter. Start-up adds at most 3 seed calls per strategy; retries are capped at 3. Endpoint checked against the official Upstox docs. |
| 17 | PAPER/LIVE isolation | OK | `runtime/history.py` (candle reads only); `core/models.py:161` (documented PAPER only); `test_launcher_can_only_build_a_paper_broker`, `test_an_analytics_token_may_read_historical_candles` | No new path to orders. |
| 18 | Restart recovery | OK | `runtime/paper.py:574-577` area (seed, then backfill, before the feed); `test_a_restart_with_an_open_trade_backfills_without_touching_caps_or_the_stop`, `test_a_mid_day_restart_seeds_then_replays_today`, `test_a_block_lasts_for_the_day_and_is_not_persisted` | The combined restart test is now in place. A block is deliberately not persisted, so a restart with complete data can trade; only the next-day reset or a restart clears it (`risk/daily_loss_guard.py:44`, `:146-147`). |
| 19 | Logging and audit trail | OK | `risk/daily_loss_guard.py:140-148` (ERROR, alerts); `runtime/history.py` warnings; `monitoring/json_logger.py:114-117` | Each block logs its reason and appears on the dashboard (`paused`). New lines pass through the secret masker. |

## Blocking issues
None.

## Resolved since the first report
1. Replay errors: replay is all or nothing (`strategies/base_strategy.py:134-141`), and a failure blocks that strategy
   (`runtime/history.py:153-158`). Other strategies are unaffected.
2. Local-monitor stop fills are now conservative.
3. A failed or incomplete backfill blocks that strategy's new entries for the day, with nothing partial replayed
   (`runtime/history.py:247-250`). The same applies to an unrepairable partial candle and a gap between candles.
4. `option_quotes` retention: 30 days by default, applied at start-up and daily at 15:45
   (`runtime/market_recorder.py:69`, `runtime/paper.py:521`).
5. New: the EMA is seeded from the previous trading day and is ready at 09:15; VWAP still resets daily
   (`runtime/history.py:193`, `strategies/vwap_ema.py:84`).
6. Both end-to-end tests the first report listed as missing now exist.

## Warnings
1. **The daily-loss kill carry-over test patches guard internals.** `test_daily_loss_kill_blocks_the_day_and_clears_next_morning`
   only passed originally because of the double-count bug. Sizing keeps one trade inside the limit, so the test forces a
   real breach by lowering `_limit` and raising `remaining_risk_budget`. The original scenario is kept, asserting no kill.
2. **Not yet verified against live Upstox.** The units of intraday candle volume (compared with live tick volume deltas)
   and how quickly a just-closed candle is published are assumptions. If a candle takes longer than the retries (about
   6 seconds) on the series a strategy runs on, that strategy is blocked for the day; this errs on the safe side, and a
   restart clears it. A unit mismatch would distort VWAP and the volume ratio on replayed candles. Check the
   "candle backfill done" log at the next mid-session start.
3. **Seeding changes when `vwap_ema` can trade.** It can now signal from 09:45 instead of about 10:55, on an EMA that
   starts from yesterday's prices. After a large overnight gap the EMA will lag price for a while, and the trend
   condition is easier to meet in the gap's direction. VWAP and the volume filter still apply. This has not been tested on
   real market data; watch the early-session trades in PAPER.
4. **A 401 from a candle API triggers the existing auth-failure kill** (`broker/upstox_rest.py:275`). Correct for a bad
   token, but it means start-up history calls can stop new entries.

Notes, no action needed for this change: if the previous-day seed is unavailable the EMA simply warms up from today's
candles and no signal can fire until it is ready, so that case warns rather than blocks. `candles` has no retention
(about 10,000 rows a day). The 2026-11-09 `daily_pnl` row keeps its old drawdown figure, and that day's journal row keeps
its late features (learning ignores them at read time). `peak_pnl` is not restored after a restart (existing behaviour;
reporting only). The dashboard must be restarted after the bot migrates the database to schema v6.

## Required tests missing
1. None that can run offline. The real Upstox candle APIs can only be exercised in a live PAPER session (Warning 2).
