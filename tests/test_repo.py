"""Tests for db/schema.sql, db/database.py, and db/repo.py.

Every test runs against a temp-file SQLite DB (via the INVESTBOT_DB env var,
pointed at a pytest tmp_path fixture) so nothing here ever touches the real
var/investbot.db.
"""

import pytest


@pytest.fixture()
def db_path(tmp_path, monkeypatch):
    path = tmp_path / "test.db"
    monkeypatch.setenv("INVESTBOT_DB", str(path))
    from db.database import init_db

    init_db()
    return str(path)


# ---------------------------------------------------------------------------
# schema / database.py
# ---------------------------------------------------------------------------


def test_schema_applies_cleanly_and_idempotently(db_path):
    from db.database import init_db, get_conn

    # init_db() already ran once via the fixture; running it again must not error.
    init_db()

    expected_tables = {
        "schema_version",
        "assets",
        "transactions",
        "app_state",
        "config",
        "price_alerts",
        "alert_latches",
        "indicator_snapshots",
        "positions",
        "notification_log",
    }
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        table_names = {r["name"] for r in rows}
        assert expected_tables.issubset(table_names)

        version_row = conn.execute("SELECT COUNT(*) AS n FROM schema_version").fetchone()
        assert version_row["n"] == 1
        version = conn.execute("SELECT version FROM schema_version").fetchone()
        assert version["version"] == 1


def test_pragmas_applied(db_path):
    from db.database import get_conn

    with get_conn() as conn:
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert journal_mode.lower() == "wal"
        fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        assert fk == 1


# ---------------------------------------------------------------------------
# assets_repo
# ---------------------------------------------------------------------------


def test_assets_repo_roundtrip(db_path):
    from db.repo import assets_repo

    assets_repo.upsert(
        {
            "ticker": "QQQ",
            "asset_type": "Core",
            "price_source": "yahoo",
            "current_price": 500.0,
        }
    )
    row = assets_repo.get("QQQ")
    assert row is not None
    assert row["asset_type"] == "Core"
    assert row["current_price"] == 500.0
    assert row["active"] == 1

    # upsert again with a changed field -- should update, not duplicate.
    assets_repo.upsert({"ticker": "QQQ", "asset_type": "Core", "current_price": 510.0})
    row2 = assets_repo.get("QQQ")
    assert row2["current_price"] == 510.0

    assets_repo.upsert({"ticker": "TSLA", "asset_type": "Satellite"})
    active = assets_repo.list_active()
    tickers = {a["ticker"] for a in active}
    assert {"QQQ", "TSLA"}.issubset(tickers)

    satellites = assets_repo.list_active(asset_type="Satellite")
    assert all(a["asset_type"] == "Satellite" for a in satellites)
    assert "TSLA" in {a["ticker"] for a in satellites}

    assets_repo.update_fields("TSLA", entry_count=2, base_price=250.0)
    tsla = assets_repo.get("TSLA")
    assert tsla["entry_count"] == 2
    assert tsla["base_price"] == 250.0

    assets_repo.soft_delete("TSLA")
    assert assets_repo.get("TSLA")["active"] == 0
    assert "TSLA" not in {a["ticker"] for a in assets_repo.list_active()}


# ---------------------------------------------------------------------------
# tx_repo
# ---------------------------------------------------------------------------


def _sample_tx(tx_id="tx-1", ticker="BTC", side="Buy", qty=1.0, price=100.0, fee=1.0,
                broker="Kraken", executed_at="2026-01-01T00:00:00-05:00"):
    return {
        "tx_id": tx_id,
        "ticker": ticker,
        "side": side,
        "qty": qty,
        "price": price,
        "fee": fee,
        "total": qty * price,
        "broker": broker,
        "source": "Auto_Bot",
        "executed_at": executed_at,
    }


def test_tx_repo_dedup(db_path):
    from db.repo import tx_repo

    first = tx_repo.insert_ignore(_sample_tx())
    second = tx_repo.insert_ignore(_sample_tx())  # same tx_id
    assert first is True
    assert second is False

    all_tx = tx_repo.list_recent(limit=10)
    assert len(all_tx) == 1


