-- ============================================================================
-- NIFTY options self-learning bot — SQLite schema, version 6
--
-- Design rules enforced IN THE DATABASE (not only in Python):
--   * Every order / fill / position / trade row carries `mode` (PAPER | LIVE).
--   * PAPER and LIVE identifiers are separate columns and mutually exclusive:
--       PAPER rows must have a paper_* id and must NOT have a broker id;
--       LIVE rows must NOT have a paper_* id.
--   * Local ids (client_order_id, trade_uid) must start with '<mode>-'.
--   * A fill / order can never be attached to a parent with a different
--     mode or strategy (triggers) — strategies stay isolated.
--   * mode / strategy_id / identifiers are immutable after insert.
--   * Every open, non-flat position and every trade must carry a stop-loss.
--   * A model can only be LIVE_APPROVED with a recorded human approver.
--   * A model trained on SYNTHETIC data can never be validated or promoted.
--   * v5: promotion to PAPER_READY (models and strategy versions) records the
--     autonomy level and is refused unless that level allows it; nothing can
--     be deployed to LIVE (strategy_deployments accepts PAPER rows only).
--   * v5: every RL decision is persisted (rl_decisions, append-only).
-- All timestamps are ISO-8601 strings in IST (+05:30).
-- ============================================================================

CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- strategies: one row per isolated strategy
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS strategies (
    strategy_id        TEXT PRIMARY KEY
                       CHECK (length(strategy_id) BETWEEN 2 AND 32
                              AND strategy_id GLOB '[a-z]*'
                              AND strategy_id NOT GLOB '*[^a-z0-9_]*'),
    name               TEXT NOT NULL,
    version            TEXT NOT NULL,
    allocated_capital  REAL NOT NULL CHECK (allocated_capital > 0),
    enabled            INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
    status             TEXT NOT NULL DEFAULT 'DISABLED'
                       CHECK (status IN ('ACTIVE','PAUSED','DISABLED','KILLED')),
    config_json        TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(config_json)),
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- trades: one round trip (entry -> exit) of one contract by one strategy
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS trades (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_uid             TEXT NOT NULL UNIQUE,
    mode                  TEXT NOT NULL CHECK (mode IN ('PAPER','LIVE')),
    strategy_id           TEXT NOT NULL REFERENCES strategies(strategy_id),
    strategy_version      TEXT NOT NULL,
    instrument_key        TEXT NOT NULL,
    trading_symbol        TEXT NOT NULL,
    option_type           TEXT NOT NULL CHECK (option_type IN ('CE','PE')),
    strike                REAL NOT NULL CHECK (strike > 0),
    expiry                TEXT NOT NULL,
    side                  TEXT NOT NULL CHECK (side IN ('BUY','SELL')),
    quantity              INTEGER NOT NULL CHECK (quantity > 0),
    entry_price           REAL CHECK (entry_price IS NULL OR entry_price >= 0),
    entry_time            TEXT,
    exit_price            REAL CHECK (exit_price IS NULL OR exit_price >= 0),
    exit_time             TEXT,
    initial_stop_loss     REAL NOT NULL CHECK (initial_stop_loss >= 0),
    stop_loss             REAL NOT NULL CHECK (stop_loss >= 0),
    target                REAL CHECK (target IS NULL OR target >= 0),
    trailing_config_json  TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(trailing_config_json)),
    entry_reason          TEXT,
    exit_reason           TEXT,
    gross_pnl             REAL,
    charges               REAL NOT NULL DEFAULT 0 CHECK (charges >= 0),
    net_pnl               REAL,
    r_multiple            REAL,
    ml_confidence         REAL CHECK (ml_confidence IS NULL OR ml_confidence BETWEEN 0 AND 1),
    regime                TEXT,
    status                TEXT NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN','CLOSED','CANCELLED')),
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL,
    CHECK (substr(trade_uid, 1, length(mode) + 1) = mode || '-'),
    CHECK (status <> 'CLOSED'
           OR (exit_price IS NOT NULL AND exit_time IS NOT NULL AND net_pnl IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS ix_trades_strategy_entry ON trades(mode, strategy_id, entry_time);
CREATE INDEX IF NOT EXISTS ix_trades_status ON trades(mode, status);

-- ---------------------------------------------------------------------------
-- orders: full lifecycle of every order (entry, exit, SL, square-off, ...)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS orders (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    client_order_id   TEXT NOT NULL UNIQUE,          -- local idempotency key, '<mode>-...'
    mode              TEXT NOT NULL CHECK (mode IN ('PAPER','LIVE')),
    paper_order_id    TEXT UNIQUE,                   -- PAPER only (simulated)
    broker_order_id   TEXT UNIQUE,                   -- LIVE only (Upstox order id)
    strategy_id       TEXT NOT NULL REFERENCES strategies(strategy_id),
    trade_uid         TEXT REFERENCES trades(trade_uid),
    instrument_key    TEXT NOT NULL,
    trading_symbol    TEXT NOT NULL,
    side              TEXT NOT NULL CHECK (side IN ('BUY','SELL')),
    order_type        TEXT NOT NULL CHECK (order_type IN ('MARKET','LIMIT','SL','SL-M')),
    product           TEXT NOT NULL DEFAULT 'I' CHECK (product IN ('I','D')),
    purpose           TEXT NOT NULL
                      CHECK (purpose IN ('ENTRY','EXIT','STOP_LOSS','TARGET','SQUARE_OFF','KILL_SWITCH')),
    quantity          INTEGER NOT NULL CHECK (quantity > 0),
    price             REAL CHECK (price IS NULL OR price >= 0),
    trigger_price     REAL CHECK (trigger_price IS NULL OR trigger_price >= 0),
    status            TEXT NOT NULL DEFAULT 'CREATED'
                      CHECK (status IN ('CREATED','SUBMITTED','OPEN','PARTIALLY_FILLED',
                                        'COMPLETE','REJECTED','CANCELLED','UNKNOWN')),
    filled_quantity   INTEGER NOT NULL DEFAULT 0
                      CHECK (filled_quantity >= 0 AND filled_quantity <= quantity),
    average_price     REAL CHECK (average_price IS NULL OR average_price >= 0),
    status_message    TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    CHECK (substr(client_order_id, 1, length(mode) + 1) = mode || '-'),
    CHECK ((mode = 'PAPER' AND paper_order_id IS NOT NULL AND broker_order_id IS NULL)
        OR (mode = 'LIVE'  AND paper_order_id IS NULL))
);
CREATE INDEX IF NOT EXISTS ix_orders_strategy_status ON orders(mode, strategy_id, status);
CREATE INDEX IF NOT EXISTS ix_orders_trade ON orders(trade_uid);

-- ---------------------------------------------------------------------------
-- fills: executions against an order (multiple rows = partial fills)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fills (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id        INTEGER NOT NULL REFERENCES orders(id),
    mode            TEXT NOT NULL CHECK (mode IN ('PAPER','LIVE')),
    strategy_id     TEXT NOT NULL REFERENCES strategies(strategy_id),
    paper_fill_id   TEXT UNIQUE,                     -- PAPER only (simulated)
    broker_fill_id  TEXT UNIQUE,                     -- LIVE only (exchange trade id)
    quantity        INTEGER NOT NULL CHECK (quantity > 0),
    price           REAL NOT NULL CHECK (price > 0),
    charges         REAL NOT NULL DEFAULT 0 CHECK (charges >= 0),
    slippage        REAL NOT NULL DEFAULT 0,
    filled_at       TEXT NOT NULL,
    CHECK ((mode = 'PAPER' AND paper_fill_id IS NOT NULL AND broker_fill_id IS NULL)
        OR (mode = 'LIVE'  AND paper_fill_id IS NULL AND broker_fill_id IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS ix_fills_order ON fills(order_id);

-- ---------------------------------------------------------------------------
-- positions: net position per (mode, strategy, instrument)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS positions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    mode            TEXT NOT NULL CHECK (mode IN ('PAPER','LIVE')),
    strategy_id     TEXT NOT NULL REFERENCES strategies(strategy_id),
    instrument_key  TEXT NOT NULL,
    trading_symbol  TEXT NOT NULL,
    quantity        INTEGER NOT NULL,                -- signed net quantity (+long / -short)
    average_price   REAL NOT NULL CHECK (average_price >= 0),
    last_price      REAL CHECK (last_price IS NULL OR last_price >= 0),
    realized_pnl    REAL NOT NULL DEFAULT 0,
    unrealized_pnl  REAL NOT NULL DEFAULT 0,
    stop_loss       REAL CHECK (stop_loss IS NULL OR stop_loss >= 0),
    target          REAL CHECK (target IS NULL OR target >= 0),
    trailing_stop   REAL CHECK (trailing_stop IS NULL OR trailing_stop >= 0),
    status          TEXT NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN','CLOSED')),
    opened_at       TEXT NOT NULL,
    closed_at       TEXT,
    updated_at      TEXT NOT NULL,
    CHECK (status = 'OPEN' OR (quantity = 0 AND closed_at IS NOT NULL)),
    CHECK (status <> 'OPEN' OR quantity = 0 OR stop_loss IS NOT NULL)
);
-- At most one OPEN position per instrument per strategy per mode.
CREATE UNIQUE INDEX IF NOT EXISTS ux_positions_open
    ON positions(mode, strategy_id, instrument_key) WHERE status = 'OPEN';

-- ---------------------------------------------------------------------------
-- daily_pnl: per-strategy daily ledger (global totals via v_daily_pnl_global)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS daily_pnl (
    trade_date           TEXT NOT NULL
                         CHECK (trade_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    mode                 TEXT NOT NULL CHECK (mode IN ('PAPER','LIVE')),
    strategy_id          TEXT NOT NULL REFERENCES strategies(strategy_id),
    realized_pnl         REAL NOT NULL DEFAULT 0,
    unrealized_pnl       REAL NOT NULL DEFAULT 0,
    charges              REAL NOT NULL DEFAULT 0 CHECK (charges >= 0),
    trades_count         INTEGER NOT NULL DEFAULT 0 CHECK (trades_count >= 0),
    wins                 INTEGER NOT NULL DEFAULT 0 CHECK (wins >= 0),
    losses               INTEGER NOT NULL DEFAULT 0 CHECK (losses >= 0),
    consecutive_losses   INTEGER NOT NULL DEFAULT 0 CHECK (consecutive_losses >= 0),
    max_drawdown         REAL NOT NULL DEFAULT 0 CHECK (max_drawdown >= 0),
    loss_limit_breached  INTEGER NOT NULL DEFAULT 0 CHECK (loss_limit_breached IN (0, 1)),
    updated_at           TEXT NOT NULL,
    PRIMARY KEY (trade_date, mode, strategy_id),
    CHECK (wins + losses <= trades_count)
);

CREATE VIEW IF NOT EXISTS v_daily_pnl_global AS
SELECT trade_date,
       mode,
       SUM(realized_pnl)                    AS realized_pnl,
       SUM(unrealized_pnl)                  AS unrealized_pnl,
       SUM(realized_pnl + unrealized_pnl)   AS total_pnl,
       SUM(charges)                         AS charges,
       SUM(trades_count)                    AS trades_count,
       SUM(wins)                            AS wins,
       SUM(losses)                          AS losses,
       MAX(loss_limit_breached)             AS any_strategy_breached
FROM daily_pnl
GROUP BY trade_date, mode;

-- ---------------------------------------------------------------------------
-- risk_events: every risk decision, breach, kill-switch action
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS risk_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    mode          TEXT NOT NULL CHECK (mode IN ('PAPER','LIVE')),
    strategy_id   TEXT REFERENCES strategies(strategy_id),   -- NULL = global / portfolio-level
    event_type    TEXT NOT NULL CHECK (length(event_type) > 0),
    severity      TEXT NOT NULL CHECK (severity IN ('INFO','WARNING','CRITICAL')),
    message       TEXT NOT NULL,
    details_json  TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(details_json)),
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_risk_events_time ON risk_events(mode, created_at);

-- ---------------------------------------------------------------------------
-- feature_snapshots: point-in-time features at signal / entry / exit
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS feature_snapshots (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    mode                    TEXT NOT NULL CHECK (mode IN ('PAPER','LIVE')),
    strategy_id             TEXT NOT NULL REFERENCES strategies(strategy_id),
    trade_uid               TEXT REFERENCES trades(trade_uid),
    instrument_key          TEXT NOT NULL,
    purpose                 TEXT NOT NULL CHECK (purpose IN ('SIGNAL','ENTRY','EXIT')),
    snapshot_time           TEXT NOT NULL,         -- the as-of time of the data used
    feature_schema_version  TEXT NOT NULL,
    features_json           TEXT NOT NULL CHECK (json_valid(features_json)),
    created_at              TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_feature_snapshots_trade ON feature_snapshots(trade_uid);
CREATE INDEX IF NOT EXISTS ix_feature_snapshots_time ON feature_snapshots(mode, strategy_id, snapshot_time);

-- ---------------------------------------------------------------------------
-- model_runs: versioned ML / RL training runs (model registry backbone)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS model_runs (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id                  TEXT NOT NULL UNIQUE,
    model_type              TEXT NOT NULL
                            CHECK (model_type IN ('SIGNAL_FILTER','REGIME_CLASSIFIER','OUTCOME_PREDICTOR','RL_PPO')),
    model_version           TEXT NOT NULL,
    strategy_id             TEXT REFERENCES strategies(strategy_id),
    feature_schema_version  TEXT NOT NULL,
    feature_schema_json     TEXT NOT NULL CHECK (json_valid(feature_schema_json)),
    training_start          TEXT NOT NULL,
    training_end            TEXT NOT NULL,
    hyperparameters_json    TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(hyperparameters_json)),
    metrics_json            TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(metrics_json)),
    seeds_json              TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(seeds_json)),
    environment_version     TEXT,
    artifact_path           TEXT NOT NULL,
    status                  TEXT NOT NULL DEFAULT 'TRAINED'
                            CHECK (status IN ('TRAINED','VALIDATED','REJECTED','PAPER_READY',
                                              'LIVE_CANDIDATE','LIVE_APPROVED','ARCHIVED')),
    approved_by             TEXT,
    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL,
    -- v4: what the model was trained on. Only HISTORICAL (real market data)
    -- models can ever be VALIDATED or promoted (see triggers below).
    data_source             TEXT NOT NULL DEFAULT 'UNKNOWN'
                            CHECK (data_source IN ('SYNTHETIC','HISTORICAL','UNKNOWN')),
    -- v5: the autonomy level in force when the run was promoted (see triggers).
    promoted_autonomy       TEXT
                            CHECK (promoted_autonomy IS NULL OR promoted_autonomy IN
                                   ('OBSERVE_ONLY','PAPER_AUTO_IMPROVE','LIVE_CANDIDATE_APPROVAL')),
    CHECK (training_start < training_end),
    CHECK (status <> 'LIVE_APPROVED' OR (approved_by IS NOT NULL AND length(approved_by) > 0)),
    UNIQUE (model_type, model_version, strategy_id)
);

-- ---------------------------------------------------------------------------
-- learning_events: Reflective Loop audit trail
--   LOG -> REFLECT -> HYPOTHESIZE -> MUTATE -> VERIFY -> PROMOTE / REJECT
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS learning_events (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    event_uid          TEXT NOT NULL UNIQUE,
    strategy_id        TEXT NOT NULL REFERENCES strategies(strategy_id),
    stage              TEXT NOT NULL
                       CHECK (stage IN ('LOG','REFLECT','HYPOTHESIZE','MUTATE','VERIFY','PROMOTE','REJECT')),
    base_version       TEXT,
    candidate_version  TEXT,
    payload_json       TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(payload_json)),
    outcome            TEXT,
    approved_by        TEXT,
    created_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_learning_events_strategy ON learning_events(strategy_id, created_at);

-- ---------------------------------------------------------------------------
-- control_commands (v3): operator commands queued by the dashboard and
-- executed by the bot through the risk / execution services. The operator is
-- the authenticated dashboard user. A processed command is final.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS control_commands (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    mode           TEXT NOT NULL CHECK (mode IN ('PAPER','LIVE')),
    command        TEXT NOT NULL CHECK (command IN ('KILL_SWITCH','RESET_KILL_SWITCH','EXIT_POSITION',
                                                    'ENGINE_START','ENGINE_PAUSE','ENGINE_STOP',
                                                    'SET_AUTONOMY','REQUEST_LIVE','REQUEST_PAPER')),
    strategy_id    TEXT REFERENCES strategies(strategy_id),   -- NULL = global scope
    target         TEXT,                                      -- e.g. trade_uid for EXIT_POSITION
    payload_json   TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(payload_json)),
    operator       TEXT NOT NULL CHECK (length(trim(operator)) > 0),
    note           TEXT NOT NULL DEFAULT '',
    status         TEXT NOT NULL DEFAULT 'PENDING'
                   CHECK (status IN ('PENDING','DONE','REJECTED','FAILED','EXPIRED')),
    result_message TEXT,
    result_json    TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(result_json)),
    requested_at   TEXT NOT NULL,
    expires_at     TEXT,                                      -- NULL = never expires (kill switch)
    processed_at   TEXT,
    CHECK (status = 'PENDING' OR processed_at IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS ix_control_commands_status ON control_commands(mode, status, id);

-- ---------------------------------------------------------------------------
-- runtime_state (v3): the bot's latest published state snapshot (one row per
-- mode), read by the dashboard. Contains no secrets.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS runtime_state (
    mode          TEXT PRIMARY KEY CHECK (mode IN ('PAPER','LIVE')),
    state_json    TEXT NOT NULL CHECK (json_valid(state_json)),
    published_at  TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- rl_decisions (v5): every decision of the RL trade manager - entry approvals
-- (ENTER / SKIP / VETO) and open-trade advice (HOLD / tighten / partial /
-- exit) with what execution did with it. Append-only audit trail.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS rl_decisions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_uid   TEXT NOT NULL UNIQUE,
    mode           TEXT NOT NULL CHECK (mode IN ('PAPER','LIVE')),
    strategy_id    TEXT NOT NULL REFERENCES strategies(strategy_id),
    kind           TEXT NOT NULL CHECK (kind IN ('ENTRY','MANAGE')),
    trade_uid      TEXT REFERENCES trades(trade_uid),
    signal_id      TEXT,
    action         TEXT NOT NULL CHECK (length(action) > 0),
    allowed        INTEGER CHECK (allowed IS NULL OR allowed IN (0, 1)),   -- ENTRY: entry approved
    applied        INTEGER CHECK (applied IS NULL OR applied IN (0, 1)),   -- MANAGE: applied by execution
    result         TEXT,
    new_stop       REAL CHECK (new_stop IS NULL OR new_stop >= 0),
    exit_lots      INTEGER CHECK (exit_lots IS NULL OR exit_lots >= 0),
    confidence     REAL CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1),
    model_version  TEXT,
    research       INTEGER NOT NULL DEFAULT 0 CHECK (research IN (0, 1)),
    reason         TEXT NOT NULL DEFAULT '',
    details_json   TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(details_json)),
    decided_at     TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    CHECK (kind <> 'MANAGE' OR trade_uid IS NOT NULL),
    CHECK (kind <> 'ENTRY' OR signal_id IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS ix_rl_decisions_time ON rl_decisions(mode, strategy_id, decided_at);
CREATE INDEX IF NOT EXISTS ix_rl_decisions_trade ON rl_decisions(trade_uid);

-- ---------------------------------------------------------------------------
-- trade_journal (v5): LOG stage of the Reflective Loop. One row per closed
-- trade with everything reflection needs (version, reasons, contract, prices,
-- stop / target / trailing settings, P&L, costs, slippage, R, features at
-- entry and exit, errors, execution quality, RL activity). Append-only.
-- data_source marks SYNTHETIC evidence, which can never justify a promotion.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS trade_journal (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_uid            TEXT NOT NULL UNIQUE REFERENCES trades(trade_uid),
    mode                 TEXT NOT NULL CHECK (mode IN ('PAPER','LIVE')),
    strategy_id          TEXT NOT NULL REFERENCES strategies(strategy_id),
    strategy_version     TEXT NOT NULL,
    data_source          TEXT NOT NULL CHECK (data_source IN ('LIVE_FEED','HISTORICAL','SYNTHETIC','UNKNOWN')),
    instrument_key       TEXT NOT NULL,
    trading_symbol       TEXT NOT NULL,
    option_type          TEXT NOT NULL CHECK (option_type IN ('CE','PE')),
    strike               REAL NOT NULL CHECK (strike > 0),
    expiry               TEXT NOT NULL,
    quantity             INTEGER NOT NULL CHECK (quantity > 0),
    entry_time           TEXT NOT NULL,
    exit_time            TEXT NOT NULL,
    entry_price          REAL NOT NULL CHECK (entry_price >= 0),
    exit_price           REAL NOT NULL CHECK (exit_price >= 0),
    entry_reason         TEXT,
    exit_reason          TEXT,
    ml_confidence        REAL CHECK (ml_confidence IS NULL OR ml_confidence BETWEEN 0 AND 1),
    regime               TEXT,
    initial_stop_loss    REAL NOT NULL CHECK (initial_stop_loss >= 0),
    final_stop_loss      REAL NOT NULL CHECK (final_stop_loss >= 0),
    target               REAL CHECK (target IS NULL OR target >= 0),
    risk_config_json     TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(risk_config_json)),
    gross_pnl            REAL NOT NULL,
    costs                REAL NOT NULL CHECK (costs >= 0),
    net_pnl              REAL NOT NULL,
    slippage             REAL NOT NULL DEFAULT 0 CHECK (slippage >= 0),   -- rupees, all fills
    r_multiple           REAL,
    features_entry_json  TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(features_entry_json)),
    features_exit_json   TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(features_exit_json)),
    errors_json          TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(errors_json)),
    execution_json       TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(execution_json)),
    rl_json              TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(rl_json)),
    created_at           TEXT NOT NULL,
    CHECK (substr(trade_uid, 1, length(mode) + 1) = mode || '-'),
    CHECK (entry_time <= exit_time)
);
CREATE INDEX IF NOT EXISTS ix_trade_journal_strategy ON trade_journal(mode, strategy_id, exit_time);

