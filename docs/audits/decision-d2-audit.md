# Risk & Execution Audit Report

Date: 2026-11-15
Scope (files / PR): branch `decision-d2-shadow-harness` vs `main` 459c67a (decision-engine D2: shadow harness).
- **New:** `research/shadow/{__init__,rules,signals,simulate,calibrate,run,report}.py`;
  tests `test_shadow_rules_parity.py`, `test_shadow_simulate.py`, `test_shadow_signals.py`, `test_shadow_e2e.py`.
- **Changed:** `research/__main__.py` (`shadow` command, `shadow_rules`), `research/config.py` + `research.yaml`
  (`shadow` section), `research/store/{schema.sql,store.py}` (v4: `shadow_outcomes`), `research/trading_readonly.py`
  (`signal_rows`, `closed_trades`, `index_close_at`), `scripts/daily-research.bat` (step 6/8 `shadow`), docs.
- **Untouched:** no file in `risk/`, `execution/`, `broker/`, `strategies/`, `runtime/`, `core/`, `data/`, `config/`,
  `dashboard/`, `ml/` or `learning/` changed (`git diff --name-only main`); no existing test changed.

Checks: **1835 passed, 0 failed** (1 existing NumPy deprecation warning); `ruff check .` and `mypy .` (strict,
170 files) clean.

Verdict: **PASS WITH WARNINGS**

## Results
| # | Area | Status (OK / WARN / FAIL / N/A) | Evidence (file:line, test) | Notes |
|---|---|---|---|---|
| 1 | Entry logic | N/A | No bot code changed | The simulator only models entries (`research/shadow/simulate.py:184`); it has no order path. |
| 2 | Exit logic | N/A | unchanged | |
| 3 | Stop-loss logic | OK | Bot `risk/stop_loss_manager.py` unchanged. Research copy `research/shadow/rules.py` checked against it: `test_levels_match_compute_levels` (16 rule combinations x 11 prices), `test_check_exit_matches_the_bot` (2,000 cases) | A copy, not a replacement: trading still uses only `risk/`. |
| 4 | Take-profit logic | OK | Same parity tests; `test_a_target_is_never_credited_above_the_target_level` | Counterfactual targets are credited at most at the target level. |
| 5 | Trailing-stop logic | OK | Bot unchanged; `test_trailing_matches_the_bot_on_random_paths` (4 modes x 8 seeds x 400 prices), `test_disabled_trailing_never_moves_the_stop` | STRUCTURAL trailing is flagged, not simulated. |
| 6 | Position sizing | N/A | unchanged | Counterfactuals are one lot each (lot from the contract list). |
| 7 | Daily loss limits | N/A | unchanged | Not simulated: each counterfactual trade stands alone (documented, Warning 3). |
| 8 | Global loss limits | N/A | unchanged | as 7 |
| 9 | Margin checks | N/A | unchanged | |
| 10 | Order lifecycle handling | N/A | unchanged | |
| 11 | Partial fills | N/A | unchanged | Simulator: top-of-book depth for one lot (`test_the_entry_waits_for_depth_inside_the_timeout`, `test_no_fill_when_the_ask_runs_away_or_depth_is_too_thin`). |
| 12 | Order rejection handling | N/A | unchanged | |
| 13 | Reconciliation | N/A | unchanged | |
| 14 | Square-off logic | N/A | unchanged | Simulator mirrors 15:10 from `cfg.risk.time_rules` (`test_square_off_at_the_first_quote_from_the_square_off_time`, `test_rules_are_built_from_the_bot_configuration`). |
| 15 | Kill-switch behavior | N/A | unchanged | Nothing in D2 can read or set it. |
| 16 | API rate-limit compliance | OK | `shadow` makes no Upstox call: not in `NETWORK_COMMANDS` (`research/__main__.py`); research/shadow imports no network client (isolation tests scan the new files) | |
| 17 | PAPER/LIVE isolation | OK | `research/shadow/*` imports only the standard library and `research.*`; `test_research_isolation.py` (all new files scanned: no `risk`/`execution`/`runtime`/`dashboard`/`data.storage`/config imports, no `open()`, no order calls); trading DB read only via `open_trading_db_readonly` (`?mode=ro`, `query_only`); `test_the_shadow_harness_reproduces_a_real_paper_trade` asserts the trading DB is unchanged | The bot log is read with `Path.read_text` in the composition root only. Only the composition root reads the bot configuration (`shadow_rules`). |
| 18 | Restart recovery | N/A | unchanged | Research store v2/v3 -> v4 migrates by `CREATE TABLE IF NOT EXISTS` (existing tests). `upsert_shadow` is idempotent (e2e test). |
| 19 | Logging and audit trail | OK | `shadow_outcomes` keeps every outcome with `sim_version`, flags, liquidity reasons and the sampling band (`research/store/schema.sql:261`); JSON report stored (`reports`, `shadow`); log signals that cannot be rebuilt are listed | No secrets read: the composition root loads config as before; the log file holds no token (masked by the bot). |

**D2 exit criteria (roadmap):**
- *Real PAPER trades reproduced within tolerance:* the end-to-end synthetic session is reproduced exactly
  (`test_the_shadow_harness_reproduces_a_real_paper_trade`: same contract, entry 0 ticks, exit inside the band, stop
  0 ticks, net per lot within Rs 1). Real data (2026-11-15, scratch copies of `trading.db` / `research.db`): **2 of 2**
  trades with recorded quotes reproduced (replay entry 0 ticks, exits inside the band, exit reasons match); the
  2026-11-09 trade cannot be simulated (no quotes recorded then; its signal is listed as not rebuildable).
- *Report includes the rejected signals:* all 8 size-zero rejections of 2026-11-12/13 are simulated (rebuilt from
  the bot log).

## Blocking issues
None.

## Warnings
1. **Calibration rests on 2 real trades.** It shows the rules and fills are modelled right, not that the
   counterfactual P&L is accurate in general. Re-check as trades accumulate (the `shadow` report prints it daily).
2. **Sampling error is real and measured:** point-in-time entries differ from the bot's by a median 2.5 ticks and
   exits by 7 ticks (5-second quotes; the bot reacts to every tick). Stored outcomes take the conservative end of
   the exit band; entries are not banded. A finer recording interval for the ATM contracts would reduce it (a bot
   recording change: owner decision, not made here).
3. **What a counterfactual ignores:** daily loss limits, cooldowns, the kill switch, entry caps per direction (a
   counterfactual stands alone), multi-lot partial fills, STRUCTURAL trailing, the RL trade manager, and any
   deployed strategy version (the YAML rules are used; the trading DB has no deployments today).
4. **The rules are a copy.** A new stop / target / trailing mode in the bot makes `TradeRules` refuse to build
   (`ValueError`), so the `shadow` command fails loudly rather than simulating wrongly; the parity tests catch a
   changed formula. Keep `research/shadow/rules.py` in step with `risk/` in any change to the trade rules.
5. **"Rejected" mixes reasons.** The 8 rejected signals were refused for a Rs 2,000 risk budget that is now
   Rs 3,000; under today's limit most would have been taken. Group by rejection reason before reading anything into
   approved vs rejected.
6. **The log is read whole** (4 MB today) on every `shadow` run; fine now, revisit if logs grow to hundreds of MB.
7. **NIFTY spot before v7** comes from the 1-minute candle close at or before the signal (`spot_source =
   INDEX_1M_CLOSE`), not the live tick; contract choice matched the bot on both real trades.

## Required tests missing
1. None that can run offline. Calibration on more real trades happens forward, in the daily `shadow` report.
