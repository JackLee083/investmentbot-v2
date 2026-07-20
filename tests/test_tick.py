"""Tests for jobs/tick.py -- the tick pipeline wrapper.

Every external system (IB Gateway, Kraken/ccxt, market-data fetchers, LINE)
is faked via monkeypatch; nothing here makes a real network call, places a
real order, or sends a real LINE push. Only db.repo (against a temp-file
SQLite DB, same pattern as tests/test_repo.py) runs for real -- that's the
whole point of this module: making sure state (tick_lock, last_tick_*)
round-trips through SQLite correctly under crash/skip/failure scenarios.

Everything that would touch the network is either naturally a no-op because
the test DB starts empty (no assets/alerts rows -> the price/indicator/
satellite loops never iterate) or is explicitly faked below:
  - connect_ib()               -> returns a FakeIB() or None
  - line_bot                   -> FakeLineBot() (records, never pushes)
  - _update_market_indicators  -> always hits CNN/VIX/AlphaVantage; faked
  - sync_kraken_trades         -> always hits ccxt; faked
  - _snapshot_kraken_btc_position -> always hits ccxt; faked
  - _handle_dca_check          -> forced to None so the DCA-order branch
                                   (which would call ccxt/IBKR order APIs)
                                   never fires purely by wall-clock luck
"""

from datetime import datetime, timedelta

import pytest

from config.config_loader import est


@pytest.fixture()
def db_path(tmp_path, monkeypatch):
    path = tmp_path / "test.db"
    monkeypatch.setenv("INVESTBOT_DB", str(path))
    from db.database import init_db

    init_db()
    return str(path)


class FakeIB:
    """Stands in for ib_insync.IB -- connected by default, empty
    portfolio/fills/account summary so downstream repo calls are no-ops."""

    def __init__(self, connected=True):
        self._connected = connected

    def isConnected(self):
        return self._connected

    def disconnect(self):
        self._connected = False

    def portfolio(self):
        return []

    def accountSummary(self):
        return []

    def fills(self):
        return []


class FakeLineBot:
    """Records every push instead of sending it."""

    def __init__(self):
        self.sent = []

    def _record(self, kind, *args, **kwargs):
        self.sent.append((kind, args, kwargs))

    def send_error_report(self, *a, **k):
        self._record("error_report", *a, **k)

    def send_dip_alert(self, *a, **k):
        self._record("dip_alert", *a, **k)

    def send_stop_loss_alert(self, *a, **k):
        self._record("stop_loss_alert", *a, **k)

    def send_price_alert(self, *a, **k):
        self._record("price_alert", *a, **k)

    def send_manual_dca_instruction(self, *a, **k):
        self._record("manual_dca_instruction", *a, **k)

    def send_trade_report(self, *a, **k):
        self._record("trade_report", *a, **k)

    def notify_weekly_login(self, *a, **k):
        self._record("weekly_login", *a, **k)


@pytest.fixture()
def tick_env(db_path, monkeypatch):
    """Wire up jobs.tick with every network-touching piece faked, and
    return (module, calls, fake_line_bot) so tests can inspect what ran."""
    import jobs.tick as tick_module

    fake_line_bot = FakeLineBot()
    monkeypatch.setattr(tick_module, "line_bot", fake_line_bot)
    monkeypatch.setattr(tick_module, "connect_ib", lambda: FakeIB())
    monkeypatch.setattr(tick_module, "_update_market_indicators", lambda: 50)
    monkeypatch.setattr(tick_module, "sync_kraken_trades", lambda: None)
    monkeypatch.setattr(tick_module, "_snapshot_kraken_btc_position", lambda: None)
    # Force the DCA-order-placement branch off deterministically -- it must
    # never fire based on wall-clock luck during a test run (see PLAN_V3.md
    # test constraints: no real network calls / no real orders).
    monkeypatch.setattr(tick_module, "_handle_dca_check", lambda: None)

    calls = []
    original_run_stage = tick_module._run_stage

    def recording_run_stage(name, fn, *args, **kwargs):
        calls.append(name)
        return original_run_stage(name, fn, *args, **kwargs)

    monkeypatch.setattr(tick_module, "_run_stage", recording_run_stage)

    return tick_module, calls, fake_line_bot