-- ---------------------------------------------------------------------------
-- strategy_versions (v5): every strategy parameter version the loop knows
-- (the YAML baseline and each whitelisted mutation), its hypothesis, its full
-- evaluation, and its promotion state. See the v5 triggers for the rules.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS strategy_versions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    version_uid      TEXT NOT NULL UNIQUE,
    strategy_id      TEXT NOT NULL REFERENCES strategies(strategy_id),
    version          TEXT NOT NULL CHECK (version GLOB '[0-9]*.[0-9]*.[0-9]*'),
    base_version     TEXT,
    parent_uid       TEXT REFERENCES strategy_versions(version_uid),
    status           TEXT NOT NULL
                     CHECK (status IN ('BASELINE','CANDIDATE','VERIFIED','REJECTED','PAPER_READY','PAPER_ACTIVE',
                                       'SUPERSEDED','ROLLED_BACK','LIVE_CANDIDATE','LIVE_APPROVED','ARCHIVED')),
    config_json      TEXT NOT NULL CHECK (json_valid(config_json)),
    mutation_json    TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(mutation_json)),
    hypothesis_json  TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(hypothesis_json)),
    evaluation_json  TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(evaluation_json)),
    evidence_source  TEXT NOT NULL DEFAULT 'UNKNOWN'
                     CHECK (evidence_source IN ('LIVE_FEED','HISTORICAL','SYNTHETIC','UNKNOWN')),
    autonomy_level   TEXT CHECK (autonomy_level IS NULL OR autonomy_level IN
                                 ('OBSERVE_ONLY','PAPER_AUTO_IMPROVE','LIVE_CANDIDATE_APPROVAL')),
    approved_by      TEXT,
    status_reason    TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    UNIQUE (strategy_id, version),
    CHECK (status <> 'LIVE_APPROVED' OR (approved_by IS NOT NULL AND length(trim(approved_by)) > 0))
);
CREATE INDEX IF NOT EXISTS ix_strategy_versions_strategy ON strategy_versions(strategy_id, status);