def test_tx_repo_other_constraint_violations_raise(db_path):
    """insert_ignore's dedup must ONLY swallow the tx_id UNIQUE violation.
    Any OTHER constraint failure (CHECK, NOT NULL) is a real bug -- the old
    INSERT OR IGNORE implementation silently dropped such rows, which is how
    a corrupt money value could mutate pool state without ever recording a
    transaction. See tx_repo.insert_ignore's docstring."""
    import sqlite3

    from db.repo import tx_repo

    # CHECK violation: side must be 'Buy' or 'Sell'.
    with pytest.raises(sqlite3.IntegrityError):
        tx_repo.insert_ignore(_sample_tx(tx_id="tx-bad-side", side="Hold"))

    # NOT NULL violation: qty is a NOT NULL column. (Set after building the
    # sample -- _sample_tx itself computes total = qty * price.)
    bad = _sample_tx(tx_id="tx-bad-qty")
    bad["qty"] = None
    with pytest.raises(sqlite3.IntegrityError):
        tx_repo.insert_ignore(bad)

    # ...and neither row landed, nor broke the connection for later writes.
    assert tx_repo.list_recent(limit=10) == []
    assert tx_repo.insert_ignore(_sample_tx(tx_id="tx-good")) is True


def test_tx_repo_list_recent_and_sum_kraken_btc_cost(db_path):
    from db.repo import tx_repo

    tx_repo.insert_ignore(_sample_tx(tx_id="tx-1", qty=1.0, price=100.0, fee=1.0))
    tx_repo.insert_ignore(_sample_tx(tx_id="tx-2", qty=2.0, price=110.0, fee=2.0))
    # Non-Kraken / non-Buy rows must not be counted.
    tx_repo.insert_ignore(_sample_tx(tx_id="tx-3", broker="IBKR", ticker="QQQ", qty=1, price=500))
    tx_repo.insert_ignore(_sample_tx(tx_id="tx-4", side="Sell", qty=0.5, price=120.0, fee=0.0))

    recent = tx_repo.list_recent(limit=10)
    assert len(recent) == 4

    cost, qty = tx_repo.sum_kraken_btc_cost()
    # (100*1 + 1) + (110*2 + 2) = 101 + 222 = 323 ; qty = 1 + 2 = 3
    assert cost == pytest.approx(323.0)
    assert qty == pytest.approx(3.0)


# ---------------------------------------------------------------------------
# state_repo
# ---------------------------------------------------------------------------


def test_state_repo_json_roundtrip(db_path):
    from db.repo import state_repo

    assert state_repo.get("nonexistent_key", default="fallback") == "fallback"

    state_repo.set("qqq_pool", 123.45)
    assert state_repo.get("qqq_pool") == 123.45

    complex_value = {"a": 1, "b": [1, 2, 3], "c": None}
    state_repo.set("blob", complex_value)
    assert state_repo.get("blob") == complex_value

    # overwrite
    state_repo.set("qqq_pool", 0.0)
    assert state_repo.get("qqq_pool") == 0.0


# ---------------------------------------------------------------------------
# config_repo
# ---------------------------------------------------------------------------


def test_config_repo_env_fallback(db_path, monkeypatch):
    from db.repo import config_repo

    monkeypatch.setenv("SOME_ENV_ONLY_KEY", "from_env")
    # Not in DB yet -> falls back to os.environ.
    assert config_repo.get("SOME_ENV_ONLY_KEY") == "from_env"
    # Not in DB and not in env -> default.
    assert config_repo.get("TOTALLY_MISSING_KEY", default="dflt") == "dflt"

    # Once set in DB, DB wins over env.
    config_repo.set("SOME_ENV_ONLY_KEY", "from_db")
    assert config_repo.get("SOME_ENV_ONLY_KEY") == "from_db"

    config_repo.set("SGOV_CASH_MULT", 1.95)
    assert config_repo.get_float("SGOV_CASH_MULT") == pytest.approx(1.95)
    assert config_repo.get_float("MISSING_FLOAT", default=2.0) == 2.0

    config_repo.set("INDICATOR_STALE_HOURS", 24)
    assert config_repo.get_int("INDICATOR_STALE_HOURS") == 24
    assert config_repo.get_int("MISSING_INT", default=7) == 7


