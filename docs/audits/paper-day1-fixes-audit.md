# Risk & Execution Audit Report
Date: 2026-11-09
Scope (files / PR): branch `paper-day1-fixes` against `main` (24 files changed, 3 new): `risk/daily_loss_guard.py`,
`risk/risk_manager.py`, `execution/position_manager.py`, `broker/paper_broker.py`, `strategies/`, `core/engine.py`,
`data/candle_builder.py`, `data/market_data.py`, `data/storage/`, `runtime/` (`history.py`, `market_recorder.py`,
`gateway.py`, `learning.py`, `bot.py`, `paper.py`), `learning/`, and their tests.
Verdict: PASS WITH WARNINGS

Test evidence: 1317 passed, ruff clean, mypy clean (135 files). Each behavioural fix was reverted alone and the new tests
were confirmed to fail. The new tests use synthetic data; nothing here has run against the live Upstox feed.
Audited by the same session that wrote the change, so a second reviewer is still worthwhile.

## Results
| # | Area | Status (OK / WARN / FAIL / N/A) | Evidence (file:line, test) | Notes |
|---|---|---|---|---|
| 1 | Entry logic | OK | `runtime/gateway.py:45-54`; `core/engine.py:141-147`; `strategies/vwap_ema.py:124`; `strategies/orb_options.py:129`; `test_a_replayed_setup_must_break_and_form_again` | Feature capture sits between approval and order and cannot raise. Replay and paused observation cannot signal; their output is discarded (`strategies/base_strategy.py:117-127`). |
| 2 | Exit logic | OK | `execution/position_manager.py:457-461`, `:484`; `test_a_frozen_price_never_stops_the_stop_loss` | The new frozen-price gate is consulted only by contract resolution (`runtime/contracts.py:99`). |
| 3 | Stop-loss logic | WARN | `broker/paper_broker.py:407-425`; `risk/stop_loss_manager.py:67-71`; schema trigger `trg_trades_stop_tighten_only`; `test_sl_m_sell_fills_at_the_last_price_when_it_is_below_the_bid`, `test_local_stop_monitor_without_exchange_stop` | Stops are unchanged and still cannot widen. Conservative fills cover resting SL-M orders only; see Warning 2. |
| 4 | Take-profit logic | OK | `risk/stop_loss_manager.py:58-64`; `test_target_hit_cancels_protective_stop_then_exits` | Unchanged. |
| 5 | Trailing-stop logic | OK | `execution/position_manager.py:455-456`; `test_trailing_stop_tightens_moves_exchange_stop_and_exits`, `test_stop_can_only_tighten` | Unchanged; trailing exits use the SL-M path and get the conservative fill. |
| 6 | Position sizing | OK | `risk/risk_manager.py:193-201`; `risk/position_sizer.py:60-79`; `test_never_exceeds_budget_property` | Unchanged. |
| 7 | Daily loss limits | OK | `risk/daily_loss_guard.py:111-126`, `:140-150`; `execution/position_manager.py:400-401`; `test_closing_a_trade_does_not_double_count_the_loss`, `test_a_close_near_the_limit_does_not_falsely_breach`, `test_stop_outs_inside_the_daily_limit_do_not_kill` | Confirmed bug fixed: real losses of 788.77 and 625.83 were seen as 1,363.77 and 1,074.58 and falsely fired the kill switch. See Warning 3 for the test change. |
| 8 | Global loss limits | OK | `risk/daily_loss_guard.py:151-156`; `test_global_limit_breach` | Benefits from the same fix: the global total reads the same state. |
| 9 | Margin checks | OK | `risk/risk_manager.py:197`; `risk/position_sizer.py:61`; `test_margin_limited` | Unchanged. |
| 10 | Order lifecycle handling | OK | `execution/position_manager.py:284-286`; `test_unknown_order_is_resolved_not_resent` | Unchanged. |
| 11 | Partial fills | OK | `broker/paper_broker.py:438-444`; `test_partial_exit_fills_complete_over_ticks`, `test_partial_fills_keep_positions_and_pnl_consistent` | The conservative reference price applies to each partial fill of a triggered SL-M. |
| 12 | Order rejection handling | OK | `test_rejection_logged_alerted_and_not_retried`, `test_repeated_rejections_trigger_kill_switch` | Unchanged. |
| 13 | Reconciliation | OK | `test_clean_reconciliation`, `test_position_mismatch_triggers_kill_switch` | Unchanged; market recording is outside reconciliation. |
| 14 | Square-off logic | OK | `test_square_off_flattens_everything_with_reconciliation`, `test_square_off_retries_until_flat` | Unchanged. |
| 15 | Kill-switch behavior | OK | `risk/risk_manager.py:147`; `tests/unit/test_kill_switch.py`; `test_kill_switch_blocks_pipeline_entries` | No kill-switch code changed. Removes a false trigger (area 7). |
| 16 | API rate-limit compliance | OK | `broker/upstox_rest.py:222-240`; `runtime/history.py:37`, `:133` | Backfill and repair go through the central limiter. Repair is capped at 3 attempts per partial candle. |
| 17 | PAPER/LIVE isolation | OK | `runtime/history.py` (candle reads only); `runtime/market_recorder.py` (writes two review tables); `test_launcher_can_only_build_a_paper_broker` | No new path to orders. Trading never reads the new tables. |
| 18 | Restart recovery | WARN | `runtime/bot.py` `prepare` (`_restore_entry_counts` before backfill); `runtime/paper.py:574`; `test_backfill_gives_strategies_the_same_state_as_a_full_session`, `test_restart_after_orb_entry_keeps_per_direction_cap` | The mid-session restart was the reason for this work. Sound by construction, with Warnings 1 and 4 and a missing combined test. |
| 19 | Logging and audit trail | OK | `broker/paper_broker.py:407-410`; `risk/risk_manager.py:202-205`; `strategies/base_strategy.py` `_log_decision`; `monitoring/json_logger.py:114-117` | New log lines pass through the existing secret masker. |

