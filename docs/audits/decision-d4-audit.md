# Risk & Execution Audit Report

Date: 2026-11-15
Scope (files / PR): branch `decision-d4-regime-momentum` vs `main` 01c0644 (decision-engine D4: regime and Momentum
Pulse on history).
- **New:** `research/regime/{__init__,indicators,pulse,regime,orb,samples,validate,shadow}.py`; tests
  `test_regime_indicators.py`, `test_regime_pulse_regime.py`, `test_regime_orb_parity.py`, `test_regime_validate.py`,
  `test_regime_samples_shadow.py`.
- **Changed:** `research/__main__.py` (offline `regime-validate`; `shadow` stores regime labels),
  `research/config.py` + `research.yaml` (`regime` section), `research/shadow/report.py`, `research/store/*`
  (v6: `shadow_regime`), `research/trading_readonly.py` (`index_minute_bars`), docs.
- **Untouched:** no file in `risk/`, `execution/`, `broker/`, `strategies/`, `runtime/`, `core/`, `data/`, `config/`,
  `dashboard/`, `ml/`, `learning/`, `options/` or `volatility/` changed (`git diff --name-only main`); no existing test
  changed.

Checks: **2082 passed, 0 failed** (1 existing NumPy deprecation warning); `ruff check .` and `mypy .` (strict,
183 files) clean.

Verdict: **PASS WITH WARNINGS**

## Results
| # | Area | Status (OK / WARN / FAIL / N/A) | Evidence (file:line, test) | Notes |
|---|---|---|---|---|
| 1 | Entry logic | N/A | No bot code changed | Regime and Pulse labels are stored beside shadow outcomes; nothing reads them to trade. |
| 2 | Exit logic | N/A | unchanged | |
| 3 | Stop-loss logic | N/A | unchanged | |
| 4 | Take-profit logic | N/A | unchanged | |
| 5 | Trailing-stop logic | N/A | unchanged | |
| 6 | Position sizing | N/A | unchanged | |
| 7 | Daily loss limits | N/A | unchanged | |
| 8 | Global loss limits | N/A | unchanged | |
| 9 | Margin checks | N/A | unchanged | |
| 10 | Order lifecycle handling | N/A | unchanged | |
| 11 | Partial fills | N/A | unchanged | |
| 12 | Order rejection handling | N/A | unchanged | |
| 13 | Reconciliation | N/A | unchanged | |
| 14 | Square-off logic | N/A | unchanged | |
| 15 | Kill-switch behavior | N/A | unchanged | |
| 16 | API rate-limit compliance | OK | `regime-validate` not in `NETWORK_COMMANDS` (`research/__main__.py:101`); no network client imported (isolation tests scan the new files) | Offline. |
| 17 | PAPER/LIVE isolation | OK | `research/regime/*` imports only the standard library, numpy, `research.*`, `volatility.*` and, read-only, `ml.labeling`, `ml.regime_classifier`, `ml.feature_engineering` (owner decision R2-2: R3 composes `ml/`, `ml/` untouched); `test_research_isolation.py` passes for every new file; the trading DB is read only via `open_trading_db_readonly` | The bot never imports `research` (isolation test). |
| 18 | Restart recovery | N/A | unchanged | Research store v6 migrates by `CREATE TABLE IF NOT EXISTS`; `upsert_regime` is idempotent (`test_regime_rows_are_stored`). |
| 19 | Logging and audit trail | OK | `shadow_regime` keyed by (signal, simulator version, regime version), with the reasons of every label and the hypotheses kept (`research/store/schema.sql:369`); report `regime_validation` stored with every fold, year and verdict | No secrets read. |

**Validation method (owner requirement), tested:**
- Purged walk-forward by calendar year, training strictly on earlier years; purge identical to
  `backtest.purged_cv.PurgedKFold(forward_only=True)` (`test_purge_matches_backtest_purged_kfold_forward_only`,
  `test_a_training_sample_overlapping_the_test_period_is_purged`, `test_folds_train_only_on_earlier_years_and_cutoffs_ignore_the_test_year`).
- Keep only what is stable: planted effects are KEPT when stable (4 of 4 and 3 of 4 years) and DROPPED otherwise
  (2 of 4 years, no effect, a training-only effect, a reversed one-sided effect, thin groups)
  (`test_regime_validate.py`, 10 tests).
- Point in time: indicators never depend on later bars (`test_no_value_depends_on_later_bars`), sample features
  never see the forward window (`test_features_never_see_the_forward_window`), shadow labels ignore bars after the
  signal (`test_a_shadow_signal_gets_pulse_and_regime_from_bars_before_it`).
- The ORB rule regenerated on history gives exactly the bot strategy's signals on 40 random sessions plus edge
  cases (`test_regime_orb_parity.py`); ADX equals the textbook definition (`test_adx_matches_the_textbook_definition`).

**Result on real 2022-2026 history (scratch copy):** 7 pre-registered hypotheses, 1 KEPT (realised / VIX-implied
variance higher after VIX expansion than after contraction, 4 of 4 years, CI +0.17 .. +0.34); Momentum Pulse,
ORB confirmation and the bot's own 30-bar trailing-trend rule DROPPED (details in `docs/research.md`).

## Blocking issues
None.

## Warnings
1. **Multiple testing.** Seven hypotheses were tested at once; the 3-of-4-years rule plus the pooled interval
   reduces, but does not remove, the chance of a lucky pass. The kept hypothesis is a candidate for D7 shadow
   evaluation (pre-registered, forward data only), not a trading rule.
2. **A reversed result must not be adopted after the fact.** Trending Pulse states were followed by LESS variance
   than chop in all four years, the opposite of the pre-registered one-sided hypothesis; it is DROPPED as
   registered and may only be tested as a new hypothesis on data after 2026-11-13.
3. **History assumes every ORB signal was taken** (no risk refusals, one entry per direction a day) and covers ORB
   only: VWAP+EMA needs futures volume history, which we do not have.
4. **Shadow labels for the 10 recorded signals carry no tested value** except the kept VIX-expansion axis; the Pulse
   label on each signal is recorded for later evaluation only.
5. **`ml` is imported read-only** by research (`trailing_regime`, `RegimePrediction`, the class names): a future change
   to those in `ml/` changes research results too; the evaluation of the bot's trailing rule would then need re-running.
6. **Event-driven regimes are not classified** (no event calendar is recorded), and the UNSTABLE flag's thresholds
   (3 sd gap, top-1 % VIX jump) are fixed, not validated.

## Required tests missing
1. None that can run offline. The forward check of the kept hypothesis belongs to D7.
