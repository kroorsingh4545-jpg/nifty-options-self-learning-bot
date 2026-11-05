# Backtesting (Phase 6)

The backtester replays historical 1-minute candles through **the same PAPER bot that trades live**
(`runtime.TradingBot`): candle builder → ORB / VWAP+EMA strategies → ML filter interface → contract
resolver → `RiskManager` → `PositionManager` / `OrderManager` → `PaperBroker` fills and charges →
stops, targets, trailing, daily loss guard, kill switch, reconciliation and the 15:10 square-off.
Nothing is re-implemented for the backtest, so a backtest exercises every risk control.

It never connects to Upstox and never writes to the trading database (an in-memory SQLite database is
used by default; the configured `data/trading.db` is refused).

## Quick start

```bash
# SYNTHETIC demo data (random walk, clearly labelled in the report). --lot-units is the contract size
# written into the synthetic instrument list; it has no default.
python -m backtest synthetic --lot-units 65 --days 20 --seed 7 --out reports/synthetic

# Your own data
python -m backtest run --data path/to/dataset --out reports/run1
python -m backtest run --data path/to/dataset --out reports/run1 --strategies orb_options --slippage-ticks 2

# Walk-forward validation (rolling 10-day train, 1-day embargo, 5-day test, 5-day untouched holdout)
python -m backtest walk-forward --data path/to/dataset --train 10 --test 5 --embargo 1 --holdout 5 --out reports/wf
```

Each run writes `report.md`, `report.json` and `trades.csv`. The exit code is 2 if the risk-compliance
check FAILS. An example on synthetic data is in [`docs/backtest-example/report.md`](backtest-example/report.md).

## Dataset format

```
<dir>/instruments.json   Upstox instrument-list records (NSE.json shape) for every contract in the period
<dir>/candles.csv        instrument_key,timestamp,open,high,low,close,volume,oi
<dir>/meta.json          {"source": "HISTORICAL", "description": "..."}
```

* `meta.json` states `"source"` explicitly. Backtests read a missing file as HISTORICAL, but
  `python -m ml train` refuses a dataset without it. A dataset labelled HISTORICAL that contains
  `SYNTH`-tagged instruments is always refused.
* `timestamp` is the candle **start** with a UTC offset (`2026-09-01T09:15:00+05:30`, the shape the Upstox
  historical-candle API returns). Naive timestamps are refused.
* 1-minute candles only, on whole minutes, inside 09:15–15:30, strictly increasing per instrument.
* Required: the NIFTY index (`NSE_INDEX|Nifty 50`). For VWAP+EMA: the current-month NIFTY future with
  volume. For trading: the weekly options around the money, with volume and OI.
* Lot size, tick size, freeze quantity and expiry come **only** from `instruments.json`.
* Every year in the data needs its NSE holidays in `config/settings.yaml` (`market_session.holidays`),
  otherwise the risk gate would block every entry; the backtester refuses such data up front, and data on a
  configured holiday or weekend is refused too.

`python -m backtest synthetic --lot-units 65 --save-data DIR --out reports/x` writes a dataset in this format,
which is a template.

## Risk scenarios covered by tests

`tests/unit/test_backtest_scenarios.py` runs the real bot through: a data gap at square-off (the exit
retries over simulated time, escalates to the kill switch, the stranded position is reported as a BREACH
and the persistent kill blocks the next day); a monthly futures roll; partial fills against shallow depth;
and a strategy daily-loss kill that blocks the rest of the day and is auto-cleared at the next day's open.

## How the replay works

