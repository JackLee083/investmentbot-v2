"""The bot's main tick pipeline -- replaces main.main() from v2.

Run manually with `python -m jobs.tick`; scheduler_job.py wires this into
APScheduler's cron grid via a safe wrapper.

Stage order (unchanged from v2's main.py, see PLAN_V3.md §5):
    connect -> load active assets -> update prices -> market indicators ->
    HV/Tier -> price alerts -> dip check -> stop-loss check ->
    DCA window logic -> execute Kraken/IBKR DCA + SGOV -> send manual
    report -> sync fills -> snapshot portfolio -> Kraken BTC position row ->
    check_and_lock_entry_atr.

Two structural rules that make this safe to run on a cron grid with a
possibly-multi-worker gunicorn (PLAN_V3.md §8 risks 2 and 3), enforced here
rather than by the caller:

1. A fresh asyncio event loop is created and torn down on every call --
   ib_insync needs one, and reusing a loop across threads/ticks is unsafe.
2. A `tick_lock` lease (app_state, ISO timestamp) guards against two ticks
   running concurrently (e.g. an accidental `-w 2`). It is acquired before
   any work starts and ALWAYS released in a `finally` block, so a crashing
   tick can never wedge every future tick. A lock older than
   TICK_LOCK_STALE_MINUTES is treated as abandoned (from a crashed process)
   and taken over rather than honored forever.

`last_tick_finished` / `last_tick_ok` / `last_tick_error` are written in a
`finally` block on every *executed* tick (success or failure) -- this is
what `/healthz` (Phase 3) uses to tell "the scheduler is alive but every
tick fails to reach IBKR" (still 200, ok=false) apart from "the scheduler
thread died" (503, timestamp stuck). A *skipped* tick (stale-lock check
says another run is still fresh) does NOT touch these keys -- it never
started, so there's nothing to report.
"""

import asyncio
import time
import traceback
from datetime import datetime

import ccxt
import pytz

from config.config_loader import est, KRAKEN_KEY, KRAKEN_SECRET
from db.repo import assets_repo, state_repo, config_repo, indicators_repo, tx_repo, positions_repo
from marketdata.fetchers import (
    process_asset_price,
    get_cnn_fng_index,
    get_crypto_fng_index,
    get_vix_value,
    get_news_sentiment_score,
    check_price_alerts,
)
from utils.hv_atr_calculator import get_strategy_metrics, determine_tier, check_and_lock_entry_atr
from utils.calendar_utils import is_dca_day, is_nyse_dca_window, check_dca_schedule
from trading.transaction_logger import sync_kraken_trades, sync_ibkr_trades
from trading.broker_utils import (
    execute_kraken_dca,
    execute_ibkr_dca,
    rebalance_cash_with_sgov,
    check_satellite_opportunities,
    check_stop_loss_notifications,
)
from services.ibkr import connect_ib, snapshot_portfolio, track_disconnect
from services.line_notify import line_bot

TICK_LOCK_STALE_MINUTES = 30
POST_DCA_SETTLE_SECONDS = 15


# ---------------------------------------------------------------------------
# tick_lock lease (app_state) -- see module docstring / PLAN_V3.md §8 risk 3
# ---------------------------------------------------------------------------


def _lock_age_minutes(lock_value):
    try:
        lock_time = datetime.fromisoformat(lock_value)
        return (datetime.now(est) - lock_time).total_seconds() / 60
    except Exception:
        return None


def _acquire_tick_lock():
    """Returns True if the lock was acquired (free, or stale and taken
    over), False if a fresh lock is held elsewhere (caller should skip)."""
    existing = state_repo.get("tick_lock", None)
    if existing:
        age = _lock_age_minutes(existing)
        if age is not None and age < TICK_LOCK_STALE_MINUTES:
            print(f"tick_lock is still fresh ({age:.1f} min), skipping this run.")
            return False
        if age is not None:
            print(f"tick_lock expired ({age:.1f} min), treating as leftover from a crash, taking over.")
        else:
            print("tick_lock has an invalid format, taking over.")
    state_repo.set("tick_lock", datetime.now(est).isoformat())
    return True


def _release_tick_lock():
    state_repo.set("tick_lock", None)


# ---------------------------------------------------------------------------
# Per-stage error containment
# ---------------------------------------------------------------------------


