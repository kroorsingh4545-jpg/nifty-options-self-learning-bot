# Risk & Execution Audit Report
Date: 2026-11-21
Scope (files / PR): branch `research-e1-orb-study` vs `main`: `research/orb_study/*` (new), `research/__main__.py`
(`orb-study` command), `docs/orb-e1-preregistration.md`, `docs/orb-e1-findings.md`, `tests/unit/test_orb_study*.py`.
No file in `risk/`, `execution/`, `broker/`, `strategies/`, `config/`, `runtime/` or existing tests changed.
Verdict: **PASS WITH WARNINGS**

Tests: full suite 2,197 passed (1 pre-existing warning); ruff clean; mypy clean on research/ and volatility/.

## Results
| # | Area | Status | Evidence | Notes |
|---|---|---|---|---|
| 1 | Entry logic | N/A | `research/orb_study/setups.py:90`, `:142` | counterfactual entries only; nothing reaches the RiskManager or broker |
| 2 | Exit logic | N/A | `setups.py:112`, `options.py:225` | simulated exits close by 15:10 / 15:29 (tests `test_square_off_exit_...`, `test_our_exit_long_...`) |
| 3 | Stop-loss logic | N/A | `options.py:243` uses `research/shadow/rules.levels` (parity: `test_shadow_rules_parity.py`) | bot stop code untouched |
| 4 | Take-profit logic | N/A | `options.py:225-262` | simulation only |
| 5 | Trailing-stop logic | N/A | `options.py:261` (`Trail.update`, tighten-only, parity-tested) | bot untouched |
| 6 | Position sizing | N/A | 1 lot fixed (`registration.py` lots: 1) | bot sizing not simulated (warning 3) |
| 7 | Daily loss limits | OK | read-only use in `research/__main__.py:545-606`; config unchanged | reported against, never changed |
| 8 | Global loss limits | OK | `risk_limits.yaml` unchanged; isolation test forbids research referencing it | - |
| 9 | Margin checks | N/A | `options.py:166` rough estimate, labelled | no broker margin call (warning 2) |
| 10 | Order lifecycle | N/A | no order code; `test_research_never_calls_order_or_account_methods` scans new files | - |
| 11 | Partial fills | N/A | - | not simulated (1 lot) |
| 12 | Order rejection | N/A | - | - |
| 13 | Reconciliation | N/A | - | - |
| 14 | Square-off | N/A | `data.py` `SQUARE_OFF_BAR = 354` (price at 15:10) | mirrors configured 15:10 |
| 15 | Kill switch | OK | untouched; research never imports risk/execution/runtime (`test_research_isolation.py`); runtime check: none loaded by the command | - |
| 16 | API rate limits | OK | `orb-study` not in `NETWORK_COMMANDS`; reads `research.db` only | no Upstox calls |
| 17 | PAPER/LIVE isolation | OK | `main()` refuses non-PAPER; `backtest.cost_model` import loads no paper broker / factory (checked); bot never imports research | LIVE never enabled |
| 18 | Restart recovery | N/A | stateless batch command | - |
| 19 | Logging / audit trail | OK | pre-registration commit `1bfafd0` before results; fingerprint test + `study.py:112` refuses a changed design; primary and post-hoc reports stored under separate names; .env not read | - |

## Blocking issues
None.

## Warnings
1. All option figures are ESTIMATED (Black-Scholes, VIX as IV, no skew); H4 depends on this most (premium stop).
2. Credit-spread margin is a rough estimate (max loss + 2 % exposure), not SPAN / broker margin.
3. Counterfactuals ignore bot sizing (Rs 3,000 risk), loss halts, cooldowns, max open trades, liquidity filters.
4. The registered session rule excluded 89 normal sessions (stray out-of-hours bars); post-hoc sensitivity labelled
   and stored separately.
5. Spread trading would need short-option support (`allow_short_options: false`) and multi-leg risk sizing; neither
   exists or was changed.

## Required tests missing
1. No end-to-end test of `_orb_study` on a temporary store (pure parts and `orb_study_fills` are tested).
