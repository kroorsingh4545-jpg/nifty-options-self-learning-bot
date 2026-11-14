# R2 findings: forecast vs reality on NIFTY and India VIX

Generated 2026-11-14 from real Upstox data in `data/research.db`:
- NIFTY index daily 2000-01-03 .. 2026-11-13 and 1-minute 2022-01-03 .. 2026-11-13;
- India VIX daily from 2009-03-02.

Every forecast uses only data from before the session it predicts. These are **observations about the past**,
not trading rules. Anything that suggests a strategy change is a **proposal** only: it needs a backtest,
walk-forward, out-of-sample testing, `/audit-risk` and the owner's approval. Regime labels are **provisional R2
buckets**; R3 builds the validated regime engine.

Reproduce: `python -m research history`, `python -m research forecast`, `python -m research report`.

## The first findings in plain words

1. **India VIX usually prices more movement than NIFTY delivers.**
   - Across 4,352 sessions since March 2009, realised day-to-day variance was about **78 %** of what VIX
     implied (95 % CI 67–91 %), i.e. about 12 % less movement in volatility terms.
   - Without the 10 biggest surprise days it is 71 %, so the result is not driven by a few events.
   - It is weaker since 2020: 90 %, CI 68–118 %, which includes "fair".
2. **But the misses are large when they come.**
   - Moves beyond 3 VIX standard deviations happened on **0.51 %** of sessions, about twice the normal
     0.27 %.
   - A surprise day is followed by another one **7.2 %** of the time, against 3.5 % normally: surprises
     cluster.
   - Big down days beyond 2 sd (1.9 %) outnumber big up days (1.6 %).
   - The largest misses are news days: 20 Sep 2019 (corporate tax cut, +6.4 sd), 4 Jun 2024 (election result,
     −5.6 sd), 12 Mar 2020 (COVID, −5.3 sd), 24 Aug 2015, 24 Feb 2022.
3. **The "premium" in VIX is real most of the time and painful some of the time.**
   - Over a matched 30-day horizon, VIX was above the volatility that followed **84 %** of the time, by
     **3.4 volatility points** on average (median 3.7).
   - The worst 5 % of months averaged **−12.6 points**: realised far above implied.
4. **Weekends are neither 1 day nor 3 days.**
   - Measured without any forecast, a Friday-to-Monday move carries **1.9×** the variance of a normal
     overnight move (95 % CI 1.2–2.9).
   - So VIX on calendar time (3 days) over-prices Mondays: variance ratio 0.62.
   - VIX on trading time (1 day) under-prices them: 1.29.
   - On normal nights, calendar time is about right (0.93, CI 0.83–1.04).
5. **Direction matters.**
   - In provisional **uptrends** VIX over-priced movement by about half (variance ratio **0.48**). This holds
     in both 2009–2019 and 2020–2026.
   - In **downtrends** it under-priced it (1.21 overall, 1.65 since 2020, but with wide intervals).
   - Rising VIX (5-day change above 10 %) came with fair pricing (0.99).
   - When VIX was low against its past year, it over-priced the most (0.61).
6. **About one-third of NIFTY's daily movement happens overnight.**
   - Since 2011, **35 %** of daily variance is the gap between the close and the next open, and it was
     higher recently (43 % in 2025, 53 % so far in 2026).
   - An intraday-only strategy never holds that part of the move.
   - The intraday high-low range was about **84 %** of the range implied by VIX's intraday share.
7. **Expiry days move more than the days before them, relative to VIX.**
   - On weekly expiry days the variance ratio was 1.07 (fair to slightly under-priced), against about
     0.75–0.81 one to three sessions before expiry.
   - The intervals overlap, and "sessions to expiry" is tangled with the weekday (Thursday expiries until
     Aug 2025), so treat this as a lead, not a result.
   - Holiday-shifted expiry days: only 19 since 2019, too few to say anything.
8. **No single forecaster wins.**
   - A HAR model (built only on past data) has the best average loss on all three measures of realised
     variance, and VIX on trading time is a close second.
   - HAR's lead over VIX is **not statistically significant** (95 % CI of the loss difference includes 0).
   - VIX carries the most information about *which* days will be volatile (highest R² on squared returns) but
     is biased high. HAR is unbiased.
   - A simple blend of the two scored better than either. That blend was chosen after seeing the data, so it is
     an **untested hypothesis** for R3/R5, not a result.

