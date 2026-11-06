---
description: Start and monitor a PAPER trading session after pre-flight checks
---

# Run a PAPER trading session

PAPER mode only. Never switch to LIVE from this command.

## Pre-flight checks (all must pass before starting)

Run `python -m runtime check --connect` first: it covers PAPER mode, no LIVE routing, Upstox credentials and
token, the holiday calendar, the database, risk limits, Telegram and dashboard login, and lists the strategy
version each strategy will trade. Then confirm:

1. **PAPER mode** — confirm `TRADING_MODE=PAPER` and that the launcher (`runtime/paper.py`) wires `PaperBroker`. If anything says LIVE, stop.
2. **Market session** — confirm today is an NSE trading day and the time is within market hours (IST). Report current session state.
3. **WebSocket connection** — confirm the market-data WebSocket connects and ticks are arriving.
4. **Risk limits** — confirm per-strategy and global daily loss limits, max exposure, square-off time, and kill switch are loaded and active.
5. **Dashboard availability** — confirm the Streamlit dashboard is reachable and shows PAPER mode.

Show a pass/fail table for these five checks. If any fail, stop and tell me why.

## Start

Ask me to confirm, then start the session with `python -m runtime paper` (add `--paused` to start the
engine from the dashboard, `--stop-at 15:40` to end it after the close). Ctrl+C stops it in order.

## Monitor

Report periodically:
- Open positions and P&L per strategy
- Daily loss used vs limit
- WebSocket and rate-limit status
- Any errors, rejections, or risk events

At square-off time, confirm all PAPER positions are closed and summarize the session.
