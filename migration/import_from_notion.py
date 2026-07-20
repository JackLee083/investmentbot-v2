"""One-off migration: Notion (read-only) -> SQLite.

Usage:
    python -m migration.import_from_notion --dry-run
    python -m migration.import_from_notion [--db PATH]
        [--qqq-pool X --stock-pool X --sat-pool X --iau-rollover X]

Reuses NOTION_TOKEN and the hardcoded database/page IDs already defined in
config/config_loader.py. Phase 1's services/notion_utils.fetch_notion_database()
ignored Notion's has_more/next_cursor and silently truncated any database
over 100 rows, so this script always implemented its own paginated query
helper instead (see PLAN_V3.md §8 risk 4); Phase 2 has since deleted
services/notion_utils.py entirely (dashboard/bot no longer talk to Notion at
all), so this script also keeps its own tiny NOTION_HEADERS copy below --
it's the one remaining piece of v3 that still legitimately talks to Notion.

Read-only guarantee: the only Notion HTTP calls made anywhere in this file
are POST .../databases/{id}/query (Notion's read/query endpoint -- it does
not create or modify anything despite the POST verb) and GET .../pages/{id}
(page retrieve). Nothing here ever calls update_notion_properties(),
create_notion_page(), or any other Notion write endpoint.

Re-run behaviour (intended as a ONE-OFF; Phase 6 rebuilds into a brand-new
DB anyway): assets and price_alerts are upserted and transactions are
deduped on tx_id, so re-running is safe for those. But be aware that a
re-run also (a) RE-FIRES latches from the Notion checkboxes, clobbering any
re-arms done since, and (b) OVERWRITES pools (app_state) and config seeds
from Notion/CLI/defaults, discarding any runtime changes made through the
dashboard. Don't re-run against a live database you've been mutating.
"""

import argparse
import sys

import requests

from config.config_loader import (
    NOTION_TOKEN,
    DATABASE_ID,
    TRANSACTION_LOG_DB_ID,
    PRICE_ALERT_ID,
    ASSETSDASHBOARD_PAGE_ID,
    BASE_AMOUNT,
    DCA_STOCK_TICKER,
)

# Phase 2 deleted services/notion_utils.py (the whole module, incl. its
# NOTION_HEADERS constant this script used to import) -- this migration
# script is the one place in v3 that's still legitimately talking to the
# Notion API, so it keeps its own minimal, self-contained copy rather than
# depending on a module the rest of the app no longer has.
NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Content-Type": "application/json",
    "Notion-Version": "2022-06-28",
}

NOTION_ASSETS_DB = DATABASE_ID
NOTION_TX_DB = TRANSACTION_LOG_DB_ID
NOTION_ALERTS_DB = PRICE_ALERT_ID
NOTION_DASHBOARD_PAGE = ASSETSDASHBOARD_PAGE_ID

# Tickers that must be priced via Kraken rather than Yahoo (PLAN_V3.md Phase 1 spec).
KRAKEN_TICKERS = {"BTCUSD", "BTC", "BTC/USD", "ETH", "ETHUSD"}


# ---------------------------------------------------------------------------
# Notion access -- paginated, read-only
# ---------------------------------------------------------------------------


def fetch_all_notion_pages(database_id):
    """Query a Notion database and follow has_more/next_cursor until every
    page has been fetched -- the paginated query helper that the deleted
    services.notion_utils.fetch_notion_database() never had (it truncated
    at 100 rows)."""
    results = []
    payload = {}
    url = f"https://api.notion.com/v1/databases/{database_id}/query"
    while True:
        res = requests.post(url, headers=NOTION_HEADERS, json=payload, timeout=30)
        res.raise_for_status()
        data = res.json()
        results.extend(data.get("results", []))
        if data.get("has_more") and data.get("next_cursor"):
            payload = {"start_cursor": data["next_cursor"]}
        else:
            break
    return results


def fetch_notion_page(page_id):
    """Retrieve a single Notion page (read-only GET)."""
    url = f"https://api.notion.com/v1/pages/{page_id}"
    res = requests.get(url, headers=NOTION_HEADERS, timeout=30)
    res.raise_for_status()
    return res.json()


# ---------------------------------------------------------------------------
# Notion property extraction helpers
# ---------------------------------------------------------------------------


def prop_title(props, name):
    arr = props.get(name, {}).get("title", [])
    return arr[0]["plain_text"] if arr else None


def prop_number(props, name):
    return props.get(name, {}).get("number")


