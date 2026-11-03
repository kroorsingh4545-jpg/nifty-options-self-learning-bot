# Build Plan — NIFTY Options Intraday Self-Learning Bot

> Master build plan. Work proceeds phase by phase; each phase stops for human
> approval before the next begins. LIVE trading is never enabled by this plan.

Read and follow these project files before doing anything:

- .claude/skills/nifty-bot-architect/SKILL.md
- .claude/skills/upstox-api-guardian/SKILL.md
- .claude/skills/ml-rl-trading-safety/SKILL.md
- .claude/skills/dashboard-builder/SKILL.md
- .claude/skills/risk-and-execution-auditor/SKILL.md
- docs/safety-policy.md
- docs/mcp-setup.md
- .mcp.json
- .env.example
- .gitignore

You are now building the core NIFTY options intraday self-learning trading bot.

## Non-negotiable rules
- Default trading mode: PAPER.
- Do not enable LIVE trading.
- Do not place, modify, or cancel real Upstox orders.
- Do not hardcode API keys, tokens, or secrets.
- Do not bypass the rate limiter.
- Do not remove or weaken stop-losses, daily loss limits, kill switch, reconciliation, or square-off logic.
- Use official Upstox REST and WebSocket APIs only.
- Use Python 3.11+, FastAPI, Streamlit, SQLite, Pydantic, pandas, NumPy, pytest, and APScheduler.
- Use Stable-Baselines3 PPO later for RL; do not implement it in this first phase.
- Write tests before or alongside each core module.
- Run the test suite after every phase.
- Commit after every completed and tested phase using clear conventional commit messages.
- Work phase by phase and wait for my approval before starting the next phase.

## Capital and risk requirements
- Total capital: ₹2,00,000.
- Each strategy receives isolated virtual capital of ₹1,00,000.
- Strategies must not share capital, positions, P&L, risk budget, or model state.
- Maximum risk per trade: ₹2,000, i.e., 2% of strategy capital.
- Maximum daily loss per strategy: ₹5,000, i.e., 5%.
- Global maximum daily loss: ₹10,000, i.e., 5% of total capital.
- Maximum open trades per strategy: 2.
- Maximum total open trades: 4.
- Maximum trades per strategy per day: 4.
- Cooldown after a losing trade: 15 minutes.
- Maximum consecutive losses before strategy pause: 3.
- No new entries after 14:45 IST.
- Mandatory square-off window: 15:10–15:20 IST.
- On global daily-loss breach:
  - Cancel pending orders.
  - Exit all open positions safely.
  - Disable all strategies for the day.
  - Trigger kill switch.
  - Send Telegram and dashboard alerts.

## Upstox API requirements
Implement a central rate limiter with:
- Order APIs: 10 requests/second, 500/minute, 2,000/30 minutes.
- Other standard APIs: 50 requests/second, 500/minute, 2,000/30 minutes.
- 20% safety buffer below all official limits.
- Request queueing, batching, debouncing, exponential backoff, and HTTP 429 circuit breaking.
- One market-data WebSocket connection.
- One portfolio/order WebSocket connection.
- Token expiry handling.
- Automatic reconnect and resubscription.
- Full credential masking in logs.

## Build phases

### Phase 1: Foundation
Create:
- Complete Python package structure.
- Pydantic configuration system.
- `config/settings.yaml`, `config/risk_limits.yaml`, and `config/strategy_defaults.yaml`.
- `.env` loading with validation.
- Structured JSON logging.
- SQLite schema and data-access layer.
- Tables for:
  - strategies
  - orders
  - fills
  - positions
  - trades
  - daily_pnl
  - risk_events
  - feature_snapshots
  - model_runs
  - learning_events
- Separate paper and live identifiers in every trade/order record.
- pytest setup and initial tests.

Do not implement broker calls yet.

Deliverables:
- Working configuration loader.
- Working SQLite schema.
- Logger.
- Test suite.
- README section explaining setup.

Stop after Phase 1 and show:
- File tree
- Test results
- SQLite schema
- Next-phase plan

### Phase 2: Market data and Upstox adapter
Implement:
- `broker/broker_interface.py`
- `broker/upstox_rest.py`
- `broker/upstox_websocket.py`
- `broker/upstox_rate_limiter.py`
- `broker/paper_broker.py`
- `data/market_data.py`
- `data/websocket_manager.py`
- `data/candle_builder.py`
- `data/option_chain.py`

