---
name: dashboard-builder
description: Use when building, changing, or testing the Streamlit dashboard for the NIFTY options bot — layout, P&L and risk displays, PAPER/LIVE mode control, kill switch, manual exits, and Playwright-based UI testing.
---

# Dashboard Builder

Apply this Skill to anything in `dashboard/`.

## 1. Technology

- Use **Streamlit** for version 1.
- The dashboard is UI only. It reads state from services and sends commands through them; it contains no trading or risk logic.

## 2. Mode control

- Show a **prominent PAPER / LIVE slider/toggle** at the top of every page.
- **Default to PAPER** on every start and every new session.
- Switching to LIVE requires the user to type exactly: `ENABLE LIVE TRADING`.
- Even after typed confirmation, LIVE is only enabled if the backend confirms all LIVE preconditions in `docs/safety-policy.md`. Otherwise show why it was refused.
- Make the current mode impossible to miss (e.g. large colored banner).

## 3. Required displays

- Total capital
- Deployed capital
- Available funds
- Realized P&L
- Unrealized P&L
- Total P&L
- Daily loss used and remaining (per strategy and global)
- Open positions
- Strategy cards (one per strategy: capital, P&L, positions, risk budget, model version, status)
- Upstox connection status
- WebSocket status
- API rate-limit usage
- ML/RL model status
- Learning-loop candidates
- Pending approvals

## 4. Controls

- **Emergency Kill Switch** — always visible; one click plus confirmation; halts new orders and exits positions through the execution layer.
- **Manual exit button for every open position**, with confirmation.
- Controls call the execution/risk services; the dashboard never calls the broker directly.

## 5. Security

- Never show secrets, tokens, or API keys in the UI, error messages, or logs.
- Protect the dashboard with `DASHBOARD_USERNAME` / `DASHBOARD_PASSWORD` from the environment.

## 6. Testing

- Test dashboard changes in **PAPER mode** using the Playwright MCP server where possible.
- Check: page loads, PAPER is default, LIVE switch rejects wrong text, kill switch is visible, every required metric renders, no secret appears on the page.
