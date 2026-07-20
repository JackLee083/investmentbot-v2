"""Dashboard routes -- see PLAN_V3.md §5's routes table for the exact spec
each view below follows.

HARD RULE (PLAN_V3.md §2): this module reads db.repo (SQLite) only. The one
deliberate exception is `system()`'s TCP probe of the IB Gateway port,
which opens a short raw socket and nothing else -- no ccxt, ib_insync,
yfinance, or LINE SDK import anywhere in this file.
"""

import math
import os
import re
import socket
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from flask import current_app, flash, jsonify, redirect, render_template, request, url_for

from config.config_loader import ADMIN_IDS, IBKR_HOST, IBKR_PORT, REPORT_VIEWER_IDS, SIGNAL_VIEWER_IDS, est
from core.portfolio import entry_prices, next_entry_amount
from db.database import get_db_path
from db.repo import (
    alerts_repo,
    assets_repo,
    config_repo,
    indicators_repo,
    latch_repo,
    notification_repo,
    positions_repo,
    reset_satellite_state,
    state_repo,
    tx_repo,
)
from web import web_bp

_BASE_DIR = Path(__file__).resolve().parent.parent
_BACKUP_DIR = _BASE_DIR / "var" / "backup"

_TICKER_RE = re.compile(r"^[A-Z0-9]{1,10}$")


# ---------------------------------------------------------------------------
# small shared helpers
# ---------------------------------------------------------------------------


def _parse_positive_float(raw, allow_zero=False):
    """Parse a form field into a FINITE positive float; returns None on any
    invalid input (the caller turns that into a form error).

    Why not just `float(raw)` + a `> 0` check: Python's float() happily
    parses "nan", "inf" and "-inf". NaN then poisons everything downstream
    -- every comparison against NaN is False (`nan > 0` AND `nan <= 0` are
    BOTH False), so a naive `if qty <= 0: reject` check waves NaN straight
    through, satellite_pool becomes NaN after one arithmetic op and STAYS
    NaN forever, and the tx INSERT can die silently on the way in. Money
    fields must be finite, full stop -- hence math.isfinite here.
    """
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(val):
        return None
    if val < 0:
        return None
    if val == 0 and not allow_zero:
        return None
    return val


def _hours_since_sqlite_utc(sqlite_utc_str):
    """`sqlite_utc_str` is a datetime('now')-style UTC string
    ('YYYY-MM-DD HH:MM:SS'), the format indicator_snapshots.fetched_at uses.
    Returns hours elapsed, or None if unparseable/missing. (Deliberately a
    local copy rather than importing jobs/tick.py's version -- importing
    that module would drag ccxt/ib_insync into the web process, which is
    exactly what the dashboard-reads-SQLite-only rule forbids.)"""
    if not sqlite_utc_str:
        return None
    try:
        import pytz

        naive = datetime.strptime(sqlite_utc_str, "%Y-%m-%d %H:%M:%S")
        then_utc = pytz.utc.localize(naive)
        now_utc = datetime.now(pytz.utc)
        return (now_utc - then_utc).total_seconds() / 3600
    except Exception:
        return None


def _hours_since_iso(iso_str):
    """`iso_str` is a datetime.isoformat() string with tzinfo (the format
    app_state's account_updated_at / last_tick_* keys use). Returns hours
    elapsed, or None if unparseable/missing."""
    if not iso_str:
        return None
    try:
        dt = datetime.fromisoformat(iso_str)
        now = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.now()
        return (now - dt).total_seconds() / 3600
    except Exception:
        return None


def _current_price_for(ticker, positions_by_ticker):
    """Best-effort 'current price' for a ticker from data already sitting in
    SQLite: assets.current_price first (updated every tick for every active
    asset), else the positions snapshot's market_price. Used by /alerts to
    auto-derive a direction -- never a live network fetch."""
    asset = assets_repo.get(ticker)
    if asset and asset.get("current_price") is not None:
        return asset["current_price"]
    pos = positions_by_ticker.get(ticker)
    if pos and pos.get("market_price") is not None:
        return pos["market_price"]
    return None


