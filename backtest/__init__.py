"""Backtesting (Phase 6): the real PAPER bot replayed on historical candles.

* ``backtester``        event-driven replay on simulated time (SimClock)
* ``cost_model``        Indian F&O charges, spread and slippage, cost attribution
* ``data``              historical dataset format, loading and validation
* ``synthetic_data``    SYNTHETIC sample-data generator (tests and demos only)
* ``metrics``           performance, cost-impact and risk-compliance metrics
* ``report_generator``  JSON / Markdown / CSV reports
* ``walk_forward``      chronological walk-forward splits and runner
* ``purged_cv``         purged K-fold cross-validation with embargo

The backtester is a composition root like ``runtime/`` (it builds a whole
PAPER bot), so it is the one package besides ``runtime`` allowed to import it.
It never imports the Upstox REST/WebSocket adapters.
"""