-- ---------------------------------------------------------------------------
-- strategy_deployments (v5): which strategy version runs in PAPER. The newest
-- row per strategy is in force (version_uid NULL = the YAML baseline).
-- mode is PAPER only: a LIVE deployment cannot even be recorded.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS strategy_deployments (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    deployment_uid        TEXT NOT NULL UNIQUE,
    mode                  TEXT NOT NULL CHECK (mode = 'PAPER'),
    strategy_id           TEXT NOT NULL REFERENCES strategies(strategy_id),
    action                TEXT NOT NULL CHECK (action IN ('DEPLOY','ROLLBACK','RETIRE')),
    version_uid           TEXT REFERENCES strategy_versions(version_uid),
    previous_version_uid  TEXT REFERENCES strategy_versions(version_uid),
    actor                 TEXT NOT NULL CHECK (length(trim(actor)) > 0),
    autonomy_level        TEXT NOT NULL
                          CHECK (autonomy_level IN ('OBSERVE_ONLY','PAPER_AUTO_IMPROVE','LIVE_CANDIDATE_APPROVAL')),
    reason                TEXT NOT NULL DEFAULT '',
    created_at            TEXT NOT NULL,
    CHECK (action <> 'DEPLOY' OR version_uid IS NOT NULL),
    CHECK (action <> 'RETIRE' OR version_uid IS NULL)
);
CREATE INDEX IF NOT EXISTS ix_strategy_deployments_strategy ON strategy_deployments(mode, strategy_id, id);

