# Phase 4 Risk & Execution Audits

This file holds two audits of the same branch, in order:

1. **Initial audit: FAIL.** The blocking issue is listed below.
2. **Re-audit after the fixes.** Appended at the end of this file.

---

# Risk & Execution Audit Report (initial)

Date: 2026-11-04
Scope (files / PR): branch `phase-4-strategies`, both commits not yet on `main`: `c001657` (Phase 4 strategies, engine, scheduler and mode controller) and `dd93755` (NSE holidays, current-month NIFTY future, lot sizes from the Upstox instrument list, and the kill-switch clock fix). Files read: `core/engine.py`, `core/mode_controller.py`, `core/scheduler.py`, `core/interfaces.py`, `runtime/bot.py`, `runtime/gateway.py`, `runtime/contracts.py`, `strategies/*.py`, `data/instruments.py`, `broker/upstox_rest.py` (instrument download and order stubs), `risk/kill_switch.py`, and the parts of `risk/` and `execution/` that Phase 4 calls into.
Mode audited: PAPER (LIVE order routing does not exist)
Tests at audit time: 545 passed
Verdict: **FAIL**

## Results

| # | Area | Status | Evidence (file:line, test) | Notes |
|---|---|---|---|---|
| 1 | Entry logic | OK | `core/engine.py:170` (single entry lock), `runtime/gateway.py:31` (risk gate), `core/engine.py:173-178` (filter error refuses entry). Tests: `test_entries_are_serialised`, `test_filter_error_fails_closed`, `test_risk_rejection_flows_back` | Entries only go through the risk manager. If the filter, contract choice or submission raises, the entry is refused. |
| 2 | Exit logic | OK | `core/engine.py:20`, `runtime/contracts.py:52-59`, `runtime/bot.py:224-226`. Test: `test_orb_day_end_to_end_then_square_off` | Pause and stop only block new entries. Contracts with open positions are never unsubscribed, including during a futures roll. |
| 3 | Stop-loss logic | OK | `core/interfaces.py:43-57`. Test: `test_bot_integration.py:140` (entry produces a STOP_LOSS order) | The ML filter decision cannot touch stops. The Phase 3 stop logic is unchanged. |
| 4 | Take-profit logic | OK | Phase 3 code unchanged | |
| 5 | Trailing-stop logic | OK | Phase 3 code unchanged; `execution/position_manager.py:446-449` on restore | |
| 6 | Position sizing | OK | `runtime/bot.py:144-162`, `data/instruments.py:77-81`, `risk/risk_manager.py:183`, `risk/position_sizer.py:53`. Tests: `test_trade_quantity_follows_instrument_list_lot_size[65,40]`, `test_quantity_not_matching_listed_lot_is_rejected_by_paper_broker`, `test_no_hardcoded_lot_sizes_in_production_code` | Lot size comes only from the Upstox list. The ML size multiplier is clamped to 0–1. |
| 7 | Daily loss limits | OK | `risk/daily_loss_guard.py:218-240` (persisted and restored). Test: `test_mid_session_restart_keeps_todays_kill` | |
| 8 | Global loss limits | OK | `risk/risk_manager.py:140-142`. Test: `test_kill_switch_blocks_pipeline_entries` | |
| 9 | Margin checks | OK | `runtime/gateway.py:30-31`, `risk/risk_manager.py:190`, `core/engine.py:195` | Funds are fetched before every entry. If the fetch fails, the entry is refused. |
| 10 | Order lifecycle handling | OK | Phase 3 code unchanged | |
| 11 | Partial fills | OK | Phase 3 code unchanged | |
| 12 | Order rejection handling | OK | The paper broker rejects a quantity that isn't a whole number of lots, with no retry | |
| 13 | Reconciliation | WARN | `runtime/bot.py:210-227`, `runtime/bot.py:270` | After a restart, reconciliation runs right after the new-day reset. When the bot runs continuously, the 09:00 start-of-day job clears the previous day's blocks and doesn't reconcile; the next check can be up to 300 s later. |
| 14 | Square-off logic | WARN | `execution/square_off.py:113-128`. Test: `test_orb_day_end_to_end_then_square_off` | The square-off itself works. Its escalation for an incomplete square-off is undone the next morning (blocking issue 1). |
| 15 | Kill-switch behavior | **FAIL** | `risk/kill_switch.py:204-222`, `runtime/bot.py:238`, `runtime/bot.py:246-248`, `execution/order_manager.py:410-415`, `execution/square_off.py:126-128` | The kill switch is cleared automatically on a new day, even when it fired because a position couldn't be exited (blocking issue 1). The clock fix (`kill_switch.py:174-178`) is correct and tested. No ML, RL or strategy code holds a reference to the kill switch. |
| 16 | API rate-limit compliance | OK | `broker/upstox_rest.py:273`, `:283-288`. Test: `test_instrument_download_filters_and_never_sends_token` | The instrument download goes through the central limiter, and a 429 response is recorded with it. The 429 path has no test. |
| 17 | PAPER/LIVE isolation | OK | `runtime/bot.py:105-106`, `:118`; `broker/upstox_rest.py:376-383`. Tests: `test_bot_refuses_non_paper_mode`, `test_default_paper_and_live_always_refused_in_this_build` | There is no route to a real order. The public instrument file is fetched without the token (tested). |
| 18 | Restart recovery | WARN | `strategies/base_strategy.py:52`, `:103-105`; `data/storage/repositories.py:141` vs `runtime/bot.py:246` | Per-direction entry counts are kept in memory only. The strategy-status timestamp uses the real clock while the new-day reset compares it with the bot's clock. |
| 19 | Logging and audit trail | WARN | `core/engine.py:213-218`, `risk/kill_switch.py:220-221`, `core/scheduler.py:38-44` | Every entry decision is logged and published. Automatic clears are recorded with operator `system:new_day`. A failed 09:00 job only produces an exception log line, with no alert. |

