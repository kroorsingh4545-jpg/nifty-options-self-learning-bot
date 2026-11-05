> **SYNTHETIC DATA - research artefact only. Trained and evaluated on a seeded random walk, NOT real NIFTY prices. It can never be validated, promoted or used for trading.**

# signal_filter - orb_options

* data source: **SYNTHETIC**
* status: **REJECTED** (SYNTHETIC: stays TRAINED/REJECTED for ever; never VALIDATED or promoted)
* events: 2782 (OOS scored 1848, holdout 472)
* seeds: [11, 23, 37, 41, 53]
* development days: 24, holdout days: 2026-08-04, 2026-08-05, 2026-08-06, 2026-08-07, 2026-08-10, 2026-08-11

## Validation folds (forward-only, purged, embargoed)

| fold | train | test | purged | embargoed | test window | note |
|---|---|---|---|---|---|---|
| 0 | 0 | 462 | 3 | 3 | 2026-07-01T09:45 -> 2026-07-07T11:06 | 0 training events < 100 |
| 1 | 461 | 462 | 2 | 0 | 2026-07-07T10:50 -> 2026-07-13T14:40 |  |
| 2 | 915 | 462 | 11 | 4 | 2026-07-13T14:25 -> 2026-07-21T10:26 |  |
| 3 | 1385 | 462 | 1 | 0 | 2026-07-21T10:20 -> 2026-07-27T14:41 |  |
| 4 | 1848 | 462 | 0 | 0 | 2026-07-28T10:00 -> 2026-08-03T14:40 |  |

## Headline

| metric | value |
|---|---|
| oos_auc_mean | 0.7445 |
| oos_auc_std | 0.0075 |
| oos_filtered_expectancy | -369.82 |
| oos_baseline_expectancy | 139.57 |
| oos_filtered_trades | 12 |
| holdout events | 472 |
| holdout auc | 0.7668 |
| holdout brier | 0.1066 |
| holdout filtered_expectancy | - |
| holdout baseline_expectancy | 92.2000 |
| holdout filtered_trades | 0 |
| gates_passed | False |

## Trading metrics (event level, net of charges and slippage)

| set | trades | win rate % | profit factor | expectancy | net P&L | max DD | avg hold min | compliance |
|---|---|---|---|---|---|---|---|---|
| OOS baseline_no_filter | 497 | 38.6300 | 1.1430 | 139.57 | 69,368.65 | 26,661.19 | 18.3900 | PASS WITH WARNINGS |
| OOS filtered | 12 | 25.0000 | 0.6050 | -369.82 | -4,437.87 | 9,812.27 | 17.0000 | PASS WITH WARNINGS |
| holdout baseline_no_filter | 244 | 39.3400 | 1.1150 | 92.2000 | 22,496.70 | 24,594.20 | 36.6800 | PASS WITH WARNINGS |
| holdout filtered | 0 | - | - | - | 0.0000 | 0.0000 | - | PASS |

## Validation gates

| gate | value | threshold | passed |
|---|---|---|---|
| min OOS events | 1848 | 100 | yes |
| mean OOS AUC over seeds | 0.7445 | 0.5500 | yes |
| AUC spread across seeds (std) | 0.0075 | 0.0500 | yes |
| filtered OOS trades | 12 | 30 | NO |
| filtered OOS expectancy > 0 (net of costs) | -369.82 | 0 | NO |
| filtered OOS expectancy > no-filter baseline | -369.82 | 139.57 | NO |
| OOS risk compliance (no BREACH) | PASS WITH WARNINGS | PASS / PASS WITH WARNINGS | yes |
| holdout AUC | 0.7668 | 0.5000 | yes |
| holdout risk compliance (no BREACH) | PASS | PASS / PASS WITH WARNINGS | yes |

## Comparison with production

no production model for this strategy: compared with the no-filter baseline only

## Leakage checks

* features computed by compute_features from bars completed at or before as-of (Bars.upto); verified by tests that rewrite every later bar and get identical features
* every label starts at its feature as-of time (t0 == as_of, asserted) and ends at or after it (t1 >= t0)
* recent-outcome features use only labels known by as-of (t1 <= as_of)
* regime features are trailing rules; the forward regime model is never a training feature
* no dataset-wide scaling or normalisation (no scaler to fit, nothing to leak)
* chronological split; never shuffled
* forward-only purged K-fold: training rows whose [t0, t1] overlaps a test block are purged, rows starting within the embargo after it are dropped, rows after it are never used
* final holdout = last days, untouched until the single final evaluation; development rows whose label ends after the holdout starts are purged
