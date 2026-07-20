-- Investment Bot v3 SQLite schema.
-- Applied idempotently at startup by db.database.init_db() (CREATE TABLE/INDEX IF NOT EXISTS).
-- Mirrors PLAN_V3.md §3 exactly (including the semantics documented in the comments below).

CREATE TABLE IF NOT EXISTS schema_version (
  version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS assets (
  ticker TEXT PRIMARY KEY,
  asset_type TEXT NOT NULL CHECK(asset_type IN ('Core','Satellite','Cash')),
  price_source TEXT NOT NULL DEFAULT 'yahoo' CHECK(price_source IN ('yahoo','kraken','skip')),
  active INTEGER NOT NULL DEFAULT 1,
  current_price REAL, price_updated_at TEXT,
  base_price REAL,                -- satellite trailing high (只升不降 ratchet)
  entry_count INTEGER NOT NULL DEFAULT 0,
  entry_atr REAL, stop_loss_cal_price REAL,
  tier TEXT CHECK(tier IN ('T1','T2','T3')),
  hv180 REAL,
  monitor_reversal INTEGER NOT NULL DEFAULT 0,
  last_buy_date TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS transactions (
  tx_id TEXT PRIMARY KEY,          -- IBKR execId / Kraken order id / 'manual-<uuid4>'
  ticker TEXT NOT NULL,
  side TEXT NOT NULL CHECK(side IN ('Buy','Sell')),
  qty REAL NOT NULL, price REAL NOT NULL,
  fee REAL NOT NULL DEFAULT 0, total REAL NOT NULL,
  broker TEXT NOT NULL CHECK(broker IN ('IBKR','Kraken','Manual')),
  source TEXT NOT NULL,            -- 'Auto_Bot'|'Manual'|'Recurring'
  avg_cost_snapshot REAL,
  executed_at TEXT NOT NULL,       -- ISO8601 US/Eastern (existing convention)
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_tx_ticker_date ON transactions(ticker, executed_at DESC);

CREATE TABLE IF NOT EXISTS app_state (key TEXT PRIMARY KEY, value TEXT NOT NULL,
  updated_at TEXT NOT NULL DEFAULT (datetime('now')));
-- keys: qqq_pool, stock_pool, satellite_pool, qqq_last_buy_price, stock_last_buy_price,
--   satellite_first_buy_price, iau_rollover, ib_disconnect_since, ib_disconnect_alerted,
--   account_net_liq, account_total_cash, account_available_funds, account_updated_at,
--   last_tick_started, last_tick_finished, last_tick_ok, last_tick_error, tick_lock

CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT NOT NULL,
  updated_at TEXT NOT NULL DEFAULT (datetime('now')));
-- seeds: BASE_AMOUNT, DCA_STOCK_TICKER, SGOV_CASH_MULT=1.95, KRAKEN_BTC_PCT=0.10,
--   QQQ_FIXED_PCT=0.30, STOCK_FIXED_PCT=0.15, INDICATOR_STALE_HOURS=24
-- config_repo.get(key): DB first, env fallback. Read at USE time, never import time
-- (BASE_AMOUNT/SGOV_CASH_TARGET are currently import-time constants -- must become calls).

CREATE TABLE IF NOT EXISTS price_alerts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ticker TEXT NOT NULL, target_price REAL NOT NULL,
  direction TEXT NOT NULL CHECK(direction IN ('Above','Below')),
  status TEXT NOT NULL DEFAULT 'Active' CHECK(status IN ('Active','Triggered','Cancelled')),
  created_at TEXT NOT NULL DEFAULT (datetime('now')), triggered_at TEXT
);

CREATE TABLE IF NOT EXISTS alert_latches (               -- replaces Sat Notified / ATR Notified
  ticker TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('dip','stop_loss')),
  level INTEGER NOT NULL,                  -- dip: entry level 1-3; stop_loss: entry_count at fire time
  fired_at TEXT NOT NULL, rearmed_at TEXT, -- NULL = latched (suppresses re-fire)
  PRIMARY KEY (ticker, kind, level)
);
-- armed  = no row for the current (ticker,kind,level) with rearmed_at IS NULL
-- fire   = INSERT ... ON CONFLICT DO UPDATE SET fired_at=now, rearmed_at=NULL
-- re-arm = automatic when a satellite Buy fill advances entry_count (key changes),
--          or manual via dashboard button setting rearmed_at=now

CREATE TABLE IF NOT EXISTS indicator_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,                      -- 'cnn_fng','crypto_fng','vix','news_sentiment'
  value REAL NOT NULL,
  ok INTEGER NOT NULL DEFAULT 1,           -- 0 = fetch failed; NEVER store sentinels (101/50/0.0)
  fetched_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_ind_name_time ON indicator_snapshots(name, fetched_at DESC);

CREATE TABLE IF NOT EXISTS positions (                   -- IBKR snapshot per tick; dashboard reads this
  ticker TEXT PRIMARY KEY,
  qty REAL NOT NULL, avg_cost REAL NOT NULL,
  market_price REAL, market_value REAL, unrealized_pnl REAL,
  broker TEXT NOT NULL DEFAULT 'IBKR',
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS notification_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,                      -- 'error','dip','stop_loss','price_alert','fill','2fa_reminder','manual_dca','startup_check'
  message TEXT NOT NULL, ok INTEGER NOT NULL,
  sent_at TEXT NOT NULL DEFAULT (datetime('now'))
);
