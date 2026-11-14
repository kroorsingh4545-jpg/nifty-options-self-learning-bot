# Research plan: volatility intelligence, forecast-vs-reality learning, Define Edge methodology

> Owner's brief, saved verbatim from `research-phase0.txt` (only headings were turned into Markdown).
> Phase 0 (read-only audit) and R1 (data foundation, `docs/research.md`) are done; later phases follow
> the order agreed in Phase 0: R2 forecast-vs-reality + volatility core, R3 regime, R4 Define Edge notes,
> R5 strategy evaluation, R6 backtesting bridge, R7 learning + dashboard.

**RESEARCH PROJECT: VOLATILITY INTELLIGENCE, FORECAST-VS-REALITY LEARNING, AND DEFINE EDGE METHODOLOGY RESEARCH**
Owner decision: run this inside THIS project (nifty-options-self-learning-bot) as an isolated RESEARCH layer.

## DO PHASE 0 ONLY IN THIS SESSION

Phase 0 is READ-ONLY. Do not create, edit, delete, commit or push any project file in this session.
The only output is a written report shown to me (you may save it in your scratchpad, not in the repo).
Never read .env or print any token. The PAPER bot may be running: do not stop it, restart it, or touch data/trading.db except read-only.
Stop after the report and wait for my approval.

## SAFETY RULES FOR THE WHOLE PROJECT (all phases)

- Research, simulation and PAPER only. Never enable LIVE, never place real orders.
- The research layer must be ISOLATED: new packages only (for example research/ and volatility/), on their own branches.
- Existing strategies, risk limits, hard limits, config files, the execution layer and the existing tests must NOT be changed by this research.
- Research code may READ market data and the database, but must never call order methods, never change strategy parameters, never change risk limits.
- Add an architecture test that enforces this isolation (research packages cannot import broker order code or write to strategy/risk config).
- Any research finding that suggests changing a strategy becomes a PROPOSAL only. It must pass backtest, walk-forward, out-of-sample and /audit-risk, and needs my explicit approval. The learning loop must never apply it automatically.
- No git reset, git clean, forced checkout, or deleting files. Every change must be on a branch, tested, audited, and reversible.
- Do not weaken or disable existing tests.

## PHASE 0 TASKS (read-only audit)

1. Record the current branch, latest commit, and git status (modified and untracked files).
2. Describe the existing architecture relevant to this research: market-data pipeline, candle builder, option chain, instruments, broker gateway (read-only analytics token), backtester (backtest/), ML feature store and regime classifier (ml/), learning loop (learning/), risk layer, dashboard, database schema.
3. List every existing piece of IV, VIX, volatility, Greeks, OI, PCR, regime and option-chain functionality, with file paths, and say what can be reused or extended instead of duplicated.
4. DATA AVAILABILITY CHECK (most important). For each item, say whether we can get it with our current Upstox Analytics token (read-only, no static IP), how far back, at what resolution, and how sure you are. Check the official Upstox docs; do not guess:
   a. NIFTY index historical candles (daily and intraday).
   b. NIFTY futures historical candles.
   c. Historical option prices for EXPIRED NIFTY options (the docs show "Expired Instruments" marked Plus).
   d. Current option chain with IV and Greeks.
   e. India VIX current and historical.
   f. Our own saved data: candles and option_quotes recorded by our PAPER sessions since 9 Nov.
   Then say what can be computed WITHOUT paid data (for example: realised volatility from index candles; expected move from India VIX; forecast-vs-reality using VIX-implied move vs actual move), and what truly needs historical option prices.
