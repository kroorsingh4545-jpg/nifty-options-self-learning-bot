# nifty-options-self-learning-bot

A NIFTY weekly options intraday trading bot for Indian markets, using the Upstox Developer API.

> **Status: Phase 8 (RL trade management) complete.** On top of Phases 1–7: a Stable-Baselines3 PPO trade
> manager (Gymnasium environment, versioned reward, 5+ seeds, walk-forward + holdout, judged on equity,
> drawdown and risk compliance). It can veto setups and tighten stops, take a partial profit or exit. It can
> never touch loss limits, exposure, the kill switch, credentials or square-off timing. **There is no real
> historical data yet: every ML and RL model so far is trained on SYNTHETIC data and can never be
> promoted.** ML and RL are off by default. PAPER is the default, and LIVE is always refused in this build.
> **No real orders can be placed.**
> Roadmap: [`docs/build-plan.md`](docs/build-plan.md). ML: [`docs/ml-layer.md`](docs/ml-layer.md).
> RL: [`docs/rl-layer.md`](docs/rl-layer.md).
> Backtesting: [`docs/backtesting.md`](docs/backtesting.md). Dashboard: [`docs/dashboard.md`](docs/dashboard.md).
> Strategies and engine: [`docs/strategies.md`](docs/strategies.md). Risk audits: [`docs/audits/`](docs/audits/).

## RL trade manager (Phase 8)

```bash
python -m ml train-rl --synthetic --lot-units 65 --days 60 --strategy vwap_ema --timesteps 30000   # SYNTHETIC
python -m ml train-rl --data path/to/dataset --strategy vwap_ema --events strategy                # your data
```

Discrete actions: HOLD, TIGHTEN_STOP, MOVE_STOP_TO_BREAKEVEN, TRAIL_STOP, TAKE_PARTIAL_PROFIT, EXIT_FULL,
ENTER (valid setup only) and SKIP_SETUP. Stops only tighten. In training, a frozen risk envelope built from
`risk_limits.yaml` sizes every entry and enforces the daily loss limit, trade caps, cooldown, the 14:45
cut-off and the 15:10 square-off. In the bot, `PositionManager.apply_advice` re-validates every proposal,
and the risk gate still checks every entry. RL is off per strategy (`rl.enabled: false`). See
[`docs/rl-layer.md`](docs/rl-layer.md) for the environment, observation and action schemas, the reward and
the evaluation plan.

## ML layer (Phase 7)

```bash
python -m ml train --synthetic --lot-units 65 --days 30 --strategy orb_options --kind all   # SYNTHETIC research run
python -m ml train --data path/to/dataset --strategy vwap_ema --kind signal_filter        # your historical data
python -m ml list --research
```

Features are computed only from data available at the decision time, by the same function in training and
live inference. Labels use a triple barrier (target / stop-loss / time, capped at the square-off start).
Validation is chronological, purged and embargoed, with a final untouched holdout and at least 5 seeds.
The signal filter can only veto, delay or shrink an entry (threshold 0.60). It never touches stops or risk
limits, and the risk manager re-checks every entry. Models trained on SYNTHETIC data are refused for
validation, promotion and trading by the database, the registry and the inference service. See
[`docs/ml-layer.md`](docs/ml-layer.md) for the feature dictionary, pipeline and registry schema.

## Backtesting (Phase 6)

```bash
python -m backtest synthetic --lot-units 65 --days 20 --seed 7 --out reports/synthetic   # SYNTHETIC demo data
python -m backtest run --data path/to/dataset --out reports/run1              # your historical data
python -m backtest walk-forward --data path/to/dataset --train 10 --test 5 --embargo 1 --holdout 5 --out reports/wf
```

The backtest runs the same strategy, risk and execution code as PAPER trading, on a simulated clock: every
order, fill, position, trade and strategy timestamp is simulated time. Reports cover returns, a CAGR-like
daily metric, win rate, profit factor, drawdown, Sharpe, trade duration, cost impact (brokerage, STT,
exchange, SEBI, stamp duty, GST, spread, slippage), risk compliance and per-strategy results. See
[`docs/backtesting.md`](docs/backtesting.md) for the dataset format and the fill model.

## Dashboard (Phase 5)

```bash
# DASHBOARD_USERNAME / DASHBOARD_PASSWORD must be set in .env
streamlit run dashboard/app.py
```

