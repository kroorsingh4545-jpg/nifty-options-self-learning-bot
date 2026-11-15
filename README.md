# nifty-options-self-learning-bot

A NIFTY weekly options intraday trading bot for Indian markets, using the Upstox Developer API.

> **Status: Phase 10 (finalization) complete. This build trades in PAPER mode only.**
> Real Upstox market data goes in; every order is simulated by `PaperBroker`. There is no LIVE order
> routing in this build: `TRADING_MODE=LIVE` is refused at config load, the Upstox client's order methods
> always refuse, and no flag, setting or environment variable changes that.
> **There is no real historical data yet**: every ML / RL model and learning-loop result so far comes from
> SYNTHETIC data and can never be promoted.

What it does:

* Two isolated strategies (ORB options, VWAP+EMA on the NIFTY future), ₹1,00,000 capital each.
* One risk gate for every entry: per-trade, per-strategy and global daily loss limits, trade caps,
  cooldowns, no entries after 14:45, forced square-off 15:10–15:20, and a kill switch.
* Optional ML signal filter (LightGBM) and PPO RL trade manager, both bounded by the risk layer.
* The Reflective Loop: `LOG → REFLECT → HYPOTHESIZE → MUTATE → VERIFY → PROMOTE / REJECT`, which can deploy
  improved strategy versions to PAPER (never to LIVE).
* A Streamlit dashboard, Telegram alerts, Docker deployment.