## What this means for strategy research (proposals to test, not rules)

- **Premium-selling, defined-risk structures** (credit spreads, iron condors) are on the side of findings 1
  and 3 on most days. They are exposed to findings 2 and 5: clustered surprise days and downtrends, exactly when
  VIX under-prices movement. Any such strategy must be judged on its tail days, not its win rate.
- **Long options** pay the premium in finding 3 on average. They are the structures that gain from findings 2
  and 5. An intraday long-option strategy also gives up the overnight third of the move (finding 6).
- **The expected move for the bot's trading window** should not be the raw VIX number:
  - remove the overnight share (finding 6);
  - scale weekend days by about 2 normal days, not 1 or 3 (finding 4).
- **Before any of this changes a strategy**, it has to survive the R6 backtest with real option prices.
  Historical option prices are not available without Upstox Plus. Our own chain snapshots start now.

## Limits of this analysis

- India VIX is a 30-day implied volatility. Using it as a one-day forecast is a horizon mismatch.
  Sections 1–6 measure how useful it is anyway. Section 7 (VRP) uses the matched 30-day horizon.
- NIFTY daily opens before 2011 are unreliable (they often equal the previous close), so gap statistics start
  in 2011. India VIX has 15 missing or invalid days (Dec 2009, Apr–May 2013, 22 Aug 2013, 2 Jul 2019). They
  are left missing.
- Expiry dates are derived from rules plus observed holidays, not from NSE's expiry list. The rules' switch dates
  were confirmed by the owner against NSE circular 103/2025 (2026-11-14). Upstox Plus would be
  needed for real expired contracts.
- Regime buckets are provisional, and many breakdowns have wide intervals. Read the CI column before trusting a
  number.
- There are no option-chain snapshots yet, so ATM IV, skew, term structure and IV rank come from VIX only.
  Run `python -m research chain-snapshot` daily (after 15:40, or with `--during-session`).

---

# Full generated report

# Forecast vs reality: India VIX against realised NIFTY movement

Sessions 2009-03-03 .. 2026-11-13 (every session with a previous-day India VIX close). Each forecast uses only data available before the session opened.

**How to read this.** *Variance ratio* = realised variance / predicted variance: 1.00 means the forecast was right on average, below 1 means it predicted MORE movement than happened. *Inside 1 sd* is the share of sessions whose close-to-close move stayed within the predicted one-standard-deviation range; a normal distribution would give 68.3 % (95.4 % for 2 sd, 0.27 % beyond 3 sd). An expected-move range is a description, not a guaranteed probability.

## 1. Overall calibration of India VIX

| convention | sessions | variance ratio (95 % CI) | inside 1 sd | inside 2 sd | beyond 3 sd | median abs z (normal 0.67) |
|---|---:|---|---:|---:|---:|---:|
| calendar time (VIX/100 x sqrt(days/365)) | 4352 | 0.78 (0.67-0.91) | 75.8 % | 96.5 % | 0.51 % | 0.55 |
| trading time (VIX/100 / sqrt(252)) | 4352 | 0.79 (0.68-0.93) | 78.4 % | 97.6 % | 0.30 % | 0.52 |

Tails: excess kurtosis of the standardised move 1.91 (normal 0). Down moves beyond 2 sd 1.93 % vs up moves 1.56 %. After a beyond-2-sd session the next session is beyond 2 sd 7.2 % of the time vs 3.5 % unconditionally (surprises cluster).

## 2. Weekends: calendar time or trading time?

Without any forecast: a close-to-close move across a weekend has **1.93x** the variance of a normal overnight move (95 % CI 1.23-2.86; 802 weekend vs 3309 normal sessions). Calendar time assumes 3x, trading time 1x.

| convention | after a normal night | after a weekend | after a longer holiday gap |
|---|---|---|---|
| calendar time | VR 0.93 (0.83-1.04), n=3309 | VR 0.62 (0.38-0.79), n=802 | VR 0.56 (0.39-0.86), n=104 |
| trading time | VR 0.64 (0.57-0.72), n=3309 | VR 1.29 (0.79-1.64), n=802 | VR 1.58 (1.09-2.40), n=104 |

## 3. Where the forecast errors are largest

### By provisional volatility regime (VIX percentile vs the past year)

