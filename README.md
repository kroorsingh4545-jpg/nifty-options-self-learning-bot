# nifty-options-self-learning-bot

A NIFTY weekly options intraday trading bot for Indian markets, using the Upstox Developer API.

> **Status: Phase 1 (foundation) complete.** Configuration, logging, SQLite schema and data-access
> layer exist. There are no broker calls, strategies, ML, or RL yet. The full roadmap is in
> [`docs/build-plan.md`](docs/build-plan.md).

## Setup (Phase 1)

Requires **Python 3.11+**.

```bash
# 1. Create a virtual environment and install (with dev tools)
python -m venv .venv
# Windows:  .venv\Scripts\activate      macOS/Linux:  source .venv/bin/activate
pip install -e ".[dev]"

# 2. Create your local .env (never commit it)
cp .env.example .env          # Windows PowerShell: Copy-Item .env.example .env
#    Leave TRADING_MODE=PAPER. Upstox credentials are not needed until Phase 2.

# 3. Validate configuration (prints a masked summary; exits non-zero if invalid)
python -m config

# 4. Create the SQLite database (data/trading.db) and register strategies
python -m data.storage init
python -m data.storage schema     # print the live schema

# 5. Run tests, lint, and type checks
pytest
ruff check .
mypy .
```

### Configuration

| File | Contents |
|---|---|
| `config/settings.yaml` | App name, autonomy level, market session, database path, logging |
| `config/risk_limits.yaml` | Capital, per-trade / per-strategy / global loss limits, trade caps, cooldown, time rules, Upstox rate limits |
| `config/strategy_defaults.yaml` | Per-strategy capital, entry window, strike selection, sizing, SL / TP / trailing, ML threshold |
| `.env` | Secrets and `TRADING_MODE` only (real environment variables override `.env`) |

- All config is validated by Pydantic models (`config/settings.py`) at startup. Unknown keys,
  bad types, and unquoted times are errors. The bot refuses to start on invalid config.
- **Risk limits can only be made stricter.** `config/hard_limits.py` holds the ceilings from the
  spec: ₹2,000 per trade, ₹5,000 per strategy per day, ₹10,000 global per day, 2/4 open trades,
  4 trades/day, 15-minute cooldown, 3 consecutive losses, no entries after 14:45, square-off
  15:10–15:20, and a rate-limit buffer of at least 20%. A config that loosens any of these fails to load.
- Effective Upstox rate limits (official minus 20%): orders 8/s, 400/min, 1,600/30 min;
  standard APIs 40/s, 400/min, 1,600/30 min.
- **`TRADING_MODE=LIVE` is refused** in this build. LIVE preconditions from
  `docs/safety-policy.md` are not implemented yet.

### Logging

JSON lines go to stderr and `logs/bot.jsonl`. Each line has an IST timestamp, level, logger,
event, `mode`, `strategy_id`, `order_id`, plus any extra context. Every secret loaded from `.env`
is registered with a masker. Bearer tokens, `access_token=…`-style values, and Telegram bot
tokens are masked by pattern too, including inside exception tracebacks.

```python
from monitoring.json_logger import get_logger
log = get_logger(__name__, strategy_id="orb_options")
log.info("signal generated", extra={"side": "CE"})
```

### Database

SQLite at `data/trading.db` (git-ignored, WAL mode, foreign keys on). The schema is
`data/storage/schema.sql`. Tables: `strategies`, `orders`, `fills`, `positions`, `trades`, `daily_pnl`
(+ view `v_daily_pnl_global`), `risk_events`, `feature_snapshots`, `model_runs`, `learning_events`.

PAPER/LIVE separation is enforced **by the database itself**, not only by Python:
- Every order, fill, position and trade row has a `mode` column.
- PAPER rows need a `paper_*_id` and cannot have a broker id. LIVE rows cannot have a `paper_*_id`.
- `client_order_id` / `trade_uid` must start with `PAPER-` or `LIVE-` to match `mode`.
- Triggers block fills or orders whose mode or strategy differs from their parent, and block
  changes to mode, strategy, or identifiers after insert.
- Open positions and all trades must carry a stop-loss. `risk_events` and `fills` are append-only.

Repositories (`data/storage/repositories.py`) are bound to one mode. A PAPER repository cannot
read or write LIVE rows, and the reverse is also true.

## What the bot will do (planned)

- Trade NIFTY weekly options intraday.
- Run multiple isolated strategies, ₹1,00,000 capital each.
- PAPER and LIVE modes — **PAPER by default**.
- Per-strategy stop-loss, take-profit, and trailing-profit rules.
- Per-strategy and global daily loss limits, plus a kill switch.
- ML signal filtering and PPO-based RL trade management (within hard risk limits).
- Reflective Loop: `LOG → REFLECT → HYPOTHESIZE → MUTATE → VERIFY → PROMOTE / REJECT`.
- Streamlit dashboard.

## Folder layout

| Folder | What goes there |
|---|---|
| `config/` | Pydantic settings |
| `data/` | Market data handling; local databases (git-ignored) |
| `core/` | Shared models, interfaces, DI |
| `strategies/` | One package per strategy |
| `ml/` | ML filters and RL agents |
| `learning/` | Reflective Loop |
| `options/` | Option chain and strike selection |
| `risk/` | Risk limits, kill switch |
| `broker/` | Upstox clients, rate limiter, paper broker |
| `execution/` | Order management — the only place real orders are sent |
| `backtest/` | Backtesting and walk-forward |
| `dashboard/` | Streamlit UI |
| `monitoring/` | Logs, metrics, alerts |
| `tests/` | Tests |
| `docs/` | Safety policy, MCP setup |

## Claude Code setup

- MCP servers: `.mcp.json` — see `docs/mcp-setup.md`
- Skills: `.claude/skills/`
- Commands: `.claude/commands/` (`/build-strategy`, `/audit-risk`, `/train-models`, `/run-paper-session`, `/review-learning-loop`)
- Permissions: `.claude/settings.json`

## Safety

Read `docs/safety-policy.md` first. Claude never places real orders.

## Secrets

Copy `.env.example` to `.env` on your own machine and fill it in. **Never commit `.env`.**
