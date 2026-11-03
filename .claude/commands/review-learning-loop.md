---
description: Review the Reflective Loop — reflections, hypotheses, mutations, validation, promotion candidates
---

# Review the learning loop

Loop: `LOG → REFLECT → HYPOTHESIZE → MUTATE → VERIFY → PROMOTE / REJECT`

Use the `ml-rl-trading-safety` Skill.

Review and summarize:
1. **Reflection reports** — what the logs say went well and badly.
2. **Hypotheses** — each proposed improvement and its reasoning.
3. **Mutations** — exact parameter or logic changes for each candidate.
4. **Validation results** — backtest, walk-forward, out-of-sample, stress test, and comparison with production. Mark each gate PASS/FAIL.
5. **Promotion candidates** — candidates that passed every gate.

For each candidate, recommend: PROMOTE TO PAPER, KEEP TESTING, or REJECT, with reasons.

## Rules

- **Never auto-promote a candidate to LIVE mode.**
- Promotion to PAPER also waits for my approval.
- Every promotion must be versioned and reversible; state the rollback version.
