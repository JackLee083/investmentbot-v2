"""Sync IBKR/Kraken fills into SQLite (transactions table) and update
satellite strategy state on a Buy fill.

Phase 2 changes from the v2 version of this file:
- Fills are written via db.repo.tx_repo.insert_ignore(), which dedups on
  tx_id (execId for IBKR, order id for Kraken) using SQLite's
  INSERT OR IGNORE, replacing the old check-then-insert round trip against
  Notion (check_tx_exists_in_notion / write_to_notion).
- A satellite Buy fill now increments assets.entry_count and sets
  last_buy_date directly. No explicit latch re-arm call is needed here: the
  dip/stop_loss latches are keyed by (ticker, kind, level) where level is
  next_level (dip) or entry_count (stop_loss) -- bumping entry_count changes
  which level key gets checked next tick, and that new key has never fired,
  so it's armed by definition. See db/schema.sql's alert_latches comment.
- A Sell fill's avg_cost_snapshot now comes from positions_repo (the IBKR
  snapshot this tick keeps), not a Notion formula column.
- DB write failures (tx_repo.insert_ignore raising) are NOT swallowed here
  -- they propagate up so jobs/tick.py's per-stage try/except can catch them
  and push a LINE error report. v2's bare `except:` blocks silently
  swallowed everything, including real DB failures -- see PLAN_V3.md §1 bug
  list.
- All Notion imports/writes removed.
"""

from datetime import datetime

import ccxt
import pytz
from ib_insync import IB

from config.config_loader import KRAKEN_KEY, KRAKEN_SECRET
from db.repo import tx_repo, assets_repo, positions_repo
from services.line_notify import line_bot

_EASTERN = pytz.timezone("US/Eastern")


def _format_eastern_iso(time_input):
    """Normalize a Kraken ('...Z' or ISO string) or IBKR (datetime, usually
    UTC) fill timestamp to an ISO 8601 string in US/Eastern -- the
    convention transactions.executed_at uses throughout the schema."""
    try:
        if isinstance(time_input, str):
            clean_time = time_input.replace("Z", "+00:00")
            dt_obj = datetime.fromisoformat(clean_time)
            if dt_obj.tzinfo is None:
                dt_obj = dt_obj.replace(tzinfo=pytz.utc)
            return dt_obj.astimezone(_EASTERN).isoformat()
        elif isinstance(time_input, datetime):
            if time_input.tzinfo is None:
                et_dt = pytz.utc.localize(time_input).astimezone(_EASTERN)
            else:
                et_dt = time_input.astimezone(_EASTERN)
            return et_dt.isoformat()
    except Exception as e:
        print(f"Timestamp conversion failed: {time_input} | Error: {e}")
        return str(time_input)
    return str(time_input)


def _lookup_avg_cost(ticker):
    """Current avg_cost for `ticker` from this tick's positions snapshot, or
    None if we don't hold (or haven't yet snapshotted) a position in it."""
    for p in positions_repo.list_all():
        if p["ticker"] == ticker:
            return p["avg_cost"]
    return None


def _handle_satellite_buy_fill(ticker):
    """On a Satellite ticker's Buy fill: advance entry_count and record
    last_buy_date. See module docstring for why no explicit latch re-arm is
    needed here."""
    asset = assets_repo.get(ticker)
    if not asset or asset.get("asset_type") != "Satellite":
        return

    new_count = (asset.get("entry_count") or 0) + 1
    today_str = datetime.now().strftime("%Y-%m-%d")
    print(f"{ticker} (Satellite) add-on fill, Entry Count: {asset.get('entry_count') or 0} -> {new_count}")
    assets_repo.update_fields(ticker, entry_count=new_count, last_buy_date=today_str)


# ---------------------------------------------------------------------------
# Kraken
# ---------------------------------------------------------------------------


