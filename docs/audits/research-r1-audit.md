# Risk & Execution Audit Report

> **Re-audit (same day) is at the end of this file.** The first audit below is kept as it was.

Date: 2026-11-14
Scope (files / PR): branch `research-r1-data-foundation` (from `main` 87e4936):
`broker/upstox_rest.py` (new read-only `get_daily_candles`), `pyproject.toml` (packages `research*` and its
data files), new package `research/` (config, store, trading_readonly, sources, sessions, collect/*, smoke,
`__main__`), `docs/research.md`, new tests `tests/unit/test_research_isolation.py`, `test_research_store.py`,
`test_research_collect.py`, `test_upstox_rest_daily.py`. No existing test was changed.
Verdict: **PASS WITH WARNINGS**

Test evidence: full suite **1515 passed** (16 min 55 s, project `.venv`), including 123 new research and
daily-candle tests. `ruff check .` and `mypy .` (strict, 148 files) are clean.

## Results
| # | Area | Status | Evidence (file:line, test) | Notes |
|---|---|---|---|---|
| 1 | Entry logic | N/A | `test_research_imports_stay_inside_the_allowed_surface`, `test_the_trading_bot_never_depends_on_research` | Research cannot reach strategies, risk or execution. The bot never imports research. |
| 2 | Exit logic | N/A | as above | |
| 3 | Stop-loss logic | N/A | as above; `config.hard_limits` import banned | Research cannot read or change stop settings. |
| 4 | Take-profit logic | N/A | as above | |
| 5 | Trailing-stop logic | N/A | as above | |
| 6 | Position sizing | N/A | Research stores the lot size from Upstox (`contracts.lot_size`) and has no literal lot sizes | |
| 7 | Daily loss limits | N/A | `test_research_never_touches_bot_config_files_or_environment` | No access to `risk_limits.yaml` or `risk/`. |
| 8 | Global loss limits | N/A | as 7 | |
| 9 | Margin checks | N/A | | No margin calls (account APIs refused by the scope guard and not exposed by `ReadOnlySource`). |
| 10 | Order lifecycle | N/A | `test_research_never_calls_order_or_account_methods` | |
| 11 | Partial fills | N/A | | |
| 12 | Order rejection handling | N/A | | |
| 13 | Reconciliation | N/A | | |
| 14 | Square-off logic | N/A | | |
| 15 | Kill switch | N/A | research cannot import `risk`/`runtime`/`execution` | Research cannot trigger, clear or bypass it. A research 401 does not call the bot's auth-failure hook (separate process, `on_auth_failure` unset). |
| 16 | API rate-limit compliance | **WARN** | `broker/upstox_rest.py` `get_daily_candles` → `_get` → `_request` (scope check, then `limiter.acquire`); `test_daily_candles_go_through_the_central_rate_limiter`, `test_daily_candles_retry_429_then_give_up`, `test_daily_candles_refuse_bad_ranges_before_sending`; `research/__main__.py` `research_limits`; `test_research_rate_share_is_below_the_bot_budget` | Every research call passes through an `UpstoxRateLimiter`. But it is a **second limiter instance in a second process** on the same token. Bot (80 % of official) + research (25 % of that = 20 % of official) could add up to **100 % of the official limits**, which removes the 20 % safety buffer if both were saturated at once. Mitigations already in place: bulk `history` is refused on weekdays 09:00–15:40 IST (`ensure_outside_window`, tested). `smoke`, `contracts` and `chain-snapshot` make at most about 7 calls. The bot's REST usage is light (market data comes from the WebSocket). See warning 1. |
| 17 | PAPER/LIVE isolation | OK | `research/__main__.py` refuses `TRADING_MODE` other than PAPER; `ReadOnlySource` exposes only 4 read methods (`test_read_only_source_exposes_only_market_data_reads`); order methods on `UpstoxRestClient` unchanged (always refuse); analytics-token scope guard unchanged (`test_daily_candles_are_read_only_and_never_route_orders`) | No path from research to any order method. |
| 18 | Restart recovery | OK | `ResearchStore.completed_ranges`, `test_daily_download_saves_resumes_and_skips_complete_ranges`, `test_summarize_recorded_quotes_is_idempotent` | Research jobs are idempotent and resumable. The trading DB is untouched: opened `?mode=ro` + `PRAGMA query_only` (`test_read_only_trading_connection_refuses_writes`). The trading DB is in WAL mode, so research readers never block the bot's writes. `ResearchStore.open` refuses the trading DB path (`test_research_store_refuses_the_trading_database`). |
| 19 | Logging / audit trail | OK | `fetch_log` table (every Upstox call: range, rows, status, error), `observations` table | The token is read only by `config.loader` and never printed. Error text comes from `UpstoxRestClient`, which masks secrets. |

## Blocking issues
None.

## Warnings
1. **Combined rate budget (area 16).** Bot + research limiters can together reach 100 % of Upstox's official
   limits in the worst case. Proposed fix (separate step, after owner review): refuse every research network
   command during the weekday session window, not only `history`, unless an explicit per-run flag is given. Or
   take research's budget out of the 20 % buffer explicitly (for example `rate_share` ≤ 0.1, so the worst case is
   88 % of official) and add a test that asserts bot + research ≤ 90 % of official.
2. **Unverified live behaviour.** The daily-candle timestamp shape (`YYYY-MM-DDT00:00:00+05:30`), the first
   available India VIX date, whether the analytics token gets VIX history, and the REST option-chain IV unit
   are covered only by mocked tests. `python -m research smoke` checks all four live (about 7 read-only calls).
   It has **not been run**: it needs the owner's go-ahead.
3. **Contract metadata deadline.** The 12–13 Nov recorded quotes (now copied to `research.db`) only carry
   instrument keys. Their strike / expiry / type comes from the Upstox contract list, which drops the 17 Nov
   contracts after expiry. `python -m research contracts` (1 call) must run before 17 Nov 2026 15:30, or those
   bars lose their strike mapping.
4. **Expiry weekday rules come from secondary sources** (NSE/FAOP/68747 as reported by brokers and news). Derived
   expiries are marked `rule` until observed. The owner should verify against the NSE circular.

## Required tests missing
1. A combined-budget test (bot + research ≤ the buffered official limit), once warning 1 is fixed.
2. A live smoke run (manual, owner-approved). It is not an automated test, by design.

---

# Re-audit: R1 after the warning-1 fix and the first live run

Date: 2026-11-14 (Saturday, market closed)
Scope: commit ad76e55 plus the follow-up changes on `research-r1-data-foundation`:
`research/config.py`, `research/research.yaml` (`rate_share` max 0.1, `queue_timeout_seconds`, window from
08:30), `research/__main__.py` (`network_guard` on every Upstox command, `--during-session` for `contracts` and
`chain-snapshot` only), `research/collect/history.py` (invalid-candle isolation), `research/sessions.py`
(no mismatch report for years without configured holidays), tests, `docs/research.md`, `docs/research-plan.md`.
`broker/` is unchanged since the first audit. No existing test was changed.
Verdict: **PASS WITH WARNINGS**

Test evidence: full suite **1522 passed** (8 min 32 s, project `.venv`; 130 research and daily-candle tests). `ruff check .` and `mypy .` (strict, 148 files) are clean. Live run: smoke 5/5 PASS, contracts, history (132 fetch-log rows).

## Changed results
| # | Area | Status | Evidence | Notes |
|---|---|---|---|---|
| 16 | API rate-limit compliance | **OK** (was WARN) | `test_bot_plus_research_stay_inside_the_official_limits` (configured share and 0.1 / 0.05 / 0.01), `test_research_rate_share_cannot_exceed_ten_percent`, `test_every_network_command_is_blocked_during_the_session_window`; `research/__main__.py` `network_guard`, `research_limits` | Worst case with both processes saturated: standard API 44/s, 440/min, 1760/30 min = **88 %** of official. Order API 9/s = 90 % (the limiter's 1-per-window floor; research never calls the order API). On top of that, every research Upstox command is refused on weekdays 08:30–15:40 unless it is `contracts` / `chain-snapshot` with `--during-session` (about 5 calls). Live: the 10 % budget throttled the VIX download correctly (one `RateLimitError` after the 30 s queue wait). Research now waits up to 120 s for its own budget and never borrows the bot's. |
| 18 | Restart recovery | OK | `test_invalid_upstox_candles_are_isolated_and_skipped` (re-run makes no call), live re-run skipped 6 + 14 completed ranges | |
| 19 | Logging / audit trail | OK | `fetch_log`: 132 rows from the live run, each with range, rows, status, error or skipped-day note | |

All other areas are unchanged from the first audit (N/A, or OK with the same evidence).

## Status of the first audit's warnings
1. **Combined rate budget: FIXED** (area 16 above).
2. **Unverified live behaviour: VERIFIED.** `python -m research smoke`, 5/5 PASS:
   - India VIX daily works with the analytics token;
   - the first VIX candle on Upstox is **2009-03-02**;
   - VIX 1-minute from 2022-01-03 works;
   - NIFTY daily works;
   - daily candles parse as session dates;
   - the **REST chain IV is in percent** (median ratio 87.7, measured on a Saturday against Friday's prices).

   The WebSocket IV stays a decimal.
3. **Contract metadata deadline: DONE.** 2,242 contracts saved. All 28 instrument keys in the recorded quote
   bars map to a strike and expiry.
4. **Expiry weekday rules: STILL OPEN.** They are consistent with the data (2025: 21 Aug, 28 Aug, then
   2 Sep, 9 Sep), and the contract list confirms the rule-based 23 Nov 2026 holiday shift. The rules are still
   not verified against the NSE circular text itself. Owner action.

## New warnings
5. **Upstox data quality: one invalid India VIX daily candle (2013-08-22, OHLC inconsistent).** It is not kept
   and not repaired: the day is skipped and named in `fetch_log`. Downstream research (R2) must treat it as
   missing. The bisection is bounded (at most 64 extra calls per range). A range with more bad days than that
   is logged as ERROR and keeps no data (tested).
6. **Observation outside research (no change made):** the bot's `broker.upstox_mapping.parse_candles` rejects
   a whole response when one candle has inconsistent OHLC. In the bot that fails safe (the history load fails,
   so entries are blocked for the day, per `runtime/history.py`). But one bad Upstox minute candle would block a
   strategy for a whole day. Worth a separate, audited look at the bot. It is out of scope for research.

## Blocking issues
None.

## Required tests missing
None for R1.
