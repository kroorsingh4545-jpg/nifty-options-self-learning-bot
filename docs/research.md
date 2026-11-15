# Research layer (R1: isolation and data foundation)

An isolated research layer for volatility intelligence and forecast-vs-reality learning. It **reads** market data
and the trading database. It never trades, never changes a strategy, a risk limit or a file under `config/`, and
the trading bot never imports it. The owner's brief and full phase list are in
[`docs/research-plan.md`](research-plan.md).

## Isolation (enforced by `tests/unit/test_research_isolation.py`)

| Rule | How it is enforced |
|---|---|
| No order / account calls | No `execution`, `risk`, `runtime`, `dashboard`, paper broker, broker factory or WebSocket imports. No `.place_order(` / `.get_funds(`-style calls in the text. Research code holds a `ReadOnlySource` with exactly four read methods. |
| Network only through the Upstox adapter | No HTTP or socket client imports. Only `research/__main__.py` (the composition root) builds `UpstoxRestClient`, with its own limiter at `rate_share` (at most 10 %) of the bot's already-buffered limits. Bot + research together stay at or below **90 % of Upstox's official limits** even if both saturate (88 % on the standard API; tested). |
| No competition with a PAPER session | **Every** command that calls Upstox is refused on weekdays 08:30–15:40 IST. Only `contracts` and `chain-snapshot` (a few calls each) may opt in with `--during-session`. `history` and `smoke` never can. |
| Trading DB is read-only | Opened only in `research/trading_readonly.py` with `?mode=ro` and `PRAGMA query_only`. `ResearchStore.open` refuses the trading DB path. No `data.storage` imports. |
| No config / file writes | No reference to the bot's YAML files or `config.hard_limits`. Only the composition root reads the bot config. No `open()`, `write_text`, `unlink` or `shutil`. `os.environ` is not read. |
| Bot never depends on research | No bot package imports `research` or `volatility`. |

## Commands

Run from the project root with the project venv (`.venv/Scripts/python.exe -m research <command>`).

| Command | Upstox calls | What it does |
|---|---|---|
| `init` | none | Creates or migrates `data/research.db` (gitignored). |
| `status` | none | Row counts, fetch-log summary, observations. |
| `summarize-quotes [--day D]` | none | Copies the bot's recorded `option_quotes` into 1-minute bars **before the 30-day purge**. Idempotent. The latest day is always redone. |
| `calendar [--year Y]` | none | Session calendar derived from NIFTY daily candles (holidays, special sessions, mismatches against `settings.yaml` holidays) and the weekly expiries, each marked `observed` or `rule` and moved if the rule day was a holiday. |
| `smoke` | about 7 | VIX daily (30 days), first available VIX date, VIX 1-minute (Jan 2022), NIFTY daily, and the option-chain IV unit (raw IV against Black-Scholes IV solved from the same chain). Records `chain_iv_unit`. |
| `contracts [--during-session]` | 1 | Saves every current NIFTY option contract (key → strike / expiry / type / lot) so recorded quotes stay interpretable after expiry. |
| `chain-snapshot [--during-session]` | 1 + up to 4 | Option-chain snapshots of the nearest 3 expiries plus the next monthly, with raw IV (percent), Greeks, OI and per-strike PCR. |
| `history [--what all\|daily\|minutes] [--keys nifty,vix] [--to D]` | about 6 daily + about 60 per key for minutes | NIFTY index and India VIX: daily from 2000 (5-year requests), 1-minute from Jan 2022 (one month per request). Resumable: ranges fetched after they ended are skipped. Auth, scope and rate-limit errors stop the run. Other API errors are logged per range. **An invalid candle from Upstox** (OHLC inconsistent) is never kept or repaired: the range is halved until the bad day is isolated, that day is skipped and named in the fetch log, and every valid candle is kept (at most 64 extra calls per range). |

All network commands use a 120 s queue timeout for research's small budget (`queue_timeout_seconds`); the bot
keeps its own 30 s.

## Data collected (first run, 2026-11-14)

| Series | Range | Rows | Notes |
|---|---|---|---|
| NIFTY index daily | 2000-01-03 .. 2026-11-13 | 6,681 | |
| NIFTY index 1-minute | 2022-01-03 .. 2026-11-13 | 450,737 | |
| India VIX daily | **2009-03-02** .. 2026-11-13 | 4,374 | Upstox has no VIX before 2 Mar 2009. **2013-08-22 skipped**: invalid candle from Upstox. |
| India VIX 1-minute | 2022-01-03 .. 2026-11-13 | 450,687 | |
| NIFTY option contracts | expiries 2026-11-17 .. | 2,242 | All 28 contracts in the recorded quotes are mapped. Lot 65. |
| Recorded quote bars | 2026-11-12 .. 13 | 10,428 | From `trading.db` (read-only). |

