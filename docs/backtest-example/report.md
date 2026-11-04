# Backtest report — synthetic-example (seed 7, 20 days)

> **⚠ SYNTHETIC DATA - generated random-walk prices, NOT real NIFTY data. These results say nothing about how the strategies would perform in the market.**

- Data: **SYNTHETIC** — SYNTHETIC random-walk data (seed 7, 20 days from 2026-09-01); NOT real NIFTY prices
- Period: 2026-09-01 → 2026-09-29 (20 trading days)
- Strategies: orb_options, vwap_ema
- Risk compliance: **PASS WITH WARNINGS**
- Config fingerprint: `2e2c82f42c911277` · generated 2026-11-04T17:56:08+05:30 · run time 66.2 s

## Summary

| Metric | Total | orb_options | vwap_ema |
|---|---:|---:|---:|
| Capital (₹) | 200,000.00 | 100,000.00 | 100,000.00 |
| Trades | 12 | 6 | 6 |
| Win rate | 58.33 % | 50.00 % | 66.67 % |
| Net P&L (₹) | -4,621.20 | -4,267.19 | -354.01 |
| Gross P&L (₹) | -3,864.25 | -3,909.75 | 45.50 |
| Charges (₹) | 756.95 | 357.44 | 399.51 |
| Total return | -2.31 % | -4.27 % | -0.35 % |
| Avg daily return | -0.12 % | -0.21 % | -0.02 % |
| CAGR-like (252 d) | -25.34 % | -41.94 % | -4.56 % |
| Sharpe (daily, ann.) | -6.84 | -5.01 | -0.68 |
| Profit factor | 0.50 | 0.27 | 0.90 |
| Expectancy / trade (₹) | -385.10 | -711.20 | -59.00 |
| Avg win (₹) | 659.10 | 526.27 | 758.73 |
| Avg loss (₹) | -1,846.98 | -1,948.67 | -1,694.46 |
| Avg R-multiple | -0.20 | -0.46 | 0.05 |
| Max drawdown (₹) | 4,962.78 | 4,904.31 | 2,544.93 |
| Max drawdown | 2.48 % | 4.90 % | 2.49 % |
| Avg trade duration (min) | 12.70 | 11.30 | 14.00 |
| Days with trades | 6 | 5 | 4 |
| Exits: STOP_LOSS | 4 | 3 | 1 |
| Exits: TRAILING_STOP | 8 | 3 | 5 |

## Cost impact (₹)

| Item | Total | orb_options | vwap_ema |
|---|---:|---:|---:|
| P&L before costs (at replayed price) | -3,256.50 | -3,640.00 | 383.50 |
| Spread | 477.75 | 204.75 | 273.00 |
| Slippage | 130.00 | 65.00 | 65.00 |
| Brokerage | 480.00 | 240.00 | 240.00 |
| STT | 120.06 | 46.11 | 73.95 |
| Exchange txn | 57.41 | 22.89 | 34.52 |
| SEBI fee | 0.21 | 0.09 | 0.12 |
| Stamp duty | 2.51 | 1.04 | 1.47 |
| GST | 96.76 | 47.31 | 49.45 |
| Total costs | 1,364.70 | 627.19 | 737.51 |
| Net P&L | -4,621.20 | -4,267.19 | -354.01 |
| Cost per trade | 113.73 | 104.53 | 122.92 |
| Costs % of turnover | 0.83 | 0.96 | 0.75 |
| Costs % of P&L before costs | 41.90 | 17.20 | 192.30 |

## Risk compliance: PASS WITH WARNINGS

| Check | Status | Detail |
|---|---|---|
| open trades per strategy | OK | none |
| open trades total | OK | none |
| trades per strategy per day | OK | none |
| no entries at/after 14:45 | OK | none |
| entries inside the strategy entry window | OK | none |
| flat after square-off | OK | none |
| every trade had a stop-loss | OK | none |
| closed by 15:20 | OK | none |
| loss per trade within the per-trade risk budget (gaps can overshoot) | WARN | PAPER-T-orb_options-9af33586d901: -2116; PAPER-T-orb_options-5631be3d468b: -2002; PAPER-T-vwap_ema-be2662e4b654: -2249 |
| daily loss per strategy within limit | OK | none |
| global daily loss within limit | OK | none |

## Signal pipeline

| Strategy | Signals | Approved | Most common rejections |
|---|---:|---:|---|
| orb_options | 35 | 6 | position size zero: risk budget # too small for one lot at #/unit (24); NIFTY # PE # SEP # SYNTH: premium # < # (3); cooldown after loss (# min left) (1); NIFTY # CE # SEP # SYNTH: premium # < # (1) |
| vwap_ema | 46 | 6 | position size zero: risk budget # too small for one lot at #/unit (26); NIFTY # PE # SEP # SYNTH: premium # < # (10); NIFTY # CE # SEP # SYNTH: premium # < # (4) |

## Daily net P&L (₹)