def _run_stage(name, fn, *args, **kwargs):
    """Run one pipeline stage. Any exception is logged (with the stage
    name), reported to LINE admins, and swallowed so the rest of the
    pipeline keeps running -- per PLAN_V3.md §1 bug list ("bare except
    blocks and silent failure... replace with per-stage try/except that
    logs stage name and pushes an error to LINE"). Returns the stage's
    return value, or None if it raised."""
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        print(f"[tick:{name}] error occurred: {e}")
        traceback.print_exc()
        try:
            line_bot.send_error_report(traceback.format_exc(), f"Tick stage failed: {name}")
        except Exception as line_e:
            print(f"Failed to send error report: {line_e}")
        return None


# ---------------------------------------------------------------------------
# Stage implementations
# ---------------------------------------------------------------------------


def _update_asset_prices(assets):
    """A. Update every active asset's current price; satellite tickers also
    ratchet base_price up on a new high and arm monitor_reversal if held.
    Ported from main.py:19-77. Returns {ticker: current_price}."""
    print("--- Updating asset current prices ---")
    price_cache = {}
    now_iso = datetime.now(est).isoformat()

    for asset in assets:
        ticker = asset["ticker"]
        price_source = asset.get("price_source") or "yahoo"
        price, source = process_asset_price(ticker, price_source)

        if price_source == "skip" or not price:
            print(f"{ticker}: skipping update")
            continue

        current_price = float(price)
        price_cache[ticker] = current_price

        updates = {"current_price": round(current_price, 2), "price_updated_at": now_iso}

        if asset.get("asset_type") == "Satellite":
            old_base_price = asset.get("base_price") or 0.0
            entry_count = asset.get("entry_count") or 0
            if current_price > old_base_price:
                print(f"{ticker} (Satellite) hit a new high! Refreshing Base Price: {old_base_price} -> {round(current_price, 2)}")
                updates["base_price"] = round(current_price, 2)
                if entry_count >= 1:
                    print(f"  -> Currently holding ({entry_count} entries), enabling the 'monitor reversal' flag")
                    updates["monitor_reversal"] = 1
                else:
                    print("  -> Currently flat, only updating the high, not enabling monitoring.")

        assets_repo.update_fields(ticker, **updates)
        print(f"{ticker}: ${round(current_price, 2)} (source: {source})")

    return price_cache


def _hours_since_sqlite_utc(sqlite_utc_str):
    """`sqlite_utc_str` is a datetime('now') string (UTC, 'YYYY-MM-DD
    HH:MM:SS'); returns hours elapsed since then, or None if unparseable."""
    try:
        naive = datetime.strptime(sqlite_utc_str, "%Y-%m-%d %H:%M:%S")
        then_utc = pytz.utc.localize(naive)
        now_utc = datetime.now(pytz.utc)
        return (now_utc - then_utc).total_seconds() / 3600
    except Exception:
        return None


def _update_market_indicators():
    """B. Refresh CNN F&G / Crypto F&G / VIX (and, in the Alpha Vantage
    rate-limit window, news sentiment) into indicator_snapshots. Returns the
    F&G value to use for sizing this tick: the latest ok=1 CNN F&G reading,
    or neutral 50 (with a LINE warning) if the latest good reading is
    stale beyond INDICATOR_STALE_HOURS, or if there has never been one."""
    print("\n--- Updating market sentiment indicators ---")

    indicators = [
        ("cnn_fng", get_cnn_fng_index),
        ("crypto_fng", get_crypto_fng_index),
        ("vix", get_vix_value),
    ]

    now_et = datetime.now(est)
    minute = now_et.minute
    if (minute <= 5) or (25 <= minute <= 35):
        print("Running Alpha Vantage update...")
        indicators.append(("news_sentiment", get_news_sentiment_score))
    else:
        print("Not in the AV update window, skipping AV update")

    for name, func in indicators:
        try:
            value, ok = func()
        except Exception as e:
            print(f"{name} update failed: {e}")
            value, ok = None, False
        # indicator_snapshots.value is NOT NULL -- a failed fetch still
        # writes a row (so staleness can be tracked) but ok=0 marks it as
        # not a real reading; see db/schema.sql's comment on this table.
        indicators_repo.insert(name, value if value is not None else 0.0, ok=ok)
        print(f"{name}: {'updated ' + str(value) if ok else 'update failed'}")

    stale_hours = config_repo.get_float("INDICATOR_STALE_HOURS", default=24)
    last_good = indicators_repo.last_good("cnn_fng")
    if last_good is None:
        print("CNN F&G has never been fetched successfully, using neutral value 50.")
        return 50

    value, fetched_at = last_good
    age_hours = _hours_since_sqlite_utc(fetched_at)
    if age_hours is not None and age_hours > stale_hours:
        print(f"CNN F&G is stale ({age_hours:.1f}h > {stale_hours}h), using neutral value 50 instead.")
        try:
            line_bot.send_error_report(
                f"CNN F&G has not updated successfully for {age_hours:.1f} hours; this DCA cycle uses neutral value 50.",
                "Indicator staleness warning",
            )
        except Exception:
            pass
        return 50

    return value


