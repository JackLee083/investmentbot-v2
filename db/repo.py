"""Plain-SQL repositories for Investment Bot v3.

Each "repo" below is a plain class used only as a namespace (all methods are
@staticmethod, so there's no instance state to worry about -- call them as
e.g. `assets_repo.get("QQQ")`). No ORM: every method just runs SQL through
db.database.get_conn(). Kept intentionally simple/explicit since this code is
also meant to be readable while still learning SQL.

Timestamps written by these repos use SQLite's own `datetime('now')` (UTC)
rather than Python's datetime.now(), so they always match the schema's
column DEFAULTs.
"""

import json
import os
import sqlite3

from db.database import get_conn

# ---------------------------------------------------------------------------
# shared helpers
# ---------------------------------------------------------------------------


def _dynamic_upsert(conn, table, pk_col, data: dict):
    """INSERT ... ON CONFLICT(pk_col) DO UPDATE, built from whatever keys are
    present in `data`. `pk_col` must be present in `data`. Only columns
    actually supplied are written, so unrelated columns (e.g. created_at)
    are left untouched on update."""
    if pk_col not in data:
        raise ValueError(f"data must include the primary key column '{pk_col}'")

    cols = list(data.keys())
    placeholders = ", ".join("?" for _ in cols)
    col_list = ", ".join(cols)
    update_cols = [c for c in cols if c != pk_col]
    if update_cols:
        update_clause = ", ".join(f"{c}=excluded.{c}" for c in update_cols)
        update_clause += ", updated_at=datetime('now')"
    else:
        update_clause = "updated_at=datetime('now')"

    sql = (
        f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) "
        f"ON CONFLICT({pk_col}) DO UPDATE SET {update_clause}"
    )
    conn.execute(sql, [data[c] for c in cols])


# ---------------------------------------------------------------------------
# assets_repo
# ---------------------------------------------------------------------------


class assets_repo:
    @staticmethod
    def get(ticker):
        with get_conn() as conn:
            row = conn.execute("SELECT * FROM assets WHERE ticker=?", (ticker,)).fetchone()
            return dict(row) if row else None

    @staticmethod
    def list_active(asset_type=None):
        with get_conn() as conn:
            if asset_type is None:
                rows = conn.execute(
                    "SELECT * FROM assets WHERE active=1 ORDER BY ticker"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM assets WHERE active=1 AND asset_type=? ORDER BY ticker",
                    (asset_type,),
                ).fetchall()
            return [dict(r) for r in rows]

    @staticmethod
    def upsert(row: dict):
        """Insert a new asset or update an existing one (matched on ticker).
        `row` must contain at least 'ticker' and 'asset_type'; any other
        assets columns present in `row` are written too."""
        with get_conn() as conn:
            _dynamic_upsert(conn, "assets", "ticker", row)

    @staticmethod
    def update_fields(ticker, **fields):
        """Update only the given columns for one asset, e.g.
        assets_repo.update_fields('QQQ', current_price=123.4, tier='T1')."""
        if not fields:
            return
        set_clause = ", ".join(f"{k}=?" for k in fields)
        sql = f"UPDATE assets SET {set_clause}, updated_at=datetime('now') WHERE ticker=?"
        with get_conn() as conn:
            conn.execute(sql, [*fields.values(), ticker])

    @staticmethod
    def soft_delete(ticker):
        with get_conn() as conn:
            conn.execute(
                "UPDATE assets SET active=0, updated_at=datetime('now') WHERE ticker=?",
                (ticker,),
            )


# ---------------------------------------------------------------------------
# tx_repo
# ---------------------------------------------------------------------------


