# ORB study E1: pre-registration (design e1.v1)

Registered: **2026-11-21**, in its own commit, BEFORE any result of the study was computed.
Design: `research/orb_study/registration.py` `STUDY_E1` (version `e1.v1`).
SHA-256 fingerprint of the design: **`529a524496a9be51d13828d8bad6061b5c3a91c8d14d6f39a7f3fc8952495cf2`**
(`tests/unit/test_orb_study_registered.py` fails if the design changes without a new version).

## Scope

Research and shadow only. The study reads the research store (Upstox NIFTY and India VIX history) and writes its
report to the research database. It never changes the PAPER bot, a strategy, a risk limit, a configuration value or
an existing test. LIVE is never enabled. Step 6 (forward shadow of spreads on real recorded quotes) is a plan only
and needs the owner's approval.

**This is a replication, not an independent test.** The source ("Backtested by Groww", parts 1 and 2) used NIFTY
spot Jan 2020 - Sep 2026; our 1-minute history starts 2022-01-03, so about 4.7 of our 4.9 years are inside the
source's sample, and the source chose its filters on that same data. The only independent evidence is forward
data from **2026-11-21** onward.

## Source claims under test (treated as claims, not facts)

Spot points before brokerage, taxes and slippage; lot 65 for all years; 1,668 sessions.

| Setup | 5 m | 15 m | 30 m | 60 m |
|---|---|---|---|---|
| Breakout | -742 | +3,290 | +6,739 (win 49 %) | +8,346 (1,579 trades, win 51 %, PF 1.16) |
| Retest | +6,101 | - | +7,336 best (1,381 trades) | - |
| Reversal | +4,069 (883 trades) | +4,030 | +2,889 | +2,955 (342 trades, win 52 %) |
| Reversal frequency after first break | 53 % | 42 % | 32 % | 22 % |

Filters (chosen by the source on the same data): A 60 m breakout, range > 0.5 % of open, entry before 12:00 ->
648 trades, +7,021. B 30 m retest, range <= 0.5 % of open, retest within 60 min of the first break -> 713 trades,
+6,487. C 5 m reversal, entry before 12:00 -> 599 trades, +5,424, win ~44 %.

Part 2 claims: credit spread beat debit spread in every setup (breakout ~4x, retest ~2x and profitable 7 of 7 years,
max drawdown ~Rs 24,000; reversal ~3.5x); long ATM made over Rs 1 lakh on breakout and retest; DTE effects for long
options (breakout best 3-4 DTE, retest 0-1 DTE, reversal 1 and 3 DTE, 2 DTE weakest); fixed 1R-5R targets all made
less than holding; capital per lot: long ATM Rs 8-10k, credit spread margin Rs 50-60k, debit spread Rs 4.5-5.5k
premium / ~Rs 40k margin.

## Rules exactly as coded

**Data.** NIFTY 50 index 1-minute candles and India VIX 1-minute closes from the research store, 2022-01-03 onward.
A session is used only if its first bar is 09:15, its last bar 15:29 and it has at least 370 of 375 bars; every
excluded session is listed in the report. A missing minute inside the opening range skips that day for that range
length; a missing minute after the range simply gives no signal at that minute. Lot 65 for every year (as the
source). Years 2022-2025 are tested; 2026 (partial) is reported separately and never counts.

**Source rules (H1-H3, H7-H9).** Range = high / low of the 1-minute bars 09:15 .. 09:15+N-1 (N = 5, 15, 30, 60).
From the next bar, a 1-minute close above the high is an up break, below the low a down break. Entry at the signal
bar's close. Stop: a later 1-minute close beyond the opposite side of the range, exit at that close. No target. Exit
otherwise at the close of the 15:29 bar (an entry on the 15:29 bar is not taken). One trade per day per setup.

* BREAKOUT: the first close beyond the range.
* RETEST: after the first breakout, a later close back inside the range (low <= close <= high), then the first later
  close beyond the same side, entry in the original direction. Literal reading: an intervening close beyond the
  other side does not cancel it.
* REVERSAL: after the first breakout, the first later close beyond the opposite side, entry in the opposite
  direction; its stop is a close back beyond the first-break side.
* Filters: "open" = open of the 09:15 bar; "entry before 12:00" = the entry bar closes before 12:00.

**Our ORB (H4-H6).** From the bot's `orb_options` configuration (read-only, by the research composition root) and
the parity-tested replica `research/regime/orb.py`: range 09:15-09:30, 5-minute close beyond high + 5 / low - 5,
entry window 09:30-11:00, one entry per direction, range 10 points to 1.5 %; ATM option; stop 25 % of premium,
target 2 R, trailing (R multiple, from 1 R, step 0.5 R); square-off 15:10 (close of the 15:09 bar). Premium exits
are checked on every 1-minute bar: the stop on the bar's adverse extreme first, then the target on the favourable
extreme, then the trailing stop moves (effective from the next bar); a stop or target fills at its level, or at the
bar's open price when the bar opened through it.