Calendar check: holidays observed from the 2026 candles match the 14 holidays in the bot's config exactly. The
contract list confirms the rule-based Monday 23 Nov 2026 expiry (Tuesday 24 Nov is a holiday). The 31 special
sessions since 2000 include the 1 Feb 2026 Budget Sunday and the 8 Nov 2026 Muhurat session.

## D1 additions (decision-engine data foundation, 2026-11-15)

| Command | Upstox calls | What it does |
|---|---|---|
| `archive-quotes [--day D]` | none | Copies the bot's **raw** `option_quotes` and `market_quotes` rows (every 5-second sample, with Greeks, resting total buy/sell quantity, last traded quantity, average traded price) into `quote_archive` before the bot's 30-day purge. Rows are grouped by the day they were **received**. Idempotent; the latest day is always redone. About 105 bytes a row. |
| `data-quality [--day D] [--json] [--expected-contracts N]` | none | Audits each recorded session (thresholds in `research.yaml` `data_quality`) and stores the JSON in `reports` (`data_quality`). Prints CLEAN or the problems, and how many clean sessions in a row (D1 exit: 10). |
| `chain-schedule --during-session` | about 5 per scheduled time | Takes `chain-snapshot` sets at the `chain.schedule` times today (09:20, 11:00, 13:30, 15:20), then exits. One of the three commands allowed during the session; at most 8 times a day (tested). |

### Data quality

`research/audit/data_quality.py` (pure functions; the CLI passes the rows in). A session is **CLEAN** when:

* the **option window** holds a sample for at least 99 % of (contract, 5-second slot) pairs, against the number of
  contracts the bot keeps subscribed (nearest expiry ATM +/- `strike_band`, next expiry ATM +/- `next_expiry_band`,
  CE and PE: 22 + 14 = 36 with the current config). A contract leaving the window as NIFTY moves is not a gap; the
  window running short is;
* the NIFTY index, the future and India VIX each have a sample in at least 99 % of the session's 5-second slots,
  no silence longer than 30 s, and the index is recorded within 2 minutes of the open and of the close;
* IV, Greeks, resting total buy/sell quantity and bid/ask are missing in at most 1 % of option rows;
* no crossed quote, at most 1 % abnormal spreads (over 5 % of mid);
* the median NIFTY receipt minus exchange time is within 2 s (PC clock offset plus latency).

Reported but never failed: how often the last price lies outside the same message's bid/ask (about 50 % on the
two days recorded before D1; a property of the Upstox snapshot feed), stale trades and duplicates.

Sessions recorded before D1 cannot be clean: they have no Greeks, no resting quantity and no market quotes.

## D2: shadow mode (counterfactual outcomes, 2026-11-15)

`python -m research shadow [--day D] [--json] [--no-log]` (no Upstox call; trading database and bot log read-only)
simulates **every recorded strategy signal** as if it had been traded alone, one lot, with the bot's own rules, on
the archived quotes (`archive-quotes` first), and stores one row per signal and simulator version in
`shadow_outcomes` (research store v4); the JSON report goes to `reports` (`shadow`).

| Part | What it does |
|---|---|
| `research/shadow/signals.py` | Signals from the bot's `signals` table (v7). Older sessions: rebuilt from three log lines per signal (decision line, risk line, `signal processed`). Signals that cannot be rebuilt are listed. |
| `research/shadow/rules.py` | Stop, target and trailing rules restated as pure functions (research may not import `risk/`). `tests/unit/test_shadow_rules_parity.py` runs them against the bot's own code. |
| `research/shadow/simulate.py` | Contract as the resolver picks it (nearest expiry, ATM by NIFTY spot); decision quote and stop from the ask; entry LIMIT ask + 2 ticks, fill ask + 1 tick with depth, 30 s timeout; trailing then exit check on every later quote; stops at the worse of bid and last, targets at the bid, minus 1 tick; square-off from 15:10. |
| `research/shadow/calibrate.py` | Each approved signal against the real PAPER trade; every signal against the risk manager's log line. |