5. Check whether YouTube video metadata and transcripts for https://www.youtube.com/@Definedge are reachable from this environment (WebSearch/WebFetch). Report honestly what works and what does not. Do not download full transcripts.
6. Margin reality check: with Rs 1,00,000 PAPER capital per strategy, which strategy families are realistic (defined-risk spreads, iron condors, debit spreads, long options) and which are not (naked short options, short straddles/strangles need far more margin). Say this plainly.
7. Produce an ARCHITECTURE REPORT and a PHASED IMPLEMENTATION PLAN using the phases below, with file list, data flow, risk-control checklist and test plan (as the nifty-bot-architect Skill requires). Order the phases so the most valuable, least data-hungry work comes first. My suggested order: Forecast-vs-Reality module and Volatility Intelligence on data we can get, then regime engine, then Define Edge research notes, then strategy evaluation, then backtesting integration.

## THE FULL PROJECT (plan for it in Phase 0, do NOT build yet)

### A. FORECAST-VS-REALITY LEARNING MODULE (highest priority)

Every session (and for every historical day where data exists), record BEFORE the session: expected move (from ATM IV or India VIX), IV, forecast realised volatility, regime. AFTER the session record: actual move (high-low and close-close), actual realised volatility.
Over hundreds of historical days, study: when IV over- or under-estimates actual movement; whether this changes near expiry and on expiry days (note: holiday-shifted expiries); which regimes give the largest forecast errors; which strategy types are exposed to unexpected volatility expansion.
The system must learn from the QUALITY OF ITS PREDICTIONS, not only from profit and loss. A profitable trade does not prove the prediction was right; a losing trade does not prove the idea was wrong.

### B. VOLATILITY INTELLIGENCE ENGINE

"A payoff graph shows possibilities. IV and RV decide probabilities."
- IV: ATM IV, call/put IV, strike-wise and expiry-wise IV, IV rank, IV percentile, IV changes, skew, term structure, historical IV distribution (where data allows).
- RV: 5, 10, 20, 30, 60-session RV, annualised, intraday RV where data exists. Proper log returns, consistent annualisation, horizons matched to the IV being compared.
- IV-RV: spread, ratio, rolling spread, its historical distribution, premium by expiry and by regime. Do not assume past RV predicts future RV; build forward RV estimates.
- Expected move = Spot x IV x sqrt(time to expiry). Document conventions and limits. Give 1 and 2 standard-deviation ranges, expected vs actual, calibration statistics. An expected-move range is not a guaranteed probability.
- Greeks: delta, gamma, theta, vega (rho if relevant), at POSITION level across all legs.
- Report: regime, ATM IV, RV, IV-RV spread, IV rank/percentile, expected move, skew, term structure, main volatility risks, data-quality warnings.

### C. MARKET REGIME ENGINE

Regimes: low/high volatility range-bound, low/high volatility trending, volatility expansion, volatility contraction, event-driven, unstable/abnormal. Inputs may include IV, RV, IV-RV, India VIX, trend, ATR, ADX, realised range, gaps, skew, OI/volume, PCR where reliable, expiry proximity. Thresholds configurable and validated on history, never arbitrary. Every classification gives a confidence score and the reasons. Extend the existing ml/regime_classifier rather than duplicating it.

### D. DEFINE EDGE / RAGHUNATH REDDY RESEARCH

Source: https://www.youtube.com/@Definedge
Do not look only at videos titled "options trading". Also study his discussions of volatility, probability, market behaviour, risk, derivatives pricing and trading psychology.
Topics: options pricing, payoff diagrams, probability of profit, IV, RV, IV vs RV, volatility risk premium, expected move, buying vs selling, Greeks, theta, gamma risk, vega, skew, expiry and strike selection, spreads, iron condors, calendars, diagonals, straddles, strangles, directional and non-directional strategies, entry, management, stop losses, profit booking, adjustments, risk management.
For every relevant video record: title, URL, date, relevant timestamps, the concept explained, any explicit numbers, examples and market conditions, context, and whether it is general education or an explicit rule.
SPEAKER ATTRIBUTION: record only statements actually made by Raghunath Reddy. Mark statements by other Define Edge presenters separately. Never invent a rule and attribute it to him.
Each extracted rule must have: source video, URL, timestamp, short quote or faithful paraphrase, interpretation, conditions, numeric threshold only if explicitly stated, confidence, and class:
  A = explicitly stated by Raghunath; B = repeated principle across sources; C = interpretation; D = general options knowledge; E = unverified hypothesis.
