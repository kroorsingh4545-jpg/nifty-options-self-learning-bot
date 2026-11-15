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

-- ============================================================================
-- v2 (R2): forecast-vs-reality. One row per regular NIFTY session.
-- ============================================================================

-- What was predicted for a session, using ONLY data from before its open.
-- recorded_live = 1: written before the session opened (a real prediction);
-- such a row can never be changed or deleted. recorded_live = 0: reconstructed
-- from history (point-in-time, but computed after the fact).
CREATE TABLE IF NOT EXISTS vol_forecasts (
    session_date      TEXT PRIMARY KEY,
    prev_date         TEXT NOT NULL,
    cal_days          INTEGER NOT NULL CHECK (cal_days >= 1),
    prev_close        REAL NOT NULL,
    vix_prev          REAL,            -- India VIX close of prev_date (annualised %, calendar time)
    sigma_vix_cal     REAL,            -- per-session log-return sd: VIX/100 * sqrt(cal_days/365)
    sigma_vix_trd     REAL,            -- VIX/100 * sqrt(1/252)
    sigma_atm_iv      REAL,            -- from an option-chain snapshot, when one exists
    sigma_rv20        REAL,            -- trailing 20-session close-to-close
    sigma_ewma        REAL,            -- RiskMetrics EWMA (lambda from research.yaml)
    sigma_har         REAL,            -- HAR-log, refitted on past data only
    vix_pctl          REAL,            -- share of the trailing lookback VIX closes below vix_prev
    vix_chg5          REAL,            -- ln(VIX prev / VIX 5 sessions earlier)
    vol_regime        TEXT,            -- LOW | MID | HIGH (provisional, VIX percentile terciles)
    trend_z           REAL,
    trend_regime      TEXT,            -- DOWN | RANGE | UP (provisional)
    dte               INTEGER,         -- sessions to the next weekly expiry (0 = expiry day); NULL before weeklies
    is_expiry         INTEGER CHECK (is_expiry IN (0,1)),
    expiry_shifted    INTEGER CHECK (expiry_shifted IN (0,1)),
    is_monthly_expiry INTEGER CHECK (is_monthly_expiry IN (0,1)),
    data_issue        TEXT,
    method_version    TEXT NOT NULL,
    recorded_live     INTEGER NOT NULL CHECK (recorded_live IN (0,1)),
    created_at        TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS trg_vol_forecasts_live_no_update
BEFORE UPDATE ON vol_forecasts WHEN OLD.recorded_live = 1
BEGIN
    SELECT RAISE(ABORT, 'a forecast recorded before its session can never be changed');
END;

CREATE TRIGGER IF NOT EXISTS trg_vol_forecasts_live_no_delete
BEFORE DELETE ON vol_forecasts WHEN OLD.recorded_live = 1
BEGIN
    SELECT RAISE(ABORT, 'a forecast recorded before its session can never be deleted');
END;

-- What actually happened in the session.
CREATE TABLE IF NOT EXISTS vol_outcomes (
    session_date   TEXT PRIMARY KEY,
    open           REAL NOT NULL,
    high           REAL NOT NULL,
    low            REAL NOT NULL,
    close          REAL NOT NULL,
    prev_close     REAL NOT NULL,
    r_cc           REAL NOT NULL,      -- ln(close / prev close)
    r_gap          REAL NOT NULL,      -- ln(open / prev close)
    gap_reliable   INTEGER NOT NULL CHECK (gap_reliable IN (0,1)),  -- 0 before reliable_open_from
    r_oc           REAL NOT NULL,      -- ln(close / open)
    ln_hl          REAL NOT NULL,      -- ln(high / low)
    park_var       REAL NOT NULL,
    gk_var         REAL NOT NULL,
    rv5_var        REAL,               -- 5-minute intraday realised variance (1-minute data, 2022+)
    n_minutes      INTEGER,
    created_at     TEXT NOT NULL
);

-- Generated report summaries (JSON), newest kept per name.
CREATE TABLE IF NOT EXISTS reports (
    name        TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    body        TEXT NOT NULL,
    PRIMARY KEY (name, created_at)
);

-- ---------------------------------------------------------------------------
-- v3 (decision-engine D1): permanent raw copy of the bot's 5-second quotes.
-- trading.db deletes option_quotes / market_quotes after 30 days; this copy is
-- kept. Times are epoch MILLISECONDS (IST instants) to keep rows small.
-- kind: OPT = option_quotes row, MKT = market_quotes row (index / future / VIX).
-- Columns the bot did not record (before trading-db v7) are NULL.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS quote_archive (
    instrument_key   TEXT NOT NULL,
    exchange_ms      INTEGER NOT NULL,
    received_ms      INTEGER NOT NULL,
    kind             TEXT NOT NULL CHECK (kind IN ('OPT','MKT')),
    ltp              REAL NOT NULL,
    bid              REAL,
    ask              REAL,
    bid_qty          INTEGER,
    ask_qty          INTEGER,
    volume_today     INTEGER,
    oi               REAL,
    iv               REAL,
    delta            REAL,
    gamma            REAL,
    theta            REAL,
    vega             REAL,
    tbq              REAL,
    tsq              REAL,
    ltq              INTEGER,
    atp              REAL,
    held             INTEGER NOT NULL DEFAULT 0 CHECK (held IN (0,1)),
    PRIMARY KEY (instrument_key, exchange_ms, received_ms)
) WITHOUT ROWID;

-- One row per archived (day, kind): what was copied and when.
CREATE TABLE IF NOT EXISTS archive_log (
    session_date  TEXT NOT NULL,
    kind          TEXT NOT NULL CHECK (kind IN ('OPT','MKT')),
    rows          INTEGER NOT NULL,
    archived_at   TEXT NOT NULL,
    PRIMARY KEY (session_date, kind)
) WITHOUT ROWID;

-- ---------------------------------------------------------------------------
-- v4 (decision-engine D2): counterfactual outcome of every recorded strategy
-- signal, one row per (signal, simulator version). Each signal traded alone,
-- one lot, with the bot's rules, on recorded quotes. Never read by trading.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS shadow_outcomes (
    signal_id         TEXT NOT NULL,
    sim_version       TEXT NOT NULL,
    strategy_id       TEXT NOT NULL,
    option_type       TEXT NOT NULL CHECK (option_type IN ('CE','PE')),
    generated_at      TEXT NOT NULL,
    candle_end        TEXT NOT NULL,
    signal_source     TEXT NOT NULL CHECK (signal_source IN ('TABLE','LOG')),
    decided           INTEGER NOT NULL CHECK (decided IN (0,1)),
    approved          INTEGER CHECK (approved IS NULL OR approved IN (0,1)),
    decision_reasons_json TEXT NOT NULL DEFAULT '[]',
    trade_uid         TEXT,
    spot              REAL,
    spot_source       TEXT,
    instrument_key    TEXT,
    strike            REAL,
    expiry            TEXT,
    lot_size          INTEGER,
    status            TEXT NOT NULL CHECK (status IN
                      ('FILLED','NO_CONTRACT','NO_SPOT','NO_QUOTE','NO_TWO_SIDED','BAD_LEVELS','NO_FILL')),
    decision_quote_at TEXT,
    entry_ref         REAL,
    stop              REAL,
    target            REAL,
    risk_per_unit     REAL,
    entry_time        TEXT,
    entry_price       REAL,
    exit_time         TEXT,
    exit_price        REAL,
    exit_price_best   REAL,
    exit_reason       TEXT,
    final_stop        REAL,
    quantity          INTEGER NOT NULL DEFAULT 0,
    gross             REAL,
    charges           REAL,
    net               REAL,
    r_gross           REAL,
    r_net             REAL,
    mfe_r             REAL,
    mae_r             REAL,
    quotes_used       INTEGER NOT NULL DEFAULT 0,
    max_gap_seconds   REAL,
    liquidity_json    TEXT NOT NULL DEFAULT '[]',
    flags_json        TEXT NOT NULL DEFAULT '[]',
    computed_at       TEXT NOT NULL,
    PRIMARY KEY (signal_id, sim_version),
    CHECK (status <> 'FILLED' OR (entry_price IS NOT NULL AND exit_price IS NOT NULL AND net IS NOT NULL))
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_shadow_time ON shadow_outcomes(sim_version, generated_at);
