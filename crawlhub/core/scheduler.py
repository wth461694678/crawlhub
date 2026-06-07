"""Scheduled tasks for CrawlHub Daemon.

Manages:
- Archived task auto-purge (daily at configurable time): permanently
  delete tasks that have been in the recycle bin for > archived_purge_days,
  including their on-disk output and per-task log directory.
- tmp/ cleanup (every 10 minutes)
- daemon.log rotation (on size threshold)
- SQLite VACUUM (every 24h, only when no running tasks)
"""

from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path
from typing import TYPE_CHECKING

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

if TYPE_CHECKING:
    from crawlhub.core.daemon import CrawlHubDaemon

logger = logging.getLogger("crawlhub.scheduler")


class ScheduledTaskManager:
    """Manages all periodic background tasks."""

    def __init__(self, daemon: CrawlHubDaemon):
        self.daemon = daemon
        self.config = daemon.config
        self.data_root = daemon.data_root
        self._scheduler = BackgroundScheduler(daemon=True)

    def start(self) -> None:
        """Start all scheduled jobs."""
        # 1. Archived purge (daily, configurable cron). Replaces the legacy
        #    "scan output/ by date and move to trash" job: archived state is
        #    now driven exclusively by the user clicking 'delete' (which
        #    stamps tasks.archived_at). This job only ages out the recycle
        #    bin, it never archives a task on its own.
        cron_parts = self.config.cleanup_cron.split()
        if len(cron_parts) == 5:
            trigger = CronTrigger(
                minute=cron_parts[0],
                hour=cron_parts[1],
                day=cron_parts[2],
                month=cron_parts[3],
                day_of_week=cron_parts[4],
            )
        else:
            trigger = CronTrigger(hour=3, minute=0)  # default 03:00

        self._scheduler.add_job(
            self._archived_purge,
            trigger=trigger,
            id="archived_purge",
            name="Archived Purge",
        )

        # 2. tmp/ cleanup (every 10 minutes)
        self._scheduler.add_job(
            self._tmp_cleanup,
            trigger=IntervalTrigger(minutes=10),
            id="tmp_cleanup",
            name="Tmp Cleanup",
        )

        # 3. Log rotation check (every 5 minutes)
        self._scheduler.add_job(
            self._check_log_rotation,
            trigger=IntervalTrigger(minutes=5),
            id="log_rotation",
            name="Log Rotation Check",
        )

        # 4. SQLite VACUUM (configurable interval, default 24h)
        self._scheduler.add_job(
            self._vacuum_db,
            trigger=IntervalTrigger(hours=self.config.vacuum_interval_hours),
            id="vacuum_db",
            name="SQLite VACUUM",
        )

        self._scheduler.start()
        logger.info("[OK] Scheduled tasks started")

    def stop(self) -> None:
        """Stop the scheduler."""
        self._scheduler.shutdown(wait=False)

    def trigger_archived_purge_now(self) -> dict:
        """Manually trigger the archived-purge job (e.g. on disk_low event
        or from an admin endpoint).
        """
        return self._archived_purge()

    def _archived_purge(self) -> dict:
        """Purge tasks that have been in the recycle bin > archived_purge_days.

        For each top-level archived task whose archived_at is older than the
        cutoff:
          1. Delete its output directory (recursive).
          2. Delete its per-task log directory (best-effort: structure is
             logs/tasks/<YYYY-MM-DD>/<task_id>_<task_type>/).
          3. Call store.purge_task(task_id) which cascades to children and
             removes related transitions / record_samples rows.

        This is the ONLY automated archive-related cleanup. Tasks never get
        archived by the scheduler \u2014 only by the user clicking 'delete'.
        """
        purge_days = self.config.archived_purge_days
        cutoff_ts = time.time() - (purge_days * 86400)

        try:
            archived = self.daemon.store.find_archived_older_than(cutoff_ts)
        except Exception as e:
            logger.error("[ERR] archived_purge: query failed: %s", e)
            return {"purged": 0, "freed_mb": 0.0, "errors": 1}

        purged = 0
        freed_bytes = 0
        errors = 0

        for task in archived:
            task_id = task["task_id"]
            try:
                # 1. Output directory
                out_dir = task.get("output_dir") or ""
                if out_dir:
                    out_path = Path(out_dir)
                    if out_path.exists() and out_path.is_dir():
                        try:
                            freed_bytes += _dir_size(out_path)
                            shutil.rmtree(out_path, ignore_errors=True)
                        except OSError as e:
                            logger.warning(
                                "[WARN] archived_purge: failed to remove output for %s: %s",
                                task_id, e,
                            )

                # 2. Per-task log directory: scan logs/tasks/<date>/<task_id>_*
                self._purge_task_log_dirs(task_id)

                # 3. DB rows (cascades children + transitions + samples)
                self.daemon.store.purge_task(task_id)
                purged += 1
            except Exception as e:
                errors += 1
                logger.error("[ERR] archived_purge: %s failed: %s", task_id, e)

        result = {
            "purged": purged,
            "freed_mb": round(freed_bytes / (1024 * 1024), 1),
            "errors": errors,
        }
        if purged > 0 or errors > 0:
            logger.info(
                "[OK] archived_purge: purged=%d, freed=%.1fMB, errors=%d",
                purged, result["freed_mb"], errors,
            )
        return result

    def _purge_task_log_dirs(self, task_id: str) -> None:
        """Best-effort: remove this task's per-task log directory under
        logs/tasks/<YYYY-MM-DD>/<task_id>_*.

        Logs are organized by date so we walk every date directory under
        logs/tasks/ and remove any subdirectory whose name starts with
        '<task_id>_'. Cheap because the date dirs are flat.
        """
        logs_root = self.data_root / "logs" / "tasks"
        if not logs_root.exists():
            return
        prefix = f"{task_id}_"
        for date_dir in logs_root.iterdir():
            if not date_dir.is_dir():
                continue
            for entry in date_dir.iterdir():
                if entry.is_dir() and entry.name.startswith(prefix):
                    shutil.rmtree(entry, ignore_errors=True)

    def _tmp_cleanup(self) -> None:
        """Remove files in tmp/ older than 24 hours."""
        tmp_dir = self.data_root / "tmp"
        if not tmp_dir.exists():
            return

        cutoff = time.time() - 86400
        cleaned = 0
        for item in tmp_dir.iterdir():
            try:
                if item.stat().st_mtime < cutoff:
                    if item.is_file():
                        item.unlink()
                    elif item.is_dir():
                        shutil.rmtree(item)
                    cleaned += 1
            except OSError:
                pass

        if cleaned > 0:
            logger.debug("[INFO] tmp cleanup: removed %d items", cleaned)

    def _check_log_rotation(self) -> None:
        """Rotate daemon.log if it exceeds size threshold."""
        log_path = self.data_root / "logs" / "daemon.log"
        if not log_path.exists():
            return

        max_size = self.config.log_max_size_mb * 1024 * 1024
        if log_path.stat().st_size < max_size:
            return

        # Rotate: daemon.log.2 -> .3, .1 -> .2, daemon.log -> .1
        backup_count = self.config.log_backup_count
        for i in range(backup_count, 0, -1):
            src = log_path.with_suffix(f".log.{i}") if i > 0 else log_path
            dst = log_path.with_suffix(f".log.{i + 1}")
            if i == backup_count and src.exists():
                src.unlink()  # Delete oldest
            elif src.exists():
                src.rename(dst)

        # Rename current to .1
        if log_path.exists():
            log_path.rename(log_path.with_suffix(".log.1"))

        # Create fresh log file
        log_path.touch()
        logger.info("[OK] daemon.log rotated (exceeded %dMB)", self.config.log_max_size_mb)

    def _vacuum_db(self) -> None:
        """Run SQLite VACUUM only when no running tasks."""
        running_count = self.daemon.store.count_by_status("running")
        if running_count > 0:
            logger.debug("[INFO] VACUUM deferred: %d running tasks", running_count)
            return

        try:
            self.daemon.store.vacuum()
            logger.info("[OK] SQLite VACUUM completed")
        except Exception as e:
            logger.error("[ERR] VACUUM failed: %s", e)


def _dir_size(path: Path) -> int:
    """Calculate total size of a directory."""
    total = 0
    for item in path.rglob("*"):
        if item.is_file():
            try:
                total += item.stat().st_size
            except OSError:
                pass
    return total
