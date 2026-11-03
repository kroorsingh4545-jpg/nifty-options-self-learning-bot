---
name: nifty-bot-architect
description: Use when designing, scaffolding, restructuring, or reviewing the architecture of the NIFTY options intraday self-learning trading bot — module layout, service boundaries, strategy isolation, configuration, logging, and PAPER/LIVE separation.
---

# NIFTY Bot Architect

You are the architecture guardian for `nifty-options-self-learning-bot`. Apply this Skill whenever you create new modules, move code between modules, add services, or review the overall design.

## Project context

- Trades NIFTY weekly options, intraday only, via the Upstox Developer API.
- Runs multiple isolated strategies, each allocated ₹1,00,000 capital.
- Supports PAPER and LIVE modes. **PAPER is the default.**
- Each strategy has its own stop-loss, take-profit, and trailing-profit rules.
- Per-strategy and global daily loss limits are enforced.
- ML filters signals; a PPO-based RL agent manages trades within limits.
- A Reflective Loop improves strategies:
  `LOG → REFLECT → HYPOTHESIZE → MUTATE → VERIFY → PROMOTE / REJECT`.
- A Streamlit dashboard shows state and controls.

## 1. Modular architecture (mandatory)

Every piece of code belongs to exactly one of these top-level modules:

| Module | Responsibility |
|---|---|
| `data/` | Market data ingestion (WebSocket, historical), storage, point-in-time access |
| `core/` | Shared domain models, events, clock/session calendar, interfaces, DI container |
| `strategies/` | One sub-package per strategy; signal generation only |
| `ml/` | Feature pipelines, signal-filter models, PPO RL agent and environments |
| `learning/` | Reflective Loop: logs, reflections, hypotheses, mutations, verification, promotion |
| `options/` | Option chain, expiry handling, strike selection, Greeks/IV utilities |
| `risk/` | Position sizing, SL/TP/trailing rules, daily/global loss limits, kill switch, exposure |
| `broker/` | Upstox REST/WebSocket clients, auth/token handling, rate limiter, paper broker |
| `execution/` | Order manager, order lifecycle, fills, reconciliation, square-off |
| `backtest/` | Event-driven backtester, walk-forward runner, cost/slippage models |
| `dashboard/` | Streamlit UI only — no business logic |
| `monitoring/` | Structured logs, metrics, alerts (Telegram), health checks |
| `tests/` | Unit, integration, and PAPER end-to-end tests |
| `config/` | Pydantic settings models and config files |

Rules:
- Dependencies point inward: `dashboard → execution/risk/learning → core`. `core` imports nothing from other project modules.
- Strategies never import from `broker/` or `execution/`. They emit signals; `risk/` approves; `execution/` acts.
- Only `execution/` may call the broker's order methods.

## 2. Strategy isolation (mandatory)

Each strategy has its **own**:
- Capital ledger (₹1,00,000 starting allocation)
- Positions
- Realized and unrealized P&L
- Risk budget and daily loss limit
- ML/RL model state and version

A bug, loss, or crash in one strategy must not change another strategy's capital, positions, or limits. The global daily loss limit sits *above* all strategies and can halt everything.

## 3. Dependency injection

Inject these services through interfaces defined in `core/` — never instantiate them inside business logic:
- Broker (`UpstoxBroker` for LIVE, `PaperBroker` for PAPER)
- Market data feed (live WebSocket, replay, or backtest feed)
- ML / RL model providers
- Risk manager
- Execution / order manager

This is what makes PAPER, LIVE, and backtest run the same strategy code.

## 4. Configuration

- Use Pydantic (`pydantic-settings`) models for every config section.
- Validate on startup; refuse to start on invalid config.
- Secrets come only from environment variables / `.env` (never hard-coded, never in YAML committed to git).
- `TRADING_MODE` defaults to `PAPER`. Any other value must be validated explicitly.

## 5. Logging

- Use structured logging (JSON lines; e.g. `structlog`).
- Every log line carries: timestamp (IST), mode (PAPER/LIVE), strategy id, order id where relevant.
- Mask API keys, secrets, and tokens before logging.

## 6. PAPER before LIVE

- Implement and test every feature in PAPER mode first.
- LIVE code paths are only enabled after PAPER validation passes (see `docs/safety-policy.md`).

## 7. Hard limits ML/RL can never override

ML and RL components must **never** be able to change or bypass:
- Stop-loss (they may only place it *within* configured limits)
- Daily loss limits (per-strategy and global)
- Maximum exposure
- Kill switch
- Square-off time
- Broker credentials

Enforce this structurally: these values live in `risk/` and `config/`, are read-only to `ml/` and `learning/`, and are checked again by `risk/` before every order.

## 8. Required output before writing any code

Before writing or changing code, always produce these five sections and wait for approval on non-trivial work:

1. **Implementation plan** — what will be built, in what order.
2. **File list** — every file to be created or changed, with its module.
3. **Data flow** — how data moves (e.g. WebSocket tick → feature → signal → ML filter → risk check → order → fill → P&L → log).
4. **Risk-control checklist** — which controls are touched and how they stay enforced.
5. **Test plan** — unit, integration, and PAPER tests that prove it works.

## 9. Review checklist

When reviewing architecture, flag:
- Cross-module imports that break the dependency rules
- Shared mutable state between strategies
- Services created inside business logic instead of injected
- Config read from raw `os.environ` instead of Pydantic settings
- Any path where ML/RL output reaches the broker without passing `risk/`
- Any LIVE path that is not guarded by mode checks