-- ---------------------------------------------------------------------------
-- candles (v6): every candle the session built or back-filled, for reviews.
-- complete = 0 is a partial candle (started mid-bucket); a complete one replaces
-- it. Market data is the same in every mode, so these rows carry no mode.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS candles (
    instrument_key     TEXT NOT NULL,
    timeframe_minutes  INTEGER NOT NULL CHECK (timeframe_minutes > 0),
    start_time         TEXT NOT NULL,
    end_time           TEXT NOT NULL,
    open               REAL NOT NULL,
    high               REAL NOT NULL,
    low                REAL NOT NULL,
    close              REAL NOT NULL,
    volume             INTEGER NOT NULL DEFAULT 0 CHECK (volume >= 0),
    oi                 REAL,
    tick_count         INTEGER NOT NULL DEFAULT 0 CHECK (tick_count >= 0),
    complete           INTEGER NOT NULL CHECK (complete IN (0,1)),
    source             TEXT NOT NULL CHECK (source IN ('LIVE','BACKFILL')),
    recorded_at        TEXT NOT NULL,
    PRIMARY KEY (instrument_key, timeframe_minutes, start_time)
) WITHOUT ROWID;

-- ---------------------------------------------------------------------------
-- option_quotes (v6): sampled quotes of subscribed option contracts, for reviews
-- (held = 1 while the bot had a position in the contract).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS option_quotes (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    instrument_key TEXT NOT NULL,
    exchange_time  TEXT NOT NULL,
    received_at    TEXT NOT NULL,
    ltp            REAL NOT NULL,
    bid            REAL,
    ask            REAL,
    bid_qty        INTEGER,
    ask_qty        INTEGER,
    volume_today   INTEGER,
    oi             REAL,
    iv             REAL,
    held           INTEGER NOT NULL DEFAULT 0 CHECK (held IN (0,1))
);
CREATE INDEX IF NOT EXISTS ix_option_quotes_key_time ON option_quotes(instrument_key, exchange_time);

