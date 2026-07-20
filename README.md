# Investment Bot v3

An automated DCA + satellite-strategy investment assistant for **Interactive Brokers (IBKR)** and **Kraken**, with a self-hosted, mobile-friendly web dashboard and **LINE** push notifications. SQLite is the only datastore — no Notion, no external database server.

> 一個整合 **Interactive Brokers (IBKR)** 與 **Kraken** 的自動化定期定額（DCA）+ 衛星策略投資助手，搭配自架、手機友善的網頁儀表板與 **LINE** 推播通知。資料一律存在 SQLite，不再依賴 Notion 或任何外部資料庫。

繁體中文版在[下方](#中文說明)。

---

## 1. What it is

- **Strategy (unchanged from earlier versions):** every scheduled tick reads market-sentiment indicators (CNN Fear & Greed, crypto F&G, VIX, Alpha Vantage news sentiment) and sizes DCA contributions across three buckets — a core ETF (QQQ), a second core stock (`DCA_STOCK_TICKER`), and a satellite pool for tactical dip-buys. BTC is bought on Kraken via `ccxt`; IAU (gold ETF) and SGOV (cash sweep) are bought/sold on IBKR via `ib_insync`. Satellite positions use an HV180/ATR-based tier system (T1/T2/T3) for dip-entry levels and stop-loss thresholds.
- **QQQ / second-stock DCA is semi-automatic by design:** the fixed monthly amount is bought through **IBKR's own built-in recurring investment** feature (fractional shares), not by the bot's API. The bot only computes pool accounting and sends a LINE message instructing the *dynamic* dip-add top-up amount — never re-instructing the fixed portion, to avoid double-buying.
- **Datastore:** a single SQLite file (`var/investbot.db`, WAL mode), created and migrated idempotently by the bot itself. No separate database service to run.
- **Dashboard:** server-rendered Flask + Jinja2 + htmx + Pico.css, vendored (no npm/build step). Session-based single-password auth sits behind Cloudflare Access as the primary access gate.
- **Notifications:** LINE Messaging API, **push-only** (no inbound webhook/commands). Message kinds: error reports, dip-buy alerts, stop-loss/exit alerts, price-alert triggers, fill confirmations, the weekly IB Key 2FA reminder, and manual-DCA buy instructions. Every send attempt (success or failure, including an empty recipient list) is audited to a `notification_log` table.
- **Scheduling:** APScheduler running in-process inside the single gunicorn worker (cron-style jobs for market hours, post-close settlement, the daily DCA-notification window, the weekly 2FA reminder, and a nightly DB backup).
- **Deployment:** Docker Compose — the bot container, a `cloudflared` tunnel container, the `gnzsnz/ib-gateway` container for IBKR connectivity, and an `autoheal` container that watches **only** the bot's healthcheck.

## 2. Architecture / project structure

```text
.
├── bot_server.py            # Flask app factory: init_db(), blueprint registration, startup tick, gunicorn entrypoint
├── scheduler_job.py         # APScheduler cron grid + nightly SQLite backup job
├── core/                    # Pure, unit-tested strategy math (no I/O)
│   ├── portfolio.py         #   entry prices, stop levels, tier params, next-entry amount
│   └── dca.py                #   FNG-based pool allocation, IAU budget, dip-add draw, SGOV sweep sizing
├── db/                      # SQLite access layer (stdlib sqlite3, no ORM)
│   ├── schema.sql           #   idempotent schema (assets, transactions, app_state, config,
│   │                             price_alerts, alert_latches, indicator_snapshots, positions,
│   │                             notification_log)
│   ├── database.py          #   connection helper + PRAGMAs (WAL, busy_timeout, foreign_keys)
│   └── repo.py               #   one repo object per table (assets_repo, tx_repo, state_repo, ...)
├── jobs/tick.py             # The tick pipeline (replaces the old main.main()): connect → prices →
│                             #   indicators → HV/tier → price alerts → dip/stop checks → DCA window
│                             #   → execute DCA + SGOV → sync fills → snapshot portfolio → ATR backfill
├── marketdata/fetchers.py   # Yahoo/Kraken/CNN/Alpha Vantage fetchers, routed by assets.price_source
├── services/
│   ├── ibkr.py               # connect_ib() with fixed-then-random clientId retry, portfolio snapshot
│   │                             + reconciliation, disconnect-timer/alerting
│   └── line_notify.py        # Push-only LINE notifier; every send audited to notification_log
├── trading/
│   ├── broker_utils.py       # Order placement, Kraken/IBKR DCA execution, SGOV rebalance,
│   │                             satellite dip-buy + stop-loss checks (latch-gated)
│   └── transaction_logger.py # Sync IBKR/Kraken fills into `transactions`, dedup on tx_id
├── utils/
│   ├── hv_atr_calculator.py  # HV180 / tier classification / delayed ATR lock backfill
│   └── calendar_utils.py     # NASDAQ trading-day calendar, DCA-day + DCA-window detection
├── web/                      # Dashboard: blueprint, auth, views, Jinja2 templates, vendored static assets
├── migration/import_from_notion.py  # One-off, paginated, idempotent Notion → SQLite importer
├── tests/                    # pytest suite (107 tests: core math, repo, tick pipeline, web routes, LINE)
├── var/                      # SQLite DB + nightly backups (gitignored, Docker volume)
├── docker-compose.yml        # bot + cloudflared tunnel + ib-gateway + autoheal (bot-only)
└── Dockerfile
```

## 3. Key design points

- **`/healthz` is schedule-aware, not just liveness-only.** It returns `200 {ok, last_tick_age_s}` only if the APScheduler thread is running, at least one job has a `next_run_time`, **and** `last_tick_finished` is under 7 hours old; otherwise `503` so `autoheal` restarts the bot. Two rules make this safe rather than a restart-loop trap: a **startup tick** is enqueued ~5s after every boot (so a restart during an overnight cron gap doesn't inherit a stale timestamp), and `last_tick_finished` is written in a `finally` block on **every** executed tick — success or failure — so an IBKR outage alone never trips healthz (that's tracked separately via `last_tick_ok` + LINE alerts).
- **Alert latches replace one-shot "notified" checkboxes.** `alert_latches` is keyed by `(ticker, kind, level)` — `kind` is `dip` or `stop_loss`, `level` is the next entry level (dip) or the current `entry_count` (stop-loss). A satellite Buy fill that advances `entry_count` automatically re-arms the next level (it's a new, never-fired key) without any explicit re-arm call. A full-exit Sell explicitly re-arms every latch for the ticker via `db.repo.reset_satellite_state()`.
- **Pools accounting.** `qqq_pool` / `stock_pool` / `satellite_pool` (plus IAU rollover) live in `app_state` and are read/written at use time — editing a config value on the dashboard (e.g. `BASE_AMOUNT`, `QQQ_FIXED_PCT`) takes effect on the very next tick, no restart needed. The satellite pool auto-deducts on a manual Buy and auto-credits `qty×price − fee` back on a recorded full-exit Sell, so satellite capital recycles inside the strategy.
- **QQQ / second-stock DCA sends *instructions*, not orders.** IBKR's own recurring-investment feature buys the fixed monthly portion; the bot only computes and messages the *dynamic* dip-add top-up over LINE, explicitly labelled so the fixed portion is never double-bought.
- **`ib-gateway` runs with NO healthcheck and NO autoheal, by design.** The gateway's API port is legitimately closed every night during its scheduled restart and all day Sunday during the 2FA wait — a port-based healthcheck would false-positive during exactly those windows and autoheal would fight the login flow in a loop. Recovery instead relies on IBC's own retry loop, Docker's `restart: always`, and the bot's own disconnect-timer LINE alert. Gateway status is surfaced on the dashboard's `/system` page via the bot's own TCP probe — the dashboard and gateway containers otherwise never talk to each other outside of trading ticks.

## 4. Quickstart

1. Copy `.env.example` to `.env` and fill in real values (LINE tokens, Kraken/Alpha Vantage keys, IBKR credentials, Cloudflare tunnel token, etc.). Never commit `.env`.
2. Generate the dashboard password hash and paste it into `.env` as `DASHBOARD_PASSWORD_HASH`:
   ```bash
   python -m web.auth <your-password>
   ```
   Also generate a `FLASK_SECRET_KEY`:
   ```bash
   python -c "import secrets; print(secrets.token_hex(32))"
   ```
3. Build and start everything:
   ```bash
   docker compose up -d --build
   ```
4. If migrating from a prior Notion-based deployment, run the migration script — **dry-run first**:
   ```bash
   docker compose exec investment-bot python -m migration.import_from_notion --dry-run
   docker compose exec investment-bot python -m migration.import_from_notion
   ```
5. Open the dashboard at your Cloudflare tunnel hostname (behind Cloudflare Access + the app's own login).

## 5. Testing

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pytest
```

All 107 tests should pass. `DISABLE_SCHEDULER=1` is set automatically for the Flask test client so tests never touch IB Gateway, Kraken, or LINE.

## 6. Disclaimer

This project is for **algorithmic-trading learning and personal assistance only** and does not constitute investment advice. Users are solely responsible for any financial loss arising from API errors, network delays, or logic bugs. **Test thoroughly in a paper-trading environment before using real funds.**

---

# 中文說明

## 1. 這是什麼

- **策略邏輯（沿用既有版本）：** 每次排程 tick 會讀取市場情緒指標（CNN 恐懼貪婪指數、加密貨幣恐懼貪婪指數、VIX、Alpha Vantage 新聞情緒），並依此決定三個資金池的定期定額投入：核心 ETF（QQQ）、第二檔核心個股（`DCA_STOCK_TICKER`）、以及用於波段抄底的衛星資金池。BTC 透過 `ccxt` 在 Kraken 下單；IAU（黃金 ETF）與 SGOV（現金再平衡用）透過 `ib_insync` 在 IBKR 下單。衛星部位採用 HV180/ATR 為基礎的分級制度（T1/T2/T3）決定加碼價位與停損門檻。
- **QQQ／第二檔股票 DCA 刻意採半自動設計：** 每月固定金額由 **IBKR 內建的定期定額功能**自動買入（可買零股），並非由 bot 透過 API 下單。Bot 只負責資金池記帳，並透過 LINE 傳送**動態加碼**部分的建議金額——絕不會重複指示已由 IBKR 自動買入的固定部分，避免重複下單。
- **資料庫：** 單一 SQLite 檔案（`var/investbot.db`，WAL 模式），由 bot 自身在啟動時以冪等方式建立與遷移，不需要另外架設資料庫服務。
- **儀表板：** 伺服器端渲染的 Flask + Jinja2 + htmx + Pico.css，全部套件內嵌（vendored），無需 npm 建置流程。以單一密碼的 session 驗證作為第二道防線，主要防護則是前面的 Cloudflare Access。
- **通知：** LINE Messaging API，**僅推播、不接收**（無 inbound webhook／指令）。訊息類型包含：錯誤回報、抄底機會警示、停損／出場警示、到價快訊觸發、成交確認、每週 IB Key 2FA 提醒、以及手動 DCA 下單指示。每一次發送嘗試（無論成功、失敗，甚至收件人清單為空）都會寫入 `notification_log` 稽核表。
- **排程：** APScheduler 在單一 gunicorn worker 內程序內執行（含美股盤中時段、收盤結算、每日 DCA 通知時段、每週 2FA 提醒、每晚資料庫備份等 cron 任務）。
- **部署：** Docker Compose——bot 容器、`cloudflared` 通道容器、負責 IBKR 連線的 `gnzsnz/ib-gateway` 容器，以及只監看 bot 健康檢查的 `autoheal` 容器。

## 2. 架構／專案結構

見上方英文區塊的目錄樹（`core/ db/ jobs/ marketdata/ services/ trading/ utils/ web/ migration/ tests/`），結構與命名在中英文版本中相同，此處不重複列出。

## 3. 值得了解的關鍵設計

- **`/healthz` 具備排程感知能力，而非單純的存活檢查。** 只有在 APScheduler 執行緒仍在運作、至少一個排程任務有 `next_run_time`、且 `last_tick_finished` 在 7 小時內時才回傳 `200 {ok, last_tick_age_s}`；否則回傳 `503`，讓 `autoheal` 重啟 bot。兩條配套規則避免這變成重啟迴圈陷阱：每次啟動後約 5 秒會自動排入一次**啟動 tick**（避免容器在夜間排程空窗期重啟後繼承過期的時間戳記），且 `last_tick_finished` 一律在 `finally` 區塊寫入——無論該次 tick 成功或失敗——因此單純的 IBKR 斷線不會觸發 healthz 異常（斷線狀態另外透過 `last_tick_ok` 與 LINE 警示追蹤）。
- **警示鎖（alert latch）取代舊版一次性的「已通知」勾選框。** `alert_latches` 以 `(ticker, kind, level)` 為鍵——`kind` 是 `dip` 或 `stop_loss`，`level` 是下一個加碼層級（dip）或目前的 `entry_count`（stop_loss）。當一筆衛星買進成交推進 `entry_count` 時，下一個層級會自動視為「重新武裝」（因為那是一個從未觸發過的全新鍵值），不需要額外呼叫重置。若記錄一筆全部出場（平倉）的賣出，則會透過 `db.repo.reset_satellite_state()` 明確重新武裝該標的所有鎖。
- **資金池記帳。** `qqq_pool`／`stock_pool`／`satellite_pool`（以及 IAU 滾存）存放在 `app_state`，並在使用當下讀寫——在儀表板上修改設定值（例如 `BASE_AMOUNT`、`QQQ_FIXED_PCT`）會在下一次 tick 立即生效，不需要重啟。衛星池會在手動記錄買進時自動扣款，並在記錄全部出場的賣出時自動把 `數量×價格 − 手續費` 貸回池中，讓衛星資金在策略內循環使用。
- **QQQ／第二檔股票 DCA 傳送的是「指示」，不是「下單」。** 每月固定金額由 IBKR 自己的定期定額功能買入；bot 只計算並透過 LINE 告知**動態加碼**的追加金額，訊息會明確標示，避免使用者把已自動買入的固定部分重複手動買入。
- **`ib-gateway` 刻意不設健康檢查、也不納入 autoheal。** Gateway 的 API 埠在每晚排程重啟期間、以及整個週日等待 2FA 期間，本來就會正常關閉——若用埠號做健康檢查，恰好會在這些時段誤判為異常，autoheal 也會在登入流程進行中反覆搗亂。復原機制改為依賴 IBC 自身的重試迴圈、Docker 的 `restart: always`，以及 bot 自己的斷線計時器所觸發的 LINE 警示。Gateway 狀態則透過 bot 自己的 TCP 探測，顯示在儀表板的 `/system` 頁——除了交易 tick 之外，儀表板容器與 gateway 容器彼此不會互相溝通。

## 4. 快速上手

1. 將 `.env.example` 複製為 `.env` 並填入實際值（LINE tokens、Kraken／Alpha Vantage 金鑰、IBKR 帳密、Cloudflare tunnel token 等）。切勿提交 `.env` 到版本控制。
2. 產生儀表板密碼雜湊並貼入 `.env` 的 `DASHBOARD_PASSWORD_HASH`：
   ```bash
   python -m web.auth <你的密碼>
   ```
   同時產生 `FLASK_SECRET_KEY`：
   ```bash
   python -c "import secrets; print(secrets.token_hex(32))"
   ```
3. 建置並啟動所有服務：
   ```bash
   docker compose up -d --build
   ```
4. 若是從舊版 Notion 部署遷移，執行遷移腳本——**先跑 dry-run**：
   ```bash
   docker compose exec investment-bot python -m migration.import_from_notion --dry-run
   docker compose exec investment-bot python -m migration.import_from_notion
   ```
5. 透過 Cloudflare tunnel 的網域開啟儀表板（會先經過 Cloudflare Access，再進到應用程式自己的登入頁）。

## 5. 測試

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pytest
```

應全數通過（107 個測試）。Flask 測試用戶端會自動設定 `DISABLE_SCHEDULER=1`，測試過程完全不會碰到 IB Gateway、Kraken 或 LINE。

## 6. 免責聲明

本專案僅供 **程式交易學習與輔助使用**，不構成任何投資建議。使用者需自行承擔 **API 錯誤、網路延遲或邏輯漏洞** 可能造成的財務損失。在實盤使用前，請務必先在 **Paper Trading** 環境進行充分測試。
