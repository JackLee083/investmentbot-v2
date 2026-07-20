"""App factory for Investment Bot v3 -- see PLAN_V3.md §5/§6 Phase 3.

create_app() wires together:
  - db.database.init_db()          -- SQLite schema, idempotent every boot.
  - web.web_bp                     -- the dashboard (auth-guarded except
                                       /login and /healthz).
  - services.line_notify.warn_if_recipients_empty() -- startup sanity check:
                                       loudly warns (print + notification_log
                                       row) if ADMIN_IDS/REPORT_VIEWER_IDS/
                                       SIGNAL_VIEWER_IDS parse empty. Inbound
                                       LINE handling (webhook, `設定`/`STATUS`)
                                       is gone entirely as of Phase 4 -- see
                                       PLAN_V3.md §1/§5 -- so there is no
                                       line_webhook blueprint to register.
  - scheduler_job.init_scheduler() -- the APScheduler cron grid.
  - a STARTUP TICK enqueued at now+5s -- healthz companion rule (1); see
    web/views.py's healthz() docstring and PLAN_V3.md §5's healthz row.
    Without this, a bot restarted during an overnight cron gap (e.g. 23:00,
    next scheduled tick not until 04:00) would inherit the stale
    pre-restart last_tick_finished timestamp, healthz would read >7h old,
    and autoheal would restart-loop the container for hours. Enqueuing a
    one-off tick right after boot refreshes that timestamp within seconds
    regardless of where in the cron grid the restart happened.

DISABLE_SCHEDULER=1 skips the scheduler AND the startup tick entirely --
used by tests and local dev, where nothing should try to reach IB Gateway,
Kraken, or LINE, or leave a background thread running past the process.
This is the same flag web/auth.py reads to decide dev-friendly fallbacks
for FLASK_SECRET_KEY / the session cookie's Secure flag.

Module-level `app = create_app()` at the bottom exists for gunicorn: the
Dockerfile's CMD points at `bot_server:app` (a plain WSGI object) rather
than a factory string, which keeps the gunicorn invocation simple and
version-independent.
"""

import atexit
import os
from datetime import datetime, timedelta

from flask import Flask

from config.config_loader import est
from db.database import init_db
from services.line_notify import warn_if_recipients_empty
from web import web_bp
from web.auth import configure_session


def create_app():
    # static_folder=None: the web blueprint is the app's ONLY '/static'
    # route (vendored pico.min.css / htmx.min.js) -- see web/__init__.py's
    # module docstring for why registering a second one here would create
    # an ambiguous duplicate route.
    app = Flask(__name__, static_folder=None)

    configure_session(app)

    init_db()

    app.register_blueprint(web_bp)

    # Config sanity check, not a network call -- safe to run on every boot,
    # including under DISABLE_SCHEDULER=1 test/local-dev runs (it only ever
    # prints + writes a notification_log row; see warn_if_recipients_empty's
    # docstring for why it deliberately never touches the LINE API).
    warn_if_recipients_empty()

    if os.environ.get("DISABLE_SCHEDULER") == "1":
        print("DISABLE_SCHEDULER=1 -- skipping APScheduler + startup tick (test/local-dev mode).")
        app.config["SCHEDULER"] = None
        return app

    # Imported lazily (only on the real-deployment path) so a DISABLE_SCHEDULER=1
    # test run never pulls in jobs.tick's ccxt/ib_insync dependency chain --
    # nothing in a test process should be able to open a real network socket.
    from jobs.tick import run_tick
    from scheduler_job import init_scheduler

    scheduler = init_scheduler()
    # Stashed on app.config, not a module global, so web/views.py's healthz()
    # can fetch it lazily via current_app at request time without importing
    # this module (which would import web, which imports web.views -- a
    # circular import). See web/views.py's healthz() docstring.
    app.config["SCHEDULER"] = scheduler
    atexit.register(lambda: scheduler.shutdown())

    scheduler.add_job(
        func=run_tick,
        next_run_time=datetime.now(est) + timedelta(seconds=5),
        # Never let APScheduler discard this job as "misfired": with the
        # default grace time (1s), a boot that takes a moment too long --
        # gunicorn still importing, thread pool busy -- would silently DROP
        # the startup tick, last_tick_finished would stay stale, and the
        # exact restart loop PLAN_V3.md §5 healthz rule ① exists to prevent
        # would come back through the side door. None = run whenever
        # dispatch happens, however late.
        misfire_grace_time=None,
        id="startup_tick",
        name="Startup Tick",
    )

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, use_reloader=False)
