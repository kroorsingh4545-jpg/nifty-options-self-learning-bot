# Dashboard (Phase 5)

A Streamlit dashboard that shows the PAPER bot's live state and sends operator commands. It is **UI only**: it
never talks to the broker, holds no trading or risk logic, and cannot enable LIVE trading.

## Run it

```bash
# 1. Set dashboard credentials in .env (required; the dashboard refuses to run without them)
#    DASHBOARD_USERNAME=...   DASHBOARD_PASSWORD=...   (8+ characters)
# 2. Start the dashboard against the bot's database (database.path in config/settings.yaml)
streamlit run dashboard/app.py                               # run from the project root
streamlit run dashboard/app.py -- --db path/to/trading.db     # another database
```

* **Loopback only.** `.streamlit/config.toml` binds the server to `127.0.0.1`. The app checks the address
  itself and refuses to run on anything else (including "all interfaces", which is what you get when
  Streamlit is started from a directory without that config file). For remote access, put it behind a TLS
  reverse proxy and start it with `-- --allow-remote`.
* **The bot owns the database.** Only the bot creates and migrates the schema. The dashboard opens the
  existing database with `Database.open_existing` and refuses to start if the file is missing or its
  schema version differs from the code's. It re-checks the version on every page run, so a bot upgraded
  while the dashboard is open is caught as well.

The bot publishes its state every second while it runs (`control` job in `runtime/bot.py`). If no bot is
running, the dashboard says **OFFLINE**. Commands still queue: a kill switch is applied when the bot starts,
and any other command expires after 60 s.

For UI testing without market data there is a **SYNTHETIC** PAPER bot (generated ticks, never connects to
Upstox):

```bash
python -m tests.e2e.synthetic_paper_bot --db /tmp/qa.db --seconds 900
streamlit run dashboard/app.py -- --db /tmp/qa.db
```

## Architecture

```
 Streamlit (dashboard/)                    SQLite                      Bot process (runtime/)
 ─────────────────────                     ──────                      ──────────────────────
 data_access  ◀── read ──────────────── runtime_state  ◀── publish ── ControlService.publish_state (1 s)
             ◀── read ──────────────── trades, orders,
                                        risk_events, ...
 commands     ── enqueue (operator) ──▶ control_commands ── poll ───▶ ControlService.process_pending (1 s)
                                                                           │
                                                 OperatorControls / KillSwitch / PositionManager /
                                                 ModeController  (risk + execution services)
                                                                           │
                                                ◀── result (DONE / REJECTED / FAILED / EXPIRED) ──┘
```

* **Why SQLite and not an HTTP API:** every command is durable and audited (who, when, what, result), no
  network server has to be secured, and a dashboard crash cannot affect trading.
* **Operator = signed-in user.** Commands carry the authenticated dashboard username, never free text.
* **A processed command is final** (database trigger). Commands expire after 60 s, except the kill switch,
  which never expires: acting late on an emergency stop is still safe.
* **Kills first.** Queued kill-switch commands run before every other queued command. Manual exits run in
  the background, so an exit that keeps retrying (up to ~30 s) never delays a later kill or the heartbeat.
  An exit that is still running is not started twice and does not expire.
* **Stop means no new entries.** Start, Pause and Stop only change the engine. Stops, square-off and
  reconciliation always keep running.
* The dashboard may import `config`, `core`, `data.storage` and `strategies.indicators` (chart overlays)
  only. An architecture test forbids `broker`, `execution`, `risk` and `runtime`.

## Controls and their safety rules

| Control | Rule |
|---|---|
| PAPER / LIVE toggle | PAPER on every start and every new session. LIVE shows the preconditions (broker connection, capital, active strategies, open positions, risk state, model version) and needs the typed phrase `ENABLE LIVE TRADING` plus a named approver. A wrong phrase is rejected in the UI and nothing is queued. Pending orders disable the request. The bot evaluates every precondition in `docs/safety-policy.md` and **always refuses** in this build (no LIVE order routing); the reasons are shown. |
| Emergency kill switch | Always visible. One click, then confirm. The bot triggers a MANUAL kill: new orders blocked, pending orders cancelled, all positions exited through the execution layer. |
| Strategy kill switch | Per strategy card, with confirmation. Other strategies are unaffected. |
| Kill-switch reset | Named operator plus a required note. The bot reconciles first and refuses on any mismatch. Daily-loss kills cannot be reset on the day they happen. After the reset, every KILLED strategy that is no longer blocked is **explicitly re-activated**, and each re-activation is recorded with the operator's name. |
| Manual exit | Per open position, with confirmation. Recorded as `OPERATOR_EXIT` with the operator's name. |
| Autonomy level | OBSERVE_ONLY / PAPER_AUTO_IMPROVE / LIVE_CANDIDATE_APPROVAL. |