If sources disagree, keep both with context. Store notes and short quotes only, never full transcripts. If transcripts cannot be retrieved, say so plainly.
Suggested storage: research/definedge/ with source_catalog.json, concepts.json, methodology_rules.json, strategy_examples.json, unresolved_questions.json, research_summary.md.

### E. STRATEGY EVALUATION ENGINE

Families: long calls/puts, short calls/puts, long/short straddles and strangles, iron condors, iron butterflies, bull/bear spreads, calendars, diagonals, debit and credit spreads. Not every strategy suits every regime.
For each: max profit, max loss, breakevens, net premium, net delta/gamma/theta/vega, margin where data exists, costs, payoff at expiry, mark-to-market sensitivity, expected move vs breakeven, scenario P&L.
Never judge by payoff diagram alone; combine with volatility, time decay, Greeks, regime and scenarios. Probability of profit is not expected value.
Entry and exit rules: only verified rules are rules; anything not explicitly stated is a configurable, clearly marked UNVERIFIED hypothesis. Never invent his stop-loss or target percentages. Monitor the whole position, all legs.

### F. BACKTESTING

Use the EXISTING backtest/ framework; extend it, do not build a second one. Real NIFTY data where available, correct expiries and strikes (including holiday-shifted expiries), bid-ask, Indian costs, slippage, liquidity, no look-ahead, no survivorship bias, no future volatility information, walk-forward, out-of-sample, parameter sensitivity. Report net P&L, drawdown, profit factor, expectancy, win/loss distribution, averages, tail losses, by regime, by expiry, on event days, IV-RV forecast accuracy, probability calibration. Compare against the existing strategies. Never choose a strategy for highest historical return alone.

### G. LEARNING ENGINE (extend the existing learning/ Reflective Loop)

Before each session: read prior research and trades, latest data, current regime, compare previous forecasts with outcomes, find recurring forecast errors, performance by regime, hypotheses to test.
After each simulated trade record: entry conditions, regime, IV, RV estimate, IV-RV spread, expected move, strategy and strikes, entry/exit, Greeks, exit reason, costs, P&L, max favourable and adverse excursion, forecast vs actual movement, whether the hypothesis was right.
It may generate hypotheses. It must never rewrite production strategies or risk limits.

### H. RISK

Keep the existing risk system. Extend only where needed: simulated exposure, daily and portfolio loss, open positions, per-strategy exposure, Greeks exposure, gap scenarios, expiry risk, liquidity and data-quality checks, circuit breakers, session-end handling. Reject evaluations when data is stale, missing or inconsistent. Nothing bypasses existing risk controls.

### I. DASHBOARD

Add a separate RESEARCH page or tab: regime, ATM IV, RV, IV-RV, IV rank/percentile, expected move, expansion/contraction, term structure, option-chain view, strategy evaluations with the reasons WHY, position Greeks, backtest results, forecast accuracy, learning observations, pending proposals.

### END GOAL. Before every simulated trade the system must be able to explain:

1. What is the current market regime?
2. What movement is the options market pricing?
3. What movement is actually being realised?
4. Is volatility relatively expensive or cheap?
5. Which strategy structures deserve further evaluation?
6. What are the possible profit and loss scenarios?
7. What would invalidate the hypothesis?
8. What did similar past market conditions teach the system?

Principles: research before coding; verify before attributing; quantify before assuming; backtest before accepting; validate out of sample; preserve existing working functionality; never overwrite an approved strategy; never let AI hypotheses bypass risk controls; never activate live trading.

REMINDER: this session is PHASE 0 ONLY, read-only. Present the report and stop.
