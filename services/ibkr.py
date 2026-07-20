"""IBKR connection + per-tick snapshot helpers for Investment Bot v3.

Replaces the connect/disconnect-tracking block that used to live at the top
of main.py's main() plus the ad-hoc data/ib_connection_state.json file. All
state now lives in SQLite via db.repo.state_repo, so the dashboard's
`/system` page can read it without ever touching IB Gateway itself (see
PLAN_V3.md §2 "the dashboard never talks to IB Gateway").
"""

import random
import time
from datetime import datetime

from ib_insync import IB

from config.config_loader import IBKR_HOST, IBKR_PORT, est
from db.repo import state_repo, positions_repo, config_repo

MAX_CONNECT_ATTEMPTS = 5
RETRY_DELAY_SECONDS = 10
CONNECT_TIMEOUT_SECONDS = 10


def _fixed_client_id():
    """The predictable clientId to try first: config_repo (DB) first, then
    the IBKR_CLIENT_ID env var, default 11 -- per PLAN_V3.md §1."""
    return config_repo.get_int("IBKR_CLIENT_ID", default=11)


def connect_ib():
    """Connect to IB Gateway and return a connected `IB()` instance, or None
    if every attempt failed.

    ClientId strategy (a deliberate superset of PLAN_V3.md §1's "random on
    error 326" note): attempt 0 uses the fixed/predictable clientId (so a
    healthy Gateway always sees the same, recognisable session id in its
    logs -- easy to debug); EVERY retry after that uses a fresh random id
    in [1000, 9999], regardless of what kind of error the first attempt
    hit. Reliably detecting "error 326: client id already in use"
    specifically would mean subscribing to ib_insync's async error events
    and correlating them with the failed connect call -- fragile plumbing
    for zero practical gain, because switching to a random id is a safe
    response to *any* connect failure (it can only ever avoid a clientId
    collision, never cause one). So the 326 case is covered without ever
    being detected.

    Mechanics: up to MAX_CONNECT_ATTEMPTS tries, a fresh `IB()` object per
    attempt (a failed connect can leave the old one in a half-open state),
    10s socket timeout, RETRY_DELAY_SECONDS between tries.
    """
    fixed_id = _fixed_client_id()
    last_error = None

    for attempt in range(MAX_CONNECT_ATTEMPTS):
        client_id = fixed_id if attempt == 0 else random.randint(1000, 9999)
        ib = IB()
        try:
            print(f"IB connect attempt {attempt + 1}/{MAX_CONNECT_ATTEMPTS} (clientId={client_id})...")
            ib.connect(IBKR_HOST, IBKR_PORT, clientId=client_id, timeout=CONNECT_TIMEOUT_SECONDS)
            print("IBKR connected.")
            return ib
        except Exception as e:
            last_error = e
            print(f"IB connect attempt failed (clientId={client_id}): {e}")
            try:
                if ib.isConnected():
                    ib.disconnect()
            except Exception:
                pass
            if attempt < MAX_CONNECT_ATTEMPTS - 1:
                time.sleep(RETRY_DELAY_SECONDS)

    print(f"IBKR connection failed after {MAX_CONNECT_ATTEMPTS} attempts: {last_error}")
    return None


# ---------------------------------------------------------------------------
# Portfolio + account summary snapshot
# ---------------------------------------------------------------------------


