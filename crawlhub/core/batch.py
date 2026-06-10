"""Batch Orchestrator - generic batch scheduling layer for CrawlHub.

Turns any single-item action into a batch operation by creating a parent task
with N child tasks, managing concurrency, delays, failure strategies, and
progress aggregation.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, Future, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from crawlhub.core.models import Task, TaskStatus
from crawlhub.core.param_snapshot import (
    build_batch_snapshot,
    build_task_snapshot,
    finalize_batch_snapshot,
)

logger = logging.getLogger("crawlhub.batch")


# --- Input Source Resolution ---

def resolve_items(
    items: list[str] | None = None,
    items_from: dict[str, Any] | None = None,
    store=None,
) -> list[str]:
    """Resolve batch input items.

    Supported input modes:
    1. Direct array: ``items`` parameter.
    2. External file: ``items_from = {"file": "/abs/path/items.txt"}``
       (one value per line).

    The legacy task-id mode (``items_from = {"task_id": ..., "field": ...}``)
    is **rejected** here — it has been superseded by the ``sources/sql/field``
    SQL pipeline, which is handled in ``BatchOrchestrator.create_batch`` via
    the SQL validator/runner. Callers must not pre-resolve those items here.

    Args:
        items: Direct list of item values.
        items_from: Dict with ``file`` key (only).
        store: Unused; kept for backwards-compat signature.

    Returns:
        List of resolved item strings (deduplicated, order preserved).

    Raises:
        ValueError: invalid input source or no valid items.
    """
    resolved: list[str] = []

    if items and len(items) > 0:
        # Mode 1: Direct array
        resolved = [str(i) for i in items if i is not None and str(i).strip()]

    elif items_from:
        # Reject legacy task_id+field mode at the door — SQL mode is the only
        # way to consume upstream task output now.
        if "task_id" in items_from:
            raise ValueError(
                "items_from {task_id, field} is no longer supported. "
                "Use the SQL pipeline: items_from = {sources, sql, field, dedup?}."
            )

        if "file" in items_from:
            file_path = Path(items_from["file"])
            if not file_path.exists():
                raise ValueError(f"Items file not found: {file_path}")
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            resolved.append(line)
            except OSError as e:
                raise ValueError(f"Failed to read items file: {e}")

        elif "sources" in items_from:
            # SQL mode is resolved in create_batch / daemon, not here.
            raise ValueError(
                "items_from with 'sources' must be resolved by the batch "
                "orchestrator (BatchOrchestrator), not by resolve_items()."
            )
        else:
            raise ValueError("items_from must contain 'file' or use SQL mode {sources, sql, field}")
    else:
        raise ValueError("Either 'items' or 'items_from' must be provided")

    if not resolved:
        raise ValueError("No valid items to process")

    # Deduplicate while preserving order
    seen = set()
    deduped = []
    for item in resolved:
        if item not in seen:
            seen.add(item)
            deduped.append(item)

    if len(deduped) < len(resolved):
        logger.info(f"Deduplicated items: {len(resolved)} -> {len(deduped)}")

    return deduped


# --- Configuration ---

@dataclass
class BatchConfig:
    """Configuration for a batch run."""

    platform: str = ""
    action: str = ""
    item_key: str = ""
    items: list[str] = field(default_factory=list)
    common_params: dict[str, Any] = field(default_factory=dict)
    concurrency: int = 1
    fail_strategy: str = "continue"  # "continue" or "abort"
    cookie_policy: dict[str, Any] = field(default_factory=dict)
    items_from_meta: dict[str, Any] | None = None  # Preserved for traceability
    allow_partial_upstream: bool = True  # Allow downstream to start even if upstream has partial failures

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BatchConfig:
        return cls(
            platform=data.get("platform", ""),
            action=data.get("action", ""),
            item_key=data.get("item_key", ""),
            items=data.get("items", []),
            common_params=data.get("common_params", {}),
            concurrency=data.get("concurrency", 1),
            fail_strategy=data.get("fail_strategy", "continue"),
            cookie_policy=data.get("cookie_policy", {}),
            items_from_meta=data.get("items_from_meta"),
            allow_partial_upstream=data.get("allow_partial_upstream", True),
        )


# --- Dependency Utilities ---

_MAX_DEPENDENCY_DEPTH = 10


def snapshot_read_upstream_output(task_id: str, field_name: str, store) -> list[str]:
    """Deprecated: kept only for forced-start of legacy waiting tasks.

    The SQL pipeline replaces this for all new tasks. We keep the function
    body intact so any pre-existing ``waiting_dependency`` rows from the old
    schema can still be force-started, but new code should use
    ``crawlhub.core.sql_runner.run_items_from`` instead.
    """
    task = store.get_task(task_id)
    if task is None:
        raise ValueError(f"Source task not found: {task_id}")

    output_dir = task.get("output_dir", "")
    data_path = Path(output_dir) / "data.jsonl" if output_dir else None

    if not data_path or not data_path.exists():
        raise ValueError(f"UPSTREAM_NO_OUTPUT: Source task {task_id} has no output")

    resolved: list[str] = []
    try:
        with open(data_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                    value = record.get(field_name)
                    if value is not None and str(value).strip():
                        resolved.append(str(value))
                except json.JSONDecodeError:
                    # Tolerate incomplete last line (snapshot read)
                    continue
    except OSError as e:
        raise ValueError(f"Failed to read source task data: {e}")

    if not resolved:
        raise ValueError(f"UPSTREAM_NO_OUTPUT: No valid items extracted from task {task_id}")

    # Deduplicate while preserving order
    seen = set()
    deduped = []
    for item in resolved:
        if item not in seen:
            seen.add(item)
            deduped.append(item)

    if len(deduped) < len(resolved):
        logger.info(f"Deduplicated upstream items: {len(resolved)} -> {len(deduped)}")

    return deduped


def check_circular_dependency(task_id: str, upstream_task_ids: list[str], store) -> str | None:
    """Check for circular dependencies by tracing each upstream chain.

    Args:
        task_id: The new task being created (to detect direct/indirect cycles).
        upstream_task_ids: All immediate upstream task IDs.
        store: StateStore instance.

    Returns:
        ``None`` if no cycle detected, or one of the error codes below:
        - ``'CIRCULAR_DEPENDENCY'`` if any upstream chain loops back to ``task_id``.
        - ``'DEPENDENCY_DEPTH_EXCEEDED'`` if any chain exceeds the max depth.
    """
    # BFS over the union of upstream chains so we don't redo shared ancestors.
    visited: set[str] = {task_id}
    frontier: list[tuple[str, int]] = [(uid, 1) for uid in upstream_task_ids if uid]

    while frontier:
        current, depth = frontier.pop()
        if depth > _MAX_DEPENDENCY_DEPTH:
            return "DEPENDENCY_DEPTH_EXCEEDED"
        if current in visited:
            return "CIRCULAR_DEPENDENCY"
        visited.add(current)

        upstream_task = store.get_task(current)
        if upstream_task is None:
            continue  # Broken chain segment; treat as no-cycle for this branch.
        for nxt in upstream_task.get("depends_on_task_ids") or []:
            if nxt:
                frontier.append((nxt, depth + 1))

    return None


def check_upstreams_and_decide(
    upstream_task_ids: list[str],
    allow_partial_upstream: bool,
    store,
) -> dict[str, Any]:
    """Inspect every upstream task and decide whether to run, wait, or fail.

    Decision rules (across the *set* of upstreams):
      - Any upstream not found  -> error UPSTREAM_NOT_FOUND.
      - Any upstream still in-flight (pending/running/waiting/interrupted)
        -> wait. ``waiting_reason`` aggregates all not-yet-ready upstreams.
      - Any upstream is FAILED/CANCELLED:
          * ``allow_partial_upstream=False`` -> error UPSTREAM_FAILED.
          * ``allow_partial_upstream=True``  -> treat that upstream as ready
            *iff* it actually wrote some output; otherwise error
            UPSTREAM_FAILED_NO_OUTPUT.
      - Otherwise (all readable) -> ``ready``. The caller is responsible for
        actually running the SQL pipeline against the readable upstreams.

    Returns one of:
        - ``{"action": "ready"}``
        - ``{"action": "wait", "waiting_reason": ...}``
        - ``{"action": "error", "code": ..., "message": ...}``
    """
    if not upstream_task_ids:
        return {"action": "ready"}

    waiting_reasons: list[str] = []
    for upstream_id in upstream_task_ids:
        upstream = store.get_task(upstream_id)
        if upstream is None:
            return {
                "action": "error",
                "code": "UPSTREAM_NOT_FOUND",
                "message": f"Source task not found: {upstream_id}",
            }

        status = upstream.get("status", "")

        # Recycle-bin semantics: an archived upstream is OFF-LIMITS as a
        # dependency source. Files may still be on disk (auto-purge hasn't
        # run yet) but the user explicitly removed it from the active set
        # — silently reading from it would surprise them. Surface a clear
        # error so the caller restores or picks a different source.
        is_archived = upstream.get("archived_at") is not None
        if is_archived:
            return {
                "action": "error",
                "code": "UPSTREAM_ARCHIVED",
                "message": f"Upstream {upstream_id} is in the recycle bin; restore it or pick a different source.",
            }

        if status in (TaskStatus.SUCCEEDED.value, TaskStatus.PARTIAL_SUCCEEDED.value):
            continue  # ready

        if status in (TaskStatus.QUEUED.value, TaskStatus.RUNNING.value, "waiting_dependency", TaskStatus.INTERRUPTED.value):
            waiting_reasons.append(f"{upstream_id}:{status}")
            continue

        if status in (TaskStatus.FAILED.value, TaskStatus.CANCELLED.value):
            if not allow_partial_upstream:
                return {
                    "action": "error",
                    "code": "UPSTREAM_FAILED",
                    "message": f"Upstream {upstream_id} {status}, partial upstream not allowed",
                }
            # allow_partial=True: tolerate as long as the upstream produced data.
            output_dir = upstream.get("output_dir", "")
            data_path = Path(output_dir) / "data.jsonl" if output_dir else None
            if not data_path or not data_path.exists() or data_path.stat().st_size == 0:
                return {
                    "action": "error",
                    "code": "UPSTREAM_FAILED_NO_OUTPUT",
                    "message": f"Upstream {upstream_id} {status} with no usable output",
                }
            continue  # readable enough, treat as ready

        # Unknown status -> conservatively wait.
        waiting_reasons.append(f"{upstream_id}:{status}")

    if waiting_reasons:
        return {
            "action": "wait",
            "waiting_reason": "upstream_pending(" + ",".join(waiting_reasons) + ")",
        }
    return {"action": "ready"}


# ---- Legacy single-upstream wrapper kept ONLY for clean migration; new
# code uses ``check_upstreams_and_decide``.
def check_upstream_and_decide(
    items_from: dict[str, Any],
    allow_partial_upstream: bool,
    store,
) -> dict[str, Any]:
    """Deprecated; routes to the new multi-upstream decision."""
    task_id = items_from.get("task_id")
    if not task_id:
        return {"action": "error", "code": "UPSTREAM_NOT_FOUND", "message": "items_from.task_id is required"}
    return check_upstreams_and_decide([task_id], allow_partial_upstream, store)


# --- Batch Orchestrator ---

class BatchOrchestrator:
    """Orchestrates batch task execution with parent-child task model."""

    def __init__(self, store, blob_store, run_child_fn: Callable[[Task], None], data_root: Path | None = None):
        """
        Args:
            store: SqliteStateStore instance
            blob_store: LocalBlobStore instance
            run_child_fn: Callable that executes a single child task (blocking).
                          Signature: run_child_fn(task: Task) -> None
                          Should update task status in store upon completion/failure.
            data_root: Root data directory for log files.
        """
        self.store = store
        self.blob_store = blob_store
        self._run_child_fn = run_child_fn
        self._data_root = data_root
        self._active_batches: dict[str, _BatchExecution] = {}
        self._lock = threading.Lock()

    def _write_parent_log(self, parent_task_id: str, message: str, level: str = "INFO") -> None:
        """Write a log entry to the parent task's log file."""
        if not self._data_root:
            return
        log_dir = self._data_root / "logs" / "tasks" / time.strftime("%Y-%m-%d")
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / f"{parent_task_id}.log"
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        log_line = f"[{timestamp}] [{level}] {message}\n"
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(log_line)

    def create_batch(
        self,
        config: BatchConfig,
        items_from: dict[str, Any] | None = None,
        depends_on_task_ids: list[str] | None = None,
        *,
        origin_type: str | None = None,
        origin_plan_id: str | None = None,
    ) -> tuple[Task, list[Task]]:
        """Create a parent task and N child tasks.

        Two upstream-related inputs:
        - ``items_from``: SQL pipeline spec ``{sources, sql, field, dedup?}``.
          When present, it is validated up-front; every ``run_id`` in
          ``sources`` is auto-merged into the dependency set.
        - ``depends_on_task_ids``: explicit user-supplied dependency list. The
          final dependency set is the **union** with the run_ids extracted
          from ``items_from.sources``. The two are independent expressions of
          intent: an explicit dep means "don't start until this task is
          ready"; a source means "and read its data when starting". Both can
          be true simultaneously, both can be true on different tasks.

        ``origin_type`` / ``origin_plan_id`` are stamped onto the *parent*
        task only. Children are looked up via parent_task_id and don't need
        their own origin tag (mirrors how the UI displays "tasks fired by
        plan X" — the parent batch is the unit of accounting).

        If any upstream is not yet ready, the parent is created in
        ``waiting_dependency`` with no children; the daemon's downstream
        trigger will revisit it. If everything is ready, items are resolved
        synchronously (file mode or SQL mode) and children are created.

        Returns:
            (parent_task, child_tasks). ``child_tasks`` may be empty if the
            task is waiting for upstreams.

        Raises:
            ValueError: with error-code prefix for dependency / SQL issues.
            SQLItemsFromError: when items_from validation fails.
        """
        if not config.platform:
            raise ValueError("Platform is required")
        if not config.action:
            raise ValueError("Action is required")

        # ------------------------------------------------------------------
        # 1. Reject / normalize obsolete shapes.
        # ------------------------------------------------------------------
        if items_from and "task_id" in items_from:
            raise ValueError(
                "items_from {task_id, field} is no longer supported. "
                "Use the SQL pipeline: items_from = {sources, sql, field, dedup?}."
            )

        # ------------------------------------------------------------------
        # 1.5. Normalize legacy flat {run_id, field?} into SQL pipeline shape.
        #      The v1 frontend (upstream_field tab) wrote this shape before the
        #      SQL pipeline was the only path. Convert it so the downstream
        #      dependency check + waiting flow kicks in.
        # ------------------------------------------------------------------
        if items_from and "run_id" in items_from and "sources" not in items_from:
            _run_id = items_from["run_id"]
            _field = items_from.get("field", "item")
            items_from = {
                "sources": {"upstream": {"run_id": _run_id, "field": _field}},
                "sql": f"SELECT {_field} FROM upstream",
                "field": _field,
            }

        # ------------------------------------------------------------------
        # 2. SQL validation (L0/L1/L2). Done BEFORE we touch the DB so a bad
        #    spec doesn't leave a half-created waiting task behind.
        # ------------------------------------------------------------------
        sql_run_id_sources: list[str] = []
        if items_from and "sources" in items_from:
            # Lazy import to avoid a hard duckdb import at module load (the SQL
            # path is opt-in).
            from crawlhub.core.sql_validator import validate_items_from
            validate_items_from(items_from, self.store)
            for ref in items_from["sources"].values():
                if isinstance(ref, dict) and ref.get("run_id"):
                    sql_run_id_sources.append(str(ref["run_id"]))

        # ------------------------------------------------------------------
        # 3. Compute the unified dependency set: union(explicit, sql sources).
        # ------------------------------------------------------------------
        explicit_deps = list(depends_on_task_ids or [])
        merged_deps: list[str] = []
        seen_dep: set[str] = set()
        for tid in (*explicit_deps, *sql_run_id_sources):
            if tid and tid not in seen_dep:
                seen_dep.add(tid)
                merged_deps.append(tid)

        # ------------------------------------------------------------------
        # 4. Cycle / depth check across the union.
        # ------------------------------------------------------------------
        if merged_deps:
            cycle_check = check_circular_dependency("__new_task__", merged_deps, self.store)
            if cycle_check == "CIRCULAR_DEPENDENCY":
                raise ValueError("CIRCULAR_DEPENDENCY: Circular dependency detected")
            if cycle_check == "DEPENDENCY_DEPTH_EXCEEDED":
                raise ValueError(
                    f"DEPENDENCY_DEPTH_EXCEEDED: Dependency chain exceeds maximum depth ({_MAX_DEPENDENCY_DEPTH})"
                )

            # Decide: ready / wait / error.
            decision = check_upstreams_and_decide(
                merged_deps, config.allow_partial_upstream, self.store
            )
            if decision["action"] == "error":
                raise ValueError(f"{decision['code']}: {decision['message']}")
            if decision["action"] == "wait":
                return self._create_waiting_task(
                    config, items_from, merged_deps, decision,
                    origin_type=origin_type, origin_plan_id=origin_plan_id,
                )

            # Ready: if SQL mode, run the SQL now to materialize items.
            if items_from and "sources" in items_from:
                from crawlhub.core.sql_runner import run_items_from
                try:
                    config.items = [str(x) for x in run_items_from(items_from, self.store)]
                except Exception as e:  # noqa: BLE001 — surface as ValueError for the API layer
                    raise ValueError(f"RESOLVE_ITEMS_FAILED: {e}")

            # Also handle the "list" tab format: items_from = {items: [...], param_name: 'xxx'}
            # This is the non-SQL path where the user typed a newline-separated list.
            elif items_from and "items" in items_from and not config.items:
                raw_items = items_from["items"]
                if isinstance(raw_items, str):
                    config.items = [l for l in raw_items.split("\n") if l.strip()]
                else:
                    config.items = [str(x) for x in raw_items if x and str(x).strip()]

        if not config.items:
            raise ValueError("No valid items to process")

        # ------------------------------------------------------------------
        # 5. Persist parent + children.
        # ------------------------------------------------------------------
        # logic_param: the original POST /api/batch body (with items_from
        # preserved, no expanded items[]). Used for audit / "copy as request".
        parent_logic: dict[str, Any] = {
            "action": config.action,
            "item_key": config.item_key,
            "common_params": config.common_params,
            "concurrency": config.concurrency,
            "fail_strategy": config.fail_strategy,
            "cookie_policy": config.cookie_policy,
            "allow_partial_upstream": config.allow_partial_upstream,
        }
        if items_from:
            parent_logic["items_from"] = items_from
        elif getattr(config, "items_from_meta", None):
            parent_logic["items_from"] = config.items_from_meta
        else:
            # If the user submitted a literal items[] (no items_from), keep
            # it on logic_param so audit shows what they actually POSTed.
            parent_logic["items"] = list(config.items)

        # snapshot_param: executable view, frozen items[] inline,
        # items_from stripped, defaults filled by build_batch_snapshot.
        parent_snapshot = build_batch_snapshot(
            parent_logic, resolved_items=list(config.items)
        )

        parent = Task(
            platform=config.platform,
            task_type="batch_run",
            logic_param=parent_logic,
            snapshot_param=parent_snapshot,
        )
        task_name = f"{config.platform}_batch_{config.action}"
        parent.output_dir = self.blob_store.get_output_dir(parent.task_id, task_name)
        parent_dict = parent.to_dict()
        parent_dict["depends_on_task_ids"] = merged_deps
        parent_dict["origin_type"] = origin_type
        parent_dict["origin_plan_id"] = origin_plan_id
        self.store.create_task(parent_dict)

        # Create child tasks.
        # Children never have items_from / batch defaults to resolve, so the
        # snapshot equals the logic param after build_task_snapshot fills the
        # common-params defaults.
        # ──────────────────────────────────────────────────────────────────
        #  R4-P14 Phase 2：把父任务的 concurrency 注入子任务 snapshot，
        #  让 daemon 在创建 BrowserSessionManager 时能据此决定 page 池容量
        #  （page_pool_size = concurrency）。
        #  消除"yaml 配 page_pool_size + CLI 传 concurrency 必须人工对齐"
        #  这种特殊情况 —— 让两者从根上就是同一个数。
        # ──────────────────────────────────────────────────────────────────
        children: list[Task] = []
        for item_value in config.items:
            child_input = dict(config.common_params)
            child_input[config.item_key] = item_value
            child_input["_parent_concurrency"] = int(config.concurrency)

            child = Task(
                platform=config.platform,
                task_type=config.action,
                logic_param=child_input,
                snapshot_param=build_task_snapshot(child_input),
                parent_task_id=parent.task_id,
            )
            child_task_name = f"{config.platform}_{config.action}"
            child.output_dir = self.blob_store.get_output_dir(child.task_id, child_task_name)
            self.store.create_task(child.to_dict())
            children.append(child)

        # B2: switch parent phase pre_expansion -> post_expansion now that
        # children exist. Spec §1.2 wants this atomic with the children
        # INSERT — pragmatic approximation: do the children INSERT first
        # (so post_expansion phase always implies children exist) and write
        # the audit transition row. Recovery handles `phase=pre + children
        # exist` by re-running this finalize step.
        #
        # Per spec §5.2, phase-only transitions still carry the current
        # aggregate status into to_status (the schema enforces NOT NULL).
        # At this point the parent has just been created so it is in queued.
        parent_status_now = parent_dict.get("status", "queued")
        self.store.update_task_phase(parent.task_id, "post_expansion")
        self.store.insert_transition(
            task_id=parent.task_id,
            from_status=parent_status_now,
            to_status=parent_status_now,
            action="expand_into_phase_b",
            actor="system",
            from_phase="pre_expansion",
            to_phase="post_expansion",
            reason=f"Items expanded synchronously: {len(children)} children",
        )

        logger.info(
            "[batch] Created batch %s: %d children for %s/%s (deps=%d)",
            parent.task_id, len(children), config.platform, config.action, len(merged_deps),
        )
        return parent, children

    def _create_waiting_task(
        self,
        config: BatchConfig,
        items_from: dict[str, Any] | None,
        depends_on_task_ids: list[str],
        decision: dict[str, Any],
        *,
        origin_type: str | None = None,
        origin_plan_id: str | None = None,
    ) -> tuple[Task, list[Task]]:
        """Create a parent task in ``waiting_dependency`` state (no children yet).

        Children will be materialized once all upstreams are ready (the
        daemon's downstream trigger calls back into
        ``create_children_for_waiting_task``).
        """
        # logic_param: original POST /api/batch body (items_from preserved
        # exactly as submitted). snapshot_param: the executable view; items
        # are still empty here — finalize_batch_snapshot will fill them in
        # once upstream tasks complete and create_children_for_waiting_task
        # is called.
        parent_logic: dict[str, Any] = {
            "action": config.action,
            "item_key": config.item_key,
            "common_params": config.common_params,
            "concurrency": config.concurrency,
            "fail_strategy": config.fail_strategy,
            "cookie_policy": config.cookie_policy,
            "allow_partial_upstream": config.allow_partial_upstream,
        }
        if items_from:
            parent_logic["items_from"] = items_from

        parent_snapshot = build_batch_snapshot(parent_logic, resolved_items=None)

        parent = Task(
            platform=config.platform,
            task_type="batch_run",
            logic_param=parent_logic,
            snapshot_param=parent_snapshot,
        )
        task_name = f"{config.platform}_batch_{config.action}"
        parent.output_dir = self.blob_store.get_output_dir(parent.task_id, task_name)

        task_dict = parent.to_dict()
        task_dict["status"] = "waiting_dependency"
        task_dict["depends_on_task_ids"] = list(depends_on_task_ids)
        task_dict["waiting_reason"] = decision["waiting_reason"]
        task_dict["origin_type"] = origin_type
        task_dict["origin_plan_id"] = origin_plan_id
        self.store.create_task(task_dict)

        logger.info(
            "[batch] Created waiting batch %s: depends_on=%s (%s)",
            parent.task_id, depends_on_task_ids, decision["waiting_reason"],
        )
        return parent, []  # No children yet

    def create_children_for_waiting_task(
        self, parent_task_id: str, items: list[str]
    ) -> list[Task]:
        """Create child tasks for a previously waiting parent task.

        Called when upstream completes and items are resolved.
        """
        parent = self.store.get_task(parent_task_id)
        if parent is None:
            raise ValueError(f"Parent task not found: {parent_task_id}")

        # Use snapshot_param as the source of executable config; logic_param
        # is left untouched (still carries items_from for audit).
        config_input = parent["snapshot_param"]
        platform = parent["platform"]
        action = config_input.get("action", "")
        item_key = config_input.get("item_key", "")
        common_params = config_input.get("common_params", {})

        # Update parent snapshot with resolved items (items_from has already
        # been stripped by build_batch_snapshot at create time).
        new_snapshot = finalize_batch_snapshot(config_input, items)
        self.store.update_task(parent_task_id, {"snapshot_param": new_snapshot})

        # Create child tasks
        # ──────────────────────────────────────────────────────────────────
        #  R4-P14 Phase 2：lazy expansion 路径同样把父 concurrency 透传
        #  给子任务（与立即展开路径一致）。父 snapshot 在创建时已保存
        #  concurrency 字段（见 §510 / §619）。
        # ──────────────────────────────────────────────────────────────────
        parent_concurrency = int(config_input.get("concurrency", 1))
        children: list[Task] = []
        for item_value in items:
            child_input = dict(common_params)
            child_input[item_key] = item_value
            child_input["_parent_concurrency"] = parent_concurrency

            child = Task(
                platform=platform,
                task_type=action,
                logic_param=child_input,
                snapshot_param=build_task_snapshot(child_input),
                parent_task_id=parent_task_id,
            )
            child_task_name = f"{platform}_{action}"
            child.output_dir = self.blob_store.get_output_dir(child.task_id, child_task_name)
            self.store.create_task(child.to_dict())
            children.append(child)

        # B2: switch parent phase pre_expansion -> post_expansion now that
        # children exist (spec §1.2). See create_batch for atomicity caveat.
        #
        # Per spec §5.2, phase-only transitions still carry the current
        # aggregate status into to_status (the schema enforces NOT NULL).
        parent_now = self.store.get_task(parent_task_id) or {}
        parent_status_now = parent_now.get("status", "queued")
        self.store.update_task_phase(parent_task_id, "post_expansion")
        self.store.insert_transition(
            task_id=parent_task_id,
            from_status=parent_status_now,
            to_status=parent_status_now,
            action="expand_into_phase_b",
            actor="system",
            from_phase="pre_expansion",
            to_phase="post_expansion",
            reason=f"Items resolved by upstream: {len(children)} children",
        )

        logger.info(
            "[batch] Created %d children for waiting batch %s",
            len(children), parent_task_id,
        )
        return children

    def execute_batch(self, parent_task_id: str) -> None:
        """Execute a batch task (blocking). Runs child tasks with concurrency control.

        This method is designed to be called in a worker thread.
        """
        parent = self.store.get_task(parent_task_id)
        if parent is None:
            raise ValueError(f"Parent task not found: {parent_task_id}")

        # Read batch config from snapshot_param (the executable view: items
        # already frozen, defaults filled, no items_from).
        config = parent["snapshot_param"]
        concurrency = config.get("concurrency", 1)
        fail_strategy = config.get("fail_strategy", "continue")

        # Update parent to running
        self.store.update_task(parent_task_id, {
            "status": TaskStatus.RUNNING.value,
            "started_at": time.time(),
        })

        # Get all pending children (skip completed ones for resume support)
        children = self.store.list_tasks(
            parent_id=parent_task_id,
            limit=10000,
            include_children=True,
        )
        pending_children = [
            c for c in children
            if c["status"] in (TaskStatus.QUEUED.value, TaskStatus.INTERRUPTED.value)
        ]
        total_children = len(children)
        completed_count = sum(1 for c in children if c["status"] == TaskStatus.SUCCEEDED.value)
        failed_count = sum(1 for c in children if c["status"] == TaskStatus.FAILED.value)
        cancelled_count = sum(1 for c in children if c["status"] == TaskStatus.CANCELLED.value)

        # Write initial scheduling log
        # Per-cookie request interval is enforced by CookieThrottle (configured per-platform in Platform Management).
        self._write_parent_log(parent_task_id, f"Batch started: {total_children} total children, {len(pending_children)} pending, concurrency={concurrency}, strategy={fail_strategy}")
        if completed_count > 0:
            self._write_parent_log(parent_task_id, f"Resuming: {completed_count} already completed, {failed_count} failed, {cancelled_count} cancelled")

        # Track execution state
        execution = _BatchExecution(
            parent_task_id=parent_task_id,
            total=total_children,
            completed=completed_count,
            failed=failed_count,
            cancelled=cancelled_count,
        )
        # Create abort_event and attach it to the execution record BEFORE
        # publishing to _active_batches, so cancel_batch() can reach it.
        abort_event = threading.Event()
        execution.abort_event = abort_event

        with self._lock:
            self._active_batches[parent_task_id] = execution

        # Update initial progress
        self._update_parent_progress(parent_task_id, execution, total_children)

        child_contexts: dict[str, Any] = {}  # task_id -> context (for abort cancellation)

        def run_one_child(child_dict: dict) -> tuple[str, str]:
            """Run a single child task. Returns (task_id, final_status)."""
            if abort_event.is_set():
                # Mark as cancelled without running
                logger.info(
                    "[CANCEL] worker skipping child=%s (abort_event set before svc.execute)",
                    child_dict["task_id"],
                )
                self.store.update_task(child_dict["task_id"], {
                    "status": TaskStatus.CANCELLED.value,
                    "finished_at": time.time(),
                    "error": "Batch aborted",
                })
                return (child_dict["task_id"], TaskStatus.CANCELLED.value)

            child_task = Task.from_dict(child_dict)
            item_value = child_dict.get("snapshot_param", {}).get(config.get("item_key", ""), child_task.task_id[:8])
            _run_exc = None
            try:
                self._run_child_fn(child_task)
            except Exception as e:
                _run_exc = e
                logger.exception("[batch] Child %s failed: %s", child_task.task_id, e)

            # Re-read status from DB (run_child_fn should have updated it)
            updated = self.store.get_task(child_task.task_id)
            final_status = updated["status"] if updated else TaskStatus.FAILED.value
            logger.info("[DIAG-BATCH] child=%s parent=%s db_status=%s record_count=%s run_exc=%s",
                        child_task.task_id, parent_task_id, final_status,
                        (updated or {}).get("record_count"),
                        type(_run_exc).__name__ if _run_exc else None)
            if final_status == TaskStatus.RUNNING.value:
                logger.error("[DIAG-BATCH] child=%s STUCK IN RUNNING! worker did not flip status. record_count=%s error=%s",
                             child_task.task_id,
                             (updated or {}).get("record_count"),
                             (updated or {}).get("error"))
                self._write_parent_log(
                    parent_task_id,
                    f"[DIAG] Child {child_task.task_id[:8]} stuck in RUNNING (record_count={(updated or {}).get('record_count')}, run_exc={type(_run_exc).__name__ if _run_exc else None})",
                    level="ERR",
                )

            # Log child result to parent
            if final_status == TaskStatus.SUCCEEDED.value:
                records = updated.get("record_count", 0) or 0
                self._write_parent_log(parent_task_id, f"Child {child_task.task_id[:8]} completed ({item_value}): {records} records")
            elif final_status == TaskStatus.PARTIAL_SUCCEEDED.value:
                records = updated.get("record_count", 0) or 0
                err = updated.get("error", "")
                self._write_parent_log(parent_task_id, f"Child {child_task.task_id[:8]} partial_failed ({item_value}): {records} records, {err}", level="WARN")
            elif final_status == TaskStatus.FAILED.value:
                err = updated.get("error", "unknown")
                self._write_parent_log(parent_task_id, f"Child {child_task.task_id[:8]} FAILED ({item_value}): {err}", level="ERR")

            return (child_task.task_id, final_status)

        # Execute with concurrency control
        try:
            with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="batch-child") as executor:
                futures: dict[Future, dict] = {}
                submitted_count = 0
                aborted_predispatch = 0

                for child_dict in pending_children:
                    if abort_event.is_set():
                        # Mark remaining as cancelled
                        self.store.update_task(child_dict["task_id"], {
                            "status": TaskStatus.CANCELLED.value,
                            "finished_at": time.time(),
                            "error": "Batch aborted",
                        })
                        execution.cancelled += 1
                        aborted_predispatch += 1
                        continue

                    future = executor.submit(run_one_child, child_dict)
                    futures[future] = child_dict
                    submitted_count += 1
                    # Note: Submission rate is bounded by `concurrency` (ThreadPoolExecutor blocks
                    # when full). The actual HTTP request interval is enforced by CookieThrottle
                    # (per-platform expected_interval, configured in Platform Management UI).

                if aborted_predispatch:
                    logger.info(
                        "[CANCEL] dispatch loop saw abort_event for parent=%s: submitted=%d, predispatch_cancelled=%d",
                        parent_task_id, submitted_count, aborted_predispatch,
                    )
                    # Note: Submission rate is bounded by `concurrency` (ThreadPoolExecutor blocks
                    # when full). The actual HTTP request interval is enforced by CookieThrottle
                    # (per-platform expected_interval, configured in Platform Management UI).

                # Collect results
                for future in as_completed(futures):
                    task_id, final_status = future.result()

                    if final_status == TaskStatus.SUCCEEDED.value:
                        execution.completed += 1
                    elif final_status == TaskStatus.PARTIAL_SUCCEEDED.value:
                        # Partial failed counts as completed (has some records)
                        execution.completed += 1
                    elif final_status == TaskStatus.FAILED.value:
                        execution.failed += 1
                        if fail_strategy == "abort":
                            logger.info("[batch] Abort triggered by child %s failure", task_id)
                            self._write_parent_log(parent_task_id, f"ABORT triggered by child {task_id[:8]} failure", level="WARN")
                            abort_event.set()
                            # Cancel remaining pending futures
                            for f, child in futures.items():
                                if not f.done():
                                    f.cancel()
                    elif final_status == TaskStatus.CANCELLED.value:
                        execution.cancelled += 1

                    # Update parent progress
                    self._update_parent_progress(parent_task_id, execution, total_children)

        except Exception as e:
            logger.error("[batch] Batch execution error: %s", e)
            self._write_parent_log(parent_task_id, f"Batch execution error: {e}", level="ERR")
            self.store.update_task(parent_task_id, {
                "status": TaskStatus.FAILED.value,
                "finished_at": time.time(),
                "error": f"Batch execution error: {e}",
            })
            return
        finally:
            with self._lock:
                self._active_batches.pop(parent_task_id, None)

        # If abort was triggered, mark remaining queued children as cancelled
        if abort_event.is_set():
            remaining = self.store.list_tasks(parent_id=parent_task_id, status="queued", limit=10000)
            for child in remaining:
                self.store.update_task(child["task_id"], {
                    "status": TaskStatus.CANCELLED.value,
                    "finished_at": time.time(),
                    "error": "Batch aborted",
                })
                execution.cancelled += 1

        # B2: parent final status is now a mirror of aggregate_parent_status,
        # which `_apply_atomic_action` -> `aggregate_with_lock` keeps in sync
        # whenever any child transitions. We only write the parent's
        # *metadata* (progress, error message, record_count, file list).
        # If somehow the aggregate hasn't fired yet (e.g. no children
        # transitioned in this run because all were already terminal on
        # entry), force-recompute so the post-batch state is canonical.
        from crawlhub.core.state_machine import aggregate_with_lock as _agg_lock
        try:
            _agg_lock(self.store, parent_task_id)
        except Exception as e:  # noqa: BLE001
            logger.warning("[batch] aggregate recompute failed for %s: %s", parent_task_id, e)

        # Choose human-readable error message based on counts (cosmetic).
        if execution.failed > 0 and fail_strategy == "abort":
            error_msg = f"Batch aborted: {execution.failed} failed, {execution.cancelled} cancelled"
        elif execution.failed > 0 and execution.completed == 0:
            error_msg = f"All items failed: {execution.failed} failed / {total_children} total"
        elif execution.failed > 0 and execution.completed > 0:
            error_msg = f"{execution.failed} failed, {execution.completed} success / {total_children} total"
        else:
            error_msg = None

        self._write_parent_log(parent_task_id, f"Batch finished: {execution.completed} success, {execution.failed} failed, {execution.cancelled} cancelled / {total_children} total")

        # Merge results
        merged_count = self.merge_results(parent_task_id)
        self._write_parent_log(parent_task_id, f"Results merged: {merged_count} total records")

        # Update parent final metadata (NOT status — aggregate is the source).
        summary = {
            "total": total_children,
            "success": execution.completed,
            "failed": execution.failed,
            "cancelled": execution.cancelled,
        }
        parent_data = self.store.get_task(parent_task_id)
        self.store.update_task(parent_task_id, {
            "finished_at": time.time(),
            "progress": 1.0,
            "error": error_msg,
            "record_count": merged_count,
            "total_bytes": self._get_merged_file_size(parent_data["output_dir"]),
            "result_files": self.blob_store.list_files(parent_data["output_dir"]),
        })

        # Write summary to parent output_dir
        parent_data = self.store.get_task(parent_task_id)
        self.blob_store.write_summary(parent_data["output_dir"], {
            "task_id": parent_task_id,
            "batch_summary": summary,
            "logic_param": parent_data["logic_param"],
            "snapshot_param": parent_data["snapshot_param"],
        })

        logger.info(
            "[batch] Batch %s finished: %d success, %d failed, %d cancelled / %d total",
            parent_task_id, execution.completed, execution.failed,
            execution.cancelled, total_children,
        )

    def _get_merged_file_size(self, output_dir: str) -> int:
        """Get the size of the merged data.jsonl file."""
        merged_path = Path(output_dir) / "data.jsonl"
        if merged_path.exists():
            return merged_path.stat().st_size
        return 0

    def merge_results(self, parent_task_id: str) -> int:
        """Merge all child task data.jsonl files into parent output_dir (streaming IO).

        Returns:
            Number of merged records.
        """
        parent = self.store.get_task(parent_task_id)
        if parent is None:
            return

        parent_output_dir = parent["output_dir"]
        merged_path = Path(parent_output_dir) / "data.jsonl"
        Path(parent_output_dir).mkdir(parents=True, exist_ok=True)

        children = self.store.list_tasks(parent_id=parent_task_id, limit=10000)
        merged_count = 0
        skipped = 0

        with open(merged_path, "w", encoding="utf-8") as out_f:
            for child in children:
                if child["status"] != TaskStatus.SUCCEEDED.value:
                    continue
                child_data_path = Path(child["output_dir"]) / "data.jsonl"
                if not child_data_path.exists():
                    logger.warning("[batch] Child %s data.jsonl not found, skipping", child["task_id"])
                    skipped += 1
                    continue
                try:
                    with open(child_data_path, "r", encoding="utf-8") as in_f:
                        for line in in_f:
                            out_f.write(line)
                            merged_count += 1
                except (OSError, UnicodeDecodeError) as e:
                    logger.warning("[batch] Child %s data.jsonl read error: %s, skipping", child["task_id"], e)
                    skipped += 1

        logger.info(
            "[batch] Merged results for %s: %d records from children (%d skipped)",
            parent_task_id, merged_count, skipped,
        )
        return merged_count

    def get_batch_summary(self, parent_task_id: str) -> dict[str, Any]:
        """Get aggregated status counts for a batch task's children."""
        children = self.store.list_tasks(parent_id=parent_task_id, limit=10000)
        status_counts = {
            "queued": 0,
            "running": 0,
            "succeeded": 0,
            "failed": 0,
            "cancelled": 0,
        }
        total_record_count = 0
        for child in children:
            status = child["status"]
            if status in status_counts:
                status_counts[status] += 1
            else:
                # Handle interrupted etc.
                status_counts.setdefault(status, 0)
                status_counts[status] = status_counts.get(status, 0) + 1
            total_record_count += child.get("record_count", 0) or 0
        return {
            "total_children": len(children),
            "status_counts": status_counts,
            "record_count": total_record_count,
        }

    def _update_parent_progress(
        self, parent_task_id: str, execution: _BatchExecution, total: int
    ) -> None:
        """Update parent task progress based on completed children.

        Updates both progress (0.0-1.0) and record_count (completed children count)
        so the frontend can display "x/y" progress text.
        """
        if total == 0:
            return
        done = execution.completed + execution.failed + execution.cancelled
        progress = done / total
        self.store.update_task(parent_task_id, {
            "progress": progress,
            "last_heartbeat": time.time(),
        })

    # --- Cascade Operations ---

    def cancel_batch(self, parent_task_id: str, cancel_context_fn: Callable[[str], bool] | None = None) -> dict:
        """Cancel a batch task and all its pending/running children.

        Non-blocking: this method only delivers the cancel SIGNAL and never
        waits for running children to finish. The HTTP /cancel endpoint
        therefore returns immediately. In-flight children are drained by the
        main `_execute_batch_task` loop's `as_completed` collector — when
        each future completes naturally, its status is aggregated and the
        parent's status flips to CANCELLED via the standard aggregate path.

        Steps (all synchronous, all fast):
          1. Set abort_event   -> dispatch loop stops submitting new children.
          2. Mark every QUEUED child as CANCELLED (single round-trip per row).
          3. Send context-cancel signal to every RUNNING child (best-effort).
          4. Mark parent as CANCELLED so UI reflects the user intent
             immediately (the eventual aggregate refresh will agree).

        Args:
            parent_task_id: The parent batch task ID
            cancel_context_fn: Optional function to cancel a running task's
                               context. Signature: cancel_context_fn(task_id) -> bool

        Returns:
            Summary dict with counts of children acted upon.
        """
        t_start = time.time()
        logger.info("[CANCEL] cancel_batch ENTERED parent=%s", parent_task_id)

        # Step 1: trip abort_event so the dispatch loop in execute_batch()
        # stops submitting more children. This is the single most important
        # action — every other step is bookkeeping.
        with self._lock:
            execution = self._active_batches.get(parent_task_id)
        if execution is not None and execution.abort_event is not None:
            execution.abort_event.set()
            logger.info("[CANCEL] abort_event set for parent=%s", parent_task_id)
        else:
            logger.info("[CANCEL] no active execution for parent=%s (already finished?)", parent_task_id)

        # Step 2 + 3: walk children once, classify, act. NO waiting loop.
        children = self.store.list_tasks(parent_id=parent_task_id, limit=10000)
        queued_cancelled = 0
        running_signalled = 0
        for child in children:
            status = child["status"]
            if status == TaskStatus.QUEUED.value:
                self.store.update_task(child["task_id"], {
                    "status": TaskStatus.CANCELLED.value,
                    "finished_at": time.time(),
                    "error": "Parent batch cancelled",
                })
                queued_cancelled += 1
            elif status == TaskStatus.RUNNING.value:
                if cancel_context_fn:
                    cancel_context_fn(child["task_id"])
                running_signalled += 1

        # Step 4: parent status -> cancelled now (don't wait for aggregate).
        self.store.update_task(parent_task_id, {
            "status": TaskStatus.CANCELLED.value,
            "finished_at": time.time(),
            "error": "Batch cancelled by user",
        })

        elapsed_ms = int((time.time() - t_start) * 1000)
        logger.info(
            "[CANCEL] cancel_batch DONE parent=%s queued_cancelled=%d running_signalled=%d elapsed_ms=%d",
            parent_task_id, queued_cancelled, running_signalled, elapsed_ms,
        )
        return {
            "cancelled": queued_cancelled,
            "running_signalled": running_signalled,
            "elapsed_ms": elapsed_ms,
        }

    # NOTE: retry_failed() was removed. It bypassed the state machine by
    # writing `tasks.status = queued` directly via `update_task`, which
    # produced no transition rows, didn't clear `cancellation_intent`, and
    # raced with `aggregate_with_lock`. Use `Daemon.apply_parent_action(
    # parent_id, "failed_retry")` instead — it fans out FULL_RETRY through
    # the state machine per child and lets the aggregate compute the parent
    # status. See spec §2.2 / §1.4 and the historical note on
    # `_fanout_failed_retry` in daemon.py.

    def recover_interrupted(self) -> list[str]:
        """Recover interrupted batch tasks after daemon restart.

        Only recovers parent tasks (parent_task_id IS NULL) with:
        - status = interrupted
        - task_type = batch_run

        For each recovered parent:
        - Reset interrupted children to pending
        - Skip already completed children

        Returns:
            List of parent task_ids that were recovered.
        """
        # Find interrupted batch parent tasks
        interrupted_parents = self.store.list_tasks(
            status="interrupted",
            include_children=False,  # Only top-level
            limit=1000,
        )
        recovered = []

        for parent in interrupted_parents:
            if parent["task_type"] != "batch_run":
                continue

            parent_id = parent["task_id"]

            # Reset interrupted children to pending
            children = self.store.list_tasks(parent_id=parent_id, limit=10000)
            reset_count = 0
            for child in children:
                if child["status"] in (TaskStatus.INTERRUPTED.value, TaskStatus.RUNNING.value):
                    self.store.update_task(child["task_id"], {
                        "status": TaskStatus.QUEUED.value,
                        "error": None,
                        "started_at": None,
                        "finished_at": None,
                    })
                    reset_count += 1

            # Reset parent to pending for re-execution
            self.store.update_task(parent_id, {
                "status": TaskStatus.QUEUED.value,
                "error": None,
                "started_at": None,
                "finished_at": None,
            })

            recovered.append(parent_id)
            logger.info(
                "[batch] Recovered batch %s: %d children reset to pending",
                parent_id, reset_count,
            )

        return recovered


@dataclass
class _BatchExecution:
    """Internal state tracking for an active batch execution."""

    parent_task_id: str
    total: int = 0
    completed: int = 0
    failed: int = 0
    cancelled: int = 0
    # Set by cancel_batch() to interrupt the dispatch loop in execute_batch().
    # Stored here (not as a local) so external callers can reach it.
    abort_event: "threading.Event | None" = None