# ---------------------------------------------------------------------------
# GET / -- main page
# ---------------------------------------------------------------------------


@web_bp.route("/")
def index():
    net_liq = state_repo.get("account_net_liq")
    cash = state_repo.get("account_total_cash")
    account_updated_at = state_repo.get("account_updated_at")
    account_stale_hours = _hours_since_iso(account_updated_at)

    positions = positions_repo.list_all()
    total_market_value = sum(p.get("market_value") or 0 for p in positions)

    holdings = []
    for p in positions:
        qty = p["qty"]
        avg_cost = p["avg_cost"]
        price = p.get("market_price")
        u_pnl = p.get("unrealized_pnl")
        cost_basis = qty * avg_cost
        return_pct = (u_pnl / cost_basis * 100) if (u_pnl is not None and cost_basis) else None
        weight_pct = (
            (p["market_value"] / total_market_value * 100)
            if p.get("market_value") and total_market_value
            else None
        )
        holdings.append(
            {
                "ticker": p["ticker"],
                "broker": p["broker"],
                "qty": qty,
                "avg_cost": avg_cost,
                "price": price,
                "u_pnl": u_pnl,
                "return_pct": return_pct,
                "weight_pct": weight_pct,
            }
        )

    stale_hours_limit = config_repo.get_float("INDICATOR_STALE_HOURS", default=24)
    indicators = []
    for name, label in (
        ("cnn_fng", "CNN 恐懼貪婪"),
        ("crypto_fng", "加密貨幣恐懼貪婪"),
        ("vix", "VIX"),
        ("news_sentiment", "新聞情緒"),
    ):
        snap = indicators_repo.latest(name)
        age_hours = _hours_since_sqlite_utc(snap["fetched_at"]) if snap else None
        indicators.append(
            {
                "name": name,
                "label": label,
                "value": snap["value"] if snap else None,
                "ok": bool(snap["ok"]) if snap else False,
                "fetched_at": snap["fetched_at"] if snap else None,
                "stale": age_hours is not None and age_hours > stale_hours_limit,
            }
        )

    satellite_pool = state_repo.get("satellite_pool", 0.0)
    satellites = []
    for asset in assets_repo.list_active(asset_type="Satellite"):
        entry_count = asset.get("entry_count") or 0
        next_level = entry_count + 1
        prices = entry_prices(asset.get("base_price"), asset.get("tier") or "T1")
        next_price = prices[next_level - 1] if (prices and next_level <= 3) else None
        next_amount = next_entry_amount(entry_count, satellite_pool) if next_level <= 3 else None
        satellites.append(
            {
                "ticker": asset["ticker"],
                "tier": asset.get("tier"),
                "entry_count": entry_count,
                "next_price": next_price,
                "next_amount": next_amount,
            }
        )

    pools = {
        "qqq_pool": state_repo.get("qqq_pool", 0.0),
        "stock_pool": state_repo.get("stock_pool", 0.0),
        "satellite_pool": satellite_pool,
    }

    return render_template(
        "index.html",
        net_liq=net_liq,
        cash=cash,
        account_updated_at=account_updated_at,
        account_stale=(account_stale_hours is not None and account_stale_hours > 24),
        holdings=holdings,
        indicators=indicators,
        satellites=satellites,
        pools=pools,
    )


# ---------------------------------------------------------------------------
# GET/POST /settings
# ---------------------------------------------------------------------------

SETTINGS_FIELDS = {
    "BASE_AMOUNT": {"kind": "float", "min": 1, "max": 1_000_000, "label": "每月基準投入金額 (USD)"},
    "DCA_STOCK_TICKER": {"kind": "ticker", "label": "第二檔定期定額股票代號"},
    "SGOV_CASH_MULT": {"kind": "float", "min": 0, "max": 50, "label": "SGOV 現金水位倍數"},
    "KRAKEN_BTC_PCT": {"kind": "float", "min": 0, "max": 1, "label": "BTC 配置比例"},
    "QQQ_FIXED_PCT": {"kind": "float", "min": 0, "max": 1, "label": "QQQ 固定配置比例"},
    "STOCK_FIXED_PCT": {"kind": "float", "min": 0, "max": 1, "label": "第二檔股票固定配置比例"},
    "INDICATOR_STALE_HOURS": {"kind": "float", "min": 1, "max": 720, "label": "指標過期時數警戒值"},
}