| Day | orb_options | vwap_ema | Total |
|---|---:|---:|---:|
| 2026-09-01 | 0.00 | 0.00 | 0.00 |
| 2026-09-02 | 0.00 | 0.00 | 0.00 |
| 2026-09-03 | 0.00 | 0.00 | 0.00 |
| 2026-09-04 | 0.00 | 0.00 | 0.00 |
| 2026-09-07 | -1,727.66 | 0.00 | -1,727.66 |
| 2026-09-08 | -1,451.86 | 1,020.09 | -431.77 |
| 2026-09-09 | 0.00 | 0.00 | 0.00 |
| 2026-09-10 | 0.00 | 0.00 | 0.00 |
| 2026-09-11 | 0.00 | 0.00 | 0.00 |
| 2026-09-15 | 277.36 | 0.00 | 277.36 |
| 2026-09-16 | 0.00 | 0.00 | 0.00 |
| 2026-09-17 | 0.00 | -1,078.56 | -1,078.56 |
| 2026-09-18 | 0.00 | 0.00 | 0.00 |
| 2026-09-21 | -2,002.15 | 565.99 | -1,436.16 |
| 2026-09-22 | 0.00 | 0.00 | 0.00 |
| 2026-09-23 | 0.00 | 0.00 | 0.00 |
| 2026-09-24 | 0.00 | 0.00 | 0.00 |
| 2026-09-25 | 0.00 | 0.00 | 0.00 |
| 2026-09-28 | 637.12 | -861.53 | -224.41 |
| 2026-09-29 | 0.00 | 0.00 | 0.00 |

## Trades (12)

| Strategy | Contract | Qty | Entry | Exit | Entry ₹ | Exit ₹ | Reason | Net ₹ | R |
|---|---|---:|---|---|---:|---:|---|---:|---:|
| orb_options | NIFTY 25050 PE 08 SEP 26 SYNTH | 65 | 2026-09-07T09:50 | 10:27 | 85.85 | 60.15 | STOP_LOSS | -1,727.66 | -1.20 |
| orb_options | NIFTY 25150 CE 08 SEP 26 SYNTH | 130 | 2026-09-08T09:45 | 09:48 | 49.15 | 54.75 | TRAILING_STOP | 664.33 | 0.46 |
| orb_options | NIFTY 25100 PE 08 SEP 26 SYNTH | 130 | 2026-09-08T10:55 | 11:02 | 40.55 | 24.70 | STOP_LOSS | -2,116.19 | -1.57 |
| orb_options | NIFTY 25000 CE 15 SEP 26 SYNTH | 130 | 2026-09-15T10:45 | 10:52 | 41.70 | 44.30 | TRAILING_STOP | 277.36 | 0.25 |
| orb_options | NIFTY 24850 CE 22 SEP 26 SYNTH | 130 | 2026-09-21T09:35 | 09:44 | 49.50 | 34.55 | STOP_LOSS | -2,002.15 | -1.21 |
| orb_options | NIFTY 25250 PE 29 SEP 26 SYNTH | 65 | 2026-09-28T09:35 | 09:37 | 85.35 | 96.10 | TRAILING_STOP | 637.12 | 0.51 |
| vwap_ema | NIFTY 25150 CE 08 SEP 26 SYNTH | 195 | 2026-09-08T09:45 | 09:48 | 49.15 | 54.75 | TRAILING_STOP | 1,020.09 | 0.57 |
| vwap_ema | NIFTY 24550 PE 22 SEP 26 SYNTH | 65 | 2026-09-17T13:45 | 14:16 | 137.50 | 156.60 | TRAILING_STOP | 1,170.83 | 0.70 |
| vwap_ema | NIFTY 24550 PE 22 SEP 26 SYNTH | 65 | 2026-09-17T14:00 | 14:37 | 145.95 | 112.35 | STOP_LOSS | -2,249.39 | -1.15 |
| vwap_ema | NIFTY 25050 CE 22 SEP 26 SYNTH | 130 | 2026-09-21T13:00 | 13:02 | 52.55 | 57.40 | TRAILING_STOP | 565.99 | 0.46 |
| vwap_ema | NIFTY 25150 PE 29 SEP 26 SYNTH | 65 | 2026-09-28T10:25 | 10:27 | 79.25 | 84.45 | TRAILING_STOP | 278.00 | 0.33 |
| vwap_ema | NIFTY 24850 PE 29 SEP 26 SYNTH | 130 | 2026-09-28T11:50 | 11:56 | 71.35 | 63.10 | TRAILING_STOP | -1,139.53 | -0.58 |

## Assumptions

- Fills: PaperBroker (same code as PAPER trading); 4 ticks per 1-minute candle (up: O-L-H-C, down: O-H-L-C); entry LIMIT at ask + entry_limit_buffer_ticks; exit MARKET at bid; protective stop resting SL-M at the stop (exchange-side).
- Costs: {"brokerage_per_order": 20.0, "stt_sell_pct": 0.15, "exchange_txn_pct": 0.03503, "sebi_fee_per_crore": 10.0, "stamp_duty_buy_pct": 0.003, "gst_pct": 18.0}; slippage 1 tick(s); spread {"half_spread_pct": 0.25, "min_half_spread_ticks": 1, "depth_lots": 20}; brokerage once per executed order (first fill).
- Capital: {"orb_options": 100000.0, "vwap_ema": 100000.0} (total ₹200,000).
- Limitation: Fills are simulated by the paper broker against prices replayed from 1-minute candles (4 ticks per candle); real order books, queue position and latency are not modelled.
- Limitation: Option bid/ask are synthesised by the spread model; historical quotes are not used.
- Limitation: Within a candle the path is open -> low -> high -> close (up-candle) or open -> high -> low -> close (down-candle); the true intra-minute order is unknown.
- Limitation: Each run starts cold: indicator warm-up (e.g. the VWAP+EMA strategy's EMA) is rebuilt from the first day.
- Limitation: Charges use the configured rates in config/settings.yaml; verify them against current Upstox/NSE sheets.