**Sampling band.** The bot reacts to every tick but quotes are recorded every 5 s (1 s while held). A stop or
target is usually crossed between two recorded quotes, so the real fill lies in a band: the stored outcome is the
conservative end (the recorded quote for a stop, the target level for a target) and `exit_price_best` the other.

**Calibration** (D2 exit criterion): a real trade is reproduced when the contract and exit reason match, the entry
is within 1 tick and the real exit within 2 ticks of the band. It is judged on a replay from the ask the risk
manager logged, which removes the decision-quote sampling error; the point-in-time differences are reported too.
First run on the real data (2026-11-15, scratch copies): **both real trades with recorded quotes reproduced**
(replay entry exact, exits inside the band); point-in-time sampling error median 2.5 ticks at entry, 7 at exit;
the 2026-11-09 trade cannot be simulated (no quotes recorded then).

**What the outcomes are not:** evidence. Each signal is traded alone (no daily loss limit, cooldown or kill
switch), one lot, with today's YAML rules. Every figure is printed with its sample size, and groups under 30 filled
trades are marked *too few to judge*. No engine uses these numbers yet (D7 does, in shadow, pre-registered).

## D3: volatility evidence (2026-11-15)

Pure maths in `volatility/` (no I/O; `tests/unit/test_volatility_purity.py`):

