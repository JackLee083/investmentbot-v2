"""Session-wide test environment defaults.

Runs before any test module is imported (pytest always loads conftest.py
first), which matters specifically for tests/test_web.py: importing
bot_server executes `app = create_app()` at module scope (see bot_server.py
-- gunicorn needs a plain module-level WSGI object), so DISABLE_SCHEDULER
must already be "1" in os.environ the first time ANY test file imports
bot_server, or that first call would try to start the real APScheduler
grid and enqueue a real startup tick against ccxt/ib_insync/LINE.

Using setdefault (not setenv) everywhere so an individual test can still
monkeypatch its own value for one test without fighting this file.

INVESTBOT_DB defaults to a throwaway path so that same first import's
init_db() call never touches the real var/investbot.db. Tests that need a
specific DB state override this per-test via monkeypatch + their own
init_db() call, same pattern as tests/test_repo.py's `db_path` fixture --
db.database.get_db_path() re-reads the env var at call time, so this
default is only ever a safety net, never load-bearing for an actual test's
assertions.
"""

import os
import tempfile

os.environ.setdefault("DISABLE_SCHEDULER", "1")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret-key-not-for-production")
os.environ.setdefault(
    "INVESTBOT_DB", os.path.join(tempfile.mkdtemp(prefix="investbot-conftest-"), "unused.db")
)