## Blocking issues

1. **The kill switch and KILLED strategies are cleared automatically the next morning, even when the kill fired because a position could not be exited.**
   - An exit fails after all retries (`order_manager.py:410-415`, "MANUAL INTERVENTION REQUIRED"), or positions are still open after 15:20 (`square_off.py:126-128`). Either one fires the global kill switch.
   - At 09:00 the next day, `start_of_day` runs `new_day`. That calls `KillSwitch.roll_day`, which clears every previous-day kill whatever the reason. It also sets every KILLED or PAUSED strategy last changed before today back to ACTIVE.
   - The stuck position is still OPEN locally, and also at the broker if it really is stuck. So reconciliation finds no mismatch and nothing fires the kill switch again.
   - From 09:15 the bot takes new entries with an unresolved position still open. No named human ever acknowledged the alert.

   This breaks `docs/safety-policy.md` §5 and contradicts the alert's own "MANUAL INTERVENTION REQUIRED" text. It came in with `c001657`. **The merge into `main` must not proceed until this is fixed and re-audited.**

## Warnings

1. **A failed 09:00 start-of-day job leaves the bot running silently on stale data.** If the instrument download fails, the scheduler only logs the exception. `new_day` is skipped, there is no retry and no CRITICAL alert, and no reconciliation runs. After a weekly expiry, the option universe and futures contract point at expired contracts, so the bot stops trading without telling anyone.
2. **The instrument list is parsed on the event loop.** The multi-MB `NSE.json.gz` is decompressed and parsed in one blocking step (`upstox_rest.py:297`). The risk is low today, because it only happens at startup and at 09:00 before the open.
3. **ORB's one-entry-per-direction rule doesn't survive a restart.** The counters are in memory only. The risk layer's 4-trades-per-day cap is persisted and still holds.
4. **Strategy status timestamps use the real clock.** `strategies.updated_at` is stamped with the real time (`repositories.py:141`), but `new_day` compares it with the bot's clock. That is wrong under simulated time (the Phase 6 backtester).
5. **The risk manager's time check doesn't know about holidays.** `entries_allowed_at` checks only the clock time. Holidays are enforced only upstream, in the engine's market-hours check.
6. **Holidays are maintained by hand.** Only 2026 is loaded. From 1 Jan 2027 the bot logs CRITICAL but still treats every weekday as a trading day.