| Module | Contents |
|---|---|
| `volatility/options.py` | Black-Scholes(-Merton) in calendar and total-variance form, Greeks with explicit units, IV solver, IV unit handling (`as_decimal_iv`, `infer_iv_unit`), put-call parity residual and implied forward. Checked against finite differences and the bot's `options/pricing.py`. |
| `volatility/business_time.py` | Three clocks for the implied variance of an intraday window: `calendar` (every minute of the year), `trading` (252 x 375 minutes) and `business` (each session's variance split between its overnight gap, share `w`, and its 375 trading minutes; weekends weighted). Realised window variance, overnight share. |
| `volatility/position.py` | Whole-position Greeks in rupees, hold decay in business time, breakeven move, P&L attribution. |

**Units** (stated once, tested in `tests/unit/test_volatility_*.py`): IV decimal, annualised on calendar time (the
REST chain's percent is converted; the feed is decimal: confirmed on all 10 recorded signals). Greeks per option
unit: delta per index point, gamma per point per point, vega per volatility point (0.01), theta per calendar day.
Window variances are per window, never annualised, so implied and realised compare on the same minutes.

**`python -m research shadow`** now also stores **`shadow_evidence`** (store v5) for every outcome: IV at entry
(feed and solved), Greeks and 1-lot position Greeks, decay over a 30-minute hold in business time vs calendar
theta, costs per unit, the breakeven move vs the 1-sd move under each clock, and, for filled outcomes, realised vs
implied variance over the hold, the IV change and a delta / gamma / vega / decay attribution.

**`python -m research intraday-vol [--json]`** (offline) tests the clocks on the stored 1-minute history: at 10:00,
11:30, 13:00 and 14:00 the India VIX close gives the implied variance of the next 15 / 30 / 60 minutes; the outcome
is the realised variance of those minutes; the overnight share is measured point in time on earlier sessions.

### First findings (real data, scratch copies, 2026-11-15)

History, 14,396 windows on 1,201 sessions (2022-01-03 .. 2026-11-13), median overnight share 0.40:

| 30-minute windows | realised / implied variance (95 % CI) | inside 1 implied sd (normal 68.3 %) | QLIKE |
|---|---|---|---|
| calendar clock | 1.96 (1.83-2.12) | 62.4 % | -11.70 |
| trading clock | 0.35 (0.33-0.38) | 92.1 % | -11.56 |
| business clock | **0.58** (0.55-0.63) | 85.1 % | **-11.84** (best) |

* **The calendar clock understates an intraday hold's variance about 2x; the trading clock overstates it about
  3x.** Business time is the best of the three, but VIX still over-prices midday windows by about 1.7x (consistent
  with the R2 variance premium and the intraday lull: 0.73 at 10:00, 0.45 at 11:30, 0.51 at 13:00, 0.63 at 14:00).
  The same holds for 15 and 60 minutes and in every year (0.52-0.69).
* So a "1-sd move" from VIX or option IV is too large for a midday hold. Any rule built on expected moves must use
  business time and allow for this bias; it is NOT corrected here (a trailing correction would be a fitted
  parameter: a later, validated step).

Per signal (10 signals with recorded quotes, all ATM weeklies 4-5 days to expiry):

* Decay over a 30-minute hold in business time is about 2.5x calendar theta (0.91 vs 0.36 rupees per unit), but
  time and costs together need only about 5 NIFTY points: **0.11 of a 1-sd move**. These trades were lost on
  direction, not on theta or costs.
* Realised / implied variance over the actual holds: median 0.65, in line with the history study.
* IV fell over most holds (median -0.49 points); on 2026-11-13 the ORB calls lost about a third of their premium
  drop to vega (IV -1.3 points). Attribution residuals are 10-30 % of the move on long holds (second-order effects
  around entry Greeks).
* With 10 signals, none of this is evidence of an edge or a filter: every group is marked *too few to judge*.

## Data caveats

* **Expired option prices need Upstox Plus** (about 6 months, OHLC + OI, no bid/ask, no IV). Not used.
  Option history is collected forward (`chain-snapshot`, `summarize-quotes`).
* **The REST option-chain IV is in percent** (smoke, 2026-11-14: median raw / Black-Scholes ratio 87.7 over 10
  near-ATM quotes, measured on a Saturday against Friday's prices, which is why it is not exactly 100). It is
  stored raw (`iv_raw`). The WebSocket IV in `option_quotes` is a **decimal** (0.126 = 12.6 %). Never mix the two
  without converting.
* **The expiry weekday rules** (Thursday until the 28 Aug 2025 contract, Tuesday from 2 Sep 2025) were
  **confirmed by the owner against NSE circular 103/2025** (2026-11-14). Derived expiries are still labelled
  `rule` (not `observed`) until seen in an Upstox contract list. Holidays come from the candles.
* **Trading days are observed from candles**, not typed in. A weekday without a NIFTY candle is a holiday. A
  weekend or configured-holiday date with a candle is a special session (Muhurat, Budget Saturday) and is never
  used as an expiry.

# R2: forecast vs reality and the volatility core

Findings in plain words, with the full generated report: [`docs/research-r2-findings.md`](research-r2-findings.md).

## Packages

| Package | Contents |
|---|---|
| `volatility/` (pure maths, no I/O; `tests/unit/test_volatility_purity.py`) | `realised` (close-to-close, Parkinson, Garman-Klass, Rogers-Satchell, Yang-Zhang, 5-minute intraday RV, point-in-time EWMA), `expected_move` (calendar vs trading time, k-sd bands), `calibration` (hit rates with Wilson CI, variance ratio, block bootstrap, QLIKE, Mincer-Zarnowitz, clustering), `surface` (ATM IV, skew, term structure, IV rank / percentile) |
| `research/forecast/` | `panel` (NIFTY + VIX + intraday RV), `build` (point-in-time forecasts and outcomes, HAR, expiry context), `run`, `analysis` |
| `research/reports/` | Markdown rendering. Research never writes files: the CLI prints, and the JSON goes to the `reports` table. |

## Commands (offline, no Upstox calls)

| Command | What it does |
|---|---|
| `forecast` | Builds one forecast row per regular NIFTY session (from data before its open) and one outcome row (after its close). The next session's forecast is **recorded live** when run before 09:15; a live row can never be changed or deleted (database triggers). Stale history makes no next-session forecast. |
| `report [--json]` | Forecast-quality report: VIX calibration (calendar vs trading time), weekend multiple, breakdowns (year, regime, trend, VIX change, weekday, gap, expiry), overnight share, forecaster comparison, VRP, and the current state with data-quality warnings. |

Daily routine after the close (outside 08:30–15:40 on weekdays): double-click `scripts\daily-research.bat`, which
runs `history --to today` → `summarize-quotes` → `chain-snapshot` → `forecast` → `report` (README, "Daily
research update"). A next-session forecast counts as **recorded live** only if it is made at least
`live_margin_minutes` (5) before the 09:15 open, so that a PC clock running a little fast cannot mislabel it.
Today's daily candle is accepted as final only after 15:40 IST. A run after the close that does not have today's
candle yet makes no next-session forecast and says so.

## Conventions

- Log returns. Per-session zero-mean variance. RV annualised with 252 sessions.
- VIX and IV are annualised on calendar time (365). The per-session sd is `VIX/100 * sqrt(cal_days/365)`
  (calendar) or `VIX/100 / sqrt(252)` (trading). The report measures which fits better.
- VRP: VIX against the volatility realised over the next 30 calendar days (matched horizon).
- Overnight share and gap statistics start in 2011 (NIFTY daily opens before that are unreliable).
- Regime labels (VIX-percentile terciles, a 1-sd trend test) are provisional R2 reporting buckets.