# ---------------------------------------------------------------------------
# (a) a tick that raises mid-pipeline still finishes + releases the lock
# ---------------------------------------------------------------------------


def test_tick_raising_mid_pipeline_still_finishes_and_releases_lock(tick_env, monkeypatch):
    tick_module, calls, fake_line_bot = tick_env

    def boom(price_cache):
        raise RuntimeError("simulated failure in dip check")

    monkeypatch.setattr(tick_module, "check_satellite_opportunities", boom)

    tick_module.run_tick()

    from db.repo import state_repo

    assert state_repo.get("last_tick_finished") is not None
    assert state_repo.get("tick_lock") is None  # released in finally
    assert "dip_check" in calls  # the failing stage was still attempted
    assert "lock_entry_atr" in calls  # pipeline continued past the failure
    # the failure was reported via LINE (not silently swallowed)
    assert any(kind == "error_report" for kind, _, _ in fake_line_bot.sent)


# ---------------------------------------------------------------------------
# (b) tick_lock: fresh lock causes skip; stale lock is taken over
# ---------------------------------------------------------------------------


def test_fresh_tick_lock_causes_skip(tick_env):
    tick_module, calls, _ = tick_env
    from db.repo import state_repo

    state_repo.set("tick_lock", datetime.now(est).isoformat())

    tick_module.run_tick()

    assert calls == []  # never even started the pipeline
    assert state_repo.get("last_tick_finished") is None
    # the skip must NOT clobber the still-fresh lock held by "the other run"
    assert state_repo.get("tick_lock") is not None


def test_stale_tick_lock_is_taken_over(tick_env):
    tick_module, calls, _ = tick_env
    from db.repo import state_repo

    stale = datetime.now(est) - timedelta(minutes=45)
    state_repo.set("tick_lock", stale.isoformat())

    tick_module.run_tick()

    assert "load_assets" in calls  # pipeline actually ran
    assert state_repo.get("last_tick_finished") is not None
    assert state_repo.get("tick_lock") is None  # released after this run


# ---------------------------------------------------------------------------
# (c) connect failure degrades to a partial (non-IBKR) tick, recorded as
#     failed, without an exception escaping
# ---------------------------------------------------------------------------


def test_connect_failure_finishes_without_exception(tick_env, monkeypatch):
    tick_module, calls, _ = tick_env
    monkeypatch.setattr(tick_module, "connect_ib", lambda: None)

    tick_module.run_tick()  # must not raise

    from db.repo import state_repo

    # Recorded as a failed-but-finished tick.
    assert state_repo.get("last_tick_ok") is False
    assert state_repo.get("last_tick_finished") is not None
    assert state_repo.get("last_tick_error") is not None
    assert state_repo.get("tick_lock") is None  # still released

    # v2 semantics: everything that doesn't need the IB socket still ran...
    for stage in [
        "load_assets",
        "update_prices",
        "market_indicators",
        "hv_tier",
        "price_alerts",
        "dip_check",
        "stop_loss_check",
        "dca_window",
        "sync_kraken_fills",
        "kraken_btc_position",
        "lock_entry_atr",
    ]:
        assert stage in calls, f"non-IBKR stage {stage} must run despite IB outage"

    # ...while every IBKR-dependent stage was gated off.
    for stage in ["ibkr_dca", "sgov_rebalance", "sync_ibkr_fills", "snapshot_portfolio"]:
        assert stage not in calls, f"IBKR stage {stage} must NOT run when disconnected"


# ---------------------------------------------------------------------------
# (d) stage order smoke test
# ---------------------------------------------------------------------------


def test_stage_order_smoke(tick_env):
    tick_module, calls, _ = tick_env

    tick_module.run_tick()

    expected_order = [
        "load_assets",
        "update_prices",
        "market_indicators",
        "hv_tier",
        "price_alerts",
        "dip_check",
        "stop_loss_check",
        "dca_window",
        "dca_window_check",
        "sync_kraken_fills",
        "sync_ibkr_fills",
        "snapshot_portfolio",
        "kraken_btc_position",
        "lock_entry_atr",
    ]
    # every expected stage ran exactly once, in the expected relative order
    assert [c for c in calls if c in expected_order] == expected_order
