# Risk & Execution Audit Report

Date: 2026-11-06
Scope (files / PR): branch `market-data-fixes` vs `main` (22463cd); 24 files, +502/−23:
`data/candle_builder.py`, `data/market_data.py`, `data/websocket_manager.py`, `broker/upstox_websocket.py`,
`runtime/paper.py`, `runtime/control.py`, `runtime/bot.py`, `core/scheduler.py`, `monitoring/json_logger.py`,
`config/settings.py`, `config/settings.yaml`, `dashboard/data_access.py`, `dashboard/components/control_bar.py`,
`dashboard/app.py`, `docs/dashboard.md`, `docs/strategies.md`; tests in `tests/unit/test_candle_builder.py`,
`test_market_data.py`, `test_upstox_websocket.py`, `test_websocket_manager.py`, `test_logging.py`,
`test_dashboard_units.py`, `test_dashboard_app.py`, `test_paper_launcher.py`. No changes in `risk/`, `execution/`
or `strategies/`.

Background: the 2026-11-06 PAPER session (14:45–15:20) received and decoded ticks correctly, but (a) the dashboard
WebSocket tile compared the text `"CONNECTED (16 subscriptions)"` to `"connected"` and always showed *offline*;
(b) the PC clock ran ~7.2 s ahead of NTP (Windows Time service stopped), and `CandleBuilder.flush` (local clock)
deleted the working candle, so later ticks of the same exchange-time bucket opened a new one: ~8 "complete"
candles per bucket reached the strategies; (c) APScheduler logged ~4 INFO lines per second.

Mode audited: PAPER. LIVE order routing does not exist in this build.
Environment: project `.venv` (Python 3.13.1), mypy 2.4.1 (pinned).
Tests: **1254 passed, 1 failed** (1 pytest warning, not inspected). The failure,
`test_dashboard_app.py::test_login_required_and_unconfigured_refuses`, also fails on `main` with this branch's
changes stashed: it is environmental (see Warning 5), not caused by this branch; `ruff check .` and `mypy .` (strict, 133 files) clean.
Verdict: **PASS WITH WARNINGS**

## Results

| # | Area | Status | Evidence (file:line, test) | Notes |
|---|---|---|---|---|
| 1 | Entry logic | OK | `data/candle_builder.py:126-128` (published-bucket guard), `:135`, `:154-158`; `core/engine.py:121-133`; `test_candle_builder.py:138`, `:153` | Entries still pass the unchanged risk gateway. The change removes the duplicate / fragment candles that strategies (ORB range, VWAP+EMA) were receiving: each bucket is now published once per timeframe |
| 2 | Exit logic | OK | `execution/stack.py:127-128`; `execution/position_manager.py:437-458` | Stops / targets / trailing run on every tick, not on candles; unaffected by the candle change |
| 3 | Stop-loss logic | N/A | not in diff | tick-driven, unchanged |
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
| 14 | Square-off logic | WARN | `execution/square_off.py:105` (local clock) | Unchanged code, but it runs on the PC clock: a clock ~7 s fast starts the 15:10–15:20 window ~7 s early (safe direction). The new feed-lag check (`runtime/paper.py:294-339`) now makes such an offset visible |
| 15 | Kill-switch behavior | OK | `data/websocket_manager.py:81-85` unchanged; `runtime/paper.py:393` (`on_stream_state` wiring unchanged); `monitoring/alerts.py:98` (alerts from ERROR) | Feed state still drives the kill switch. The new lag WARNING is informational: it neither trips the kill switch nor pages Telegram |
| 16 | API rate-limit compliance | OK | `runtime/paper.py:264-268`; `data/market_data.py:82-99` | No new REST calls: lag and quote status come from the tick cache; subscription requests are WebSocket frames (unchanged chunking, `broker/upstox_websocket.py:336-348`) |
| 17 | PAPER/LIVE isolation | OK | `runtime/paper.py:120`, `:268` (`enable_portfolio=False`), `:372`; `test_paper_launcher.py:247` (`test_upstox_connections_are_read_only`) | No order path touched; read-only status fields only |
| 18 | Restart recovery | OK | `data/candle_builder.py:82` | `_closed_until` is in-memory and starts empty: after a restart the current bucket may form from the restart onward (same as before); no persisted state changed |
| 19 | Logging and audit trail | OK | `broker/upstox_websocket.py:236-238`, `:346-348`, `:371`, `:374-378`; `runtime/paper.py:314-339`; `monitoring/json_logger.py:141`, `:180-181`; `core/scheduler.py:68`; `test_upstox_websocket.py:278`, `test_logging.py:113`, `test_paper_launcher.py:360` | Subscriptions, first ticks after connect, market-status changes, decode-error counts and a 60 s feed summary are now logged; APScheduler held at WARNING (never below root level); jobs carry their ids. No secrets added to any log line (instrument keys and counts only) |

## Blocking issues

None.

## Warnings

1. **The PC clock must be synced before the next session.** The flush grace (`websocket.candle_flush_grace_seconds`,
   default 2 s, `config/settings.py:167`) is smaller than the 7.2 s offset measured on 2026-11-06. With that offset
   the guard prevents duplicate candles, but a bucket can be closed by the timer ~5 s early in exchange time when no
   tick of the next bucket has arrived yet, and the last seconds of ticks for it are then dropped as late
   (counted in `late_ticks_dropped`). Run `net start w32time` and `w32tm /resync` (admin), or raise the grace.
2. **High feed lag only warns.** `FeedHealthMonitor` (`runtime/paper.py:294-339`) logs a WARNING and the dashboard
   shows a banner, but new entries are not blocked. Gating entries on lag is a policy decision left to the owner.
3. **Square-off and entry cut-offs follow the PC clock** (area 14). Not changed here; an unsynced clock shifts them.
4. **Feed lag is measured on the NIFTY index only.** For options, `exchange_time` is the last trade time, so lag means
   nothing there (`data/market_data.py:82-90` documents this); the dashboard shows the index lag only.

5. **Pre-existing test-isolation gap (not in this diff).** `test_login_required_and_unconfigured_refuses` deletes
   `DASHBOARD_USERNAME` / `DASHBOARD_PASSWORD` from the process environment, but the dashboard's `load_config()`
   also reads the project `.env` (`config/loader.py:94-95`). With the dashboard login set in `.env` (as it is now)
   the test cannot observe the "not configured" state and fails; it passes on a checkout without those `.env`
   entries. The test should load config with `use_default_env_file=False` (or point at an empty env file). The
   refusal itself (`dashboard/auth.py:43-46`) is unchanged.

## Required tests missing

1. No end-to-end test drives the real `MarketDataStream` → `MarketDataService` → `CandleBuilder` → engine chain
   with a skewed local clock; the skew is covered at the `CandleBuilder` level
   (`test_candle_builder.py:153`) and the lag check at the `FeedHealthMonitor` level (`test_paper_launcher.py:360`).
2. The Streamlit app test checks that the new "NIFTY index", "NIFTY future" and "Feed lag" metrics render
   (`test_dashboard_app.py`), but not the feed-lag warning banner in a running app.