@web_bp.route("/settings", methods=["GET", "POST"])
def settings():
    errors = {}
    if request.method == "POST":
        cleaned = {}
        for key, spec in SETTINGS_FIELDS.items():
            raw = (request.form.get(key) or "").strip()
            if not raw:
                errors[key] = "必填"
                continue
            if spec["kind"] == "ticker":
                upper = raw.upper()
                if not _TICKER_RE.match(upper):
                    errors[key] = "股票代號需為 1-10 位英數字"
                    continue
                cleaned[key] = upper
            else:
                try:
                    val = float(raw)
                except ValueError:
                    errors[key] = "必須是數字"
                    continue
                # isfinite: float() parses "nan"/"inf" too, and NaN would
                # slip past the range check below the wrong way around --
                # see _parse_positive_float's docstring.
                if not math.isfinite(val) or not (spec["min"] <= val <= spec["max"]):
                    errors[key] = f"需介於 {spec['min']} 與 {spec['max']} 之間"
                    continue
                cleaned[key] = raw

        if not errors:
            for key, value in cleaned.items():
                config_repo.set(key, value)
            return redirect(url_for("web.settings", saved=1))

    current = {key: config_repo.get(key, "") for key in SETTINGS_FIELDS}
    return render_template(
        "settings.html",
        fields=SETTINGS_FIELDS,
        current=current,
        errors=errors,
        saved=request.args.get("saved"),
    )


# ---------------------------------------------------------------------------
# GET/POST /watchlist (+ delete)
# ---------------------------------------------------------------------------


@web_bp.route("/watchlist", methods=["GET", "POST"])
def watchlist():
    error = None
    if request.method == "POST":
        ticker = (request.form.get("ticker") or "").strip().upper()
        asset_type = request.form.get("asset_type")
        tier = request.form.get("tier") or None
        price_source = request.form.get("price_source", "yahoo")

        if not _TICKER_RE.match(ticker):
            error = "股票代號需為 1-10 位英數字"
        elif asset_type not in ("Core", "Satellite", "Cash"):
            error = "資產類型錯誤"
        elif price_source not in ("yahoo", "kraken", "skip"):
            error = "價格來源錯誤"
        elif tier and tier not in ("T1", "T2", "T3"):
            error = "Tier 錯誤"
        else:
            assets_repo.upsert(
                {
                    "ticker": ticker,
                    "asset_type": asset_type,
                    "price_source": price_source,
                    "tier": tier,
                    "active": 1,
                }
            )
            return redirect(url_for("web.watchlist"))

    assets = assets_repo.list_active()
    return render_template("watchlist.html", assets=assets, error=error)


@web_bp.route("/watchlist/<ticker>/delete", methods=["POST"])
def watchlist_delete(ticker):
    assets_repo.soft_delete(ticker)
    return redirect(url_for("web.watchlist"))


# ---------------------------------------------------------------------------
# GET/POST /trades/new
# ---------------------------------------------------------------------------


