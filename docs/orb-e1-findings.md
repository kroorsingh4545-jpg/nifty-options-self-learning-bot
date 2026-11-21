# ORB study E1: findings (step 5 report)

Study design `e1.v1`, pre-registered in `docs/orb-e1-preregistration.md` (commit `1bfafd0`, before any result).
Reproduce: `python -m research orb-study` (report `orb_e1_study` in `data/research.db`); the post-hoc session
sensitivity: `python -m research orb-study --posthoc-ignore-stray-bars` (report `orb_e1_study_posthoc_stray_bars`).
Research only. Nothing in the PAPER bot, strategies, risk limits or configuration was changed. Step 6 was not
started.

**Every option rupee figure below is ESTIMATED, not real option prices** (Black-Scholes with India VIX as the IV,
D3 business-time clock, no skew; see "Estimate quality"). Spot-point figures are real index moves, before costs.

## The short answer

1. **The source's spot-point rules reproduce well.** Trade counts, win rates, reversal frequencies and the per-trade
   average of the three filtered setups land close to the video's numbers. Some unfiltered variants are smaller
   than claimed (5-minute retest, 15/30-minute reversal).
2. **Nothing survives our pre-registered rule with multiple-testing control.** 68 tests were run; **0 PASS**,
   8 are WEAK (they pass the 95 % rule but not the Bonferroni-corrected one), 60 FAIL. The 8 are not 8 independent
   findings: four of them test the same 5-minute reversal trades against different targets. After costs, the best results are long ATM options on the three filtered
   setups, and the 30-minute retest is the most consistent (positive in every year 2022-2026).
3. **The exit finding (H5 vs H4), in one sentence:** on the same ORB entries the bot takes, the source's exit (stop
   only when a 1-minute close goes back beyond the other side of the opening range, no target, no trailing, hold
   to 15:10) did about **Rs 302 per trade per lot better** than our 25 % premium stop + 2R target + trailing stop,
   which itself **lost money in every year 2022-2025** (estimated); the improvement is WEAK (3 of 4 years, 95 %
   interval excludes zero, Bonferroni interval does not).
4. **Spreads: the source's claim that credit spreads beat debit spreads is not supported by our estimates.** Both
   spreads lose slightly after costs (4 orders each), credit and debit are statistically the same, and both are
   clearly worse than a plain long ATM option on these signals (ESTIMATES).
5. **A 1-lot credit spread does not fit our risk rules as written**: its maximum loss at entry is above our Rs 3,000
   per-trade risk in about 85-89 % of trades and above the Rs 5,000 daily loss limit in 72-80 %; the PAPER broker
   also refuses short options (`allow_short_options: false`). Its estimated margin (Rs 37k median) fits in
   Rs 1,00,000 capital.
6. **Forward testing will be slow.** At the effect sizes seen, confirming any one of these with 80 % power needs
   roughly 500-1,250 trades, i.e. about 5 to 13 years at one setup's natural trade rate. A faster, useful first
   step is to check the option-price ESTIMATE itself against our real recorded quotes.

## Step 1: data check

| Item | Value |
|---|---|
| Source | Upstox NIFTY 50 index 1-minute candles and India VIX 1-minute candles, research store |
| Range in store | 2022-01-03 to 2026-11-13 (the store was not refreshed for 16-20 Nov; no network calls were made) |
| Sessions in store | 1,207 |
| Sessions used (registered rule) | **1,111** (2022: 163, 2023: 245, 2024: 246, 2025: 243, 2026: 214) |
| Excluded (registered rule) | 96 (below) |
| Missing NIFTY minutes in used sessions | 7 minutes in 5 sessions (worst 2022-03-07: 3) |
| Missing VIX minutes in used sessions | 4 minutes in 4 sessions (filled from the previous minute) |
| Weekday holidays in range | 70 (12 / 15 / 16 / 13 / 14 for 2022-2026) |
| Weekend sessions used | 2024-01-20 (Saturday special session), 2025-02-01 (Budget Saturday), 2026-02-01 (Budget Sunday) |
| Weekly expiry | Thursday until 2025-08-28, Tuesday from 2025-09-02 (checked: 08-21, 08-28, 09-02, 09-09); holiday -> previous trading day |

Excluded sessions under the registered rule (first bar 09:15, last bar 15:29, at least 370 bars):

* **84 normal sessions, 2022-03-24 to 2022-07-22**: each has one extra flat bar at 09:07 (the pre-open price), so
  its "first bar" is not 09:15. Their 09:15-15:29 bars are complete.