| bucket | sessions | variance ratio (95 % CI) | inside 1 sd | inside 2 sd | beyond 3 sd |
|---|---:|---|---:|---:|---:|
| HIGH | 1259 | 0.91 (0.70-1.18) | 73.6 % | 95.1 % | 0.95 % |
| LOW | 1731 | 0.61 (0.55-0.68) | 77.7 % | 97.0 % | 0.35 % |
| MID | 1236 | 0.66 (0.61-0.72) | 75.7 % | 97.1 % | 0.24 % |

### By provisional trend regime (20-session move vs its expected size)

| bucket | sessions | variance ratio (95 % CI) | inside 1 sd | inside 2 sd | beyond 3 sd |
|---|---:|---|---:|---:|---:|
| DOWN | 476 | 1.21 (0.80-1.62) | 65.1 % | 93.5 % | 1.26 % |
| RANGE | 2721 | 0.79 (0.68-0.95) | 74.7 % | 96.3 % | 0.48 % |
| UP | 1155 | 0.48 (0.41-0.52) | 82.9 % | 98.3 % | 0.26 % |

### By 5-session change in India VIX

| bucket | sessions | variance ratio (95 % CI) | inside 1 sd | inside 2 sd | beyond 3 sd |
|---|---:|---|---:|---:|---:|
| falling >10% | 595 | 0.78 (0.62-0.92) | 73.5 % | 95.5 % | 1.18 % |
| flat | 3123 | 0.72 (0.63-0.83) | 76.3 % | 96.7 % | 0.35 % |
| rising >10% | 618 | 0.99 (0.66-1.46) | 75.7 % | 96.3 % | 0.65 % |

### By weekday

| bucket | sessions | variance ratio (95 % CI) | inside 1 sd | inside 2 sd | beyond 3 sd |
|---|---:|---|---:|---:|---:|
| Fri | 857 | 1.01 (0.90-1.16) | 66.9 % | 94.4 % | 0.93 % |
| Mon | 870 | 0.61 (0.39-0.78) | 88.3 % | 99.0 % | 0.23 % |
| Thu | 874 | 0.99 (0.82-1.17) | 71.9 % | 95.2 % | 0.69 % |
| Tue | 875 | 0.82 (0.71-0.96) | 76.3 % | 96.0 % | 0.46 % |
| Wed | 876 | 0.79 (0.69-0.91) | 75.8 % | 98.0 % | 0.23 % |

### By overnight gap size (in predicted sd; 2011 onwards)

| bucket | sessions | variance ratio (95 % CI) | inside 1 sd | inside 2 sd | beyond 3 sd |
|---|---:|---|---:|---:|---:|
| <0.5 sd | 2919 | 0.56 (0.50-0.61) | 80.9 % | 98.2 % | 0.14 % |
| 0.5-1 sd | 790 | 1.04 (0.91-1.15) | 65.8 % | 94.3 % | 0.63 % |
| 1-2 sd | 173 | 2.49 (1.98-3.02) | 34.7 % | 81.5 % | 3.47 % |

### By year

| bucket | sessions | variance ratio (95 % CI) | inside 1 sd | inside 2 sd | beyond 3 sd |
|---|---:|---|---:|---:|---:|
| 2009 | 197 | 0.82 (0.53-1.25) | 78.2 % | 98.5 % | 0.51 % |
| 2010 | 251 | 0.54 (0.44-0.68) | 80.9 % | 98.0 % | 0.00 % |
| 2011 | 247 | 0.74 (0.67-0.80) | 72.1 % | 96.0 % | 0.00 % |
| 2012 | 247 | 0.58 (0.51-0.65) | 77.7 % | 97.2 % | 0.40 % |
| 2013 | 239 | 0.88 (0.66-1.13) | 71.5 % | 93.3 % | 0.84 % |
| 2014 | 243 | 0.49 (0.32-0.69) | 74.9 % | 96.7 % | 0.00 % |
| 2015 | 247 | 0.83 (0.57-1.14) | 74.5 % | 95.1 % | 1.62 % |
| 2016 | 246 | 0.81 (0.64-0.95) | 72.4 % | 93.9 % | 0.81 % |
| 2017 | 248 | 0.51 (0.45-0.60) | 80.2 % | 98.4 % | 0.00 % |
| 2018 | 246 | 0.69 (0.53-0.91) | 76.8 % | 96.8 % | 0.41 % |
| 2019 | 243 | 0.65 (0.41-1.00) | 77.8 % | 98.4 % | 0.41 % |
| 2020 | 250 | 1.11 (0.54-1.66) | 76.0 % | 96.8 % | 0.80 % |
| 2021 | 248 | 0.72 (0.56-0.87) | 77.0 % | 97.2 % | 0.81 % |
| 2022 | 248 | 0.78 (0.61-0.95) | 74.6 % | 96.0 % | 0.40 % |
| 2023 | 245 | 0.61 (0.52-0.72) | 76.3 % | 98.8 % | 0.00 % |
| 2024 | 246 | 0.89 (0.58-1.33) | 74.8 % | 95.1 % | 0.41 % |
| 2025 | 248 | 0.74 (0.51-0.96) | 76.6 % | 97.2 % | 0.00 % |
| 2026 | 213 | 0.92 (0.69-1.11) | 72.8 % | 93.9 % | 1.88 % |

