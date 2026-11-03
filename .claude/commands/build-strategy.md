---
description: Build a new isolated NIFTY options strategy module (asks for rules first)
argument-hint: [strategy-name]
---

# Build a new strategy

Strategy name (if given): $ARGUMENTS

Use the `nifty-bot-architect`, `ml-rl-trading-safety`, and `risk-and-execution-auditor` Skills.

## Step 1 — Ask before building

Ask me for each of these and wait for my answers. Do not invent values.

1. Strategy name (snake_case)
2. Entry rules
3. Exit rules
4. Strike-selection rules (ATM/OTM offset, CE/PE, weekly expiry handling)
5. Risk parameters:
   - Capital (default ₹1,00,000)
   - Stop-loss, take-profit, trailing-profit rules
   - Per-strategy daily loss limit
   - Max positions / max lots
6. Backtest requirements (date range, costs, slippage, walk-forward windows)

## Step 2 — Plan

Produce the five required sections from `nifty-bot-architect` (plan, file list, data flow, risk checklist, test plan). Wait for my approval.

## Step 3 — Build (after approval only)

Create:
- `strategies/<name>/` strategy module (signals only — no broker or execution imports)
- Pydantic configuration schema for the strategy
- Tests in `tests/strategies/test_<name>.py`
- Documentation in `docs/strategies/<name>.md`

The strategy must run in PAPER mode only until it passes validation and I approve it.

## Step 4 — Audit

Run `/audit-risk` on the new strategy and show the verdict.