# ---------------------------------------------------------------------------
# alerts_repo
# ---------------------------------------------------------------------------


def test_alerts_repo_crud(db_path):
    from db.repo import alerts_repo

    alert_id = alerts_repo.add("AAPL", 150.0, "Below")
    row = alerts_repo.get(alert_id)
    assert row["ticker"] == "AAPL"
    assert row["status"] == "Active"

    active = alerts_repo.list(status="Active")
    assert any(a["id"] == alert_id for a in active)

    alerts_repo.update_status(alert_id, "Triggered")
    row2 = alerts_repo.get(alert_id)
    assert row2["status"] == "Triggered"
    assert row2["triggered_at"] is not None

    alerts_repo.delete(alert_id)
    assert alerts_repo.get(alert_id) is None


# ---------------------------------------------------------------------------
# latch_repo
# ---------------------------------------------------------------------------


def test_latch_lifecycle(db_path):
    from db.repo import latch_repo

    # No row yet -> armed.
    assert latch_repo.is_armed("TSLA", "dip", 1) is True

    latch_repo.fire("TSLA", "dip", 1)
    assert latch_repo.is_armed("TSLA", "dip", 1) is False

    latch_repo.rearm("TSLA", "dip", 1)
    assert latch_repo.is_armed("TSLA", "dip", 1) is True

    # Firing again after rearm relatches it.
    latch_repo.fire("TSLA", "dip", 1)
    assert latch_repo.is_armed("TSLA", "dip", 1) is False

    # A different level for the same ticker/kind is armed by default.
    assert latch_repo.is_armed("TSLA", "dip", 2) is True

    latches = latch_repo.list_all()
    assert any(l["ticker"] == "TSLA" and l["kind"] == "dip" and l["level"] == 1 for l in latches)


def test_latch_rearm_all_for(db_path):
    from db.repo import latch_repo

    latch_repo.fire("NVDA", "dip", 1)
    latch_repo.fire("NVDA", "stop_loss", 2)
    assert latch_repo.is_armed("NVDA", "dip", 1) is False
    assert latch_repo.is_armed("NVDA", "stop_loss", 2) is False

    latch_repo.rearm_all_for("NVDA")
    assert latch_repo.is_armed("NVDA", "dip", 1) is True
    assert latch_repo.is_armed("NVDA", "stop_loss", 2) is True


# ---------------------------------------------------------------------------
# indicators_repo
# ---------------------------------------------------------------------------


def test_indicators_repo(db_path):
    from db.repo import indicators_repo

    assert indicators_repo.last_good("cnn_fng") is None
    assert indicators_repo.latest("cnn_fng") is None

    indicators_repo.insert("cnn_fng", 45.0, ok=True)
    indicators_repo.insert("cnn_fng", 50.0, ok=False)  # a failed fetch, not a real value

    value, fetched_at = indicators_repo.last_good("cnn_fng")
    assert value == 45.0
    assert fetched_at is not None

    latest = indicators_repo.latest("cnn_fng")
    assert latest["value"] == 50.0
    assert latest["ok"] == 0


# ---------------------------------------------------------------------------
# positions_repo
# ---------------------------------------------------------------------------


def test_positions_reconcile_ibkr_leaves_kraken_row(db_path):
    from db.repo import positions_repo

    positions_repo.upsert({"ticker": "AAPL", "qty": 10, "avg_cost": 150.0, "broker": "IBKR"})
    positions_repo.upsert({"ticker": "MSFT", "qty": 5, "avg_cost": 300.0, "broker": "IBKR"})
    positions_repo.upsert_kraken_btc(qty=0.5, avg_cost=40000.0, price=45000.0)

    all_positions = {p["ticker"] for p in positions_repo.list_all()}
    assert {"AAPL", "MSFT", "BTC"}.issubset(all_positions)

    # Simulate a new snapshot where MSFT was fully sold (no longer present).
    positions_repo.reconcile_ibkr({"AAPL"})

    remaining = {p["ticker"]: p for p in positions_repo.list_all()}
    assert "AAPL" in remaining
    assert "MSFT" not in remaining
    # The Kraken BTC row must be untouched by an IBKR reconciliation.
    assert "BTC" in remaining
    assert remaining["BTC"]["broker"] == "Kraken"
    assert remaining["BTC"]["qty"] == 0.5


