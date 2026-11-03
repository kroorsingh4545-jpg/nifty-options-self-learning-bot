# Safety Policy

These rules apply to the bot, to every contributor, and to Claude. They cannot be overridden by ML/RL components, config changes, or chat instructions.

## 1. Claude's role

- Claude is a **development and analysis assistant, not an autonomous trader**.
- Claude **must not place, modify, or cancel real Upstox orders directly**.
- No MCP server or tool that can place, modify, or cancel real orders is connected to Claude.

## 2. Who may place real orders

- The bot's **execution layer** (`execution/`) is the **only** component permitted to place real orders.
- Strategies, ML, RL, the learning loop, and the dashboard request actions; they never call the broker's order APIs.

## 3. Default mode

- The bot always starts in **PAPER** mode (`TRADING_MODE=PAPER`).

## 4. LIVE mode requirements

LIVE mode may only be enabled when **all** of these are true:

1. **Explicit typed confirmation** — the user types `ENABLE LIVE TRADING`.
2. **Successful broker reconciliation** — local orders, positions, and funds match Upstox.
3. **Active kill switch** — the kill switch is wired, tested, and armed.
4. **Configured daily loss limits** — per-strategy and global limits are set and validated.
5. **Completed PAPER validation** — the strategy/model version has passed PAPER trading.
6. **Human approval** — a named human has approved this specific version for LIVE.

If any check fails, LIVE is refused and the reason is shown and logged.

## 5. Risk-control failures

- **Any risk-control failure must halt trading.** This includes: loss-limit breach, kill-switch fault, reconciliation mismatch, stale market data, token expiry, or repeated order rejections.
- Halting means: block new orders, alert the user, and exit positions per the configured policy.

## 6. Model and strategy promotions

- All model promotions must be **versioned, auditable, and reversible**.
- Every promotion records who approved it, when, what changed, the validation results, and the rollback version.
- No candidate is ever auto-promoted to LIVE.

## 7. Credentials

- API credentials must remain in **`.env` or a secret manager** — never in source code, config files committed to git, logs, chat, or the dashboard.
- `.env` is excluded by `.gitignore`. Only `.env.example` (empty placeholders) is committed.
- Secrets are masked in all logs.
