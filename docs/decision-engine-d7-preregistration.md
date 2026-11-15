# D7 shadow decision engine: pre-registration (rules v1)

Registered: **2026-11-15**, in its own commit, BEFORE the engine was implemented or run on any signal.
Rules: `research/decision/rules.py` `RULES_V1` (version `d7.v1`).
SHA-256 fingerprint of the rules: **`4632769f8b6b3992264f92c519cdd809ab74adba03f228887a2dacdec871c767`**
(`tests/unit/test_decision_rules_registered.py` fails if the rules change without a new version).

## Scope

Shadow only. The engine reads shadow evidence and writes its verdicts to the research database. It never places,
blocks, sizes or changes an order, a strategy, a risk limit or a configuration value; the existing `RiskManager`
keeps final authority. Promotion beyond shadow requires the owner's approval and `/audit-risk`.

## Evidence used (only what exists now)

| Source | What | Status |
|---|---|---|
| D3 volatility evidence | delta, gamma, business-time decay over the hold, costs per unit (charges, spread, slippage), business-clock expected move | analytical (built, not a tested edge) |
| D4 kept hypothesis `vol_change_iv_bias` | realised / VIX-implied variance depends on the VIX-change state (EXPANSION > CONTRACTION, 4 of 4 years) | the one D4 hypothesis KEPT |
| D5 Thrust, PairFlow | slot | not built: gives no evidence, never changes a verdict in v1 |
| D6 CrossFlow | slot | not built: gives no evidence, never changes a verdict in v1 |

Not used: Momentum Pulse, ORB confirmation, the bot's trailing-trend rule, VIX level (all DROPPED in D4).

## Rule

For a long option held `h` = 30 minutes, with the underlying's log move `m` (mean 0, variance `v`) and a direction
that is right with probability `p`, the expected P&L per option unit is

    E = |delta| x S x sqrt(2 v / pi) x (2p - 1) + gamma x S^2 x v / 2 - decay - costs

so the **breakeven hit rate** of the signal's direction is

    p* = 1/2 + (decay + costs - gamma S^2 v / 2) / (2 |delta| S sqrt(2 v / pi))

* `v` = `bias` x `v_implied`. `v_implied` is the business-clock implied variance of the 30 minutes from the
  option's own IV (D3 `expected_move_business`, squared, over S squared). `bias` is the realised / VIX-implied variance
  ratio of 30-minute windows in the SAME VIX-change state (EXPANSION / STABLE / CONTRACTION) over the 252 sessions
  before the signal day (D4 history samples; exp of the mean log ratio). With fewer than 100 windows in that state,
  all states are pooled; with fewer than 100 in total the verdict is INSUFFICIENT_EVIDENCE.
* **TRADE** when `p* <= 0.55`; **NO_TRADE** when `p* > 0.55`; **INSUFFICIENT_EVIDENCE** when any required input
  (spot, delta, gamma, decay, costs, expected move, VIX-change state) is missing.
* Confidence = `min(1, |p* - 0.55| / 0.10)`. Every verdict records `p*`, every input and the reason.

Why 0.55: D4 found no stable directional evidence (Pulse, ORB confirmation and trailing-trend rules all dropped),
so a signal that must be right more than 55 % of the time over 30 minutes just to cover time, costs and the
variance premium is not supported by any evidence we have. The threshold was chosen on that ground, before any
engine result existed.

## Evaluation plan (forward only)

* Counted: every shadow signal generated on or after **2026-11-16** with a TRADE or NO_TRADE verdict. Signals
  before that date are shown, marked *before registration*, and never count.
* Outcome: the D2 counterfactual net R of each signal (1 lot, after charges).
* Primary metric: mean net R of TRADE minus mean net R of NO_TRADE. It passes when its day-block bootstrap 95 %
  interval lies above zero AND the total net of TRADE signals is at least the total net of all signals.
* First look at >= 100 signals per group and >= 60 sessions; final verdict at >= 175 per group and >= 60 sessions.
  Below that the report says *not yet evaluable*.
* No retuning. Any change to the rules is a new version (v2) with its own registration and a fresh evaluation.
