# Risk & Execution Audit Report

Date: 2026-11-15
Scope (files / PR): branch `decision-d3-volatility` vs `main` 38e7591 (decision-engine D3: volatility evidence).
- **New:** `volatility/{options,business_time,position}.py` (pure maths), `research/shadow/evidence.py`,
  `research/forecast/intraday.py`; tests `test_volatility_options.py`, `test_volatility_business_time.py`,
  `test_volatility_position.py`, `test_shadow_evidence.py`, `test_intraday_study.py`.
- **Changed:** `research/__main__.py` (`shadow` stores evidence; new offline `intraday-vol`), `research/config.py` +
  `research.yaml` (`volatility` section), `research/shadow/report.py`, `research/store/*` (v5: `shadow_evidence`),
  `research/trading_readonly.py` (`index_minute_closes`), docs.
- **Untouched:** no file in `risk/`, `execution/`, `broker/`, `strategies/`, `runtime/`, `core/`, `data/`, `config/`,
  `dashboard/`, `ml/`, `learning/` or `options/` changed (`git diff --name-only main`); no existing test changed.

Checks: **1958 passed, 0 failed** (1 existing NumPy deprecation warning); `ruff check .` and `mypy .` (strict,
175 files) clean.

Verdict: **PASS WITH WARNINGS**

## Results
| # | Area | Status (OK / WARN / FAIL / N/A) | Evidence (file:line, test) | Notes |
|---|---|---|---|---|
| 1 | Entry logic | N/A | No bot code changed | Evidence is computed after the fact, beside shadow outcomes; nothing gates an entry. |
| 2 | Exit logic | N/A | unchanged | |
| 3 | Stop-loss logic | N/A | unchanged | |
| 4 | Take-profit logic | N/A | unchanged | |
| 5 | Trailing-stop logic | N/A | unchanged | |
| 6 | Position sizing | N/A | unchanged | `volatility/position.py` aggregates Greeks for analysis only; it sizes nothing. |
| 7 | Daily loss limits | N/A | unchanged | |
| 8 | Global loss limits | N/A | unchanged | |
| 9 | Margin checks | N/A | unchanged | |
| 10 | Order lifecycle handling | N/A | unchanged | |
| 11 | Partial fills | N/A | unchanged | |
| 12 | Order rejection handling | N/A | unchanged | |
| 13 | Reconciliation | N/A | unchanged | |
| 14 | Square-off logic | N/A | unchanged | The evidence horizon is capped at the configured square-off (`test_the_horizon_is_capped_at_the_square_off`). |
| 15 | Kill-switch behavior | N/A | unchanged | |
| 16 | API rate-limit compliance | OK | `shadow` and `intraday-vol` are not in `NETWORK_COMMANDS` (`research/__main__.py:91`); no network client imported (isolation tests scan the new files) | Offline. |
| 17 | PAPER/LIVE isolation | OK | `volatility/*` imports only `math`, `dataclasses`, `typing`, `collections`, `numpy` and itself (`test_volatility_purity.py` covers the new files); `research/*` passes `test_research_isolation.py`; trading DB only via `open_trading_db_readonly` | The bot never imports `volatility` or `research` (isolation test). |
| 18 | Restart recovery | N/A | unchanged | Research store v5 migrates by `CREATE TABLE IF NOT EXISTS`; `upsert_evidence` is idempotent (`test_evidence_rows_are_stored_with_missing_fields_as_null`). |
| 19 | Logging and audit trail | OK | `shadow_evidence` keyed by (signal, simulator version, evidence version) with units documented in the schema (`research/store/schema.sql:318`); JSON reports `shadow` and `intraday_vol` stored | No secrets read. |

**Unit conventions (owner requirement), tested:**
- IV: decimal vs percent conversion and inference, refusal to guess (`test_iv_units_decimal_and_percent`; the feed
  was confirmed decimal on all 10 recorded signals).
- Calendar 365-day year; window variance under calendar / trading / business clocks with the exact 5.56x factor
  (`test_the_three_clocks_differ_by_exact_factors`); the business clock spreads exactly the total implied variance
  (`test_business_time_spreads_exactly_the_total_implied_variance`).
- Greeks per unit checked by finite differences: vega per volatility point, theta per calendar day and per minute,
  delta and gamma per point (`test_vega_is_per_volatility_point`, `test_theta_is_per_calendar_day_and_minute`,
  `test_delta_and_gamma_per_point`); prices, Greeks and IV identical to the bot's `options/pricing.py`
  (`test_prices_and_greeks_match_options_pricing`, `test_implied_vol_round_trips`).
- Put-call parity on model prices with and without a dividend yield; total-variance form equals calendar form.
- Matched windows: realised and implied variance over the same minutes, no annualisation
  (`test_with_trading_time_variance_the_trading_clock_is_right_and_calendar_understates`, point in time:
  `test_checkpoint_price_is_the_close_of_the_minute_ending_at_the_checkpoint`,
  `test_trailing_overnight_shares_use_only_earlier_sessions`).

## Blocking issues
None.

## Warnings
1. **India VIX over-prices midday intraday variance even in business time** (realised / implied 0.58 for 30-minute
   windows, 2022-2026). Expected moves in the evidence are therefore too large for midday holds and
   `breakeven_to_move` too small. The bias is reported, not corrected: a correction factor would be a fitted
   parameter needing its own out-of-sample validation (D4 / D7).
2. **The business clock is a model** with two inputs from history: the overnight share (trailing, point in time)
   and a weekend weight derived from R2's 1.9x close-to-close multiple; holidays are weighted like weekends. VIX is
   a 30-day all-strike measure, not the IV of the ATM weekly a strategy buys.
3. **IV source:** the feed IV is used whenever its unit checks out against the IV solved from the quote's mid (ratio
   within 0.5-2x); the model behind Upstox's IV is undocumented. Both values are stored (`iv_feed`, `iv_solved`).
   The solved IV uses r = 6.5 %, q = 0 (NIFTY yields about 1 %; small effect on weeklies).
4. **Attribution residuals** are 10-30 % of the option's move on long holds (entry Greeks, no vanna / charm, 5-second
   option quotes vs 1-minute index closes). Fine for describing where P&L came from, not for precise accounting.
5. **Sample size:** 10 signals with evidence. All findings per signal are descriptive; groups are marked
   *too few to judge*.

## Required tests missing
1. None that can run offline. The per-signal evidence grows with every recorded session (daily `shadow` run).