| Topic | Behaviour |
|---|---|
| Clock | `SimClock`, injected as `now_fn` into strategies, risk, execution, broker, repositories and the event bus. It only moves forward. Every persisted timestamp (orders, fills, positions, trades, risk events, daily P&L, strategies) is simulated time. |
| Ticks | Each 1-minute candle becomes 4 ticks at +0/+15/+30/+45 s: open → low → high → close for an up-candle, open → high → low → close for a down-candle (a long option meets its stop before its target inside one candle). |
| Quotes | Options get a bid/ask from the spread model: half-spread = max(1 tick, 0.25 % of price), depth 20 lots per side (configurable `SpreadModel`). Option ticks are emitted before index/futures ticks at the same instant, so a contract quote is current when a candle closes. |
| Subscriptions | Only instruments the bot subscribed to (index, future, ATM ± `strike_band` strikes, held contracts) receive ticks, like the live feed. |
| Jobs | The bot's own job list (`TradingBot.schedule`) runs on simulated time as background tasks, like APScheduler: candle flush, stale-entry expiry, square-off check, reconciliation, strike recentering, and the 09:00 start-of-day (instrument list, new-day reset). A slow job is never overlapped and never blocks the feed. The dashboard control plane is skipped. |
| Waiting | `SimClock.sleep` is the bot's injected `sleep`. Exit retries and backoffs, order waits (`wait_terminal`) and the reconciliation debounce park until the replay has advanced simulated time, so a retry meets the ticks that arrived meanwhile (or their absence). Nothing waits on the wall clock. |
| Fills | The real `PaperBroker`: entry LIMIT at ask + buffer, exits MARKET at bid, resting SL-M protective stop, slippage ticks, partial fills against depth, no fills on stale quotes. |
| Costs | Brokerage (once per order), STT, exchange transaction charges, SEBI fee, stamp duty, GST from `config/settings.yaml` `costs`, plus spread and slippage. |

## No lookahead bias

* A candle exists for a strategy only once a tick from the next interval arrives (or the flush job runs at
  its end); every tick is time-stamped inside its own candle.
* Orders fill at the price at the moment they are placed, or later.
* A backtest only receives the days it runs; walk-forward windows get `dataset.slice(window)`.
* Tested: rewriting every price after a cutoff leaves every signal, entry and exit before the cutoff
  identical (`test_no_lookahead_future_prices_cannot_change_past_decisions`), and a 2-day run equals the
  first two days of a 3-day run (`test_backtest_is_deterministic`).

## Report

| Section | Contents |
|---|---|
| Header | Data source (**SYNTHETIC** data carries a warning banner), period, strategies, compliance verdict, config fingerprint |
| Summary | Per strategy and total: trades, win rate, net/gross P&L, charges, total return, average daily return, CAGR-like annualised return (252 days), Sharpe (daily, annualised, rf = 0), profit factor, expectancy, average win/loss, average R, max drawdown (₹ and %), average trade duration, exits by reason |
| Cost impact | P&L before costs, spread, slippage, brokerage, STT, exchange, SEBI, stamp, GST, total, cost per trade, costs as % of turnover and of P&L before costs |
| Risk compliance | Open-trade caps, trades/day, no entries after 14:45, entries inside the window, flat after square-off, closed by 15:20, stop on every trade (**BREACH = FAIL**); per-trade and daily loss overshoots through gaps (**WARN**) |
| Signal pipeline | Signals, approvals, most common rejection reasons per strategy |
| Daily / trades | Daily net P&L per strategy; every trade (also `trades.csv`) |
| Assumptions | Fill model, cost rates, spread model, capital, limitations |

## Walk-forward and purged CV

* `backtest.walk_forward.walk_forward_splits`: chronological train → embargo → test windows (rolling or
  anchored), non-overlapping tests, and a final **holdout** that no fold touches.
* `WalkForwardRunner`: evaluates parameter candidates (validated strategy overrides) on each train window,
  picks the best (ties → the earlier candidate, so the current config wins ties) and scores only that one
  on the test window. The holdout runs once, only with `--evaluate-holdout`.
* Candidates may change only entry/exit parameters, windows, strike selection, SL/TP/trailing and the
  reduce-only size multiplier. Capital, risk limits and ML settings are refused, and every candidate is
  re-validated against the hard limits.
* `backtest.purged_cv.PurgedKFold`: purged K-fold with embargo for labelled events (`t0` features known,
  `t1` label known); `forward_only=True` trains strictly on the past. Phase 7 uses it for the ML models.

## Limitations

* 1-minute candles with a fixed intra-candle path and synthetic quotes: no real order book, queue position
  or latency.
* Each run starts cold, so the VWAP+EMA EMA warms up from the first day of each run or window.
* Charges use the configured rates; verify them against the current Upstox/NSE charge sheets.
* Synthetic data is for tests and demos only. It says nothing about live performance.
