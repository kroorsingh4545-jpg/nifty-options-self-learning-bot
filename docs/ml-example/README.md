# Example ML evaluation reports (SYNTHETIC)

> **SYNTHETIC DATA.** These reports were produced from a seeded random walk
> (`backtest/synthetic_data.py`), NOT real NIFTY prices. The models were registered in the research
> registry only, ended **REJECTED**, and can never be validated, promoted or used for trading.

Command used:

```bash
python -m ml train --synthetic --lot-units 65 --days 30 --start 2026-07-01 --strategy orb_options --kind all
```

| file | model |
|---|---|
| `signal_filter-report.md` / `.json` | ORB signal filter: P(target before stop), 5 seeds |
| `regime_classifier-report.md` / `.json` | trend + volatility regime classifier |

## How to read the signal-filter result

* **OOS AUC 0.74 on a random walk is not leakage.** The top features are the option premium, premium vs.
  spot, ATR, realised volatility, minutes to expiry and IV. On any price path, these set how likely a
  2R target (about +50% of premium) is to come before a 25% stop or the 60-minute time barrier. Cheap,
  near-expiry, high-volatility options reach it more often. The model ranks that geometry well.
* **It has no edge.** The random walk has no direction to learn. The few signals the filter lets through
  lose money (expectancy -₹370 per trade vs. +₹140 for taking every event), and on the holdout nothing
  passes the 0.60 threshold. The expectancy gates reject it, which is what they are for.
* Fold 0 has no training data (forward-only folds train only on the past), so it is skipped.
* Trading metrics are event-level and sized like the risk manager. An event that sizes to 0 lots (a premium
  too high for the ₹2,000 risk budget) is not traded. "PASS WITH WARNINGS" compliance means some
  stop-outs through gaps lost slightly more than ₹2,000, as the backtester reports too.

Each report lists the leakage checks that were applied.
