---
name: risk-and-execution-auditor
description: Use before merging or deploying any code that affects orders, positions, risk controls, the kill switch, or live trading in the NIFTY options bot. Produces a PASS / PASS WITH WARNINGS / FAIL audit report.
---

# Risk and Execution Auditor

Run this audit before any merge or deployment that touches `risk/`, `execution/`, `broker/`, `strategies/`, or LIVE-mode code.

## 1. Audit scope

Read the relevant code and tests, then assess each area:

| # | Area | What to check |
|---|---|---|
| 1 | Entry logic | Entries only after risk approval; no duplicate entries |
| 2 | Exit logic | Every position has a guaranteed exit path |
| 3 | Stop-loss logic | Always set; cannot be removed or widened beyond limits |
| 4 | Take-profit logic | Correct trigger and sizing |
| 5 | Trailing-stop logic | Only tightens, never loosens |
| 6 | Position sizing | Within strategy capital and lot-size rules |
| 7 | Daily loss limits | Per-strategy limit halts that strategy |
| 8 | Global loss limits | Global limit halts all strategies |
| 9 | Margin checks | Checked before order; insufficient margin handled |
| 10 | Order lifecycle handling | All states handled (open, filled, cancelled, rejected, unknown) |
| 11 | Partial fills | Position and P&L correct on partial fills |
| 12 | Order rejection handling | Rejections logged, alerted, never blindly retried |
| 13 | Reconciliation | Local state matches broker on start, after errors, periodically |
| 14 | Square-off logic | All positions closed before configured square-off time |
| 15 | Kill-switch behavior | Blocks new orders, exits positions, cannot be overridden by ML/RL |
| 16 | API rate-limit compliance | All calls through central limiter with 20% buffer |
| 17 | PAPER/LIVE isolation | No path from PAPER mode to real orders |
| 18 | Restart recovery | Safe restart mid-session; no lost or duplicated positions |
| 19 | Logging and audit trail | Every decision and order traceable; secrets masked |

## 2. Report format

```
# Risk & Execution Audit Report
Date:
Scope (files / PR):
Verdict: PASS | PASS WITH WARNINGS | FAIL

## Results
| # | Area | Status (OK / WARN / FAIL / N/A) | Evidence (file:line, test) | Notes |

## Blocking issues
1. ...

## Warnings
1. ...

## Required tests missing
1. ...
```

## 3. Verdict rules

- **PASS** — every area OK or N/A, with test evidence.
- **PASS WITH WARNINGS** — no blocking issues, but non-critical gaps.
- **FAIL** — any blocking issue. Any FAIL in areas 3, 7, 8, 15, or 17 is automatically blocking.

## 4. If FAIL

- List **every** blocking issue.
- **Do not proceed with merge or deployment.**
- Do not "fix and pass" in the same step — report first, then fix, then re-audit.
