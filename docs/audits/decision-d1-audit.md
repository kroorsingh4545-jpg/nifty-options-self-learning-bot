# Risk & Execution Audit Report

Date: 2026-11-15
Scope (files / PR): branch `decision-d1-data-recorder` vs `main` 4b18be6 (decision-engine D1: data recorder).
- **Bot recording path:** `broker/upstox_mapping.py`, `core/models.py`, `config/settings.py`, `config/settings.yaml`,
  `data/storage/{schema.sql,database.py,repositories.py}`, `runtime/{market_recorder.py,bot.py,paper.py,control.py}`.
- **Research:** `research/{__main__.py,config.py,research.yaml,trading_readonly.py}`,
  `research/collect/{archive.py,chain.py}`, `research/audit/`, `research/store/*`, `research/reports/` (now tracked).
- **Scripts / docs:** `scripts/{daily-research.bat,chain-schedule.bat}`, `README.md`, `docs/research.md`,
  `docs/decision-engine-plan.md`, `.gitignore`.
- **Tests:** new `tests/unit/test_decision_d1_recording.py` (39) and `tests/unit/test_research_d1.py` (13);
  changed `test_database.py`, `test_paper_day1_fixes.py` (pinned schema version 6 -> 7) and
  `test_research_isolation.py` (in-session allow-list + a new cap test).
- **Untouched:** no file in `risk/`, `execution/`, `strategies/` or `dashboard/` changed.

Checks: **1707 passed, 0 failed** (1 existing NumPy deprecation warning); `ruff check .` and `mypy .` (strict,
163 files) clean.

Verdict: **PASS WITH WARNINGS**

## Results
| # | Area | Status (OK / WARN / FAIL / N/A) | Evidence (file:line, test) | Notes |
|---|---|---|---|---|
| 1 | Entry logic | OK | `runtime/bot.py:229` (resolver gets `self.universe` only), `:331` separate `recording_universe`; `test_a_next_expiry_contract_is_never_selected_for_a_trade` | Next-expiry contracts are never in the traded universe; ATM / ITM / OTM selections stay on the nearest expiry. No dashboard entry command exists (`core/enums.py:209`). Band 3 -> 5 only widens subscriptions; both strategies stay ATM offset 0. |
| 2 | Exit logic | OK | `runtime/bot.py:529` (`remove -= self.subscribed_keys() \| held`); `test_a_key_both_universes_want_is_never_unsubscribed`, `test_an_expiry_roll_moves_contracts_between_universes_without_dropping_any` | A held contract, the index, the future and VIX are never unsubscribed, including across an expiry roll. |
| 3 | Stop-loss logic | OK | No change in `risk/` / `execution/`; `test_trading_is_unaffected_when_every_recording_write_fails` | A failing recorder cannot reach the stop path. |
| 4 | Take-profit logic | N/A | unchanged | |
| 5 | Trailing-stop logic | N/A | unchanged | |
| 6 | Position sizing | N/A | unchanged | |
| 7 | Daily loss limits | OK | `risk/daily_loss_guard.py` unchanged; full suite | |
| 8 | Global loss limits | OK | unchanged; full suite | |
| 9 | Margin checks | N/A | unchanged | |
| 10 | Order lifecycle handling | N/A | `execution/` unchanged | |
| 11 | Partial fills | N/A | unchanged | |
| 12 | Order rejection handling | N/A | unchanged | |
| 13 | Reconciliation | N/A | unchanged; recording-only contracts are never ordered | |
| 14 | Square-off logic | OK | `execution/square_off.py` unchanged; `test_square_off_closes_everything_while_entries_are_lag_blocked` | The 15:45 purge runs after square-off (Warning 2). |
| 15 | Kill-switch behavior | OK | `risk/kill_switch.py` unchanged; entry gates read only `base_keys()` (`runtime/bot.py:404-430`, `:412`); `test_a_frozen_vix_never_blocks_entries`, `test_vix_is_subscribed_but_never_a_base_key_of_the_entry_gates` | VIX and next-expiry keys can neither block nor unblock anything. |
| 16 | API rate-limit compliance | OK | Bot: no new REST calls (WebSocket subscriptions only; `recover_snapshot` has no bot caller). Research: `research/__main__.py:71`; `test_the_in_session_chain_schedule_is_small`, `test_bot_plus_research_stay_inside_the_official_limits` | 39 subscriptions vs 1,500 combined / 2,000 full-mode (`broker/upstox_websocket.py:47-48`, `check_limits`). `chain-schedule` runs on research's own 10 % limiter, at most 40 calls a day. |
| 17 | PAPER/LIVE isolation | OK | `research/sources.py` `ReadOnlySource`; isolation tests scan the new research files; trading DB `?mode=ro` (`research/trading_readonly.py`); `test_raw_quotes_read_v7_rows_read_only` | No order path added. `signals.mode` from `bot.cfg.mode` (`runtime/paper.py:520`) with a CHECK (`data/storage/schema.sql:565`). |
| 18 | Restart recovery | WARN | `data/storage/database.py` `ADDED_COLUMNS`; `test_a_v6_database_migrates_and_keeps_its_quotes`; dry run on a scratch copy of the real `trading.db` (v6 -> v7 in 0.02-0.04 s, 117,062 rows kept, idempotent) | Warnings 3 and 6. |
| 19 | Logging and audit trail | OK | `signals` append-only (`data/storage/schema.sql:793-803`; `test_recorded_signals_are_append_only`); `runtime/market_recorder.py:136-150` (rate-limited failure / drop logs); `test_a_signal_is_recorded_with_its_entry_decision` | Every signal and its decision is now in the database. No secrets touched. |