def sync_kraken_trades():
    """Pull recent closed Kraken orders and record any not already in
    `transactions`."""
    exchange = ccxt.kraken(
        {
            "apiKey": KRAKEN_KEY,
            "secret": KRAKEN_SECRET,
            "enableRateLimit": True,
        }
    )

    try:
        print("Fetching order history from Kraken...")
        orders = exchange.fetch_closed_orders(limit=3)
    except Exception as e:
        print(f"Failed to fetch Kraken order history: {e}")
        return

    if not orders:
        print("No recently settled orders found.")
        return

    for order in orders:
        if order["status"] != "closed" or order["filled"] <= 0:
            continue

        tx_id = str(order["id"])
        symbol = order["symbol"].replace("/", "")

        raw_info = order.get("info", {})
        user_ref = raw_info.get("userref", 0)
        source_name = "Auto_Bot" if str(user_ref) == "999" else "Manual"
        side = order["side"].capitalize()

        avg_price = order["average"] if order.get("average") else order["price"]
        qty = order["filled"]
        fee_cost = order["fee"]["cost"] if order.get("fee") else 0.0
        total_cost = order["cost"]
        executed_at = _format_eastern_iso(order["datetime"])

        # The positions row for Kraken holdings is keyed by the BASE asset
        # ('BTC' -- see positions_repo.upsert_kraken_btc), while the tx row
        # keeps the flattened pair ('BTCUSD'). Look up avg cost by the base
        # asset so a Sell here wouldn't silently snapshot None. NOTE: BTC is
        # currently buy-only DCA, so this path is dormant -- it exists so a
        # future manual sell degrades gracefully instead of silently.
        base_asset = order["symbol"].split("/")[0]
        avg_cost_snapshot = _lookup_avg_cost(base_asset) if side == "Sell" else None

        tx = {
            "tx_id": tx_id,
            "ticker": symbol,
            "side": side,
            "qty": qty,
            "price": avg_price,
            "fee": fee_cost,
            "total": total_cost,
            "broker": "Kraken",
            "source": source_name,
            "avg_cost_snapshot": avg_cost_snapshot,
            "executed_at": executed_at,
        }

        # DB write intentionally NOT wrapped in try/except here -- a failure
        # must propagate to the tick stage handler (see module docstring).
        inserted = tx_repo.insert_ignore(tx)
        if not inserted:
            continue  # already recorded, dedup on tx_id

        line_bot.send_trade_report(symbol, side, qty, avg_price, total_cost)
        if side == "Buy":
            _handle_satellite_buy_fill(symbol)


# ---------------------------------------------------------------------------
# IBKR
# ---------------------------------------------------------------------------


def sync_ibkr_trades(ib):
    """Pull today's IBKR fills and record any not already in
    `transactions`."""
    try:
        print("Fetching fill history from IBKR...")
        fills = ib.fills()
    except Exception as e:
        print(f"Failed to fetch IBKR fill history: {e}")
        return

    side_map = {"BOT": "Buy", "SLD": "Sell", "BUY": "Buy", "SELL": "Sell"}

    for fill in fills:
        execution = fill.execution
        commission_report = fill.commissionReport
        tx_id = execution.execId
        symbol = fill.contract.symbol

        order_ref = execution.orderRef
        source_name = "Auto_Bot" if order_ref == "999" else "Manual"
        side = side_map.get(execution.side.upper(), execution.side)

        avg_price = execution.avgPrice
        qty = execution.cumQty
        fee = commission_report.commission if commission_report else 0.0
        total = avg_price * qty
        executed_at = _format_eastern_iso(execution.time)

        avg_cost_snapshot = _lookup_avg_cost(symbol) if side == "Sell" else None

        tx = {
            "tx_id": tx_id,
            "ticker": symbol,
            "side": side,
            "qty": qty,
            "price": avg_price,
            "fee": fee,
            "total": total,
            "broker": "IBKR",
            "source": source_name,
            "avg_cost_snapshot": avg_cost_snapshot,
            "executed_at": executed_at,
        }

        # DB write intentionally NOT wrapped in try/except here -- a failure
        # must propagate to the tick stage handler (see module docstring).
        inserted = tx_repo.insert_ignore(tx)
        if not inserted:
            continue  # already recorded, dedup on tx_id

        line_bot.send_trade_report(symbol, side, qty, avg_price, total)
        if side == "Buy":
            _handle_satellite_buy_fill(symbol)