-- ============================================================================
-- Isolation / integrity triggers
-- ============================================================================

-- A processed control command can never be changed again.
CREATE TRIGGER IF NOT EXISTS trg_control_commands_final
BEFORE UPDATE ON control_commands
WHEN OLD.status <> 'PENDING'
BEGIN
    SELECT RAISE(ABORT, 'control command already processed');
END;

-- An order linked to a trade must share its mode and strategy.
CREATE TRIGGER IF NOT EXISTS trg_orders_trade_match_insert
BEFORE INSERT ON orders
WHEN NEW.trade_uid IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'isolation: order mode/strategy must match its trade')
    WHERE NOT EXISTS (SELECT 1 FROM trades t
                      WHERE t.trade_uid = NEW.trade_uid
                        AND t.mode = NEW.mode
                        AND t.strategy_id = NEW.strategy_id);
END;

CREATE TRIGGER IF NOT EXISTS trg_orders_trade_match_update
BEFORE UPDATE OF trade_uid ON orders
WHEN NEW.trade_uid IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'isolation: order mode/strategy must match its trade')
    WHERE NOT EXISTS (SELECT 1 FROM trades t
                      WHERE t.trade_uid = NEW.trade_uid
                        AND t.mode = NEW.mode
                        AND t.strategy_id = NEW.strategy_id);
