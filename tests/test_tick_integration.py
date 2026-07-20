"""Integration-style test for the real-money wiring of a full DCA-day tick.

Unlike tests/test_tick.py (which fakes whole stages to test the wrapper's
lock/error semantics), this file runs the REAL pipeline code -- the actual
execute_ibkr_dca / rebalance_cash_with_sgov / sync_ibkr_trades /
snapshot_portfolio implementations against a real (temp-file) SQLite DB --
and only fakes the process boundaries:

  - IB Gateway            -> FakeIB (records placed orders, returns a
                              canned portfolio / fills / account summary)
  - Yahoo prices/highs    -> dict-backed fakes in trading.broker_utils
  - Kraken (ccxt)         -> execute_kraken_dca / sync_kraken_trades /
                              _snapshot_kraken_btc_position faked at the
                              tick-module seam
  - LINE                  -> FakeLineBot (records, never pushes)
  - calendar              -> is_dca_day forced to 'First_Day', window True
  - sentiment indicators  -> F&G forced to 25 (extreme-fear bracket)

No network, no real orders, no real LINE pushes.

Seeded numbers and what they must produce (BASE_AMOUNT=1000, F&G=25):
  allocations (fng<30): qqq/stock/sat contrib = 60 / 60 / 180
  QQQ:  price 90 < 4wk-high 100 * 0.97 -> dip-add triggers.
        pool 100 + 60 contrib = 160; dip_add_draw(160, 60):
        old=100, alloc=33.33, dynamic=min(93.33, 240, 160)=93.33
        -> pool ends at 66.67, last_buy=90, one manual-report line.
  VT:   price 100, high 100 -> no trigger; pool = 50 + 60 = 110 untouched.
  IAU:  budget = 1000*0.05 + rollover 30 = 80; price 40 -> BUY 2 whole
        shares, rollover -> 0.
  SGOV: cash 2000, target 1950, buffer 1000 -> within band, no order.
"""

from datetime import datetime
from types import SimpleNamespace

import pytest
import pytz

from config.config_loader import est


@pytest.fixture()
def db_path(tmp_path, monkeypatch):
    path = tmp_path / "test.db"
    monkeypatch.setenv("INVESTBOT_DB", str(path))
    from db.database import init_db

    init_db()
    return str(path)


def _portfolio_item(symbol, qty, avg_cost, price):
    return SimpleNamespace(
        contract=SimpleNamespace(symbol=symbol),
        position=qty,
        averageCost=avg_cost,
        marketPrice=price,
        marketValue=qty * price,
        unrealizedPNL=qty * (price - avg_cost),
    )


def _fill(exec_id, symbol, side, qty, price, order_ref=""):
    return SimpleNamespace(
        execution=SimpleNamespace(
            execId=exec_id,
            orderRef=order_ref,
            side=side,  # 'BOT'/'SLD'
            avgPrice=price,
            cumQty=qty,
            time=datetime.now(pytz.utc),
        ),
        commissionReport=SimpleNamespace(commission=1.0),
        contract=SimpleNamespace(symbol=symbol),
    )


class FakeIB:
    """Connected IB fake that records orders and serves canned data."""

    def __init__(self):
        self._connected = True
        self.placed_orders = []  # (symbol, action, qty, orderRef)
        self.portfolio_items = [
            _portfolio_item("IAU", 2.0, 40.0, 41.0),
            _portfolio_item("SGOV", 20.0, 100.0, 100.5),
            _portfolio_item("TSLA", 5.0, 200.0, 200.0),
        ]
        self.fill_items = [_fill("exec-tsla-1", "TSLA", "BOT", 5.0, 200.0)]

    def isConnected(self):
        return self._connected

    def disconnect(self):
        self._connected = False

    def portfolio(self):
        return self.portfolio_items

    def fills(self):
        return self.fill_items

    def positions(self):
        return [SimpleNamespace(contract=SimpleNamespace(symbol="SGOV"), position=20.0)]

    def accountSummary(self):
        return [
            SimpleNamespace(tag="NetLiquidation", currency="USD", value="10000"),
            SimpleNamespace(tag="TotalCashValue", currency="USD", value="2000"),
            SimpleNamespace(tag="AvailableFunds", currency="USD", value="2000"),
        ]

    def qualifyContracts(self, contract):
        return [contract]

    def placeOrder(self, contract, order):
        self.placed_orders.append(
            (contract.symbol, order.action, float(order.totalQuantity), order.orderRef)
        )

    def sleep(self, seconds):
        pass


class FakeLineBot:
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


YAHOO_PRICES = {"QQQ": 90.0, "VT": 100.0, "IAU": 40.0, "SGOV": 100.0}
FOUR_WEEK_HIGHS = {"QQQ": 100.0, "VT": 100.0}