## Option estimates (H3-H9): ESTIMATED, not real option prices

We have no historical option prices on our data plan. Every option result is a Black-Scholes estimate:

* European BS, r = 0.065, q = 0, strike step 50, IV = India VIX at that minute (flat: no skew, no term structure).
* Expiry: nearest weekly expiry on or after the day (`research.sessions.weekly_expiries`: Thursday until
  2025-08-28, Tuesday from 2025-09-02 per NSE circular 103/2025; a holiday moves it to the previous trading day).
* D3 business-time clock: total implied variance at entry = VIX^2 x calendar years to the 15:30 expiry close; each
  trading minute held uses up the business-time minute variance (overnight share from the trailing 252 sessions,
  weekend gap weight from R2's 1.9 multiple); a VIX change rescales the remaining variance by (VIX_t / VIX_0)^2.
* Documented assumptions: D3 found VIX about 1.7x too high versus realised intraday variance on the business clock;
  this is reported, not corrected, because option prices embed implied variance. VIX (30-day) stands in for the
  weekly ATM IV. No skew, so the 0.2-delta wing is priced at the ATM IV.
* ATM = round(spot / 50) x 50; the "0.2 delta" strike = the 50-step strike whose model |delta| is nearest 0.20.
* LONG_ATM: buy ATM call (up) / put (down). DEBIT: buy ATM, sell 0.2-delta of the same type. CREDIT: sell ATM of the
  opposite type, buy its 0.2-delta protection (bullish = put credit spread, bearish = call credit spread). 1 lot.

**Costs.** `backtest/cost_model.py` with the configured rates: brokerage per order, STT (sell side), exchange, GST,
SEBI, stamp (buy side); quote from `CostModel.quote` (half spread 0.25 % of price, at least 1 tick); buys at the ask,
sells at the bid, plus `paper_broker.slippage_ticks` adverse ticks; tick 0.05. Long ATM = 2 orders, spreads = 4.

## Hypotheses

| ID | What | Metric | Pass rule |
|---|---|---|---|
| H1 | 12 source variants (3 setups x 4 ranges), spot points before costs | trades, total points, win %, PF, avg win, avg loss, by year; vs source (scaled) | descriptive (replication) |
| H2 | the 3 filtered setups exactly as stated, spot points | same, by year | descriptive (replication) |
| H3 | H1 + H2 as approximate long ATM per lot: 0.5 x points x 65 minus long-ATM costs (estimated premium as cost basis) | Rs per lot after costs; share of the gross edge removed by costs | pass rule |
| H4 | our ORB as the bot runs it | estimated long ATM, Rs per lot after costs | pass rule |
| H5 | our ORB entries, THEIR exit (1-min close beyond the opposite side of our 15 m range, no target, no trailing, 15:10) | paired H5 - H4 per signal | pass rule on the paired difference |
| H6 | A_BREAKOUT_60 entries with OUR exit | paired vs the same entries with their exit (H2 A, estimated long ATM, exit 15:29) | pass rule on the paired difference |
| H7 | each filtered setup: LONG_ATM vs DEBIT vs CREDIT, source stops / exit 15:29 | Rs per lot after costs, by year, max drawdown, margin estimate, fit in Rs 1,00,000 with our loss limits | pass rule per structure and on CREDIT - DEBIT, CREDIT - LONG, DEBIT - LONG |
| H8 | LOW PRIOR: long ATM by trading days to expiry (0, 1, 2, 3, 4+) | n, mean, by-year sign per bucket | descriptive only; no bucket is selected |
| H9 | fixed spot targets 1R..5R (R = entry to the opposite range side) vs hold to stop / 15:10, on LONG_ATM and CREDIT | paired target - hold per setup | pass rule on each paired difference |

**Pass rule** (each tested result, rupees per lot after costs): (1) mean per-trade result > 0; (2) the same sign
in at least 3 of the 4 calendar years 2022-2025; (3) the day-block bootstrap 95 % percentile interval of the mean
excludes zero (10,000 draws, seed 20261121). A comparison uses the same entries and tests the paired per-trade
difference with the same three conditions.

**Multiple-testing note.** About 50 pass-rule tests run on one history that overlaps the source's. At 5 % each,
2-3 false passes are expected by chance alone, and the source picked its filters on this data. Every pass is also
checked with a Bonferroni interval (alpha = 0.05 / number of pass-rule tests actually run); a pass that fails it is
labelled WEAK. Every variant tested is reported, including failures. Anything found after seeing the results is
labelled POST-HOC and only becomes a new hypothesis for forward shadow testing.
