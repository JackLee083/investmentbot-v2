"""Order placement + satellite strategy checks for Investment Bot v3.

Phase 2 changes from the v2 version of this file:
- Pool state (qqq_pool/stock_pool/satellite_pool/... ) moves from the
  0-byte-prone data/dca_state.json file to db.repo.state_repo (SQLite).
- BASE_AMOUNT / SGOV_CASH_TARGET / KRAKEN_BTC_AMOUNT are no longer
  import-time constants -- they're looked up from db.repo.config_repo AT USE
  TIME (DB row first, env fallback), so editing them on the dashboard takes
  effect on the very next tick with no restart.
- The actual allocation/IAU/dip-add/SGOV *math* now lives in core/dca.py
  (pure, unit-tested); this module only wires DB state + IBKR order calls
  around those pure functions.
- check_satellite_opportunities/check_stop_loss_notifications are rewritten
  against assets_repo + alert_latches (per-ticker-per-level latches) instead
  of Notion's single "Sat Notified"/"ATR Notified" checkboxes, which were
  one-shot and never reset anywhere -- see PLAN_V3.md §1 bug list. Order
  placement mechanics themselves (IAU whole shares + rollover, orderRef
  '999', SGOV sweep) are unchanged.
"""

import math

import yfinance as yf
from ib_insync import IB, Stock, Order, MarketOrder

from config.config_loader import KRAKEN_KEY, KRAKEN_SECRET
from core.dca import get_allocations, iau_budget, dip_add_draw, sgov_orders
from core.portfolio import entry_prices, next_entry_amount, dip_trigger, stop_levels, tier_params
from db.repo import state_repo, config_repo, assets_repo, latch_repo
from marketdata.fetchers import get_yahoo_price, get_4_week_high
from services.line_notify import line_bot

import ccxt

# app_state keys that make up the DCA pool/state -- see db/schema.sql's
# app_state comment for the full canonical list.
_POOL_STATE_KEYS = [
    "qqq_pool",
    "stock_pool",
    "satellite_pool",
    "qqq_last_buy_price",
    "stock_last_buy_price",
    "satellite_first_buy_price",
    "iau_rollover",
]


def _load_pool_state():
    return {k: state_repo.get(k, 0.0) for k in _POOL_STATE_KEYS}


def _save_pool_state(state):
    for key in _POOL_STATE_KEYS:
        state_repo.set(key, state.get(key, 0.0))
    print(
        f"State updated: QQQ pool ${state['qqq_pool']:.1f} | "
        f"{config_repo.get('DCA_STOCK_TICKER')} pool ${state['stock_pool']:.1f} | "
        f"Sat pool ${state['satellite_pool']:.1f}"
    )


def _base_amount():
    return config_repo.get_float("BASE_AMOUNT")


# ---------------------------------------------------------------------------
# Kraken BTC DCA
# ---------------------------------------------------------------------------


def execute_kraken_dca(amount_usd=None, symbol="BTC/USD"):
    """Market-buy `amount_usd` worth of BTC on Kraken. `amount_usd` defaults
    to BASE_AMOUNT * KRAKEN_BTC_PCT, both read from config_repo at call time
    (so a dashboard edit to either takes effect on the very next tick)."""
    if amount_usd is None:
        base = _base_amount()
        pct = config_repo.get_float("KRAKEN_BTC_PCT", default=0.10)
        amount_usd = base * pct

    exchange = ccxt.kraken(
        {
            "apiKey": KRAKEN_KEY,
            "secret": KRAKEN_SECRET,
            "enableRateLimit": True,
        }
    )
    params = {"userref": 999}  # marks this as a bot order for sync/dedup

    try:
        ticker = exchange.fetch_ticker(symbol)
        current_price = ticker["last"]
        amount_to_buy = amount_usd / current_price

        order = exchange.create_market_buy_order(symbol, amount_to_buy, params=params)

        print(f"Kraken buy succeeded. Order ID: {order['id']} | Price: {order['price']}")
        return {
            "status": "Success",
            "order_id": order["id"],
            "price": order["price"],
            "amount": amount_to_buy,
            "broker": "Kraken",
            "userref": params["userref"],
        }
    except Exception as e:
        print(f"Kraken order failed: {e}")
        return {"status": "Failed", "error": str(e)}


# ---------------------------------------------------------------------------
# IBKR DCA (QQQ/stock manual-instruction accounting + IAU auto-buy)
# ---------------------------------------------------------------------------


