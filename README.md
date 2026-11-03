# nifty-options-self-learning-bot

A NIFTY weekly options intraday trading bot for Indian markets, using the Upstox Developer API.

> **Status: workspace setup only.** No trading, strategy, ML, or RL code exists yet.

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