Requirements:
- Abstract broker interface for REST, WebSocket, orders, positions, funds, and instruments.
- Upstox REST adapter with authentication, retries, timeout handling, and rate limiting.
- Upstox WebSocket adapter with subscribe/unsubscribe, reconnect, heartbeat, and resubscription.
- Candle builder for 1-minute, 3-minute, 5-minute, and 15-minute candles.
- Option-chain fetcher with expiry, strike, premium, volume, OI, bid, ask, and spread fields.
- Liquidity filter using volume, OI, and bid-ask spread.
- Paper broker that consumes real market data but simulates fills with spread, slippage, brokerage, STT, exchange charges, GST, stamp duty, and SEBI charges.
- No real order placement in this phase.

Write unit tests using mocked Upstox responses and mocked WebSocket messages.

Stop after Phase 2 and show:
- File tree
- Test results
- Rate-limiter test coverage
- WebSocket reconnect test coverage
- Next-phase plan

### Phase 3: Risk and execution
Implement:
- `risk/risk_manager.py`
- `risk/position_sizer.py`
- `risk/stop_loss_manager.py`
- `risk/trailing_stop_manager.py`
- `risk/daily_loss_guard.py`
- `risk/kill_switch.py`
- `execution/order_manager.py`
- `execution/position_manager.py`
- `execution/reconciliation.py`
- `execution/square_off.py`

Requirements:
- Position sizing based on fixed rupee risk, option premium, stop-loss distance, and available margin.
- Individual trade SL, TP, and trailing stop.
- Trailing modes:
  - Premium percentage
  - Points
  - R-multiple
  - NIFTY structural level
- Local real-time monitoring using WebSocket ticks.
- Exchange-side protective stop orders where practical.
- Safe retry logic for failed exit orders.
- Full order lifecycle tracking:
  created, submitted, open, partially filled, complete, rejected, cancelled.
- Broker/local position reconciliation:
  - At startup
  - After every order update
  - Every 5 minutes
  - Before square-off
  - After square-off
- Kill switch triggers:
  - Daily loss breach
  - API failure
  - WebSocket disconnect
  - Reconciliation mismatch
  - Abnormal spread
  - Manual dashboard action
- Forced square-off between 15:10 and 15:20 IST.

Write extensive tests for:
- Position sizing
- Stop-loss triggers
- Target triggers
- Trailing-stop updates
- Daily loss limits
- Consecutive-loss pause
- Order rejection
- Partial fill
- Reconciliation mismatch
- Kill switch
- Square-off

Stop after Phase 3 and show:
- Test results
- Risk-control matrix
- Order lifecycle diagram
- Next-phase plan

### Phase 4: Strategies
Implement:
- `strategies/base_strategy.py`
- `strategies/orb_options.py`
- `strategies/vwap_ema.py`
- `core/engine.py`
- `core/scheduler.py`
- `core/event_bus.py`
- `core/state_manager.py`
- `core/mode_controller.py`

Strategy 1: Opening Range Breakout
- Build first 15-minute NIFTY range from 09:15 to 09:30 IST.
- CE entry on confirmed break above range high.
- PE entry on confirmed break below range low.
- Entry window: 09:30–11:00 IST.
- One entry per direction per day by default.

Strategy 2: VWAP + EMA Momentum
- Use 3-minute or 5-minute candles.
- CE entry when price is above VWAP and 20 EMA with momentum confirmation.
- PE entry when price is below VWAP and 20 EMA with momentum confirmation.
- Avoid low-volume or choppy conditions.

Each strategy must have:
- Isolated ₹1,00,000 capital.
- Configurable entry and exit rules.
- Strike-selection rules.
- Position-sizing rules.
- SL/TP/trailing rules.
- Daily trade cap.
- Cooldown rules.
- Strategy-level kill switch.
- Enable/disable state.

The engine must:
- Run only during Indian market hours.
- Use WebSocket ticks as the primary data source.
- Generate signals.
- Pass signals through ML-ready interfaces.
- Apply risk checks.
- Route approved orders to the execution layer.
- Maintain separate strategy state.

Write tests using synthetic candle and tick data.

Stop after Phase 4 and show:
- Strategy flow diagrams
- Test results
- Example signal JSON
- Next-phase plan

### Phase 5: Dashboard
Implement:
- `dashboard/app.py`
- `dashboard/auth.py`
- Dashboard components under `dashboard/components/`

Build a Streamlit dashboard with:

Control bar:
- PAPER / LIVE slider
- Default PAPER
- Start / Pause / Stop
- Emergency Kill Switch
- Upstox connection status
- WebSocket status
- Market session status
- Square-off countdown
- Autonomy level selector:
  - OBSERVE_ONLY
  - PAPER_AUTO_IMPROVE
  - LIVE_CANDIDATE_APPROVAL

