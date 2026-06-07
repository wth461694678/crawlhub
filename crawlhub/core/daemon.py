"""CrawlHub Daemon - the core long-running server.

Responsibilities:
- FastAPI app serving REST API + WebSocket
- Task scheduler with per-platform ThreadPoolExecutor
- PID file management + stale lock detection
- Graceful shutdown (3-phase)
- Startup self-check (exit_marker.json analysis)
- Disk space monitoring
"""

from __future__ import annotations

import io
import json
import logging
import os
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, Future
from pathlib import Path
from typing import Any, Callable

import uvicorn

from crawlhub.core.batch import BatchConfig, BatchOrchestrator, resolve_items, check_circular_dependency
from crawlhub.core.blob_store import LocalBlobStore
from crawlhub.core.flux import GlobalFluxCounter


from crawlhub.core.config import CrawlHubConfig, ensure_directories, get_data_root, load_config
from crawlhub.core.cookie_dispatcher import CookieStatus, get_cookie_throttle
from crawlhub.core.cookies import load_cookie
from crawlhub.core.failure_detector import FailureMode, detect_failure, format_response_dump
from crawlhub.core.models import Task, TaskStatus
from crawlhub.core.param_snapshot import build_task_snapshot
from crawlhub.core.plugin_manifest import ActionDef, BrowserConfig
from crawlhub.core.registry import discover_platforms, get_registry, get_action_meta

from crawlhub.core.sqlite_store import SqliteStateStore
from crawlhub.core.state_machine import (
    Action,
    IllegalTransitionError,
    aggregate_with_lock,
    can_transition,
    transition_task,
)
from crawlhub.core.task_context import TaskCancelled, TaskContext
from crawlhub.core.cookie_override import (
    set_thread_cookie_override,
    clear_thread_cookie_override,
)

logger = logging.getLogger("crawlhub.daemon")


# R7: _NoopBrowserPage 已删除（spec §8.7）。
# 无 cookie 的 BBA 任务必须 fail-fast：daemon factory 直接抛
# RuntimeError("BBA action requires a valid cookie")，不再有 noop 兜底。





class TaskExecutionPlan:
    """Runtime route selected from manifest action metadata."""


    def __init__(self, action_meta: ActionDef | None) -> None:
        self.action_meta = action_meta
        self.runtime = action_meta.runtime if action_meta else "stateless"
        self.throttle_scope = action_meta.throttle_scope if action_meta else "task"
        self.transport = action_meta.transport if action_meta else "http"
        self.browser = action_meta.browser if action_meta else BrowserConfig()

    @property
    def task_level_throttle(self) -> bool:
        return self.runtime == "stateless" and self.throttle_scope == "task"

    @property
    def request_level_throttle(self) -> bool:
        return self.throttle_scope == "request"

    @property
    def browser_backed(self) -> bool:
        return self.runtime == "browser_backed"


# Global daemon state (singleton per process)

_daemon: CrawlHubDaemon | None = None


def get_daemon() -> CrawlHubDaemon:
    """Get the running daemon instance."""
    if _daemon is None:
        raise RuntimeError("Daemon not initialized")
    return _daemon


def _format_silent_failure_dump(ctx) -> str | None:
    """Build a one-line "last response" string for silent NATURAL_FAIL paths.

    Silent failures = task ends with record_count==0 but no exception was
    raised by the bridge. The bridge captures every HTTP response into
    ``ctx.set_last_response(...)``; if it did, return a compact dump:

        "last_response: GET https://... -> HTTP 200 | body=<truncated>"

    Returns None if no snapshot is available (bridge hasn't been wired yet
    for that platform — degrades gracefully).
    """
    try:
        snapshot = ctx.get_last_response_snapshot()
    except Exception:
        return None
    if not snapshot:
        return None
    url = snapshot.get("url") or ""
    dump = snapshot.get("dump") or ""
    if not dump:
        return None
    if url:
        return f"last_response: {url} -> {dump}"
    return f"last_response: {dump}"


def _log_zero_record_response_dump(ctx, *, level: str, message: str) -> str | None:
    dump = _format_silent_failure_dump(ctx)
    if not dump:
        return None
    ctx.log(f"{message}: {dump}", level=level)
    return dump


# ════════════════════════════════════════════════════════════════════
#  R7 P5：Origin metadata callback factory
# ────────────────────────────────────────────────────────────────────
#  把 BBA 实抓的 wire 身份头持久化到 cookie_jar.metadata 的 callback。
#
#  设计要点：
#    - 平台路由：当前只快手接入；未接入平台返回 None（关闭 capture）
#    - 同步执行：listener 在 patchright 事件循环内调用 callback，
#      所以这里只能调同步 IO（jar.update + jar.save）
#    - 平台 jar 类局部 import，避免顶部 import 链拖累 daemon 启动
#    - 异常吞回 logger.warning：BBA 是主流程，metadata 持久化失败
#      不应让浏览器创建失败
# ════════════════════════════════════════════════════════════════════
def _make_origin_metadata_callback(
    session_key: Any,
) -> Callable[[dict[str, str]], None] | None:
    """Build the on_origin_headers_captured callback for a session_key.

    Returns ``None`` 时 playwright_runtime 不挂 listener、零开销。
    """
    platform = getattr(session_key, "platform", "")
    cookie_path = getattr(session_key, "cookie_path", "")
    if not cookie_path:
        return None

    if platform == "kuaishou":
        # 局部 import 避免循环依赖（kuaishou pkg → core → daemon）
        from crawlhub.crawlers.kuaishou.crawler._internal.cookie_jar import (
            KuaishouCookieJar, ORIGIN_SOURCE_CAPTURED,
        )
        jar = KuaishouCookieJar(cookie_path)

        def _persist(headers: dict[str, str]) -> None:
            try:
                jar.update_origin_headers(headers, source=ORIGIN_SOURCE_CAPTURED)
                jar.save()
                logger.info(
                    "[origin] persisted to %s (ua=%s platform=%s)",
                    jar.path,
                    headers.get("user-agent", "")[:60],
                    headers.get("sec-ch-ua-platform", ""),
                )
            except Exception as exc:
                # daemon-level warning：metadata 写盘失败不阻塞 BBA，
                # 但必须可见——下次 cffi 出网会因 strict 模式 raise，
                # 那时再看这条日志定位
                logger.warning(
                    "[origin] persist failed for %s: %s", jar.path, exc,
                )

        return _persist

    # douyin / 其他 platform 暂未接入
    return None


def _RETRY_RESET_UPDATES() -> dict:

    """Field reset payload applied by every FULL_RETRY transition.

    Centralised because three call sites need identical semantics:
      * Daemon.retry_task (single-task retry)
      * _fanout_failed_retry (batch parent → failed children)
      * _fanout_full_retry  (batch parent → all terminal children)

    Why every field is here:
      * `error / started_at / finished_at` — without started_at reset,
        formatDuration uses the stale value and shows "-52677s" once a
        new last_heartbeat lands. (Bug seen on f8c3521465c3.)
      * `record_count / total_bytes` — task list aggregates these
        directly; if not zeroed, the parent shows the previous run's
        totals during the new run.
      * `progress` — UI progress bar would otherwise jump from 0 → 100
        instantly when the previous run's value is read.
      * `last_heartbeat` — old heartbeat + new started_at = phantom
        "running but quiet" appearance. Cleared so the next real
        heartbeat sets it fresh.
      * `result_files` — drawer-list shows files from the previous run
        until the new run's terminal hook overwrites it; reset so the
        UI never shows stale files mid-retry.
    """
    return {
        "error": None,
        "started_at": None,
        "finished_at": None,
        "record_count": 0,
        "total_bytes": 0,
        "progress": 0.0,
        "last_heartbeat": None,
        "result_files": [],
    }


