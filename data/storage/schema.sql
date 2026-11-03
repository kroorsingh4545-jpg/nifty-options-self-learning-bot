-- ============================================================================
-- NIFTY options self-learning bot — SQLite schema, version 1
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

-- ============================================================================
-- Isolation / integrity triggers
-- ============================================================================

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
