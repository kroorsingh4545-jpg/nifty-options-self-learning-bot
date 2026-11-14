"""Isolated RESEARCH layer (volatility intelligence, forecast-vs-reality).

Rules enforced by ``tests/unit/test_research_isolation.py``:

* Research reads market data and the trading database; it never places,
  modifies or cancels orders, never imports the execution / risk / runtime /
  paper-broker code, and never changes strategy parameters, risk limits or
  any file under ``config/``.
* The trading database is opened read-only only (``research.trading_readonly``).
  Research writes only to its own database (``research.store``,
  ``data/research.db`` by default), which can never be the trading database.
* Network access goes only through the existing Upstox adapter, built in the
  composition root ``research.__main__`` with its own, smaller share of the
  rate-limit budget. Everything else receives a read-only source
  (``research.sources.ResearchSource``).
* Findings that suggest a strategy change are PROPOSALS only. Nothing here
  applies them.
"""