class CrawlHubDaemon:
    """The core daemon process managing all crawl tasks."""

    def __init__(self, config: CrawlHubConfig):
        self.config = config
        self.data_root = get_data_root()

        # Storage
        self.store = SqliteStateStore(self.data_root / "crawlhub.db")
        self.blob_store = LocalBlobStore(self.data_root)

        # Global flux counter — system-wide downloaded record total. Decoupled
        # from `tasks.record_count` (which is per-run progress and resets on
        # retry). See crawlhub/core/flux.py for design rationale.
        # Instantiated in initialize() AFTER store.initialize() because the
        # constructor reads from `global_flux_counter`, a table that doesn't
        # exist until schema migration runs.
        self.flux: GlobalFluxCounter | None = None

        # Per-platform executors
        self._executors: dict[str, ThreadPoolExecutor] = {}
        self._futures: dict[str, Future] = {}  # task_id -> Future
        self._contexts: dict[str, TaskContext] = {}  # task_id -> TaskContext

        # Shutdown coordination
        self._shutdown_flag = threading.Event()
        self._shutdown_lock = threading.Lock()

        # Event bus (simple callback list)
        self._event_listeners: list[Any] = []

        # Notification service (initialized in start_daemon)
        self.notification_service = None

        # Plan scheduler (initialized + started in start_daemon, after
        # notification_service so fire-failure events have a sink).
        self.plan_scheduler = None

        # Batch orchestrator (initialized in initialize())
        self.batch_orchestrator: BatchOrchestrator | None = None

        # ══════════════════════════════════════════════════════════════════
        #  Browser-backed action runtime is lazy so classic crawls pay zero cost.
        # ──────────────────────────────────────────────────────────────────
        #  ⚠️ Thread-safety contract（2026-06-01 修复 batch 并发 SingletonLock 冲突）
        #  这两个字段被多个 worker thread 通过 _get_browser_runner /
        #  _get_browser_session_manager 并发懒加载。GIL 只保护单条字节码，
        #  "if x is None: x = T()" 是 TOCTOU 模式——三个 worker 同时进入会
        #  各自创建独立实例，导致 BrowserSessionManager 的 singleflight
        #  机制被绕过（每个 manager 各管各的 _sessions/_creating），最终多个
        #  线程并发 launch_persistent_context 到同一个 user_data_dir，
        #  Chromium SingletonLock 拒绝后两个，TargetClosedError。
        #  修复：双重检查锁（DCL）保护初始化路径，命中走无锁快路径。
        # ══════════════════════════════════════════════════════════════════
        self._browser_runner = None
        self._browser_runner_lock = threading.Lock()
        self._browser_managers: dict[tuple[int, int, int, int], Any] = {}
        self._browser_managers_lock = threading.Lock()

        # Throughput sampler thread (started in initialize, stopped in shutdown).

        # Periodically snapshots ctx.record_count for every active task into
        # record_samples. This is the ONLY reliable speed source — relying on
        # _on_task_log throttle gives 0 samples for tasks that don't log.
        self._record_sampler_thread: threading.Thread | None = None

        # Global throughput accounting.
        # Why we maintain this separately from per-task samples:
        #   Dashboard "下载速度" wants ONE smooth curve over time. Building it from
        #   per-task samples is unreliable for short-lived child tasks (a 1.2s
        #   child between two 5s sampler ticks contributes 0 mid-run points →
        #   the chart shows everything dumped at the terminal sample = "spike at
        #   the end" bug). The fix: also persist a single GLOBAL sample each tick
        #   whose value = SUM(record_count) over all non-archived atomic tasks +
        #   the live record_count of currently active contexts (which haven't
        #   yet flushed to the DB column).
        #   Recycle-bin semantics: archived tasks are excluded from the global
        #   total so deleting a task makes its records stop counting from the
        #   next tick onward (no negative jumps — the curve is just rate, and
        #   rate of an excluded task is 0). Restoring brings them back the same
        #   way. No baseline / no session-delta: the SQL aggregation is the
        #   single source of truth and is fully idempotent under archive churn.
        self._global_records_baseline: int = 0  # legacy field, unused (kept for
                                                #  test/back-compat introspection)

        # Startup time
        self._started_at = time.time()

    def initialize(self) -> None:
        """Initialize daemon: directories, DB, platform discovery, cookie migration."""
        logger.info("[INIT] Starting daemon initialization...")
        ensure_directories()
        self.store.initialize()
        logger.info("[INIT] DB initialized, discovering platforms...")

        # Flux counter must be built AFTER schema migration — its constructor
        # reads `global_flux_counter` to recover the lifetime total across
        # daemon restarts.
        self.flux = GlobalFluxCounter(self.store)

        discover_platforms()

        # Migrate legacy single-file cookies to new multi-account structure
        from crawlhub.core.cookies import get_cookie_store
        cookie_store = get_cookie_store()
        migrated = cookie_store.migrate_legacy()
        if migrated > 0:
            logger.info("[INFO] Migrated %d legacy cookie files to multi-account structure", migrated)

        # Migrate any remaining storage_state format cookies to native format
        fmt_migrated = cookie_store.migrate_cookie_formats()
        if fmt_migrated > 0:
            logger.info("[INFO] Converted %d cookie files from storage_state to native format", fmt_migrated)

        # Create per-platform executors
        registry = get_registry()
        logger.info("[INIT] Registered platforms: %s", list(registry.keys()))
        for platform_name in registry:
            max_workers = self.config.get_concurrency(platform_name)
            self._executors[platform_name] = ThreadPoolExecutor(
                max_workers=max_workers,
                thread_name_prefix=f"crawl-{platform_name}",
            )
            logger.info("[INIT] Executor created: platform=%s, max_workers=%d", platform_name, max_workers)

        # Also create a default executor for unknown platforms
        default_workers = self.config.get_concurrency("default")
        self._executors["_default"] = ThreadPoolExecutor(
            max_workers=default_workers,
            thread_name_prefix="crawl-default",
        )
        logger.info("[INIT] Default executor created: max_workers=%d", default_workers)

        # Initialize Batch Orchestrator
        self.batch_orchestrator = BatchOrchestrator(
            store=self.store,
            blob_store=self.blob_store,
            run_child_fn=self._run_batch_child,
            data_root=self.data_root,
        )
        logger.info("[INIT] Daemon initialization complete.")

        # Start throughput sampler thread (5s period). Daemon thread so it
        # dies with the process if shutdown hangs; explicit join in
        # graceful_shutdown handles the clean case.
        # No baseline seeding needed: the global sample now reads
        # SUM(record_count) WHERE archived_at IS NULL straight from the DB
        # each tick, which is naturally consistent across daemon restarts and
        # archive/restore operations.

        self._record_sampler_thread = threading.Thread(
            target=self._record_sampler_loop,
            name="record-sampler",
            daemon=True,
        )
        self._record_sampler_thread.start()
        logger.info("[INIT] Record sampler thread started (period=5s).")



    def startup_recovery(self) -> None:
        """Recover from previous run: mark interrupted tasks, check exit status."""
        logger.info("[RECOVERY] Starting startup recovery...")
        # Mark all queued/running tasks as interrupted
        count = self.store.bulk_update_status(
            from_statuses=[TaskStatus.QUEUED.value, TaskStatus.RUNNING.value],
            to_status="interrupted",
            error="daemon restart recovery",
        )
        if count > 0:
            logger.info("[RECOVERY] Marked %d tasks as interrupted", count)
        else:
            logger.info("[RECOVERY] No stale tasks to recover.")

        # B3: honor cancellation_intent on parents.
        # Spec §B3 — for every freshly-interrupted child whose parent had
        # cancellation_intent=true (user clicked Cancel on the batch before
        # the daemon went down), the user clearly does NOT want us to resume
        # them. Convert those interrupted children to cancelled directly.
        # (Atomic-only — phase=pre_expansion parents are dealt with by
        # B2's two-phase create logic; B3 covers the post_expansion case.)
        cancelled_via_intent = self._honor_cancellation_intent_on_recovery()
        if cancelled_via_intent > 0:
            logger.info(
                "[RECOVERY] Converted %d interrupted children to cancelled "
                "(parent had cancellation_intent=true)",
                cancelled_via_intent,
            )

        # Recover interrupted batch tasks
        if self.batch_orchestrator:
            recovered = self.batch_orchestrator.recover_interrupted()
            logger.info("[RECOVERY] Batch orchestrator recovered %d parent tasks", len(recovered))
            for parent_id in recovered:
                # Re-execute recovered batch tasks in background
                logger.info("[RECOVERY] Re-submitting batch parent %s to executor", parent_id)
                executor = self._executors.get("_default")
                executor.submit(self._execute_batch_task, parent_id)

        # Recover waiting_dependency tasks: re-check upstream status
        self._recover_waiting_tasks()

        # Check previous exit status
        self._check_previous_exit()

    def _honor_cancellation_intent_on_recovery(self) -> int:
        """B3: convert interrupted children to cancelled when parent has
        cancellation_intent=true. Returns count converted.

        Why: user pressed Cancel on a batch parent right before the daemon
        crashed. The cancel had set cancellation_intent=true on the parent
        and started cascading CANCEL actions to children, but the daemon
        died mid-cascade — leaving some children in `running` status. After
        the bulk recovery flip them to `interrupted`; we now finish what the
        user asked for.

        Implementation: SELECT children WHERE status='interrupted' AND
        parent_id IN (SELECT task_id WHERE cancellation_intent=1). For each,
        apply state-machine CANCEL action (interrupted -> cancelled is
        legal per ALLOWED_TRANSITIONS).
        """
        # Find interrupted children whose parent had cancellation_intent=true.
        # We rely on list_tasks(parent_id=...) per parent rather than a single
        # JOIN query — the parent set is small (only those with intent set).
        all_interrupted = self.store.list_tasks(status=TaskStatus.INTERRUPTED.value, limit=100000)
        if not all_interrupted:
            return 0

        # Cache parent lookups so we don't hit the DB once per child.
        parent_intents: dict[str, bool] = {}
        converted = 0
        for child in all_interrupted:
            parent_id = child.get("parent_task_id")
            if not parent_id:
                continue  # top-level interrupted task — not our concern here
            if parent_id not in parent_intents:
                parent = self.store.get_task(parent_id)
                parent_intents[parent_id] = bool(parent and parent.get("cancellation_intent"))
            if not parent_intents[parent_id]:
                continue

            # Convert via state machine. (interrupted, CANCEL) is legal.
            result = self._apply_atomic_action(
                child["task_id"],
                action=Action.CANCEL,
                actor="system_recover",
                reason="Parent cancellation_intent honored on recovery",
                extra_updates={
                    "finished_at": time.time(),
                    "error": "Cancelled on recovery (parent intent)",
                },
            )
            if result is not None:
                converted += 1
        return converted

    def _check_previous_exit(self) -> None:
        """Analyze exit_marker.json and daemon.pid to detect abnormal exits."""
        exit_marker_path = self.data_root / "exit_marker.json"
        pid_path = self.data_root / "daemon.pid"

        had_clean_exit = False
        if exit_marker_path.exists():
            try:
                with open(exit_marker_path, "r") as f:
                    marker = json.load(f)
                had_clean_exit = marker.get("clean", False)
            except (json.JSONDecodeError, OSError):
                pass
            # Remove old marker
            exit_marker_path.unlink(missing_ok=True)

        if not had_clean_exit and pid_path.exists():
            # Previous daemon didn't exit cleanly
            self._emit_event("on_daemon_unexpected_exit", {
                "reason": "missing_exit_marker" if not exit_marker_path.exists() else "stale_pid_only",
                "severity": "ERR" if not had_clean_exit else "WARN",
            })

    def submit_task(
        self,
        platform: str,
        task_type: str,
        logic_param: dict,
        depends_on_task_ids: list[str] | None = None,
        *,
        origin_type: str | None = None,
        origin_plan_id: str | None = None,
    ) -> Task:
        """Submit a new task for execution.

        Args:
            platform: Platform name
            task_type: Action/task type
            logic_param: Task logic parameters (the user's request body, kept
                verbatim). Used to derive the executable snapshot_param.
            depends_on_task_ids: Optional list of upstream task IDs to wait for.
                The task will only start when ALL of them are ready.
            origin_type: Where this task came from. NULL/None = legacy / API call.
                ``'plan'`` = fired by a scheduling plan trigger.
                ``'plan_manual'`` = manual run of a scheduling plan.
            origin_plan_id: Linked plan_id when origin_type is plan-related.
                Stays as a soft reference: set to NULL on plan delete.
        """
        deps = list(depends_on_task_ids or [])
        logger.info("[SUBMIT] submit_task called: platform=%s, task_type=%s, depends_on=%s", platform, task_type, deps)
        if self._shutdown_flag.is_set():
            logger.warning("[SUBMIT] Rejected - daemon is shutting down")
            raise DaemonShuttingDown()

        # Check disk space
        free_bytes = self.blob_store.disk_free_bytes()
        if free_bytes < self.config.disk_low_threshold_mb * 1024 * 1024:
            self._emit_event("on_disk_low", {"free_mb": free_bytes // (1024 * 1024)})
            raise DiskSpaceLow(free_bytes)

        # Check cookie validity
        cookie_data = load_cookie(platform)
        # Steam doesn't require cookies, so only block if platform needs them
        registry = get_registry()
        if platform in registry:
            from crawlhub.core.registry import create_platform_service
            # Instantiate to check cookie (lightweight, manifest injected)
            svc = create_platform_service(platform)
            cookie_status = svc.check_cookie()
            if cookie_status.status == "expired":
                self._emit_event("on_cookie_invalid", {"platform": platform})

        # Create task. logic_param keeps the user's POSTed body verbatim;
        # snapshot_param is the executable view (defaults filled, time
        # templates rendered) used by the worker and by retry.
        task = Task(
            platform=platform,
            task_type=task_type,
            logic_param=dict(logic_param or {}),
            snapshot_param=build_task_snapshot(logic_param or {}),
        )
        # Create output directory
        task_name = f"{platform}_{task_type}"
        task.output_dir = self.blob_store.get_output_dir(task.task_id, task_name)

        # Handle dependency if specified
        if deps:
            from crawlhub.core.batch import check_circular_dependency, check_upstreams_and_decide

            # Validate every upstream exists
            for up_id in deps:
                upstream = self.store.get_task(up_id)
                if upstream is None:
                    raise ValueError(f"UPSTREAM_NOT_FOUND: Source task not found: {up_id}")

            # Check circular dependency over the whole upstream set
            cycle_check = check_circular_dependency(task.task_id, deps, self.store)
            if cycle_check:
                raise ValueError(f"{cycle_check}: Circular dependency detected")

            # Decide ready/wait/error
            allow_partial = bool(logic_param.get("allow_partial_upstream", True))
            decision = check_upstreams_and_decide(deps, allow_partial, self.store)

            if decision["action"] == "error":
                # Single tasks can't recover from a failed upstream; surface immediately.
                raise ValueError(f"{decision['code']}: {decision['message']}")

            task_dict = task.to_dict()
            task_dict["depends_on_task_ids"] = list(deps)
            task_dict["origin_type"] = origin_type
            task_dict["origin_plan_id"] = origin_plan_id

            if decision["action"] == "ready":
                self.store.create_task(task_dict)
                self._execute_task(task)
            else:  # wait
                task_dict["status"] = "waiting_dependency"
                task_dict["waiting_reason"] = decision["waiting_reason"]
                self.store.create_task(task_dict)
                logger.info(
                    "[dep] Task %s waiting on upstreams %s (%s)",
                    task.task_id, deps, decision["waiting_reason"],
                )

            return task

        # No dependency - standard flow
        task_dict = task.to_dict()
        task_dict["origin_type"] = origin_type
        task_dict["origin_plan_id"] = origin_plan_id
        self.store.create_task(task_dict)
        logger.info("[SUBMIT] Task %s created in DB, calling _execute_task...", task.task_id)
        self._execute_task(task)
        logger.info("[SUBMIT] Task %s submitted to executor successfully", task.task_id)

        return task

    def _execute_task(self, task: Task) -> None:
        """Submit task to the appropriate platform executor."""
        executor_key = task.platform if task.platform in self._executors else "_default"
        executor = self._executors[executor_key]
        logger.info("[EXEC] _execute_task: task_id=%s, platform=%s, executor=%s, executor_threads=%d/%d",
                    task.task_id, task.platform, executor_key,
                    executor._work_queue.qsize() if hasattr(executor, '_work_queue') else -1,
                    executor._max_workers)

        future = executor.submit(self._run_task, task)
        self._futures[task.task_id] = future
        logger.info("[EXEC] Future submitted for task %s, future=%s, done=%s", task.task_id, id(future), future.done())

        # Add a callback to log when the future completes or fails
        def _future_done_callback(f, tid=task.task_id):
            exc = f.exception()
            if exc:
                # Log full traceback so silent post-execute failures
                # (e.g. ValueError: I/O operation on closed file. when
                # record_count==0) can be located precisely instead of
                # only seeing the bare "<Type>: <msg>" line.
                import traceback as _tb
                tb_str = "".join(_tb.format_exception(type(exc), exc, exc.__traceback__))
                logger.error(
                    "[EXEC] Future for task %s raised exception: %s: %s\n%s",
                    tid, type(exc).__name__, exc, tb_str,
                )
            else:
                logger.info("[EXEC] Future for task %s completed normally", tid)
        future.add_done_callback(_future_done_callback)

        # Update status to running via state machine.
        # Spec: queued -> running uses SCHEDULER_DISPATCH action.
        self._apply_atomic_action(
            task.task_id,
            action=Action.SCHEDULER_DISPATCH,
            actor="scheduler",
            reason=None,
            extra_updates={"started_at": time.time()},
        )
        logger.info("[EXEC] Task %s status set to RUNNING", task.task_id)

    # --- Batch Task Methods ---

    def submit_batch(
        self,
        config: BatchConfig,
        items_from: dict | None = None,
        depends_on_task_ids: list[str] | None = None,
        *,
        origin_type: str | None = None,
        origin_plan_id: str | None = None,
    ) -> tuple[Task, int]:
        """Submit a batch task for execution.

        Args:
            config: BatchConfig with platform, action, items, etc.
            items_from: Optional items_from dict for SQL pipeline / file source.
            depends_on_task_ids: Optional explicit dependency list (merged with
                run_ids extracted from items_from.sources).
            origin_type / origin_plan_id: Stamp the parent task with its origin.
                Children are looked up via parent_task_id; only the parent
                carries the origin tag.

        Returns:
            (parent_task, child_count) tuple
        """
        if self._shutdown_flag.is_set():
            raise DaemonShuttingDown()

        if not self.batch_orchestrator:
            raise RuntimeError("Batch orchestrator not initialized")

        # Create parent + children (may return waiting task with 0 children)
        parent, children = self.batch_orchestrator.create_batch(
            config,
            items_from=items_from,
            depends_on_task_ids=depends_on_task_ids,
            origin_type=origin_type,
            origin_plan_id=origin_plan_id,
        )

        # If task is waiting for dependency, don't execute yet
        task_dict = self.store.get_task(parent.task_id)
        if task_dict and task_dict.get("status") == "waiting_dependency":
            logger.info(
                "[batch] Task %s waiting for upstreams %s",
                parent.task_id, task_dict.get("depends_on_task_ids"),
            )
            return parent, 0

        # Execute batch in background thread
        executor = self._executors.get(config.platform, self._executors["_default"])
        future = executor.submit(self._execute_batch_task, parent.task_id)
        self._futures[parent.task_id] = future

        return parent, len(children)

    def _execute_batch_task(self, parent_task_id: str) -> None:
        """Execute a batch task (called in worker thread)."""
        logger.info("[BATCH] _execute_batch_task ENTERED: parent_id=%s, thread=%s", parent_task_id, threading.current_thread().name)
        try:
            self.batch_orchestrator.execute_batch(parent_task_id)

            # Emit completion event for parent
            parent = self.store.get_task(parent_task_id)
            if parent:
                old_status = "running"
                new_status = parent["status"]
                if new_status == TaskStatus.SUCCEEDED.value:
                    self._emit_event("on_task_completed", {
                        "task_id": parent_task_id,
                        "platform": parent["platform"],
                    })
                elif new_status == TaskStatus.FAILED.value:
                    self._emit_event("on_task_failed", {
                        "task_id": parent_task_id,
                        "platform": parent["platform"],
                        "error": parent.get("error", ""),
                    })
                # Trigger downstream dependency check
                self._on_task_status_changed(parent_task_id, old_status, new_status)
        except Exception as e:
            logger.error("[batch] Batch execution failed: %s", e)
            # If the batch-level exception carries an HTTP response (rare but
            # possible when batch orchestration itself does HTTP calls), dump it.
            _resp_dump = format_response_dump(e)
            error_msg = f"Batch execution error: {e}"
            if _resp_dump:
                error_msg = f"{error_msg} | {_resp_dump}"
            # Try state-machine path first (parent might be RUNNING already).
            applied = self._apply_atomic_action(
                parent_task_id,
                action=Action.NATURAL_FAIL,
                actor="system",
                reason=error_msg,
                extra_updates={
                    "finished_at": time.time(),
                    "error": error_msg,
                },
            )
            if applied is None:
                # Parent wasn't in RUNNING — fall back to a raw update so we
                # don't leave a half-cooked batch in an undefined state.
                # B2 will replace this with a phase-aware transition once
                # two-phase create lands.
                self.store.update_task(parent_task_id, {
                    "status": TaskStatus.FAILED.value,
                    "finished_at": time.time(),
                    "error": error_msg,
                })
                self._on_task_status_changed(parent_task_id, "running", TaskStatus.FAILED.value)
            self._emit_event("on_task_failed", {
                "task_id": parent_task_id,
                "platform": self.store.get_task(parent_task_id).get("platform", ""),
                "error": error_msg,
            })
        finally:
            self._futures.pop(parent_task_id, None)
            # --- Telemetry: task.completed for batch parent ----------------
            # We pull final status + aggregated record_count from the DB.
            # batch.py already aggregates child record_counts onto the parent
            # row before we get here. Per-child events are NOT emitted for
            # parent-driven runs (see _run_batch_child + _telemetry_ctx);
            # this single row is the rollup.
            try:
                from crawlhub.core.telemetry import emit_task_completed

                _final = self.store.get_task(parent_task_id) or {}
                _final_status = _final.get("status") or "unknown"
                _record_count = int(_final.get("record_count") or 0)
                _started = _final.get("started_at")
                _finished = _final.get("finished_at") or time.time()
                _duration_ms = (
                    int((_finished - _started) * 1000) if _started else 0
                )
                emit_task_completed(
                    task_id=parent_task_id,
                    platform=_final.get("platform", "") or "",
                    task_type=_final.get("task_type", "") or "",
                    final_status=_final_status,
                    record_count=_record_count,
                    duration_ms=_duration_ms,
                    parent_task_id=None,  # parent itself
                )
            except Exception:
                pass
            logger.info("[BATCH] _execute_batch_task EXITED: parent_id=%s", parent_task_id)

    def _run_batch_child(self, task: Task) -> None:
        """Execute a single batch child task (blocking).

        This is the run_child_fn passed to BatchOrchestrator.
        It reuses the existing _run_task logic.

        Note: BatchOrchestrator.run_one_child does NOT pre-flip child status to
        RUNNING (unlike single-task path where _execute_task does it). So we
        flip it here, mirroring _execute_task, to keep status/started_at
        semantics consistent across both paths.

        CRITICAL: If the SCHEDULER_DISPATCH transition is rejected (e.g. the
        task has been cancelled while sitting in the queue), we MUST abort
        immediately. Otherwise the worker would still call svc.execute() and
        burn HTTP requests against a cancelled task.
        """
        from crawlhub.core._telemetry_ctx import PARENT_DRIVEN_RUN

        applied = self._apply_atomic_action(
            task.task_id,
            action=Action.SCHEDULER_DISPATCH,
            actor="scheduler",
            reason=None,
            extra_updates={"started_at": time.time()},
        )
        if applied is None:
            # Transition refused (task is already cancelled/failed/succeeded).
            # Do not run the underlying service.
            logger.info(
                "[batch] Skipping cancelled/terminated child task %s (status=%s)",
                task.task_id,
                self.store.get_task(task.task_id).get("status"),
            )
            return
        # Mark this child run as parent-driven so the telemetry hook in
        # _run_task knows to skip per-child task.completed events. Local
        # retries via /api/tasks/{id}/retry don't go through this path,
        # so they will emit their own task.completed (with parent_task_id).
        token = PARENT_DRIVEN_RUN.set(True)
        try:
            self._run_task(task)
        finally:
            PARENT_DRIVEN_RUN.reset(token)

    def _run_task(self, task: Task) -> None:
        """Execute a task in a worker thread.

        Redirects stdout/stderr to the task log file so that underlying
        crawler print() calls (which may contain emoji) are captured safely
        instead of crashing on Windows GBK consoles.
        """
        logger.info("[RUN] _run_task ENTERED: task_id=%s, platform=%s, task_type=%s, thread=%s",
                    task.task_id, task.platform, task.task_type, threading.current_thread().name)
        try:
            self._run_task_impl(task)
        finally:
            # --- Telemetry: task.completed (outermost chokepoint) ----------
            # Catches every terminal path of _run_task_impl, including the
            # unknown-platform short-circuit (which returns early without
            # entering the inner try/finally). We pull the final status from
            # the DB so we observe whatever the state machine actually
            # committed, not what we *thought* would happen.
            #
            # Skip rules:
            #   - Batch parent does NOT execute via _run_task; only its
            #     children do. The parent's completion event is emitted by
            #     _execute_batch_task with aggregated record_count.
            #   - Batch child driven by the parent's run (PARENT_DRIVEN_RUN
            #     ContextVar set by _run_batch_child): parent's aggregate
            #     event covers it. Per-child events only fire on user-
            #     initiated retry of a single child via /api/tasks/{id}/retry
            #     (which goes through retry_task -> single-task path,
            #     bypassing _run_batch_child, so the ContextVar stays False).
            try:
                from crawlhub.core.telemetry import emit_task_completed
                from crawlhub.core._telemetry_ctx import PARENT_DRIVEN_RUN

                if not PARENT_DRIVEN_RUN.get():
                    _final = self.store.get_task(task.task_id) or {}
                    _final_status = _final.get("status") or "unknown"
                    _record_count = int(_final.get("record_count") or 0)
                    _started = _final.get("started_at")
                    _finished = _final.get("finished_at") or time.time()
                    _duration_ms = (
                        int((_finished - _started) * 1000)
                        if _started else 0
                    )
                    emit_task_completed(
                        task_id=task.task_id,
                        platform=task.platform,
                        task_type=task.task_type,
                        final_status=_final_status,
                        record_count=_record_count,
                        duration_ms=_duration_ms,
                        parent_task_id=task.parent_task_id or None,
                    )
            except Exception:
                # Telemetry must never break task teardown.
                pass

    def _plan_task_execution(self, platform: str, action: str) -> TaskExecutionPlan:
        """Resolve runtime/throttle route for one action."""
        return TaskExecutionPlan(get_action_meta(platform, action))

    def _pin_task_cookie(self, task: Task, throttle: Any) -> str | None:
        """Select one cookie and publish it via snapshot + thread-local override."""
        if throttle.cookie_count(task.platform) == 0:
            throttle.load_platform_cookies(task.platform)
        existing_override = task.snapshot_param.get("_override_cookie_path")
        chosen_state = None
        if existing_override:
            for state in throttle.get_platform_states(task.platform):
                if state.path == existing_override:
                    chosen_state = state
                    break
        if chosen_state is None:
            if throttle.cookie_count(task.platform) == 0:
                throttle.ensure_virtual_cookie(task.platform)
            chosen_state = throttle.select_best_cookie(task.platform)
        if chosen_state is None:
            return None
        set_thread_cookie_override(chosen_state.path)
        task.snapshot_param["_override_cookie_path"] = chosen_state.path
        return chosen_state.cookie_id

    def _execute_service_action(
        self,
        svc,
        task,
        ctx,
        plan,
        cookie_id,
    ):
        if plan.browser_backed:
            self._execute_browser_backed_action(svc, task, ctx, plan, cookie_id)
            return
        svc.execute(task.task_type, task.snapshot_param, ctx)

    def _execute_browser_backed_action(
        self,
        svc,
        task,
        ctx,
        plan,
        cookie_id,
    ):
        # =====================================================================
        #  R7 统一 BBA 路径（spec R7 §5.4 + §8.6）
        # ---------------------------------------------------------------------
        #  daemon 不再二分 lease_policy = action / manual——所有 BBA action
        #  走同一条路径：daemon 注入 BrowserSessionProvider，scraper 自管
        #  hold（with provider.hold() as page）。
        #
        #  Daemon 不预先 acquire ref_count——ref_count 由 hold 的 acquire/release
        #  累加。R7 模型核心：daemon 只注入 provider，意愿层由 scraper 自己表达。
        #
        #  Fail-fast 校验：BBA service 必须继承 RuntimeAwareService。
        #
        #  Finally 兜底：扫 owned_pages，对漏关的 PageHandle 调
        #  _fallback_close_and_release（关 page + release ref）。
        #  R7 §5.4：daemon finally 不调 maybe_close——关 chrome 由 release 唯一负责。
        # =====================================================================
        from crawlhub.core.browser.provider import BrowserSessionProvider
        from crawlhub.core.browser.session_key import SessionKey
        from crawlhub.core.platform.runtime_service import (
            RuntimeAwareService,
            RuntimeServices,
        )

        if not isinstance(svc, RuntimeAwareService):
            raise TypeError(
                f"BBA service '{type(svc).__name__}' must inherit RuntimeAwareService. "
                f"R7 removed the setattr fallback path. "
                f"MRO: {[c.__name__ for c in type(svc).__mro__]}"
            )

        task_tag = task.task_id[:12]
        resolved_cookie_id = cookie_id or f"{task.platform}:virtual"
        cookie_path = str((task.snapshot_param or {}).get("_override_cookie_path") or "")

        manager = self._get_browser_session_manager(plan.browser)
        runner = self._get_browser_runner()
        key = SessionKey(
            platform=task.platform,
            cookie_id=resolved_cookie_id,
            cookie_path=cookie_path,
        )

        owned_pages: set = set()
        provider = BrowserSessionProvider(
            manager=manager,
            runner=runner,
            key=key,
            owned_pages=owned_pages,
            cancel_event=getattr(ctx, "_cancelled", None),
        )
        runtime = RuntimeServices(
            browser=provider,
            cookie_id=resolved_cookie_id,
            cookie_path=cookie_path,
            transport=plan.transport,
            owned_pages=owned_pages,
        )

        logger.info(
            "[BBA] inject task=%s platform=%s cookie_id=%s transport=%s",
            task_tag, task.platform, resolved_cookie_id, plan.transport,
        )

        try:
            svc.execute_with_runtime(task.task_type, task.snapshot_param, ctx, runtime)
        finally:
            # === R7 finally 兜底：扫 owned_pages 关漏的（spec §5.4） ===
            # 正确 hold 用法不会留残留（hold.__exit__ 已 discard 自己）。
            # 残留来源：scraper 写 bug 或异常路径 hold 未走 finally（极罕见）。
            leaked = list(owned_pages)
            if leaked:
                logger.warning(
                    "[BBA] fallback closing %d leaked page(s) task=%s",
                    len(leaked), task_tag,
                )
                for page in leaked:
                    try:
                        runner.run(page._fallback_close_and_release(manager, key))
                    except Exception as exc:
                        logger.warning("[BBA] fallback close exc=%s", exc)
                owned_pages.clear()
            # === 不调 maybe_close_if_empty / 不另调 manager.release ===
            # 关 chrome 由 release 路径全权负责（hold 退出已 release；
            # fallback 路径里 _fallback_close_and_release 也调了 release）。

    def _get_browser_runner(self):
        # ────────────────────────────────────────────────────────────
        #  Double-checked locking：99% 命中走无锁快路径，1% 初始化加锁。
        #  快路径无锁：dict/字段读单条字节码 → GIL 保证原子。
        #  慢路径锁内重读：消除 TOCTOU race（两个线程同时进 if 分支
        #  各创建一个 runner，后写覆盖前写、引用泄漏）。
        # ────────────────────────────────────────────────────────────
        if self._browser_runner is None:
            with self._browser_runner_lock:
                if self._browser_runner is None:  # 锁内重读
                    from crawlhub.core.browser.runner import BrowserAsyncRunner

                    self._browser_runner = BrowserAsyncRunner()
        return self._browser_runner

    def _get_browser_session_manager(self, config):
        # =====================================================================
        #  R7：BrowserConfig 已简化（只剩 session_scope），manager cache key
        #  仅按 session_scope 区分（实际只会有一个 manager 实例，因为
        #  platform_cookie 是唯一 scope）。
        #
        #  R5/R6 时代的 page_pool_size / idle_ttl / max_age 多维 cache 已废。
        #
        #  ⚠️ Double-checked locking（同 _get_browser_runner 同款 race 修复）
        #  快路径：dict.get 无锁返回；慢路径：锁内重读 dict + 创建。
        #  关键：必须保证全进程 1 个 manager 实例，否则 BrowserSessionManager
        #  内部的 singleflight（asyncio.Lock + _creating[key]）跨实例失效，
        #  并发 launch_persistent_context 撞 Chromium SingletonLock。
        # =====================================================================
        key = (config.session_scope,)
        manager = self._browser_managers.get(key)
        if manager is None:
            with self._browser_managers_lock:
                manager = self._browser_managers.get(key)  # 锁内重读
                if manager is None:
                    from crawlhub.core.browser.session_manager import BrowserSessionManager
                    from crawlhub.core.request_gate import AsyncRequestGate

                    holder: dict = {}

                    async def factory(session_key):
                        throttle = get_cookie_throttle()
                        request_gate = AsyncRequestGate(
                            throttle, session_key.cookie_id, session_key.platform,
                        )

                        def mark_cookie_expired() -> None:
                            manager_ref = holder.get("manager")
                            if manager_ref is not None:
                                import asyncio

                                asyncio.create_task(
                                    manager_ref.mark_unhealthy(
                                        session_key, reason="cookie_expired",
                                    )
                                )

                        # R7 P5：BBA 抓 wire 身份头 → 写 cookie_jar.metadata
                        # ────────────────────────────────────────────────
                        # 当前只有 kuaishou 接入了 metadata 持久化（douyin
                        # 后续按同模式接入）。未接入的平台传 None，等同关闭
                        # capture，对 BBA 行为零影响。
                        # callback 必须便宜（patchright listener 在事件循环
                        # 内同步调用），所以这里只做：update_origin_headers
                        # + save 两个同步 IO，不调任何 async/network。
                        on_origin = _make_origin_metadata_callback(session_key)

                        # R7: 无 cookie → fail-fast，无 _NoopBrowserPage 兜底
                        if not session_key.cookie_path or not Path(session_key.cookie_path).exists():
                            raise RuntimeError(
                                f"BBA action requires a valid cookie for platform="
                                f"{session_key.platform!r}, cookie_id={session_key.cookie_id!r}, "
                                f"but cookie_path={session_key.cookie_path!r} does not exist."
                            )

                        from crawlhub.core.browser.playwright_runtime import create_playwright_browser_session
                        from crawlhub.core.registry import create_platform_service

                        # 从 platform service 读取 bba_skip_stealth 配置
                        # 快手等平台的 SDK 会检测 stealth 注入的自动化指纹（如
                        # --disable-extensions、navigator.webdriver patch），
                        # 导致数据抓取时身份被标记为自动化 → 请求返回未登录态。
                        _svc = create_platform_service(session_key.platform)
                        _skip_stealth = getattr(_svc, "bba_skip_stealth", False) if _svc else False

                        return await create_playwright_browser_session(
                            session_key,
                            config,
                            request_gate=request_gate,
                            on_cookie_expired=mark_cookie_expired,
                            on_origin_headers_captured=on_origin,
                            skip_stealth=_skip_stealth,
                        )

                    manager = BrowserSessionManager(factory=factory, config=config)
                    holder["manager"] = manager
                    self._browser_managers[key] = manager

        return manager

    def _run_task_impl(self, task: Task) -> None:

        """Inner implementation of _run_task. Wrapped by _run_task with a
        telemetry chokepoint so all terminal paths emit task.completed."""
        registry = get_registry()
        svc_cls = registry.get(task.platform)
        if svc_cls is None:
            logger.error("[RUN] Unknown platform '%s' for task %s", task.platform, task.task_id)
            self._apply_atomic_action(
                task.task_id,
                action=Action.NATURAL_FAIL,
                actor="worker",
                reason=f"Unknown platform: {task.platform}",
                extra_updates={
                    "finished_at": time.time(),
                    "error": f"Unknown platform: {task.platform}",
                },
            )
            return
        logger.info("[RUN] Service class resolved: %s for task %s", svc_cls.__name__, task.task_id)

        # Create TaskContext
        log_dir = self.data_root / "logs" / "tasks" / time.strftime("%Y-%m-%d")
        log_path = str(log_dir / f"{task.task_id}.log")
        # Determine action name for output_schema validation.
        # batch_run tasks store the real action in snapshot_param.action.
        _ctx_action = task.task_type
        if _ctx_action == "batch_run":
            _snap = (task.snapshot_param or {}) if isinstance(
                getattr(task, "snapshot_param", None), dict
            ) else {}
            _ctx_action = _snap.get("action", _ctx_action)
        ctx = TaskContext(
            task_id=task.task_id,
            output_dir=task.output_dir,
            log_path=log_path,
            on_progress=self._on_task_progress,
            on_log=self._on_task_log,
            platform=task.platform,
            action=_ctx_action,
            flux=self.flux,
        )
        self._contexts[task.task_id] = ctx

        # Throughput anchor: write a (record_count=0) sample at task start.
        # Without this anchor, a task that finishes within the first 5s sampler
        # tick has only a single sample (the terminal one) -> no delta -> no
        # speed point at all on the chart.
        try:
            self.store.add_record_sample(task.task_id, time.time(), 0)
        except Exception as e:
            logger.debug("[sample] start anchor failed for %s: %s", task.task_id, e)

        # Redirect stdout/stderr to task log file for THIS THREAD only.
        # We install a thread-aware proxy on sys.stdout/stderr at daemon
        # startup; here we simply register the per-task writers in the
        # current thread's local slot.  HTTP request threads and other
        # concurrent workers stay unaffected and keep writing to the real
        # underlying streams.
        _thread_log_file = None
        _thread_stdout = None
        _thread_stderr = None
        try:
            Path(log_path).parent.mkdir(parents=True, exist_ok=True)
            _thread_log_file = open(log_path, "a", encoding="utf-8", errors="replace")
            _thread_stdout = _TaskLogWriter(_thread_log_file, ctx, "STDOUT")
            _thread_stderr = _TaskLogWriter(_thread_log_file, ctx, "STDERR")
            register_thread_streams(_thread_stdout, _thread_stderr)
        except OSError:
            # Could not open log file — leave thread without per-task
            # redirection; the proxy will fall through to real stderr.
            _thread_stdout = None
            _thread_stderr = None

        try:
            logger.info("[RUN] Instantiating service %s for task %s...", svc_cls.__name__, task.task_id)
            from crawlhub.core.registry import create_platform_service
            svc = create_platform_service(task.platform)
            execution_plan = self._plan_task_execution(task.platform, _ctx_action)

            # --- Throttle: select-then-acquire ---

            # We MUST decide which cookie this task will use BEFORE acquiring the
            # throttle lock, then publish the choice via:
            #   1. thread-local override (read by bridge.resolve_cookie_path)
            #   2. task.snapshot_param["_override_cookie_path"] (read by retry logic)
            # This eliminates the race where daemon throttles cookie A but bridge
            # opens cookie B (because select_best_cookie was called twice and
            # returned different results due to next_available_at drift).
            throttle = get_cookie_throttle()
            acquired_cookie_id: str | None = None
            needs_cookie_pin = execution_plan.task_level_throttle or execution_plan.browser_backed

            if needs_cookie_pin:
                acquired_cookie_id = self._pin_task_cookie(task, throttle)
                if acquired_cookie_id and execution_plan.task_level_throttle:
                    acquire_start = time.time()
                    throttle.acquire(acquired_cookie_id, task.platform)
                    wait_seconds = time.time() - acquire_start
                    if wait_seconds > 0.05:
                        ctx.log(f"[throttle] waited {wait_seconds:.3f}s for cookie {acquired_cookie_id}")

            logger.info("[RUN] Calling action=%s for task %s...", task.task_type, task.task_id)
            self._execute_service_action(svc, task, ctx, execution_plan, acquired_cookie_id)
            logger.info("[RUN] action returned for task %s", task.task_id)


            # PR-C: cancel-intent honor point.
            # If the service returned normally but the task was cancelled
            # mid-flight (e.g. user clicked Cancel and the service detected
            # ctx.is_cancelled then silently `return`-ed), we must NOT treat
            # the empty-record-count as a "failure" — it's a cancel. Raise
            # TaskCancelled so the existing except-branch handles it
            # (and writes a clean CANCELLED status via state machine).
            #
            # Spec §1.4: cancel intent on cancel_event always wins over the
            # "looks like a fail" interpretation. Without this check the
            # cancel bug surfaces as: cancel -> service exits empty ->
            # daemon marks FAILED because record_count==0.
            if ctx.is_cancelled:
                raise TaskCancelled(task.task_id)

            # Mark cookie as healthy on successful task-level execution only.
            if acquired_cookie_id and execution_plan.task_level_throttle:
                throttle.report_success(acquired_cookie_id)


            # Determine final status based on error_count and record_count
            ctx.log(f"summary: records_written={ctx.record_count}, errors={ctx.error_count}, duration={ctx.duration:.1f}s")
            logger.info("[DIAG] task=%s pre-finalize: records=%d errors=%d", task.task_id, ctx.record_count, ctx.error_count)

            # Status determination logic:

            # - No errors and no records → either FAILED (silent failure) or
            #   SUCCEEDED with empty result, depending on per-task config
            #   `treat_empty_as_success` (default True). Many crawl APIs
            #   legitimately return 0 items (no comments, no search hits)
            #   without any error — treating those as failures floods the
            #   ops view with false positives. Users who want strict
            #   detection can pass `treat_empty_as_success=False` to opt
            #   into the old behavior.
            # - Has errors and no records → failed (complete failure)
            # - Has errors but also has records → partial_failed (some items failed)
            # - No errors and has records → completed
            treat_empty_as_success = bool(
                            (task.snapshot_param or {}).get("treat_empty_as_success", True)
            )
            if ctx.error_count == 0 and ctx.record_count == 0:
                if treat_empty_as_success:
                    final_status = TaskStatus.SUCCEEDED
                    final_action = Action.NATURAL_COMPLETE
                    error_msg = None
                    ctx.log(
                        "[NATURAL_EMPTY] task ended with 0 records and 0 errors; "
                        "treated as SUCCESS per task config (treat_empty_as_success=True)",
                        level="WARN",
                    )
                    _log_zero_record_response_dump(
                        ctx,
                        level="WARN",
                        message="Last response on empty result",
                    )
                    logger.info(

                        "[DIAG] task=%s branch=zero_records -> NATURAL_EMPTY (succeeded)",
                        task.task_id,
                    )
                else:
                    final_status = TaskStatus.FAILED
                    final_action = Action.NATURAL_FAIL
                    error_msg = "Task completed but produced 0 records"
                    logger.info("[DIAG] task=%s branch=zero_records -> NATURAL_FAIL", task.task_id)
                    # Silent failure: SDK returned empty without raising.
                    # If the bridge captured a last response, surface it so
                    # #log-panel shows what the server actually returned.
                    _silent_dump = _log_zero_record_response_dump(
                        ctx,
                        level="ERR",
                        message="Last response on zero-record failure",
                    )
                    if _silent_dump:
                        error_msg = f"{error_msg} | {_silent_dump}"
                    logger.info("[RUN] Task %s marked FAILED: %s", task.task_id, error_msg)
            elif ctx.error_count > 0 and ctx.record_count == 0:
                final_status = TaskStatus.FAILED
                final_action = Action.NATURAL_FAIL
                error_msg = f"All items failed ({ctx.error_count} errors, 0 records)"
                _silent_dump = _log_zero_record_response_dump(
                    ctx,
                    level="ERR",
                    message="Last response on all-items-failed",
                )
                if _silent_dump:
                    error_msg = f"{error_msg} | {_silent_dump}"
                logger.info("[RUN] Task %s marked FAILED: %s", task.task_id, error_msg)

            elif ctx.error_count > 0 and ctx.record_count > 0:
                final_status = TaskStatus.PARTIAL_SUCCEEDED
                final_action = Action.NATURAL_PARTIAL
                error_msg = f"{ctx.error_count} errors, {ctx.record_count} records"
                logger.info("[RUN] Task %s marked PARTIAL_FAILED: %s", task.task_id, error_msg)
            else:
                final_status = TaskStatus.SUCCEEDED
                final_action = Action.NATURAL_COMPLETE
                error_msg = None

            try:
                ctx.close()
                logger.info("[DIAG] task=%s ctx.close() OK", task.task_id)
            except Exception as _close_e:
                logger.exception("[DIAG] task=%s ctx.close() RAISED: %s", task.task_id, _close_e)
                raise
            try:
                summary = ctx.generate_summary(task.snapshot_param)
                logger.info("[DIAG] task=%s generate_summary OK", task.task_id)
            except Exception as _sum_e:
                logger.exception("[DIAG] task=%s generate_summary RAISED: %s", task.task_id, _sum_e)
                raise
            try:
                self.blob_store.write_summary(task.output_dir, summary)
                logger.info("[DIAG] task=%s write_summary OK", task.task_id)
            except Exception as _ws_e:
                logger.exception("[DIAG] task=%s write_summary RAISED: %s", task.task_id, _ws_e)
                raise

            update_data = {
                "finished_at": time.time(),

                "progress": 1.0,
                "result_files": self.blob_store.list_files(task.output_dir),
                "record_count": ctx.record_count,
                "total_bytes": ctx.total_bytes,
                "last_heartbeat": time.time(),
            }
            if error_msg:
                update_data["error"] = error_msg
            logger.info("[DIAG] task=%s pre-apply action=%s", task.task_id, final_action)
            try:
                _applied = self._apply_atomic_action(
                    task.task_id,
                    action=final_action,
                    actor="worker",
                    reason=error_msg,
                    extra_updates=update_data,
                )
                logger.info("[DIAG] task=%s post-apply applied=%s", task.task_id, _applied is not None)
                # Verify DB state actually flipped — this is the smoking-gun check.
                _verify = self.store.get_task(task.task_id)
                _verify_status = _verify.get("status") if _verify else "<gone>"
                logger.info("[DIAG] task=%s db-verify status=%s expected=%s",
                            task.task_id, _verify_status, final_status.value)
                if _verify_status != final_status.value:
                    logger.error("[DIAG] task=%s STATUS MISMATCH! db=%s expected=%s record_count=%d",
                                 task.task_id, _verify_status, final_status.value, ctx.record_count)
            except Exception as _apply_e:
                logger.exception("[DIAG] task=%s _apply_atomic_action RAISED: %s", task.task_id, _apply_e)
                raise

            # Cookie health is tracked through two paths only (see R4 P13):
            #   * Probe path: service.check_cookie() -> client.probe()
            #   * Task-execution path: detect_failure() + throttle.report_*
            # The legacy log-grep fallback used to live here; it was
            # redundant with detect_failure and produced false positives.

            # Only emit event for top-level tasks (not batch children).
            # Note: downstream dependency hook is fired by _apply_atomic_action.
            if not task.parent_task_id:
                if final_status == TaskStatus.SUCCEEDED:
                    self._emit_event("on_task_completed", {"task_id": task.task_id, "platform": task.platform})
                elif final_status == TaskStatus.FAILED:
                    self._emit_event("on_task_failed", {"task_id": task.task_id, "platform": task.platform, "error": error_msg})

        except TaskCancelled:
            ctx.close()
            # Two cases:
            #  (a) cancel_task -> _apply_atomic_action(CANCEL) already
            #      flipped status to CANCELLED; this branch just stamps
            #      finished_at / error. The CANCEL action below will be
            #      rejected by state_machine (cancelled→cancelled illegal)
            #      and the swallow path keeps things consistent.
            #  (b) The TaskCancelled was raised some other way (e.g. test
            #      directly calls ctx.cancel()); in that case status is
            #      still RUNNING and CANCEL is the right action.
            now = time.time()
            applied = self._apply_atomic_action(
                task.task_id,
                action=Action.CANCEL,
                actor="worker",
                reason="Task cancelled by user",
                extra_updates={
                    "finished_at": now,
                    "error": "Task cancelled by user",
                },
            )
            if applied is None:
                # Already in terminal state — just stamp finished_at if missing.
                cur = self.store.get_task(task.task_id) or {}
                if not cur.get("finished_at"):
                    self.store.update_task(task.task_id, {"finished_at": now, "error": "Task cancelled by user"})

        except UnicodeEncodeError as e:
            # Specific handling for encoding errors from underlying crawlers
            error_msg = f"UnicodeEncodeError: {str(e)[:500]} (GBK encoding issue - check crawler output)"
            logger.error("[ERR] Task %s encoding error: %s", task.task_id, error_msg)
            ctx.log(f"Encoding error caught: {e}", level="ERR")
            # If the encoding error happened while reading an HTTP body, dump status+text
            _resp_dump = format_response_dump(e)
            if _resp_dump:
                ctx.log(f"[ERR] Response dump: {_resp_dump}", level="ERR")
                error_msg = f"{error_msg} | {_resp_dump}"
            ctx.close()

            self._apply_atomic_action(
                task.task_id,
                action=Action.NATURAL_FAIL,
                actor="worker",
                reason=error_msg,
                extra_updates={
                    "finished_at": time.time(),
                    "error": error_msg,
                },
            )
            # Only emit event for top-level tasks (not batch children).
            # downstream dependency hook fired by _apply_atomic_action.
            if not task.parent_task_id:
                self._emit_event("on_task_failed", {"task_id": task.task_id, "platform": task.platform, "error": error_msg})

        except Exception as e:
            # --- Failure Mode Detection & Retry Engine ---
            failure_result = detect_failure(
                response=getattr(e, 'response', None),
                exception=e,
                platform=task.platform,
            )
            ctx.log(f"[RETRY] Failure detected: {failure_result}", level="WARN")
            logger.warning("[RETRY] Task %s failure: %s", task.task_id, failure_result)

            throttle = get_cookie_throttle()
            # Determine current cookie_id (best effort)
            current_cookie_id = self._get_task_cookie_id(task)

            retried = False

            if not execution_plan.task_level_throttle:
                ctx.log("[RETRY] request-level action: daemon retry/accounting skipped", level="WARN")

            elif failure_result.mode == FailureMode.COOKIE_EXPIRED and current_cookie_id:
                # Mark cookie as expired, try switching to another cookie

                expired_now = throttle.report_failure(current_cookie_id, FailureMode.COOKIE_EXPIRED)
                if expired_now:
                    self._emit_event("on_cookie_invalid", {"platform": task.platform})
                ctx.log(f"[RETRY] Cookie {current_cookie_id} marked expired, attempting switch...")

                # Retry with different cookies (max = cookie_count - 1)
                max_retries = max(0, throttle.cookie_count(task.platform) - 1)
                tried_ids = {current_cookie_id}
                retry_success = False

                for attempt in range(max_retries):
                    # PR-C: bail out if user cancelled during cookie retry storm
                    if ctx.is_cancelled:
                        ctx.log("[RETRY] Cancelled by user, stopping cookie retries", level="WARN")
                        break
                    next_cookie = throttle.select_next_cookie(task.platform, exclude_ids=tried_ids)
                    if next_cookie is None:
                        ctx.log(f"[RETRY] No more cookies available for retry", level="WARN")
                        break

                    tried_ids.add(next_cookie.cookie_id)
                    ctx.log(f"[RETRY] Attempt {attempt + 1}/{max_retries}: switching to cookie {next_cookie.label}")

                    try:
                        # Re-execute with new cookie path
                        # Update task snapshot_param + thread-local override so bridge picks the
                        # SAME cookie that we are about to throttle/report against.
                        task.snapshot_param["_override_cookie_path"] = next_cookie.path
                        set_thread_cookie_override(next_cookie.path)
                        # Acquire throttle on the new cookie before re-executing
                        throttle.acquire(next_cookie.cookie_id, task.platform)
                        svc2 = create_platform_service(task.platform)
                        svc2.execute(task.task_type, task.snapshot_param, ctx)
                        # PR-C: cancel-intent check after retry too
                        if ctx.is_cancelled:
                            raise TaskCancelled(task.task_id)
                        throttle.report_success(next_cookie.cookie_id)
                        retry_success = True
                        retried = True
                        break
                    except Exception as retry_e:
                        retry_failure = detect_failure(
                            response=getattr(retry_e, 'response', None),
                            exception=retry_e,
                            platform=task.platform,
                        )
                        ctx.log(f"[RETRY] Attempt {attempt + 1} failed: {retry_failure}", level="WARN")
                        if retry_failure.mode == FailureMode.COOKIE_EXPIRED:
                            expired_now2 = throttle.report_failure(next_cookie.cookie_id, FailureMode.COOKIE_EXPIRED)
                            if expired_now2:
                                self._emit_event("on_cookie_invalid", {"platform": task.platform})
                        else:
                            # Non-cookie error during retry, stop retrying
                            break

                if not retry_success:
                    retried = False

            elif failure_result.mode == FailureMode.NETWORK_ERROR:
                # Retry in-place up to 3 times with 5s delay
                max_network_retries = 3
                retry_success = False

                for attempt in range(max_network_retries):
                    # PR-C: bail out if user cancelled during the retry storm.
                    if ctx.is_cancelled:
                        ctx.log("[RETRY] Cancelled by user, stopping network retries", level="WARN")
                        break
                    ctx.log(f"[RETRY] Network error retry {attempt + 1}/{max_network_retries}, waiting 5s...")
                    try:
                        # Cancel-aware sleep: returns immediately on ctx.cancel()
                        # and raises TaskCancelled, which propagates up to the
                        # outer except TaskCancelled handler. Without this the
                        # cancel latency = full 5s × remaining_attempts.
                        ctx.sleep(5)
                    except TaskCancelled:
                        raise  # let the outer handler set status=cancelled
                    try:
                        svc3 = create_platform_service(task.platform)
                        svc3.execute(task.task_type, task.snapshot_param, ctx)
                        # PR-C: same cancel-intent check as the main path.
                        if ctx.is_cancelled:
                            raise TaskCancelled(task.task_id)
                        if current_cookie_id:
                            throttle.report_success(current_cookie_id)
                        retry_success = True
                        retried = True
                        break
                    except Exception as retry_e:
                        retry_failure = detect_failure(
                            response=getattr(retry_e, 'response', None),
                            exception=retry_e,
                            platform=task.platform,
                        )
                        ctx.log(f"[RETRY] Network retry {attempt + 1} failed: {retry_failure}", level="WARN")
                        if retry_failure.mode != FailureMode.NETWORK_ERROR:
                            # Different failure mode, stop network retries
                            break

                if not retry_success:
                    retried = False

            elif failure_result.mode == FailureMode.RATE_LIMITED and current_cookie_id:
                # Trigger backoff. If the cookie has now exhausted its backoff budget,
                # report_failure() returns True -> escalate to on_cookie_invalid.
                expired_now = throttle.report_failure(current_cookie_id, FailureMode.RATE_LIMITED)
                if expired_now:
                    self._emit_event("on_cookie_invalid", {"platform": task.platform})
                    ctx.log(f"[RETRY] Cookie {current_cookie_id} ESCALATED to EXPIRED (rate limited, backoff exhausted)", level="WARN")
                else:
                    ctx.log(f"[RETRY] Cookie {current_cookie_id} entered backoff (rate limited)")

            elif failure_result.mode == FailureMode.ANTI_CRAWL and current_cookie_id:
                # Trigger aggressive backoff (doubled base). Same escalation rule applies.
                expired_now = throttle.report_failure(current_cookie_id, FailureMode.ANTI_CRAWL)
                if expired_now:
                    self._emit_event("on_cookie_invalid", {"platform": task.platform})
                    ctx.log(f"[RETRY] Cookie {current_cookie_id} ESCALATED to EXPIRED (anti-crawl, backoff exhausted)", level="WARN")
                else:
                    ctx.log(f"[RETRY] Cookie {current_cookie_id} entered aggressive backoff (anti-crawl)")

            # If retry succeeded, handle as normal completion
            if retried:
                ctx.log(f"[RETRY] Task recovered after retry")
                ctx.log(f"summary: records_written={ctx.record_count}, errors={ctx.error_count}, duration={ctx.duration:.1f}s")

                # Same per-task `treat_empty_as_success` semantics as the

                # primary path above. After retry, a 0-record outcome may
                # still be a legitimate empty-but-fine result.
                treat_empty_as_success_retry = bool(
                            (task.snapshot_param or {}).get("treat_empty_as_success", True)
                )
                if ctx.error_count == 0 and ctx.record_count == 0:
                    if treat_empty_as_success_retry:
                        final_status = TaskStatus.SUCCEEDED
                        final_action = Action.NATURAL_COMPLETE
                        error_msg = None
                        ctx.log(
                            "[NATURAL_EMPTY] task ended with 0 records and 0 errors after retry; "
                            "treated as SUCCESS per task config (treat_empty_as_success=True)",
                            level="WARN",
                        )
                        _log_zero_record_response_dump(
                            ctx,
                            level="WARN",
                            message="Last response on empty result after retry",
                        )
                    else:

                        final_status = TaskStatus.FAILED
                        final_action = Action.NATURAL_FAIL
                        error_msg = "Task completed after retry but produced 0 records"
                        _silent_dump = _log_zero_record_response_dump(
                            ctx,
                            level="ERR",
                            message="Last response on zero-record failure after retry",
                        )
                        if _silent_dump:
                            error_msg = f"{error_msg} | {_silent_dump}"
                elif ctx.error_count > 0 and ctx.record_count == 0:
                    final_status = TaskStatus.FAILED
                    final_action = Action.NATURAL_FAIL
                    error_msg = f"All items failed after retry ({ctx.error_count} errors)"
                    _silent_dump = _log_zero_record_response_dump(
                        ctx,
                        level="ERR",
                        message="Last response on all-items-failed after retry",
                    )
                    if _silent_dump:
                        error_msg = f"{error_msg} | {_silent_dump}"

                elif ctx.error_count > 0:
                    final_status = TaskStatus.PARTIAL_SUCCEEDED
                    final_action = Action.NATURAL_PARTIAL
                    error_msg = f"{ctx.error_count} errors, {ctx.record_count} records (recovered via retry)"
                else:
                    final_status = TaskStatus.SUCCEEDED
                    final_action = Action.NATURAL_COMPLETE
                    error_msg = None

                ctx.close()
                summary = ctx.generate_summary(task.snapshot_param)
                self.blob_store.write_summary(task.output_dir, summary)

                update_data = {

                    "finished_at": time.time(),
                    "progress": 1.0,
                    "result_files": self.blob_store.list_files(task.output_dir),
                    "record_count": ctx.record_count,
                    "total_bytes": ctx.total_bytes,
                    "last_heartbeat": time.time(),
                }
                if error_msg:
                    update_data["error"] = error_msg
                self._apply_atomic_action(
                    task.task_id,
                    action=final_action,
                    actor="worker",
                    reason=error_msg,
                    extra_updates=update_data,
                )

                if not task.parent_task_id:
                    if final_status == TaskStatus.SUCCEEDED:
                        self._emit_event("on_task_completed", {"task_id": task.task_id, "platform": task.platform})
                    elif final_status == TaskStatus.FAILED:
                        self._emit_event("on_task_failed", {"task_id": task.task_id, "platform": task.platform, "error": error_msg})
            else:
                # All retries exhausted or non-retryable failure.
                # IMPORTANT: emit response dump BEFORE ctx.close() so the line
                # is captured by the task log (and pushed to the #log-panel).
                _resp_dump = format_response_dump(e)
                if _resp_dump:
                    ctx.log(f"[ERR] Response dump: {_resp_dump}", level="ERR")
                ctx.close()
                base_msg = f"{type(e).__name__}: {str(e)[:500]} [failure_mode={failure_result.mode.value}]"
                error_msg = f"{base_msg} | {_resp_dump}" if _resp_dump else base_msg
                logger.error("[ERR] Task %s failed (no retry): %s", task.task_id, error_msg)

                # Write error.json to task directory
                error_path = Path(task.output_dir) / "error.json"
                try:
                    import traceback
                    # Re-extract raw response for richer error.json (status + full preview).
                    _resp_obj = getattr(e, "response", None)
                    _resp_status = None
                    _resp_text_preview = None
                    if _resp_obj is not None and hasattr(_resp_obj, "status_code"):
                        try:
                            _resp_status = _resp_obj.status_code
                        except Exception:
                            _resp_status = None
                        try:
                            _txt = _resp_obj.text or ""
                            _resp_text_preview = _txt[:2000]
                        except Exception:
                            _resp_text_preview = None
                    with open(error_path, "w", encoding="utf-8") as f:
                        json.dump({
                            "error_type": type(e).__name__,
                            "error_message": str(e),
                            "failure_mode": failure_result.mode.value,
                            "failure_reason": failure_result.reason,
                            "response_status": _resp_status,
                            "response_text_preview": _resp_text_preview,
                            "traceback": traceback.format_exc(),
                        }, f, ensure_ascii=False, indent=2)
                except OSError:
                    pass

                self._apply_atomic_action(
                    task.task_id,
                    action=Action.NATURAL_FAIL,
                    actor="worker",
                    reason=error_msg,
                    extra_updates={
                        "finished_at": time.time(),
                        "error": error_msg,
                    },
                )
                if not task.parent_task_id:
                    self._emit_event("on_task_failed", {"task_id": task.task_id, "platform": task.platform, "error": error_msg})

        finally:
            # Unregister thread-local writers BEFORE closing the file so
            # any in-flight write from this thread (e.g. teardown logging)
            # falls through to the real stderr instead of hitting a
            # closed file.
            unregister_thread_streams()
            if _thread_log_file and not _thread_log_file.closed:
                _thread_log_file.close()
            # Clear thread-local cookie override so the worker thread is clean
            # for whatever task it picks up next.
            clear_thread_cookie_override()
            self._futures.pop(task.task_id, None)
            # Before dropping the ctx, the next sampler tick will read the
            # finalized record_count via SUM(record_count) FROM tasks (the
            # _on_task_log heartbeat or the _run_task body already persisted
            # it via update_task). No session-delta accounting needed since
            # the global sample is now SQL-aggregated each tick.
            try:
                self._contexts.pop(task.task_id, None)
            except Exception as e:
                logger.debug("[sampler] context pop failed for %s: %s", task.task_id, e)
            logger.info("[RUN] _run_task EXITED: task_id=%s, thread=%s", task.task_id, threading.current_thread().name)

    def cancel_task(self, task_id: str) -> bool:
        """Cancel a task. Supports cascade cancel for batch parents.

        Single chokepoint via state machine: every non-terminal status has a
        legal CANCEL transition (spec §3), so we just delegate to
        `_apply_atomic_action`. The wrapper handles ctx.cancel() side effect
        for running tasks and parent-aggregate refresh.
        """
        task = self.store.get_task(task_id)
        if task is None:
            return False

        # Batch parent: cascade-cancel via orchestrator. The orchestrator will
        # set cancellation_intent and walk children, calling _apply_atomic_action
        # internally (B2). The parent's own status flip happens via aggregate.
        if task["task_type"] == "batch_run" and task.get("parent_task_id") is None:
            if self.batch_orchestrator:
                # B4 / spec §1.4: set parent cancellation_intent=true and
                # write a SET_CANCELLATION_INTENT audit row before fanning
                # out to children. Recovery (B3) reads this flag to decide
                # whether interrupted children should be re-queued or stay
                # cancelled.
                self.store.update_cancellation_intent(task_id, True)
                self.store.insert_transition(
                    task_id=task_id,
                    from_status=task["status"],
                    to_status=task["status"],
                    action="set_cancellation_intent",
                    actor="user",
                    reason="User cancelled batch parent",
                )
                self.batch_orchestrator.cancel_batch(
                    task_id,
                    cancel_context_fn=self._cancel_context,
                )
                return True
            return False

        # Atomic / child task: single state-machine transition.
        result = self._apply_atomic_action(
            task_id,
            action=Action.CANCEL,
            actor="user",
            reason="Cancelled via API",
            extra_updates={
                "finished_at": time.time(),
                "error": (
                    "Cancelled while waiting for upstream"
                    if task["status"] == TaskStatus.WAITING_DEPENDENCY.value
                    else (
                        "Cancelled before execution"
                        if task["status"] == TaskStatus.QUEUED.value
                        else "Cancelled by user"
                    )
                ),
            },
        )
        return result is not None

    def _cancel_context(self, task_id: str) -> bool:
        """Cancel a task's context (used by batch cascade cancel)."""
        ctx = self._contexts.get(task_id)
        if ctx:
            ctx.cancel()
            return True
        return False

    def retry_task(self, task_id: str) -> Task:
        """Retry a failed/interrupted/cancelled task.

        Routing by task_type:
          - batch_run parent: dispatch to batch orchestrator. `_execute_task`
            would feed the sentinel "batch_run" into the per-platform service
            dispatch and crash with `Unknown action: batch_run`.
          - everything else: single-task path (atomic FULL_RETRY transition,
            then resubmit to platform executor).
        """
        existing = self.store.get_task(task_id)
        if existing is None:
            raise TaskNotFound(task_id)

        # Single source of truth: ask the state machine whether FULL_RETRY is
        # legal from the current status. Previously this used a duplicated
        # `TaskStatus.retryable()` set which drifted from the transition table
        # (v4 added SUCCEEDED/PARTIAL_SUCCEEDED to FULL_RETRY but the legacy
        # gate still rejected them — UI showed a "重新执行" button that the
        # backend then refused with 409). Going through can_transition keeps
        # the gate, the state machine, and the UI in lockstep automatically.
        #
        # Note: for batch parents the actual fan-out below uses the parent
        # action `failed_retry` (which dispatches FULL_RETRY per failed/
        # cancelled child). Gating on the parent's own FULL_RETRY legality is
        # the right proxy: the parent statuses that allow FULL_RETRY are
        # exactly the terminal-ish statuses where re-running children makes
        # sense.
        if not can_transition(existing["status"], Action.FULL_RETRY):
            raise TaskNotRetryable(task_id, existing["status"])

        # --- Batch parent path -----------------------------------------------
        if existing.get("task_type") == "batch_run" and existing.get("parent_task_id") is None:
            if not self.batch_orchestrator:
                raise RuntimeError("Batch orchestrator not initialized")
            # Full retry semantics: reset EVERY terminal child (incl.
            # succeeded) back to QUEUED and re-run the batch from scratch.
            # This is distinct from the "重试失败子任务" button, which routes
            # through `/retry-failed` -> apply_parent_action(..., "failed_retry")
            # and only resets failed/cancelled children. If we used
            # "failed_retry" here, the two buttons would be indistinguishable
            # and succeeded children would never re-run — which is exactly
            # the bug users hit.
            #
            # Throughput accounting note: full_retry does NOT delete prior
            # `record_samples` rows. Past speed points stay intact in the
            # dashboard's historical window. New samples are written as the
            # re-run progresses (start anchor at 0, periodic samples by the
            # sampler thread, terminal sample on completion), so the new
            # download contributes to the current speed curve. Per-task
            # series safely handles the cumulative-counter reset because
            # the rate query filters out negative deltas (see
            # api/routes.py:get_record_rate, "if delta <= 0: continue").
            #
            # apply_parent_action goes through the state machine end-to-end
            # (FULL_RETRY per child, cancellation_intent cleared, parent
            # aggregate recomputed) AND re-submits the batch executor itself.
            self.apply_parent_action(task_id, "full_retry", actor="user")
            return Task.from_dict(self.store.get_task(task_id))

        # --- Single-task path (atomic / non-batch child retry) ---------------
        # Reuse same task_id and output_dir, reset status to QUEUED.
        # Spec §3: terminal-ish -> queued via FULL_RETRY action.
        #
        # Metadata reset on retry (bug fix 2026-05-22): the previous run's
        # `record_count`, `total_bytes`, `progress`, `last_heartbeat` and
        # `result_files` MUST be cleared along with the timestamps. If we
        # leave them in place the UI shows nonsense during the new run:
        #   * old finished_at + new last_heartbeat -> "duration = -52677s"
        #     (formatDuration uses started_at→finished_at; finished_at from
        #     the previous run is in the past relative to a fresh restart)
        #   * record_count never appears to "start from 0" — it just sits at
        #     the previous run's terminal value until the new run overwrites
        #     it via _on_task_log heartbeat.
        # The output_dir itself is preserved (full_retry semantics) so the
        # actual data files on disk overwrite naturally.
        task = Task.from_dict(existing)
        task.status = TaskStatus.QUEUED
        task.error = None
        task.started_at = None
        task.finished_at = None
        applied = self._apply_atomic_action(
            task_id,
            action=Action.FULL_RETRY,
            actor="user",
            reason="Retry by user",
            extra_updates=_RETRY_RESET_UPDATES(),
        )
        if applied is None:
            # Status changed under our feet (e.g. dependency-resolver triggered
            # it). Surface as not-retryable — caller will re-read.
            raise TaskNotRetryable(task_id, self.store.get_task(task_id)["status"])
        self._execute_task(task)
        return task

    # --- Dependency: Unified Status Hook & Auto-Trigger ---

    def _apply_atomic_action(
        self,
        task_id: str,
        action: str,
        actor: str,
        reason: str | None = None,
        extra_updates: dict | None = None,
        *,
        skip_parent_aggregate: bool = False,
    ) -> dict | None:
        """The single chokepoint for *atomic-task* status mutations.

        Wraps `state_machine.transition_task` and adds the side effects every
        caller used to do by hand:
          1. If the action is CANCEL, signal `ctx.cancel_event` so the worker
             unblocks immediately (otherwise the cancel races with whatever
             the worker is doing — sleep / requests / write).
          2. If the task has a parent, recompute parent aggregate under the
             parent-scoped lock (spec §0.8) — this is what eventually flips
             the parent's status when the last child settles.
          3. If the new status is terminal, fire `_on_task_status_changed`
             so dependency-waiting downstream tasks advance.
          4. Push a WebSocket event so the UI updates without a poll.

        Returns the updated task dict, or None if the transition was illegal
        (logged + swallowed; callers should not need defensive try/except for
        the 'already in target state' case — that's the whole point of the
        idempotency guarantee).

        spec §1.1 / §2.1 / §5.2 — this is the unified entry point.

        Performance opt (`skip_parent_aggregate`, perf-2026-05-22):
          When True, skips Side effect 2 entirely. This is ONLY safe when
          the caller is doing a batched fan-out over many children of the
          same parent and will run a single aggregate_with_lock at the end
          of the loop. Used by `_fanout_full_retry`, `_fanout_failed_retry`,
          `_fanout_force_succeeded` to collapse N parent aggregations
          (one per child, O(N²) due to children re-reads) into a single
          aggregation at the tail. Side effect 4 (child WS event) still
          fires so the UI sees per-child state change.
        """
        try:
            updated = transition_task(
                self.store,
                task_id=task_id,
                action=action,
                actor=actor,
                reason=reason,
                extra_updates=extra_updates,
            )
        except IllegalTransitionError as e:
            logger.warning("[DIAG] _apply_atomic_action illegal-transition task=%s action=%s err=%s",
                           task_id, action, e)
            return None
        except Exception as e:
            logger.exception("[DIAG] _apply_atomic_action transition_task RAISED task=%s action=%s err=%s",
                             task_id, action, e)
            raise

        old_status = "<via_state_machine>"  # the state machine knows; for the hook we pass new_status
        new_status = updated["status"]

        # Side effect 1: if this is a cancel, signal the running worker.
        # transition_task only flipped the DB row — the in-memory ctx still
        # needs to be poked so the worker thread checks cancellation on its
        # next yield point (sleep / time.sleep / explicit ctx.check_cancelled).
        if action == Action.CANCEL:
            ctx = self._contexts.get(task_id)
            if ctx is not None:
                ctx.cancel()

        # Side effect 2: parent aggregate refresh.
        # `skip_parent_aggregate` is set by fan-out callers that will run
        # a single aggregate at the tail — saves O(N²) child re-reads.
        parent_id = updated.get("parent_task_id")
        if parent_id and not skip_parent_aggregate:
            try:
                agg = aggregate_with_lock(self.store, parent_id)
                # If aggregate produced a *terminal* parent status, downstream
                # of the parent itself may need to advance (parent has its
                # own dependency edges). Fire the hook on parent only when
                # changed — avoids redundant downstream work.
                if agg.get("changed") and agg.get("new_status") in (
                    TaskStatus.SUCCEEDED.value,
                    TaskStatus.PARTIAL_SUCCEEDED.value,
                    TaskStatus.FAILED.value,
                    TaskStatus.CANCELLED.value,
                ):
                    self._on_task_status_changed(parent_id, agg.get("old_status") or "running", agg["new_status"])
                # Push parent's aggregate change to UI (spec §0.8 — always
                # refresh UI on aggregate, even if the status string is
                # the same, because counts may have changed).
                self._emit_event("on_task_aggregate_changed", {
                    "task_id": parent_id,
                    "status": agg.get("new_status"),
                    "changed": agg.get("changed", False),
                })
            except Exception as e:  # noqa: BLE001 — aggregate must never crash the worker
                logger.error("[state] aggregate_with_lock(%s) failed: %s", parent_id, e)

        # Side effect 3: downstream dependency hook (only for top-level tasks
        # AND when the new status is terminal). Children's downstream is
        # handled via the parent-aggregate path above — we don't fan out
        # downstream off children directly, that's a parent responsibility.
        if not parent_id and new_status in (
            TaskStatus.SUCCEEDED.value,
            TaskStatus.PARTIAL_SUCCEEDED.value,
            TaskStatus.FAILED.value,
            TaskStatus.CANCELLED.value,
        ):
            self._on_task_status_changed(task_id, old_status, new_status)

        # Side effect 4: WS push for the task itself.
        self._emit_event("on_task_status_changed", {
            "task_id": task_id,
            "status": new_status,
            "action": action,
        })

        return updated

    # --- Dependency: Unified Status Hook & Auto-Trigger ---

    def _on_task_status_changed(self, task_id: str, old_status: str, new_status: str) -> None:
        """Unified hook called whenever a task's status changes to a terminal state.

        Checks if any downstream tasks are waiting on this task and triggers them.
        With multi-upstream support, a downstream is triggered only when ALL of
        its upstreams have settled.
        """
        if new_status not in (TaskStatus.SUCCEEDED.value, TaskStatus.PARTIAL_SUCCEEDED.value, TaskStatus.FAILED.value, TaskStatus.CANCELLED.value):
            return

        # Throughput sampling: lock in the FINAL record_count when a task
        # transitions to terminal. Without this final point, the speed chart
        # would lose the last (often largest) batch produced just before completion.
        try:
            now = time.time()
            ctx = self._contexts.get(task_id)
            final_count = None
            if ctx is not None:
                final_count = int(ctx.record_count or 0)
            else:
                row = self.store.get_task(task_id)
                if row:
                    final_count = int(row.get("record_count") or 0)
            if final_count is not None:
                self.store.add_record_sample(task_id, now, final_count)
        except Exception as e:
            logger.debug("[sample] terminal sample failed for %s: %s", task_id, e)

        # Find all downstream tasks waiting on this task
        downstream_tasks = self.store.find_waiting_downstream(task_id)
        if not downstream_tasks:
            return

        logger.info(
            "[dep] Task %s -> %s, found %d waiting downstream tasks",
            task_id, new_status, len(downstream_tasks),
        )

        for downstream in downstream_tasks:
            self._maybe_trigger_downstream(downstream)

    def _maybe_trigger_downstream(self, downstream: dict) -> None:
        """Decide whether to trigger or fail a downstream task.

        Re-evaluates the *whole* upstream set. If any upstream is still in
        flight, leaves the task in waiting_dependency. If any upstream is
        terminally bad and ``allow_partial_upstream=False``, fails it. If all
        usable, hands off to ``_resolve_and_start_downstream``.
        """
        from crawlhub.core.batch import check_upstreams_and_decide

        downstream_id = downstream["task_id"]
        downstream_logic_param = downstream.get("logic_param", {}) or {}
        allow_partial = bool(downstream_logic_param.get("allow_partial_upstream", True))
        deps = list(downstream.get("depends_on_task_ids") or [])

        if not deps:
            # Shouldn't happen — a waiting_dependency row with no deps is a bug.
            logger.warning(
                "[dep] Downstream %s has no depends_on_task_ids but is waiting; treating as ready",
                downstream_id,
            )
            self._resolve_and_start_downstream(downstream_id)
            return

        decision = check_upstreams_and_decide(deps, allow_partial, self.store)

        if decision["action"] == "ready":
            self._resolve_and_start_downstream(downstream_id)
            return

        if decision["action"] == "wait":
            # Update waiting_reason but stay in waiting_dependency.
            self.store.update_task(downstream_id, {"waiting_reason": decision["waiting_reason"]})
            logger.info("[dep] Downstream %s still waiting: %s", downstream_id, decision["waiting_reason"])
            return

        # error — mark failed.
        # State machine note: there's no direct (waiting_dependency, NATURAL_FAIL)
        # rule (spec §3) because failure-from-waiting is conceptually
        # "upstream is bad, we never started". We synthesize the canonical
        # transition path: DEPS_READY -> SCHEDULER_DISPATCH -> NATURAL_FAIL,
        # so the audit log shows a clean lineage.
        self._apply_atomic_action(
            downstream_id, action=Action.DEPS_READY, actor="system",
            reason="auto-trigger (about to fail due to bad upstream)",
        )
        self._apply_atomic_action(
            downstream_id, action=Action.SCHEDULER_DISPATCH, actor="system",
            reason="dispatch-then-fail (bad upstream)",
            extra_updates={"started_at": time.time()},
        )
        self._apply_atomic_action(
            downstream_id, action=Action.NATURAL_FAIL, actor="system",
            reason=f"{decision['code']}: {decision['message']}",
            extra_updates={
                "finished_at": time.time(),
                "error": f"{decision['code']}: {decision['message']}",
                "waiting_reason": None,
            },
        )
        logger.info("[dep] Downstream %s failed: %s", downstream_id, decision["code"])

    def _trigger_downstream(self, upstream_task_id: str, upstream_status: str, downstream: dict) -> None:
        """Deprecated thin wrapper kept for any in-tree callers.

        New code uses ``_maybe_trigger_downstream`` which re-evaluates the
        full upstream set instead of single-upstream branching.
        """
        self._maybe_trigger_downstream(downstream)

    def _resolve_and_start_downstream(self, downstream_id: str) -> None:
        """Atomically transition a fully-ready downstream to pending and execute.

        For batch tasks, materialize children from items_from (SQL or file) at
        this moment. For non-batch tasks, just kick off execution.
        """
        downstream = self.store.get_task(downstream_id)
        if not downstream:
            logger.warning("[dep] Downstream task %s not found", downstream_id)
            return

        is_batch = downstream.get("task_type") == "batch_run"

        # Atomic transition: waiting_dependency -> queued via state machine
        # (spec §3 DEPS_READY action). Idempotent: if another path already
        # advanced the task we get None and bail.
        applied = self._apply_atomic_action(
            downstream_id,
            action=Action.DEPS_READY,
            actor="system",
            reason="Upstream dependencies ready",
            extra_updates={"waiting_reason": None},
        )
        if applied is None:
            logger.warning("[dep] DEPS_READY transition failed for %s (already triggered?)", downstream_id)
            return

        if is_batch:
            # Materialize items from items_from spec.
            task_logic_param = downstream.get("logic_param", {}) or {}
            items_from = task_logic_param.get("items_from")
            try:
                items = self._materialize_items_from(items_from)
            except Exception as e:  # noqa: BLE001
                # Tick the task into RUNNING then FAILED so the audit log
                # and downstream hooks see the canonical (queued -> running -> failed)
                # path. State machine: SCHEDULER_DISPATCH then NATURAL_FAIL.
                self._apply_atomic_action(
                    downstream_id, action=Action.SCHEDULER_DISPATCH, actor="system",
                    reason="dispatch-then-fail (items resolution)",
                    extra_updates={"started_at": time.time()},
                )
                self._apply_atomic_action(
                    downstream_id, action=Action.NATURAL_FAIL, actor="system",
                    reason=f"RESOLVE_ITEMS_FAILED: {e}",
                    extra_updates={
                        "finished_at": time.time(),
                        "error": f"RESOLVE_ITEMS_FAILED: {e}",
                    },
                )
                logger.error("[dep] Downstream %s resolve_items failed: %s", downstream_id, e)
                return

            if not items:
                # Empty result: spec says treat as completed with 0 children.
                # SCHEDULER_DISPATCH then NATURAL_COMPLETE for the same audit reason.
                self._apply_atomic_action(
                    downstream_id, action=Action.SCHEDULER_DISPATCH, actor="system",
                    reason="dispatch-then-succeed (0 items)",
                    extra_updates={"started_at": time.time()},
                )
                self._apply_atomic_action(
                    downstream_id, action=Action.NATURAL_COMPLETE, actor="system",
                    reason="0 items resolved",
                    extra_updates={
                        "finished_at": time.time(),
                        "progress": 1.0,
                    },
                )
                logger.info("[dep] Downstream %s completed with 0 items", downstream_id)
                return

            try:
                self.batch_orchestrator.create_children_for_waiting_task(downstream_id, items)
            except Exception as e:  # noqa: BLE001
                self._apply_atomic_action(
                    downstream_id, action=Action.SCHEDULER_DISPATCH, actor="system",
                    reason="dispatch-then-fail (create_children)",
                    extra_updates={"started_at": time.time()},
                )
                self._apply_atomic_action(
                    downstream_id, action=Action.NATURAL_FAIL, actor="system",
                    reason=f"RESOLVE_ITEMS_FAILED: Failed to create children: {e}",
                    extra_updates={
                        "finished_at": time.time(),
                        "error": f"RESOLVE_ITEMS_FAILED: Failed to create children: {e}",
                    },
                )
                logger.error("[dep] Downstream %s create_children failed: %s", downstream_id, e)
                return

            logger.info("[dep] Triggering batch downstream %s with %d items", downstream_id, len(items))
            executor = self._executors.get(
                downstream.get("platform", ""),
                self._executors["_default"],
            )
            future = executor.submit(self._execute_batch_task, downstream_id)
            self._futures[downstream_id] = future
        else:
            # Non-batch task: simply execute it directly
            logger.info("[dep] Triggering non-batch downstream %s", downstream_id)
            task = Task(
                task_id=downstream["task_id"],
                platform=downstream["platform"],
                task_type=downstream["task_type"],
                logic_param=downstream.get("logic_param", {}) or {},
                snapshot_param=downstream.get("snapshot_param", {}) or {},
                output_dir=downstream.get("output_dir", ""),
            )
            self._execute_task(task)

    def _materialize_items_from(self, items_from: dict | None) -> list[str]:
        """Resolve a waiting batch's items_from at trigger time.

        Supports both file-mode and SQL-mode. SQL mode is the canonical path
        for upstream-derived inputs.
        """
        if not items_from:
            raise ValueError("waiting batch has no items_from spec")

        if "sources" in items_from:
            from crawlhub.core.sql_runner import run_items_from
            logger.info("[dep] Running SQL pipeline for items_from")
            items = [str(x) for x in run_items_from(items_from, self.store)]
            logger.info("[dep] SQL pipeline produced %d items", len(items))
            return items

        if "file" in items_from:
            from crawlhub.core.batch import resolve_items
            return resolve_items(items=None, items_from=items_from, store=self.store)

        # Legacy {task_id, field} — explicitly rejected.
        if "task_id" in items_from:
            raise ValueError(
                "items_from {task_id, field} is no longer supported. "
                "Use the SQL pipeline: items_from = {sources, sql, field, dedup?}."
            )

        raise ValueError("unrecognized items_from shape")

    def _recover_waiting_tasks(self) -> None:
        """Recover waiting_dependency tasks after daemon restart.

        Re-evaluates each waiting task's full upstream set, triggering or
        failing it as appropriate.
        """
        waiting_tasks = self.store.list_tasks(status="waiting_dependency", limit=1000, include_children=True)
        if not waiting_tasks:
            return

        logger.info("[dep] Recovery: found %d waiting_dependency tasks", len(waiting_tasks))

        for task in waiting_tasks:
            deps = task.get("depends_on_task_ids") or []
            if not deps:
                logger.warning("[dep] Recovery: task %s has no upstream ids; ignoring", task["task_id"])
                continue
            self._maybe_trigger_downstream(task)

    # --- Force Operations ---

    def force_complete(self, task_id: str) -> dict:
        """Force a task to completed status and trigger downstream.

        Implemented via state-machine FORCE_SUCCEEDED action (spec §2.2).
        Legal source statuses: every non-`succeeded` status — see
        ALLOWED_TRANSITIONS. We restrict at the API layer to a sensible
        subset to avoid users accidentally force-completing a queued
        task (which would skip its real work).
        """
        task = self.store.get_task(task_id)
        if task is None:
            raise TaskNotFound(task_id)

        disallowed_statuses = ["succeeded"]
        if task["status"] in disallowed_statuses:
            raise ValueError(f"Cannot force-complete task in status '{task['status']}'")

        result = self._apply_atomic_action(
            task_id,
            action=Action.FORCE_SUCCEEDED,
            actor="user",
            reason="Forced to succeeded by user",
            extra_updates={
                "finished_at": time.time(),
                "waiting_reason": None,
            },
        )
        if result is None:
            # Race: status changed between check and apply.
            raise ValueError(f"Cannot force-complete task in status '{self.store.get_task(task_id)['status']}'")

        # Write to task log
        self._write_force_log(task_id, "complete")

        return self.store.get_task(task_id)

    # v4 (2026-05-12): force_fail() removed — "强制失败" was never part of the
    # canonical action matrix; the API endpoint /api/tasks/{id}/force-fail is
    # also removed. Users either cancel or let the task fail naturally.

    def force_start(self, task_id: str) -> dict:
        """Force-start a waiting_dependency task, ignoring upstream status."""
        task = self.store.get_task(task_id)
        if task is None:
            raise TaskNotFound(task_id)

        if task["status"] != "waiting_dependency":
            raise ValueError(f"Cannot force-start task in status '{task['status']}' (must be waiting_dependency)")

        is_batch = task.get("task_type") == "batch_run"

        if is_batch:
            # Batch task: resolve items from items_from spec (SQL or file).
            task_logic_param = task.get("logic_param", {}) or {}
            items_from = task_logic_param.get("items_from")

            try:
                items = self._materialize_items_from(items_from)
            except Exception as e:  # noqa: BLE001
                msg = str(e)
                if "UPSTREAM_NO_OUTPUT" in msg or "ArtifactNotReady" in msg:
                    raise ValueError(f"UPSTREAM_NO_OUTPUT: {msg}")
                raise ValueError(f"RESOLVE_ITEMS_FAILED: {msg}")

            # Transition to queued via state machine FORCE_START action.
            applied = self._apply_atomic_action(
                task_id, action=Action.FORCE_START, actor="user",
                reason="Force-started by user (bypassing dependency check)",
                extra_updates={"waiting_reason": None},
            )
            if applied is None:
                raise ValueError("Failed to transition task (may have already been triggered)")

            # Empty result -> mark completed with 0 children.
            # Tick through running -> succeeded for canonical audit lineage.
            if not items:
                self._apply_atomic_action(
                    task_id, action=Action.SCHEDULER_DISPATCH, actor="system",
                    reason="dispatch-then-succeed (force-start, 0 items)",
                    extra_updates={"started_at": time.time()},
                )
                self._apply_atomic_action(
                    task_id, action=Action.NATURAL_COMPLETE, actor="system",
                    reason="0 items resolved",
                    extra_updates={
                        "finished_at": time.time(),
                        "progress": 1.0,
                    },
                )
                self._write_force_log(task_id, "start (0 items, completed)")
                return self.store.get_task(task_id)

            # Create children and execute
            try:
                self.batch_orchestrator.create_children_for_waiting_task(task_id, items)
            except Exception as e:
                self._apply_atomic_action(
                    task_id, action=Action.SCHEDULER_DISPATCH, actor="system",
                    reason="dispatch-then-fail (force-start, create_children)",
                    extra_updates={"started_at": time.time()},
                )
                self._apply_atomic_action(
                    task_id, action=Action.NATURAL_FAIL, actor="system",
                    reason=f"RESOLVE_ITEMS_FAILED: Failed to create children: {e}",
                    extra_updates={
                        "finished_at": time.time(),
                        "error": f"RESOLVE_ITEMS_FAILED: Failed to create children: {e}",
                    },
                )
                raise ValueError(f"RESOLVE_ITEMS_FAILED: {e}")

            # Write to task log
            self._write_force_log(task_id, "start")

            # Execute batch
            executor = self._executors.get(task.get("platform", ""), self._executors["_default"])
            future = executor.submit(self._execute_batch_task, task_id)
            self._futures[task_id] = future
        else:
            # Non-batch task: simply transition and execute via state machine
            applied = self._apply_atomic_action(
                task_id, action=Action.FORCE_START, actor="user",
                reason="Force-started by user (bypassing dependency check)",
                extra_updates={"waiting_reason": None},
            )
            if applied is None:
                raise ValueError("Failed to transition task (may have already been triggered)")

            # Write to task log
            self._write_force_log(task_id, "start")

            # Execute directly
            task_obj = Task(
                task_id=task["task_id"],
                platform=task["platform"],
                task_type=task["task_type"],
                logic_param=task.get("logic_param", {}) or {},
                snapshot_param=task.get("snapshot_param", {}) or {},
                output_dir=task.get("output_dir", ""),
            )
            self._execute_task(task_obj)

        return self.store.get_task(task_id)

    # --- B4: Parent-level action API (spec §2.2) ---

    # Action set that CLEARS cancellation_intent on the parent (spec §1.4).
    # v4: removed 'resume' and 'continue' (parent actions deleted).
    _PARENT_INTENT_CLEARING = {"full_retry", "failed_retry", "force_succeeded"}

    def apply_parent_action(self, parent_id: str, action: str, actor: str = "user") -> dict:
        """Unified entry point for parent-level (batch) operations.

        Spec v4 §2.2 — parent actions fan out to children differently:
          * cancel:           CANCEL all non-terminal children, set intent=true
          * force_succeeded:  FORCE_SUCCEEDED on all non-succeeded children, clear intent
          * failed_retry:     FULL_RETRY only failed/cancelled children, clear intent
          * full_retry:       FULL_RETRY all children, clear intent

        v4 (2026-05-12): pause / resume / continue actions removed.
        Users who want to "continue after cancellation" go through
        full_retry which preserves output_dir.

        After fan-out, aggregate_with_lock recomputes parent status as a
        mirror. Cancellation_intent is set/cleared per spec §1.4.

        Returns the updated parent task dict.
        """
        parent = self.store.get_task(parent_id)
        if parent is None:
            raise TaskNotFound(parent_id)
        if parent.get("parent_task_id") is not None:
            raise ValueError(f"Task {parent_id} is a child, not a parent")
        if parent.get("task_type") != "batch_run":
            raise ValueError(f"apply_parent_action only valid for batch_run; got {parent['task_type']}")

        children = self.store.list_tasks(parent_id=parent_id, limit=100000)

        if action == "cancel":
            return self.cancel_task(parent_id) and self.store.get_task(parent_id) or self.store.get_task(parent_id)
        elif action == "force_succeeded":
            self._fanout_force_succeeded(children)
        elif action == "failed_retry":
            self._fanout_failed_retry(parent_id, children)
        elif action == "full_retry":
            self._fanout_full_retry(parent_id, children)
        else:
            raise ValueError(f"Unknown parent action: {action}")

        # Spec §1.4: clearing actions reset cancellation_intent.
        if action in self._PARENT_INTENT_CLEARING:
            current = bool(parent.get("cancellation_intent"))
            if current:
                self.store.update_cancellation_intent(parent_id, False)
                self.store.insert_transition(
                    task_id=parent_id,
                    from_status=parent["status"],
                    to_status=parent["status"],
                    action="clear_cancellation_intent",
                    actor=actor,
                    reason=f"Cleared by parent action: {action}",
                )

        # Recompute parent aggregate so the mirror status reflects fan-out.
        try:
            agg = aggregate_with_lock(self.store, parent_id)
            if agg.get("changed"):
                self._emit_event("on_task_aggregate_changed", {
                    "task_id": parent_id,
                    "status": agg.get("new_status"),
                    "changed": True,
                })
        except Exception as e:  # noqa: BLE001
            logger.error("[parent-action] aggregate failed for %s: %s", parent_id, e)

        return self.store.get_task(parent_id)

    def _fanout_force_succeeded(self, children: list[dict]) -> None:
        """Force every non-succeeded child to SUCCEEDED.

        Per-child parent aggregates are suppressed (`skip_parent_aggregate`);
        `apply_parent_action` runs a single aggregate at the tail. This
        collapses O(N) redundant aggregations (each of which re-reads all
        N children) into one — see perf-2026-05-22 in `_apply_atomic_action`.
        """
        for c in children:
            if c["status"] != "succeeded":
                self._apply_atomic_action(
                    c["task_id"], action=Action.FORCE_SUCCEEDED, actor="user",
                    reason="Parent force_succeeded action",
                    extra_updates={"finished_at": time.time(), "error": None},
                    skip_parent_aggregate=True,
                )

    def _fanout_failed_retry(self, parent_id: str, children: list[dict]) -> None:
        """Retry only the failed/cancelled children (spec §2.2).

        Historical note: previously delegated to
        `BatchOrchestrator.retry_failed`, which did raw `update_task(... status=
        queued)` writes that BYPASSED the state machine — no transition rows,
        no `cancellation_intent` clearing, and the parent status got force-
        written to QUEUED only to be immediately overwritten by
        `aggregate_with_lock` at the end of `apply_parent_action`, producing a
        race. Now every reset goes through `_apply_atomic_action(FULL_RETRY)`
        so each child gets a proper transition row and the state machine's
        invariants hold. Parent status is updated exactly once by the
        aggregate step at the tail of `apply_parent_action`
        (`skip_parent_aggregate=True` collapses per-child aggregations,
        perf-2026-05-22).
        """
        reset_count = 0
        for c in children:
            if c["status"] in (TaskStatus.FAILED.value, TaskStatus.CANCELLED.value):
                result = self._apply_atomic_action(
                    c["task_id"], action=Action.FULL_RETRY, actor="user",
                    reason="Parent failed_retry action",
                    extra_updates=_RETRY_RESET_UPDATES(),
                    skip_parent_aggregate=True,
                )
                if result is not None:
                    reset_count += 1

        logger.info(
            "[parent-action] failed_retry %s: %d children reset via state machine",
            parent_id, reset_count,
        )

        # Kick the batch loop so the queued children get picked up.
        # Note: _execute_batch_task itself is idempotent w.r.t. already-
        # completed children (it only dispatches queued ones via the
        # dispatch loop's status check), so re-running it is safe.
        if reset_count > 0 and self.batch_orchestrator:
            executor = self._executors.get("_default")
            if executor is not None:
                executor.submit(self._execute_batch_task, parent_id)

    def _fanout_full_retry(self, parent_id: str, children: list[dict]) -> None:
        """Reset every terminal child to QUEUED then re-execute the batch.

        Per-child parent aggregates are suppressed (`skip_parent_aggregate`);
        `apply_parent_action` runs a single aggregate at the tail
        (perf-2026-05-22 — see `_apply_atomic_action`).
        """
        for c in children:
            if c["status"] in ("succeeded", "failed", "partial_succeeded", "cancelled", "interrupted"):
                self._apply_atomic_action(
                    c["task_id"], action=Action.FULL_RETRY, actor="user",
                    reason="Parent full_retry action",
                    extra_updates=_RETRY_RESET_UPDATES(),
                    skip_parent_aggregate=True,
                )
        # Also reset the parent's own running-metric columns so the parent
        # row in the task list doesn't keep showing the previous run's
        # finished_at / record_count / total_bytes during the new run. The
        # state-machine aggregate will refresh `status` on its own; we only
        # touch the metric columns here.
        self.store.update_task(parent_id, {
            "started_at": None,
            "finished_at": None,
            "record_count": 0,
            "total_bytes": 0,
            "progress": 0.0,
            "last_heartbeat": None,
            "result_files": [],
            "error": None,
        })
        if self.batch_orchestrator:
            executor = self._executors.get("_default")
            executor.submit(self._execute_batch_task, parent_id)

    def _write_force_log(self, task_id: str, action: str) -> None:
        """Write a force operation log entry to the task's log file."""
        log_dir = self.data_root / "logs" / "tasks" / time.strftime("%Y-%m-%d")
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / f"{task_id}.log"
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        log_line = f"[{timestamp}] [FORCE] Force {action} by user at {timestamp}\n"
        try:
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(log_line)
        except OSError:
            pass

    def _get_task_cookie_id(self, task: Task) -> str | None:
        """Get the cookie_id currently associated with a task.

        Looks up the platform's best cookie from the throttle system.
        Returns cookie_id string ("{platform}:{label}") or None.
        """
        throttle = get_cookie_throttle()
        # If task has an override cookie path, find its cookie_id
        override_path = task.snapshot_param.get("_override_cookie_path")
        if override_path:
            states = throttle.get_platform_states(task.platform)
            for state in states:
                if state.path == override_path:
                    return state.cookie_id
            return None

        # Otherwise, use the best cookie for the platform
        state = throttle.select_best_cookie(task.platform)
        if state:
            return state.cookie_id
        return None

    def _on_task_progress(self, task_id: str, progress: float) -> None:
        """Callback when task reports progress. Also updates heartbeat and metrics."""
        updates = {"progress": progress, "last_heartbeat": time.time()}
        # Also update record_count and total_bytes from context if available
        ctx = self._contexts.get(task_id)
        if ctx:
            updates["record_count"] = ctx.record_count
            updates["total_bytes"] = ctx.total_bytes
        self.store.update_task(task_id, updates)

    def _on_task_log(self, task_id: str, log_line: str) -> None:
        """Callback when task writes a log line (for WebSocket broadcast). Also updates heartbeat."""
        # Throttle heartbeat updates to max once per 5 seconds per task
        now = time.time()
        last = getattr(self, '_last_heartbeat_update', {}).get(task_id, 0)
        if now - last >= 5:
            if not hasattr(self, '_last_heartbeat_update'):
                self._last_heartbeat_update = {}
            self._last_heartbeat_update[task_id] = now
            updates = {"last_heartbeat": now}
            ctx = self._contexts.get(task_id)
            if ctx:
                updates["record_count"] = ctx.record_count
                updates["total_bytes"] = ctx.total_bytes
            self.store.update_task(task_id, updates)
            # Throughput sampling: persist a (task_id, ts, record_count) snapshot.
            # Reuses the 5s throttle so we get one sample per task per ~5s while
            # the task is producing log output. Sampler is best-effort: a DB
            # error here must NOT break log forwarding.
            if ctx:
                try:
                    self.store.add_record_sample(task_id, now, ctx.record_count)
                    # Lazy cleanup: 1% chance per sample, keeping ~25h of history.
                    # Avoids a dedicated background loop while bounding table size.
                    import random
                    if random.random() < 0.01:
                        self.store.cleanup_old_samples(now - 25 * 3600)
                except Exception as e:
                    logger.debug("[sample] add_record_sample failed for %s: %s", task_id, e)

    def _record_sampler_loop(self) -> None:
        """Periodic throughput sampler — runs in a dedicated daemon thread.

        WHY this exists separately from _on_task_log:
          _on_task_log only fires when a task calls ctx.log(). Crawlers that
          only ctx.write_record() (no mid-execution logging) never trigger
          _on_task_log -> 0 mid-run samples -> the speed chart shows all
          data dumped at the terminal sample (the "completed in one instant"
          bug). Active polling fixes this: we read ctx.record_count directly
          regardless of whether the task chose to log.

        Dual-track sampling per tick:
          (A) Per-task sample: one (task_id, now, ctx.record_count) row per
              active ctx in `record_samples`. Powers the task-detail
              drill-down chart. Short-lived children (<5s) will only have
              anchor+terminal — expected limitation.
          (B) Global flux sample: one (now, total) row in
              `global_flux_samples` where total = self.flux.snapshot()
              (an in-memory monotonic counter ticked by every
              TaskContext.write_record). Drives the dashboard speed chart
              AND the lifetime "累计下载条数" stat card. Decoupled from
              `tasks.record_count` so retry / archive / purge CANNOT cause
              a phantom dip; the curve is monotonic by construction.

        Loop:
          - Wake every 5s (interruptible via _shutdown_flag).
          - Persist (A) for every active TaskContext.
          - Persist (B) once per tick (sample + counter).
          - Lazy cleanup of >25h-old samples every 100 ticks (~8min).
          - Best-effort: any exception logged at DEBUG; loop continues.
        """
        SAMPLE_PERIOD_SEC = 5.0
        CLEANUP_RETENTION_SEC = 25 * 3600
        tick = 0
        while not self._shutdown_flag.is_set():
            try:
                now = time.time()
                # list() copies refs so other threads can mutate _contexts
                # without affecting iteration.
                active_items = list(self._contexts.items())

                # (A) Per-task samples (drives task-detail drill-down chart).
                # Also flush the *live* counter into tasks.record_count /
                # tasks.total_bytes so list_tasks() (which aggregates batch
                # parents from these columns) shows real-time progress for
                # quiet crawlers that don't call ctx.log() — those crawlers
                # never trip _on_task_log and would otherwise leave the DB
                # column at 0 until the task transitions to a terminal state.
                # See bug 2026-05-22: batch parent record_count was always 0
                # mid-run because every child only ctx.write_record()'s.
                for task_id, ctx in active_items:
                    try:
                        cnt = int(ctx.record_count or 0)
                        self.store.add_record_sample(task_id, now, cnt)
                        # Best-effort heartbeat write of the running counters.
                        # Keep this in the same try-block as the sample so any
                        # transient SQLite contention is logged at DEBUG and
                        # the loop keeps going.
                        try:
                            self.store.update_task(task_id, {
                                "record_count": cnt,
                                "total_bytes": int(ctx.total_bytes or 0),
                                "last_heartbeat": now,
                            })
                        except Exception as _e:
                            logger.debug("[sampler] heartbeat write %s: %s", task_id, _e)
                    except Exception as e:
                        logger.debug("[sampler] task %s: %s", task_id, e)

                # (B) Global flux sample — drives the dashboard speed chart
                # AND the "lifetime downloaded count" stat card. Sourced
                # ENTIRELY from `self.flux` (an in-memory monotonic counter
                # incremented inside TaskContext.write_record). Decoupled
                # from `tasks.record_count`, so retry / archive / purge
                # CANNOT cause a phantom dip — the curve is monotonic by
                # construction. See crawlhub/core/flux.py for design notes.
                try:
                    flux_total = self.flux.snapshot()
                    self.store.add_global_flux_sample(now, flux_total)
                    # Persist the counter every tick so we recover the
                    # lifetime total on daemon restart with at most ~5s loss.
                    self.store.update_global_flux_counter(flux_total, now)
                except Exception as e:
                    logger.debug("[sampler] global flux sample failed: %s", e)

                tick += 1
                if tick % 100 == 0:
                    try:
                        self.store.cleanup_old_samples(now - CLEANUP_RETENTION_SEC)
                        self.store.cleanup_old_global_flux_samples(now - CLEANUP_RETENTION_SEC)
                    except Exception as e:
                        logger.debug("[sampler] cleanup failed: %s", e)
            except Exception as e:
                logger.warning("[sampler] tick error: %s", e)
            # Interruptible sleep: returns True if shutdown_flag was set
            # during wait, so we exit promptly on graceful_shutdown.
            if self._shutdown_flag.wait(timeout=SAMPLE_PERIOD_SEC):
                break
        logger.info("[sampler] record sampler thread exited cleanly.")

    def _emit_event(self, event_type: str, data: dict) -> None:
        """Emit an event to all registered listeners."""
        for listener in self._event_listeners:
            try:
                listener(event_type, data)
            except Exception as e:
                logger.error("[ERR] Event listener error: %s", e)

    def register_event_listener(self, listener) -> None:
        """Register an event listener callback."""
        self._event_listeners.append(listener)

    def graceful_shutdown(self) -> None:
        """Graceful shutdown: stop accepting -> signal cancel -> wait workers -> DB cleanup.

        Order matters: workers MUST finish (release SQLite write locks) BEFORE
        the main thread issues bulk_update_status, otherwise we race against
        them and hit `database is locked`.
        """
        logger.info("[INFO] Initiating graceful shutdown...")

        # Phase 0: Stop the plan scheduler FIRST so no new plan-fired tasks
        # appear while we're tearing down workers/contexts. wait=True blocks
        # until any in-flight fire() returns; per spec it does NOT interrupt
        # mid-fire because that would leave dangling task IDs.
        if self.plan_scheduler is not None:
            try:
                self.plan_scheduler.shutdown(wait=True)
            except Exception as e:
                logger.warning("[shutdown] plan_scheduler shutdown error: %s", e)

        # Phase 1: Reject new requests
        self._shutdown_flag.set()

        # Phase 1b: Join the record sampler thread. It checks _shutdown_flag
        # in its sleep loop, so wake-up is bounded by SAMPLE_PERIOD_SEC (5s).
        # Joining here (before workers finish) is safe: the sampler only reads
        # from _contexts and writes to record_samples table — it doesn't touch
        # the same task rows the workers are updating.
        if self._record_sampler_thread is not None:
            try:
                self._record_sampler_thread.join(timeout=6.0)
                if self._record_sampler_thread.is_alive():
                    logger.warning("[shutdown] record sampler did not exit in 6s; abandoning")
            except Exception as e:
                logger.debug("[shutdown] sampler join error: %s", e)

        # Phase 2: Signal cancel to all running task contexts so cooperative
        # checkpoints (ctx.sleep / ctx.check_cancelled) wake up immediately.
        for task_id, ctx in list(self._contexts.items()):
            try:
                ctx.cancel()
            except Exception:
                pass

        # Phase 3: Soft-wait running futures up to 15s. After cancel signal
        # most workers should exit quickly via cooperative checkpoints.
        deadline = time.time() + 15
        running_futures = list(self._futures.values())
        for future in running_futures:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            try:
                future.result(timeout=max(0.1, remaining))
            except Exception:
                pass

        # Phase 4: Shutdown executors and WAIT for workers to actually exit.
        # cancel_futures=True drops queued tasks; wait=True ensures any
        # still-running worker finishes its current DB write and releases
        # the SQLite lock before we proceed.
        # NOTE: ThreadPoolExecutor.shutdown has no timeout param; if a worker
        # is stuck in a blocking HTTP call this can hang. We accept that risk
        # because the alternative (skipping wait) corrupts shutdown state.
        for executor in self._executors.values():
            try:
                executor.shutdown(wait=True, cancel_futures=True)
            except TypeError:
                # Python < 3.9 fallback (no cancel_futures)
                executor.shutdown(wait=True)
            except Exception as e:
                logger.warning(f"[WARN] Executor shutdown error: {e}")

        # ════════════════════════════════════════════════════════════════
        #  Phase 4.5: Shutdown all browser sessions (R7 §5.5)
        # ────────────────────────────────────────────────────────────────
        #  时机：在 Phase 4 executor.shutdown 之后（worker 已退或被强制取消），
        #  Phase 5 DB cleanup 之前（chrome 子进程可能持有 cookie 文件 fd）。
        #
        #  策略：直接关所有 chrome，不等 hold 释放。in-hold task 已被 Phase 3/4
        #  cancel；走到这里说明它们要么自然退出要么硬终止——chrome 关闭对应的
        #  TargetClosedError 由 failure_detector 在 _shutdown_flag 期间识别为
        #  NETWORK_ERROR（不污染 anti_crawl 检测）。
        # ════════════════════════════════════════════════════════════════
        if self._browser_runner is not None and self._browser_managers:
            logger.info(
                "[shutdown] closing %d browser manager(s)",
                len(self._browser_managers),
            )
            for cfg_key, manager in list(self._browser_managers.items()):
                try:
                    self._browser_runner.run(manager.close_all_sessions())
                except Exception as e:
                    logger.warning(
                        "[shutdown] manager close exc=%s key=%s", e, cfg_key,
                    )
            try:
                self._browser_runner.shutdown(cancel_timeout=10.0)
            except Exception as e:
                logger.warning("[shutdown] runner shutdown exc=%s", e)
            self._browser_managers.clear()
            self._browser_runner = None

        # Phase 5: Now safe to write DB - no worker holds the write lock.
        # Wrap in try/except so a single DB hiccup can't block exit_marker.
        try:
            self.store.bulk_update_status(
                from_statuses=["running"],
                to_status="cancelled",
                error="daemon shutdown forced",
            )
        except Exception as e:
            logger.warning(f"[WARN] bulk_update_status during shutdown failed: {e}")

        # Write exit marker
        self._write_exit_marker(clean=True, reason="graceful_shutdown")

        # Remove PID file
        pid_path = self.data_root / "daemon.pid"
        pid_path.unlink(missing_ok=True)

        logger.info("[OK] Daemon shutdown complete.")

        # Force process exit - uvicorn's event loop won't stop on its own
        # when shutdown is triggered via HTTP endpoint (not signal).
        # Use os._exit to bypass atexit handlers that might hang.
        import os as _os
        _os._exit(0)

    def _write_exit_marker(self, clean: bool, reason: str) -> None:
        """Write exit_marker.json."""
        marker_path = self.data_root / "exit_marker.json"
        with open(marker_path, "w", encoding="utf-8") as f:
            json.dump({
                "clean": clean,
                "exited_at": time.time(),
                "reason": reason,
            }, f)

    @property
    def uptime(self) -> float:
        return time.time() - self._started_at

    @property
    def is_shutting_down(self) -> bool:
        return self._shutdown_flag.is_set()


# --- Task Log Writer (stdout/stderr redirect) ---

class _TaskLogWriter:
    """A file-like object that captures print() output from underlying crawlers.

    Writes to both the task log file and notifies the TaskContext for WebSocket broadcast.
    Uses UTF-8 encoding with errors='replace' to handle any character safely.
    """

    def __init__(self, log_file, ctx: TaskContext, stream_name: str = "STDOUT"):
        self._log_file = log_file
        self._ctx = ctx
        self._stream_name = stream_name

    def write(self, text: str) -> int:
        if not text or text == "\n":
            return len(text) if text else 0
        try:
            self._log_file.write(text)
            self._log_file.flush()
            # Also notify via TaskContext for WebSocket broadcast
            if text.strip():
                self._ctx._on_log(self._ctx.task_id, text) if self._ctx._on_log else None
        except (OSError, UnicodeEncodeError):
            pass
        return len(text)

    def flush(self) -> None:
        try:
            self._log_file.flush()
        except OSError:
            pass

    def fileno(self):
        return self._log_file.fileno()

    @property
    def buffer(self):
        """Provide a buffer attribute for compatibility with code that accesses sys.stdout.buffer."""
        return self

    def readable(self):
        return False

    def writable(self):
        return True

    def seekable(self):
        return False

    @property
    def encoding(self):
        return "utf-8"

    @property
    def errors(self):
        return "replace"


# --- Thread-aware stdout/stderr proxy ---
#
# Why this exists
# ---------------
# The daemon used to do ``sys.stdout = task_writer`` at the start of each
# task, then restore it in ``finally``. That is **process-global** state in
# CPython — every other thread (including FastAPI HTTP workers) sees the
# same ``sys.stdout`` / ``sys.stderr``. The fallout:
#   - HTTP handlers calling crawler code (or even just the platform service
#     constructor) print into the *currently-running task's* log file.
#   - Worse: when the task ends and the file is closed, any later HTTP
#     write hits ``ValueError: I/O operation on closed file``.
#   - Concurrent tasks corrupt each other's restoration order.
#
# Fix: install a single proxy *once*; route via ``threading.local`` so each
# thread sees its own writer, with HTTP threads (no writer registered)
# always falling through to the original ``sys.__stdout__`` / ``sys.__stderr__``.

_thread_streams = threading.local()


class _ThreadAwareStream:
    """A sys.stdout/stderr stand-in that routes writes per-thread.

    Worker threads register a per-task writer via ``register(writer)`` at
    task start and ``unregister()`` at task end.  Threads without a
    registered writer fall through to the *real* underlying stream (kept
    in ``self._fallback``).  Because we install proxies on the module-level
    ``sys.stdout`` / ``sys.stderr`` exactly once at daemon startup, no
    thread ever sees the wrong writer regardless of how many tasks run
    concurrently.
    """

    __slots__ = ("_fallback", "_attr")

    def __init__(self, fallback, attr_name: str):
        # _fallback: the real underlying stream (sys.__stdout__ etc.).
        # _attr: which thread-local attribute holds *this* stream's writer
        # ("stdout" or "stderr") so stdout and stderr can be registered
        # independently.
        self._fallback = fallback
        self._attr = attr_name

    def _active(self):
        return getattr(_thread_streams, self._attr, None)

    def write(self, text: str) -> int:
        target = self._active() or self._fallback
        try:
            return target.write(text)
        except (OSError, ValueError):
            # Writer file got closed under us (e.g. task tore down between
            # the active-check and the write).  Fall back to the real
            # underlying stream so we never blow up the caller.
            try:
                return self._fallback.write(text)
            except Exception:
                return len(text) if text else 0

    def flush(self) -> None:
        target = self._active() or self._fallback
        try:
            target.flush()
        except (OSError, ValueError):
            try:
                self._fallback.flush()
            except Exception:
                pass

    def fileno(self):
        target = self._active() or self._fallback
        return target.fileno()

    @property
    def buffer(self):
        target = self._active() or self._fallback
        return getattr(target, "buffer", target)

    def readable(self):
        return False

    def writable(self):
        return True

    def seekable(self):
        return False

    @property
    def encoding(self):
        target = self._active() or self._fallback
        return getattr(target, "encoding", "utf-8")

    @property
    def errors(self):
        target = self._active() or self._fallback
        return getattr(target, "errors", "replace")

    def isatty(self):
        target = self._active() or self._fallback
        return getattr(target, "isatty", lambda: False)()


def install_thread_aware_streams() -> None:
    """Install thread-aware stdout/stderr proxies (idempotent).

    Safe to call multiple times — only the first call replaces the streams.
    Must be called from the main thread before any worker thread starts
    redirecting output.

    The proxy's *fallback* stream is the **current** ``sys.stdout`` /
    ``sys.stderr`` at install time, NOT ``sys.__stdout__`` / ``sys.__stderr__``.
    This matters on Windows daemons: ``_ensure_utf8_stdio`` wraps stdio in
    a UTF-8 TextIOWrapper to survive emoji from crawler output, and the
    fallback must inherit that wrapper — using the raw originals would
    re-introduce GBK crashes.
    """
    current_stdout = sys.stdout
    current_stderr = sys.stderr
    if not isinstance(current_stdout, _ThreadAwareStream):
        sys.stdout = _ThreadAwareStream(current_stdout, "stdout")
    if not isinstance(current_stderr, _ThreadAwareStream):
        sys.stderr = _ThreadAwareStream(current_stderr, "stderr")


def register_thread_streams(stdout_writer, stderr_writer) -> None:
    """Bind *this thread*'s stdout/stderr writers (called by worker threads)."""
    _thread_streams.stdout = stdout_writer
    _thread_streams.stderr = stderr_writer


def unregister_thread_streams() -> None:
    """Clear *this thread*'s stdout/stderr writers (worker thread teardown)."""
    for attr in ("stdout", "stderr"):
        if hasattr(_thread_streams, attr):
            delattr(_thread_streams, attr)


# --- PID file management ---

def check_pid_file() -> int | None:
    """Check if daemon.pid exists and process is alive.

    Returns:
        PID if daemon is running, None if not (stale lock cleaned up).
    """
    pid_path = get_data_root() / "daemon.pid"
    if not pid_path.exists():
        return None

    try:
        pid = int(pid_path.read_text().strip())
    except (ValueError, OSError):
        # Corrupt PID file, remove it
        pid_path.unlink(missing_ok=True)
        return None

    # Check if process exists
    if _is_process_alive(pid):
        return pid
    else:
        # Stale lock
        logger.warning("[WARN] stale pid file overwritten (was pid=%d)", pid)
        pid_path.unlink(missing_ok=True)
        return None


def write_pid_file() -> None:
    """Write current PID to daemon.pid."""
    pid_path = get_data_root() / "daemon.pid"
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(str(os.getpid()))


def _is_process_alive(pid: int) -> bool:
    """Cross-platform check if a process is alive."""
    if sys.platform == "win32":
        import ctypes
        kernel32 = ctypes.windll.kernel32
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            # OpenProcess can succeed for zombie/terminated processes.
            # Must check exit code to confirm it's truly alive.
            exit_code = ctypes.c_ulong()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return exit_code.value == STILL_ACTIVE
            return False
        finally:
            kernel32.CloseHandle(handle)
    else:
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ProcessLookupError):
            return False


# --- Daemon startup ---

def _check_version_change() -> None:
    """Detect daemon version upgrades by comparing with last_version.json.

    On first start or upgrade, writes the current version to
    ``~/.crawlhub/last_version.json``.  On subsequent starts, if the
    running version differs, logs an [UPGRADE] notice with both versions.

    This is a purely local check — no network calls.
    """
    from crawlhub._version import __version__
    version_file = get_data_root() / "last_version.json"

    current_version = __version__
    last_version: str | None = None

    if version_file.exists():
        try:
            data = json.loads(version_file.read_text(encoding="utf-8"))
            last_version = data.get("version")
        except Exception:
            pass

    if last_version is None:
        # First start ever (or file was deleted)
        logger.info("[VERSION] First start with version %s", current_version)
    elif last_version != current_version:
        logger.info(
            "[UPGRADE] CrawlHub upgraded: %s -> %s",
            last_version, current_version,
        )
    else:
        logger.info("[VERSION] CrawlHub %s (unchanged)", current_version)

    # Always write current version (also records timestamp)
    try:
        version_file.write_text(
            json.dumps({
                "version": current_version,
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:
        logger.warning("[VERSION] Failed to write last_version.json: %s", exc)


def _ensure_utf8_stdio() -> None:
    """Ensure sys.stdout/stderr use UTF-8 encoding.

    On Windows (GBK/cp936), underlying crawler code may print emoji or
    other Unicode characters that cannot be encoded in GBK, causing
    UnicodeEncodeError and crashing the entire task thread.
    This sets process-level UTF-8 with errors='replace' to prevent crashes.
    """
    if sys.platform == "win32":
        try:
            if hasattr(sys.stdout, 'buffer'):
                sys.stdout = io.TextIOWrapper(
                    sys.stdout.buffer, encoding='utf-8', errors='replace',
                    line_buffering=True,
                )
            if hasattr(sys.stderr, 'buffer'):
                sys.stderr = io.TextIOWrapper(
                    sys.stderr.buffer, encoding='utf-8', errors='replace',
                    line_buffering=True,
                )
        except Exception:
            # If wrapping fails, at least set error handler
            pass


def start_daemon(host: str = "127.0.0.1", port: int = 8787) -> None:
    """Start the CrawlHub Daemon server."""
    global _daemon

    # ── R7 Observability assertion (spec §3.4) ─────────────────────────────
    # If install_all() did not run before now, the entry-point patch order is
    # broken — every transport patch will be silently no-op. Fail fast.
    from crawlhub.core.observability import is_installed as _r7_is_installed
    assert _r7_is_installed(), (
        "[R7] observability.install_all() did not run before daemon start. "
        "Check crawlhub/cli/__init__.py and crawlhub/__main__.py — install_all() "
        "must be the first executable line of the entry-point module."
    )
    # ────────────────────────────────────────────────────────────────────────

    # Ensure UTF-8 stdio to prevent GBK encoding crashes on Windows
    _ensure_utf8_stdio()

    # Install thread-aware stdout/stderr proxies so per-task log redirection
    # only affects the worker thread that runs that task — not HTTP request
    # threads or other concurrent workers.  See _ThreadAwareStream docstring.
    install_thread_aware_streams()

    # Configure logging - output to both stderr and daemon.log file
    log_dir = get_data_root() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "daemon.log"

    # Setup root logger with both file and stream handlers
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    # File handler - append to daemon.log
    file_handler = logging.FileHandler(str(log_file), mode="a", encoding="utf-8")
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(logging.Formatter(
        "[%(asctime)s] %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    ))
    root_logger.addHandler(file_handler)

    # Stream handler - output to stderr (captured by spawn_daemon redirect)
    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setLevel(logging.INFO)
    stream_handler.setFormatter(logging.Formatter(
        "[%(asctime)s] %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    ))
    root_logger.addHandler(stream_handler)

    logger.info("=" * 60)
    logger.info("[DAEMON] CrawlHub Daemon starting (pid=%d)", os.getpid())
    logger.info("=" * 60)

    # ── Version change detection ────────────────────────────────────────────
    # Compare current __version__ against the last version that started this
    # daemon.  On upgrade, log a one-shot notice so operators know the daemon
    # is running new code after a `pip install --force-reinstall`.
    _check_version_change()

    # Load config
    config = load_config()
    config.host = host
    config.port = port

    # Check for existing daemon
    existing_pid = check_pid_file()
    if existing_pid is not None:
        print(f"[ERR] Daemon already running at pid={existing_pid}", file=sys.stderr)
        sys.exit(1)

    # Write PID file
    write_pid_file()

    # Initialize daemon
    _daemon = CrawlHubDaemon(config)
    _daemon.initialize()
    _daemon.startup_recovery()

    # Initialize NotificationService
    from crawlhub.core.notifications import NotificationService
    _daemon.notification_service = NotificationService(_daemon)
    _daemon.notification_service.start()

    # Initialize PlanScheduler (must come AFTER notification_service so
    # the on_plan_step_submit_failed event has a registered sink, but
    # BEFORE FastAPI starts accepting requests so manual /run calls work).
    from crawlhub.core.plan_scheduler import PlanScheduler
    _daemon.plan_scheduler = PlanScheduler(_daemon)
    _daemon.plan_scheduler.start()

    logger.info("[OK] CrawlHub Daemon starting on %s:%d", host, port)

    # Import and create FastAPI app
    from crawlhub.api.app import create_app

    app = create_app(_daemon)

    # Setup signal handlers for graceful shutdown
    def _signal_handler(signum, frame):
        if _daemon:
            _daemon.graceful_shutdown()
        sys.exit(0)

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    # Run uvicorn
    uvicorn.run(app, host=host, port=port, log_level="info")


# --- Exceptions ---

class DaemonShuttingDown(Exception):
    """Raised when daemon is shutting down and rejects new requests."""
    pass


class DiskSpaceLow(Exception):
    """Raised when disk space is below threshold."""
    def __init__(self, free_bytes: int):
        self.free_bytes = free_bytes
        super().__init__(f"Disk space low: {free_bytes // (1024*1024)} MB free")


class TaskNotFound(Exception):
    """Raised when task_id doesn't exist."""
    def __init__(self, task_id: str):
        self.task_id = task_id
        super().__init__(f"Task not found: {task_id}")


class TaskNotRetryable(Exception):
    """Raised when task is not in a retryable state."""
    def __init__(self, task_id: str, current_status: str):
        self.task_id = task_id
        self.current_status = current_status
        super().__init__(f"Task {task_id} not retryable (status={current_status})")