@pytest.fixture()
def dca_day_env(db_path, monkeypatch):
    """Seed config/state/assets/positions and fake all process boundaries
    for a forced First_Day DCA tick. Returns (tick_module, fake_ib,
    fake_line_bot)."""
    from db.repo import config_repo, state_repo, assets_repo, positions_repo

    # --- config seeds ---
    config_repo.set("BASE_AMOUNT", 1000)
    config_repo.set("DCA_STOCK_TICKER", "VT")
    config_repo.set("QQQ_FIXED_PCT", 0.30)
    config_repo.set("STOCK_FIXED_PCT", 0.15)
    config_repo.set("SGOV_CASH_MULT", 1.95)
    config_repo.set("KRAKEN_BTC_PCT", 0.10)
    config_repo.set("INDICATOR_STALE_HOURS", 24)

    # --- pool state ---
    state_repo.set("qqq_pool", 100.0)
    state_repo.set("stock_pool", 50.0)
    state_repo.set("satellite_pool", 1000.0)
    state_repo.set("qqq_last_buy_price", 0.0)
    state_repo.set("stock_last_buy_price", 0.0)
    state_repo.set("iau_rollover", 30.0)

    # --- watchlist: one held satellite ---
    assets_repo.upsert(
        {
            "ticker": "TSLA",
            "asset_type": "Satellite",
            "price_source": "yahoo",
            "entry_count": 1,
            "base_price": 300.0,
            "tier": "T1",
        }
    )

    # --- pre-existing positions: a stale IBKR row + the Kraken BTC row ---
    positions_repo.upsert({"ticker": "MSFT", "qty": 3, "avg_cost": 300.0, "broker": "IBKR"})
    positions_repo.upsert_kraken_btc(qty=0.5, avg_cost=40000.0, price=45000.0)

    import jobs.tick as tick_module
    import trading.broker_utils as broker_utils
    import trading.transaction_logger as tx_logger

    fake_ib = FakeIB()
    fake_line_bot = FakeLineBot()

    # LINE faked in every namespace that pushes.
    monkeypatch.setattr(tick_module, "line_bot", fake_line_bot)
    monkeypatch.setattr(broker_utils, "line_bot", fake_line_bot)
    monkeypatch.setattr(tx_logger, "line_bot", fake_line_bot)

    # IB Gateway faked.
    monkeypatch.setattr(tick_module, "connect_ib", lambda: fake_ib)

    # Yahoo faked (both price and 4-week-high, in broker_utils' namespace
    # where execute_ibkr_dca/rebalance_cash_with_sgov resolve them).
    monkeypatch.setattr(broker_utils, "get_yahoo_price", lambda t: YAHOO_PRICES.get(t))
    monkeypatch.setattr(broker_utils, "get_4_week_high", lambda t: FOUR_WEEK_HIGHS.get(t))

    # Asset-price stage faked at the tick seam (yfinance otherwise).
    monkeypatch.setattr(
        tick_module, "process_asset_price", lambda t, src: (YAHOO_PRICES.get(t, 200.0), "Yahoo")
    )
    monkeypatch.setattr(
        tick_module, "get_strategy_metrics", lambda t: {"HV180": 25.0, "CurrentPrice": 200.0}
    )

    # Kraken (ccxt) faked at the tick seams.
    kraken_calls = []
    monkeypatch.setattr(
        tick_module, "execute_kraken_dca", lambda: kraken_calls.append("btc_dca") or {"status": "Success"}
    )
    monkeypatch.setattr(tick_module, "sync_kraken_trades", lambda: None)
    monkeypatch.setattr(tick_module, "_snapshot_kraken_btc_position", lambda: None)
    tick_module._test_kraken_calls = kraken_calls  # inspection hook

    # Sentiment forced to extreme fear; calendar forced to First_Day in
    # window; no 15s settle sleep in tests.
    monkeypatch.setattr(tick_module, "_update_market_indicators", lambda: 25)
    monkeypatch.setattr(tick_module, "is_dca_day", lambda: "First_Day")
    monkeypatch.setattr(tick_module, "is_nyse_dca_window", lambda: True)
    monkeypatch.setattr(tick_module, "check_dca_schedule", lambda: None)
    monkeypatch.setattr(tick_module, "POST_DCA_SETTLE_SECONDS", 0)

    return tick_module, fake_ib, fake_line_bot