## 4. Expiry

Weekly NIFTY expiries from 2019-02-11 (rule calendar, holiday shifts applied; Thursday until 28 Aug 2025, Tuesday from 2 Sep 2025).

### By sessions to the next weekly expiry (0 = expiry day)

| bucket | sessions | variance ratio (95 % CI) | inside 1 sd | inside 2 sd | beyond 3 sd |
|---|---:|---|---:|---:|---:|
| 0 | 405 | 1.07 (0.77-1.40) | 74.1 % | 96.8 % | 0.74 % |
| 1 | 404 | 0.75 (0.58-0.92) | 77.7 % | 98.3 % | 0.00 % |
| 2 | 406 | 0.81 (0.65-0.97) | 76.8 % | 96.1 % | 0.25 % |
| 3 | 400 | 0.81 (0.49-1.20) | 81.8 % | 96.8 % | 0.50 % |
| 4+ | 297 | 1.20 (0.99-1.54) | 65.7 % | 95.0 % | 1.68 % |

| expiry days | sessions | variance ratio (95 % CI) | inside 1 sd |
|---|---:|---|---:|
| all expiry days | 405 | 1.07 (0.77-1.40) | 74.1 % |
| monthly expiry days | 93 | 1.06 (0.77-1.59) | 75.3 % |
| holiday-shifted expiry days | 19 | n/a (n/a) | too few sessions |

Caution: sessions-to-expiry overlaps with weekday (before Sep 2025, '4+' was mostly Fridays), so these two tables are not independent evidence.

## 5. Overnight vs intraday

From 2011-01-03: **34.8 % of daily NIFTY variance happens between the close and the next open** (when an intraday strategy holds nothing). Open-to-close variance vs the VIX-implied intraday share: ratio 0.83; intraday high-low range vs the VIX-implied Brownian range: ratio 0.84.

| year | overnight share |
|---|---:|
| 2011 | 30.9 % |
| 2012 | 28.4 % |
| 2013 | 24.0 % |
| 2014 | 24.4 % |
| 2015 | 26.5 % |
| 2016 | 34.6 % |
| 2017 | 34.4 % |
| 2018 | 35.7 % |
| 2019 | 20.9 % |
| 2020 | 41.9 % |
| 2021 | 30.5 % |
| 2022 | 45.4 % |
| 2023 | 32.4 % |
| 2024 | 27.5 % |
| 2025 | 43.4 % |
| 2026 | 52.6 % |

## 6. Which forecast describes the next session best?

