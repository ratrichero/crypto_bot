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
    close_ord   TEXT                         -- order id dong lenh
);
CREATE INDEX IF NOT EXISTS idx_binance_trades_closed ON binance_trades (closed_at);
CREATE INDEX IF NOT EXISTS idx_binance_trades_tag    ON binance_trades (tag);
