---
description: Review the Reflective Loop — reflections, hypotheses, mutations, validation, promotion candidates
---

# Review the learning loop

Loop: `LOG → REFLECT → HYPOTHESIZE → MUTATE → VERIFY → PROMOTE / REJECT`

Use the `ml-rl-trading-safety` Skill.

Review and summarize:
1. **Reflection reports** — what the logs say went well and badly.
2. **Hypotheses** — each proposed improvement and its reasoning.
3. **Mutations** — exact parameter changes for each candidate (whitelisted parameters only; logic changes are never mutated).
4. **Validation results** — backtest, walk-forward, out-of-sample, stress test, multi-seed, comparison with production, and the no-trade baseline. Mark each gate PASS/FAIL, and state whether the evidence is HISTORICAL or SYNTHETIC.
5. **PAPER deployments** — candidates that passed every gate and were promoted to PAPER automatically.
6. **LIVE candidates** — PAPER versions nominated after the paper-validation period, waiting for my approval.

For each PAPER deployment, recommend: KEEP IN PAPER or ROLL BACK. For each LIVE candidate, recommend: APPROVE FOR LIVE, KEEP IN PAPER, or ROLL BACK. Give reasons.

## Rules

- At autonomy `PAPER_AUTO_IMPROVE` (the default), a candidate that passes every gate on HISTORICAL data is promoted to PAPER_READY and deployed to PAPER automatically. This is approved; it does not wait for me. SYNTHETIC evidence never promotes anything.
- **LIVE always needs my explicit approval.** Never auto-promote or deploy anything to LIVE, and never approve LIVE on my behalf.
- Every promotion must be versioned and reversible; state the rollback version.