@web_bp.route("/trades/new", methods=["GET", "POST"])
def trades_new():
    error = None
    if request.method == "POST":
        ticker = (request.form.get("ticker") or "").strip().upper()
        side = request.form.get("side")
        broker = request.form.get("broker")
        date_str = (request.form.get("date") or "").strip()
        full_exit = request.form.get("full_exit") == "on"

        # Finite-positive parsing (rejects NaN/Inf, not just non-numbers) --
        # these three numbers feed pool arithmetic and a NOT NULL money
        # column, see _parse_positive_float.
        qty = _parse_positive_float(request.form.get("qty"))
        price = _parse_positive_float(request.form.get("price"))
        fee = _parse_positive_float(request.form.get("fee") or "0", allow_zero=True)
        if qty is None or price is None or fee is None:
            error = "數量與價格必須是大於 0 的有限數字，手續費必須是不小於 0 的有限數字"

        if error is None:
            if not _TICKER_RE.match(ticker):
                error = "股票代號需為 1-10 位英數字"
            elif side not in ("Buy", "Sell"):
                error = "買賣別錯誤"
            elif broker not in ("IBKR", "Kraken", "Manual"):
                error = "券商錯誤"
            elif not date_str:
                error = "請選擇成交日期"

        if error is None:
            asset = assets_repo.get(ticker)

            avg_cost_snapshot = None
            if side == "Sell":
                pos = next((p for p in positions_repo.list_all() if p["ticker"] == ticker), None)
                avg_cost_snapshot = pos["avg_cost"] if pos else None

            # Manual entries only capture a DATE, not a time-of-day. We
            # anchor at noon US/Eastern purely so executed_at still parses
            # as an ISO8601-US/Eastern timestamp (the column's convention
            # across the whole schema) -- ordering by date is all that
            # actually matters for a manually recorded fill.
            try:
                naive = datetime.strptime(date_str, "%Y-%m-%d")
                executed_at = est.localize(naive.replace(hour=12)).isoformat()
            except ValueError:
                error = "日期格式錯誤 (YYYY-MM-DD)"

        if error is None:
            tx_id = f"manual-{uuid.uuid4()}"
            tx_repo.insert_ignore(
                {
                    "tx_id": tx_id,
                    "ticker": ticker,
                    "side": side,
                    "qty": qty,
                    "price": price,
                    "fee": fee,
                    "broker": broker,
                    "source": "Manual",
                    "avg_cost_snapshot": avg_cost_snapshot,
                    "executed_at": executed_at,
                }
            )

            is_satellite = bool(asset) and asset.get("asset_type") == "Satellite"
            if is_satellite and side == "Buy":
                # Same bookkeeping as an auto-synced satellite Buy fill
                # (trading/transaction_logger.py's _handle_satellite_buy_fill)
                # plus the pool deduction that only a MANUAL entry needs to
                # do itself (an auto fill's pool deduction already happened
                # when the order was placed).
                new_count = (asset.get("entry_count") or 0) + 1
                assets_repo.update_fields(ticker, entry_count=new_count, last_buy_date=date_str)
                pool = state_repo.get("satellite_pool", 0.0)
                state_repo.set("satellite_pool", pool - qty * price)
            elif is_satellite and side == "Sell" and full_exit:
                # Full exit: reset the ladder AND credit the sale back
                # to satellite_pool -- see db.repo.reset_satellite_state's
                # docstring for why the pool credit lives here and not
                # inside that shared function.
                reset_satellite_state(ticker)
                pool = state_repo.get("satellite_pool", 0.0)
                state_repo.set("satellite_pool", pool + (qty * price - fee))
            # Partial sell (full_exit unchecked), or a non-satellite trade:
            # transaction row only -- no strategy-state or pool changes.

            return redirect(url_for("web.trades_new", saved=1))

    assets = assets_repo.list_active()
    positions_by_ticker = {p["ticker"]: p for p in positions_repo.list_all()}
    return render_template(
        "trade_new.html",
        assets=assets,
        positions=positions_by_ticker,
        error=error,
        saved=request.args.get("saved"),
    )


# ---------------------------------------------------------------------------
# GET/POST /alerts (+ cancel)
# ---------------------------------------------------------------------------


