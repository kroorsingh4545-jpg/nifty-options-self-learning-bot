-- ============================================================================
-- Research database (data/research.db), version 1. NEVER the trading database.
-- Market data collected for research only; the trading bot never reads it.
-- All timestamps are ISO-8601 strings in IST (+05:30); dates are YYYY-MM-DD.
-- ============================================================================

CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT NOT NULL
);

-- Every Upstox fetch the collectors made (resume + audit trail).
CREATE TABLE IF NOT EXISTS fetch_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    instrument_key  TEXT NOT NULL,
    unit            TEXT NOT NULL CHECK (unit IN ('days','minutes','chain','contracts')),
    interval        INTEGER NOT NULL,
    from_date       TEXT,
    to_date         TEXT,
    rows            INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL CHECK (status IN ('OK','ERROR')),
    note            TEXT,
    fetched_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_fetch_log_key ON fetch_log(instrument_key, unit, interval, from_date);

-- Daily candles (NIFTY index, India VIX, ...). One row per session date.
CREATE TABLE IF NOT EXISTS daily_candles (
    instrument_key  TEXT NOT NULL,
    session_date    TEXT NOT NULL,
    open            REAL NOT NULL,
    high            REAL NOT NULL,
    low             REAL NOT NULL,
    close           REAL NOT NULL,
    volume          INTEGER NOT NULL DEFAULT 0,
    oi              REAL,
    complete        INTEGER NOT NULL CHECK (complete IN (0,1)),
    fetched_at      TEXT NOT NULL,
    PRIMARY KEY (instrument_key, session_date),
    CHECK (low <= open AND low <= close AND high >= open AND high >= close)
) WITHOUT ROWID;

-- Minute candles (start time = candle start).
CREATE TABLE IF NOT EXISTS minute_candles (
    instrument_key     TEXT NOT NULL,
    interval_minutes   INTEGER NOT NULL CHECK (interval_minutes > 0),
    start_time         TEXT NOT NULL,
    open               REAL NOT NULL,
    high               REAL NOT NULL,
    low                REAL NOT NULL,
    close              REAL NOT NULL,
    volume             INTEGER NOT NULL DEFAULT 0,
    oi                 REAL,
    complete           INTEGER NOT NULL CHECK (complete IN (0,1)),
    fetched_at         TEXT NOT NULL,
    PRIMARY KEY (instrument_key, interval_minutes, start_time),
    CHECK (low <= open AND low <= close AND high >= open AND high >= close)
) WITHOUT ROWID;

-- Option / future contract metadata, kept after expiry (the Upstox contract
-- list drops expired contracts; recorded quotes only carry instrument keys).
CREATE TABLE IF NOT EXISTS contracts (
    instrument_key   TEXT PRIMARY KEY,
    trading_symbol   TEXT NOT NULL,
    underlying_key   TEXT,
    instrument_type  TEXT NOT NULL,
    strike           REAL,
    expiry           TEXT,
    lot_size         INTEGER NOT NULL CHECK (lot_size > 0),
    weekly           INTEGER CHECK (weekly IN (0,1)),
    first_seen       TEXT NOT NULL,
    last_seen        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_contracts_expiry ON contracts(expiry);

-- Option-chain snapshots. IV is stored EXACTLY as Upstox returned it
-- (iv_raw); its unit is recorded separately once verified (smoke check).
CREATE TABLE IF NOT EXISTS chain_snapshots (
    snapshot_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    taken_at         TEXT NOT NULL,
    underlying_key   TEXT NOT NULL,
    expiry           TEXT NOT NULL,
    spot             REAL,
    strikes          INTEGER NOT NULL,
    UNIQUE (underlying_key, expiry, taken_at)
);

CREATE TABLE IF NOT EXISTS chain_snapshot_rows (
    snapshot_id      INTEGER NOT NULL REFERENCES chain_snapshots(snapshot_id),
    strike           REAL NOT NULL,
    option_type      TEXT NOT NULL CHECK (option_type IN ('CE','PE')),
    instrument_key   TEXT NOT NULL,
    ltp              REAL,
    close_price      REAL,
    bid              REAL,
    ask              REAL,
    bid_qty          INTEGER,
    ask_qty          INTEGER,
    volume           INTEGER,
    oi               REAL,
    prev_oi          REAL,
    iv_raw           REAL,
    delta            REAL,
    gamma            REAL,
    theta            REAL,
    vega             REAL,
    pop              REAL,
    strike_pcr       REAL,      -- Upstox per-strike put-call ratio, as returned
    PRIMARY KEY (snapshot_id, strike, option_type)
) WITHOUT ROWID;

-- One-minute summary of the trading bot's recorded option quotes
-- (trading.db option_quotes is purged after 30 days; this copy is not).
CREATE TABLE IF NOT EXISTS quote_minute_bars (
    instrument_key   TEXT NOT NULL,
    minute_start     TEXT NOT NULL,
    quotes           INTEGER NOT NULL CHECK (quotes > 0),
    open_ltp         REAL NOT NULL,
    high_ltp         REAL NOT NULL,
    low_ltp          REAL NOT NULL,
    close_ltp        REAL NOT NULL,
    last_bid         REAL,
    last_ask         REAL,
    last_iv          REAL,      -- WebSocket feed IV as recorded (decimal, e.g. 0.126)
    last_oi          REAL,
    last_volume_today INTEGER,
    held_quotes      INTEGER NOT NULL DEFAULT 0,
    summarized_at    TEXT NOT NULL,
    PRIMARY KEY (instrument_key, minute_start)
) WITHOUT ROWID;

-- Settings discovered by checks (e.g. the option-chain IV unit).
CREATE TABLE IF NOT EXISTS observations (
    name         TEXT PRIMARY KEY,
    value        TEXT NOT NULL,
    evidence     TEXT NOT NULL,
    observed_at  TEXT NOT NULL
);
