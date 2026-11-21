# Risk & Execution Audit Report
Date: 2026-11-21
Scope (files / PR): branch `research-shadow-variants` vs `research-e1-orb-study`: `research/shadow/variant_rules.py`,
`research/shadow/variants.py`, `research/__main__.py` (`variants` command, shared contract / quote helpers),
`research/store/*` (schema v8 `shadow_variants`), `scripts/daily-research.bat` (step 7/9), README, `docs/research.md`,
`docs/shadow-variants-preregistration.md`, tests `test_shadow_variants*.py`. No file in `risk/`, `execution/`,
`broker/`, `strategies/`, `config/`, `runtime/` or existing tests changed.
Verdict: **PASS WITH WARNINGS**

Tests: full suite 2,224 passed (1 pre-existing warning); ruff clean; mypy clean on research/ and volatility/.

## Results
| # | Area | Status | Evidence | Notes |
|---|---|---|---|---|
| 1 | Entry logic | N/A | `variants.py:82` `enter` = D2 entry; `test_entry_matches_the_d2_simulator` (5 cases) | counterfactual only |
| 2 | Exit logic | N/A | `variants.py:133` stop trigger or 15:10 square-off, END_OF_DATA flagged | `test_run_trade_stop_square_off_end_of_data_and_depth` |
| 3 | Stop-loss logic | N/A | `variants.py:117` index-close stop, simulated | bot stops untouched |
| 4 | Take-profit | N/A | none (registered) | - |
| 5 | Trailing stop | N/A | none (registered) | - |
| 6 | Position sizing | N/A | 1 lot (registered) | bot sizing not simulated |
| 7 | Daily loss limits | OK | config unchanged; not simulated (registered) | - |
| 8 | Global loss limits | OK | `risk_limits.yaml` unchanged; isolation tests | - |
| 9 | Margin checks | N/A | long options only | - |
| 10 | Order lifecycle | N/A | no order code; `test_research_never_calls_order_or_account_methods` scans new files | - |
| 11 | Partial fills | N/A | ask / bid depth waits as D2 | - |
| 12 | Order rejection | N/A | - | - |
| 13 | Reconciliation | N/A | - | - |
| 14 | Square-off | N/A | simulated at configured `square_off_start` (`variants.py:139`) | - |
| 15 | Kill switch | OK | untouched; research never imports risk / execution / runtime | - |
| 16 | API rate limits | OK | `variants` not in `NETWORK_COMMANDS`; no Upstox calls | - |
| 17 | PAPER/LIVE isolation | OK | `main()` refuses non-PAPER; trading DB read-only (`?mode=ro`, `query_only`); bot never imports research | LIVE never enabled |
| 18 | Restart recovery | N/A | idempotent recompute (INSERT OR REPLACE) | - |
| 19 | Audit trail | OK | pre-registration commit `c6280a0` before code; fingerprint test; `before_registration` per row; refactored `shadow` reproduced the stored 10 D2 rows exactly | - |

## Blocking issues
None.

## Warnings
1. Research store schema v8: code on `main` (v7) refuses a `research.db` migrated by this branch; merge E1 and this
   branch before running the new daily script. All runs here used a scratch copy; the real `research.db` is still v7.
2. 13 signals recorded 16-20 Nov have no archived quotes yet (NO_QUOTE): `daily-research.bat` has not run since
   15 Nov; the bot keeps raw quotes 30 days.
3. B needs the recorded ATM +/-5 band to cover the ATM at retest time; otherwise NO_QUOTE (in coverage, not counted).
4. Counterfactuals ignore loss limits, cooldowns, max open trades and liquidity refusals (as D2).
5. Evaluation is years away (first look A 525, B 625 trades).

## Required tests missing
1. No end-to-end test of `_variants` on a temporary store and trading DB (pure parts, store round trip and a manual
   run on a copy were checked).