Portfolio overview:
- Total capital
- Deployed capital
- Available funds
- Realized P&L
- Unrealized P&L
- Total P&L
- Daily loss used / remaining
- Open positions
- Trades today
- Win rate
- Profit factor
- Maximum drawdown

Strategy cards:
- Name
- Version
- Enabled/disabled
- Allocated capital
- Current mode
- P&L
- Daily loss usage
- Open trades
- Trades today
- Last signal
- ML confidence placeholder
- Regime placeholder
- Strategy kill switch

Live positions:
- Strategy
- Symbol
- Side
- Quantity
- Entry price
- LTP
- Unrealized P&L
- Stop-loss
- Target
- Trailing stop
- R-multiple
- Holding time
- Manual exit button

Risk panel:
- Global daily loss limit
- Per-strategy daily loss limit
- Per-trade risk
- Open exposure
- Margin usage
- API rate-limit usage
- WebSocket subscription count
- Recent risk events

Charts:
- NIFTY intraday chart with VWAP, EMA, ORB levels, entries, exits
- Cumulative P&L
- Strategy-wise P&L
- Drawdown
- Trade distribution

Logs:
- Live event stream
- Order log
- Risk events
- Telegram alert status

LIVE mode requirements:
- Typed confirmation: “ENABLE LIVE TRADING”
- Show broker connection, capital, active strategies, open positions, risk state, and model version.
- Do not allow mode change while orders are pending without reconciliation.
- Do not actually enable live order routing in this phase.

Use Playwright MCP to verify:
- Dashboard loads
- PAPER is default
- LIVE requires confirmation
- Kill switch is visible
- Strategy cards render
- Positions table renders

Stop after Phase 5 and show:
- Dashboard screenshots or Playwright verification results
- Test results
- Manual QA checklist
- Next-phase plan

### Phase 6: Backtesting
Implement:
- `backtest/backtester.py`
- `backtest/cost_model.py`
- `backtest/walk_forward.py`
- `backtest/purged_cv.py`
- `backtest/report_generator.py`

Requirements:
- Event-driven backtester using historical candles.
- Support ORB and VWAP+EMA strategies.
- Include realistic Indian costs:
  - Brokerage
  - STT
  - Exchange transaction charges
  - GST
  - SEBI charges
  - Stamp duty
  - Slippage
- Chronological walk-forward splits.
- Purged cross-validation with embargo.
- No lookahead bias.
- Reports:
  - Total return
  - CAGR-like daily return metric
  - Win rate
  - Profit factor
  - Maximum drawdown
  - Sharpe ratio
  - Average trade duration
  - Number of trades
  - Cost impact
  - Strategy-wise results

Create a small sample-data generator for tests, clearly marked as synthetic.

Stop after Phase 6 and show:
- Backtest report format
- Test results
- Example report on synthetic data
- Next-phase plan

### Phase 7: ML layer
Implement:
- `ml/feature_engineering.py`
- `ml/feature_store.py`
- `ml/regime_classifier.py`
- `ml/signal_filter.py`
- `ml/outcome_predictor.py`
- `ml/training_pipeline.py`
- `ml/inference_service.py`
- `ml/model_registry.py`

Requirements:
- Point-in-time features only.
- No lookahead bias or target leakage.
- Features:
  - Returns over 1, 3, 5, 15, 30 minutes
  - VWAP distance
  - EMA distance and slope
  - ATR
  - Realized volatility
  - Opening-range position
  - Time of day
  - Minutes to expiry
  - Option premium
  - IV where available
  - OI, volume, bid-ask spread
  - Recent trade outcomes
  - Regime labels
- Regime classifier:
  - Trending up
  - Trending down
  - Range-bound
  - High volatility
  - Low volatility
  - Expiry-day behavior
- Signal filter:
  - Score from 0 to 1
  - Default rejection threshold: 0.60
  - May filter, delay, or reduce trade size
  - Must never remove stop-loss or exceed risk limits
- Outcome predictor:
  - Probability of target before stop
  - Expected premium movement
  - Expected holding duration
  - Probability of loss
- Use triple-barrier labeling:
  - Upper barrier: target
  - Lower barrier: stop-loss
  - Time barrier: maximum holding period
- Use LightGBM or XGBoost.
- Use purged, embargoed walk-forward validation.
- Save model version, feature schema, training window, hyperparameters, metrics, and artifact path.

Stop after Phase 7 and show:
- Feature dictionary
- Training pipeline diagram
- Model registry schema
- Test results
- Next-phase plan

### Phase 8: RL trade management
Implement:
- `ml/rl_environment.py`
- `ml/rl_agent.py`
- `ml/reward_design.py`
- `ml/inference_service.py`