* **5 normal sessions in Apr-May 2025** (04-25, 04-30, 05-08, 05-13, 05-21): stray flat bars at 15:39 / 15:59.
* **7 special sessions**, correctly excluded: Muhurat 2022-10-24, 2023-11-12, 2024-11-01, 2025-10-21, 2026-11-08;
  Saturday half sessions 2024-03-02 and 2024-05-18.

The 89 normal sessions were excluded by the literal wording of my registered rule, which did not intend that. The
registered run is the primary result. A **POST-HOC sensitivity** that drops bars outside 09:15-15:29 first (1,200
sessions, only the 7 special sessions excluded) gives the same conclusions: 0 PASS, 7 WEAK (A_BREAKOUT_60 long ATM
drops from WEAK to FAIL; B_RETEST_30 long ATM rises from FAIL to WEAK). Both are reported below where they differ.

**Overlap.** The source used Jan 2020 - Sep 2026. All but the last ~7 weeks of our data are inside that period, and
the source chose its filters on it. **This is a replication, not an independent test.** The only independent
evidence will be forward data from 21 Nov 2026 on. 13 Mar 2020 (excluded by the source) is outside our range.

## Step 2: replicating Part 1 (spot points, before costs)

Source totals are scaled by our sessions / theirs (1,111 / 1,668 = 0.666). Per-trade averages need no scaling.

| Variant | Our trades | Our total | Win % | PF | Source total (scaled) | Our pts/trade | Source pts/trade |
|---|---|---|---|---|---|---|---|
| Breakout 5 m | 1,107 | -64 | 37.8 | 1.00 | -494 | -0.06 | - |
| Breakout 15 m | 1,103 | +5,172 | 45.3 | 1.12 | +2,191 | 4.69 | - |
| Breakout 30 m | 1,082 | +6,612 | 50.3 | 1.16 | +4,489 (win 49 %) | 6.11 | - |
| Breakout 60 m | 1,041 | +5,445 | 51.8 | 1.15 | +5,559 (win 51 %, PF 1.16) | 5.23 | 5.29 |
| Retest 5 m | 899 | +1,035 | 39.8 | 1.03 | +4,064 | 1.15 | - |
| Retest 15 m | 904 | +6,701 | 46.9 | 1.20 | - | 7.41 | - |
| Retest 30 m | 907 | +6,901 | 51.7 | 1.22 | +4,886 | 7.61 | 5.31 |
| Retest 60 m | 872 | +5,366 | 51.9 | 1.19 | - | 6.15 | - |
| Reversal 5 m | 581 | +3,971 | 44.9 | 1.22 | +2,710 | 6.84 | 4.61 |
| Reversal 15 m | 448 | +796 | 46.2 | 1.06 | +2,684 | 1.78 | - |
| Reversal 30 m | 329 | +708 | 47.4 | 1.08 | +1,924 | 2.15 | - |
| Reversal 60 m | 213 | +1,171 | 52.6 | 1.23 | +1,968 (win 52 %) | 5.50 | 8.64 |

Reversal frequency after the first break: ours 52.5 / 40.6 / 30.4 / 20.5 % vs the source's 53 / 42 / 32 / 22 %.
Trade frequency matches too (60 m breakout on 94 % of days vs their 95 %; 30 m retest 82 % vs 83 %).

**H2, the three filtered setups:**

| Filter | Our trades | Our total | Win % | PF | pts/trade (ours / source) | by year 2022 / 23 / 24 / 25 / 26 |
|---|---|---|---|---|---|---|
| A: 60 m breakout, range > 0.5 %, entry < 12:00 | 373 | +4,094 | 54.7 | 1.25 | 10.98 / 10.84 | +1,129 / -80 / +1,943 / +1,194 / -92 |
| B: 30 m retest, range <= 0.5 %, within 60 min | 516 | +5,682 | 52.3 | 1.37 | 11.01 / 9.10 | +1,879 / +567 / +1,232 / +1,360 / +643 |
| C: 5 m reversal, entry < 12:00 | 413 | +3,818 | 44.1 | 1.28 | 9.24 / 9.06 | +509 / +880 / +1,673 / +1,009 / -253 |

**Reproduced:** the reversal frequencies, the trade frequencies, the win rates, the 60 m breakout per-trade
average, the 5 m breakout being roughly flat, and the per-trade average of all three filtered setups (within
about 2 points). **Not reproduced:** the 5 m retest (ours about a quarter of theirs), the 15 m and 30 m reversals
(ours about a third), and the steady rise of breakout totals with range length (ours peaks at 30 m). Likely
reasons: our period lacks 2020-2021 (including the COVID crash, a strongly trending, high-volatility period), a
different data vendor, and rule ambiguity (I used the literal retest reading; the source may cancel a retest after
an opposite break).