Lower QLIKE is better (Patton's robust loss). *Bias* = mean realised / mean predicted variance. *MZ b*: slope of realised on predicted (1 = right scale); *MZ R2*: share of the variation explained.

### proxy: squared close-to-close return (4352 sessions from 2009-03-03)

| forecast | QLIKE | bias | MZ b | MZ R2 |
|---|---:|---:|---:|---:|
| HAR-log | -8.3207 | 1.09 | 1.20 | 0.10 |
| India VIX (calendar time) | -8.3055 | 0.78 | 1.21 | 0.20 |
| India VIX (trading time) | -8.2971 | 0.79 | 1.23 | 0.12 |
| EWMA (0.94) | -8.2961 | 0.99 | 0.75 | 0.06 |
| Trailing 20-day realised | -8.2745 | 1.00 | 0.60 | 0.06 |

### proxy: gap squared + Garman-Klass (2011+) (3904 sessions from 2011-01-03)

| forecast | QLIKE | bias | MZ b | MZ R2 |
|---|---:|---:|---:|---:|
| HAR-log | -8.3848 | 1.03 | 1.11 | 0.15 |
| India VIX (trading time) | -8.3638 | 0.81 | 1.23 | 0.16 |
| EWMA (0.94) | -8.3609 | 1.02 | 0.82 | 0.10 |
| Trailing 20-day realised | -8.3429 | 1.02 | 0.62 | 0.09 |
| India VIX (calendar time) | -8.3348 | 0.80 | 0.87 | 0.13 |

### proxy: gap squared + 5-minute realised variance (2022+) (1197 sessions from 2022-01-03)

| forecast | QLIKE | bias | MZ b | MZ R2 |
|---|---:|---:|---:|---:|
| HAR-log | -8.6486 | 0.95 | 0.79 | 0.10 |
| India VIX (trading time) | -8.6078 | 0.81 | 1.01 | 0.11 |
| India VIX (calendar time) | -8.6065 | 0.80 | 0.55 | 0.08 |
| EWMA (0.94) | -8.5905 | 1.01 | 0.66 | 0.06 |
| Trailing 20-day realised | -8.5548 | 1.01 | 0.49 | 0.04 |

Best forecast per calendar year (squared-return proxy): HAR-log 7, India VIX (trading time) 4, EWMA (0.94) 4, India VIX (calendar time) 3.

## 7. Volatility risk premium (India VIX vs the next 30 days)

4333 sessions 2009-03-02 .. 2026-10-14. India VIX was above the volatility actually realised over the next 30 calendar days **83.9 %** of the time; average premium **3.36 volatility points** (percentiles 5/25/50/75/95: -4.67, 1.28, 3.67, 6.03, 10.92). The worst 5 % of months averaged -12.57 points (realised far above implied).

| regime | sessions | mean | median | VIX above realised |
|---|---:|---:|---:|---:|
| LOW | 1731 | 3.13 | 3.19 | 86.7 % |
| MID | 1235 | 2.99 | 3.38 | 82.2 % |
| HIGH | 1241 | 3.78 | 4.71 | 81.5 % |

## 8. Largest surprises (|move| in VIX-implied sd)

| date | z | NIFTY move | VIX before |
|---|---:|---:|---:|
| 2019-09-20 | 6.37 | 5.18 % | 15.55 |
| 2024-06-04 | -5.58 | -6.11 % | 20.94 |
| 2020-03-12 | -5.25 | -8.67 % | 31.56 |
| 2015-01-06 | -4.11 | -3.04 % | 14.15 |
| 2020-02-28 | -4.06 | -3.78 % | 17.77 |
| 2015-08-24 | -3.93 | -6.1 % | 17.12 |
| 2022-02-24 | -3.81 | -4.9 % | 24.54 |
| 2026-07-08 | -3.51 | -2.14 % | 11.65 |
| 2026-02-03 | 3.47 | 2.52 % | 13.87 |
| 2009-05-18 | 3.46 | 16.33 % | 52.01 |
| 2026-03-19 | -3.38 | -3.32 % | 18.72 |
| 2021-11-26 | -3.38 | -2.95 % | 16.66 |

## 9. Current state (as of 2026-11-13)

NIFTY 25144.3, India VIX 15.5 (1-year percentile 68.7 %, 1-year rank 33.9 %).

Realised volatility (annualised, close-to-close): 5d 7.76 %, 10d 20.35 %, 20d 16.08 %, 30d 16.77 %, 60d 13.71 %. 20-day Parkinson 12.8 %, Garman-Klass 12.91 %, Yang-Zhang 15.64 %.

VIX minus 20-day realised: -0.58 points (historical percentile 11.2 %).

Next session 2026-11-16 (3 calendar day(s) after the last close; forecast recorded live before the open): provisional regime HIGH, trend UP, 1 session(s) to the weekly expiry.

| forecast | 1 sd move |
|---|---:|
| India VIX (calendar time) | 1.405 % |
| India VIX (trading time) | 0.976 % |
| Trailing 20-day realised | 1.013 % |
| EWMA (0.94) | 1.016 % |
| HAR-log | 0.9 % |

VIX expected move (1 sd, calendar time): 353.0 points.
Historically calibrated: 279.0 points (VIX calendar-time move x sqrt(historical variance ratio 0.6214 for 'after_weekend' sessions); a description of the past, not a guarantee).

### Data-quality warnings

- 15 sessions since March 2009 without a VIX forecast (missing VIX closes)
- no option-chain snapshots yet: ATM IV, skew and term structure are not available (run `python -m research chain-snapshot` daily)

