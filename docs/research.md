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
