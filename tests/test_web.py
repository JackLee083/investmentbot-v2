"""Tests for the Phase 3 web dashboard (web/ + bot_server.create_app()).

Every test builds a fresh app via bot_server.create_app() against a
temp-file SQLite DB (INVESTBOT_DB monkeypatched per test, same pattern as
tests/test_repo.py). DISABLE_SCHEDULER=1 (set app-wide by tests/conftest.py)
means create_app() never starts APScheduler or enqueues the startup tick,
so no test can ever reach IB Gateway, Kraken, or LINE -- the dashboard code
under test only talks to SQLite anyway (that's the PLAN_V3.md §2 hard rule
these tests indirectly enforce: if a view ever grew a network call, it
would hang/fail right here with no mocks to hide behind).

The known dashboard password for all login tests is PASSWORD below; its
werkzeug hash is generated once at import time and injected via the
DASHBOARD_PASSWORD_HASH env var (which web/auth.py reads at request time).
"""

from datetime import datetime, timedelta

import pytest
from werkzeug.security import generate_password_hash

from config.config_loader import est

PASSWORD = "correct-horse-battery"
PASSWORD_HASH = generate_password_hash(PASSWORD)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("INVESTBOT_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("DISABLE_SCHEDULER", "1")
    monkeypatch.setenv("DASHBOARD_PASSWORD_HASH", PASSWORD_HASH)

    import bot_server

    flask_app = bot_server.create_app()
    flask_app.config["TESTING"] = True
    return flask_app


@pytest.fixture()
def client(app):
    return app.test_client()


def _login(client):
    return client.post("/login", data={"password": PASSWORD}, follow_redirects=False)


@pytest.fixture()
def auth_client(client):
    _login(client)
    return client


class FakeJob:
    def __init__(self, next_run_time):
        self.next_run_time = next_run_time


class FakeScheduler:
    """Stands in for a BackgroundScheduler as far as healthz() cares:
    a .running flag and .get_jobs() returning objects with next_run_time."""

    def __init__(self, running=True, jobs=None):
        self.running = running
        self._jobs = jobs if jobs is not None else []

    def get_jobs(self):
        return self._jobs


def _healthy_scheduler():
    return FakeScheduler(running=True, jobs=[FakeJob(datetime.now(est) + timedelta(minutes=10))])


# ---------------------------------------------------------------------------
# auth guard
# ---------------------------------------------------------------------------

# The ONLY endpoints allowed to answer without a session. Everything else --
# including any route added in the future -- must 302 to /login, which the
# test below enforces by walking the app's actual url_map instead of a
# hand-maintained route list (a hand-kept list silently stops covering new
# routes the day someone forgets to update it).
PUBLIC_ENDPOINTS = {
    "web.login",  # can't require login to see the login page
    "web.healthz",  # Docker healthcheck -- no session, must never redirect
    "web.static",  # pico.min.css / htmx.min.js: the login page needs them
    # line_webhook.callback used to be here (LINE inbound webhook,
    # signature-verified instead of session-guarded) -- Phase 4 deleted the
    # webhook entirely (push-only Messaging API), so POST /callback no
    # longer exists as a route at all and there is nothing to exempt.
}


def test_every_route_guarded_by_default(app, client):
    from flask import url_for

    # Phase 1: build a concrete URL for every non-public rule (dummy value
    # "1" satisfies both int and string URL converters).
    with app.test_request_context():
        to_check = []
        for rule in app.url_map.iter_rules():
            if rule.endpoint in PUBLIC_ENDPOINTS:
                continue
            url = url_for(rule.endpoint, **{arg: 1 for arg in rule.arguments})
            for method in rule.methods - {"HEAD", "OPTIONS"}:
                to_check.append((method, url, rule.endpoint))

    # Phase 2: every method of every rule (incl. POST-only endpoints like
    # /latches/rearm) must redirect an unauthenticated request to /login.
    assert len(to_check) >= 10  # sanity: the walk actually found the app's routes
    for method, url, endpoint in to_check:
        resp = client.open(url, method=method)
        assert resp.status_code == 302, f"{method} {url} ({endpoint}) not guarded"
        assert "/login" in resp.headers["Location"], f"{method} {url} ({endpoint}) redirected elsewhere"


def test_login_page_and_healthz_are_public(client):
    assert client.get("/login").status_code == 200
    # healthz answers directly (503 here -- no scheduler), never redirects.
    assert client.get("/healthz").status_code == 503


def test_login_wrong_password_rerenders_with_error(client):
    resp = client.post("/login", data={"password": "wrong"})
    assert resp.status_code == 200
    assert "密碼錯誤" in resp.get_data(as_text=True)
    # still locked out afterwards
    assert client.get("/").status_code == 302


def test_login_right_password_redirects_and_grants_access(client):
    resp = _login(client)
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/")
    assert client.get("/").status_code == 200


def test_logout_revokes_access(auth_client):
    assert auth_client.get("/").status_code == 200
    auth_client.get("/logout")
    assert auth_client.get("/").status_code == 302


def test_login_next_open_redirect_blocked(client):
    # `next` pointing off-site must NOT be followed after a successful
    # login -- absolute URLs and scheme-relative ("//host") ones both fall
    # back to the index page.
    for evil in ("https://evil.example/phish", "//evil.example/phish"):
        resp = client.post(f"/login?next={evil}", data={"password": PASSWORD})
        assert resp.status_code == 302
        assert "evil.example" not in resp.headers["Location"]
        assert resp.headers["Location"].endswith("/")
        client.get("/logout")  # reset session for the next loop iteration

    # ...while a legitimate same-site relative path still works.
    resp = client.post("/login?next=/system", data={"password": PASSWORD})
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/system")


# ---------------------------------------------------------------------------
# /healthz
# ---------------------------------------------------------------------------


def test_healthz_503_without_scheduler_or_tick(client):
    resp = client.get("/healthz")
    assert resp.status_code == 503
    assert resp.get_json()["ok"] is False
    assert resp.get_json()["reason"] == "scheduler_not_running"


def test_healthz_503_when_scheduler_has_no_jobs(app, client):
    app.config["SCHEDULER"] = FakeScheduler(running=True, jobs=[])
    resp = client.get("/healthz")
    assert resp.status_code == 503
    assert resp.get_json()["reason"] == "no_scheduled_jobs"


def test_healthz_503_when_jobs_exist_but_none_scheduled(app, client):
    # Jobs EXIST but all have next_run_time=None (paused/never-scheduled) --
    # condition (b) is about jobs actually going to run, not merely existing.
    app.config["SCHEDULER"] = FakeScheduler(running=True, jobs=[FakeJob(None), FakeJob(None)])
    resp = client.get("/healthz")
    assert resp.status_code == 503
    assert resp.get_json()["reason"] == "no_scheduled_jobs"


def test_healthz_503_when_no_tick_yet(app, client):
    app.config["SCHEDULER"] = _healthy_scheduler()
    resp = client.get("/healthz")
    assert resp.status_code == 503
    assert resp.get_json()["reason"] == "no_tick_yet"


def test_healthz_200_when_scheduler_running_and_tick_fresh(app, client):
    from db.repo import state_repo

    app.config["SCHEDULER"] = _healthy_scheduler()
    state_repo.set("last_tick_finished", datetime.now(est).isoformat())

    resp = client.get("/healthz")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert 0 <= body["last_tick_age_s"] < 60


def test_healthz_503_when_tick_stale_over_7h(app, client):
    from db.repo import state_repo

    app.config["SCHEDULER"] = _healthy_scheduler()
    state_repo.set("last_tick_finished", (datetime.now(est) - timedelta(hours=8)).isoformat())

    resp = client.get("/healthz")
    assert resp.status_code == 503
    body = resp.get_json()
    assert body["reason"] == "tick_stale"
    assert body["last_tick_age_s"] > 7 * 3600


# ---------------------------------------------------------------------------
# POST /refresh
# ---------------------------------------------------------------------------


class RecordingScheduler(FakeScheduler):
    """FakeScheduler that also records add_job() calls, for /refresh."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.added = []

    def add_job(self, func=None, **kwargs):
        self.added.append((func, kwargs))


def test_refresh_enqueues_one_off_tick(app, auth_client):
    scheduler = RecordingScheduler(running=True)
    app.config["SCHEDULER"] = scheduler

    resp = auth_client.post("/refresh", follow_redirects=True)
    assert resp.status_code == 200
    assert "更新已排入" in resp.get_data(as_text=True)

    assert len(scheduler.added) == 1
    func, kwargs = scheduler.added[0]
    assert func.__name__ == "run_tick"  # the real tick entry point
    assert kwargs["next_run_time"] is not None
    assert kwargs["misfire_grace_time"] is None  # never discard as misfired


def test_refresh_without_scheduler_flashes_error(auth_client):
    # DISABLE_SCHEDULER=1 -> app.config["SCHEDULER"] is None
    resp = auth_client.post("/refresh", follow_redirects=True)
    assert resp.status_code == 200
    assert "排程器未啟用" in resp.get_data(as_text=True)


# ---------------------------------------------------------------------------
# /trades/new -- satellite Buy / partial Sell / full-exit Sell
# ---------------------------------------------------------------------------


def _seed_satellite(ticker="TSLA", entry_count=1, base_price=200.0, pool=1000.0):
    from db.repo import assets_repo, state_repo

    assets_repo.upsert(
        {
            "ticker": ticker,
            "asset_type": "Satellite",
            "price_source": "yahoo",
            "tier": "T1",
            "base_price": base_price,
            "entry_count": entry_count,
            "entry_atr": 5.0,
            "stop_loss_cal_price": base_price - 7.5,
            "monitor_reversal": 1,
        }
    )
    state_repo.set("satellite_pool", pool)


def _trade_form(**overrides):
    form = {
        "ticker": "TSLA",
        "side": "Buy",
        "qty": "2",
        "price": "100",
        "fee": "0",
        "date": "2026-07-10",
        "broker": "IBKR",
    }
    form.update(overrides)
    return form


def test_satellite_buy_advances_entry_count_and_deducts_pool(auth_client):
    from db.repo import assets_repo, state_repo, tx_repo

    _seed_satellite(entry_count=1, pool=1000.0)

    resp = auth_client.post("/trades/new", data=_trade_form(side="Buy", qty="2", price="100"))
    assert resp.status_code == 302

    asset = assets_repo.get("TSLA")
    assert asset["entry_count"] == 2
    assert asset["last_buy_date"] == "2026-07-10"
    assert state_repo.get("satellite_pool") == pytest.approx(1000.0 - 2 * 100)

    txs = tx_repo.list_recent()
    assert len(txs) == 1
    tx = txs[0]
    assert tx["tx_id"].startswith("manual-")
    assert tx["ticker"] == "TSLA"
    assert tx["side"] == "Buy"
    assert tx["broker"] == "IBKR"
    assert tx["source"] == "Manual"
    assert tx["total"] == pytest.approx(200.0)


def test_satellite_partial_sell_is_transaction_only(auth_client):
    from db.repo import assets_repo, state_repo, tx_repo

    _seed_satellite(entry_count=2, base_price=200.0, pool=500.0)

    resp = auth_client.post(
        "/trades/new",
        data=_trade_form(side="Sell", qty="1", price="150", fee="1"),  # no full_exit key
    )
    assert resp.status_code == 302

    # transaction row exists...
    txs = tx_repo.list_recent()
    assert len(txs) == 1
    assert txs[0]["side"] == "Sell"

    # ...but strategy state and pool are untouched (position still open,
    # ladder still valid -- PLAN_V3.md §1 partial-sell decision).
    asset = assets_repo.get("TSLA")
    assert asset["entry_count"] == 2
    assert asset["base_price"] == pytest.approx(200.0)
    assert asset["entry_atr"] == pytest.approx(5.0)
    assert state_repo.get("satellite_pool") == pytest.approx(500.0)


def test_satellite_full_exit_resets_state_credits_pool_and_snapshots_cost(auth_client):
    from db.repo import assets_repo, latch_repo, positions_repo, state_repo, tx_repo

    _seed_satellite(entry_count=3, base_price=200.0, pool=100.0)
    # a fired (latched) dip alert + the position the avg_cost_snapshot comes from
    latch_repo.fire("TSLA", "dip", 3)
    positions_repo.upsert(
        {"ticker": "TSLA", "qty": 4.0, "avg_cost": 120.0, "market_price": 150.0, "broker": "IBKR"}
    )

    resp = auth_client.post(
        "/trades/new",
        data=_trade_form(side="Sell", qty="4", price="150", fee="2", full_exit="on"),
    )
    assert resp.status_code == 302

    # strategy state reset (the PLAN §5 full-exit list)
    asset = assets_repo.get("TSLA")
    assert asset["entry_count"] == 0
    assert asset["base_price"] is None
    assert asset["entry_atr"] is None
    assert asset["stop_loss_cal_price"] is None
    assert asset["monitor_reversal"] == 0

    # every latch for the ticker re-armed
    assert latch_repo.is_armed("TSLA", "dip", 3)

    # pool credited qty*price - fee
    assert state_repo.get("satellite_pool") == pytest.approx(100.0 + (4 * 150 - 2))

    # avg_cost_snapshot pulled from the positions row
    tx = tx_repo.list_recent()[0]
    assert tx["side"] == "Sell"
    assert tx["avg_cost_snapshot"] == pytest.approx(120.0)


def test_satellite_buy_pool_deduction_excludes_fee(auth_client):
    """Pins the decisions-table semantics: a satellite Buy deducts EXACTLY
    qty*price from satellite_pool -- the fee is NOT part of the deduction
    (unlike the full-exit credit, which is qty*price - fee)."""
    from db.repo import state_repo

    _seed_satellite(entry_count=0, pool=1000.0)

    resp = auth_client.post(
        "/trades/new", data=_trade_form(side="Buy", qty="2", price="100", fee="5")
    )
    assert resp.status_code == 302
    # exactly 1000 - 200; NOT 795 (fee added) nor 805 (fee subtracted twice)
    assert state_repo.get("satellite_pool") == pytest.approx(800.0)


def test_trades_new_rejects_bad_input(auth_client):
    from db.repo import tx_repo

    resp = auth_client.post("/trades/new", data=_trade_form(qty="not-a-number"))
    assert resp.status_code == 200  # re-rendered with error, nothing written
    assert tx_repo.list_recent() == []

    resp = auth_client.post("/trades/new", data=_trade_form(qty="-5"))
    assert resp.status_code == 200
    assert tx_repo.list_recent() == []


def test_trades_new_rejects_nan_and_inf(auth_client):
    """float() parses 'nan'/'inf' -- without the isfinite guard, NaN slips
    past `<= 0` checks (every NaN comparison is False), pool arithmetic
    turns satellite_pool into NaN permanently, and the tx row vanishes.
    All such inputs must instead re-render with an error and leave pool /
    entry_count / transactions completely untouched."""
    from db.repo import assets_repo, state_repo, tx_repo

    _seed_satellite(entry_count=1, pool=1000.0)

    for bad_field in ({"qty": "nan"}, {"price": "inf"}, {"qty": "-inf"}, {"fee": "nan"}):
        resp = auth_client.post("/trades/new", data=_trade_form(side="Buy", **bad_field))
        assert resp.status_code == 200, f"{bad_field} not rejected"
        assert "有限數字" in resp.get_data(as_text=True)

    assert state_repo.get("satellite_pool") == pytest.approx(1000.0)
    assert assets_repo.get("TSLA")["entry_count"] == 1
    assert tx_repo.list_recent() == []


# ---------------------------------------------------------------------------
# /watchlist
# ---------------------------------------------------------------------------


def test_watchlist_add_and_soft_delete(auth_client):
    from db.repo import assets_repo

    resp = auth_client.post(
        "/watchlist",
        data={"ticker": "nvda", "asset_type": "Satellite", "tier": "T2", "price_source": "yahoo"},
    )
    assert resp.status_code == 302

    asset = assets_repo.get("NVDA")  # lowercase input was uppercased
    assert asset is not None
    assert asset["active"] == 1
    assert asset["asset_type"] == "Satellite"
    assert asset["tier"] == "T2"

    resp = auth_client.post("/watchlist/NVDA/delete")
    assert resp.status_code == 302
    # soft delete: row still exists, just inactive
    assert assets_repo.get("NVDA")["active"] == 0
    assert all(a["ticker"] != "NVDA" for a in assets_repo.list_active())


def test_watchlist_rejects_bad_ticker(auth_client):
    from db.repo import assets_repo

    resp = auth_client.post(
        "/watchlist",
        data={"ticker": "BAD TICKER!", "asset_type": "Core", "price_source": "yahoo"},
    )
    assert resp.status_code == 200  # re-rendered with error
    assert assets_repo.list_active() == []


# ---------------------------------------------------------------------------
# /settings
# ---------------------------------------------------------------------------


def test_settings_valid_write_persists(auth_client):
    from db.repo import config_repo

    resp = auth_client.post(
        "/settings",
        data={
            "BASE_AMOUNT": "1500",
            "DCA_STOCK_TICKER": "goog",
            "SGOV_CASH_MULT": "2.0",
            "KRAKEN_BTC_PCT": "0.12",
            "QQQ_FIXED_PCT": "0.25",
            "STOCK_FIXED_PCT": "0.15",
            "INDICATOR_STALE_HOURS": "48",
        },
    )
    assert resp.status_code == 302

    assert config_repo.get_float("BASE_AMOUNT") == pytest.approx(1500.0)
    assert config_repo.get("DCA_STOCK_TICKER") == "GOOG"  # uppercased
    assert config_repo.get_float("KRAKEN_BTC_PCT") == pytest.approx(0.12)
    assert config_repo.get_float("INDICATOR_STALE_HOURS") == pytest.approx(48.0)


def test_settings_invalid_values_rejected_not_persisted(auth_client):
    from db.database import get_conn

    resp = auth_client.post(
        "/settings",
        data={
            "BASE_AMOUNT": "abc",  # not numeric
            "DCA_STOCK_TICKER": "G$OG",  # bad ticker chars
            "SGOV_CASH_MULT": "999",  # out of range (max 50)
            "KRAKEN_BTC_PCT": "0.10",
            "QQQ_FIXED_PCT": "0.30",
            "STOCK_FIXED_PCT": "0.15",
            "INDICATOR_STALE_HOURS": "24",
        },
    )
    assert resp.status_code == 200  # re-rendered with field errors

    # nothing at all persisted -- writes are all-or-nothing per submit
    with get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM config").fetchone()["n"] == 0


def test_settings_rejects_nan_and_inf(auth_client):
    from db.database import get_conn

    base = {
        "BASE_AMOUNT": "1000",
        "DCA_STOCK_TICKER": "GOOG",
        "SGOV_CASH_MULT": "1.95",
        "KRAKEN_BTC_PCT": "0.10",
        "QQQ_FIXED_PCT": "0.30",
        "STOCK_FIXED_PCT": "0.15",
        "INDICATOR_STALE_HOURS": "24",
    }
    for key, bad in (("BASE_AMOUNT", "nan"), ("SGOV_CASH_MULT", "inf")):
        resp = auth_client.post("/settings", data={**base, key: bad})
        assert resp.status_code == 200  # re-rendered with a field error

    with get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM config").fetchone()["n"] == 0


# ---------------------------------------------------------------------------
# /alerts
# ---------------------------------------------------------------------------


def test_alert_add_auto_direction_and_cancel(auth_client):
    from db.repo import alerts_repo, assets_repo

    # current price known via assets -> direction auto-derived (no live fetch)
    assets_repo.upsert({"ticker": "AAPL", "asset_type": "Core", "current_price": 100.0})

    resp = auth_client.post("/alerts", data={"ticker": "AAPL", "target_price": "120"})
    assert resp.status_code == 302
    alert = alerts_repo.list(status="Active")[0]
    assert alert["direction"] == "Above"  # 120 > current 100

    resp = auth_client.post(f"/alerts/{alert['id']}/cancel")
    assert resp.status_code == 302
    assert alerts_repo.get(alert["id"])["status"] == "Cancelled"
    assert alerts_repo.list(status="Active") == []


def test_alert_without_price_data_requires_explicit_direction(auth_client):
    from db.repo import alerts_repo

    # UNKNOWN has no assets row and no positions row -> no direction to derive
    resp = auth_client.post("/alerts", data={"ticker": "UNKNOWN", "target_price": "50"})
    assert resp.status_code == 200  # re-rendered asking for a direction
    assert alerts_repo.list() == []

    resp = auth_client.post(
        "/alerts", data={"ticker": "UNKNOWN", "target_price": "50", "direction": "Below"}
    )
    assert resp.status_code == 302
    assert alerts_repo.list(status="Active")[0]["direction"] == "Below"


def test_alerts_rejects_nan_and_inf_target(auth_client):
    from db.repo import alerts_repo, assets_repo

    assets_repo.upsert({"ticker": "AAPL", "asset_type": "Core", "current_price": 100.0})
    for bad in ("nan", "inf", "-inf"):
        resp = auth_client.post("/alerts", data={"ticker": "AAPL", "target_price": bad})
        assert resp.status_code == 200
        assert "有限數字" in resp.get_data(as_text=True)
    assert alerts_repo.list() == []


# ---------------------------------------------------------------------------
# /latches
# ---------------------------------------------------------------------------


def test_latch_rearm_button(auth_client):
    from db.repo import latch_repo

    latch_repo.fire("TSLA", "dip", 2)
    assert not latch_repo.is_armed("TSLA", "dip", 2)

    resp = auth_client.post(
        "/latches/rearm", data={"ticker": "TSLA", "kind": "dip", "level": "2"}
    )
    assert resp.status_code == 302
    assert latch_repo.is_armed("TSLA", "dip", 2)


def test_latch_rearm_malformed_level_is_form_error_not_500(auth_client):
    from db.repo import latch_repo

    latch_repo.fire("TSLA", "dip", 2)

    resp = auth_client.post(
        "/latches/rearm",
        data={"ticker": "TSLA", "kind": "dip", "level": "garbage"},
        follow_redirects=True,
    )
    assert resp.status_code == 200  # flashed error, not a 500
    assert "無效的 level" in resp.get_data(as_text=True)
    assert not latch_repo.is_armed("TSLA", "dip", 2)  # nothing rearmed


def test_reset_satellite_button_resets_without_pool_credit(auth_client):
    from db.repo import assets_repo, latch_repo, state_repo

    _seed_satellite(ticker="PLTR", entry_count=3, base_price=50.0, pool=777.0)
    latch_repo.fire("PLTR", "stop_loss", 3)

    resp = auth_client.post("/latches/reset-satellite", data={"ticker": "PLTR"})
    assert resp.status_code == 302

    asset = assets_repo.get("PLTR")
    assert asset["entry_count"] == 0
    assert asset["base_price"] is None
    assert asset["entry_atr"] is None
    assert asset["monitor_reversal"] == 0
    assert latch_repo.is_armed("PLTR", "stop_loss", 3)

    # THE point of this button vs. the Sell full-exit path: NO pool credit.
    assert state_repo.get("satellite_pool") == pytest.approx(777.0)


# ---------------------------------------------------------------------------
# GET / -- main page with a seeded fixture
# ---------------------------------------------------------------------------


def test_index_renders_seeded_data(auth_client):
    from db.repo import assets_repo, indicators_repo, positions_repo, state_repo

    state_repo.set("account_net_liq", 50000.0)
    state_repo.set("account_total_cash", 2345.67)
    state_repo.set("account_updated_at", datetime.now(est).isoformat())
    state_repo.set("qqq_pool", 111.0)
    state_repo.set("stock_pool", 222.0)
    state_repo.set("satellite_pool", 1000.0)

    positions_repo.upsert(
        {
            "ticker": "AAPL",
            "qty": 10.0,
            "avg_cost": 100.0,
            "market_price": 150.0,
            "market_value": 1500.0,
            "unrealized_pnl": 500.0,
            "broker": "IBKR",
        }
    )
    # the BTC row (broker='Kraken') must render in the same holdings table
    positions_repo.upsert_kraken_btc(qty=0.5, avg_cost=40000.0, price=60000.0)

    indicators_repo.insert("cnn_fng", 45.0, ok=True)
    indicators_repo.insert("vix", 18.5, ok=True)

    # satellite summary: T1 base 200, entry_count 0 -> next entry 180.00 at 30% of pool
    assets_repo.upsert(
        {"ticker": "TSLA", "asset_type": "Satellite", "tier": "T1", "base_price": 200.0, "entry_count": 0}
    )

    resp = auth_client.get("/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    assert "50,000" in html  # net liq (KPI hero: thousands-separated, no cents)
    assert "2,346" in html  # cash (KPI tile: {:,.0f} rounds 2345.67 -> 2,346)
    assert "AAPL" in html
    assert "150.00" in html  # AAPL market price
    assert "500.00" in html  # AAPL uPnL
    assert "50.0%" in html  # AAPL return% (500 / 1000)
    assert "BTC" in html  # Kraken BTC row present
    assert "60000.00" in html  # BTC market price
    assert "45.0" in html  # cnn_fng indicator tile
    assert "18.5" in html  # vix indicator tile
    assert "180.00" in html  # TSLA next entry price (200 * 0.9)
    assert "300.00" in html  # TSLA next entry amount (1000 * 0.30)
    assert "111" in html and "222" in html  # pools (KPI tile sub-text, no cents)
