---
description: Run the risk-and-execution-auditor Skill and produce a structured audit report
argument-hint: [files, folder, or PR to audit]
---

# Risk & execution audit

Scope: $ARGUMENTS (if empty, audit everything in `risk/`, `execution/`, `broker/`, and `strategies/`).

1. Use the `risk-and-execution-auditor` Skill.
2. Read the code and tests in scope. Do not modify any files during the audit.
3. Assess all 19 audit areas.
4. Produce the structured report exactly in the Skill's report format, with file:line evidence.
5. Give the verdict: **PASS**, **PASS WITH WARNINGS**, or **FAIL**.
6. If FAIL: list every blocking issue and state clearly that merge/deployment must not proceed.
7. Stop and wait for my instructions.
