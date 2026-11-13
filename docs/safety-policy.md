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
7. **Per-trade risk re-decided for LIVE** — the owner has decided the LIVE per-trade risk again and
   recorded it in section 8. The PAPER value (Rs 3,000) never carries over: the config refuses LIVE with
   a per-trade risk above Rs 2,000 (2 % of strategy capital, `MAX_RISK_PER_TRADE_RS_LIVE`).

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

## 8. Risk decisions (owner only)

Risk limits are changed only by the owner, recorded here with the date and the reason. The learning
loop, ML, RL and the dashboard can never change any risk limit (`learning/mutation_engine.py`
`NEVER_MUTABLE`; tests in `tests/unit/test_risk_3000_and_open_risk.py`).

| Date | Decision | Applies to | Reason |
|---|---|---|---|
| 2026-11-13 | Per-trade risk raised from Rs 2,000 to **Rs 3,000** (3 % of the Rs 1,00,000 strategy capital). `config/hard_limits.py` now allows Rs 3,000 for PAPER (`MAX_RISK_PER_TRADE_RS_PAPER`). Per-strategy daily loss stays **Rs 5,000** and global daily loss stays **Rs 10,000**. | PAPER testing phase only | On 2026-11-12 all three vwap_ema signals were refused by position sizing: one lot of the ATM option with the 20 % premium stop needed Rs 2,433 to Rs 2,627 of risk, above the Rs 2,000 budget, so the PAPER test produced no trades to learn from. |
| 2026-11-13 | LIVE per-trade risk stays at **Rs 2,000** (2 %, `MAX_RISK_PER_TRADE_RS_LIVE`). It must be re-decided by the owner before LIVE (section 4, item 7). | LIVE | The Rs 3,000 is for testing on simulated money only. |
| 2026-11-13 | **Open trades' stop-risk counts against the daily loss limits.** Before any new entry is approved, the worst-case loss of every open trade if its stop is hit (entries still filling counted in full) is treated as already lost, per strategy and globally. So the open trades plus the new one can never exceed Rs 5,000 per strategy or Rs 10,000 overall. | PAPER and LIVE | With Rs 3,000 per trade, two concurrent stop-outs (about Rs 2,900 each) could otherwise have exceeded the Rs 5,000 daily limit before the guard reacted. |