Requirements:
- Use Stable-Baselines3 PPO.
- RL manages trades, not unrestricted speculation.
- Observation space:
  - NIFTY price, VWAP, EMA, ATR, realized volatility
  - Option premium and Greeks where available
  - Entry price
  - Unrealized P&L
  - R-multiple
  - Time in trade
  - Time to square-off
  - Regime probabilities
  - ML confidence
  - Open positions
  - Daily P&L
  - Recent trade outcomes
- Discrete actions:
  - HOLD
  - TIGHTEN_STOP
  - MOVE_STOP_TO_BREAKEVEN
  - TRAIL_STOP
  - TAKE_PARTIAL_PROFIT
  - EXIT_FULL
  - ENTER if no position and setup is valid
  - SKIP_SETUP
- Reward:
  realized_trade_return
  - transaction_costs
  - slippage_penalty
  - drawdown_penalty
  - excessive_holding_penalty
  - risk_limit_violation_penalty
  - overtrading_penalty
- Penalize risk-limit violations heavily.
- Evaluate on realized equity, trade quality, drawdown, and risk compliance—not reward alone.
- Support multiple random seeds.
- Save model version, observation normalizer, feature schema, environment version, hyperparameters, training range, and evaluation report.
- RL must never control daily loss limits, exposure, kill switch, credentials, or square-off timing.

Stop after Phase 8 and show:
- Environment specification
- Action/observation schema
- Reward function
- Training/evaluation plan
- Test results

### Phase 9: Self-learning loop
Implement:
- `learning/trade_journal.py`
- `learning/reflection_engine.py`
- `learning/hypothesis_generator.py`
- `learning/mutation_engine.py`
- `learning/validation_engine.py`
- `learning/promotion_gate.py`
- `learning/deployment_controller.py`

Implement:
LOG → REFLECT → HYPOTHESIZE → MUTATE → VERIFY → PROMOTE / REJECT

LOG:
- Strategy version
- Entry/exit timestamps
- Entry reason
- ML confidence
- Regime
- Contract
- Entry price
- Quantity
- SL/TP/trailing settings
- Exit reason
- Realized P&L
- Costs
- Slippage
- R-multiple
- Features at entry and exit
- Errors and execution quality

REFLECT:
- Daily and weekly reports
- Best/worst trades
- Performance by regime, time, strike, stop/target configuration
- Slippage and execution-quality issues
- Degrading strategies
- Predictive and non-predictive features

HYPOTHESIZE:
- Generate concrete testable hypotheses.
- Each must include proposed change, expected effect, affected strategy, test metric, minimum sample size, validation plan, risk impact, and rollback plan.

MUTATE:
- Mutate only whitelisted parameters:
  - Entry thresholds
  - Confirmation conditions
  - Stop-loss percentage/points
  - Target R-multiple
  - Trailing activation threshold
  - Trailing step
  - Trade-size multiplier within limits
  - Time windows
  - Strike-selection rules
  - ML confidence threshold
- Never mutate:
  - Capital allocation
  - Global daily loss limit
  - Maximum exposure
  - Kill switch
  - Square-off time
  - Credentials
  - Risk-control defaults

VERIFY:
- Backtest
- Walk-forward validation
- Out-of-sample test
- Stress tests
- Multiple-seed testing for ML/RL
- Comparison with current production version
- No-trade baseline comparison

PROMOTE / REJECT:
- Passing candidates become PAPER_READY.
- Deploy to PAPER first.
- After configured paper-validation period, mark LIVE_CANDIDATE.
- Require explicit human approval before LIVE deployment.
- Failed candidates are archived with full evaluation results.

Default autonomy:
PAPER_AUTO_IMPROVE

Stop after Phase 9 and show:
- Learning-loop diagram
- Database schema additions
- Promotion-gate rules
- Test results

### Phase 10: Finalization
Implement:
- Telegram alerts for:
  - Entries
  - Exits
  - Errors
  - Daily loss breach
  - Kill switch
  - End-of-day summary
- Dockerfile and docker-compose.yml.
- Complete README with:
  - Setup
  - Upstox app creation
  - Token generation
  - PAPER mode instructions
  - LIVE mode safety checklist
  - Backtesting instructions
  - ML/RL training instructions
  - Dashboard instructions
- Final test suite.
- Final risk-and-execution audit.

Run:
- All tests
- Linting
- Type checks where configured
- Risk-and-execution-auditor Skill

Produce:
- Final project tree
- Test report
- Known limitations
- Deployment checklist
- PAPER-mode launch instructions

Do not enable LIVE trading.
