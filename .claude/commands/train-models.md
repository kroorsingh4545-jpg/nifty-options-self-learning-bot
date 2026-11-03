---
description: Train or retrain ML signal filters / PPO RL agents with leakage-safe validation and versioning
argument-hint: [model or strategy name]
---

# Train or retrain models

Target: $ARGUMENTS

Use the `ml-rl-trading-safety` Skill. All of these are mandatory:

1. **Confirm inputs** — ask me for the model type (ML filter or PPO RL), strategy, data range, and hyperparameters if not given.
2. **Point-in-time features** — verify every feature is point-in-time; check for lookahead bias and target leakage. Report what you checked.
3. **Walk-forward validation** — chronological windows with purging and embargo. No random shuffling.
4. **Multiple seeds** — at least 5 seeds; report mean and spread.
5. **RL boundaries** — confirm the action space only contains allowed actions and is clipped to risk limits.
6. **Model versioning** — save feature schema, training window, hyperparameters, environment version, evaluation report, and model artifact path.
7. **Evaluation report** — all required metrics, including transaction-cost-adjusted expectancy and risk-limit compliance.
8. **Comparison** — compare with the current production model.

Never deploy the result. New models go to PAPER only, and only after I approve.