def test_positions_reconcile_ibkr_empty_set_deletes_all_ibkr_but_not_kraken(db_path):
    """The most destructive branch: a successful snapshot that returned zero
    holdings must wipe every IBKR row -- but never the Kraken BTC row."""
    from db.repo import positions_repo

    positions_repo.upsert({"ticker": "AAPL", "qty": 10, "avg_cost": 150.0, "broker": "IBKR"})
    positions_repo.upsert({"ticker": "SGOV", "qty": 20, "avg_cost": 100.0, "broker": "IBKR"})
    positions_repo.upsert_kraken_btc(qty=0.5, avg_cost=40000.0, price=45000.0)

    positions_repo.reconcile_ibkr(set())

    remaining = {p["ticker"]: p for p in positions_repo.list_all()}
    assert "AAPL" not in remaining
    assert "SGOV" not in remaining
    assert set(remaining) == {"BTC"}
    assert remaining["BTC"]["broker"] == "Kraken"


def test_upsert_kraken_btc_zero_qty_deletes_row(db_path):
    """qty<=0 / avg_cost None must never write a row (positions.avg_cost is
    NOT NULL); it deletes any existing Kraken BTC row instead."""
    from db.repo import positions_repo

    # Zero balance with no existing row: no-op, no IntegrityError.
    positions_repo.upsert_kraken_btc(qty=0, avg_cost=None, price=45000.0)
    assert all(p["ticker"] != "BTC" for p in positions_repo.list_all())

    # Existing row, then balance goes to ~0 -> row removed.
    positions_repo.upsert_kraken_btc(qty=0.5, avg_cost=40000.0, price=45000.0)
    assert any(p["ticker"] == "BTC" for p in positions_repo.list_all())
    positions_repo.upsert_kraken_btc(qty=0.0, avg_cost=40000.0, price=45000.0)
    assert all(p["ticker"] != "BTC" for p in positions_repo.list_all())

    # avg_cost None alone also refuses to write (and clears any stale row).
    positions_repo.upsert_kraken_btc(qty=0.5, avg_cost=40000.0, price=45000.0)
    positions_repo.upsert_kraken_btc(qty=0.5, avg_cost=None, price=45000.0)
    assert all(p["ticker"] != "BTC" for p in positions_repo.list_all())


def test_positions_delete(db_path):
    from db.repo import positions_repo

    positions_repo.upsert({"ticker": "GOOG", "qty": 1, "avg_cost": 100.0, "broker": "IBKR"})
    positions_repo.delete("GOOG")
    assert positions_repo.list_all() == [] or all(
        p["ticker"] != "GOOG" for p in positions_repo.list_all()
    )


# ---------------------------------------------------------------------------
# reset_satellite_state
# ---------------------------------------------------------------------------


def test_reset_satellite_state(db_path):
    from db.repo import assets_repo, latch_repo, reset_satellite_state

    assets_repo.upsert(
        {
            "ticker": "TSLA",
            "asset_type": "Satellite",
            "entry_count": 3,
            "base_price": 300.0,
            "entry_atr": 12.5,
            "stop_loss_cal_price": 280.0,
            "monitor_reversal": 1,
        }
    )
    latch_repo.fire("TSLA", "dip", 3)
    latch_repo.fire("TSLA", "stop_loss", 3)
    assert latch_repo.is_armed("TSLA", "dip", 3) is False
    assert latch_repo.is_armed("TSLA", "stop_loss", 3) is False

    reset_satellite_state("TSLA")

    row = assets_repo.get("TSLA")
    assert row["entry_count"] == 0
    assert row["base_price"] is None
    assert row["entry_atr"] is None
    assert row["stop_loss_cal_price"] is None
    assert row["monitor_reversal"] == 0

    assert latch_repo.is_armed("TSLA", "dip", 3) is True
    assert latch_repo.is_armed("TSLA", "stop_loss", 3) is True