## Required tests missing

1. Day rollover after EXIT_FAILURE or SQUARE_OFF_INCOMPLETE with a position still open: the kill switch must stay active and the strategies KILLED until a named human resets them.
2. `start_of_day` when the instrument download fails: an alert is raised, the bot stays in a safe state, and the job retries.
3. `get_instruments` on HTTP 429, on exhausted timeouts, and on a non-200 response.
4. A mid-session restart after an ORB entry: no second entry in the same direction.
5. Strategy status timestamps under an injected clock.
6. Reconciliation runs after `start_of_day` clears the previous day's blocks.

---

# Risk & Execution Audit Report (re-audit after fixes)

Date: 2026-11-04
Scope (files / PR): branch `phase-4-strategies`, all Phase 4 code plus the fixes for the initial FAIL in `risk/kill_switch.py`, `runtime/bot.py`, `runtime/contracts.py`, `risk/risk_manager.py`, `data/storage/repositories.py`, `execution/stack.py`, `execution/square_off.py` and `strategies/base_strategy.py`. New tests are in `tests/unit/test_day_rollover_and_recovery.py`, `test_kill_switch.py`, `test_instruments_and_holidays.py` and `test_risk_manager.py`.
Mode audited: PAPER (LIVE order routing does not exist)
Tests at audit time: 576 passed. ruff and mypy (strict) are clean.
Verdict: **PASS WITH WARNINGS**

## Results