def execute_ibkr_dca(ib, fng_val, dca_type):
    """Run the IBKR side of a DCA cycle:

    1. QQQM / stock: monitor-only. The FIXED monthly amount is bought via
       IBKR's own recurring investment feature (not this bot), so it needs
       no instruction. Only the EXTRA dip-add draw from the pool (when
       triggered) needs a manual LINE instruction, since IBKR's recurring
       investment has no concept of that dynamic top-up.
    2. IAU: auto-bought via the API based on the F&G-scaled budget, with
       any leftover (too small for a whole share) rolled over to next time.

    Returns `manual_report`: a list[str], one line per QQQ/stock dip-add
    that fired this cycle. The caller (jobs/tick.py) is responsible for
    actually sending it via line_bot.send_manual_dca_instruction -- in v2
    this list was built but never sent (see PLAN_V3.md §1 bug list).
    """
    try:
        state = _load_pool_state()
        base = _base_amount()
        dca_stock_ticker = config_repo.get("DCA_STOCK_TICKER")

        qqq_pool_pct, stock_pool_pct, sat_pool_pct = get_allocations(fng_val)

        qqq_fixed_pct = config_repo.get_float("QQQ_FIXED_PCT", default=0.30)
        stock_fixed_pct = config_repo.get_float("STOCK_FIXED_PCT", default=0.15)
        qqq_fixed = base * qqq_fixed_pct
        stock_fixed = base * stock_fixed_pct

        qqq_contrib = base * qqq_pool_pct
        stock_contrib = base * stock_pool_pct
        sat_contrib = base * sat_pool_pct

        print(
            f"FNG: {fng_val} | contribution allocation: QQQ +${qqq_contrib:.1f}, "
            f"{dca_stock_ticker} +${stock_contrib:.1f}, Sat +${sat_contrib:.1f}"
        )

        state["qqq_pool"] += qqq_contrib
        state["stock_pool"] += stock_contrib
        state["satellite_pool"] += sat_contrib

        orders_to_place = []  # auto-placed via API (IAU)
        manual_report = []  # QQQ/stock dip-add instructions for LINE

        # NOTE on the manual instruction amounts below (USER DECISION, real
        # money): the user's IBKR recurring investment covers ONLY the fixed
        # monthly portion (BASE x QQQ_FIXED_PCT / BASE x STOCK_FIXED_PCT) --
        # it buys that part automatically every cycle. So the LINE manual
        # instruction must tell the user to hand-buy the DYNAMIC part ONLY;
        # instructing fixed+dynamic (as v2's never-sent draft did) would
        # make the user DOUBLE-BUY the fixed part on every dip cycle. The
        # dynamic draw from dip_add_draw() already folds this cycle's FNG
        # contribution into its capped sum, so 'FNG contrib' vs 'dip-add'
        # aren't separable here -- the message shows a two-line breakdown:
        # fixed (already auto-bought, do NOT re-buy) vs manual-buy amount.
        # Pool accounting below is untouched -- message content only.

        # --- QQQ ---
        qqq_price = get_yahoo_price("QQQ")
        qqq_high = get_4_week_high("QQQ")
        if qqq_price and qqq_high:
            last_buy = state.get("qqq_last_buy_price", 0.0)
            trigger_high = qqq_price < (qqq_high * 0.97)
            trigger_last = (last_buy > 0) and (qqq_price < (last_buy * 0.98))

            if trigger_high or trigger_last:
                dynamic_part = dip_add_draw(state["qqq_pool"], qqq_contrib)
                state["qqq_pool"] -= dynamic_part
                state["qqq_last_buy_price"] = qqq_price

                manual_report.append(
                    f"● QQQ 本次請手動買入: ${dynamic_part:.1f}\n"
                    f"   ├ 固定部分 ${qqq_fixed:.1f} — IBKR 定期定額已自動買入，勿重複下單\n"
                    f"   └ 加碼部分 ${dynamic_part:.1f}（FNG 動態提撥＋逢低加碼池提取）\n"
                    f"   (QQQ 池餘額: ${state['qqq_pool']:.1f})"
                )

        # --- DCA stock (second ticker) ---
        stk_price = get_yahoo_price(dca_stock_ticker)
        stk_high = get_4_week_high(dca_stock_ticker)
        if stk_price and stk_high:
            last_buy = state.get("stock_last_buy_price", 0.0)
            trigger_high = stk_price < (stk_high * 0.94)
            trigger_last = (last_buy > 0) and (stk_price < (last_buy * 0.96))

            if trigger_high or trigger_last:
                dynamic_part = dip_add_draw(state["stock_pool"], stock_contrib)
                state["stock_pool"] -= dynamic_part
                state["stock_last_buy_price"] = stk_price

                # Same USER DECISION as the QQQ branch above: instruct the
                # dynamic part only; the fixed part is auto-bought by IBKR.
                manual_report.append(
                    f"● {dca_stock_ticker} 本次請手動買入: ${dynamic_part:.1f}\n"
                    f"   ├ 固定部分 ${stock_fixed:.1f} — IBKR 定期定額已自動買入，勿重複下單\n"
                    f"   └ 加碼部分 ${dynamic_part:.1f}（FNG 動態提撥＋逢低加碼池提取）\n"
                    f"   (池餘額: ${state['stock_pool']:.1f})"
                )

        # --- IAU (auto-bought via API) ---
        iau_base = iau_budget(fng_val, base)
        iau_rollover = state.get("iau_rollover", 0.0)
        iau_total_budget = iau_base + iau_rollover

        iau_price = get_yahoo_price("IAU")
        if iau_price and iau_price > 0:
            iau_shares = math.floor(iau_total_budget / iau_price)
            if iau_shares >= 1:
                cost = iau_shares * iau_price
                orders_to_place.append({"symbol": "IAU", "shares": iau_shares})
                state["iau_rollover"] = round(iau_total_budget - cost, 2)
                print(
                    f"IAU budget ${iau_total_budget} (incl. rollover ${iau_rollover}) -> "
                    f"bought {iau_shares} shares -> remaining ${state['iau_rollover']}"
                )
            else:
                state["iau_rollover"] = round(iau_total_budget, 2)
                print(f"IAU total budget ${iau_total_budget} is not enough for 1 share (${iau_price}), fully rolling over to next month.")
        else:
            print("Could not fetch IAU price, budget kept unchanged.")

        for item in orders_to_place:
            symbol = item["symbol"]
            shares = item["shares"]

            contract = Stock(symbol, "SMART", "USD")
            ib.qualifyContracts(contract)

            order = Order()
            order.action = "BUY"
            order.orderType = "MKT"
            order.totalQuantity = float(shares)
            order.tif = "DAY"
            order.orderRef = "999"
            order.transmit = True

            ib.placeOrder(contract, order)
            print(f"{symbol} order placed successfully: {shares} shares")
            ib.sleep(1.0)

        _save_pool_state(state)
        return manual_report

    except Exception as e:
        print(f"IBKR DCA execution failed: {e}")
        return []


