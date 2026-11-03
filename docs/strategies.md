# Strategies & Engine (Phase 4)

Strategies only produce **signals**: a direction (CE = bullish, PE = bearish, long options only) and a reason.
They never choose size, place orders, or touch the broker. Contract selection, the ML filter, risk checks,
sizing, stops and execution all happen downstream, as the engine flow below shows.

## Engine flow

```
Upstox WebSocket tick
   └─▶ MarketDataService ──┬─▶ PaperBroker.on_tick        (fills resting orders)
                           ├─▶ PositionManager.on_tick    (SL / TP / trailing, MTM)
                           ├─▶ CandleBuilder.on_tick ──▶ completed 1/3/5/15-min candles
                           │                                   │
                           └─▶ TradingEngine.on_tick           ▼
                                                   TradingEngine.on_candle
                                         (only while RUNNING and during market hours)
                                                               │
                                       strategy.on_candle(candle) ─▶ Signal
                                                               │
                        ┌──────────── single entry lock (no racing the trade caps) ─┐
                        │  SignalFilter.evaluate   ML-ready; pass-through until Phase 7;
                        │                          may veto or shrink size, never enlarge;
                        │                          an error means the entry is refused
                        │  ContractResolver        nearest expiry, ATM/ITM/OTM strike,
                        │                          liquidity filter on the live quote
                        │  EntryGateway            RiskManager gate ─▶ PositionManager
                        │                          ─▶ OrderManager ─▶ PaperBroker
                        └──────────────────────────────────────────────────────────────┘
                                                               │
                              strategy.on_entry_result(approved)   + ENTRY_DECISION event
```

## Strategy 1: Opening Range Breakout (`strategies/orb_options.py`)

```
09:15 ─────────── 09:30           09:30 ─────────────────────────────── 11:00
 1-min NIFTY candles               5-min NIFTY candles (timeframe_minutes)
 OR_high = max(high)                │
 OR_low  = min(low)                 ├─ close > OR_high + buffer ─▶ CE signal ─┐
 (range invalid if < 10 pts         ├─ close < OR_low  - buffer ─▶ PE signal ─┤
  or > 1.5 % of price               └─ close inside range ─▶ re-arm both      │
  → no trades that day)                                                       ▼
                                           only on the transition inside → outside;
                                           at most 1 approved entry per direction per day
```

## Strategy 2: VWAP + EMA momentum (`strategies/vwap_ema.py`)

The signal instrument is **NIFTY futures**, because the index has no traded volume and VWAP needs volume.
Set `instruments.nifty_futures_key` in `config/settings.yaml`. If it is empty, this strategy is disabled at
startup with a CRITICAL log.

```
each completed 5-min (or 3-min) futures candle
   update session VWAP (resets daily), EMA20 (seeded via warm_up), rolling volume
   │
   ├─ EMA not warmed up ───────────────────────────────▶ no signal
   ├─ last N closes > VWAP & > EMA, rising, EMA slope > 0 ─▶ bullish setup
   ├─ last N closes < VWAP & < EMA, falling, EMA slope < 0 ─▶ bearish setup
   │
   ├─ outside 09:45–14:30 ─────────────────────────────▶ no signal
   ├─ VWAP crosses in last 12 bars > 4 (choppy) ───────▶ no signal
   ├─ volume < 1.2 × rolling average (low volume) ──────▶ no signal
   └─ otherwise CE (bullish) / PE (bearish), once per setup (re-arms when the setup breaks)
```

## Example signal (from the synthetic ORB day in `tests/unit/test_bot_integration.py`)

The data is **synthetic**: these are not real NIFTY prices.

```json
{
  "signal_id": "SIG-orb_options-e11cac8e03ac",
  "strategy_id": "orb_options",
  "strategy_version": "0.1.0",
  "option_type": "CE",
  "generated_at": "2026-11-03T09:40:00+05:30",
  "candle_end": "2026-11-03T09:40:00+05:30",
  "underlying_key": "NSE_INDEX|Nifty 50",
  "underlying_price": 24528.75,
  "reason": "ORB breakout: close 24528.75 > high 24518.00 + 5.0",
  "features": {
    "or_high": 24518.0,
    "or_low": 24482.0,
    "or_width": 36.0,
    "close": 24528.75,
    "breakout_in_ranges": 0.298611
  }
}
```

The engine's entry decision for that signal, followed by the resulting PAPER trade:

```json
{"signal_id": "SIG-orb_options-e11cac8e03ac", "strategy_id": "orb_options", "approved": true,
 "reasons": [], "trade_uid": "PAPER-T-orb_options-…", "quantity": 75, "ml_score": null}

{"contract": "NIFTY 24550 CE 10 NOV 26", "qty": 75, "avg_entry": 100.1, "stop": 75.05, "target": 150.05}
```

## Session jobs (`runtime/bot.py` via `core/scheduler.py`, APScheduler, IST)

| Job | Every | Purpose |
|---|---|---|
| `candle_flush` | 1 s | close candles whose interval ended without a further tick |
| `expire_entries` | 5 s | cancel entry orders unfilled after 30 s |
| `square_off` | 5 s | mandatory 15:10–15:20 flatten, with escalation |
| `recenter` | 30 s | keep ATM ± 3 strikes (CE and PE) subscribed; held contracts are never dropped |
| `reconcile` | 300 s | periodic broker reconciliation |
| `new_day` | 09:00 Mon–Fri | clear kill events and PAUSED/KILLED statuses from **previous** days only |

## Engine states

`STOPPED → RUNNING ⇄ PAUSED`. Pause and stop only halt **new entries**. Open positions keep their protective
stops, targets, trailing stops, kill switch and square-off.
