> **SYNTHETIC DATA - research artefact only. Trained and evaluated on a seeded random walk, NOT real NIFTY prices. It can never be validated, promoted or used for trading.**

# regime_classifier - orb_options

* data source: **SYNTHETIC**
* status: **REJECTED** (SYNTHETIC: stays TRAINED/REJECTED for ever; never VALIDATED or promoted)
* events: 2782 (OOS scored 936, holdout 244)
* seeds: [11, 23, 37, 41, 53]
* development days: 24, holdout days: 2026-08-04, 2026-08-05, 2026-08-06, 2026-08-07, 2026-08-10, 2026-08-11

## Validation folds (forward-only, purged, embargoed)

| fold | train | test | purged | embargoed | test window | note |
|---|---|---|---|---|---|---|
| 0 | 0 | 234 | 3 | 3 | 2026-07-01T09:45 -> 2026-07-07T11:15 | 0 training events < 100 |
| 1 | 230 | 234 | 5 | 0 | 2026-07-07T10:50 -> 2026-07-13T14:50 |  |
| 2 | 462 | 234 | 11 | 1 | 2026-07-13T14:25 -> 2026-07-21T10:45 |  |
| 3 | 700 | 234 | 2 | 0 | 2026-07-21T10:20 -> 2026-07-27T14:55 |  |
| 4 | 936 | 234 | 0 | 0 | 2026-07-28T10:00 -> 2026-08-03T14:55 |  |

## Headline

| metric | value |
|---|---|
| oos_trend_log_loss | 1.2005 |
| oos_trend_baseline | 0.8919 |
| oos_vol_log_loss | 1.0947 |
| oos_vol_baseline | 1.0012 |
| holdout events | 244 |
| holdout trend_log_loss | 0.8700 |
| holdout vol_log_loss | 0.7153 |
| gates_passed | False |

## Validation gates

| gate | value | threshold | passed |
|---|---|---|---|
| min OOS events | 936 | 100 | yes |
| trend: OOS log loss below the prior baseline | 1.2005 | 0.8919 | NO |
| vol: OOS log loss below the prior baseline | 1.0947 | 1.0012 | NO |

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
