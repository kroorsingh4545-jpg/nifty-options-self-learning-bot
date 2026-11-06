# Risk & Execution Audit Report

Date: 2026-11-06 (re-audit after round 2; updated for the lag-blocked square-off test)
Scope (files / PR): branch `market-data-fixes` vs `main` (22463cd), two rounds.

* **Round 1** (e6d334a): one candle per bucket, structured WebSocket status, price / feed-lag tiles, feed logging,
  APScheduler quieted. `data/candle_builder.py`, `data/market_data.py`, `data/websocket_manager.py`,
  `broker/upstox_websocket.py`, `runtime/paper.py`, `runtime/control.py`, `runtime/bot.py`, `core/scheduler.py`,
  `monitoring/json_logger.py`, `config/settings.py`, `config/settings.yaml`, `dashboard/*`.
* **Round 2**: (a) the suite can no longer read the real `.env` (`config/loader.py`, `tests/conftest.py`);
  (b) PC-clock pre-flight against Upstox server time, FAIL beyond 2 s (`broker/upstox_rest.py`, `core/clock.py`,
  `runtime/paper.py`, `runtime/__main__.py`); (c) in-session clock re-check with a dashboard warning
  (`runtime/paper.py`, `runtime/control.py`, `dashboard/*`); (d) new entries blocked while the NIFTY feed lag is
  above 5 s, exits never (`runtime/bot.py`). Docs: `README.md`, `docs/deployment.md`, `docs/strategies.md`,
  `docs/dashboard.md`.

No changes in `risk/`, `execution/` or `strategies/`.

Background: in the 2026-11-06 PAPER session (14:45–15:20) ticks arrived and decoded correctly, but:
(a) the dashboard's WebSocket tile compared the text `"CONNECTED (16 subscriptions)"` with `"connected"`, so it
always showed *offline*;
(b) the PC clock ran ~7.2 s ahead of NTP and `CandleBuilder.flush` closes buckets by the local clock, so about 8
"complete" candles per bucket reached the strategies;
(c) APScheduler logged about 4 INFO lines a second.

Mode audited: PAPER. LIVE order routing does not exist in this build.
Environment: project `.venv` (Python 3.13.1), mypy 2.4.1 (pinned).
Tests: **1268 passed, 0 failed** (1 pytest warning, a NumPy deprecation in `test_ml_training.py`); `ruff check .` and `mypy .` (strict, 133 files) clean. The round-1 failure
(`test_dashboard_app.py::test_login_required_and_unconfigured_refuses`, caused by the developer `.env`) now passes.
Verdict: **PASS WITH WARNINGS**

## Results