## Blocking issues
None.

## Warnings
1. **`runtime/history.py:108` and `:142`: replay errors are not caught, contrary to the module docstring.** Fetch errors are
   caught, but an exception while applying candles would stop start-up (fail-closed, no trading) or, during a repair, die
   in a background task with the indicators part-updated. Low likelihood. Wrap both calls.
2. **Conservative fills do not cover a stop hit by the local monitor.** When no SL-M is resting
   (`execution/position_manager.py:457-461`), the exit is a MARKET order filled at the bid, so the optimistic fill seen on
   2026-11-09 is still possible on that path. Rare in normal running.
3. **The daily-loss kill carry-over test now patches guard internals.** `test_daily_loss_kill_blocks_the_day_and_clears_next_morning`
   only passed before because of the double-count bug. Sizing keeps a single trade inside the limit, so the test now
   lowers `_limit` and raises `remaining_risk_budget` to force a real breach. The purpose is kept, and the original
   scenario is retained asserting no kill, but this deserves a reviewer's eye.
4. **A failed backfill leaves VWAP as "since start-up".** It logs a WARNING and the strategy can still trade once 20 live
   candles have passed, which is the 2026-11-09 condition. ORB fails safe (no opening range, no trades). A partial candle
   that cannot be repaired is skipped the same way. Consider blocking `vwap_ema` entries when session history is incomplete.
5. **Not yet verified against live Upstox.** The units of intraday candle volume (compared with live tick volume deltas)
   and how soon a just-closed candle is final are assumptions. A unit mismatch would distort VWAP and the volume ratio on
   replayed candles. Check the "candle backfill done" log and compare recorded candles at the next mid-session start.
6. **`option_quotes` has no retention** (about 60,000 rows a day at the default sampling). A 401 from the candle API also
   triggers the existing auth-failure kill (`broker/upstox_rest.py:205`, `:275`); this is correct for a bad token, but
   it means the backfill can stop new entries.

Notes, no action needed for this change: the 2026-11-09 `daily_pnl` row keeps the old 2,635.09 drawdown and that day's
journal row keeps its late features (learning now ignores them at read time). `peak_pnl` is not restored after a restart
(existing behaviour; reporting only). The dashboard must be restarted after the bot migrates the database to schema v6.

## Required tests missing
1. A restart with today's entries and open trades followed by a backfill, asserting per-direction caps, cooldown and
   protective stops are intact (each part is tested alone, not combined).
2. A full session where a real first partial candle is built by the candle builder and repaired end to end (today the
   builder and the loader are tested separately).
3. Replay or repair raising an exception (after Warning 1 is fixed).