The edge is concentrated: for most variants the best 10 % of days produced 2-5 times the whole total (up to 18
times for the weakest), so the other 90 % of days lose money together. Filtered setups: A 2.3x, B 1.8x, C 2.5x.

**H3, spot points as an approximate long ATM per lot (0.5 x points x 65, after our real cost model):** costs are
about Rs 105 per round trip (2 orders: brokerage + GST about Rs 47, STT, exchange, stamp, spread and 1 tick of
slippage each side, on an estimated ATM premium of about Rs 97). Costs remove **29-35 % of the gross edge** of the
filtered setups, 43-70 % of most unfiltered ones, and **more than all of it** for the 5 m breakout, 5 m retest and
15 / 30 m reversals. Only B (30 m retest) is WEAK (mean Rs 254 / trade, 4 of 4 years, 95 % interval 0.4 to 512); all
other 14 fail, mostly because the interval includes zero.

## Step 3: our ORB vs theirs (the exit question; ESTIMATED long ATM, 1 lot, after costs)

| | Trades | Total Rs | Rs / trade | Win % | 2022 | 2023 | 2024 | 2025 | 2026 (partial) | Verdict |
|---|---|---|---|---|---|---|---|---|---|---|
| H4 our ORB, our exit | 840 | -1,32,501 | -158 | 46.7 | -33,127 | -55,533 | -7,569 | -9,796 | -26,475 | FAIL (interval -292 to -25: reliably negative) |
| H5 our ORB entries, their exit (15:10) | 840 | +1,21,198 | +144 | 41.2 | +39,884 | -67,986 | +1,45,768 | +11,814 | -8,281 | FAIL (interval includes 0) |
| H5 - H4, same entries | 840 | +2,53,699 | +302 | | +73,011 | -12,453 | +1,53,337 | +21,610 | +18,194 | **WEAK** |
| H6 their A entries, our exit | 322 | +17,827 | +55 | 50.6 | -10,340 | -8,072 | +41,813 | -8,745 | +3,172 | FAIL |
| same A entries, their exit (15:29) | 322 | +90,803 | +282 | 46.6 | +40,185 | -13,632 | +75,648 | +4,253 | -15,651 | (baseline) |
| H6 - H2 A, same entries | 322 | -72,975 | -227 | | -50,525 | +5,560 | -33,836 | -12,998 | +18,824 | FAIL |

Of 1,012 ORB signals, 172 were not counted because the estimated premium fell outside the bot's Rs 40-400 band. Our exits: 404 stops, 304 trailing stops, 50 targets,
82 square-offs. Liquidity filters (volume, OI, spread) cannot be checked on history.

What this says, in plain words: **the entry is not where our ORB loses; the exit is.** Our 25 % premium stop is hit
often (about half of the trades), and the 2R target and trailing stop cut the rare big trend days that pay for
everything else. With the source's wide, range-based stop and no target, the same entries become roughly break-even
to slightly positive, and the same "our exit" hurts the source's own good entries (H6). Profit is concentrated:
for H5 the best 10 % of days made 6.2 times the total. This agrees with D4/D7: the ORB direction is close to a coin
flip over 30-60 minutes, so any edge must come from letting the occasional trend day run.

## Step 4: option structures on history (ESTIMATED, not real option prices)

Per lot after costs, source stops, exit 15:29, all on the filtered entries.

| Setup | Structure | Rs / trade | Total Rs | Win % | Max drawdown | Years + (2022-25) | 2026 | Verdict |
|---|---|---|---|---|---|---|---|---|
| A 60 m breakout | Long ATM | +490 | +1,82,947 | 45.8 | 45,435 | 3 of 4 | +12,287 | WEAK (post-hoc: FAIL) |
| | Debit spread | +28 | +10,513 | 47.5 | 47,898 | 2 | -9,471 | FAIL |
| | Credit spread | -25 | -9,213 | 56.8 | 42,145 | 3 | -21,633 | FAIL |
| B 30 m retest | Long ATM | +298 | +1,53,886 | 42.4 | 33,153 | 4 of 4 | +10,531 | FAIL, interval -19 to 630 (post-hoc: WEAK) |
| | Debit spread | -21 | -10,807 | 44.4 | 41,336 | 1 | -12,261 | FAIL |
| | Credit spread | -2 | -901 | 54.1 | 39,356 | 1 | -5,436 | FAIL |
| C 5 m reversal | Long ATM | +325 | +1,34,362 | 36.6 | 37,295 | 4 of 4 | -7,603 | FAIL, interval -51 to 733 |
| | Debit spread | -59 | -24,400 | 38.3 | 38,160 | 1 | -15,133 | FAIL |
| | Credit spread | -44 | -17,987 | 44.1 | 29,412 | 1 | -13,875 | FAIL |