def snapshot_portfolio(ib):
    """Persist ib.portfolio() and account summary into SQLite for the
    dashboard to read. Must only be called with a live, connected `ib`.

    Positions reconciliation (PLAN_V3.md §2 HARD RULE): this function is the
    ONLY place positions_repo.reconcile_ibkr() is called, and it is only
    reached when ib.portfolio() itself did not raise -- i.e. only on a
    successful snapshot. A failed/partial fetch must never reach the
    reconcile call, or a single disconnect would wipe the whole holdings
    table (that's why present_tickers is built directly from the portfolio
    items in the same call, not from some earlier/cached list).

    Extra guard on top of that rule: an EMPTY portfolio list also skips
    reconciliation entirely. ib_insync can transiently return [] right
    after connecting, before the Gateway has streamed the portfolio state
    down -- that's a non-raising call that nonetheless doesn't represent
    reality, and reconciling against it would delete every IBKR row. The
    cost of this guard is that a genuinely-everything-sold account keeps
    stale rows until a manual cleanup -- for this bot (always holding at
    least SGOV) that state is effectively unreachable, so the trade-off is
    safe.
    """
    items = ib.portfolio()

    if not items:
        print("ib.portfolio() returned an empty list -- skipping the positions snapshot and reconcile (possibly a transient empty read right after connecting).")
        _snapshot_account_summary(ib)
        return

    present_tickers = set()
    for item in items:
        ticker = item.contract.symbol
        qty = float(item.position)
        if qty != 0:
            # Only tickers with a genuinely open position count as "present"
            # -- IBKR can report a just-closed position as qty=0 within the
            # same session, and those must NOT survive reconciliation.
            present_tickers.add(ticker)
        positions_repo.upsert(
            {
                "ticker": ticker,
                "qty": qty,
                "avg_cost": float(item.averageCost),
                "market_price": float(item.marketPrice) if item.marketPrice is not None else None,
                "market_value": float(item.marketValue) if item.marketValue is not None else None,
                "unrealized_pnl": float(item.unrealizedPNL) if item.unrealizedPNL is not None else None,
                "broker": "IBKR",
            }
        )

    positions_repo.reconcile_ibkr(present_tickers)

    _snapshot_account_summary(ib)


def _snapshot_account_summary(ib):
    summary = ib.accountSummary()
    tag_map = {
        "NetLiquidation": "account_net_liq",
        "TotalCashValue": "account_total_cash",
        "AvailableFunds": "account_available_funds",
    }
    for item in summary:
        if item.currency != "USD":
            continue
        state_key = tag_map.get(item.tag)
        if state_key:
            try:
                state_repo.set(state_key, float(item.value))
            except (TypeError, ValueError):
                continue
    state_repo.set("account_updated_at", datetime.now(est).isoformat())


# ---------------------------------------------------------------------------
# Disconnect timer / alert tracking (replaces data/ib_connection_state.json)
# ---------------------------------------------------------------------------

DISCONNECT_ALERT_MINUTES = 10


def track_disconnect(connected: bool, line_bot):
    """Port of main.py's disconnect-timer logic, now backed by app_state
    instead of data/ib_connection_state.json.

    - On a successful connection: clear ib_disconnect_since/alerted so the
      next failure starts a fresh timer.
    - On a failed connection: track how long we've been down
      (ib_disconnect_since) and, once that exceeds DISCONNECT_ALERT_MINUTES
      AND we haven't already alerted for this outage (ib_disconnect_alerted
      latch), send one LINE alert and set the latch so we don't spam every
      tick afterwards.
    - Suppressed 23:40-23:59 ET: this is the Gateway's own nightly
      AUTO_RESTART_TIME window (see docker-compose.yml / PLAN_V3.md §7) --
      a brief expected disconnect, not a real outage worth paging over.
    """
    if connected:
        state_repo.set("ib_disconnect_since", None)
        state_repo.set("ib_disconnect_alerted", False)
        return

    now_et = datetime.now(est)
    if now_et.hour == 23 and now_et.minute >= 40:
        print("Within IB Gateway's nightly auto-restart window -- suppressing disconnect alert.")
        return

    current_time = time.time()
    start_fail_time = state_repo.get("ib_disconnect_since", None)
    if start_fail_time is None:
        start_fail_time = current_time
        state_repo.set("ib_disconnect_since", start_fail_time)
        state_repo.set("ib_disconnect_alerted", False)

    already_alerted = state_repo.get("ib_disconnect_alerted", False)
    duration_minutes = (current_time - start_fail_time) / 60
    print(f"IB Gateway has been disconnected for {duration_minutes:.1f} minutes...")

    if duration_minutes > DISCONNECT_ALERT_MINUTES and not already_alerted:
        line_bot.send_error_report(
            f"IB Gateway has been disconnected for over {DISCONNECT_ALERT_MINUTES} minutes.\n"
            f"Please check the VPS / Gateway status.",
            "IB connection outage",
        )
        state_repo.set("ib_disconnect_alerted", True)
