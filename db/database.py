"""SQLite connection helper + schema bootstrap for Investment Bot v3.

Single source of truth for how the rest of the app talks to SQLite:
- get_conn(): a context manager that yields a sqlite3.Connection with the
  PRAGMAs the plan requires (WAL journal, 5s busy timeout, FK enforcement)
  and dict-like row access. Commits on a clean exit, rolls back on any
  exception raised inside the `with` block.
- init_db(): applies db/schema.sql. The schema is written entirely with
  `CREATE TABLE/INDEX IF NOT EXISTS`, so calling this repeatedly (e.g. on
  every app boot) is a no-op after the first run.

DB_PATH defaults to var/investbot.db (relative to the repo root) and can be
overridden with the INVESTBOT_DB env var -- tests point this at a temp file
so they never touch the real database.
"""

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


def _default_db_path() -> str:
    return str(BASE_DIR / "var" / "investbot.db")


def get_db_path() -> str:
    """Resolve the DB path at call time so INVESTBOT_DB overrides (e.g. set by
    a test right before it runs) are always honored, even if this module was
    imported earlier in the process."""
    return os.environ.get("INVESTBOT_DB", _default_db_path())


# Kept for backwards-compatible/simple access (e.g. `from db.database import DB_PATH`).
# Prefer get_db_path() in new code since it re-reads the env var each call.
DB_PATH = get_db_path()


@contextmanager
def get_conn():
    """Yield a sqlite3.Connection configured per PLAN_V3.md §2/§3.

    Commits automatically when the `with` block exits cleanly; rolls back and
    re-raises if the block raises. Always closes the connection.
    """
    db_path = get_db_path()
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.execute("PRAGMA foreign_keys=ON;")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    """Create all tables/indexes if they don't already exist, and make sure
    the single-row schema_version table is seeded to 1. Safe to call on every
    app startup."""
    schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")
    with get_conn() as conn:
        conn.executescript(schema_sql)
        row = conn.execute("SELECT COUNT(*) AS n FROM schema_version").fetchone()
        if row["n"] == 0:
            conn.execute("INSERT INTO schema_version (version) VALUES (1)")