@web_bp.route("/alerts", methods=["GET", "POST"])
def alerts():
    error = None
    if request.method == "POST":
        ticker = (request.form.get("ticker") or "").strip().upper()
        direction = request.form.get("direction") or None  # optional explicit override

        # Finite-positive parsing (rejects NaN/Inf) -- a NaN target would
        # make the tick's alert comparison silently never fire.
        target_price = _parse_positive_float(request.form.get("target_price"))
        if target_price is None:
            error = "目標價格必須是大於 0 的有限數字"

        if error is None and not _TICKER_RE.match(ticker):
            error = "股票代號需為 1-10 位英數字"

        if error is None and direction not in ("Above", "Below", None):
            error = "方向錯誤"

        if error is None and direction is None:
            # Dashboard never fetches a live quote (hard rule, PLAN_V3.md
            # §2) -- this is a deliberate deviation from v2, where the LINE
            # `設定` command called get_yahoo_price() synchronously on every
            # alert creation. Here we only use whatever price the last tick
            # already wrote to SQLite; if there isn't one yet, the user
            # must pick a direction explicitly.
            positions_by_ticker = {p["ticker"]: p for p in positions_repo.list_all()}
            current_price = _current_price_for(ticker, positions_by_ticker)
            if current_price is None:
                error = "尚無現價資料，請手動選擇方向"
            else:
                direction = "Above" if target_price > current_price else "Below"

        if error is None:
            alerts_repo.add(ticker, target_price, direction)
            return redirect(url_for("web.alerts", saved=1))

    active_alerts = alerts_repo.list(status="Active")
    return render_template("alerts.html", alerts=active_alerts, error=error, saved=request.args.get("saved"))


@web_bp.route("/alerts/<int:alert_id>/cancel", methods=["POST"])
def alerts_cancel(alert_id):
    alerts_repo.update_status(alert_id, "Cancelled")
    return redirect(url_for("web.alerts"))


# ---------------------------------------------------------------------------
# GET /latches (+ rearm, + reset-satellite)
# ---------------------------------------------------------------------------


@web_bp.route("/latches")
def latches():
    all_latches = latch_repo.list_all()
    satellites = assets_repo.list_active(asset_type="Satellite")
    return render_template("latches.html", latches=all_latches, satellites=satellites)


@web_bp.route("/latches/rearm", methods=["POST"])
def latches_rearm():
    ticker = request.form.get("ticker")
    kind = request.form.get("kind")
    try:
        level = int(request.form.get("level"))
    except (TypeError, ValueError):
        # A malformed POST (missing/garbage level) is a user-facing form
        # error, not a 500.
        flash("無效的 level 參數")
        return redirect(url_for("web.latches"))
    latch_repo.rearm(ticker, kind, level)
    return redirect(url_for("web.latches"))


@web_bp.route("/latches/reset-satellite", methods=["POST"])
def latches_reset_satellite():
    # Recovery button ONLY -- deliberately does NOT touch satellite_pool.
    # Crediting sale proceeds is the /trades/new full-exit path's job; see
    # db.repo.reset_satellite_state's docstring. The template renders a
    # warning line saying exactly this, right next to the button.
    ticker = request.form.get("ticker")
    reset_satellite_state(ticker)
    return redirect(url_for("web.latches"))


# ---------------------------------------------------------------------------
# POST /refresh -- enqueue a one-off tick
# ---------------------------------------------------------------------------


@web_bp.route("/refresh", methods=["POST"])
def refresh():
    """The dashboard "Refresh" mechanism required by PLAN_V3.md §2's hard
    rule: the web layer NEVER talks to IB Gateway itself -- refreshing data
    means enqueuing a one-off tick on the scheduler and letting the normal
    tick pipeline (a background thread, with its own event loop and tick
    lock) do all the fetching. This handler returns immediately; the new
    numbers appear on the next page load once the tick lands."""
    scheduler = current_app.config.get("SCHEDULER")
    if scheduler is None:
        # DISABLE_SCHEDULER=1 (tests/local dev) -- nothing to enqueue on.
        flash("排程器未啟用，無法排入更新")
        return redirect(url_for("web.index"))

    # Lazy import, same reason as bot_server.py: keep jobs.tick's
    # ccxt/ib_insync dependency chain out of processes that never get here.
    from jobs.tick import run_tick

    scheduler.add_job(
        func=run_tick,
        next_run_time=datetime.now(est) + timedelta(seconds=1),
        # Never discard this job as misfired: it exists precisely because
        # the user asked for it NOW; a busy thread pool delaying dispatch
        # by a few seconds must not silently drop it (same reasoning as
        # the startup tick, PLAN_V3.md §5 healthz rule ①).
        misfire_grace_time=None,
        name="Dashboard Refresh",
    )
    flash("更新已排入，數秒後重新整理頁面即可看到新資料")
    return redirect(url_for("web.index"))