def _update_satellite_tiers(assets):
    """C. Recompute HV180/Tier for every active Satellite asset.

    hv180 is stored as the raw PERCENT value (e.g. 25.34 for 25.34%), not
    divided by 100 -- v2 stored it divided by 100 purely so Notion's
    "percent" number format would re-multiply it back for display; SQLite
    has no such formatting layer, and determine_tier()'s own thresholds
    (30/40) are written in percent terms, so storing the raw percent keeps
    the number self-consistent with the tier boundaries."""
    print("\n--- Detecting and computing Satellite strategy metrics ---")
    for asset in assets:
        if asset.get("asset_type") != "Satellite":
            continue
        ticker = asset["ticker"]
        metrics = get_strategy_metrics(ticker)
        if not metrics:
            print(f"{ticker}: could not fetch strategy metrics, skipping.")
            continue
        tier_info = determine_tier(metrics["HV180"])
        assets_repo.update_fields(ticker, hv180=metrics["HV180"], tier=tier_info["Tier"])
        print(f"{ticker}: strategy metrics synced (HV180: {metrics['HV180']}, Tier: {tier_info['Tier']})")


def _check_price_alerts_stage():
    """Fetch a quote per Active price_alerts ticker (independent of the
    watchlist) and push a LINE alert for any that fired."""
    fired = check_price_alerts()
    for alert in fired:
        line_bot.send_price_alert(alert["ticker"], alert["target"], alert["direction"])


def _handle_dca_check():
    dca_type = is_dca_day()
    if not dca_type:
        print("Today is not a DCA trading day; only updating data, no orders will be placed.")
    return dca_type


def _snapshot_kraken_btc_position():
    """Fetch the Kraken BTC balance and combine it with
    tx_repo.sum_kraken_btc_cost() to write the synthetic BTC positions row.
    See PLAN_V3.md §2: this is the ONLY place besides IBKR's own portfolio
    that positions gets a row from, since positions is otherwise IBKR-only.
    positions_repo.upsert_kraken_btc() itself guards the qty<=0/avg_cost
    None cases (deletes any stale row instead of writing) -- no need to
    pre-check that here."""
    exchange = ccxt.kraken(
        {"apiKey": KRAKEN_KEY, "secret": KRAKEN_SECRET, "enableRateLimit": True}
    )
    balance = exchange.fetch_balance()
    qty = (balance.get("total") or {}).get("BTC", 0.0) or 0.0

    price = None
    try:
        ticker = exchange.fetch_ticker("BTC/USD")
        price = ticker.get("last")
    except Exception as e:
        print(f"Failed to fetch current BTC price: {e}")

    cost, sum_qty = tx_repo.sum_kraken_btc_cost()
    avg_cost = (cost / sum_qty) if sum_qty and sum_qty > 0 else None

    positions_repo.upsert_kraken_btc(qty=qty, avg_cost=avg_cost, price=price)