# ---------------------------------------------------------------------------
# SGOV cash sweep
# ---------------------------------------------------------------------------


def rebalance_cash_with_sgov(ib, target_cash=None):
    """Two-way cash sweep against SGOV: sell SGOV when cash is short of
    target, buy SGOV when cash is well above target. `target_cash` defaults
    to BASE_AMOUNT * SGOV_CASH_MULT (config_repo, at call time)."""
    try:
        base = _base_amount()
        if target_cash is None:
            mult = config_repo.get_float("SGOV_CASH_MULT", default=1.95)
            target_cash = base * mult

        summary = ib.accountSummary()
        current_cash = next(
            (float(item.value) for item in summary if item.tag == "AvailableFunds" and item.currency == "USD"),
            0.0,
        )
        print(f"Currently available cash: ${current_cash}")

        positions = ib.positions()
        sgov_position_raw = next((float(p.position) for p in positions if p.contract.symbol == "SGOV"), 0.0)
        max_sellable_shares = math.floor(sgov_position_raw)

        price = get_yahoo_price("SGOV")
        if not price or price <= 0:
            print("Could not fetch SGOV price, cancelling rebalance.")
            return

        decision = sgov_orders(current_cash, target_cash, base, price, max_sellable_shares)
        if decision is None:
            print("Cash level is within target range, no adjustment needed.")
            return

        contract = Stock("SGOV", "SMART", "USD")
        ib.qualifyContracts(contract)

        action, qty = decision
        order = MarketOrder(action, qty, orderRef="999")
        ib.placeOrder(contract, order)
        print(f"SGOV {action} order sent: {qty} shares")

    except Exception as e:
        print(f"SGOV rebalance failed: {e}")


def get_ibkr_usd_cash(ib):
    """Total USD cash (TotalCashValue) from the IBKR account summary, or
    0.0 if it can't be found/fetched."""
    try:
        summary = ib.accountSummary()
        for item in summary:
            if item.tag == "TotalCashValue" and item.currency == "USD":
                return float(item.value)
        print("Warning: USD TotalCashValue not found in Account Summary, returning 0.0")
        return 0.0
    except Exception as e:
        print(f"Error occurred while fetching cash balance: {e}")
        return 0.0


# ---------------------------------------------------------------------------
# Satellite dip-buy alerts
# ---------------------------------------------------------------------------