| # | Area | Status | Evidence (file:line, test) | Notes |
|---|---|---|---|---|
| 1 | Entry logic | OK | `core/engine.py:170`, `runtime/gateway.py:31`, `runtime/contracts.py:99` (stale-instrument block). Tests: `test_entries_are_serialised`, `test_start_of_day_download_failure_retries_alerts_and_blocks_entries`, `test_restart_after_orb_entry_keeps_per_direction_cap` | Per-direction caps now survive a restart (`base_strategy.py:107`, `bot.py:207`, `repositories.py:322`). |
| 2 | Exit logic | OK | Phase 3 exit paths unchanged. Test: `test_stuck_position_keeps_kill_and_strategies_killed_next_day` | A position that can't be exited stays tracked across the day and across a restart. |
| 3 | Stop-loss logic | OK | Unchanged; `core/interfaces.py:43-57` | |
| 4 | Take-profit logic | OK | Unchanged | |
| 5 | Trailing-stop logic | OK | Unchanged | |
| 6 | Position sizing | OK | Unchanged from the initial audit; lot sizes come from the instrument list, and a stale list now blocks entries (`bot.py:230-237`) | |
| 7 | Daily loss limits | OK | `kill_switch.py:68` (`DAY_SCOPED_REASONS`), `kill_switch.py:249-266`. Tests: `test_roll_day_clears_only_daily_loss_scopes`, `test_daily_loss_kill_still_clears_next_day` | Daily-loss kills still clear on the next trading day, as the build plan says ("disabled for the day"). |
| 8 | Global loss limits | OK | Same as 7. Test: `test_persistent_reason_escalates_an_active_daily_loss_kill` | If the global daily-loss flatten fails, EXIT_FAILURE escalates the kill and is no longer swallowed. |
| 9 | Margin checks | OK | Unchanged | |
| 10 | Order lifecycle handling | OK | Unchanged | |
| 11 | Partial fills | OK | Unchanged | |
| 12 | Order rejection handling | OK | Unchanged. Test: `test_stuck_position_...[exit_failure]` (rejected exits escalate to a persistent kill) | |
| 13 | Reconciliation | OK | `bot.py:268`. Tests: `test_start_of_day_reconciles_after_clearing_previous_day_blocks`, `test_start_of_day_reconciliation_catches_a_broker_only_position` | Reconciliation now runs right after the 09:00 roll-over. |
| 14 | Square-off logic | OK | `square_off.py:126-128`. Test: `test_stuck_position_...[square_off_incomplete]` | The SQUARE_OFF_INCOMPLETE kill now survives the next day and a restart. |
| 15 | Kill-switch behavior | OK | `kill_switch.py:130-165` (escalation), `:223-247` (restore across days), `:249-280` (roll-over plus "still active" alert), `bot.py:303-308` (KILLED stays while blocked). Tests: `test_roll_day_keeps_persistent_reasons_until_named_reset` (all 10 persistent reasons), `test_strategy_persistent_reason_kept_under_day_scoped_global`, `test_restore_unknown_reason_is_treated_as_persistent`, `test_stuck_position_keeps_kill_and_strategies_killed_next_day` | Every reason other than daily loss stays active until a named operator resets it. An unknown stored reason is treated as persistent. ML, RL and strategy code still hold no reference to the kill switch. |
| 16 | API rate-limit compliance | OK | `upstox_rest.py:273`, `:283-288`. Tests: `test_instrument_download_429_is_reported_to_limiter_then_retried`, `..._persistent_429_raises_rate_limit_error`, `..._exhausted_timeouts_fail_cleanly`, `..._non_200_is_not_retried` | |
| 17 | PAPER/LIVE isolation | OK | Unchanged: `bot.py:105-106`, `upstox_rest.py:376-383` | |
| 18 | Restart recovery | OK | `kill_switch.py:223-247`, `bot.py:207`, `bot.py:212-217`. Tests: restart legs of `test_stuck_position_...`, `test_restart_after_orb_entry_keeps_per_direction_cap` | |
| 19 | Logging and audit trail | OK | `bot.py:239-255` (CRITICAL alert through the fan-out sink `stack.py:53`), `kill_switch.py:157-158` (escalation recorded), `kill_switch.py:267-280` (daily "still active" alert), `repositories.py:138` (status timestamps on the caller's clock) | |

Holidays (initial warnings 5 and 6): `risk_manager.py:121-124` rejects entries on a configured holiday, and rejects all entries when the current year has no calendar. Tests: `test_holiday_blocks_entries_in_the_risk_gate`, `test_missing_holiday_calendar_for_the_year_blocks_entries`.

## Blocking issues

None.

## Warnings

1. **The instrument list is still parsed on the event loop** (`upstox_rest.py:297`). This is initial warning 2 and was not in scope for the fixes. It only runs at startup and at 09:00 (with retries until about 09:05), when nothing should be open.
2. **A named human reset clears the kill but not the KILLED statuses.** The strategies stay KILLED until the next trading day's reset. That is conservative, but the Phase 5 reset flow should re-activate strategies explicitly, and audit it.
3. **A PAPER restart with open positions now needs a human reset.** The paper broker's order book is still not persisted (Phase 3 warning). A mid-session PAPER restart with open positions trips a RECONCILIATION_MISMATCH kill, which is now persistent, so it needs a named human reset. Before, it cleared the next day.
4. **Other repository timestamps still use the real clock** (`repositories.py:208`, `:266`, `:272`, `:307`, `:342`). None of them drive a day-boundary decision today, but the Phase 6 backtester will need them on the injected clock.
5. **The stale-instrument block sits in the contract resolver (`runtime/`), not in `risk/`.** The engine is the only entry path today. Any future entry path, such as a dashboard manual entry, must go through the resolver, or the check should move into the risk gate.
6. **A strategy-scoped persistent reason is still dropped while a persistent global kill is active.** That behaviour predates these fixes. If a human then resets only the global kill, the strategy-level condition is gone, and only the next reconciliation (within 300 s) would detect a mismatch that still exists.
7. **Ad-hoc NSE holidays still have to be added by hand.** An example is the 15 Jan 2026 closure, announced at short notice. On an unlisted holiday no quotes arrive, so the freshness check blocks entries.

## Required tests missing

None blocking. Optional: a bot-level test where a real daily-loss flatten fails and escalates. Escalation is covered at the kill-switch level by `test_persistent_reason_escalates_an_active_daily_loss_kill`.