def test_full_dca_day_tick_end_to_end(dca_day_env):
    tick_module, fake_ib, fake_line_bot = dca_day_env
    from db.repo import state_repo, assets_repo, positions_repo, tx_repo

    tick_module.run_tick()

    # The tick completed cleanly.
    assert state_repo.get("last_tick_ok") is True
    assert state_repo.get("tick_lock") is None

    # (a) IAU order placed with the whole-share qty implied by the F&G
    # bracket + rollover math: 1000*0.05 + 30 = $80 budget @ $40 = 2 shares,
    # via orderRef '999' (the bot-order marker). SGOV stayed in band -> the
    # IAU order is the ONLY order of the whole tick.
    assert fake_ib.placed_orders == [("IAU", "BUY", 2.0, "999")]

    # ...and the sub-share remainder rolled over (80 - 2*40 = 0).
    assert state_repo.get("iau_rollover") == pytest.approx(0.0)

    # (b) QQQ pool mutated per dip_add_draw: 100+60 contrib, draw 93.33.
    assert state_repo.get("qqq_pool") == pytest.approx(160.0 - 93.333333, abs=0.01)
    assert state_repo.get("qqq_last_buy_price") == pytest.approx(90.0)
    # VT didn't trigger: pool just accrued its contribution, last buy untouched.
    assert state_repo.get("stock_pool") == pytest.approx(110.0)
    assert state_repo.get("stock_last_buy_price") == pytest.approx(0.0)
    # Satellite pool accrued its contribution (no satellite order placed).
    assert state_repo.get("satellite_pool") == pytest.approx(1180.0)

    # (c) manual_report constructed AND actually sent (the v2 never-sent
    # bug): exactly one entry, for QQQ (VT didn't trigger).
    manual_sends = [args for kind, args, _ in fake_line_bot.sent if kind == "manual_dca_instruction"]
    assert len(manual_sends) == 1
    (instructions,) = manual_sends[0]
    assert len(instructions) == 1
    qqq_line = instructions[0]
    assert "QQQ" in qqq_line

    # USER DECISION pin (real money): the IBKR recurring investment already
    # auto-buys the FIXED portion (BASE*QQQ_FIXED_PCT = $300.0), so the
    # instructed manual-buy amount must be the DYNAMIC part ONLY ($93.3 --
    # the dip_add_draw result). Instructing fixed+dynamic ($393.3) would
    # double-buy the fixed part.
    assert "請手動買入: $93.3" in qqq_line
    assert "$393.3" not in qqq_line
    # ...and the fixed part is called out as already auto-bought.
    assert "$300.0" in qqq_line
    assert "勿重複下單" in qqq_line

    # Kraken BTC DCA ran in the window.
    assert tick_module._test_kraken_calls == ["btc_dca"]

    # (d) snapshot wrote position rows; reconcile removed the stale MSFT
    # row (absent from the fake portfolio) but left the Kraken BTC row.
    remaining = {p["ticker"]: p for p in positions_repo.list_all()}
    assert set(remaining) == {"IAU", "SGOV", "TSLA", "BTC"}
    assert "MSFT" not in remaining
    assert remaining["BTC"]["broker"] == "Kraken"
    assert remaining["TSLA"]["qty"] == pytest.approx(5.0)
    assert remaining["IAU"]["avg_cost"] == pytest.approx(40.0)
    # Account summary keys landed in app_state too.
    assert state_repo.get("account_net_liq") == pytest.approx(10000.0)
    assert state_repo.get("account_available_funds") == pytest.approx(2000.0)

    # (e) the TSLA Buy fill was recorded once and advanced strategy state.
    tsla = assets_repo.get("TSLA")
    assert tsla["entry_count"] == 2
    assert tsla["last_buy_date"] == datetime.now().strftime("%Y-%m-%d")
    tx_ids = {t["tx_id"] for t in tx_repo.list_recent()}
    assert "exec-tsla-1" in tx_ids


def test_second_run_does_not_double_count_fill(dca_day_env, monkeypatch):
    """Re-running the tick with the same fill list must not re-insert the
    tx (dedup on execId) nor bump entry_count again."""
    tick_module, fake_ib, _ = dca_day_env
    from db.repo import assets_repo, tx_repo

    tick_module.run_tick()
    # Second run: not a DCA day anymore, same fills returned by FakeIB.
    monkeypatch.setattr(tick_module, "is_dca_day", lambda: None)
    tick_module.run_tick()

    assert assets_repo.get("TSLA")["entry_count"] == 2  # not 3
    all_tx = [t for t in tx_repo.list_recent() if t["tx_id"] == "exec-tsla-1"]
    assert len(all_tx) == 1


def test_snapshot_portfolio_empty_read_skips_reconcile(db_path):
    """Fix for the empty-but-non-raising ib.portfolio() read: an empty list
    must NOT reconcile (which would wipe every IBKR row) -- existing
    positions survive untouched."""
    from db.repo import positions_repo, state_repo
    from services.ibkr import snapshot_portfolio

    positions_repo.upsert({"ticker": "IAU", "qty": 2, "avg_cost": 40.0, "broker": "IBKR"})
    positions_repo.upsert({"ticker": "SGOV", "qty": 20, "avg_cost": 100.0, "broker": "IBKR"})

    empty_ib = FakeIB()
    empty_ib.portfolio_items = []

    snapshot_portfolio(empty_ib)

    remaining = {p["ticker"] for p in positions_repo.list_all()}
    assert {"IAU", "SGOV"}.issubset(remaining)
    # Account summary still refreshed even on the empty read.
    assert state_repo.get("account_net_liq") == pytest.approx(10000.0)