def _close_loop(loop):
    try:
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        if loop.is_running():
            loop.stop()
        loop.close()
        print("--- Asyncio loop closed ---")
    except Exception as ex:
        print(f"Error while closing the loop: {ex}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run_tick():
    # Fresh event loop every run -- ib_insync needs one, and APScheduler
    # runs jobs on a pool thread that may be reused across ticks, so we
    # can't rely on a loop surviving (or being safe to reuse) between calls.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    if not _acquire_tick_lock():
        _close_loop(loop)
        return

    start_time = datetime.now(est)
    state_repo.set("last_tick_started", start_time.isoformat())
    print(f"Portfolio automation system started - {start_time.strftime('%Y-%m-%d %H:%M:%S')}")

    ib = None
    tick_ok = True
    tick_error = None

    try:
        try:
            ib = connect_ib()
        except Exception as e:
            ib = None
            tick_error = f"connect_ib() raised: {e}"
            print(tick_error)

        connected = bool(ib is not None and ib.isConnected())
        track_disconnect(connected, line_bot)

        if not connected:
            # v2 semantics preserved: an IBKR outage degrades the tick, it
            # does NOT abort it. Everything that doesn't need the IB socket
            # (prices, indicators, dip/stop checks, Kraken DCA + fills, the
            # Kraken BTC positions row, ATR backfill) still runs below;
            # only the IBKR-dependent stages are gated on `connected`. The
            # tick is still recorded as failed (last_tick_ok=False) so
            # /system and LINE can surface the outage -- but it finishes,
            # not raises (see module docstring).
            tick_ok = False
            tick_error = tick_error or "IBKR connection failed"
            print(f"IBKR connection failed, continuing this tick in 'partial execution' mode (skipping IBKR-dependent stages): {tick_error}")

        assets = _run_stage("load_assets", assets_repo.list_active) or []

        price_cache = _run_stage("update_prices", _update_asset_prices, assets) or {}

        fng_val = _run_stage("market_indicators", _update_market_indicators)
        if fng_val is None:
            fng_val = 50

        _run_stage("hv_tier", _update_satellite_tiers, assets)

        _run_stage("price_alerts", _check_price_alerts_stage)

        _run_stage("dip_check", check_satellite_opportunities, price_cache)

        _run_stage("stop_loss_check", check_stop_loss_notifications, price_cache)

        dca_type = _run_stage("dca_window", _handle_dca_check)
        in_time_window = _run_stage("dca_window_check", is_nyse_dca_window)

        if dca_type:
            _run_stage("dca_schedule_notify", check_dca_schedule)

        if dca_type and in_time_window:
            # Kraken BTC DCA needs no IB connection -- always runs in window.
            print("Running Kraken BTC DCA")
            _run_stage("kraken_dca", execute_kraken_dca)

            if connected:
                print(f"Running IBKR logic check (mode: {dca_type})")
                manual_report = _run_stage("ibkr_dca", execute_ibkr_dca, ib, fng_val, dca_type)

                if manual_report:
                    # v2 built this list but never sent it -- see PLAN_V3.md §1.
                    _run_stage("send_manual_report", line_bot.send_manual_dca_instruction, manual_report)

                if dca_type == "First_Day":
                    print("Running SGOV cash rebalance")
                    _run_stage("sgov_rebalance", rebalance_cash_with_sgov, ib)
            else:
                print("IBKR not connected, skipping US-stock DCA.")

            time.sleep(POST_DCA_SETTLE_SECONDS)
        else:
            print("Not currently in the 11:20-11:45 ET window or not a trading day; no orders placed.")

        print("\n--- Syncing recent transaction history ---")
        _run_stage("sync_kraken_fills", sync_kraken_trades)

        if connected:
            _run_stage("sync_ibkr_fills", sync_ibkr_trades, ib)
            _run_stage("snapshot_portfolio", snapshot_portfolio, ib)
        else:
            # HARD RULE (PLAN_V3.md §2): snapshot/reconcile must never run
            # off a failed connection -- positions stays untouched so one
            # disconnect can't wipe the holdings table.
            print("IBKR not connected, skipping fill sync and positions snapshot.")

        _run_stage("kraken_btc_position", _snapshot_kraken_btc_position)

        _run_stage("lock_entry_atr", check_and_lock_entry_atr)

        print(f"\nFull system sync succeeded! Total elapsed: {datetime.now(est) - start_time}")

    except Exception as e:
        # Belt-and-braces: every known stage already goes through
        # _run_stage, but if something truly unexpected slips past all of
        # them, it must still land here rather than escape run_tick() and
        # leave the lock/timestamps unwritten.
        tick_ok = False
        tick_error = str(e)
        print(f"Unexpected error during tick: {e}")
        traceback.print_exc()
        try:
            line_bot.send_error_report(traceback.format_exc(), "Main process crash")
        except Exception:
            pass

    finally:
        try:
            if ib is not None and ib.isConnected():
                ib.disconnect()
                print("--- Disconnected from IB Gateway ---")
        except Exception as e:
            print(f"Error while disconnecting: {e}")

        # Written on EVERY executed tick, success or failure -- this is
        # what /healthz (Phase 3) relies on to distinguish "scheduler dead"
        # from "scheduler alive but IB unreachable". See module docstring.
        state_repo.set("last_tick_finished", datetime.now(est).isoformat())
        state_repo.set("last_tick_ok", tick_ok)
        state_repo.set("last_tick_error", tick_error)

        _release_tick_lock()
        _close_loop(loop)


if __name__ == "__main__":
    run_tick()
