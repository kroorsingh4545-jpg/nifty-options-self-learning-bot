# Risk & Execution Audit Report
Date: 2026-11-28
Scope (files / PR): branch `fix-frozen-price-after-cutoff` vs `main` (95b667e): `runtime/bot.py`
(`frozen_price_block_reason`, lines 404-434), new `tests/unit/test_frozen_price_after_cutoff.py` (14 tests). No file in
`risk/`, `execution/`, `broker/`, `strategies/`, `config/` or existing tests changed.
Verdict: **PASS WITH WARNINGS**

Tests: full suite 2,238 passed (1 pre-existing warning); ruff clean; mypy clean on `runtime/bot.py`. The new
after-cut-off tests fail on the old code (7 failed / 7 passed with the fix stashed), so they pin the change.

## The change
Before: the frozen-price gate ran for the whole market session (09:15-15:30), so a quiet NIFTY index near the
close (after the 14:45 cut-off, and during 15:10-15:30) raised a "New entries blocked: price unchanged for over
60s" block that the dashboard shows as a red banner (`session.entry_block` = `bot.entry_block_reason()`,
`runtime/control.py:335`), although no entry was possible any more.

After: the gate is checked only while new entries are still allowed today: market hours **and** before
`risk.time_rules.no_new_entries_after` (`runtime/bot.py:416`). Before the cut-off nothing changed. An episode still
open at the cut-off ends there with one INFO line ("frozen-price check ended: new entries are closed for today",
`:427-428`), not the misleading "new entries allowed again" WARNING. The next trading morning the gate is active
again.

## Results
| # | Area | Status | Evidence (file:line, test) | Notes |
|---|---|---|---|---|
| 1 | Entry logic | OK | `bot.py:416` gate unchanged before 14:45; after 14:45 entries are refused by `risk_manager.py:172-173` (`no new entries after 14:45`), and `settings.py:810` forbids an entry window ending after the cut-off | `test_before_the_cutoff_a_frozen_price_still_blocks_new_entries` (09:30, 12:00, 14:43), `test_one_second_before_the_cutoff_still_blocks` (14:44:59) |
| 2 | Exit logic | OK | exits never consult `entry_block_reason` (only `OptionContractResolver`, `bot.py:238`) | `test_stop_loss_runs_during_a_frozen_price_before_the_cutoff` / `_after_the_cutoff` |
| 3 | Stop-loss logic | OK | unchanged | stop-loss closes the trade with a frozen index, before and after 14:45 |
| 4 | Take-profit logic | N/A | unchanged | - |
| 5 | Trailing-stop logic | N/A | unchanged | - |
| 6 | Position sizing | N/A | unchanged | - |
| 7 | Daily loss limits | N/A | unchanged | - |
| 8 | Global loss limits | N/A | unchanged | - |
| 9 | Margin checks | N/A | unchanged | - |
| 10 | Order lifecycle | N/A | unchanged | - |
| 11 | Partial fills | N/A | unchanged | - |
| 12 | Order rejection | N/A | unchanged | - |
| 13 | Reconciliation | N/A | unchanged | - |
| 14 | Square-off | OK | `square_off.py` never reads the gate | `test_square_off_runs_during_a_frozen_price_after_the_cutoff` (frozen 15:08-15:10, SQUARE_OFF, flat) |
| 15 | Kill switch | N/A | unchanged | - |
| 16 | API rate limits | N/A | no API calls added | - |
| 17 | PAPER/LIVE isolation | N/A | unchanged | - |
| 18 | Restart recovery | OK | `_frozen_blocked` is in-memory; a restart after 14:45 starts unblocked, which is correct | - |
| 19 | Logging / audit trail | OK | block WARNING logged once per episode before the cut-off (unchanged); one INFO when an episode ends at the cut-off; no WARNING after it | `test_after_the_cutoff_a_frozen_price_raises_no_block_or_banner` (14:45, 14:50, 15:10, 15:25), `test_an_episode_open_at_the_cutoff_ends_there_without_an_allowed_again_warning` |

## Blocking issues
None.

## Warnings
1. The other entry gates (feed lag, instrument list) still publish their block reason after the cut-off, so a
   *feed-lag* banner could still appear near the close. That was not reported as noise and is unchanged.
2. The cut-off used is the global `no_new_entries_after`; a strategy whose own entry window ends earlier is still
   checked until 14:45 (harmless: it only blocks entries that the window already blocks).

## Required tests missing
None for this change.