Contents: [Setup](#setup) · [Upstox app](#create-an-upstox-app) · [Access token](#generate-an-access-token) ·
[PAPER trading](#paper-trading) · [Telegram](#telegram-alerts) · [Dashboard](#dashboard) ·
[Docker](#docker) · [Backtesting](#backtesting) · [ML / RL training](#ml-and-rl-training) ·
[Learning loop](#self-learning-loop) · [LIVE safety checklist](#live-mode-safety-checklist) ·
[Configuration](#configuration) · [Project layout](#project-layout)

Deeper docs: [deployment checklist and known limitations](docs/deployment.md) ·
[safety policy](docs/safety-policy.md) · [strategies](docs/strategies.md) · [backtesting](docs/backtesting.md) ·
[ML](docs/ml-layer.md) · [RL](docs/rl-layer.md) · [learning loop](docs/learning-loop.md) ·
[dashboard](docs/dashboard.md) · [risk audits](docs/audits/) · [build plan](docs/build-plan.md)

## Setup

Requires **Python 3.11+** (Phases 1–9 were developed on 3.11; this release was tested on 3.13; the Docker
image uses 3.11).

```bash
# 1. Virtual environment
python -m venv .venv
# Windows:  .venv\Scripts\activate        macOS/Linux:  source .venv/bin/activate

# 2. Install. CPU-only PyTorch first if you have no GPU (much smaller download):
pip install torch --index-url https://download.pytorch.org/whl/cpu
pip install -e ".[dev]"

# 3. Local secrets (never commit .env)
cp .env.example .env              # Windows PowerShell: Copy-Item .env.example .env
#    Leave TRADING_MODE=PAPER. Fill in Upstox (below), Telegram and dashboard values.

# 4. Validate the configuration (prints a masked summary; exits non-zero if invalid)
python -m config

# 5. Create / migrate the trading database (data/trading.db) and register the strategies
python -m data.storage init

# 6. Tests, lint, type checks
pytest
ruff check .
mypy .
```

Moving to another computer: delete `.venv` and recreate it there (a venv is tied to the Python install it
was created with). `.env` and `data/trading.db` are local files; copy them yourself if you need them.

## Create an Upstox app

1. Open an Upstox account (with F&O enabled) and sign in to the developer console:
   <https://account.upstox.com/developer/apps>.
2. **Create new app.** Give it a name and a **Redirect URL** you control, for example
   `http://127.0.0.1:5000/callback` (it only has to match exactly in the token step; nothing needs to run
   there).
3. Copy the **API Key** and **API Secret** into `.env`:

   ```
   UPSTOX_API_KEY=...
   UPSTOX_API_SECRET=...
   ```

The bot uses the official REST API (`https://api.upstox.com`) and the Market Data Feed V3 WebSocket, through
a central rate limiter set 20% below Upstox's published limits. In PAPER mode it calls only market-data
endpoints: the instrument list, market-feed authorisation, option contracts / chain and LTP (recovery, and the
one `--connect` check).

## Analytics token (recommended for PAPER)

An Upstox **Analytics Token** is read-only, valid for about a year and cannot place orders. Without a static IP
it only works with the market-data APIs (market quote, historical data, option chain, market information and
the market-data WebSocket); user, funds, portfolio and order APIs reject it. That is everything PAPER needs.

```
UPSTOX_ACCESS_TOKEN=<analytics token>
UPSTOX_TOKEN_TYPE=analytics   # the default; UPSTOX_API_KEY is not needed with an analytics token
```

With `UPSTOX_TOKEN_TYPE=analytics` the REST client refuses every non-market-data path locally
(`TokenScopeError`; nothing is sent), there is no 03:30 daily expiry (a rejected token still trips the kill
switch with `AUTH_FAILURE`), and **LIVE always refuses it**: config loading, the broker factory, the LIVE mode
request and the order methods each reject an analytics token. Use `UPSTOX_TOKEN_TYPE=daily` only with a daily
token from the flow below.

## Generate an access token

(Only for `UPSTOX_TOKEN_TYPE=daily`; skip this with an analytics token.) Upstox access tokens are issued per day and **expire at 03:30 IST the next morning**. Generate one each
trading day before starting the bot. (Check the current flow in the Upstox API docs, "Authentication".)

1. Open this URL in a browser (your API key and the exact redirect URL from the app):

   ```
   https://api.upstox.com/v2/login/authorization/dialog?response_type=code&client_id=<API_KEY>&redirect_uri=<REDIRECT_URL>
   ```

2. Log in and approve. The browser is sent to `<REDIRECT_URL>?code=<CODE>`; copy the `code` value (it is
   single-use and short-lived).
3. Exchange it for a token:

   ```bash
   curl -X POST https://api.upstox.com/v2/login/authorization/token \
     -H "Content-Type: application/x-www-form-urlencoded" -H "Accept: application/json" \
     -d "code=<CODE>" -d "client_id=<API_KEY>" -d "client_secret=<API_SECRET>" \
     -d "redirect_uri=<REDIRECT_URL>" -d "grant_type=authorization_code"
   ```

4. Put the `access_token` from the response into `.env` as `UPSTOX_ACCESS_TOKEN=...` with
   `UPSTOX_TOKEN_TYPE=daily`, then check it (one market-data call, the NIFTY index LTP):

   ```bash
   python -m runtime check --connect
   ```

Never paste the token into chat, tickets or code. It is masked in every log line. When the token expires
mid-session, the next Upstox call fails, the kill switch trips with `AUTH_FAILURE` (persistent), and new
entries stop until a named operator resets it after you restart with a fresh token.

## PAPER trading

```bash
python -m runtime check                  # pre-flight checks + which strategy version each strategy will trade
python -m runtime paper                  # start a real-time PAPER session (Ctrl+C for an orderly stop)
python -m runtime paper --paused         # start with the engine PAUSED; start it from the dashboard
python -m runtime paper --stop-at 15:40  # end the session at 15:40 IST today
python -m runtime paper --baseline       # trade the YAML config, ignoring learned PAPER deployments
python -m runtime health                 # exit 0 if the bot's heartbeat is fresh (used by Docker)
```

**PC clock.** `check` and `paper` compare this PC's clock with Upstox server time (the HTTP `Date` header of
three rate-limited market-data calls). More than `broker.max_clock_offset_seconds` (2 s) off, or not measurable,
is a FAIL and `paper` refuses to start. Candles are timed by the exchange but the session (entry cut-off,
square-off) by this PC, so keep it synced (Windows, admin: `net start w32time` then `w32tm /resync`). During a
session the clock is re-checked every 15 minutes and the dashboard warns on drift; while the NIFTY feed lag
(local receipt − exchange time of index ticks) is above `websocket.max_entry_feed_lag_seconds` (5 s), **new
entries are blocked**. Exits (stops, targets, trailing, square-off, manual exits) are never blocked.

**Pre-flight** (`check`, and again at start-up). FAIL items block the launch: not PAPER, LIVE routing
present, missing Upstox credentials, a database with a newer schema, or a database inside a research
directory. WARN items do not: no NSE holidays for this year (every entry is then blocked), not a trading
day, Telegram or dashboard login not configured.

**Start-up**, in order: open and migrate the database; choose the autonomy level (the most restrictive of
`app.autonomy_level` and the last level applied from the dashboard); resolve the strategy versions; connect
the read-only Upstox clients; build the bot with `PaperBroker`; load today's instrument list (lot sizes,
expiries, the current NIFTY future); restore open PAPER trades, the daily loss guard and the kill switch;
reconcile; subscribe the index, the future and ATM ±3 strikes; start the scheduler and the engine.

**Which version trades.** Each strategy runs its YAML config unless the learning loop has deployed a
PAPER version of it. Deployed versions are applied **only at start-up**: the YAML config with the
deployment overlaid, re-checked so that only whitelisted parameters may differ and every validator passes.
A deployment that no longer fits the YAML falls back to the YAML config with a CRITICAL log. Under
`OBSERVE_ONLY`, or with `--baseline`, every strategy runs its YAML version and the deployment records stay
as they are. `check` lists the version each strategy will run and why.

**During the session** (all IST): candles and signals from the live feed; entries only through the risk
gate; an exchange-style SL-M stop per position plus tick-level stop / target / trailing; reconciliation at
least every 5 minutes; no entries after 14:45; forced square-off 15:10–15:20; end-of-day summary at 15:35;
the learning loop at 15:45 if enabled. The dashboard heartbeat is published every second.

**Stopping.** Ctrl+C or SIGTERM: no new entries, background work settles, pending trades are journaled,
the final state is published, then the feed and database close. Positions still open stay in the database
and are restored at the next start, but their stops are monitored only while the bot runs, so stop after
the square-off. Run one session per trading day (a daily access token expires overnight).

The kill switch trips on: a daily loss breach (cleared automatically the next trading day), WebSocket down
for more than 30 s or reconnects exhausted, Upstox auth failure, the rate-limit circuit locking, repeated API
failures or order rejections, an exit or square-off that cannot complete, a reconciliation mismatch, an
abnormal spread on a held contract, or a manual kill. Every reason except daily loss needs a named
operator to reset it (dashboard).

## Telegram alerts

1. Create a bot with [@BotFather](https://t.me/BotFather) (`/newbot`) and copy its token.
2. Send your bot a message, then read your chat id from
   `https://api.telegram.org/bot<TOKEN>/getUpdates` (`"chat":{"id":...}`).
3. Set both in `.env` (they must be set together):

   ```
   TELEGRAM_BOT_TOKEN=123456789:AA...
   TELEGRAM_CHAT_ID=987654321
   ```

The bot sends: **entries**, **exits** (reason, net P&L after charges, R-multiple), **errors** (ERROR and
CRITICAL log lines, de-duplicated over 5 minutes), **daily loss breaches**, **kill switch** triggers,
escalations and failed flattens, and the **end-of-day summary** (per-strategy trades and P&L, loss used vs
limit, open trades after the close, kill-switch state). Tune it under `alerts:` in
`config/settings.yaml`; CRITICAL alerts are always sent while Telegram is enabled.

Sending never blocks trading: alerts go through a bounded queue drained in the background, paced to about one
message per second, with HTTP 429 `retry_after` honoured. A rejected token or chat disables the channel with
one ERROR log. Messages pass through the secret masker.

## Dashboard

```bash
# DASHBOARD_USERNAME / DASHBOARD_PASSWORD (8+ characters) must be set in .env
streamlit run dashboard/app.py           # from the project root; http://127.0.0.1:8501
```

It shows mode, autonomy, connections, rate-limit usage, P&L and loss limits per strategy, open positions,
the kill switch, the strategy version each strategy runs (and whether it is a learned deployment), alerts and
logs, and the learning loop's candidates. It queues commands (kill switch and named reset, exit a position,
engine start / pause / stop, autonomy level) that the bot runs through `execution/operator_controls.py`; the
dashboard never talks to the broker. It listens on 127.0.0.1 only. A LIVE request is evaluated and always
refused in this build. See [`docs/dashboard.md`](docs/dashboard.md).

## Double-click start (Windows)

Two start files in `scripts/` do the whole morning start in one double-click:

| File | For | Python it uses |
|---|---|---|
| `scripts\start-office.bat` | office PC | `C:\Users\Prince\venvs\nifty-bot\Scripts\python.exe` |
| `scripts\start-home.bat` | home PC | the project's `.venv\Scripts\python.exe` |

Each one goes to its own project folder (the folder above `scripts\`), then:

1. runs `python -m runtime check --connect`. If **any** check is `[FAIL]` it stops with a clear message and
   starts nothing (`WARN` lines do not stop it);
2. starts the PAPER bot (`python -m runtime paper`) in its own window, **unless a bot is already running**
   (a `runtime paper` process on this PC, or a fresh heartbeat from `runtime health`). It never starts a
   second bot;
3. waits 30 seconds, then starts the dashboard in another window (skipped if something already listens on
   port 8501);
4. opens <http://127.0.0.1:8501> in your browser.

Stop the bot as before: Ctrl+C in the bot window, after the square-off. The shared steps live in
`scripts\start-common.cmd`; do not run that file by itself.

**Desktop shortcut.** In File Explorer open the project's `scripts` folder, right-click `start-office.bat`
(or `start-home.bat`) and choose **Send to → Desktop (create shortcut)** (Windows 11: click **Show more
options** first). Rename the
shortcut if you like (e.g. "NIFTY PAPER bot"); to give it an icon, right-click it → **Properties → Change
Icon…**. Keep the `.bat` itself in `scripts\`: the shortcut only points to it, and the file finds the project
folder from its own location.

### Daily research update (after 15:40)

`scripts\daily-research.bat` updates the research layer (see `docs/research.md`) in one double-click. It is
**research only**: it never starts, stops or talks to the PAPER bot, changes no trading setting and places no
orders. It uses the same Python as the start files: the project's `.venv` if it exists (home PC), otherwise
`C:\Users\Prince\venvs\nifty-bot` (office PC).

**When:** after **15:40** on a trading day, or any time at the weekend. Research refuses Upstox calls on weekdays
08:30–15:40 IST so it never competes with a running PAPER session. If you double-click too early, it stops with
a message and does nothing.

**What it does**, in this order:

1. `history --to today` refreshes the NIFTY and India VIX candles (a few Upstox calls; already-complete ranges
   are skipped);
2. `summarize-quotes` copies the bot's recorded option quotes into one-minute bars (no Upstox call; the trading
   database is opened read-only);
3. `archive-quotes` copies the bot's **raw 5-second** option and market quotes into the research database before
   the bot's 30-day purge (no Upstox call; read-only);
4. `data-quality` audits what the bot recorded today and saves it to `logs\research\data-quality-YYYY-MM-DD.txt`
   (see *Data quality* in `docs/research.md`);
5. `chain-snapshot` saves an option-chain snapshot of the nearest expiries plus the next monthly (a few calls);
6. `shadow` simulates every recorded strategy signal on the recorded quotes and saves the counterfactual outcomes
   with their volatility evidence (IV, Greeks, decay and costs vs the expected move) and the regime and
   Momentum Pulse at each signal
   to `logs\research\shadow-YYYY-MM-DD.txt` (see *Shadow mode* in `docs/research.md`; no Upstox call);
7. `forecast` adds today's outcome and **tomorrow's forecast**, made before the open so it is recorded live and
   can never be changed later;
8. `report` saves the full report to `logs\research\report-YYYY-MM-DD.md`.

**Reading the result.** The window stays open at the end:

- `Done.` means every step worked. The forecast step's lines say whether tomorrow's forecast was recorded
  live.
- `Finished WITH PROBLEMS in: …` names the failed step(s). The messages above it say why (for example an
  expired Upstox token).
- If the forecast step says *today's NIFTY daily candle is not in the history yet*, Upstox had not published
  today's candle when you ran it. Double-click again later in the evening, or before 08:30 the next morning;
  a forecast made by 09:10 still counts as live.

Make a desktop shortcut the same way as for the start files (right-click `daily-research.bat` → **Send to →
Desktop (create shortcut)**).

### Option-chain snapshots during the day (optional, morning)

`scripts\chain-schedule.bat` takes option-chain snapshots at 09:20, 11:00, 13:30 and 15:20 IST (`chain.schedule`
in `research\research.yaml`) and closes itself after the last one. Double-click it in the morning next to the
PAPER bot. It is research only, with about 5 read-only Upstox calls per time on research's own small budget, and
it never talks to the bot. Closing the window early is safe: the snapshots already taken are kept.

## Docker

```bash
cp .env.example .env                            # fill it in (TRADING_MODE stays PAPER)
docker compose build
docker compose run --rm bot python -m runtime check --connect
docker compose up -d                            # bot + dashboard
docker compose logs -f bot
# dashboard: http://127.0.0.1:8501 (bound to localhost on the host)
docker compose stop bot                         # SIGTERM -> orderly shutdown (60 s grace)
```

`TRADING_MODE` is pinned to `PAPER` in the image and in `docker-compose.yml`. The database lives on the
`bot-db` volume (`DATABASE_PATH=/app/var/trading.db`), shared by the bot and the dashboard; research runs,
logs, reports and models have their own volumes. The three YAML config files are mounted read-only from the
host, so you can change config and restart without rebuilding. The bot container's health check is
`python -m runtime health` (heartbeat younger than 30 s). Inside the container the dashboard listens on
0.0.0.0 so the port mapping works (hence `--allow-remote`); the host side is bound to 127.0.0.1 only. Put a
TLS reverse proxy in front before exposing it anywhere else.

## Backtesting

```bash
python -m backtest synthetic --lot-units 65 --days 20 --seed 7 --out reports/synthetic   # SYNTHETIC demo data
python -m backtest run --data path/to/dataset --out reports/run1                          # your data
python -m backtest walk-forward --data path/to/dataset --train 10 --test 5 --embargo 1 --holdout 5 --out reports/wf
```

The backtest runs the same strategy, risk and execution code as PAPER trading on a simulated clock, through
`PaperBroker` fills with slippage and Indian F&O charges. It never connects to Upstox and refuses the trading
database. A dataset is a directory with `instruments.json`, `candles.csv` (1-minute candles, IST offsets) and
`meta.json` stating `"source": "HISTORICAL"` or `"SYNTHETIC"`. See [`docs/backtesting.md`](docs/backtesting.md).

## ML and RL training

```bash
# ML signal filter / outcome predictor / regime classifier (LightGBM)
python -m ml train --synthetic --lot-units 65 --days 30 --strategy orb_options --kind all   # SYNTHETIC research
python -m ml train --data path/to/dataset --strategy vwap_ema --kind signal_filter        # HISTORICAL data
python -m ml list --research

# PPO RL trade manager (Stable-Baselines3)
python -m ml train-rl --synthetic --lot-units 65 --days 60 --strategy vwap_ema --timesteps 30000
python -m ml train-rl --data path/to/dataset --strategy vwap_ema --events strategy
```

Features are computed only from data available at decision time, by the same code in training and in the
bot. Validation is chronological, purged and embargoed, with an untouched holdout and at least 5 seeds.
Models trained on SYNTHETIC data go to a separate research registry and are refused for promotion and
trading by the database, the registry and the inference service. The ML filter can only veto, delay or
shrink an entry; the RL agent can veto entries and tighten stops, take a partial profit or exit, and never
touches loss limits, exposure, the kill switch, credentials or square-off timing. Both are off per strategy
until you enable them (`ml.enabled`, `rl.enabled`). See [`docs/ml-layer.md`](docs/ml-layer.md) and
[`docs/rl-layer.md`](docs/rl-layer.md).

## Self-learning loop

```bash
python -m learning synthetic --lot-units 65 --paper-days 25 --verify-days 12 --min-trades 5 --out reports/learning
python -m learning run --data path/to/historical-dataset      # on the trading database, after the close
```

Every closed PAPER trade is journaled with its features at entry and exit. The loop reflects on the journal,
proposes changes to **whitelisted** parameters only (entry thresholds, stops, targets, trailing, time
windows, strike rules, ML threshold, a size multiplier that can only shrink size), verifies each candidate
by backtest, walk-forward, out-of-sample, stress, multi-seed and no-trade comparisons, and promotes passing
candidates to `PAPER_READY` **only on HISTORICAL evidence**. Capital, loss limits, exposure, the kill switch,
square-off and credentials are never mutable.

The loop reads the **running bot's autonomy level at every promotion step**: inside the PAPER session
(`learning.loop.enabled`, 15:45 IST, HISTORICAL dataset in `learning.loop.verify_data`) it reads the bot's
live level; `python -m learning run` in another process reads the bot's published heartbeat and, when the
bot is not running, the level the bot would start with. A switch to `OBSERVE_ONLY` on the dashboard stops the
next promotion or deployment immediately.

| autonomy level | PAPER_READY | auto PAPER deploy | LIVE_CANDIDATE | LIVE |
|---|---|---|---|---|
| `OBSERVE_ONLY` | no | no (and sessions run the YAML baseline) | no | never |
| `PAPER_AUTO_IMPROVE` (default) | yes | yes | no | never |
| `LIVE_CANDIDATE_APPROVAL` | yes | yes | yes, after the PAPER validation period | never |

A PAPER deployment takes effect at the next session start. LIVE approval needs a named human and a typed
confirmation, and even then nothing in this build deploys to LIVE. See
[`docs/learning-loop.md`](docs/learning-loop.md).

## LIVE mode safety checklist

**LIVE trading is not available in this build and must not be enabled by editing code or config.** Before
any future LIVE build is even considered, every item below must be true and verified by a person
([`docs/safety-policy.md`](docs/safety-policy.md)):

1. LIVE order routing is implemented in `execution/` only, reviewed, and has passed a risk-and-execution
   audit with no blocking issues (`/audit-risk`).
2. The strategy version and every model it uses were validated on **real HISTORICAL data** and then in PAPER
   for at least `learning.paper_validation_days` with at least `paper_validation_min_trades` trades.
3. A **named human** approved that exact version (`LIVE_APPROVED`) with the typed confirmation phrase.
4. Broker reconciliation passes (orders, positions, funds match Upstox) and there are no pending orders.
5. The kill switch is armed and has been tested end to end, including a manual kill and named reset.
6. Per-strategy and global daily loss limits are configured and no looser than `config/hard_limits.py`.
   **The per-trade risk has been re-decided by the owner for LIVE** and recorded in
   [`docs/safety-policy.md`](docs/safety-policy.md) (section 8). PAPER uses Rs 3,000 per trade (decision
   of 2026-11-13); the config refuses LIVE with more than Rs 2,000.
7. Upstox rate limits and charges are re-verified against current Upstox / NSE documentation.
8. The holiday calendar for the year is loaded; the Upstox token is valid (fresh, if daily); the WebSocket is connected.
9. Telegram alerts are configured and a test alert was received.
10. Start with the smallest size (one lot, one strategy) and the operator watching the dashboard.
11. The operator types `ENABLE LIVE TRADING` to confirm. Any failed check refuses LIVE and logs why.

## Configuration

| File | Contents |
|---|---|
| `config/settings.yaml` | Autonomy level, market session and NSE holidays, instruments, database, broker / WebSocket, paper broker, costs, execution, ML, learning loop, alerts, logging |
| `config/risk_limits.yaml` | Capital, per-trade / per-strategy / global loss limits, trade caps, cooldown, time rules, kill-switch thresholds, Upstox rate limits |
| `config/strategy_defaults.yaml` | Per-strategy capital, entry window, strike selection, sizing, SL / TP / trailing, ML and RL settings |
| `.env` | Secrets, `TRADING_MODE` (PAPER), optional `DATABASE_PATH`. Real environment variables override `.env` |

* Pydantic validates everything at start-up; unknown keys, bad types and unquoted times are errors, and the
  bot refuses to start on invalid config.
* **Risk limits can only be made stricter.** `config/hard_limits.py` holds the ceilings: ₹3,000 per trade in PAPER (₹2,000 in LIVE; owner decision 2026-11-13),
  ₹5,000 per strategy per day, ₹10,000 global per day, 2 / 4 open trades, 4 trades per day, 15-minute
  cooldown after a loss, pause after 3 consecutive losses, no entries after 14:45, square-off 15:10–15:20,
  rate-limit buffer of at least 20%.
* **Add next year's NSE holidays** to `market_session.holidays` when NSE publishes them (usually December):
  without them every new entry is blocked.
* Verify the cost rates (`costs:`) against the current Upstox and NSE charge sheets.

Logging: JSON lines to stderr and `logs/bot.jsonl` (rotated), IST timestamps, mode, strategy and order ids.
Every secret loaded from `.env` is masked, as are bearer tokens and Telegram bot tokens by pattern.

Database: SQLite (WAL) at `data/trading.db`, schema in `data/storage/schema.sql`. PAPER / LIVE separation is
enforced by the database itself (mode columns, id prefixes, triggers), not only by Python; learned strategy
deployments can only be recorded for PAPER.

## Project layout

| Folder | What goes there |
|---|---|
| `config/` | Pydantic settings, YAML config, hard limits |
| `core/` | Shared models, enums, interfaces, engine, event bus, autonomy policy |
| `broker/` | Upstox REST / WebSocket clients, rate limiter, paper broker, charges |
| `data/` | Market data, candles, instruments, SQLite storage |
| `options/` | Option chain and strike selection |
| `strategies/` | One module per strategy |
| `risk/` | Risk gate, sizing, stops, trailing, daily loss guard, kill switch |
| `execution/` | Orders, positions, reconciliation, square-off, operator controls |
| `ml/` | Features, labels, LightGBM models, PPO agent, registry, inference |
| `learning/` | Trade journal and the Reflective Loop |
| `backtest/` | Backtester, walk-forward, synthetic data, reports |
| `monitoring/` | JSON logging, alerts, Telegram |
| `runtime/` | Composition root: the bot, PAPER launcher, control plane, notifications |
| `dashboard/` | Streamlit UI |
| `tests/` | Unit, integration and end-to-end tests |
| `docs/` | Safety policy, design docs, audits |

## Claude Code setup

MCP servers: `.mcp.json` (see `docs/mcp-setup.md`). Skills: `.claude/skills/`. Commands: `.claude/commands/`
(`/build-strategy`, `/audit-risk`, `/train-models`, `/run-paper-session`, `/review-learning-loop`).

## Safety

Read [`docs/safety-policy.md`](docs/safety-policy.md) first. Claude never places real orders. Credentials live
only in `.env`: copy `.env.example`, fill it in on your own machine, and **never commit `.env`**.
