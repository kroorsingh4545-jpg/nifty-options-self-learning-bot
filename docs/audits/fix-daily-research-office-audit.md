# Risk & Execution Audit Report
Date: 2026-11-28
Scope (files / PR): branch `fix-daily-research-office` vs `main` (95b667e): `scripts/daily-research.bat` (Python
selection only; the nine research steps are unchanged), new `scripts/daily-research-office.bat` and
`scripts/daily-research-home.bat`, README (*Daily research update*), tests appended to
`tests/unit/test_research_followups.py`. No file in `risk/`, `execution/`, `broker/`, `strategies/`, `config/`,
`runtime/`, `research/` Python code or existing tests changed.
Verdict: **PASS WITH WARNINGS**

Tests: full suite 2,228 passed (1 pre-existing warning); ruff clean on the changed test file. Manual dry run of the
selection block in a scratch copy (home PC): working `.venv` -> chosen (auto and via home wrapper); `.venv` copied
with a broken `pyvenv.cfg` -> auto stops naming both paths, home wrapper stops with "does not start", exit 1.

## Results
| # | Area | Status | Evidence | Notes |
|---|---|---|---|---|
| 1 | Entry logic | N/A | no trading code changed | - |
| 2 | Exit logic | N/A | no trading code changed | - |
| 3 | Stop-loss logic | N/A | no trading code changed | - |
| 4 | Take-profit logic | N/A | no trading code changed | - |
| 5 | Trailing-stop logic | N/A | no trading code changed | - |
| 6 | Position sizing | N/A | no trading code changed | - |
| 7 | Daily loss limits | N/A | no trading code changed | - |
| 8 | Global loss limits | N/A | no trading code changed | - |
| 9 | Margin checks | N/A | no trading code changed | - |
| 10 | Order lifecycle | N/A | no trading code changed | - |
| 11 | Partial fills | N/A | no trading code changed | - |
| 12 | Order rejection | N/A | no trading code changed | - |
| 13 | Reconciliation | N/A | no trading code changed | - |
| 14 | Square-off | N/A | no trading code changed | - |
| 15 | Kill switch | N/A | no trading code changed | the scripts never talk to the bot |
| 16 | API rate limits | OK | `daily-research.bat:68-130` same `-m research` steps as before; research keeps its own 0.1 rate share and the 08:30-15:40 weekday block (`exit /b 3`, line 77) | `test_daily_research_script_runs_only_research_commands` (unchanged, still passes) |
| 17 | PAPER/LIVE isolation | OK | wrappers only set `PY` / `NIFTY_PC` and `call daily-research.bat` (`daily-research-office.bat:5-8`, `daily-research-home.bat:5-8`) | `test_daily_research_wrappers_use_the_start_script_python` forbids `-m runtime`, `-m research`, `place_order`, `trading_mode`, `.env`, `del`, `rmdir` in the wrappers; existing test forbids them in the main script |
| 18 | Restart recovery | N/A | research steps are resumable as before | - |
| 19 | Logging / audit trail | OK | prints the project folder and the chosen Python + PC (`daily-research.bat:43-44`); never reads `.env`, prints no token | - |

Python selection (the change itself): `daily-research.bat:27-32` and `:try_python` (`:156-161`). A wrapper's choice
wins (`NIFTY_PC` set); otherwise any inherited `PY` is cleared and the office Python is tried first (its path only
exists on the office PC), then the project `.venv`. Each candidate must start and `import research` before it is
used, and the chosen Python is test-started again (`:53`) before the first step, so a `.venv` copied from the other
PC can no longer be picked. Pinned by `test_start_scripts_and_daily_research_agree_on_both_pythons` and
`test_daily_research_double_click_auto_detects_and_test_starts_python`.

## Blocking issues
None.

## Warnings
1. The office path itself was not exercised: this audit ran on the home PC only (static tests + scratch dry run).
   First office run should show `Python: C:\Users\Prince\venvs\nifty-bot\Scripts\python.exe (office PC)`.
2. `scripts/chain-schedule.bat` still uses the old selection (`.venv` if it exists, else office), so on the office
   PC a copied `.venv` would still be chosen there. Out of this task's scope; not changed.
3. A user-level environment variable named `NIFTY_PC` would skip auto-detection; the chosen `PY` is still
   test-started, so this fails safe with a message.

## Required tests missing
1. No automated test runs the batch files under `cmd.exe` (static text tests only, as for the start scripts).
