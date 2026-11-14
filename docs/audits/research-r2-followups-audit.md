# Risk & Execution Audit Report

Date: 2026-11-14
Scope (files / PR): branch `research-r2-followups` vs `main` b62d938 (commits 158e816, 54cd3d4).
- **Changed:** `research/__main__.py`, `research/collect/history.py`, `research/config.py`,
  `research/forecast/build.py`, `research/forecast/run.py`, `research/research.yaml`.
- **New:** `scripts/daily-research.bat`, `tests/unit/test_research_followups.py`.
- **Docs:** `README.md`, `docs/research.md`, `docs/research-r2-findings.md`, `docs/audits/research-r1-audit.md`
  and `research-r2-audit.md` (dated status updates appended).
- **Untouched:** no file in `risk/`, `execution/`, `broker/`, `strategies/`, `config/`, `runtime/` or
  `dashboard/` changed; no existing test was changed.

Verdict: **PASS WITH WARNINGS**

Test evidence:
- Full suite **1639 passed** (11 min 18 s, `.venv`), including the 17 new follow-up tests.
- All 230 existing research tests pass unchanged.
- `ruff check .` is clean; `mypy .` (strict, 160 files) is clean.
- The batch file's flow was dry-run on Windows with a stand-in executable (no Upstox calls): all 5 steps ran in
  order, failures were caught and listed, and a missing Python stops it with a message.

## Results
| # | Area | Status | Evidence (file:line, test) | Notes |
|---|---|---|---|---|
| 1 | Entry logic | N/A | `test_research_isolation.py::test_the_trading_bot_never_depends_on_research` (unchanged, passing) | No trading code changed. |
| 2 | Exit logic | N/A | as 1 | |
| 3 | Stop-loss logic | N/A | as 1 | |
| 4 | Take-profit logic | N/A | as 1 | |
| 5 | Trailing-stop logic | N/A | as 1 | |
| 6 | Position sizing | N/A | | |
| 7 | Daily loss limits | N/A | `config/` untouched | |
| 8 | Global loss limits | N/A | as 7 | |
| 9 | Margin checks | N/A | | |
| 10 | Order lifecycle | N/A | `test_research_followups.py::test_daily_research_script_runs_only_research_commands` | Script contains no `-m runtime`, `place_order` or `trading_mode`. |
| 11 | Partial fills | N/A | | |
| 12 | Order rejection handling | N/A | | |
| 13 | Reconciliation | N/A | | |
| 14 | Square-off logic | N/A | | |
| 15 | Kill switch | N/A | | The script never starts, stops or talks to the bot. |
| 16 | API rate-limit compliance | OK | `scripts/daily-research.bat:34,54` (`history`, `chain-snapshot`, no `--during-session`, tested); `network_guard` unchanged; exit code 3 stops the script (`:36-44`) | R1 budget (≤ 10 %, combined ≤ 90 % of official) and the 08:30–15:40 weekday block still apply. About 10 calls per routine run. |
| 17 | PAPER/LIVE isolation | OK | script content test; non-PAPER still refused | |
| 18 | Restart recovery | OK | `research/forecast/build.py:51` `live_deadline`, `:218`; `test_live_label_needs_the_safety_margin`; `research/forecast/run.py:56` evening guard; `test_evening_run_without_todays_candle_makes_no_next_forecast`; `test_a_run_during_the_session_still_writes_a_non_live_row` | R2 warning 1 fixed. Live rows stay immutable. Reruns are idempotent; a corrected candle is fetched again. |
| 19 | Logging / audit trail | OK | `research/collect/history.py:119-143` `_usable`; `test_todays_daily_candle_is_final_only_after_1540`, `test_an_old_incomplete_candle_is_never_accepted`; report in `logs\research\` (gitignored, `.gitignore:16`) | No token in the report. Failed steps are named at the end of the run. |

Other checks:
- **Expiry confirmation is recorded.** `research/research.yaml` cites NSE circular 103/2025
  (`test_expiry_rules_cite_the_confirming_circular`). The rule values are unchanged.
- **The batch file has CRLF line endings** in the working tree (`git ls-files --eol`: `w/crlf`, `attr eol=crlf`;
  `test_daily_research_script_keeps_crlf_line_endings`).

## Blocking issues
None.

## Warnings
1. **Unverified: whether Upstox's historical-candle API returns today's daily candle after 15:40.** The docs
   point to the intraday API for current-day data. If it isn't returned, the evening run saves the snapshot and
   outcomes but makes no next-session forecast, and says so (`run.py:56`). A run before 08:30 the next morning
   (or by 09:10) records it live. Verify on the first weekday evening run. A read-only current-day source would
   mean extending `ReadOnlySource`, which needs its own approval.
2. **The margin bounds the clock error but doesn't measure it.** More than 5 minutes of drift would still
   mislabel a forecast. The known drift is about 7 s.
3. **An evening live forecast uses a daily candle accepted as final at 15:40.** A later Upstox correction does
   not change the live row (intended); reconstructed rows update.

## Required tests missing
1. A live check of warning 1 on the first weekday evening run (manual by design).