## Displays

* **Control bar:** mode banner, bot heartbeat, Upstox, WebSocket, market session, square-off countdown,
  autonomy, instrument-list and holiday-calendar blocks. The WebSocket tile reads the bot's structured
  `websocket_connected` / `websocket_stale` fields: *connected*, *stale* (connected but silent), *offline*, or
  *unknown* while the bot is not publishing.
* **Market strip:** latest NIFTY index and NIFTY future prices from the live feed (*live*, *STALE* or
  *snapshot*, with the tick age) and the **feed lag**: the median (local receipt − exchange time) of NIFTY
  index ticks. A lag above `websocket.max_feed_lag_seconds` (default 2 s) shows a warning: it almost always
  means the PC clock is off (Windows: `w32tm /resync`). Above `websocket.max_entry_feed_lag_seconds` (5 s) new
  entries are blocked (never exits) and the reason shows under "New entries blocked". The **PC clock** tile shows
  the last in-session comparison with Upstox server time (re-checked every 15 min) and warns beyond
  `broker.max_clock_offset_seconds` (2 s).
* **Overview:** total / deployed capital, available funds, realized / unrealized / total P&L, daily loss used
  and remaining, open positions, trades today, win rate, profit factor, max drawdown. Strategy cards show
  name, version, status, enabled, allocated capital, mode, P&L, daily loss usage, open trades, trades today,
  risk per trade, last signal and decision, ML confidence and regime placeholders, model version, and a
  strategy kill switch.
* **Positions:** strategy, symbol, side, quantity, entry, LTP, unrealized P&L, stop-loss, target, trailing
  stop, R-multiple, holding time, and an Exit button.
* **Risk:** global and per-strategy daily loss limits, per-trade risk, open exposure, margin usage, API
  rate-limit usage, WebSocket subscriptions, reconciliation, a per-strategy table, and recent risk events.
* **Charts:** NIFTY intraday as two views. The index view shows price, EMA 20 and the ORB high and low; the
  futures view shows price, VWAP and EMA 20. Futures trade at a basis to the index, so their VWAP is never
  drawn over index prices. Both views show entries and exits. There are also charts for cumulative P&L, P&L
  by strategy, drawdown and trade distribution, each with a table view. Colours were validated with the
  dataviz palette checker in light and dark mode.
* **Logs:** live events, alerts, orders, risk events, operator commands, and alert-channel status.
* **Models & approvals:** ML/RL model status, learning-loop candidates and pending approvals (read-only
  until Phases 7–9).

## Security

* Listens on `127.0.0.1` only, unless explicitly started with `--allow-remote` (see above).
* Login with `DASHBOARD_USERNAME` / `DASHBOARD_PASSWORD`. The comparison is constant-time, and 5 failed
  attempts lock the session for 5 minutes. The password is never shown, logged or kept in session state.
* Streamlit reconnects the **same browser tab** to its session after a quick reload. A new tab or browser
  always asks for the password.
* The published state contains no credentials (a test checks this), and command errors pass through the
  secret masker.

## QA (Playwright, PAPER, synthetic bot), 2026-11-04

| Check | Result |
|---|---|
| Dashboard loads; login required; wrong password rejected | pass |
| PAPER is the default; banner always visible | pass |
| LIVE: preconditions shown; pending orders disable the request | pass |
| LIVE: wrong text rejected, nothing queued, toggle back to PAPER | pass |
| LIVE: correct text → bot refuses, with every reason listed | pass |
| Kill switch visible → click → confirm → bot kills, strategies KILLED | pass |
| Named reset → kill cleared, both strategies re-activated, audited | pass |
| Strategy cards render | pass |
| Positions table renders; manual exit → confirm → closed (MANUAL), audited | pass |
| Every required metric renders; no secret on the page; no console errors | pass |

## Known limitations

* There is one dashboard account: the "named operator" is the configured username. Per-user accounts are
  needed before LIVE.
* The state snapshot reads funds from the broker every second. That is free for the paper broker; cache it
  before a real Upstox client is wired in.

* There is no production PAPER runner yet that connects Upstox market data to the bot. Until it exists, use
  the synthetic bot to exercise the dashboard. Connection and rate-limit status show "not connected" or
  "n/a" because no Upstox client is wired into the process.
* Trade exit and order update times are stamped with the real clock (Phase 4 audit warning 4). Under
  simulated time they can differ from the bot clock. Chart markers outside the plotted window are left out.