def prop_select(props, name):
    sel = props.get(name, {}).get("select")
    return sel["name"] if sel else None


def prop_checkbox(props, name):
    return bool(props.get(name, {}).get("checkbox"))


def prop_date(props, name):
    d = props.get(name, {}).get("date")
    return d["start"] if d else None


def prop_relation_ids(props, name):
    return [r["id"] for r in props.get(name, {}).get("relation", [])]


def hv180_to_percent(value):
    """v2's main.py wrote HV180 to Notion as a FRACTION ("HV 180": hv180/100,
    e.g. 0.28 for 28%), but assets.hv180 stores percent (e.g. 28.0). Convert
    on import. Guard: only scale values < 5 -- no real 180-day historical
    volatility is below 5% while fractions are always < 5, so anything >= 5
    is assumed to already be in percent (e.g. hand-entered)."""
    if value is None:
        return None
    if value < 5:
        return value * 100
    return value


# Tickers with no market quote of their own (a literal cash line, not a
# priced instrument) -- these get price_source 'skip'. A Cash-TYPE asset that
# IS a real ETF (SGOV, the cash-sweep vehicle) must NOT be skipped: it has a
# Yahoo price, and skipping it leaves its current_price blank on the watchlist
# while the sweep code fetches the price separately anyway. So route by whether
# the ticker is actually quotable, not by the Cash asset_type alone.
UNQUOTED_TICKERS = {"CASH", "USD", "USDT", "USDC"}


def price_source_for(ticker, asset_type):
    if ticker in UNQUOTED_TICKERS:
        return "skip"
    if ticker in KRAKEN_TICKERS:
        return "kraken"
    return "yahoo"


# ---------------------------------------------------------------------------
# Import steps
# ---------------------------------------------------------------------------


def import_assets():
    """Returns (page_id_to_ticker map, stats dict)."""
    from db.repo import assets_repo, latch_repo

    pages = fetch_all_notion_pages(NOTION_ASSETS_DB)
    page_id_to_ticker = {}
    imported = 0
    skipped = 0
    latches_seeded = 0

    for page in pages:
        props = page["properties"]
        ticker = prop_title(props, "Ticker")
        if not ticker:
            print(f"  [skip] asset page {page['id']}: no Ticker")
            skipped += 1
            continue

        asset_type = prop_select(props, "Asset Type")
        if asset_type not in ("Core", "Satellite", "Cash"):
            print(f"  [skip] {ticker}: invalid/missing Asset Type ({asset_type!r})")
            skipped += 1
            continue

        page_id_to_ticker[page["id"]] = ticker

        entry_count = int(prop_number(props, "Entry Count") or 0)
        tier = prop_select(props, "Tier")
        if tier not in ("T1", "T2", "T3"):
            tier = None

        row = {
            "ticker": ticker,
            "asset_type": asset_type,
            "price_source": price_source_for(ticker, asset_type),
            "active": 1,
            "current_price": prop_number(props, "Current Price"),
            "base_price": prop_number(props, "Base Price"),
            "entry_count": entry_count,
            "entry_atr": prop_number(props, "Entry ATR"),
            "stop_loss_cal_price": prop_number(props, "Stop Loss Cal Price"),
            "tier": tier,
            "hv180": hv180_to_percent(prop_number(props, "HV 180")),
            "monitor_reversal": 1 if prop_checkbox(props, "Monitor Reversal") else 0,
            "last_buy_date": prop_date(props, "Last Buy Date"),
            # Deliberately NOT imported: Notion's "Entry Price N" / "Entry AmountN"
            # formula columns -- these are recomputed live from core/dca.py in
            # Phase 2, per PLAN_V3.md Phase 1 spec.
        }
        assets_repo.upsert(row)
        imported += 1

        # Latch seeding from the old one-shot checkboxes (PLAN_V3.md §5/§8 risk 5).
        if prop_checkbox(props, "Sat Notified"):
            next_level = entry_count + 1
            if next_level <= 3:
                latch_repo.fire(ticker, "dip", next_level)
                latches_seeded += 1
            else:
                # entry_count >= 3: there is no level-4 dip entry, so a
                # ('dip', 4) latch row would be dead data -- skip it.
                print(f"  [note] {ticker}: Sat Notified set but entry_count={entry_count}; no dip latch to seed")
        if prop_checkbox(props, "ATR Notified"):
            latch_repo.fire(ticker, "stop_loss", entry_count)
            latches_seeded += 1

    print(f"Assets: imported {imported}, skipped {skipped}, latches seeded {latches_seeded}")
    return page_id_to_ticker, {"imported": imported, "skipped": skipped}


