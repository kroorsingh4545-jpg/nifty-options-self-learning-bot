# Forward shadow variants A and B: pre-registration (sv.v1)

Registered: **2026-11-21**, in its own commit, BEFORE the variants were implemented or run on any recorded session.
Rules: `research/shadow/variant_rules.py` `VARIANTS_V1` (version `sv.v1`).
SHA-256 fingerprint: **`301fdc1c208c6afa69e19a21b9f99b2f518bab993789a79ec8bd9b0c7a520a70`**
(`tests/unit/test_shadow_variants_registered.py` fails if the rules change without a new version).

## Scope

Research and shadow only, on the bot's REAL recorded option quotes. The variants never place, block or size an
order and never change a strategy, a risk limit or the configuration; the PAPER bot is unchanged. This replaces
ORB study step 6 (spreads), which the owner skipped. Origin: ORB study E1 (`docs/orb-e1-findings.md`), where both
were only ESTIMATED on history and both were WEAK at best.

## Common rules

* 1 lot. Quotes: the bot's recorded 5-second option quotes archived in `research.db` (`archive-quotes`). NIFTY
  1-minute bars: the bot's own candles (trading database, read-only), else the research store's Upstox candles.
* Contract, entry and trade-rule values exactly as the D2 shadow uses for `orb_options` (bot configuration,
  read-only): ATM of the nearest expiry; decision quote at most 5 s old (else the first within 30 s, flagged); LIMIT
  ask + 2 ticks, filled at ask + 1 tick while the ask depth covers the order, within 30 s, else NO_FILL.
* A 1-minute close is acted on **2 s after its bar ends** (the bot's candle flush grace).
* Exit fill: the first recorded quote at or after the trigger; sell at the bid (last price if no bid; for a stop the
  lower of bid and last) minus 1 tick; wait while bid depth is below the order. Square-off: first quote at or after
  15:10. No quote left: END_OF_DATA, flagged.
* Costs: the bot's charge calculator round trip (as D2); spread and slippage are in the fill prices.
* Not simulated (each trade stands alone): loss limits, cooldowns, kill switch, max open trades, RL manager;
  liquidity-filter refusals are recorded as flags, as in D2.

## Variant A: our ORB entries with the source exit (`A_ORB_SOURCE_EXIT`)

* Signals: every `orb_options` signal in the D2 shadow, with the same contract and entry as its D2 row.
* Opening range: NIFTY 1-minute bars 09:15-09:29 (all minutes present, else NO_RANGE).
* Stop: the first 1-minute close (bar ending at or after the entry fill, before 15:10) beyond the OPPOSITE side of
  the range (CE: below the low; PE: above the high). No target, no trailing, no premium stop. Else square-off 15:10.
* Compared per signal with the current ORB's D2 outcome: **A net minus current-ORB net**.

## Variant B: 30-minute retest, long ATM (`B_RETEST30_LONG_ATM`)

* Signals: the E1 filter `B_RETEST_30` (`research/orb_study/setups.py`, design e1.v1) on each session's NIFTY
  1-minute bars: 30-minute range from 09:15, width <= 0.5 % of the 09:15 open, retest within 60 minutes of the first
  break, one a day, no entry on or after the 15:09 bar.
* Long ATM CE (up retest) or PE (down). Decision at the retest bar's end + 2 s, ATM from its close.
* Stop: the first later 1-minute close beyond the opposite side of the 30-minute range. No target, no trailing.
  Else square-off 15:10 (the bot cannot hold to the source's 15:29).

## Evaluation (forward only)

* Counted: FILLED trades on sessions on or after **2026-11-23** (A: only signals whose A and D2 rows are both
  FILLED). Earlier sessions are shown, marked *before registration*, and never count.
* Primary metrics: A = mean paired difference A - current ORB; B = mean net per trade (rupees per lot after charges).
* Pass: the day-block bootstrap **97.5 %** interval (Bonferroni for 2 primary tests; 10,000 draws, seed 20261123)
  lies above zero AND the mean is positive in both halves of the counted sample (split at the median session).
* First look: A >= 525 trades, B >= 625; final: A >= 1,050, B >= 1,250 (80 % power at the E1 estimated effects;
  roughly 3 / 5.5 years for A and 5.5 / 11 years for B at their natural rates). Below the first look the report says
  *not yet evaluable* and shows progress only.
* No retuning. Any change is a new version (sv.v2) with its own registration and a fresh evaluation.

## Reporting

Every run of `python -m research variants` (step 7 of `scripts/daily-research.bat`, after `shadow`) shows, per
session: the current ORB (D2), A, A - ORB and B (trades, net), running totals since 2026-11-23, coverage (signals
without a range, quotes or fill) and the evaluation status.
