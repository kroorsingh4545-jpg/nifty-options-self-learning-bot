# Risk & Execution Audit Report

Date: 2026-11-14
Scope (files / PR): branch `research-r2-forecast-vs-reality` vs `main` 95a7391 (commits c2f53f8, ff1a10e, 6306bdb,
85605cb), 22 files, +2390 / −4.
- **Changed:** `pyproject.toml` (one line: package `volatility*`), `research/__main__.py`, `research/config.py`,
  `research/research.yaml`, `research/store/schema.sql`, `research/store/store.py`.
- **New packages:** `volatility/`, `research/forecast/`, `research/reports/`.
- **New docs:** `docs/research-r2-findings.md`; `docs/research.md` extended.
- **New tests:** `test_volatility.py`, `test_volatility_purity.py`, `test_research_forecast.py`,
  `test_research_analysis.py`.
- **Untouched:** `risk/`, `execution/`, `broker/`, `strategies/`, `config/` and all existing tests.

Verdict: **PASS WITH WARNINGS**

Test evidence:
- Full suite **1622 passed** (10 min 25 s, `.venv`), including 100 new R2 tests.
- `ruff check .` is clean; `mypy .` (strict, 160 files) is clean.

## Results
| # | Area | Status | Evidence (file:line, test) | Notes |
|---|---|---|---|---|
| 1 | Entry logic | N/A | `test_research_isolation.py::test_research_imports_stay_inside_the_allowed_surface` (scans `volatility/` too), `::test_the_trading_bot_never_depends_on_research` | No bot package imports research or volatility. |
| 2 | Exit logic | N/A | as 1 | |
| 3 | Stop-loss logic | N/A | `config.hard_limits` import banned (isolation test) | |
| 4 | Take-profit logic | N/A | as 1 | |
| 5 | Trailing-stop logic | N/A | as 1 | |
| 6 | Position sizing | N/A | | |
| 7 | Daily loss limits | N/A | `::test_research_never_touches_bot_config_files_or_environment` | |
| 8 | Global loss limits | N/A | as 7 | |
| 9 | Margin checks | N/A | | |
| 10 | Order lifecycle | N/A | `::test_research_never_calls_order_or_account_methods`; grep found no order or account calls | |
| 11 | Partial fills | N/A | | |
| 12 | Order rejection handling | N/A | | |
| 13 | Reconciliation | N/A | | |
| 14 | Square-off logic | N/A | | |
| 15 | Kill switch | N/A | `risk`, `runtime` and `execution` imports banned | |
| 16 | API rate-limit compliance | OK | `research/__main__.py:57` (`NETWORK_COMMANDS` excludes `forecast` and `report`); `:218-230` return before `_with_source` (`:157`, `:265`) | R2 adds no Upstox calls. The R1 limiter and session guard are unchanged. |
| 17 | PAPER/LIVE isolation | OK | non-PAPER refused (R1, unchanged); `test_volatility_purity.py` | No path to any order method. |
| 18 | Restart recovery | OK | `research/store/schema.sql:175-185` (live-forecast triggers); guarded upsert `store.py` `upsert_rows`; `test_research_forecast.py:212`, `:238` | Idempotent rebuilds. Live rows are preserved byte-for-byte. Schema v1 to v2 migration is additive. |
| 19 | Logging / audit trail | OK | `reports` table; `vol_forecasts.created_at`, `method_version`, `recorded_live`, `data_issue` | Missing VIX is recorded, never filled (`test_research_forecast.py:145`). |

**Research integrity:**
- **No look-ahead:** `research/forecast/build.py:211` plus `LookaheadError`;
  `test_a_session_outcome_never_changes_its_own_forecast`, `test_har_uses_only_the_past`,
  `test_ewma_is_point_in_time`.
- **Estimators verified** against known-volatility fixtures; the range-estimator bias is documented and tested.
- **R2 never opens the trading database.**

## Blocking issues
None.

## Warnings
1. **The live/not-live label depends on the laptop clock** (`build.py:211`, `now < 09:15`). The laptop is known
   to run about 7 s ahead of NTP. Fix proposed (separate step): a margin of at least 5 minutes before the open,
   or check the Upstox server clock offset first.
2. **Provisional regime labels and trend thresholds** (`forecast.vol_terciles`, `trend_z`) are reporting
   buckets. They must not drive any trading decision before R3 validates them.
3. **Expiry findings rest on the rule-based calendar.** The switch dates are not yet checked against the NSE
   circular (owner action). Holiday-shifted expiry days: n = 19, too few to conclude anything.
4. **In the findings document:** the HAR + VIX blend was chosen after seeing the data, and HAR's lead over VIX
   is not significant. Both are labelled; both need out-of-sample validation.
5. **`upsert_rows` builds SQL from column names** (`# noqa: S608`). It is safe today (whitelisted table,
   columns from code) and must never receive external keys.
6. **`reports` rows accumulate** with no retention (about 40 KB per run).

## Required tests missing
1. A clock-margin test, once warning 1 is fixed.