def import_transactions(page_id_to_ticker):
    from db.repo import tx_repo

    pages = fetch_all_notion_pages(NOTION_TX_DB)
    inserted = 0
    duplicates = 0
    skipped = 0

    for page in pages:
        props = page["properties"]
        tx_id = prop_title(props, "Transaction ID")
        side = prop_select(props, "Type")
        price = prop_number(props, "Price")
        qty = prop_number(props, "Amount")
        total = prop_number(props, "Netflow")
        broker = prop_select(props, "Broker")
        source = prop_select(props, "Source")
        executed_at = prop_date(props, "Transaction Date")
        fee = prop_number(props, "Transaction Fees") or 0.0
        avg_cost_snapshot = prop_number(props, "Avg Cost Snapshot")

        relation_ids = prop_relation_ids(props, "Assets")
        ticker = None
        for rid in relation_ids:
            if rid in page_id_to_ticker:
                ticker = page_id_to_ticker[rid]
                break

        missing = [
            n
            for n, v in {
                "Transaction ID": tx_id,
                "ticker (Assets relation)": ticker,
                "side": side,
                "price": price,
                "qty": qty,
                "total (Netflow)": total,
                "broker": broker,
                "source": source,
                "executed_at": executed_at,
            }.items()
            if v is None
        ]
        if missing:
            print(f"  [skip] tx page {page['id']}: missing {', '.join(missing)}")
            skipped += 1
            continue

        if side not in ("Buy", "Sell") or broker not in ("IBKR", "Kraken", "Manual"):
            print(f"  [skip] tx {tx_id}: invalid side={side!r} or broker={broker!r}")
            skipped += 1
            continue

        was_inserted = tx_repo.insert_ignore(
            {
                "tx_id": tx_id,
                "ticker": ticker,
                "side": side,
                "qty": qty,
                "price": price,
                "fee": fee,
                "total": total,
                "broker": broker,
                "source": source,
                "avg_cost_snapshot": avg_cost_snapshot,
                "executed_at": executed_at,
            }
        )
        if was_inserted:
            inserted += 1
        else:
            duplicates += 1

    print(f"Transactions: inserted {inserted}, already present {duplicates}, skipped {skipped}")
    return {"inserted": inserted, "duplicates": duplicates, "skipped": skipped}


def import_price_alerts():
    from db.repo import alerts_repo

    pages = fetch_all_notion_pages(NOTION_ALERTS_DB)
    existing = alerts_repo.list()
    imported = 0
    updated = 0
    skipped = 0

    for page in pages:
        props = page["properties"]
        ticker = prop_title(props, "Ticker")
        target_price = prop_number(props, "Target Price")
        direction = prop_select(props, "Direction")
        status = prop_select(props, "Status") or "Active"

        if not ticker or target_price is None:
            print(f"  [skip] alert page {page['id']}: missing ticker/target_price")
            skipped += 1
            continue

        if direction not in ("Above", "Below"):
            print(f"  [note] alert {ticker}@{target_price}: no/invalid Direction, defaulting to 'Below'")
            direction = "Below"

        if status not in ("Active", "Triggered", "Cancelled"):
            status = "Active"

        # No natural key survives the Notion -> SQLite move (price_alerts.id is
        # an autoincrement surrogate), so idempotency here means "match on
        # (ticker, target_price, direction) and update status instead of
        # inserting a duplicate row" -- the REPLACE-equivalent for this table.
        match = next(
            (
                a
                for a in existing
                if a["ticker"] == ticker
                and a["target_price"] == target_price
                and a["direction"] == direction
            ),
            None,
        )
        if match:
            if match["status"] != status:
                alerts_repo.update_status(match["id"], status)
                updated += 1
        else:
            new_id = alerts_repo.add(ticker, target_price, direction, status)
            existing.append(
                {"id": new_id, "ticker": ticker, "target_price": target_price,
                 "direction": direction, "status": status}
            )
            imported += 1

    print(f"Price alerts: imported {imported}, updated {updated}, skipped {skipped}")
    return {"imported": imported, "updated": updated, "skipped": skipped}