class tx_repo:
    @staticmethod
    def insert_ignore(tx: dict) -> bool:
        """Insert a transaction row, deduped on tx_id. Returns True if a new
        row was inserted, False if tx_id already existed.

        Deliberately NOT implemented with SQLite's `INSERT OR IGNORE`: OR
        IGNORE swallows EVERY constraint violation -- CHECK failures, NOT
        NULL violations, all of it -- not just the tx_id dedup we actually
        want. That once made a corrupt row (NaN money value tripping a NOT
        NULL/CHECK) vanish silently instead of erroring: pool state mutated
        but no transaction was recorded, the worst kind of books-don't-
        balance bug. So: plain INSERT, catch IntegrityError, and treat ONLY
        the tx_id UNIQUE violation as "duplicate, skip" -- anything else is
        a real bug and must raise (the tick's per-stage handler / the web
        error path will surface it)."""
        total = tx.get("total")
        if total is None:
            total = tx["qty"] * tx["price"]
        try:
            with get_conn() as conn:
                conn.execute(
                    """
                    INSERT INTO transactions
                        (tx_id, ticker, side, qty, price, fee, total, broker, source,
                         avg_cost_snapshot, executed_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        tx["tx_id"],
                        tx["ticker"],
                        tx["side"],
                        tx["qty"],
                        tx["price"],
                        tx.get("fee", 0.0),
                        total,
                        tx["broker"],
                        tx["source"],
                        tx.get("avg_cost_snapshot"),
                        tx["executed_at"],
                    ),
                )
            return True
        except sqlite3.IntegrityError as e:
            if "UNIQUE constraint failed: transactions.tx_id" in str(e):
                return False  # duplicate fill -- the one benign case
            raise

    @staticmethod
    def list_recent(limit=50):
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM transactions ORDER BY executed_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    @staticmethod
    def sum_kraken_btc_cost():
        """Returns (sum_total_plus_fee, sum_qty) over broker='Kraken' AND
        side='Buy' transactions -- used to derive BTC average cost as
        (Sigma total + Sigma fee) / Sigma qty. See PLAN_V3.md §2: this is a
        buy-only-DCA cost basis and becomes wrong the day BTC is ever sold."""
        with get_conn() as conn:
            row = conn.execute(
                """
                SELECT COALESCE(SUM(total), 0) + COALESCE(SUM(fee), 0) AS cost,
                       COALESCE(SUM(qty), 0) AS qty
                FROM transactions
                WHERE broker='Kraken' AND side='Buy'
                """
            ).fetchone()
            return (row["cost"], row["qty"])


# ---------------------------------------------------------------------------
# state_repo -- app_state, JSON-encoded values
# ---------------------------------------------------------------------------


class state_repo:
    @staticmethod
    def get(key, default=None):
        with get_conn() as conn:
            row = conn.execute("SELECT value FROM app_state WHERE key=?", (key,)).fetchone()
            if row is None:
                return default
            return json.loads(row["value"])

    @staticmethod
    def set(key, value):
        encoded = json.dumps(value)
        with get_conn() as conn:
            conn.execute(
                """
                INSERT INTO app_state (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=datetime('now')
                """,
                (key, encoded),
            )


# ---------------------------------------------------------------------------
# config_repo -- DB first, env fallback, string-valued
# ---------------------------------------------------------------------------


class config_repo:
    @staticmethod
    def get(key, default=None):
        with get_conn() as conn:
            row = conn.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
            if row is not None:
                return row["value"]
        return os.environ.get(key, default)

    @staticmethod
    def set(key, value):
        with get_conn() as conn:
            conn.execute(
                """
                INSERT INTO config (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=datetime('now')
                """,
                (key, str(value)),
            )

    @staticmethod
    def get_float(key, default=None):
        val = config_repo.get(key, None)
        if val is None:
            return default
        try:
            return float(val)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def get_int(key, default=None):
        val = config_repo.get(key, None)
        if val is None:
            return default
        try:
            return int(float(val))
        except (TypeError, ValueError):
            return default


# ---------------------------------------------------------------------------
# alerts_repo -- price_alerts CRUD
# ---------------------------------------------------------------------------


class alerts_repo:
    @staticmethod
    def add(ticker, target_price, direction, status="Active"):
        """Store a price alert exactly as given. Deriving `direction` from
        the current price is a web-layer concern (Phase 3) -- this repo just
        persists whatever it's handed."""
        with get_conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO price_alerts (ticker, target_price, direction, status)
                VALUES (?, ?, ?, ?)
                """,
                (ticker, target_price, direction, status),
            )
            return cur.lastrowid

    @staticmethod
    def get(alert_id):
        with get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM price_alerts WHERE id=?", (alert_id,)
            ).fetchone()
            return dict(row) if row else None

    @staticmethod
    def list(status=None):
        with get_conn() as conn:
            if status is None:
                rows = conn.execute(
                    "SELECT * FROM price_alerts ORDER BY created_at DESC, id DESC"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM price_alerts WHERE status=? ORDER BY created_at DESC, id DESC",
                    (status,),
                ).fetchall()
            return [dict(r) for r in rows]

    @staticmethod
    def update_status(alert_id, status):
        with get_conn() as conn:
            if status == "Triggered":
                conn.execute(
                    "UPDATE price_alerts SET status=?, triggered_at=datetime('now') WHERE id=?",
                    (status, alert_id),
                )
            else:
                conn.execute(
                    "UPDATE price_alerts SET status=? WHERE id=?", (status, alert_id)
                )

    @staticmethod
    def delete(alert_id):
        with get_conn() as conn:
            conn.execute("DELETE FROM price_alerts WHERE id=?", (alert_id,))


# ---------------------------------------------------------------------------
# latch_repo -- alert_latches (replaces Notion's Sat Notified / ATR Notified)
# ---------------------------------------------------------------------------


class latch_repo:
    @staticmethod
    def is_armed(ticker, kind, level) -> bool:
        """A (ticker, kind, level) latch is armed when there's no row for it
        yet, OR its row has been rearmed (rearmed_at IS NOT NULL)."""
        with get_conn() as conn:
            row = conn.execute(
                "SELECT rearmed_at FROM alert_latches WHERE ticker=? AND kind=? AND level=?",
                (ticker, kind, level),
            ).fetchone()
            if row is None:
                return True
            return row["rearmed_at"] is not None

    @staticmethod
    def fire(ticker, kind, level):
        with get_conn() as conn:
            conn.execute(
                """
                INSERT INTO alert_latches (ticker, kind, level, fired_at, rearmed_at)
                VALUES (?, ?, ?, datetime('now'), NULL)
                ON CONFLICT(ticker, kind, level)
                DO UPDATE SET fired_at=datetime('now'), rearmed_at=NULL
                """,
                (ticker, kind, level),
            )

    @staticmethod
    def rearm(ticker, kind, level):
        """No-op if the latch has never fired -- it's already armed by
        definition (no row)."""
        with get_conn() as conn:
            conn.execute(
                """
                UPDATE alert_latches SET rearmed_at=datetime('now')
                WHERE ticker=? AND kind=? AND level=?
                """,
                (ticker, kind, level),
            )

    @staticmethod
    def rearm_all_for(ticker):
        with get_conn() as conn:
            conn.execute(
                """
                UPDATE alert_latches SET rearmed_at=datetime('now')
                WHERE ticker=? AND rearmed_at IS NULL
                """,
                (ticker,),
            )

    @staticmethod
    def list_all():
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM alert_latches ORDER BY ticker, kind, level"
            ).fetchall()
            return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# indicators_repo -- indicator_snapshots
# ---------------------------------------------------------------------------


class indicators_repo:
    @staticmethod
    def insert(name, value, ok=True):
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO indicator_snapshots (name, value, ok) VALUES (?, ?, ?)",
                (name, value, 1 if ok else 0),
            )

    @staticmethod
    def last_good(name):
        """Most recent ok=1 snapshot for `name`. Returns (value, fetched_at)
        or None if there has never been a successful reading."""
        with get_conn() as conn:
            row = conn.execute(
                """
                SELECT value, fetched_at FROM indicator_snapshots
                WHERE name=? AND ok=1
                ORDER BY fetched_at DESC, id DESC LIMIT 1
                """,
                (name,),
            ).fetchone()
            if row is None:
                return None
            return (row["value"], row["fetched_at"])

    @staticmethod
    def latest(name):
        """Most recent snapshot for `name` regardless of ok, or None."""
        with get_conn() as conn:
            row = conn.execute(
                """
                SELECT * FROM indicator_snapshots
                WHERE name=?
                ORDER BY fetched_at DESC, id DESC LIMIT 1
                """,
                (name,),
            ).fetchone()
            return dict(row) if row else None


# ---------------------------------------------------------------------------
# positions_repo -- positions (IBKR + Kraken BTC snapshot per tick)
# ---------------------------------------------------------------------------


class positions_repo:
    @staticmethod
    def upsert(row: dict):
        """`row` must include 'ticker'; other positions columns present in
        `row` (qty, avg_cost, market_price, market_value, unrealized_pnl,
        broker) are written too."""
        with get_conn() as conn:
            _dynamic_upsert(conn, "positions", "ticker", row)

    @staticmethod
    def reconcile_ibkr(present_tickers: set):
        """Delete every broker='IBKR' positions row whose ticker is NOT in
        `present_tickers`.

        WARNING: call this ONLY after a SUCCESSFUL ib.portfolio() snapshot
        this tick. If IB was unreachable and this is called with an empty/
        partial set anyway, it will wipe out the entire IBKR holdings table
        on a single disconnect. See PLAN_V3.md §2 "HARD RULE".
        """
        present_tickers = list(present_tickers)
        with get_conn() as conn:
            if not present_tickers:
                conn.execute("DELETE FROM positions WHERE broker='IBKR'")
            else:
                placeholders = ", ".join("?" for _ in present_tickers)
                conn.execute(
                    f"DELETE FROM positions WHERE broker='IBKR' AND ticker NOT IN ({placeholders})",
                    present_tickers,
                )

    @staticmethod
    def upsert_kraken_btc(qty, avg_cost, price):
        """Writes/updates the synthetic BTC row (broker='Kraken') since
        positions is otherwise IBKR-only. See PLAN_V3.md §2 for the
        (Sigma total + Sigma fee) / Sigma qty avg-cost formula this is fed
        from -- buy-only DCA assumption, must be replaced if BTC is sold.

        Contract: if qty is None/<= 0 or avg_cost is None (e.g. the Kraken
        balance reads ~0, or sum_kraken_btc_cost() returned Sigma qty = 0 so
        no avg cost could be derived), this does NOT insert/update -- the
        positions.avg_cost column is NOT NULL and a zero-qty row is exactly
        what reconciliation is supposed to remove. Instead any existing
        Kraken BTC row is deleted (same semantics as reconcile_ibkr for a
        position that no longer exists) and the function returns early.
        """
        if qty is None or qty <= 0 or avg_cost is None:
            with get_conn() as conn:
                conn.execute(
                    "DELETE FROM positions WHERE ticker='BTC' AND broker='Kraken'"
                )
            return
        market_value = qty * price if price is not None else None
        unrealized_pnl = (
            qty * (price - avg_cost) if price is not None else None
        )
        with get_conn() as conn:
            _dynamic_upsert(
                conn,
                "positions",
                "ticker",
                {
                    "ticker": "BTC",
                    "qty": qty,
                    "avg_cost": avg_cost,
                    "market_price": price,
                    "market_value": market_value,
                    "unrealized_pnl": unrealized_pnl,
                    "broker": "Kraken",
                },
            )

    @staticmethod
    def delete(ticker):
        with get_conn() as conn:
            conn.execute("DELETE FROM positions WHERE ticker=?", (ticker,))

    @staticmethod
    def list_all():
        with get_conn() as conn:
            rows = conn.execute("SELECT * FROM positions ORDER BY ticker").fetchall()
            return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# notification_repo -- notification_log (written by services/line_notify.py
# on every send attempt -- incl. empty-recipient-list "silent drop" and SDK
# failures, per PLAN_V3.md §1/§5 Phase 4; read by the dashboard's /system
# page)
# ---------------------------------------------------------------------------


class notification_repo:
    @staticmethod
    def add(kind, message, ok):
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO notification_log (kind, message, ok) VALUES (?, ?, ?)",
                (kind, message, 1 if ok else 0),
            )

    @staticmethod
    def list_recent(limit=20):
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM notification_log ORDER BY sent_at DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# reset_satellite_state -- shared full-exit reset (PLAN_V3.md §5 /trades/new)
# ---------------------------------------------------------------------------


def reset_satellite_state(ticker):
    """Reset one satellite ticker's strategy state after a full exit,
    shared by the dashboard Sell form and the /latches "reset
    satellite state" button.

    Clears entry_count/base_price/entry_atr/stop_loss_cal_price/
    monitor_reversal and re-arms every latch for the ticker, so the next
    tick re-ratchets base_price from the current price and dip/stop alerts
    can fire again from a clean slate.

    NOTE: crediting the sale proceeds (qty*price - fee) back to
    satellite_pool is deliberately NOT done here -- that's the Sell
    handler's job (per PLAN_V3.md §5), since the manual "reset satellite
    state" recovery button must NOT also move money.
    """
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE assets
            SET entry_count=0, base_price=NULL, entry_atr=NULL,
                stop_loss_cal_price=NULL, monitor_reversal=0,
                updated_at=datetime('now')
            WHERE ticker=?
            """,
            (ticker,),
        )
        conn.execute(
            """
            UPDATE alert_latches SET rearmed_at=datetime('now')
            WHERE ticker=? AND rearmed_at IS NULL
            """,
            (ticker,),
        )
