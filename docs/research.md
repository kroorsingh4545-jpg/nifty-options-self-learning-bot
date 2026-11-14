# Research layer (R1: isolation and data foundation)

An isolated research layer for volatility intelligence and forecast-vs-reality learning. It **reads** market data
and the trading database. It never trades, never changes a strategy, a risk limit or a file under `config/`, and
the trading bot never imports it. Phase 0 report and plan: owner's copy (not in the repo).

## Isolation (enforced by `tests/unit/test_research_isolation.py`)

| Rule | How it is enforced |
|---|---|
| No order / account calls | No `execution`, `risk`, `runtime`, `dashboard`, paper broker, broker factory or WebSocket imports. No `.place_order(` / `.get_funds(`-style calls in the text. Research code holds a `ReadOnlySource` with exactly four read methods. |
| Network only through the Upstox adapter | No HTTP or socket client imports. Only `research/__main__.py` (the composition root) builds `UpstoxRestClient`, with its own limiter at `rate_share` (25 %) of the bot's already-buffered limits. |
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
| `contracts` | 1 | Saves every current NIFTY option contract (key → strike / expiry / type / lot) so recorded quotes stay interpretable after expiry. |
| `chain-snapshot` | 1 + up to 4 | Option-chain snapshots of the nearest 3 expiries plus the next monthly, with raw IV, Greeks, OI and per-strike PCR. |
| `history [--what all\|daily\|minutes] [--keys nifty,vix] [--to D]` | about 6 daily + about 60 per key for minutes | NIFTY index and India VIX: daily from 2000 (5-year requests), 1-minute from Jan 2022 (one month per request). Resumable: ranges fetched after they ended are skipped. **Refused on weekdays 09:00–15:40 IST.** Auth, scope and rate-limit errors stop the run. Other API errors are logged per range. |

## Data caveats

* **Expired option prices need Upstox Plus** (about 6 months, OHLC + OI, no bid/ask, no IV). Not used.
  Option history is collected forward (`chain-snapshot`, `summarize-quotes`).
* **The chain IV unit is undocumented.** It is stored raw (`iv_raw`) until `smoke` records the unit. The
  WebSocket IV in `option_quotes` is a decimal (0.126 = 12.6 %).
* **The expiry weekday rules** (Thursday until the 28 Aug 2025 contract, Tuesday from 2 Sep 2025) come from
  secondary sources citing NSE circular NSE/FAOP/68747. The owner should verify them against the circular.
  Derived expiries stay `rule` until seen in an Upstox contract list.
* **Trading days are observed from candles**, not typed in. A weekday without a NIFTY candle is a holiday. A
  weekend or configured-holiday date with a candle is a special session (Muhurat, Budget Saturday) and is never
  used as an expiry.