def import_pools(args):
    from db.repo import state_repo

    notion_pools = {"qqq_pool": 0.0, "stock_pool": 0.0, "satellite_pool": 0.0}
    try:
        page = fetch_notion_page(NOTION_DASHBOARD_PAGE)
        props = page.get("properties", {})
        notion_pools["qqq_pool"] = prop_number(props, "QQQ Pool") or 0.0
        notion_pools["stock_pool"] = prop_number(props, "Stock Pool") or 0.0
        notion_pools["satellite_pool"] = prop_number(props, "Satellite Pool") or 0.0
    except Exception as e:
        print(f"  [warn] could not read Dashboard page pools from Notion: {e}")

    final = {
        "qqq_pool": args.qqq_pool if args.qqq_pool is not None else notion_pools["qqq_pool"],
        "stock_pool": args.stock_pool if args.stock_pool is not None else notion_pools["stock_pool"],
        "satellite_pool": args.sat_pool if args.sat_pool is not None else notion_pools["satellite_pool"],
        # Notion never tracked this (only the empty/broken data/dca_state.json did) --
        # CLI override or 0.0.
        "iau_rollover": args.iau_rollover if args.iau_rollover is not None else 0.0,
    }
    for key, value in final.items():
        state_repo.set(key, value)
    print(f"Pools: {final}")
    return final


def import_config_seeds():
    from db.repo import config_repo

    seeds = {
        "BASE_AMOUNT": BASE_AMOUNT,
        "DCA_STOCK_TICKER": DCA_STOCK_TICKER,
        "SGOV_CASH_MULT": 1.95,
        "KRAKEN_BTC_PCT": 0.10,
        "QQQ_FIXED_PCT": 0.30,
        "STOCK_FIXED_PCT": 0.15,
        "INDICATOR_STALE_HOURS": 24,
    }
    for key, value in seeds.items():
        config_repo.set(key, value)
    print(f"Config seeds written: {list(seeds.keys())}")
    return seeds


def print_summary():
    from db.database import get_conn

    tables = [
        "assets",
        "transactions",
        "app_state",
        "config",
        "price_alerts",
        "alert_latches",
        "indicator_snapshots",
        "positions",
        "notification_log",
    ]
    print("\n--- Row counts per table ---")
    with get_conn() as conn:
        for t in tables:
            n = conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"]
            print(f"  {t:<22} {n}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_arg_parser():
    parser = argparse.ArgumentParser(
        description="One-off, read-only Notion -> SQLite migration for Investment Bot v3."
    )
    parser.add_argument("--db", default=None, help="Override the SQLite DB path (sets INVESTBOT_DB).")
    parser.add_argument("--qqq-pool", type=float, default=None, help="Override QQQ pool value.")
    parser.add_argument("--stock-pool", type=float, default=None, help="Override stock pool value.")
    parser.add_argument("--sat-pool", type=float, default=None, help="Override satellite pool value.")
    parser.add_argument("--iau-rollover", type=float, default=None, help="Override IAU rollover value.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print which Notion databases/pages WOULD be read, then exit without any network calls.",
    )
    return parser


def run_dry_run():
    print("=== DRY RUN -- no network calls will be made ===")
    print("Would read the following Notion sources (read-only: query/retrieve only):")
    print(f"  Assets database:        {NOTION_ASSETS_DB}")
    print(f"  Transactions database:  {NOTION_TX_DB}")
    print(f"  Price alerts database:  {NOTION_ALERTS_DB}")
    print(f"  Dashboard page (pools): {NOTION_DASHBOARD_PAGE}")
    if NOTION_TOKEN:
        print("NOTION_TOKEN: set (value not shown).")
    else:
        print(
            "NOTION_TOKEN: NOT SET. A real (non-dry-run) invocation would abort "
            "immediately with an error -- set it in .env before running for real."
        )
    print("Dry run complete. No data was read or written.")


def main():
    args = build_arg_parser().parse_args()

    if args.dry_run:
        run_dry_run()
        return 0

    if not NOTION_TOKEN:
        print(
            "ERROR: NOTION_TOKEN is not set (checked environment and .env). "
            "Cannot query Notion. Set NOTION_TOKEN and try again, or use --dry-run "
            "to preview what this script would do without it."
        )
        return 1

    if args.db:
        import os

        os.environ["INVESTBOT_DB"] = args.db

    from db.database import init_db, get_db_path

    print(f"Target SQLite DB: {get_db_path()}")
    init_db()

    print("\n--- Importing assets ---")
    page_id_to_ticker, _ = import_assets()

    print("\n--- Importing transactions (paginated) ---")
    import_transactions(page_id_to_ticker)

    print("\n--- Importing price alerts ---")
    import_price_alerts()

    print("\n--- Importing pools ---")
    import_pools(args)

    print("\n--- Writing config seeds ---")
    import_config_seeds()

    print_summary()
    print("\nMigration complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
