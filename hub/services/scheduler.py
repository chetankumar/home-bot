"""APScheduler wrapper: per-app job ids, run_now(), and run history in hub.db.

Job exceptions are logged against the owning app and recorded in job_runs;
they never propagate into the scheduler or the host.
"""

from __future__ import annotations

import logging
import threading
import traceback
from collections.abc import Callable
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from hub.services.db import Database

log = logging.getLogger("hub.scheduler")


class Scheduler:
    def __init__(self, db: Database, tz: ZoneInfo):
        self._db = db
        self.tz = tz
        self._sched = BackgroundScheduler(timezone=tz)
        self._funcs: dict[str, tuple[str, str, Callable[[], Any]]] = {}
        self._locks: dict[str, threading.Lock] = {}

    # -- lifecycle ---------------------------------------------------------------
    def start(self) -> None:
        with self._db() as conn:
            conn.execute(
                "UPDATE job_runs SET status = 'interrupted', finished_at = ?"
                " WHERE status = 'running'",
                (self._now(),),
            )
        if not self._sched.running:
            self._sched.start()

    def shutdown(self) -> None:
        if self._sched.running:
            self._sched.shutdown(wait=False)

    # -- registration ------------------------------------------------------------
    @staticmethod
    def full_id(app_id: str, job_id: str) -> str:
        return f"{app_id}:{job_id}"

    def add(self, app_id: str, job_id: str, func: Callable[[], Any], trigger: Any) -> str:
        fid = self.full_id(app_id, job_id)
        self._funcs[fid] = (app_id, job_id, func)
        self._locks.setdefault(fid, threading.Lock())
        self._sched.add_job(
            self._execute,
            trigger=trigger,
            args=[fid, "schedule"],
            id=fid,
            name=fid,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=3600,
        )
        return fid

    def remove_app(self, app_id: str) -> None:
        for fid in [f for f, (a, _, _) in self._funcs.items() if a == app_id]:
            self._funcs.pop(fid, None)
            try:
                self._sched.remove_job(fid)
            except Exception:
                pass

    # -- execution -------------------------------------------------------------------
    def _now(self) -> str:
        return datetime.now(self.tz).isoformat(timespec="seconds")

    def _execute(self, fid: str, trigger: str, locked: bool = False) -> None:
        app_id, job_id, func = self._funcs[fid]
        lock = self._locks[fid]
        if not locked and not lock.acquire(blocking=False):
            log.info("job %s already running; skipping %s trigger", fid, trigger)
            return
        try:
            with self._db() as conn:
                run_id = conn.execute(
                    "INSERT INTO job_runs(app_id, job_id, trigger, started_at, status)"
                    " VALUES (?, ?, ?, ?, 'running')",
                    (app_id, job_id, trigger, self._now()),
                ).lastrowid
            status, error = "ok", None
            try:
                func()
            except Exception:
                status, error = "error", traceback.format_exc()
                logging.getLogger(f"apps.{app_id}").exception("job %s failed", job_id)
            with self._db() as conn:
                conn.execute(
                    "UPDATE job_runs SET status = ?, error = ?, finished_at = ? WHERE id = ?",
                    (status, error, self._now(), run_id),
                )
        finally:
            lock.release()

    def run_now(self, app_id: str, job_id: str, wait: bool = False) -> bool:
        """Run a job immediately in a background thread. False if it's already running."""
        fid = self.full_id(app_id, job_id)
        if fid not in self._funcs:
            raise KeyError(f"no job {fid}")
        # Take the lock here, not in the thread, so is_running() is true the
        # moment this returns (the UI renders "Syncing…" and starts polling).
        if not self._locks[fid].acquire(blocking=False):
            return False
        t = threading.Thread(target=self._execute, args=(fid, "manual", True), daemon=True)
        t.start()
        if wait:
            t.join()
        return True

    def is_running(self, app_id: str, job_id: str) -> bool:
        lock = self._locks.get(self.full_id(app_id, job_id))
        return bool(lock and lock.locked())

    # -- history -------------------------------------------------------------------
    def last_run(self, app_id: str, job_id: str) -> dict[str, Any] | None:
        with self._db() as conn:
            row = conn.execute(
                "SELECT * FROM job_runs WHERE app_id = ? AND job_id = ? ORDER BY id DESC LIMIT 1",
                (app_id, job_id),
            ).fetchone()
        if row is None:
            return None
        run = dict(row)
        # A row can say 'running' before the thread takes the lock or after a crash;
        # trust the live lock for the current process.
        if run["status"] == "running" and not self.is_running(app_id, job_id):
            run["status"] = "interrupted"
        return run

    def jobs(self) -> list[dict[str, Any]]:
        out = []
        for fid, (app_id, job_id, _) in sorted(self._funcs.items()):
            job = self._sched.get_job(fid)
            out.append(
                {
                    "id": fid,
                    "app_id": app_id,
                    "job_id": job_id,
                    "trigger": str(job.trigger) if job else "",
                    "next_run": getattr(job, "next_run_time", None) if job else None,
                    "running": self.is_running(app_id, job_id),
                    "last_run": self.last_run(app_id, job_id),
                }
            )
        return out


class AppScheduler:
    """The scheduler as one app sees it: job ids are namespaced by app id."""

    def __init__(self, scheduler: Scheduler, app_id: str):
        self._s = scheduler
        self.app_id = app_id

    def cron(self, job_id: str, func: Callable[[], Any], expr: str) -> str:
        """Standard 5-field crontab expression, evaluated in the hub timezone."""
        return self._s.add(
            self.app_id, job_id, func, CronTrigger.from_crontab(expr, timezone=self._s.tz)
        )

    def interval(self, job_id: str, func: Callable[[], Any], **every: int) -> str:
        """e.g. interval("poll", fn, minutes=15)."""
        return self._s.add(
            self.app_id, job_id, func, IntervalTrigger(timezone=self._s.tz, **every)
        )

    def run_now(self, job_id: str, wait: bool = False) -> bool:
        return self._s.run_now(self.app_id, job_id, wait=wait)

    def is_running(self, job_id: str) -> bool:
        return self._s.is_running(self.app_id, job_id)

    def last_run(self, job_id: str) -> dict[str, Any] | None:
        return self._s.last_run(self.app_id, job_id)
