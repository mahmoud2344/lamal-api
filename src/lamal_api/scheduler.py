"""Optional in-process scheduler for periodic data syncs.

Enabled by setting ``SYNC_CRON`` to a five-field cron expression. Leaving it
empty disables the scheduler entirely, which is the right choice when an
external cron or a Kubernetes CronJob drives ``lamal-api sync`` instead.
"""

from __future__ import annotations

import logging
import threading

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import Engine

from .config import Settings

log = logging.getLogger(__name__)

#: Guards against a scheduled run overlapping a manual or startup sync.
_sync_lock = threading.Lock()


def run_sync_guarded(engine: Engine, settings: Settings, *, reason: str) -> None:
    """Run a sync unless one is already in flight."""
    if not _sync_lock.acquire(blocking=False):
        log.warning("skipping %s sync: another sync is already running", reason)
        return
    try:
        from .fetch.sync import sync

        log.info("starting %s sync", reason)
        report = sync(engine, settings)
        log.info("%s sync finished\n%s", reason, report.summary())
    except Exception:
        log.exception("%s sync failed", reason)
    finally:
        _sync_lock.release()


def start_background_sync(engine: Engine, settings: Settings, *, reason: str) -> threading.Thread:
    """Kick off a sync on a worker thread so the API can serve immediately."""
    thread = threading.Thread(
        target=run_sync_guarded,
        args=(engine, settings),
        kwargs={"reason": reason},
        name=f"lamal-sync-{reason}",
        daemon=True,
    )
    thread.start()
    return thread


def start_scheduler(engine: Engine, settings: Settings) -> BackgroundScheduler | None:
    """Start the cron scheduler, or return ``None`` when disabled."""
    expression = settings.sync_cron.strip()
    if not expression:
        return None
    try:
        trigger = CronTrigger.from_crontab(expression)
    except ValueError:
        log.error(
            "SYNC_CRON=%r is not a valid five-field cron expression; scheduler disabled",
            expression,
        )
        return None

    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(
        run_sync_guarded,
        trigger=trigger,
        args=(engine, settings),
        kwargs={"reason": "scheduled"},
        id="lamal-sync",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )
    scheduler.start()
    log.info("sync scheduler started with cron %r (UTC)", expression)
    return scheduler