The bot publishes its state to SQLite every second, and the dashboard queues commands that the bot runs
through `execution/operator_controls.py`. The dashboard never talks to the broker. See
[`docs/dashboard.md`](docs/dashboard.md).

## Risk & execution (Phase 3)

- **`risk/risk_manager.py`** is the only gate from a signal to an order. It checks the kill switch, strategy status, session time (entry window, no entries after 14:45, none in square-off), daily guard, open-trade caps (2 per strategy, 4 total), quote freshness, spread, premium bounds, stop/target levels, and the risk budget, then sizes the position.
- **`risk/position_sizer.py`**: whole lots from the rupee risk budget (≤ ₹2,000 and ≤ the remaining daily budget), stop distance, slippage, round-trip charges, and margin. The ML multiplier can only shrink size.
- **`risk/stop_loss_manager.py`** and **`risk/trailing_stop_manager.py`**: initial SL/TP plus trailing in PREMIUM_PERCENT, POINTS, R_MULTIPLE and STRUCTURAL (NIFTY level) modes. Stops only tighten, and the database refuses widening.
- **`risk/daily_loss_guard.py`**: ₹5,000 per strategy and ₹10,000 global, both including mark-to-market. Also enforces 4 trades/day, a 15-minute cooldown after a loss, and a pause after 3 consecutive losses. State persists and is restored after a restart.
- **`risk/kill_switch.py`**: global or per-strategy. It cancels pending orders, exits positions, disables strategies, alerts, and writes an audit record. Triggers: loss breach, API failure, rate-limit lock, auth failure, WebSocket down past the grace period, reconciliation mismatch, abnormal spread, repeated rejections, exit failure, incomplete square-off, and manual. Reset requires a named operator.
- **`execution/order_manager.py`**: lifecycle CREATED → SUBMITTED → OPEN → PARTIALLY_FILLED → COMPLETE / REJECTED / CANCELLED, plus UNKNOWN on timeouts, which is reconciled and never blindly re-sent. Exits retry safely.
- **`execution/position_manager.py`**: per-strategy trades from fills, an exchange-side SL-M protective stop per position, and tick-based SL/TP/trailing monitoring.
- **`execution/reconciliation.py`**: runs at startup, after order updates (debounced), every 5 minutes, and before and after square-off.
- **`execution/square_off.py`**: forced square-off from 15:10 to 15:20, escalating if anything is still open.
- **`execution/stack.py`** wires it all together.

## Market data & broker layer (Phase 2)

| Module | Role |
|---|---|
| `broker/upstox_rate_limiter.py` | The single gate for every Upstox REST call. Order and standard budgets each enforce 1 s / 60 s / 30 min sliding windows at 80% of the official limits. Requests queue FIFO. HTTP 429 opens a circuit breaker with exponential cooldown that respects `Retry-After`. Repeated 429s lock it until a manual reset. Includes debounce (`RequestCoalescer`) and batching (`KeyBatcher`). |
| `broker/upstox_rest.py` | Official REST endpoints for option contracts, option chain, LTP, intraday candles, positions, funds, order book, and feed authorize. Retries GETs only. Auth failures and the daily 03:30 token expiry fail fast without looping. **Order methods always refuse.** |
| `broker/upstox_websocket.py` | Market feed V3 (Protobuf, official `.proto` in `broker/proto/`) and the portfolio stream. Handles subscribe/unsubscribe/change_mode, heartbeat, reconnect with backoff, and re-authorize plus re-subscribe after every reconnect. Limited to one stream of each kind per process. |
| `broker/paper_broker.py` | Simulates fills against live quotes, with slippage, partial fills, and Indian F&O charges (`broker/charges.py`). Never fills on stale data and refuses naked selling. |
| `data/` | `market_data.py` (tick cache and staleness), `candle_builder.py` (1/3/5/15-minute candles), `option_chain.py` (chain, strike selection, liquidity filter), `websocket_manager.py`. |

Settings live in `config/settings.yaml` under `broker`, `websocket`, `paper_broker`, and `costs`.
**Verify the cost rates against the current Upstox/NSE charge sheets.** To regenerate the
Protobuf module, see `broker/proto/__init__.py`.

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
(+ view `v_daily_pnl_global`), `risk_events`, `feature_snapshots`, `model_runs`, `learning_events`,
`control_commands`, `runtime_state`. Schema v4 adds `model_runs.data_source`: only models trained on
HISTORICAL data can be validated or promoted.

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
| `runtime/` | Composition root: wires everything; imported only by entry points |
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