| # | Area | Status | Evidence (file:line, test) | Notes |
|---|---|---|---|---|
| 1 | Entry logic | OK | `runtime/bot.py:341-368` (lag gate, `entry_block_reason`), `:229` (`block_fn=self.entry_block_reason`); `runtime/contracts.py:99-101`; `core/engine.py:162`, `:186`; `data/candle_builder.py:126-135`, `:154-158`; `test_bot_integration.py:272`, `:286`; `test_candle_builder.py:138`, `:153` | Entries still pass the unchanged risk gateway. A NIFTY feed lag above ±5 s (`config/settings.py:182`) refuses every new entry and gives the reason. 4 s does not block. Each candle bucket is now published once. No lag reading yet (`None`) means no lag block; without a spot price the resolver refuses anyway (`runtime/contracts.py:103-105`) |
| 2 | Exit logic | OK | `execution/stack.py:127-128`; `execution/position_manager.py:437-458`; `test_bot_integration.py:296` (`test_feed_lag_never_blocks_exits`), `:316` | The lag gate is only consulted by contract resolution, which is entry-only. With entries blocked by a 9 s lag, a stop-loss exit (`:296`) and the mandatory square-off (`:316`) both still close the trade |
| 3 | Stop-loss logic | OK | `test_bot_integration.py:296` | Unchanged and tick-driven. Proven to still fire while entries are lag-blocked |
| 4 | Take-profit logic | N/A | not in diff | tick-driven, unchanged |
| 5 | Trailing-stop logic | N/A | not in diff | unchanged |
| 6 | Position sizing | N/A | not in diff | unchanged |
| 7 | Daily loss limits | N/A | not in diff | unchanged |
| 8 | Global loss limits | N/A | not in diff | unchanged |
| 9 | Margin checks | N/A | not in diff | unchanged |
| 10 | Order lifecycle handling | N/A | not in diff | unchanged |
| 11 | Partial fills | N/A | not in diff | unchanged |
| 12 | Order rejection handling | N/A | not in diff | unchanged |
| 13 | Reconciliation | N/A | not in diff | unchanged |
| 14 | Square-off logic | OK | `execution/square_off.py:105` (unchanged, never consults the entry block); `runtime/__main__.py:132-152`; `test_paper_launcher.py:420`; `test_bot_integration.py:316` (`test_square_off_closes_everything_while_entries_are_lag_blocked`) | The test opens the ORB trade, lags the feed 9 s so new entries are blocked, then runs square-off at 15:10 while the block is still active: the trade closes with `SQUARE_OFF` and no position is left. Square-off still runs on the PC clock, but a session can no longer start with that clock more than ±2 s off Upstox server time: `paper` refuses on a FAIL. Drift during a session is re-checked and shown (area 19) |
| 15 | Kill-switch behavior | OK | `risk/risk_manager.py:244-245`; `broker/upstox_rest.py:276`; `runtime/paper.py:314`, `:365-394`; `monitoring/alerts.py:98` | The lag gate only adds a block and can't lift the kill switch. The in-session clock probe uses the session REST client: a 401 still trips `AUTH_FAILURE` as before. A failed measurement is logged, never raised, and trips nothing. Lag and drift WARNINGs do not page Telegram (alerts start at ERROR) |
| 16 | API rate-limit compliance | OK | `broker/upstox_rest.py:235-240` (scope check, then `limiter.acquire`), `:384-427`; `test_upstox_rest.py:324` (3 calls granted by the limiter, market-data path only) | Clock measurement = 3 STANDARD LTP calls, once per `check`/`paper` and once per 900 s in a session (`config/settings.py:152`, min 60 s). That is far inside the 400/min budget |
| 17 | PAPER/LIVE isolation | OK | `broker/upstox_rest.py:235`; `runtime/paper.py:120`, `:268`; `test_paper_launcher.py:247` | The probe calls `/v3/market-quote/ltp` only, which an analytics token is allowed to call. No order path is touched |
| 18 | Restart recovery | OK | `data/candle_builder.py:82`; `runtime/bot.py:174` | `_closed_until` and `_lag_blocked` are in memory and rebuilt from live ticks after a restart. No persisted state changed |
| 19 | Logging and audit trail | OK | `runtime/bot.py:351-357` (block / unblock WARNINGs), `runtime/paper.py:383-389`; `runtime/control.py:335`, `:350-351`; `dashboard/components/control_bar.py:87-97`, `:124`; `tests/conftest.py:48-66`; `test_env.py:127`; `test_paper_launcher.py:449`, `:471`; `test_dashboard_units.py:207` | Lag block and recovery are logged at WARNING. The dashboard shows the block reason, a PC-clock tile and a drift warning. The pre-flight detail and failures carry the exception type only, never the token. `on_response` reads only the `Date` header. Tests can no longer read the real `.env`: there is no default, and an explicit path is refused |

## Blocking issues

None.

## Warnings

1. **Measurement resolution.** The HTTP `Date` header has 1 s resolution. The estimate is accurate to
   ±(0.5 s + RTT/2) and is shown in the detail. The best of 3 samples is used, but an offset within about
   0.6 s of the 2 s limit can be misjudged either way. Today's 7.2 s offset is detected without doubt.
2. **The pre-flight fails closed.** If Upstox server time can't be read (network down, bad token, no
   credentials), `check` FAILs and `paper` refuses (`runtime/paper.py:170-184`, `runtime/__main__.py:132-136`).
   This is intended: without Upstox there is no market data anyway.
3. **The pre-flight gate lives in the CLI.** `python -m runtime paper` (and Docker, `docker-compose.yml:37`)
   are gated. A `PaperSession` started from code without `main()` gets only the in-session `ClockMonitor` warning
   (`runtime/paper.py:547-549`).
4. **In-session drift only warns.** As specified, it does not block. But a fast clock also raises the feed lag,
   so a drift above 5 s blocks entries through the lag gate anyway.
5. **The lag gate reacts at the speed of a median.** It uses the median of the last 30 NIFTY ticks
   (`data/market_data.py:25`, `:82-89`), so it engages or clears after roughly 15 ticks (about 15 s at one tick
   a second). If the feed goes silent, the last reading persists, which is conservative; stale prices also block
   entries through the quote-freshness checks.
6. **Square-off and the entry cut-off follow the PC clock.** This code is unchanged; it is now gated at start
   (area 14) and watched during a session.

## Required tests missing

1. No end-to-end test of `MarketDataStream` → `MarketDataService` → `CandleBuilder` → engine with a skewed
   clock. The parts are tested separately (`test_candle_builder.py:153`, `test_bot_integration.py:272`).
2. No test of `UpstoxRestClient.measure_clock_offset` against the real Upstox host. A one-off manual
   `python -m runtime check` on the PC is the live check.
