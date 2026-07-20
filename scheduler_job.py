import glob
import os
import sqlite3
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from config.config_loader import est, mel_tz
from db.database import get_db_path
from jobs.tick import run_tick
from services.line_notify import line_bot

BACKUP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "var", "backup")
BACKUPS_TO_KEEP = 14


def safe_run_bot():
    """Safe wrapper around jobs.tick.run_tick -- run_tick already contains
    its own top-level try/except (see jobs/tick.py), so this is a second,
    outer safety net specifically for the scheduler: if anything above and
    beyond a normal tick failure ever escapes run_tick(), it must not kill
    the APScheduler job (an uncaught exception in a job callback would
    otherwise just get logged by APScheduler and silently stop that job
    from being rescheduled correctly in some configurations)."""
    try:
        print(f"[{datetime.now()}] Schedule triggered: starting tick...")
        run_tick()
        print(f"[{datetime.now()}] Tick finished.")
    except Exception as e:
        print(f"Unexpected error during scheduled run: {e}")


def notify_weekly_login():
    line_bot.notify_weekly_login()


def backup_database():
    """Nightly SQLite backup via sqlite3.Connection.backup() (safe under
    WAL -- unlike a plain file copy, which can grab a torn/inconsistent
    snapshot while the bot is mid-write). Keeps the newest BACKUPS_TO_KEEP
    files, deletes the rest. See PLAN_V3.md §8 risk 1."""
    try:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        stamp = datetime.now(mel_tz).strftime("%Y%m%d")
        dest_path = os.path.join(BACKUP_DIR, f"investbot-{stamp}.db")

        src = sqlite3.connect(get_db_path())
        dest = sqlite3.connect(dest_path)
        with dest:
            src.backup(dest)
        dest.close()
        src.close()
        print(f"[{datetime.now()}] Database backup complete: {dest_path}")

        existing = sorted(glob.glob(os.path.join(BACKUP_DIR, "investbot-*.db")))
        for old_file in existing[:-BACKUPS_TO_KEEP]:
            os.remove(old_file)
            print(f"Removed old backup: {old_file}")
    except Exception as e:
        print(f"Database backup failed: {e}")


def init_scheduler():
    # job_defaults rationale (companion to PLAN_V3.md §5 healthz rule ①):
    # APScheduler's default misfire_grace_time is 1 SECOND -- any cron tick
    # whose dispatch is delayed past that (a long-running previous tick
    # hogging the pool thread, a GC pause, VPS CPU contention) would be
    # silently discarded, quietly skipping trading ticks. 300s says "run it
    # anyway if it's less than 5 minutes late" (well inside the 20-min cron
    # spacing). coalesce=True makes several missed runs of the SAME job
    # collapse into one catch-up execution instead of firing back-to-back
    # -- one fresh tick is a full resync, replaying stale ones adds nothing.
    scheduler = BackgroundScheduler(
        job_defaults={"misfire_grace_time": 300, "coalesce": True}
    )

    # US market trading session (refresh every 20 minutes)

    # Window 1: 09:30, 09:50
    scheduler.add_job(
        func=safe_run_bot,
        trigger=CronTrigger(hour=9, minute='30,50', timezone=est),
        id='market_open_9',
        name='Market Open (09:30, 09:50)'
    )

    # Regular intraday window (10:00 ~ 15:59)
    scheduler.add_job(
        func=safe_run_bot,
        trigger=CronTrigger(hour='10-15', minute='10,30,50', timezone=est),
        id='market_regular',
        name='Market Hours (Every 20 mins)'
    )

    # Post-close settlement (16:00)
    scheduler.add_job(
        func=safe_run_bot,
        trigger=CronTrigger(hour='16,22,4', minute=0, timezone=est),
        id='post_market_6h',
        name='Post Market (16:00, 22:00, 04:00)'
    )

    # LINE DCA notification (Melbourne 20:00)
    scheduler.add_job(
        func=safe_run_bot,
        trigger=CronTrigger(hour=20, minute=0, timezone=mel_tz),
        id='daily_notify',
        name='Daily Notification (AU)'
    )

    # Weekly IB Key 2FA reminder -- aligned to the actual Sunday
    # re-authentication time (~17:00 Melbourne ≈ Sunday early-morning ET,
    # see PLAN_V3.md §1 defaults).
    scheduler.add_job(
        func=notify_weekly_login,
        trigger=CronTrigger(day_of_week='sun', hour=17, minute=0, timezone=mel_tz),
        id='weekly_login_alert',
        name='Weekly Login Reminder'
    )

    # Nightly database backup (Melbourne 03:30, off-peak)
    scheduler.add_job(
        func=backup_database,
        trigger=CronTrigger(hour=3, minute=30, timezone=mel_tz),
        id='nightly_db_backup',
        name='Nightly DB Backup'
    )

    scheduler.start()
    print("APScheduler started, waiting for job triggers...")
    return scheduler