**Owner conditions for D1:**
- **Never raise:** every listener and write is guarded (`runtime/market_recorder.py:72,79,85,117,133,180,193,208`).
  Tests: `test_a_failing_callback_never_raises_from_on_tick` (3 cases), `test_garbage_bus_payloads_never_raise`,
  `test_one_failing_table_never_raises_and_the_others_are_still_written` (4 cases),
  `test_close_never_raises_when_the_database_is_gone`, and `test_trading_is_unaffected_when_every_recording_write_fails`
  (a full PAPER session with every recording write failing still opens the ORB trade).
- **Never block:** listeners never touch the database (`test_listeners_never_touch_the_database`); at most 5,000 rows
  per flush (`market_recorder.py:163-177`; `test_a_backlog_is_written_over_several_bounded_flushes`); capped buffers
  (`test_full_buffers_drop_rows_instead_of_growing`); signals first (`test_signals_are_written_before_a_quote_backlog`);
  bounded shutdown drain (`test_close_drains_a_full_backlog_in_bounded_time`) and bounded daily purge
  (`test_the_daily_retention_purge_at_production_volume_is_bounded`).

## Blocking issues
None.

## Warnings
1. **Not yet verified against live Upstox:** Greeks non-zero in `full` mode, `tbq`/`tsq` populated for options and
   the future, VIX streaming in `full` mode as an index feed. Check with `python -m research data-quality` after the
   first PAPER session (missing Greeks or `tbq`/`tsq` are reported as problems).
2. **Writes still run on the event loop** on one shared connection with `busy_timeout = 5000` (unchanged design).
   Normal flush: about 45 rows every 5 s, capped at 5,000. The daily purge of one day (about 170,000 rows) blocks
   for about 1 s (bounded by test), at start-up before the feed and at 15:45 after square-off. Shutdown
   `MarketRecorder.close()` drains the whole buffer (a full 100,000-row backlog in about 3 s, bounded by test) with
   no timeout of its own; the bot is already stopped then.
3. **Three existing tests changed** (none weakened): the in-session allow-list in `test_research_isolation.py` now
   includes `chain-schedule` (the explicit "never history / smoke" assertion stays, plus a new cap test); the pinned
   schema version in `test_database.py:387` and `test_paper_day1_fixes.py:996` moved 6 -> 7 (that test's name still
   says v6).
4. **Storage growth:** `trading.db` about 52 MB a day, about 1.2 GB at the 30-day retention; `candles` (no retention)
   now covers 39 instruments, about 23,000 rows / 3.6 MB a day (about 0.9 GB a year); research `quote_archive` is
   permanent, about 18 MB a day (104 B a row measured), about 4.5 GB a year. 77 GB free on 2026-11-15. Revisit candle
   retention in a later phase.
5. **A hard crash loses up to about 5 s of recording** (signals included) still in memory; a clean shutdown writes
   everything, undecided signals too.
6. **Restart the dashboard after the bot migrates the database to v7** (`Database.open_existing` requires the exact
   version; existing behaviour, as for v6). Research reads v6 and v7 alike
   (`test_a_v6_trading_database_reads_with_nulls_for_the_new_columns`).
7. Minor: three edited files are LF in the working tree (git normalises them); `signals.trade_uid` is deliberately
   not a foreign key, so a trade write can never block recording.

## Required tests missing
1. A live check on the first PAPER session (manual by design): `data-quality` shows Greeks and `tbq`/`tsq` present,
   and index, future and VIX quotes recorded.

## Status update 2026-11-15 (owner follow-up, same branch)
- Required test 2 (shutdown bound) **ADDED**: `test_close_drains_a_full_backlog_in_bounded_time` (100,000-row backlog,
  every row written in bounded batches, under 30 s; about 3 s measured).
- Required test 3 (expiry roll) **ADDED**: `test_an_expiry_roll_moves_contracts_between_universes_without_dropping_any`
  (`start_of_day` after the nearest expiry: the former next expiry becomes the traded one without leaving the feed;
  every expired contract leaves; the new next expiry joins).
- Required test 4 (purge at production volume) **ADDED**: `test_the_daily_retention_purge_at_production_volume_is_bounded`
  (one day of about 170,000 option + market rows deleted under 10 s; newer rows untouched).
- Minor warning resolved: the `strike_band` code default is now 5 (`config/settings.py`), matching the YAML.
- Full suite after the follow-up: **1707 passed**; ruff and mypy clean.
