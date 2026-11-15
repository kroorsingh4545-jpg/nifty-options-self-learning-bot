# Intelligent decision engine: plan and status

Owner decisions: Upstox only (read-only Analytics token, no Plus), research before code, engines produce
evidence only, the existing `RiskManager` keeps final authority, no machine learning until enough reliable
history exists, LIVE never enabled. Every phase is on its own branch with tests and `/audit-risk`, and existing
tests are never weakened.

Roadmap approved by the owner on 2026-11-15 (order: data, shadow harness, volatility, momentum + regime, thrust +
PairFlow, CrossFlow (optional), decision engine in shadow, learning from history):

| Phase | Goal | Status |
|---|---|---|
| D1 | Data audit and recorder: stop losing data, measure its quality | **merged** (PR #19); exit = 10 clean sessions |
| D2 | Shadow harness: counterfactual P&L of every recorded signal from recorded quotes | **built** (branch `decision-d2-shadow-harness`) |
| D3 | Volatility evidence (theta and costs vs expected move, IV-RV on matched horizons, position Greeks) | not started |
| D4 | Regime (R3) and Momentum Pulse, validated on 2022-2026 NIFTY history | not started |
| D5 | Thrust and PairFlow, shadow-recorded (needs at least 40 recorded sessions) | not started |
| D6 | CrossFlow, only if the D1 audit shows the data can support it (optional) | not started |
| D7 | Central decision engine in shadow (pre-registered; at least about 440 signals before any verdict) | not started |
| D8 | Learning from history (only after the sample thresholds) | not started |

Promotion beyond shadow needs the owner's approval and `/audit-risk`, and could only ever act through the
existing `SignalFilter` slot (veto or reduce size, never add risk), upstream of `RiskManager`.

## D1: what changed

Owner approvals (2026-11-15): bot recording-path changes with never-raise / never-block tests and `/audit-risk`;
strike band 3 -> 5; next expiry ATM +/- 3 if load and database size are fine; fix the `.gitignore` reports issue.

* **Recording (bot, write-only, trading never reads it):** schema v7. `option_quotes` gains Greeks (delta, gamma,
  theta, vega), resting total buy/sell quantity (`tbq`/`tsq`, displayed liquidity, not executed flow), last traded
  quantity and average traded price. New `market_quotes` (NIFTY index, current future, India VIX, 5 s) and
  `signals` (every strategy signal with its entry decision; append-only).
* **Subscriptions:** nearest expiry ATM +/- 5 (was 3); the next weekly expiry ATM +/- 3 and India VIX are
  subscribed for recording only. They are never in the strike universe the resolver trades from and never in the
  entry gates (`base_keys`). A key leaves the feed only when no universe, held position or base key needs it.
* **Never raise / never block:** listeners only append to memory, every call is guarded; the timer job writes at
  most `max_rows_per_flush` (5,000) rows per run; one failing table does not stop the others; full buffers drop
  and count rows.
* **Research:** `archive-quotes`, `data-quality`, `chain-schedule` (see `docs/research.md`), research store v3.
* **`.gitignore`:** `reports/` now only matches the project root, so `research/reports/` (imported by the research
  CLI and its tests) is tracked.

Load with the new settings: 39 subscriptions (Upstox full-mode limit 2,000). The feed sends the same ~10
messages a second whatever the subscription count. Estimated about 170,000 quote rows a day: about 52 MB a day in
`trading.db` (about 310 bytes a row, measured 237 before the new columns), about 1.2 GB at the 30-day retention;
the permanent research archive grows about 18 MB a day (measured 104 bytes a row), about 4.5 GB a year. There were
77 GB free on the data drive on 2026-11-15.

**D1 exit criteria:** 10 consecutive CLEAN sessions in `python -m research data-quality`, every recorded day
archived before its purge, Greeks confirmed non-zero from the live feed, and `/audit-risk` PASS (or PASS WITH
WARNINGS accepted by the owner).
