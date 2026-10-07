-- Schema PostgreSQL cho crypto-bots (bot OKX + radar meme Solana)
-- Chay: psql $DATABASE_URL -f schema.sql

CREATE TABLE IF NOT EXISTS okx_trades (
    id          BIGINT PRIMARY KEY,          -- id tu trades.jsonl
    inst        TEXT NOT NULL,               -- BTC-USDT-SWAP ...
    side        TEXT,                        -- long | short
    tag         TEXT,                        -- scalp | grid
    entry       DOUBLE PRECISION,
    exit        DOUBLE PRECISION,
    notional    DOUBLE PRECISION,
    pnl         DOUBLE PRECISION,            -- P&L goc (chua tru phi)
    fee         DOUBLE PRECISION,            -- phi = 0.05% x 2 x notional
    reason      TEXT,                        -- TP | SL | ...
    closed_at   TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_okx_trades_closed ON okx_trades (closed_at);
CREATE INDEX IF NOT EXISTS idx_okx_trades_tag    ON okx_trades (tag);

CREATE TABLE IF NOT EXISTS radar_trades (
    token      TEXT NOT NULL,
    symbol     TEXT,
    wallet     TEXT NOT NULL,
    opened_at  TIMESTAMPTZ NOT NULL,
    closed_at  TIMESTAMPTZ NOT NULL,
    entry      DOUBLE PRECISION,
    exit       DOUBLE PRECISION,
    size_usd   DOUBLE PRECISION,
    final_ret  DOUBLE PRECISION,
    pnl_usd    DOUBLE PRECISION,             -- size_usd * final_ret
    reason     TEXT,
    plan       TEXT NOT NULL CHECK (plan IN ('scalp', 'holder')),
    legs       JSONB,
    snaps      JSONB,
    UNIQUE (token, opened_at, wallet, closed_at, final_ret, plan)
);
CREATE INDEX IF NOT EXISTS idx_radar_trades_closed ON radar_trades (closed_at);
CREATE INDEX IF NOT EXISTS idx_radar_trades_wallet ON radar_trades (wallet);
CREATE INDEX IF NOT EXISTS idx_radar_trades_plan   ON radar_trades (plan);

CREATE TABLE IF NOT EXISTS wallets (
    address  TEXT PRIMARY KEY,
    label    TEXT,
    src      TEXT,
    added_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS equity_snapshots (
    ts      TIMESTAMPTZ NOT NULL,
    system  TEXT NOT NULL,                   -- okx | radar
    equity  DOUBLE PRECISION,
    PRIMARY KEY (ts, system)
);

CREATE TABLE IF NOT EXISTS binance_trades (
    id          BIGINT PRIMARY KEY,          -- id tu binance-bot/trades.jsonl
    symbol      TEXT NOT NULL,               -- BTCUSDT ...
    side        TEXT,                        -- long | short
    tag         TEXT,                        -- scalp | grid
    entry       DOUBLE PRECISION,
    exit        DOUBLE PRECISION,
    notional    DOUBLE PRECISION,
    pnl         DOUBLE PRECISION,            -- P&L rong (da tru phi o bot)
    reason      TEXT,                        -- TP | SL | ...
    closed_at   TIMESTAMPTZ NOT NULL,
    live        BOOLEAN,                     -- true = tien that
    dry         BOOLEAN,                     -- true = dry-run
    close_ord   TEXT,                        -- order id dong lenh
    pnl_gross   DOUBLE PRECISION,            -- P&L gop (truoc phi)
    fee_entry   DOUBLE PRECISION,            -- phi mo (commission userTrades)
    fee_exit    DOUBLE PRECISION,            -- phi dong (commission userTrades)
    fee_estimated BOOLEAN,                   -- true = phi uoc tinh fee_rate
    estimated   BOOLEAN,                     -- true = gia thoat uoc tinh
    exit_source TEXT                         -- bot | exchange_algo | exchange_detect[_partial]
);
-- DB cu: bo sung cot (idempotent; bot/sync cung tu chay khi khoi dong)
ALTER TABLE binance_trades ADD COLUMN IF NOT EXISTS pnl_gross DOUBLE PRECISION;
ALTER TABLE binance_trades ADD COLUMN IF NOT EXISTS fee_entry DOUBLE PRECISION;
ALTER TABLE binance_trades ADD COLUMN IF NOT EXISTS fee_exit DOUBLE PRECISION;
ALTER TABLE binance_trades ADD COLUMN IF NOT EXISTS fee_estimated BOOLEAN;
ALTER TABLE binance_trades ADD COLUMN IF NOT EXISTS estimated BOOLEAN;
ALTER TABLE binance_trades ADD COLUMN IF NOT EXISTS exit_source TEXT;
CREATE INDEX IF NOT EXISTS idx_binance_trades_closed ON binance_trades (closed_at);
CREATE INDEX IF NOT EXISTS idx_binance_trades_tag    ON binance_trades (tag);

-- ==== Config runtime bot + tai khoan dashboard + scanner (G1/G2) ====
-- Dong bo voi db/bot_config.py:DDL (test_bot_config kiem tra).
CREATE TABLE IF NOT EXISTS bot_config_versions (
    version     BIGSERIAL PRIMARY KEY,
    bot         TEXT NOT NULL,
    config      JSONB NOT NULL,
    author      TEXT NOT NULL,
    note        TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS bot_config_versions_bot_idx
    ON bot_config_versions (bot, version DESC);
CREATE TABLE IF NOT EXISTS bot_config_applied (
    bot         TEXT PRIMARY KEY,
    version     BIGINT,
    status      TEXT NOT NULL,
    error       TEXT,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS dashboard_users (
    id              BIGSERIAL PRIMARY KEY,
    username        TEXT NOT NULL UNIQUE,
    password_hash   TEXT NOT NULL,
    role            TEXT NOT NULL DEFAULT 'viewer'
                    CHECK (role IN ('admin', 'viewer')),
    is_active       BOOLEAN NOT NULL DEFAULT true,
    failed_attempts INTEGER NOT NULL DEFAULT 0,
    locked_until    TIMESTAMPTZ,
    created_by      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_login_at   TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS scanner_snapshots (
    id          BIGSERIAL PRIMARY KEY,
    bot         TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
    passed      BOOLEAN NOT NULL,
    score       DOUBLE PRECISION,
    metrics     JSONB,
    reasons     JSONB
);
CREATE INDEX IF NOT EXISTS scanner_snapshots_sym_idx
    ON scanner_snapshots (bot, symbol, ts DESC);