END;

-- Identity columns on orders are immutable.
CREATE TRIGGER IF NOT EXISTS trg_orders_identity_immutable
BEFORE UPDATE OF mode, strategy_id, client_order_id, paper_order_id ON orders
WHEN NEW.mode IS NOT OLD.mode
  OR NEW.strategy_id IS NOT OLD.strategy_id
  OR NEW.client_order_id IS NOT OLD.client_order_id
  OR NEW.paper_order_id IS NOT OLD.paper_order_id
BEGIN
    SELECT RAISE(ABORT, 'immutable: order mode/strategy/identifiers cannot change');
END;

-- A broker order id, once recorded, cannot be changed or removed.
CREATE TRIGGER IF NOT EXISTS trg_orders_broker_id_immutable
BEFORE UPDATE OF broker_order_id ON orders
WHEN OLD.broker_order_id IS NOT NULL AND NEW.broker_order_id IS NOT OLD.broker_order_id
BEGIN
    SELECT RAISE(ABORT, 'immutable: broker_order_id cannot change once set');
END;

-- A fill must share its order's mode and strategy.
CREATE TRIGGER IF NOT EXISTS trg_fills_order_match
BEFORE INSERT ON fills
BEGIN
    SELECT RAISE(ABORT, 'isolation: fill mode/strategy must match its order')
    WHERE NOT EXISTS (SELECT 1 FROM orders o
                      WHERE o.id = NEW.order_id
                        AND o.mode = NEW.mode
                        AND o.strategy_id = NEW.strategy_id);
END;

CREATE TRIGGER IF NOT EXISTS trg_fills_immutable
BEFORE UPDATE ON fills
BEGIN
    SELECT RAISE(ABORT, 'immutable: fills are append-only');
END;

CREATE TRIGGER IF NOT EXISTS trg_trades_identity_immutable
BEFORE UPDATE OF mode, strategy_id, trade_uid ON trades
WHEN NEW.mode IS NOT OLD.mode
  OR NEW.strategy_id IS NOT OLD.strategy_id
  OR NEW.trade_uid IS NOT OLD.trade_uid
BEGIN
    SELECT RAISE(ABORT, 'immutable: trade mode/strategy/uid cannot change');
END;

-- v2: a long trade's stop-loss may only be tightened (moved up), never widened.
CREATE TRIGGER IF NOT EXISTS trg_trades_stop_tighten_only
BEFORE UPDATE OF stop_loss ON trades
WHEN NEW.side = 'BUY' AND NEW.stop_loss < OLD.stop_loss
BEGIN
    SELECT RAISE(ABORT, 'risk: stop_loss of a long trade can only be tightened');
END;

CREATE TRIGGER IF NOT EXISTS trg_positions_identity_immutable
BEFORE UPDATE OF mode, strategy_id, instrument_key ON positions
WHEN NEW.mode IS NOT OLD.mode
  OR NEW.strategy_id IS NOT OLD.strategy_id
  OR NEW.instrument_key IS NOT OLD.instrument_key
BEGIN
    SELECT RAISE(ABORT, 'immutable: position mode/strategy/instrument cannot change');
END;

-- A feature snapshot linked to a trade must share its mode and strategy.
CREATE TRIGGER IF NOT EXISTS trg_feature_snapshots_trade_match
BEFORE INSERT ON feature_snapshots
WHEN NEW.trade_uid IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'isolation: snapshot mode/strategy must match its trade')
    WHERE NOT EXISTS (SELECT 1 FROM trades t
                      WHERE t.trade_uid = NEW.trade_uid
                        AND t.mode = NEW.mode
                        AND t.strategy_id = NEW.strategy_id);
END;

-- v4: a model trained on SYNTHETIC (or unknown) data can never be validated
-- or promoted toward production; its data source can never be changed.
CREATE TRIGGER IF NOT EXISTS trg_model_runs_promotion_needs_real_data_insert
BEFORE INSERT ON model_runs
WHEN NEW.status IN ('VALIDATED','PAPER_READY','LIVE_CANDIDATE','LIVE_APPROVED')
 AND NEW.data_source <> 'HISTORICAL'
BEGIN
    SELECT RAISE(ABORT, 'model_runs: only models trained on HISTORICAL data can be validated or promoted');
END;

CREATE TRIGGER IF NOT EXISTS trg_model_runs_promotion_needs_real_data_update
BEFORE UPDATE OF status ON model_runs
WHEN NEW.status IN ('VALIDATED','PAPER_READY','LIVE_CANDIDATE','LIVE_APPROVED')
 AND NEW.data_source <> 'HISTORICAL'
BEGIN
    SELECT RAISE(ABORT, 'model_runs: only models trained on HISTORICAL data can be validated or promoted');
END;

CREATE TRIGGER IF NOT EXISTS trg_model_runs_data_source_immutable
BEFORE UPDATE OF data_source ON model_runs
WHEN NEW.data_source IS NOT OLD.data_source
BEGIN
    SELECT RAISE(ABORT, 'immutable: model_runs.data_source cannot change');
END;

-- Risk events are an append-only audit trail.
CREATE TRIGGER IF NOT EXISTS trg_risk_events_append_only_update
BEFORE UPDATE ON risk_events
BEGIN
    SELECT RAISE(ABORT, 'immutable: risk_events are append-only');