def check_satellite_opportunities(price_cache):
    """Check every active Satellite asset for a dip-buy opportunity.

    `price_cache`: {ticker: current_price} built earlier this tick (falls
    back to the asset's stored current_price if a ticker is missing, e.g.
    price_source='skip').

    Per PLAN_V3.md §4: next_level = entry_count + 1 (skipped once > 3);
    target price is core.portfolio.entry_prices()[next_level-1]; amount is
    core.portfolio.next_entry_amount(entry_count, satellite_pool); trigger
    is core.portfolio.dip_trigger(). The (ticker, 'dip', next_level) latch
    gates re-firing -- unlike v2's single "Sat Notified" checkbox (which
    never reset and permanently silenced the ticker after one alert), this
    latch is scoped per level, so it auto-re-arms itself in effect: once
    entry_count advances (a fill bumps it), next_level changes to a level
    that has never fired, so it's armed again with no explicit re-arm call
    needed.
    """
    print("\n--- Checking for dip-buy opportunities ---")
    satellite_pool = state_repo.get("satellite_pool", 0.0)

    for asset in assets_repo.list_active(asset_type="Satellite"):
        ticker = asset["ticker"]
        entry_count = asset.get("entry_count") or 0
        next_level = entry_count + 1
        if next_level > 3:
            continue

        curr_price = price_cache.get(ticker)
        if curr_price is None:
            curr_price = asset.get("current_price")
        if not curr_price:
            continue

        base_price = asset.get("base_price")
        tier = asset.get("tier") or "T1"
        prices = entry_prices(base_price, tier)
        if not prices:
            continue

        target_price = prices[next_level - 1]
        target_amount = next_entry_amount(entry_count, satellite_pool)

        if not dip_trigger(curr_price, target_price):
            continue

        if not latch_repo.is_armed(ticker, "dip", next_level):
            continue

        print(f"{ticker} triggered a dip-buy opportunity notification (Level {next_level})")
        line_bot.send_dip_alert(ticker, curr_price, target_price, target_amount, next_level)
        latch_repo.fire(ticker, "dip", next_level)


# ---------------------------------------------------------------------------
# Satellite stop-loss / reversal notifications (notification-only)
# ---------------------------------------------------------------------------


def check_stop_loss_notifications(price_cache):
    """Check every held (entry_count > 0) Satellite asset for a stop-loss or
    3-day-reversal notification. Notification-only -- no auto-sell.

    Latch key is (ticker, 'stop_loss', entry_count): a fresh entry (higher
    entry_count) is a fresh position size, so it gets its own latch level,
    matching the dip-latch's per-level scoping.
    """
    print("\n--- Checking stop-loss/exit signals (Notification Only) ---")

    for asset in assets_repo.list_active(asset_type="Satellite"):
        ticker = asset["ticker"]
        entry_count = asset.get("entry_count") or 0
        if entry_count == 0:
            continue

        # Latch check FIRST (mirrors v2, where "ATR Notified" was the very
        # first gate): a latched ticker is skipped wholesale -- no yfinance
        # history call, no monitor_reversal mutation, no work at all. This
        # matters beyond just saving an HTTP call: if the 3-day-reversal
        # condition holds while the latch is closed, clearing
        # monitor_reversal here would silently consume the (one-shot)
        # reversal monitor without any alert ever reaching the user.
        if not latch_repo.is_armed(ticker, "stop_loss", entry_count):
            continue

        curr_price = price_cache.get(ticker)
        if curr_price is None:
            curr_price = asset.get("current_price")

        base_price = asset.get("base_price")
        entry_atr = asset.get("entry_atr")
        tier = asset.get("tier") or "T1"
        monitor_reversal = bool(asset.get("monitor_reversal"))

        if not curr_price or not base_price or not entry_atr:
            continue

        levels = stop_levels(base_price, entry_atr, tier)
        mult = tier_params(tier)["atr_mult"]

        triggered = False
        reason = ""
        clear_reversal = False

        if curr_price < levels["alert_threshold"]:
            triggered = True
            reason = f"ATR stop-loss breached ({tier} - {mult}x)"
        elif monitor_reversal and (curr_price < base_price):
            try:
                df = yf.Ticker(ticker).history(period="5d")
                if len(df) >= 3:
                    last_3_closes = df["Close"].tail(3).values
                    if all(c < base_price for c in last_3_closes):
                        triggered = True
                        reason = "3 consecutive closes below the prior high"
                        clear_reversal = True
            except Exception as e:
                print(f"Failed to check {ticker}'s historical data: {e}")

        if not triggered:
            continue

        print(f"{ticker} triggered a stop-loss notification: {reason}")
        line_bot.send_stop_loss_alert(ticker, curr_price, levels["display_stop"], reason)
        latch_repo.fire(ticker, "stop_loss", entry_count)
        if clear_reversal:
            # Only consumed once the alert has actually been sent.
            assets_repo.update_fields(ticker, monitor_reversal=0)