* **Credit vs debit:** differences of -53, +19 and +16 Rs per trade (A, B, C), none distinguishable from zero.
  The source's "credit beat debit in every setup, 2-4x" is **not reproduced**. Long ATM beat both spreads by
  Rs 300-515 per trade in every setup (reliably, in the wrong direction for the spread claims).
* **Why spreads look poor here:** a spread pays 4 orders (about Rs 170-187 per trade vs about Rs 110 for a long
  option) and caps the big trend days that make all of the long option's profit. The estimate has no volatility
  skew and no IV crush, which is where a real credit spread's advantage would come from; real quotes are needed to
  test that (step 6).
* **Long ATM made over Rs 1 lakh on breakout and retest (source):** our estimates agree in direction (A +1.83 lakh,
  B +1.54 lakh over 4.9 years), but these are estimates, A is not robust to the session sensitivity, and none is
  significant after multiple-testing control.
* **Capital per lot (estimated):** long ATM premium median Rs 6.0-6.9k (source Rs 8-10k); debit spread net debit
  median Rs 4.3-4.8k (source Rs 4.5-5.5k); credit spread margin estimate (max loss + 2 % exposure margin; not SPAN,
  no broker margin call) median Rs 37k, 90th percentile Rs 46-48k, max Rs 60k (source Rs 50-60k).
* **Does a 1-lot credit spread fit Rs 1,00,000 with our loss limits?** Capital: yes (90th-percentile margin Rs 48k).
  Risk limits: **no**. Its maximum loss at entry (median Rs 8.5-10.4k, up to Rs 30k) is above our Rs 3,000
  per-trade risk in 85-89 % of trades and above the Rs 5,000 strategy daily loss limit in 72-80 %. With the spot
  stop, realised losses beyond Rs 3,000 happened in 7-51 trades per setup and beyond Rs 5,000 in up to 13 (A).
  And the bot cannot trade it today: the PAPER broker refuses short options and the risk manager sizes single long
  options only. (Not changed; out of scope.)
* **0.2-delta wing vs our recorded strikes (for step 6):** the estimated 0.2-delta strike is a median 4-5 strikes
  from ATM (90th percentile 7-8, max 14). It lies inside our recorded band (ATM +/-5 strikes, nearest expiry) in
  only **64-75 %** of trades, and within +/-3 (the next-expiry band) in 32-47 %. Real put skew pushes the 0.2-delta
  put further out. Step 6 would miss a quarter to a third of spread trades unless the band is widened.

**H8, days to expiry (long ATM; LOW PRIOR, descriptive only, nothing selected):**

| Setup | 0 DTE | 1 DTE | 2 DTE | 3 DTE | 4+ DTE |
|---|---|---|---|---|---|
| A breakout | n 74, +965, 2/4 yrs | n 78, -122, 1/4 | n 84, +667, 3/4 | n 77, +659, 3/4 | n 60, +237, 2/4 |
| B retest | n 107, +1,495, 4/4 | n 122, +332, 2/4 | n 107, -522, 1/4 | n 99, -19, 0/4 | n 81, +139, 2/4 |
| C reversal | n 89, +982, 4/4 | n 89, +398, 2/4 | n 87, -514, 1/4 | n 86, +314, 2/4 | n 62, +472, 2/4 |

(Rs per trade; "yrs" = test years 2022-2025 with a positive mean.) Partly like the source (retest best on expiry
day and the day before; 2 DTE weakest for retest and reversal), partly not (breakout best at 0 and 2-3 DTE, not
3-4; reversal best at 0, not 1 and 3). Expiry-day estimates are the least reliable (VIX is a poor stand-in for
0-DTE IV, and gamma is huge). Buckets hold 60-120 trades each. Only record DTE on every forward trade.

**H9, fixed targets vs holding (exit 15:10):** consistent with the source, targets did not help. Holding beat a
1R target on long ATM for B (+Rs 270 / trade, WEAK, 4 of 4 years) and C (+Rs 401, WEAK), and a 2R / 3R target for C
(WEAK). On credit spreads the differences are small (one WEAK at 4R for C, +Rs 15). For A, targets of 4-5R were
never reached, so they equal holding. No target result passes the Bonferroni rule.

## Hypotheses for FORWARD shadow testing (independent data from 21 Nov 2026)