# ---------------------------------------------------------------------------
# GET /system
# ---------------------------------------------------------------------------


def _tcp_probe(host, port, timeout=2):
    """The ONE permitted non-SQLite I/O in the whole dashboard (PLAN_V3.md
    §5): a short-lived raw socket connect to the IB Gateway port, done here
    (server-side, on page load) rather than reading Docker healthcheck
    state -- the bot container has no docker.sock, and mounting one into a
    web-exposed container would hand out host root to anyone who ever
    compromised this process. This probe only ever proves "is a TCP
    listener up", nothing more."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _last_backup_file():
    if not _BACKUP_DIR.exists():
        return None
    backups = sorted(_BACKUP_DIR.glob("investbot-*.db"))
    return backups[-1].name if backups else None


@web_bp.route("/system")
def system():
    gateway_up = _tcp_probe(IBKR_HOST, IBKR_PORT)

    recipients = {
        "ADMIN_IDS": len(ADMIN_IDS),
        "REPORT_VIEWER_IDS": len(REPORT_VIEWER_IDS),
        "SIGNAL_VIEWER_IDS": len(SIGNAL_VIEWER_IDS),
    }

    db_path = get_db_path()
    db_size_bytes = os.path.getsize(db_path) if os.path.exists(db_path) else 0

    return render_template(
        "system.html",
        last_tick_started=state_repo.get("last_tick_started"),
        last_tick_finished=state_repo.get("last_tick_finished"),
        last_tick_ok=state_repo.get("last_tick_ok"),
        last_tick_error=state_repo.get("last_tick_error"),
        ib_disconnect_since=state_repo.get("ib_disconnect_since"),
        ib_disconnect_alerted=state_repo.get("ib_disconnect_alerted"),
        gateway_host=IBKR_HOST,
        gateway_port=IBKR_PORT,
        gateway_up=gateway_up,
        notifications=notification_repo.list_recent(limit=20),
        recipients=recipients,
        db_size_bytes=db_size_bytes,
        last_backup=_last_backup_file(),
    )


# ---------------------------------------------------------------------------
# GET /healthz -- NO auth (see web/__init__.py's _PUBLIC_ENDPOINTS)
# ---------------------------------------------------------------------------


@web_bp.route("/healthz")
def healthz():
    """Schedule-aware healthcheck -- see PLAN_V3.md §5's healthz row for the
    full rationale. Returns 200 only if ALL of:
      (a) the scheduler is running,
      (b) at least one job has a next_run_time,
      (c) last_tick_finished is < 7h old.
    Otherwise 503, with a `reason` field so /system / logs can tell which
    condition failed.

    The scheduler handle is fetched from current_app.config at request time
    (set by bot_server.create_app()) rather than imported at module level --
    that's the "import the scheduler handle lazily to avoid circulars" the
    plan calls for: web/views.py must not import bot_server (which imports
    this blueprint).
    """
    scheduler = current_app.config.get("SCHEDULER")
    if scheduler is None or not scheduler.running:
        return jsonify(ok=False, reason="scheduler_not_running"), 503

    jobs = scheduler.get_jobs()
    if not any(getattr(j, "next_run_time", None) is not None for j in jobs):
        return jsonify(ok=False, reason="no_scheduled_jobs"), 503

    last_finished = state_repo.get("last_tick_finished", None)
    if not last_finished:
        return jsonify(ok=False, reason="no_tick_yet"), 503

    age_hours = _hours_since_iso(last_finished)
    if age_hours is None:
        return jsonify(ok=False, reason="bad_timestamp"), 503

    age_s = age_hours * 3600
    if age_hours > 7:
        return jsonify(ok=False, reason="tick_stale", last_tick_age_s=age_s), 503

    return jsonify(ok=True, last_tick_age_s=age_s), 200