END;

CREATE TRIGGER IF NOT EXISTS trg_risk_events_append_only_delete
BEFORE DELETE ON risk_events
BEGIN
    SELECT RAISE(ABORT, 'immutable: risk_events are append-only');
END;

-- ============================================================================
-- v5: autonomy, Reflective Loop and RL audit triggers
-- ============================================================================

-- A model can become PAPER_READY only under an autonomy level that allows it,
-- and LIVE_CANDIDATE / LIVE_APPROVED only under LIVE_CANDIDATE_APPROVAL.
CREATE TRIGGER IF NOT EXISTS trg_model_runs_autonomy_insert
BEFORE INSERT ON model_runs
WHEN NEW.data_source = 'HISTORICAL'
 AND NOT (NEW.status = 'LIVE_APPROVED' AND (NEW.approved_by IS NULL OR length(NEW.approved_by) = 0))
 AND ((NEW.status = 'PAPER_READY'
      AND (NEW.promoted_autonomy IS NULL
           OR NEW.promoted_autonomy NOT IN ('PAPER_AUTO_IMPROVE','LIVE_CANDIDATE_APPROVAL')))
  OR (NEW.status IN ('LIVE_CANDIDATE','LIVE_APPROVED')
      AND (NEW.promoted_autonomy IS NULL OR NEW.promoted_autonomy <> 'LIVE_CANDIDATE_APPROVAL')))
BEGIN
    SELECT RAISE(ABORT, 'autonomy: the autonomy level does not allow this model promotion');
END;

CREATE TRIGGER IF NOT EXISTS trg_model_runs_autonomy_update
BEFORE UPDATE OF status ON model_runs
WHEN NEW.status IS NOT OLD.status
 AND NEW.data_source = 'HISTORICAL'
 AND NOT (NEW.status = 'LIVE_APPROVED' AND (NEW.approved_by IS NULL OR length(NEW.approved_by) = 0))
 AND ((NEW.status = 'PAPER_READY'
       AND (NEW.promoted_autonomy IS NULL
            OR NEW.promoted_autonomy NOT IN ('PAPER_AUTO_IMPROVE','LIVE_CANDIDATE_APPROVAL')))
   OR (NEW.status IN ('LIVE_CANDIDATE','LIVE_APPROVED')
       AND (NEW.promoted_autonomy IS NULL OR NEW.promoted_autonomy <> 'LIVE_CANDIDATE_APPROVAL')))
BEGIN
    SELECT RAISE(ABORT, 'autonomy: the autonomy level does not allow this model promotion');
END;

-- RL decisions: a linked trade must match mode / strategy; append-only.
CREATE TRIGGER IF NOT EXISTS trg_rl_decisions_trade_match
BEFORE INSERT ON rl_decisions
WHEN NEW.trade_uid IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'isolation: RL decision mode/strategy must match its trade')
    WHERE NOT EXISTS (SELECT 1 FROM trades t
                      WHERE t.trade_uid = NEW.trade_uid
                        AND t.mode = NEW.mode
                        AND t.strategy_id = NEW.strategy_id);
END;

CREATE TRIGGER IF NOT EXISTS trg_rl_decisions_append_only_update
BEFORE UPDATE ON rl_decisions
BEGIN
    SELECT RAISE(ABORT, 'immutable: rl_decisions are append-only');
END;

CREATE TRIGGER IF NOT EXISTS trg_rl_decisions_append_only_delete
BEFORE DELETE ON rl_decisions
BEGIN
    SELECT RAISE(ABORT, 'immutable: rl_decisions are append-only');
END;

-- Trade journal: must match a CLOSED trade of the same mode / strategy; append-only.
CREATE TRIGGER IF NOT EXISTS trg_trade_journal_trade_match
BEFORE INSERT ON trade_journal
BEGIN
    SELECT RAISE(ABORT, 'isolation: journal entry must match a CLOSED trade of the same mode/strategy')
    WHERE NOT EXISTS (SELECT 1 FROM trades t
                      WHERE t.trade_uid = NEW.trade_uid
                        AND t.mode = NEW.mode
                        AND t.strategy_id = NEW.strategy_id
                        AND t.status = 'CLOSED');
END;

CREATE TRIGGER IF NOT EXISTS trg_trade_journal_append_only_update
BEFORE UPDATE ON trade_journal
BEGIN
    SELECT RAISE(ABORT, 'immutable: trade_journal is append-only');
END;

CREATE TRIGGER IF NOT EXISTS trg_trade_journal_append_only_delete
BEFORE DELETE ON trade_journal
BEGIN
    SELECT RAISE(ABORT, 'immutable: trade_journal is append-only');
END;

-- Strategy versions start as BASELINE or CANDIDATE.
CREATE TRIGGER IF NOT EXISTS trg_strategy_versions_insert_status
BEFORE INSERT ON strategy_versions
WHEN NEW.status NOT IN ('BASELINE','CANDIDATE')
BEGIN
    SELECT RAISE(ABORT, 'strategy_versions: new versions start as BASELINE or CANDIDATE');
END;

