"""TaskContext - the interface between platform services and the scheduler.

Each running task gets a TaskContext instance that provides methods to:
- Write crawled records (JSONL append)
- Report progress
- Log messages
- Write binary assets
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
#  R7 Observability — 是否记录 requests.jsonl
#  Single source of truth: ~/.crawlhub/config.yaml -> observability.record_requests
#  No ENV override (config is authoritative). Tests monkeypatch this function.
# ─────────────────────────────────────────────────────────────────────────────
def _record_requests_enabled() -> bool:
    """Return whether per-task `requests.jsonl` writer should be active.

    Reads `observability.record_requests` from `~/.crawlhub/config.yaml`.
    Default: False. Returns False if config can't be loaded (safe default).
    """
    try:
        from crawlhub.core.config import get_config
        return bool(get_config().observability.record_requests)
    except Exception:
        return False


class TaskContext:
    """Execution context passed to BasePlatformService.execute().

    Thread-safe: multiple threads may call write_record concurrently
    (though typically one task = one thread).
    """

    def __init__(
        self,
        task_id: str,
        output_dir: str,
        log_path: str,
        on_progress: Callable[[str, float], None] | None = None,
        on_log: Callable[[str, str], None] | None = None,
        platform: str | None = None,
        action: str | None = None,
        flux: Any | None = None,
    ):
        self.task_id = task_id
        self.output_dir = output_dir
        self.log_path = log_path
        self._on_progress = on_progress
        self._on_log = on_log
        # platform/action 缓存供 observability recorder 使用
        self._platform = platform
        self._action = action
        # Optional global flux counter — when injected (production path via
        # Daemon), every successful write_record ticks the system-wide total
        # used by the dashboard speed chart and lifetime download stat card.
        # Kept Optional so unit tests / one-off ctx instances stay simple.
        # See crawlhub/core/flux.py for the design rationale.
        self._flux = flux

        self._lock = threading.Lock()
        self._record_count = 0
        self._total_bytes = 0
        self._error_count = 0
        self._cancelled = threading.Event()
        self._started_at = time.time()

        # ── R7 observability fields（spec §3.5）──────────────────────────────
        self._requests_lock = threading.Lock()
        self._requests_path = os.path.join(output_dir, "requests.jsonl")
        self._requests_writer: Any | None = None  # lazy 启动
        self._requests_in_flight: dict[str, dict] = {}
        self._requests_count = 0
        self._requests_disabled = not _record_requests_enabled()
        # ────────────────────────────────────────────────────────────────────

        # --- output_schema validation (best-effort) ---
        # Resolve declared output_schema keys from plugin.yaml via registry.
        # If platform/action are not provided (e.g. tests), skip validation.
        self._schema_keys: frozenset[str] | None = None
        self._schema_mismatch_logged: bool = False
        if platform and action:
            try:
                from crawlhub.core.registry import get_output_schema
                schema = get_output_schema(platform, action)
                if schema:
                    self._schema_keys = frozenset(schema.keys())
            except Exception:
                pass  # registry may not be available in tests

        # Most-recent HTTP response observed by the platform service. Used
        # by the daemon when a task transitions to FAILED with 0 records
        # (silent failure) so #log-panel can show the actual response that
        # came back even though no exception was raised.
        # Stored as a snapshot dict, not the live response object, because:
        #   - response objects may be closed/GC'd by the time we read them
        #   - we want to bound memory (truncate body once, keep small)
        self._last_response_snapshot: dict[str, Any] | None = None

        # Ensure directories exist
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)

        # Open JSONL file for appending
        self._data_path = os.path.join(output_dir, "data.jsonl")
        self._data_file = open(self._data_path, "a", encoding="utf-8")

        # Open log file
        self._log_file = open(log_path, "a", encoding="utf-8")

    def write_record(self, record: dict[str, Any]) -> None:
        """Append a single record to data.jsonl (thread-safe).

        Also validates record keys against the declared output_schema (if
        platform/action were provided at construction).  Mismatch is logged
        as a warning (once per task) rather than raised, so development-time
        schema drift is noticed without breaking production runs.
        """
        if self._cancelled.is_set():
            raise TaskCancelled(self.task_id)

        # --- schema validation (best-effort, warn once) ---
        if self._schema_keys is not None and not self._schema_mismatch_logged:
            record_keys = set(record.keys())
            extra = record_keys - self._schema_keys
            missing = self._schema_keys - record_keys
            if extra or missing:
                parts = []
                if extra:
                    parts.append(f"extra keys: {sorted(extra)}")
                if missing:
                    parts.append(f"missing keys: {sorted(missing)}")
                logger.warning(
                    "[WARN] task=%s record schema mismatch: %s. "
                    "This means the crawler yields fields not declared in "
                    "plugin.yaml output_schema (or vice versa). "
                    "Fix the crawler or the schema.",
                    self.task_id, "; ".join(parts),
                )
                self._schema_mismatch_logged = True
        # --- end schema validation ---

        line = json.dumps(record, ensure_ascii=False) + "\n"
        with self._lock:
            self._data_file.write(line)
            self._data_file.flush()
            self._record_count += 1
            self._total_bytes += len(line.encode("utf-8"))

        # Tick the global flux counter OUTSIDE the per-task lock so the
        # GlobalFluxCounter's own lock never nests inside ours (avoids any
        # cross-task contention pathology). flux.tick is O(1) and
        # exception-free for sane inputs; even if it raises we don't want
        # to fail the write that already landed on disk.
        if self._flux is not None:
            try:
                self._flux.tick(records=1)
            except Exception:
                pass

    def write_asset(self, filename: str, data: bytes) -> str:
        """Write binary asset to assets/ subdirectory. Returns relative path."""
        assets_dir = os.path.join(self.output_dir, "assets")
        os.makedirs(assets_dir, exist_ok=True)
        filepath = os.path.join(assets_dir, filename)
        with open(filepath, "wb") as f:
            f.write(data)
        return f"assets/{filename}"

    def set_progress(self, progress: float) -> None:
        """Update task progress (0.0 ~ 1.0)."""
        progress = max(0.0, min(1.0, progress))
        if self._on_progress:
            self._on_progress(self.task_id, progress)

    def log(self, message: str, level: str = "INFO") -> None:
        """Write a log message to the task log file and notify listeners.

        Defensive against post-close use: in the daemon flow, ctx.close()
        runs before the final status branches in _run_task, but several
        code paths there (silent-failure dump, retry detector, etc.) still
        call ctx.log(). Writing to a closed file raises
        ``ValueError: I/O operation on closed file.`` which used to escape
        the worker thread, leaving the task stuck in RUNNING and the future
        marked failed. Instead of papering over each call site, we make
        log() a no-op (with a fallback to the daemon logger) once the file
        is closed.
        """
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        log_line = f"[{timestamp}] [{level}] {message}\n"
        try:
            with self._lock:
                if self._log_file is None or self._log_file.closed:
                    logger.warning(
                        "[task=%s] ctx.log() after close (level=%s): %s",
                        self.task_id, level, message,
                    )
                else:
                    self._log_file.write(log_line)
                    self._log_file.flush()
        except ValueError:
            # File was closed between the check and the write (race) —
            # fall back to logger rather than crashing the worker.
            logger.warning(
                "[task=%s] ctx.log() race-after-close (level=%s): %s",
                self.task_id, level, message,
            )
        if self._on_log:
            self._on_log(self.task_id, log_line)

    def record_error(self, error: str, response: Any | None = None) -> None:
        """Record an error (non-fatal, task continues).

        Args:
            error: Human-readable error description.
            response: Optional HTTP response object (requests.Response /
                httpx.Response / object exposing ``.status_code`` + ``.text``).
                When provided, the response status and a 2 KB preview of the
                body are appended to the log so #log-panel shows enough
                context to diagnose the failure even when the task continues.
        """
        self._error_count += 1
        self.log(f"Error: {error}", level="ERR")
        if response is not None:
            # Lazy import to avoid circular deps (failure_detector -> requests).
            try:
                from crawlhub.core.failure_detector import format_response_dump
                dump = format_response_dump(response)
            except Exception:
                dump = None
            if dump:
                self.log(f"[ERR] Response dump: {dump}", level="ERR")
            return

        snapshot = self._last_response_snapshot
        if not snapshot:
            return
        dump = snapshot.get("dump") or ""
        if not dump:
            return
        url = snapshot.get("url") or ""
        if url:
            self.log(f"[ERR] Last response snapshot near error: {url} -> {dump}", level="ERR")
        else:
            self.log(f"[ERR] Last response snapshot near error: {dump}", level="ERR")


    def set_last_response(self, response: Any) -> None:
        """Record the most-recent HTTP response observed by the SDK / bridge.

        Called by platform bridges after every HTTP call so that, when a
        task ends with ``record_count == 0`` and no exception was raised
        (silent failure / NATURAL_FAIL), the daemon can still surface the
        actual response that was received.

        We snapshot status + truncated body immediately rather than holding
        the response object — connection objects may be released by the SDK
        before we want to read them, and a snapshot is cheap and bounded.

        Args:
            response: requests.Response / httpx.Response / any object with
                ``.status_code`` + ``.text``. ``None`` clears the snapshot.
        """
        if response is None:
            self._last_response_snapshot = None
            return
        try:
            from crawlhub.core.failure_detector import format_response_dump
            dump = format_response_dump(response)
        except Exception:
            dump = None
        if not dump:
            return
        # Also keep raw status separately for daemon convenience.
        try:
            status = int(getattr(response, "status_code", 0)) or None
        except Exception:
            status = None
        try:
            url = str(getattr(response, "url", "")) or None
        except Exception:
            url = None
        self._last_response_snapshot = {
            "status": status,
            "url": url,
            "dump": dump,  # "HTTP <status> | body=<truncated>"
        }

    def get_last_response_snapshot(self) -> dict[str, Any] | None:
        """Return the most-recent response snapshot or None."""
        return self._last_response_snapshot

    # ─────────────────────────────────────────────────────────────────────────
    #  R7 Observability: requests.jsonl writer (spec §3.5)
    # ─────────────────────────────────────────────────────────────────────────

    def record_request(self, record: dict[str, Any]) -> None:
        """Append a request/response/ws frame record (thread-safe, async-safe).

        Best-effort: any failure is logged once then disabled for this task
        (silent best-effort — observability layer must never break business).

        Called from:
          - http_patches.py recorders (urllib3 / httpx / websockets)
          - cdp_recorder.py (Phase 2)
          - business code via observe_request() context manager
        """
        if self._requests_disabled:
            return
        if self._requests_writer is None:
            with self._requests_lock:
                if self._requests_writer is None:
                    try:
                        # Lazy import 避免循环依赖（observability 不依赖 task_context）
                        from crawlhub.core.observability.writer import _RequestsWriter
                        self._requests_writer = _RequestsWriter(self._requests_path)
                    except Exception as exc:
                        logger.warning(
                            "[task=%s] obs writer init failed, disabling: %s",
                            self.task_id, exc,
                        )
                        self._requests_disabled = True
                        return
        try:
            self._requests_writer.put(record)
            self._requests_count += 1
        except Exception as exc:
            if not self._requests_disabled:
                logger.warning(
                    "[task=%s] requests.jsonl write failed, disabling: %s",
                    self.task_id, exc,
                )
                self._requests_disabled = True

    @contextmanager
    def observe_request(self, ref_id: str, request_meta: dict) -> Iterator[None]:
        """Track an in-flight request (for legacy code that wants explicit pairing).

        Usage:
            with ctx.observe_request("rq_abc", {"url": ..., "method": "GET"}):
                resp = httpx.get(...)
        """
        with self._requests_lock:
            self._requests_in_flight[ref_id] = request_meta
        try:
            yield
        finally:
            with self._requests_lock:
                self._requests_in_flight.pop(ref_id, None)

    def get_in_flight_count(self) -> int:
        with self._requests_lock:
            return len(self._requests_in_flight)

    @property
    def requests_count(self) -> int:
        return self._requests_count

    def check_cancelled(self) -> None:
        """Raise TaskCancelled if the task has been cancelled."""
        if self._cancelled.is_set():
            raise TaskCancelled(self.task_id)

    def cancel(self) -> None:
        """Signal cancellation to the running task."""
        self._cancelled.set()

    def sleep(self, seconds: float) -> None:
        """Cancel-aware sleep.

        Returns early if the task is cancelled. After waking (whether by
        timeout or cancel signal), raises TaskCancelled if the task was
        cancelled.

        Use this everywhere a worker would otherwise call time.sleep() —
        long retry backoffs, polite-rate-limit pauses, anything that
        could keep a cancelled task running for tens of seconds. Without
        this API, cancellation latency = remaining sleep duration.
        """
        if seconds <= 0:
            self.check_cancelled()
            return
        # Event.wait returns True if the event was set, False on timeout.
        # We don't actually care which — we just check is_set after.
        self._cancelled.wait(timeout=seconds)
        if self._cancelled.is_set():
            raise TaskCancelled(self.task_id)

    @property
    def is_cancelled(self) -> bool:
        return self._cancelled.is_set()

    @property
    def record_count(self) -> int:
        return self._record_count

    @property
    def total_bytes(self) -> int:
        return self._total_bytes

    @property
    def error_count(self) -> int:
        return self._error_count

    @property
    def duration(self) -> float:
        return time.time() - self._started_at

    def close(self) -> None:
        """Close file handles. Called by scheduler after task completes."""
        with self._lock:
            if self._data_file and not self._data_file.closed:
                self._data_file.close()
            if self._log_file and not self._log_file.closed:
                self._log_file.close()
        # ── 关闭 requests.jsonl writer（best-effort，不抛）─────────────────
        if self._requests_writer is not None:
            try:
                self._requests_writer.close(timeout=3.0)
            except Exception:
                logger.exception("[task=%s] obs writer close failed", self.task_id)

    def generate_summary(self, task_input: dict[str, Any]) -> dict[str, Any]:
        """Generate summary.json content for the completed task."""
        file_list = []
        data_path = Path(self._data_path)
        if data_path.exists():
            file_list.append({
                "path": "data.jsonl",
                "size": data_path.stat().st_size,
                "rows": self._record_count,
            })

        assets_dir = Path(self.output_dir) / "assets"
        if assets_dir.exists():
            for f in assets_dir.iterdir():
                if f.is_file():
                    file_list.append({
                        "path": f"assets/{f.name}",
                        "size": f.stat().st_size,
                        "rows": None,
                    })

        return {
            "task_id": self.task_id,
            "snapshot_param": task_input,
            "record_count": self._record_count,
            "total_bytes": self._total_bytes,
            "duration_seconds": round(self.duration, 2),
            "error_count": self._error_count,
            "file_list": file_list,
        }


class TaskCancelled(Exception):
    """Raised when a task detects it has been cancelled."""

    def __init__(self, task_id: str):
        self.task_id = task_id
        super().__init__(f"Task {task_id} was cancelled")