Sample sizes for 80 % power at the effect seen on history (two-sided 5 %), from the day-block bootstrap spread; a
"first look" (half the power) needs about half as many. Rates assume one setup's natural frequency on history.

| # | Hypothesis (forward) | Effect on history (ESTIMATED) | Trades needed (80 % power) | Rate | Rough time |
|---|---|---|---|---|---|
| F1 | Their exit beats ours on our own ORB entries (paired, same signal, real recorded quotes) | +Rs 302 / trade | ~1,050 | 0.76 / session | ~5.5 years (first look ~2.8) |
| F2 | 30 m retest (filter B), long ATM, is positive after costs | +Rs 254-298 / trade | ~1,050-1,250 | 0.46 / session | ~9-11 years (first look ~4.5-5.5) |
| F3 | 60 m breakout (filter A), long ATM | +Rs 490 / trade | ~710 | 0.34 / session | ~8.5 years |
| F4 | 5 m reversal (filter C), long ATM | +Rs 325 / trade | ~1,220 | 0.37 / session | ~13 years |
| F5 | Holding beats a 1R target (B and C, long ATM) | +Rs 270-400 / trade | ~500-600 | 0.37-0.46 / session | ~5.5 years |
| F6 | Credit spread beats debit spread (source claim) | about 0 (-53 to +19) | thousands | - | not testable in practice; record only |
| F7 | DTE buckets (low prior) | mixed | >= 100 per bucket per setup | - | record only |

Plain words: at these effect sizes, forward shadow testing cannot confirm any of them within a year. F1 is the most
useful because it is paired (same entries) and our ORB already fires most days. A quicker and cheaper check, done
before any of these, is to **compare the option-price ESTIMATE with our real recorded quotes** on each signal (a
calibration, not a hypothesis): if the estimate is off, every rupee figure above moves.

## Risks and limits

* **Overfitting.** The source picked its three filters on the same data, and we ran 68 related tests. The
  pre-registration expected 2-3 chance passes among independent tests; we got 8 WEAK, but they cluster on a few
  trade sets (four are the same reversal trades against different targets). Nothing passes the
  multiple-testing-corrected rule.
* **Overlap with the source's data.** About 95 % of our period is inside theirs, so agreement with the source is
  expected and is not independent confirmation.
* **Estimate quality.** No historical option prices. VIX (30-day) stands in for weekly ATM IV; no skew, no IV
  crush, no expiry-day IV dynamics; D3 found VIX about 1.7x too high vs realised intraday variance (not corrected);
  premium exits are checked on 1-minute bar extremes priced at the bar's close time; the bot's liquidity filters
  cannot be applied. H4 depends on all of this because our stop is on the premium.
* **Costs.** The real cost model is applied (charges, spread, 1 tick slippage per fill). Real spreads on 0.2-delta
  wings and on fast days are often wider than the modelled 0.25 % half-spread, so spread results are optimistic.
* **Liquidity of 0.2-delta strikes.** 4-8 strikes out, sometimes up to 14; often outside our recorded band.
* **Margin.** Our credit-spread margin is a rough estimate (max loss + 2 % exposure), not SPAN; the broker's real
  margin was not queried (research has no margin API). Margin for the short leg needs a live check before any use.
* **Lot size.** 65 for all years (as the source); the real lot was different in earlier years.
* **Sizing.** Everything is 1 lot; the bot sizes by its Rs 3,000 risk per trade, which would change totals and
  would refuse many trades.
* **Session rule.** The registered rule excluded 89 normal sessions (stray bars); the post-hoc sensitivity changes
  no conclusion.
* **Counterfactuals.** Daily loss limits, cooldowns, the kill switch and the 2-open-trades limit are not simulated.

## What was built (research only)

* `research/orb_study/`: `registration.py` (pre-registered design + fingerprint), `data.py` (sessions, data
  check), `setups.py` (source setups, our ORB entries via the parity-tested `research/regime/orb.py`, spot exits),
  `options.py` (ESTIMATED pricing, structures, our premium exit via `research/shadow/rules.py`), `stats.py`
  (metrics, day-block bootstrap, pass rule, Bonferroni), `study.py` (H1-H9).
* `python -m research orb-study [--json] [--posthoc-ignore-stray-bars]`; the cost model comes from
  `backtest/cost_model.py` with the configured charges, built in the research composition root.
* Tests: `tests/unit/test_orb_study_registered.py`, `tests/unit/test_orb_study.py`; the existing isolation tests
  scan the new package.

Next step (step 6, forward shadow of spreads on real recorded quotes) waits for the owner's approval.