-- Only these status transitions exist.
CREATE TRIGGER IF NOT EXISTS trg_strategy_versions_transitions
BEFORE UPDATE OF status ON strategy_versions
WHEN NEW.status IS NOT OLD.status
 AND (OLD.status || '>' || NEW.status) NOT IN (
     'BASELINE>ARCHIVED',
     'CANDIDATE>VERIFIED', 'CANDIDATE>REJECTED', 'CANDIDATE>ARCHIVED',
     'VERIFIED>PAPER_READY', 'VERIFIED>REJECTED', 'VERIFIED>ARCHIVED',
     'PAPER_READY>PAPER_ACTIVE', 'PAPER_READY>REJECTED', 'PAPER_READY>ARCHIVED',
     'PAPER_ACTIVE>SUPERSEDED', 'PAPER_ACTIVE>ROLLED_BACK', 'PAPER_ACTIVE>LIVE_CANDIDATE',
     'SUPERSEDED>PAPER_ACTIVE', 'SUPERSEDED>ARCHIVED',
     'ROLLED_BACK>ARCHIVED',
     'LIVE_CANDIDATE>LIVE_APPROVED', 'LIVE_CANDIDATE>PAPER_ACTIVE', 'LIVE_CANDIDATE>SUPERSEDED',
     'LIVE_CANDIDATE>ROLLED_BACK',
     'LIVE_APPROVED>SUPERSEDED', 'LIVE_APPROVED>ROLLED_BACK',
     'REJECTED>ARCHIVED')
BEGIN
    SELECT RAISE(ABORT, 'strategy_versions: status transition not allowed');
END;

-- PAPER_READY and beyond need HISTORICAL (real market) verification evidence.
CREATE TRIGGER IF NOT EXISTS trg_strategy_versions_real_evidence
BEFORE UPDATE OF status ON strategy_versions
WHEN NEW.status IN ('PAPER_READY','PAPER_ACTIVE','LIVE_CANDIDATE','LIVE_APPROVED')
 AND NEW.evidence_source <> 'HISTORICAL'
BEGIN
    SELECT RAISE(ABORT, 'strategy_versions: only versions verified on HISTORICAL data can be promoted');
END;

-- Autonomy: PAPER_READY / PAPER_ACTIVE need a PAPER-promotion level;
-- LIVE_CANDIDATE needs LIVE_CANDIDATE_APPROVAL.
CREATE TRIGGER IF NOT EXISTS trg_strategy_versions_autonomy
BEFORE UPDATE OF status ON strategy_versions
WHEN NEW.status IS NOT OLD.status
 AND ((NEW.status IN ('PAPER_READY','PAPER_ACTIVE')
       AND (NEW.autonomy_level IS NULL
            OR NEW.autonomy_level NOT IN ('PAPER_AUTO_IMPROVE','LIVE_CANDIDATE_APPROVAL')))
   OR (NEW.status = 'LIVE_CANDIDATE'
       AND (NEW.autonomy_level IS NULL OR NEW.autonomy_level <> 'LIVE_CANDIDATE_APPROVAL')))
BEGIN
    SELECT RAISE(ABORT, 'autonomy: the autonomy level does not allow this strategy promotion');
END;

-- The definition of a version never changes; its evidence source is set once.
CREATE TRIGGER IF NOT EXISTS trg_strategy_versions_immutable
BEFORE UPDATE OF version_uid, strategy_id, version, base_version, parent_uid, config_json, mutation_json,
                 hypothesis_json ON strategy_versions
WHEN NEW.version_uid IS NOT OLD.version_uid
  OR NEW.strategy_id IS NOT OLD.strategy_id
  OR NEW.version IS NOT OLD.version
  OR NEW.base_version IS NOT OLD.base_version
  OR NEW.parent_uid IS NOT OLD.parent_uid
  OR NEW.config_json IS NOT OLD.config_json
  OR NEW.mutation_json IS NOT OLD.mutation_json
  OR NEW.hypothesis_json IS NOT OLD.hypothesis_json
BEGIN
    SELECT RAISE(ABORT, 'immutable: a strategy version definition cannot change');
END;

CREATE TRIGGER IF NOT EXISTS trg_strategy_versions_evidence_once
BEFORE UPDATE OF evidence_source ON strategy_versions
WHEN OLD.evidence_source <> 'UNKNOWN' AND NEW.evidence_source IS NOT OLD.evidence_source
BEGIN
    SELECT RAISE(ABORT, 'immutable: strategy_versions.evidence_source is set once');
END;

CREATE TRIGGER IF NOT EXISTS trg_strategy_versions_no_delete
BEFORE DELETE ON strategy_versions
BEGIN
    SELECT RAISE(ABORT, 'immutable: strategy versions are archived, never deleted');
END;

-- A PAPER deployment must name a HISTORICAL-verified version of the same
-- strategy that is PAPER_READY (new) or was in PAPER before (rollback), under
-- an autonomy level that allows PAPER promotion.
CREATE TRIGGER IF NOT EXISTS trg_strategy_deployments_version_check
BEFORE INSERT ON strategy_deployments
WHEN NEW.version_uid IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'deployment: version must be a PAPER_READY or previously deployed HISTORICAL version of the same strategy')
    WHERE NOT EXISTS (SELECT 1 FROM strategy_versions v
                      WHERE v.version_uid = NEW.version_uid
                        AND v.strategy_id = NEW.strategy_id
                        AND v.evidence_source = 'HISTORICAL'
                        AND v.status IN ('PAPER_READY','SUPERSEDED','PAPER_ACTIVE','LIVE_CANDIDATE','LIVE_APPROVED'));
    SELECT RAISE(ABORT, 'autonomy: the autonomy level does not allow PAPER deployment')
    WHERE NEW.autonomy_level NOT IN ('PAPER_AUTO_IMPROVE','LIVE_CANDIDATE_APPROVAL');
END;

CREATE TRIGGER IF NOT EXISTS trg_strategy_deployments_append_only_update
BEFORE UPDATE ON strategy_deployments
BEGIN
    SELECT RAISE(ABORT, 'immutable: strategy_deployments are append-only');
END;

CREATE TRIGGER IF NOT EXISTS trg_strategy_deployments_append_only_delete
BEFORE DELETE ON strategy_deployments
BEGIN
    SELECT RAISE(ABORT, 'immutable: strategy_deployments are append-only');
END;
