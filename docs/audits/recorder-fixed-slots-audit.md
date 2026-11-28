# Risk & Execution Audit Report
Date: 2026-11-28
Scope (files / PR): branch `recorder-fixed-slots` vs `main` (ba20030). `runtime/market_recorder.py` (`_due`: fixed
clock slots), `research/audit/data_quality.py` (crossed futures quotes become a warning; `warnings` in the report
and its text), `config/settings.yaml` (comment only), `docs/research.md` (*Data quality*), README (stop after
15:31), new `tests/unit/test_recorder_fixed_slots.py` (16 tests). No change to `risk/`, `execution/`, `broker/`,
`strategies/`, `config/settings.py` or existing tests. Owner approval: item B, 2026-11-28.
Verdict: **PASS WITH WARNINGS**

Tests: full suite 2,318 passed (1 pre-existing warning); ruff clean; mypy clean on `runtime/` and
`research/audit/`. With the old recorder and audit, 10 of the 16 new tests fail. The 6 that pass check
behaviour that must stay the same: silences are not filled, snapshots are not recorded, the slots are aligned,
crossed index and option quotes still fail, and a 15:20 stop still fails. The existing recorder tests pass
unchanged.

## The change
* **Recorder** (`market_recorder.py:100-110`): a quote is saved for the first tick of each fixed clock slot.
  Slots are `quote_interval_seconds` (5 s) long, aligned to the epoch (:00, :05, ...), and
  `held_quote_interval_seconds` (1 s) for a held contract. These are the same slots the data-quality audit
  counts (09:15:00 IST is a multiple of 5 s; `test_slots_are_clock_aligned_like_the_audit`). The old rule saved
  a tick once 5 s had passed since the last saved one. That drifted to about one sample every 5-6 s, so 26 Nov
  (no outage) reached only 89-98 % of slots and the 99 % check could not be met. A slot with no tick stays empty:
  nothing is invented.
* **Audit** (`data_quality.py:171-177`): crossed quotes (bid > ask) on the NIFTY future are listed as
  `~ warning (does not fail the day)` (the future showed 20-82 a day on 25-27 Nov). Crossed quotes on options and
  the index still fail the day. Reports carry `warnings` (`:203`); older stored reports without it still render.
* **15:31 stop**: no code change. The README and `docs/research.md` say to stop the bot after 15:31. The
  existing 2-minute close-edge check then passes (`test_stopping_after_1531_passes_the_close_edge_and_1520_fails_it`).

## Results
| # | Area | Status | Evidence (file:line, test) | Notes |
|---|---|---|---|---|
| 1-14 | Entry / exit / stops / sizing / limits / orders / reconciliation / square-off | N/A | the recorder is write-only and never read by trading (module docstring); no trading code changed | `test_trading_is_unaffected_when_every_recording_write_fails` (existing) still passes |
| 15 | Kill switch | N/A | unchanged | - |
| 16 | API rate limits | N/A | no API calls | - |
| 17 | PAPER/LIVE isolation | N/A | unchanged; the research audit stays read-only (isolation tests pass) | - |
| 18 | Restart recovery | OK | the slot memory is in-memory; after a restart the first tick of the current slot is saved again: at most one extra row per instrument per restart, flagged as a duplicate (reported only) | - |
| 19 | Logging / audit trail | OK | recording volume unchanged (one row per slot with a tick, about the same as before); the report now shows warnings separately | `test_one_row_per_fixed_five_second_slot`, `test_held_contracts_use_one_second_slots_and_others_five`, `test_crossed_futures_quotes_are_a_warning_not_a_failure` |

Fairness check: with jittered ~1-s ticks (`test_a_feed_ticking_about_every_second_now_fills_every_slot`, 3 seeds),
every slot that has a tick gets exactly one row. The old rule left more than 1 % of those slots empty. A whole
uninterrupted synthetic session stopped at 15:31 is CLEAN, and the same session sampled the old way is not
(`test_an_uninterrupted_session_recorded_with_fixed_slots_is_clean`,
`test_the_same_session_with_the_old_sampling_was_never_clean`).

## Blocking issues
None.

## Warnings
1. **Not yet seen on the real feed.** Whether VIX, the future and the 08-Dec strikes really tick in at least 99 %
   of 5-s slots is unknown until the first session after merging. Illiquid next-expiry strikes may still keep the
   option window under 99 %. That would then be a real property of the feed, not a sampling artefact.
2. Sessions recorded before this change keep their shortfall, so `consecutive_clean` effectively restarts with
   the first session recorded after the merge.
3. The research shadow / quote-bar code reads `option_quotes` rows. Their spacing is now regular (5 s on the
   clock) instead of 5-6 s. The sampling-band logic treats rows as samples and needs no change, but shadow bands
   computed before and after the merge come from slightly different sampling.
4. A held/not-held switch starts a new slot series, so it can add one extra row in that slot (harmless).

## Required tests missing
None for this change.
