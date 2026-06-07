"""API routes for CrawlHub Daemon.

All REST endpoints are defined here, organized by resource.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import platform as platform_mod
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from crawlhub.core.config import get_data_root
from crawlhub.core.cookie_converters import convert_storage_state
from crawlhub.core.daemon import (
    CrawlHubDaemon,
    DaemonShuttingDown,
    DiskSpaceLow,
    TaskNotFound,
    TaskNotRetryable,
)
from crawlhub.core.models import TaskStatus
from crawlhub.core.registry import get_registry

logger = logging.getLogger(__name__)

router = APIRouter()


def _get_daemon(request: Request) -> CrawlHubDaemon:
    """Get daemon instance from app state."""
    return request.app.state.daemon


# --- Pydantic models ---

class TaskSubmitRequest(BaseModel):
    platform: str
    task_type: str
    logic_param: dict[str, Any] = {}
    depends_on_task_ids: list[str] = []
    note: str | None = None  # <= 100 chars; truncated server-side if longer
    # When True (default), a task that ends with 0 records and 0 errors is
    # finalized as SUCCEEDED instead of FAILED. Many crawl APIs legitimately
    # return empty results (no comments on a post, no search hits) without
    # raising any error — treating those as failures floods the ops view
    # with false positives. Pass False to opt into strict detection (the
    # legacy behavior, useful when 0-record outcomes really do indicate a
    # bug, e.g. malformed ID).
    treat_empty_as_success: bool = True


class ExportRequest(BaseModel):
    format: str = "csv"  # csv | xlsx | json | jsonl
    output_path: str | None = None


class NotificationTestRequest(BaseModel):
    channel: str = "default"


class ChannelRequest(BaseModel):
    name: str
    webhook_url: str | None = None
    enabled: bool = True


class RuleRequest(BaseModel):
    rule_id: str | None = None
    event_type: str
    channel_name: str
    enabled: bool = True


class QimaiLoginRequest(BaseModel):
    username: str
    password: str


# --- Scheduling-plans request models ---
# These mirror the Plan / Trigger / Step rows in sqlite_store but with looser
# typing: request_payload arrives as a JSON object from the browser, gets
# serialized to TEXT in the DB, and is deserialized again on read.
# Validation (cron grammar, interval keys, timezone, step refs) lives in
# ``_validate_*`` helpers near the endpoints.

class PlanGroupCreateRequest(BaseModel):
    name: str
    note: str | None = None


class PlanGroupPatchRequest(BaseModel):
    name: str | None = None
    note: str | None = None


class PlanTriggerInput(BaseModel):
    trigger_id: str | None = None  # ignored on POST/PUT, server assigns
    kind: str                      # "cron" | "interval" | "once"
    expr: str
    enabled: bool = True


class PlanStepInput(BaseModel):
    step_id: str | None = None     # ignored on POST/PUT
    # Wire-level submit kind. 'task'  -> submit via POST /api/task body shape
    #                        'batch' -> submit via POST /api/batch body shape
    request_kind: str
    platform: str
    task_type: str
    # Full POST /api/task or POST /api/batch JSON body, stored verbatim.
    # May contain ${YYYYMMDD}-style time placeholders and ${step[K].task_id}
    # cross-step references; both resolved at fire time.
    request_payload: dict[str, Any] = {}
    note: str | None = None


class PlanWriteRequest(BaseModel):
    group_id: str
    name: str
    enabled: bool = False
    timezone: str = "Asia/Shanghai"
    notify_on_fire_fail: bool = True
    note: str | None = None
    triggers: list[PlanTriggerInput] = []
    steps: list[PlanStepInput] = []


class EnabledPatchRequest(BaseModel):
    enabled: bool


class PlanRunRequest(BaseModel):
    """POST /api/plans/{id}/run body. instance_time is optional ISO-8601;
    if absent the route uses ``datetime.now(tz=plan.timezone)``.
    """
    instance_time: str | None = None


# --- Task endpoints ---

@router.post("/api/tasks")
def create_task(body: TaskSubmitRequest, request: Request):
    """Submit a new crawl task."""
    logger.info("[API] POST /api/tasks: platform=%s, task_type=%s, depends_on=%s",
                body.platform, body.task_type, body.depends_on_task_ids)

    from crawlhub.core.telemetry import emit_task_submitted

    _telemetry_task_id = ""
    _telemetry_status = 200
    try:
        # Validate platform and task_type dynamically against the registry
        from crawlhub.core.registry import get_registry, create_platform_service
        registry = get_registry()
        if body.platform not in registry:
            _telemetry_status = 404
            raise HTTPException(404, detail=f"Platform '{body.platform}' not found. Available platforms: {list(registry.keys())}")
        svc = create_platform_service(body.platform)
        if body.task_type not in svc.list_actions():
            _telemetry_status = 404
            raise HTTPException(404, detail=f"Action '{body.task_type}' not found for platform '{body.platform}'. Available actions: {svc.list_actions()}")

        daemon = _get_daemon(request)
        try:
            # Inject treat_empty_as_success into the task logic_param so the
            # daemon's finalize logic can read it without changing the daemon
            # signature. Honor an explicit value already inside body.logic_param
            # (in case the caller put it there directly), otherwise fall through
            # to the top-level field's default.
            merged_logic_param = dict(body.logic_param or {})
            if "treat_empty_as_success" not in merged_logic_param:
                merged_logic_param["treat_empty_as_success"] = body.treat_empty_as_success
            task = daemon.submit_task(
                body.platform, body.task_type, merged_logic_param,
                depends_on_task_ids=body.depends_on_task_ids or None,
            )
            _telemetry_task_id = task.task_id
            # Attach user note (post-creation to keep daemon signature stable).
            # Hard cap 100 chars at the API boundary so storage never has oversized notes.
            if body.note is not None:
                note = body.note.strip()[:100] or None
                if note is not None:
                    daemon.store.update_task(task.task_id, {"note": note})
            logger.info("[API] Task created: task_id=%s", task.task_id)
            task_dict = task.to_dict()
            # Re-read from DB so response includes note + any status transition
            # (dependency waiting, etc.) that happened during submit.
            db_task = daemon.store.get_task(task.task_id)
            if db_task:
                task_dict = db_task
            return task_dict
        except DaemonShuttingDown:
            _telemetry_status = 503
            raise HTTPException(503, detail="Daemon is shutting down")
        except DiskSpaceLow as e:
            _telemetry_status = 503
            raise HTTPException(503, detail={"error": "disk_low", "free_bytes": e.free_bytes})
        except ValueError as e:
            _telemetry_status = 400
            err_str = str(e)
            if any(code in err_str for code in ["UPSTREAM_NOT_FOUND", "CIRCULAR_DEPENDENCY", "DEPENDENCY_DEPTH_EXCEEDED"]):
                raise HTTPException(400, detail=err_str)
            raise HTTPException(400, detail=err_str)
    except HTTPException:
        # status_code already captured above
        raise
    except Exception:
        _telemetry_status = 500
        raise
    finally:
        emit_task_submitted(
            task_id=_telemetry_task_id,
            platform=body.platform,
            task_type=body.task_type,
            status_code=_telemetry_status,
        )


@router.get("/api/tasks/{task_id}")
def get_task(task_id: str, request: Request):
    """Get task details."""
    daemon = _get_daemon(request)
    task = daemon.store.get_task(task_id)
    if task is None:
        raise HTTPException(404, detail=f"Task {task_id} not found")
    return task


@router.get("/api/tasks")
def list_tasks(
    request: Request,
    platform: str | None = None,
    status: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    limit: int = Query(default=100, le=1000),
    offset: int = Query(default=0, ge=0),
    include_children: bool = Query(default=False),
    parent_id: str | None = None,
    search: str | None = None,
    only_archived: bool = Query(default=False, description="Recycle-bin view: only archived tasks"),
    include_archived: bool = Query(default=False, description="Bypass archived filter (admin / lineage)"),
    origin_plan_id: str | None = Query(default=None, description="Filter to tasks fired by this scheduling plan"),
    sort_by: str = Query(default="created_at", description="Sort field: created_at, started_at, finished_at, status, platform, task_type, progress, record_count"),
    sort_order: str = Query(default="DESC", description="ASC or DESC"),
):
    """List tasks with filters. By default hides batch child tasks AND
    archived tasks (recycle-bin contents).

    archived filter precedence:
      only_archived=True   -> show ONLY archived tasks (recycle-bin tab)
      include_archived=True -> show all (active + archived)
      default              -> show only active (archived hidden)

    When `parent_id` is provided, returns only children of that parent
    (overrides include_children default). This is the canonical way for
    MCP clients / SQL pipelines / smoke tests to enumerate children of
    a batch parent without needing to hit the dedicated /children route.

    `search` accepts space-separated keywords; each keyword must appear
    (as substring, case-insensitive) in task_id, note, or input JSON.
    Empty / whitespace-only search is a no-op.

    `origin_plan_id` filters to tasks whose origin_plan_id matches — useful
    for the scheduling tab to show "tasks fired by plan X".
    """
    daemon = _get_daemon(request)
    return daemon.store.list_tasks(
        platform=platform,
        status=status,
        start_time=start_time,
        end_time=end_time,
        limit=limit,
        offset=offset,
        include_children=include_children,
        parent_id=parent_id,
        search=search,
        only_archived=only_archived,
        include_archived=include_archived,
        origin_plan_id=origin_plan_id,
        sort_by=sort_by,
        sort_order=sort_order,
    )


@router.post("/api/tasks/{task_id}/retry")
def retry_task(task_id: str, request: Request):
    """Retry a failed/interrupted/cancelled task."""
    from crawlhub.core.telemetry import emit_task_retried

    daemon = _get_daemon(request)

    # Resolve telemetry context BEFORE the action so we can still report
    # platform/task_type even when the action raises (404/409).
    _existing = daemon.store.get_task(task_id)
    _platform = (_existing or {}).get("platform", "") if _existing else ""
    _task_type = (_existing or {}).get("task_type", "") if _existing else ""
    _is_batch_parent = bool(
        _existing
        and _existing.get("task_type") == "batch_run"
        and _existing.get("parent_task_id") is None
    )
    _retry_scope = "batch_full" if _is_batch_parent else "single"
    _status = 200

    try:
        task = daemon.retry_task(task_id)
        return task.to_dict()
    except TaskNotFound:
        _status = 404
        raise HTTPException(404, detail=f"Task {task_id} not found")
    except TaskNotRetryable as e:
        _status = 409
        raise HTTPException(409, detail={"error": "task not retryable", "current_status": e.current_status})
    finally:
        emit_task_retried(
            task_id=task_id,
            platform=_platform,
            task_type=_task_type,
            retry_scope=_retry_scope,
            status_code=_status,
        )


@router.post("/api/tasks/{task_id}/cancel")
def cancel_task(task_id: str, request: Request):
    """Cancel a running task."""
    daemon = _get_daemon(request)
    task = daemon.store.get_task(task_id)
    if task is None:
        raise HTTPException(404, detail=f"Task {task_id} not found")

    success = daemon.cancel_task(task_id)
    if not success:
        raise HTTPException(409, detail="Task cannot be cancelled (not running or pending)")

    return {"status": "cancelled", "task_id": task_id}


class TaskNoteRequest(BaseModel):
    note: str | None = None  # None or "" clears the note; otherwise truncated to 100 chars


@router.patch("/api/tasks/{task_id}/note")
def update_task_note(task_id: str, body: TaskNoteRequest, request: Request):
    """Update the user-editable note on a task.

    Only top-level (parent or standalone) tasks may have a note. Attempting to
    set a note on a batch child returns 400 so the UI never offers the option
    in the first place but the API enforces it too. An empty string or null
    body clears the note.
    """
    daemon = _get_daemon(request)
    task = daemon.store.get_task(task_id)
    if task is None:
        raise HTTPException(404, detail=f"Task {task_id} not found")
    if task.get("parent_task_id") is not None:
        raise HTTPException(400, detail="Notes are only supported on parent/single tasks, not batch children")

    # Normalize: null or empty-after-strip -> clear. Otherwise cap at 100.
    if body.note is None:
        note_value = None
    else:
        stripped = body.note.strip()
        note_value = stripped[:100] if stripped else None

    daemon.store.update_task(task_id, {"note": note_value})
    return daemon.store.get_task(task_id)


# --- Batch API ---

class BatchSubmitRequest(BaseModel):
    platform: str
    action: str
    items: list[str] | None = None
    items_from: dict[str, Any] | None = None
    item_key: str = ""
    common_params: dict[str, Any] = {}
    concurrency: int = 1
    fail_strategy: str = "continue"
    cookie_policy: dict[str, Any] | None = None
    allow_partial_upstream: bool = True
    depends_on_task_ids: list[str] = []
    note: str | None = None  # <= 100 chars; applied to parent only
    # Per-task knob propagated to every child via common_params. See
    # TaskSubmitRequest.treat_empty_as_success for full semantics.
    treat_empty_as_success: bool = True


@router.post("/api/batch")
def submit_batch(body: BatchSubmitRequest, request: Request):
    """Submit a batch task."""
    from crawlhub.core.batch import BatchConfig, resolve_items
    from crawlhub.core.sql_errors import SQLItemsFromError
    from crawlhub.core.registry import get_registry, create_platform_service
    from crawlhub.core.telemetry import emit_batch_submitted

    _telemetry_parent_id = ""
    _telemetry_status = 200
    try:
        # Validate platform and action dynamically against the registry
        registry = get_registry()
        if body.platform not in registry:
            _telemetry_status = 404
            raise HTTPException(404, detail=f"Platform '{body.platform}' not found. Available platforms: {list(registry.keys())}")
        svc = create_platform_service(body.platform)
        if body.action not in svc.list_actions():
            _telemetry_status = 404
            raise HTTPException(404, detail=f"Action '{body.action}' not found for platform '{body.platform}'. Available actions: {svc.list_actions()}")

        daemon = _get_daemon(request)

        items_from = body.items_from
        resolved_items: list[str] = []

        # items + items_from are mutually exclusive (both filled means user is
        # confused or AI made a mistake; fail loud).
        if body.items and items_from:
            _telemetry_status = 400
            raise HTTPException(400, detail="items and items_from are mutually exclusive; pick one")

        sql_mode = bool(items_from and "sources" in items_from)

        if sql_mode:
            # SQL mode: BatchOrchestrator validates + (if ready) resolves.
            # Don't pre-resolve here.
            resolved_items = []
        elif items_from and "task_id" in items_from:
            # Reject legacy shape early with a clear message.
            _telemetry_status = 400
            raise HTTPException(
                400,
                detail=(
                    "items_from {task_id, field} is no longer supported. "
                    "Use the SQL pipeline: items_from = {sources, sql, field, dedup?}."
                ),
            )
        elif items_from and "file" in items_from:
            # File mode: resolve right here so BatchConfig.items is populated.
            try:
                resolved_items = resolve_items(
                    items=None, items_from=items_from, store=daemon.store,
                )
            except ValueError as e:
                _telemetry_status = 400
                raise HTTPException(400, detail=str(e))
        else:
            # Direct items.
            try:
                resolved_items = resolve_items(
                    items=body.items, items_from=None, store=daemon.store,
                )
            except ValueError as e:
                _telemetry_status = 400
                raise HTTPException(400, detail=str(e))

        # Build config
        # Inject treat_empty_as_success into common_params so each batch child
        # carries it through to the daemon's finalize logic. Honor an explicit
        # value already in common_params if the caller set it directly.
        merged_common_params = dict(body.common_params or {})
        if "treat_empty_as_success" not in merged_common_params:
            merged_common_params["treat_empty_as_success"] = body.treat_empty_as_success
        config = BatchConfig(
            platform=body.platform,
            action=body.action,
            item_key=body.item_key,
            items=resolved_items,
            common_params=merged_common_params,
            concurrency=body.concurrency,
            fail_strategy=body.fail_strategy,
            cookie_policy=body.cookie_policy or {},
            items_from_meta=items_from,
            allow_partial_upstream=body.allow_partial_upstream,
        )

        try:
            parent_task, child_count = daemon.submit_batch(
                config,
                items_from=items_from,
                depends_on_task_ids=body.depends_on_task_ids or None,
            )
            _telemetry_parent_id = parent_task.task_id
        except (DaemonShuttingDown, DiskSpaceLow) as e:
            _telemetry_status = 503
            raise HTTPException(503, detail=str(e))
        except SQLItemsFromError as e:
            # SQL validation / runtime failure surfaced from create_batch.
            _telemetry_status = 400
            raise HTTPException(400, detail={
                "error_code": e.__class__.__name__,
                "message": str(e),
            })
        except ValueError as e:
            err_str = str(e)
            if any(code in err_str for code in ["UPSTREAM_NOT_FOUND", "CIRCULAR_DEPENDENCY", "DEPENDENCY_DEPTH_EXCEEDED"]):
                _telemetry_status = 400
                raise HTTPException(400, detail=err_str)
            if any(code in err_str for code in ["UPSTREAM_FAILED", "UPSTREAM_FAILED_NO_OUTPUT", "UPSTREAM_PARTIAL_FAILURE", "UPSTREAM_NO_OUTPUT"]):
                _telemetry_status = 409
                raise HTTPException(409, detail=err_str)
            _telemetry_status = 400
            raise HTTPException(400, detail=err_str)

        # Get the task from DB to include depends_on_task_ids if waiting
        task_dict = daemon.store.get_task(parent_task.task_id)

        # Attach user note to the parent only (children don't get notes by design).
        # Truncate at API boundary \u2014 storage stays clean.
        if body.note is not None:
            note = body.note.strip()[:100] or None
            if note is not None:
                daemon.store.update_task(parent_task.task_id, {"note": note})
                # Refresh so the response carries the note too.
                task_dict = daemon.store.get_task(parent_task.task_id)

        response = {
            "task_id": parent_task.task_id,
            "child_count": child_count,
            "platform": body.platform,
            "action": body.action,
                    "status": task_dict.get("status", "queued") if task_dict else "queued",
                    "note": task_dict.get("note") if task_dict else None,
        }
        if task_dict and task_dict.get("depends_on_task_ids"):
            response["depends_on_task_ids"] = task_dict["depends_on_task_ids"]
            response["waiting_reason"] = task_dict.get("waiting_reason")

        return response
    except HTTPException:
        raise
    except Exception:
        _telemetry_status = 500
        raise
    finally:
        emit_batch_submitted(
            parent_task_id=_telemetry_parent_id,
            platform=body.platform,
            task_type=body.action,
            status_code=_telemetry_status,
        )


@router.get("/api/tasks/{parent_id}/children")
def list_children(
    parent_id: str,
    request: Request,
    status: str | None = None,
    limit: int = Query(default=100, le=1000),
    offset: int = Query(default=0, ge=0),
):
    """List child tasks of a batch parent."""
    daemon = _get_daemon(request)
    parent = daemon.store.get_task(parent_id)
    if parent is None:
        raise HTTPException(404, detail=f"Task {parent_id} not found")

    children = daemon.store.list_tasks(
        parent_id=parent_id,
        status=status,
        limit=limit,
        offset=offset,
    )
    return children


# --- Lineage & Force Operations ---

@router.get("/api/tasks/{task_id}/lineage")
def get_task_lineage(
    task_id: str,
    request: Request,
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
):
    """Get upstream and downstream lineage for a task."""
    daemon = _get_daemon(request)
    task = daemon.store.get_task(task_id)
    if task is None:
        raise HTTPException(404, detail=f"Task {task_id} not found")

    lineage = daemon.store.get_lineage(task_id, limit=limit, offset=offset)
    return lineage


@router.post("/api/tasks/{task_id}/force-complete")
def force_complete_task(task_id: str, request: Request):
    """Force a task to completed status."""
    daemon = _get_daemon(request)
    try:
        result = daemon.force_complete(task_id)
        return result
    except TaskNotFound:
        raise HTTPException(404, detail=f"Task {task_id} not found")
    except ValueError as e:
        raise HTTPException(409, detail=str(e))


# v4 (2026-05-12): /force-fail endpoint removed — "强制失败" is not part of
# the canonical action matrix; users either cancel or let the task fail naturally.




@router.post("/api/tasks/{task_id}/force-start")
def force_start_task(task_id: str, request: Request):
    """Force-start a waiting_dependency task."""
    daemon = _get_daemon(request)
    try:
        result = daemon.force_start(task_id)
        return result
    except TaskNotFound:
        raise HTTPException(404, detail=f"Task {task_id} not found")
    except ValueError as e:
        err_str = str(e)
        if "UPSTREAM_NO_OUTPUT" in err_str:
            raise HTTPException(409, detail=err_str)
        raise HTTPException(400, detail=err_str)


@router.get("/api/tasks/{parent_id}/summary")
def batch_summary(parent_id: str, request: Request):
    """Get aggregated status summary for a batch task."""
    daemon = _get_daemon(request)
    parent = daemon.store.get_task(parent_id)
    if parent is None:
        raise HTTPException(404, detail=f"Task {parent_id} not found")

    if not daemon.batch_orchestrator:
        raise HTTPException(500, detail="Batch orchestrator not available")

    summary = daemon.batch_orchestrator.get_batch_summary(parent_id)
    return summary


@router.post("/api/tasks/{parent_id}/retry-failed")
def retry_failed_children(parent_id: str, request: Request):
    """Retry all failed/cancelled children of a batch task."""
    from crawlhub.core.telemetry import emit_task_retried

    daemon = _get_daemon(request)
    parent = daemon.store.get_task(parent_id)
    _platform = (parent or {}).get("platform", "") if parent else ""
    _task_type = (parent or {}).get("task_type", "") if parent else ""
    _status = 200

    if parent is None:
        emit_task_retried(
            task_id=parent_id,
            platform="",
            task_type="",
            retry_scope="batch_failed_only",
            status_code=404,
        )
        raise HTTPException(404, detail=f"Task {parent_id} not found")

    if not daemon.batch_orchestrator:
        emit_task_retried(
            task_id=parent_id,
            platform=_platform,
            task_type=_task_type,
            retry_scope="batch_failed_only",
            status_code=500,
        )
        raise HTTPException(500, detail="Batch orchestrator not available")

    try:
        # Use apply_parent_action so cancellation_intent is cleared per spec §1.4.
        daemon.apply_parent_action(parent_id, "failed_retry", actor="user")
        retried_ids = []  # apply_parent_action handles re-execution itself
    except ValueError as e:
        _status = 400
        emit_task_retried(
            task_id=parent_id,
            platform=_platform,
            task_type=_task_type,
            retry_scope="batch_failed_only",
            status_code=_status,
        )
        raise HTTPException(400, detail=str(e))

    # Re-execute the batch if there are retried children
    if retried_ids:
        executor = daemon._executors.get(parent["platform"], daemon._executors["_default"])
        executor.submit(daemon._execute_batch_task, parent_id)

    emit_task_retried(
        task_id=parent_id,
        platform=_platform,
        task_type=_task_type,
        retry_scope="batch_failed_only",
        status_code=_status,
    )
    return {
        "retried_count": len(retried_ids),
        "retried_task_ids": retried_ids,
    }


@router.post("/api/tasks/{parent_id}/force-succeeded")
def force_succeeded_parent(parent_id: str, request: Request):
    """B4: force every non-succeeded child to succeeded (spec §2.2)."""
    daemon = _get_daemon(request)
    parent = daemon.store.get_task(parent_id)
    if parent is None:
        raise HTTPException(404, detail=f"Task {parent_id} not found")
    try:
        result = daemon.apply_parent_action(parent_id, "force_succeeded", actor="user")
        return result
    except ValueError as e:
        raise HTTPException(400, detail=str(e))


# v4 (2026-05-12): /pause, /resume, /continue endpoints removed.
# Users who want to "continue after cancellation" go through /retry
# (full_retry), which preserves output_dir.


@router.delete("/api/tasks/{task_id}")
def delete_task(
    task_id: str,
    request: Request,
    purge: bool = Query(default=False, description="If true, permanently delete (skip recycle bin). Caller must confirm."),
):
    """Move a task to the recycle bin (or permanently delete with ?purge=true).

    Soft-delete (default):
      * Marks `archived_at = now()` on the task and cascades to all batch
        children. The on-disk output and logs are NOT touched — they live
        until either the user purges them manually or the auto-purge
        scheduler hits (archived_purge_days, default 30).
      * Pre-conditions:
          - task must be top-level (no parent_task_id). Children of a
            batch parent are deleted automatically when the parent is
            archived; they have no individual recycle-bin lifecycle.
          - task must be in a terminal status. Running / queued tasks
            cannot be archived (cancel them first).
          - no active (non-archived) downstream task may depend on this
            task. Archive the downstream task(s) first.

    Hard-delete (?purge=true):
      * Same pre-conditions as soft-delete (i.e. you can purge a live
        task only by going via archive first — the UX puts a confirm
        dialog between archive and purge anyway).
      * Permanently removes: tasks row(s), task_status_transitions,
        record_samples, output directory, per-task log directory.
      * Used by 'Purge all' in the recycle-bin tab and the per-task
        'Delete forever' button.
    """
    daemon = _get_daemon(request)
    task = daemon.store.get_task(task_id)
    if task is None:
        raise HTTPException(404, detail=f"Task {task_id} not found")

    if not purge:
        # ---- Soft-delete (archive) path ----
        # 1) top-level only
        if task.get("parent_task_id"):
            raise HTTPException(
                400,
                detail={
                    "code": "not_top_level",
                    "message": "Only top-level tasks (single_run / batch_run) can be deleted. "
                               "Children of a batch are archived together with their parent.",
                },
            )
        # 2) terminal status only
        terminal_statuses = {
            TaskStatus.SUCCEEDED.value,
            TaskStatus.PARTIAL_SUCCEEDED.value,
            TaskStatus.FAILED.value,
            TaskStatus.CANCELLED.value,
            TaskStatus.INTERRUPTED.value,
        }
        if task.get("status") not in terminal_statuses:
            raise HTTPException(
                409,
                detail={
                    "code": "not_terminal",
                    "message": f"Cannot delete task in status '{task.get('status')}'. "
                               "Cancel or wait for it to finish first.",
                },
            )
        # 3) refuse if any active (non-archived) downstream depends on this
        lineage = daemon.store.get_lineage(task_id, limit=1000)
        active_downstream = [
            t for t in lineage.get("downstream", [])
            if t.get("archived_at") is None
        ]
        if active_downstream:
            raise HTTPException(
                409,
                detail={
                    "code": "has_downstream",
                    "message": f"{len(active_downstream)} downstream task(s) still depend on this. "
                               "Delete them first.",
                    "downstream": [
                        {"task_id": t["task_id"], "status": t.get("status")}
                        for t in active_downstream[:10]
                    ],
                },
            )
        daemon.store.archive_task(task_id)
        return {"status": "archived", "task_id": task_id}

    # ---- Hard-delete (purge) path ----
    # Same pre-conditions as soft-delete: only top-level + terminal +
    # no active downstream. (You can purge an already-archived task whose
    # status is whatever terminal value it had.)
    if task.get("parent_task_id"):
        raise HTTPException(
            400,
            detail={"code": "not_top_level", "message": "Only top-level tasks can be purged."},
        )
    if task.get("archived_at") is None:
        # Refuse: prevents accidental nuke of live tasks.
        terminal_statuses = {
            TaskStatus.SUCCEEDED.value, TaskStatus.PARTIAL_SUCCEEDED.value,
            TaskStatus.FAILED.value, TaskStatus.CANCELLED.value,
            TaskStatus.INTERRUPTED.value,
        }
        if task.get("status") not in terminal_statuses:
            raise HTTPException(
                409,
                detail={"code": "not_terminal", "message": "Archive before purging non-terminal tasks."},
            )
    # Collect on-disk paths BEFORE we drop the rows.
    out_dir = task.get("output_dir") or ""
    # Delete output directory (best-effort; missing dir is fine).
    if out_dir:
        out_path = Path(out_dir)
        if out_path.exists() and out_path.is_dir():
            import shutil
            shutil.rmtree(out_path, ignore_errors=True)
    # Delete per-task log directories under logs/tasks/<date>/<task_id>_*
    logs_root = daemon.data_root / "logs" / "tasks"
    if logs_root.exists():
        prefix = f"{task_id}_"
        import shutil
        for date_dir in logs_root.iterdir():
            if not date_dir.is_dir():
                continue
            for entry in date_dir.iterdir():
                if entry.is_dir() and entry.name.startswith(prefix):
                    shutil.rmtree(entry, ignore_errors=True)
    # DB cascade: tasks + transitions + record_samples (children pulled in too).
    rows = daemon.store.purge_task(task_id)
    return {"status": "purged", "task_id": task_id, "rows_deleted": rows}


@router.post("/api/tasks/{task_id}/restore")
def restore_task(task_id: str, request: Request):
    """Restore an archived task (and its children) from the recycle bin."""
    daemon = _get_daemon(request)
    task = daemon.store.get_task(task_id)
    if task is None:
        raise HTTPException(404, detail=f"Task {task_id} not found")
    if task.get("archived_at") is None:
        raise HTTPException(400, detail={"code": "not_archived", "message": "Task is not in the recycle bin."})
    if task.get("parent_task_id"):
        raise HTTPException(
            400,
            detail={"code": "not_top_level", "message": "Restore the parent task; children come back with it."},
        )
    daemon.store.restore_task(task_id)
    return {"status": "restored", "task_id": task_id}


@router.get("/api/trash")
def list_trash(
    request: Request,
    limit: int = Query(default=200, le=1000),
    offset: int = Query(default=0, ge=0),
    search: str | None = None,
):
    """List archived (recycle-bin) tasks. Top-level only — children are
    archived alongside their parent and are not separately addressable here.
    """
    daemon = _get_daemon(request)
    return daemon.store.list_tasks(
        only_archived=True,
        limit=limit,
        offset=offset,
        search=search,
    )


@router.post("/api/trash/purge_all")
def purge_all_trash(request: Request):
    """Permanently delete every task currently in the recycle bin.

    Removes DB rows + on-disk output + per-task logs. The frontend MUST
    show a confirmation dialog before calling this.
    """
    daemon = _get_daemon(request)
    archived = daemon.store.list_tasks(only_archived=True, limit=100000)
    purged = 0
    failed = 0
    import shutil
    logs_root = daemon.data_root / "logs" / "tasks"
    for task in archived:
        task_id = task["task_id"]
        try:
            out_dir = task.get("output_dir") or ""
            if out_dir:
                out_path = Path(out_dir)
                if out_path.exists() and out_path.is_dir():
                    shutil.rmtree(out_path, ignore_errors=True)
            if logs_root.exists():
                prefix = f"{task_id}_"
                for date_dir in logs_root.iterdir():
                    if not date_dir.is_dir():
                        continue
                    for entry in date_dir.iterdir():
                        if entry.is_dir() and entry.name.startswith(prefix):
                            shutil.rmtree(entry, ignore_errors=True)
            daemon.store.purge_task(task_id)
            purged += 1
        except Exception:
            failed += 1
    return {"status": "ok", "purged": purged, "failed": failed}


@router.get("/api/tasks/{task_id}/open-dir")
def open_task_dir(task_id: str, request: Request):
    """Open task output directory in file manager."""
    daemon = _get_daemon(request)
    task = daemon.store.get_task(task_id)
    if task is None:
        raise HTTPException(404, detail=f"Task {task_id} not found")

    output_dir = task.get("output_dir", "")
    if not output_dir:
        raise HTTPException(400, detail="Task has no output directory")

    # Security: validate path is under ~/.crawlhub/output/
    output_root = str(get_data_root() / "output")
    resolved = str(Path(output_dir).resolve())
    if ".." in output_dir or not resolved.startswith(output_root):
        raise HTTPException(400, detail="Invalid path")

    if not Path(output_dir).exists():
        raise HTTPException(404, detail="Output directory does not exist")

    # Open in file manager
    system = platform_mod.system()
    try:
        if system == "Windows":
            subprocess.Popen(["explorer.exe", "/select,", resolved])
        elif system == "Darwin":
            subprocess.Popen(["open", "-R", resolved])
        else:
            subprocess.Popen(["xdg-open", resolved])
    except OSError as e:
        raise HTTPException(500, detail=f"Failed to open file manager: {e}")

    return {"status": "ok", "path": resolved}


@router.post("/api/tasks/{task_id}/export")
def export_task(task_id: str, body: ExportRequest, request: Request):
    """Export task results to file."""
    daemon = _get_daemon(request)
    task = daemon.store.get_task(task_id)
    if task is None:
        raise HTTPException(404, detail=f"Task {task_id} not found")

    output_dir = task.get("output_dir", "")
    data_path = Path(output_dir) / "data.jsonl"
    if not data_path.exists():
        raise HTTPException(404, detail="No data.jsonl found for this task")

    # Determine output path
    if body.output_path:
        export_path = Path(body.output_path)
    else:
        tmp_dir = get_data_root() / "tmp"
        tmp_dir.mkdir(exist_ok=True)
        ts = int(time.time())
        ext = body.format if body.format != "xlsx" else "xlsx"
        export_path = tmp_dir / f"export_{task_id}_{ts}.{ext}"

    # Read all records
    records = daemon.blob_store.read_records(output_dir, offset=0, limit=999999)

    if body.format == "jsonl":
        with open(export_path, "w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    elif body.format == "json":
        with open(export_path, "w", encoding="utf-8") as f:
            json.dump(records, f, ensure_ascii=False, indent=2)
    elif body.format == "csv":
        if records:
            with open(export_path, "w", encoding="utf-8-sig", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=records[0].keys())
                writer.writeheader()
                writer.writerows(records)
        else:
            export_path.write_text("")
    elif body.format == "xlsx":
        try:
            from openpyxl import Workbook

            wb = Workbook()
            ws = wb.active
            if records:
                headers = list(records[0].keys())
                ws.append(headers)
                for r in records:
                    ws.append([str(r.get(h, "")) for h in headers])
            wb.save(str(export_path))
        except ImportError:
            raise HTTPException(500, detail="openpyxl not installed for xlsx export")
    else:
        raise HTTPException(400, detail=f"Unsupported format: {body.format}")

    return {
        "status": "ok",
        "path": str(export_path),
        "size": export_path.stat().st_size,
        "rows": len(records),
    }


@router.get("/api/tasks/{task_id}/result")
def read_task_result(
    task_id: str,
    request: Request,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, le=1000),
    filter: str | None = None,
):
    """Read task result records with pagination."""
    daemon = _get_daemon(request)
    task = daemon.store.get_task(task_id)
    if task is None:
        raise HTTPException(404, detail=f"Task {task_id} not found")

    output_dir = task.get("output_dir", "")

    # For batch_run tasks that are still running, aggregate from completed children
    if task.get("task_type") == "batch_run" and task.get("status") == "running":
        return _read_batch_running_result(daemon, task_id, output_dir, offset, limit, filter)

    if not output_dir:
        return {"records": [], "total": 0}

    records = daemon.blob_store.read_records(output_dir, offset=offset, limit=limit, filter_expr=filter)
    summary = daemon.blob_store.get_summary(output_dir)

    # Calculate total rows from summary or by counting lines
    total_rows = 0
    if summary and "record_count" in summary:
        total_rows = summary["record_count"]
    else:
        data_path = Path(output_dir) / "data.jsonl"
        if data_path.exists():
            try:
                with open(data_path, "r", encoding="utf-8") as f:
                    total_rows = sum(1 for line in f if line.strip())
            except Exception:
                total_rows = len(records)

    return {
        "task_id": task_id,
        "output_dir": output_dir,
        "summary": summary,
        "records": records,
        "total_rows": total_rows,
        "offset": offset,
        "limit": limit,
        "count": len(records),
    }


def _read_batch_running_result(daemon, parent_task_id: str, parent_output_dir: str, offset: int, limit: int, filter_expr: str | None):
    """Aggregate results from completed child tasks for a running batch parent."""
    import json as _json
    from crawlhub.core.blob_store import _matches_filter

    children = daemon.store.list_tasks(parent_id=parent_task_id, limit=10000)
    # Only read from children that have completed and have output
    completed_children = [
        c for c in children
        if c.get("status") == "succeeded" and c.get("output_dir")
    ]

    # Count total rows across all completed children
    total_rows = 0
    child_data_paths = []
    for child in completed_children:
        data_path = Path(child["output_dir"]) / "data.jsonl"
        if data_path.exists():
            try:
                with open(data_path, "r", encoding="utf-8") as f:
                    row_count = sum(1 for line in f if line.strip())
                child_data_paths.append((data_path, row_count))
                total_rows += row_count
            except Exception:
                pass

    # Read records with offset/limit across all child data files
    records = []
    current = 0
    for data_path, _ in child_data_paths:
        if len(records) >= limit:
            break
        try:
            with open(data_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    if current < offset:
                        current += 1
                        continue
                    if len(records) >= limit:
                        break
                    try:
                        record = _json.loads(line)
                        if filter_expr:
                            if _matches_filter(record, filter_expr):
                                records.append(record)
                        else:
                            records.append(record)
                    except _json.JSONDecodeError:
                        continue
                    current += 1
        except Exception:
            continue

    return {
        "task_id": parent_task_id,
        "output_dir": parent_output_dir or "(aggregated from children)",
        "summary": {"record_count": total_rows, "completed_children": len(completed_children), "total_children": len(children)},
        "records": records,
        "total_rows": total_rows,
        "offset": offset,
        "limit": limit,
        "count": len(records),
    }


@router.get("/api/tasks/{task_id}/logs")
def get_task_logs(
    task_id: str,
    request: Request,
    tail: int = Query(default=200, le=5000),
    since: Optional[str] = Query(default=None, description="ISO datetime filter, e.g. 2026-05-21T08:00:00"),
):
    """Get task log tail, optionally filtered by time."""

    daemon = _get_daemon(request)
    task = daemon.store.get_task(task_id)
    if task is None:
        raise HTTPException(404, detail=f"Task {task_id} not found")

    # Find log file
    log_dir = get_data_root() / "logs" / "tasks"
    log_file = None
    for date_dir in sorted(log_dir.iterdir(), reverse=True) if log_dir.exists() else []:
        candidate = date_dir / f"{task_id}.log"
        if candidate.exists():
            log_file = candidate
            break

    if log_file is None:
        return {"task_id": task_id, "lines": [], "total_lines": 0}

    # Parse since parameter into a timestamp for filtering
    since_ts: Optional[float] = None
    if since:
        since = since.strip()
        for fmt in (
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%dT%H:%M",
            "%Y-%m-%d",
        ):
            try:
                dt = datetime.strptime(since, fmt)
                since_ts = dt.replace(tzinfo=timezone.utc).timestamp()
                break
            except ValueError:
                continue

    # Read and filter lines
    with open(log_file, "r", encoding="utf-8") as f:
        all_lines = f.readlines()

    # Filter by since if provided — parse timestamp from log line prefix
    if since_ts is not None:
        filtered = []
        for line in all_lines:
            # Match [2026-05-21 16:43:28] at line start
            m = re.match(r"\[(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2})\]", line)
            if m:
                try:
                    line_dt = datetime.strptime(f"{m.group(1)} {m.group(2)}", "%Y-%m-%d %H:%M:%S")
                    line_ts = line_dt.replace(tzinfo=timezone.utc).timestamp()
                    if line_ts >= since_ts:
                        filtered.append(line)
                except ValueError:
                    # If we can't parse, keep the line (defensive)
                    filtered.append(line)
            else:
                # Line without timestamp prefix — keep it
                filtered.append(line)
        all_lines = filtered

    lines = all_lines[-tail:] if tail < len(all_lines) else all_lines

    return {
        "task_id": task_id,
        "lines": [l.rstrip("\n") for l in lines],
        "total_lines": len(all_lines),
    }


@router.get("/api/logs/daemon")
def get_daemon_logs(
    request: Request,
    tail: int = Query(default=200, le=5000),
    since: Optional[str] = Query(default=None, description="ISO datetime filter, e.g. 2026-05-21T08:00:00"),
):
    """Get daemon log tail, optionally filtered by time.

    Log file location: {data_root}/logs/daemon.log
    Supports log rotation (daemon.log, daemon.log.1, daemon.log.2, ...).
    """

    log_dir = get_data_root() / "logs"
    log_file = log_dir / "daemon.log"

    if not log_file.exists():
        return {"lines": [], "total_lines": 0}

    # Parse since parameter
    since_ts: Optional[float] = None
    if since:
        since = since.strip()
        for fmt in (
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%dT%H:%M",
            "%Y-%m-%d",
        ):
            try:
                d = datetime.strptime(since, fmt)
                since_ts = d.replace(tzinfo=timezone.utc).timestamp()
                break
            except ValueError:
                continue

    # Read all lines from daemon.log (rotation files not included for simplicity)
    with open(log_file, "r", encoding="utf-8") as f:
        all_lines = f.readlines()

    # Filter by since if provided
    if since_ts is not None:
        filtered = []
        for line in all_lines:
            m = re.match(r"\[(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2})\]", line)
            if m:
                try:
                    line_dt = datetime.strptime(f"{m.group(1)} {m.group(2)}", "%Y-%m-%d %H:%M:%S")
                    line_ts = line_dt.replace(tzinfo=timezone.utc).timestamp()
                    if line_ts >= since_ts:
                        filtered.append(line)
                except ValueError:
                    filtered.append(line)
            else:
                filtered.append(line)
        all_lines = filtered

    lines = all_lines[-tail:] if tail < len(all_lines) else all_lines

    return {
        "lines": [l.rstrip("\n") for l in lines],
        "total_lines": len(all_lines),
    }


# --- Platform endpoints ---

@router.get("/api/platforms")
def list_platforms(request: Request):
    """Return registered platforms and their actions.

    Each action also exposes ``output_schema`` (when declared by the service
    class). Actions without an output_schema may not be used as run_id sources
    in the items_from SQL pipeline (L2 schema check rejects them).

    ``display_name`` and ``description`` come from the platform's
    ``plugin.yaml`` (``display_name:`` / ``description:`` fields). When a
    manifest is unavailable (defensive fallback, shouldn't happen for
    registered platforms) ``display_name`` falls back to the bare
    ``platform`` name and ``description`` to empty string.
    """
    from crawlhub.core.registry import (
        create_platform_service,
        get_output_schema,
        get_platform_manifest,
    )

    registry = get_registry()
    platforms = []
    for name, svc_cls in registry.items():
        svc = create_platform_service(name)
        manifest_for_actions = get_platform_manifest(name)
        actions = []
        for action_name in svc.list_actions():
            schema = svc.get_action_schema(action_name)
            output_schema = get_output_schema(name, action_name)
            # Schema-v2 enrichment: ``output_schema_v2`` carries label +
            # description per field, used by the frontend action-schema modal
            # and the drawer column-header tooltips. v1 ``output_schema`` is
            # preserved for legacy callers (CLI fallback, items_from L2).
            output_schema_v2 = None
            action_display_name = ""
            if manifest_for_actions is not None:
                action_def = manifest_for_actions.actions.get(action_name)
                if action_def is not None:
                    action_display_name = action_def.display_name or ""
                    if action_def.output_schema:
                        output_schema_v2 = action_def.get_output_schema_v2()
            actions.append({
                "name": action_name,
                "display_name": action_display_name or action_name,
                "description": schema.get("description", ""),
                "schema": schema,
                "output_schema": output_schema,  # None if not declared
                "output_schema_v2": output_schema_v2,  # v2 form: {field: {type, label, description}}
                "has_output_schema": output_schema is not None,
            })
        manifest = manifest_for_actions
        platforms.append({
            "platform": name,
            "display_name": (manifest.display_name if manifest else "") or name,
            "description": manifest.description if manifest else "",
            "version": manifest.version if manifest else "",
            "actions": actions,
        })
    return {"platforms": platforms}


@router.get("/api/actions/{platform}/{action}/schema")
def get_action_output_schema_endpoint(platform: str, action: str):
    """Return the declared output_schema for a (platform, action).

    Used by the frontend SQL editor to give column hints / autocomplete,
    and by the action-schema modal for drawer / task-creation flows.
    Returns 404 if the action has no declared output_schema.

    Both v1 (``output_schema``: ``{field: type_str}``) and v2
    (``output_schema_v2``: ``{field: {type, label, description}}``) forms
    are returned so the frontend can display rich labels while CLI / SQL
    consumers keep using the bare type form.
    """
    from crawlhub.core.registry import get_output_schema, get_platform_manifest

    schema = get_output_schema(platform, action)
    if schema is None:
        raise HTTPException(
            404,
            detail=f"No output_schema declared for {platform}/{action}",
        )
    manifest = get_platform_manifest(platform)
    schema_v2 = None
    action_description = ""
    input_schema = None
    if manifest is not None:
        action_def = manifest.actions.get(action)
        if action_def is not None:
            schema_v2 = action_def.get_output_schema_v2()
            action_description = action_def.description or ""
            input_schema = action_def.input_schema or None
    return {
        "platform": platform,
        "action": action,
        "description": action_description,
        "input_schema": input_schema,
        "output_schema": schema,
        "output_schema_v2": schema_v2,
    }


class ItemsFromPreviewRequest(BaseModel):
    sources: dict[str, Any]
    sql: str
    field: str
    dedup: bool = True
    limit: int = 10
    timeout_s: float = 10.0


@router.post("/api/items_from/preview")
def preview_items_from_endpoint(body: ItemsFromPreviewRequest, request: Request):
    """Run L0/L1/L2 validation and (if upstreams ready) preview sample items.

    Behavior per requirements:
    - Always run L0/L1/L2 validation. Validation failure → 400 with error_code.
    - For run_id sources, every referenced task must be in a terminal-success
      state (completed / partial_failed) AND have data on disk. If any upstream
      is still in flight → 409 (validation passed but data unavailable yet).
    - Otherwise execute the SQL with LIMIT and return rows + extracted field
      values + the resolved item count after dedup.
    """
    from crawlhub.core.sql_validator import validate_items_from
    from crawlhub.core.sql_runner import preview_items_from
    from crawlhub.core.sql_errors import (
        SQLItemsFromError,
        ArtifactNotReadyError,
    )
    from crawlhub.core.models import TaskStatus

    daemon = _get_daemon(request)
    items_from = {
        "sources": body.sources,
        "sql": body.sql,
        "field": body.field,
        "dedup": body.dedup,
    }

    # 1) L0/L1/L2 validation — always runs.
    try:
        validate_items_from(items_from, daemon.store)
    except SQLItemsFromError as e:
        raise HTTPException(400, detail={
            "error_code": e.__class__.__name__,
            "message": str(e),
            "stage": "validation",
        })

    # 2) Check every run_id source is terminally readable.
    not_ready: list[dict[str, str]] = []
    for alias, ref in body.sources.items():
        if not isinstance(ref, dict) or "run_id" not in ref:
            continue
        upstream = daemon.store.get_task(ref["run_id"])
        if upstream is None:
            raise HTTPException(404, detail={
                "error_code": "ArtifactNotFoundError",
                "message": f"Source task not found: {ref['run_id']} (alias '{alias}')",
            })
        status = upstream.get("status", "")
        # Recycle-bin: refuse archived sources outright. Files might still
        # be on disk pre-auto-purge, but the user explicitly removed them
        # from the active set. Surface a 409 so the UI can prompt to restore.
        is_archived = upstream.get("archived_at") is not None
        if is_archived:
            raise HTTPException(409, detail={
                "error_code": "ArtifactArchivedError",
                "message": f"Source task {ref['run_id']} (alias '{alias}') is in the recycle bin; restore it or pick another source.",
            })
        if status not in (TaskStatus.SUCCEEDED.value, TaskStatus.PARTIAL_SUCCEEDED.value):
            not_ready.append({"alias": alias, "run_id": ref["run_id"], "status": status})

    if not_ready:
        # Validation passed but actual preview can't run yet — 409 is the right
        # code: "the request is valid, the resource just isn't in a state that
        # allows the operation".
        raise HTTPException(409, detail={
            "error_code": "ArtifactNotReadyError",
            "message": "Validation passed but one or more upstream tasks are not yet completed",
            "not_ready": not_ready,
        })

    # 3) Execute with LIMIT. Catch runtime errors (timeout, etc.) cleanly.
    try:
        result = preview_items_from(
            items_from,
            daemon.store,
            limit=max(1, min(body.limit, 100)),
            timeout_s=max(1.0, min(body.timeout_s, 30.0)),
        )
    except ArtifactNotReadyError as e:
        # Race: source went un-ready between the check above and execution.
        raise HTTPException(409, detail={
            "error_code": "ArtifactNotReadyError",
            "message": str(e),
        })
    except SQLItemsFromError as e:
        raise HTTPException(400, detail={
            "error_code": e.__class__.__name__,
            "message": str(e),
            "stage": "execution",
        })

    # 4) Surface the final batch-input view: extract the configured field, run
    #    the same dedup the runner uses, so the user sees exactly what would
    #    be sent into the batch.
    rows = result.get("rows", [])
    field_col = result.get("field_column") or body.field
    raw_items = [r.get(field_col) for r in rows if r.get(field_col) is not None]
    if body.dedup:
        seen: set = set()
        items_preview: list = []
        for v in raw_items:
            if v not in seen:
                seen.add(v)
                items_preview.append(v)
    else:
        items_preview = list(raw_items)

    return {
        **result,
        "items_preview": items_preview,
        "items_preview_count": len(items_preview),
        "raw_items_count": len(raw_items),
        "dedup_enabled": body.dedup,
        "limit_applied": max(1, min(body.limit, 100)),
        "note": (
            "items_preview shows the first N rows after LIMIT and dedup; "
            "the actual batch may produce more items if the underlying data "
            "exceeds the preview limit."
        ),
    }


# --- Notification endpoints ---

@router.get("/api/notifications/channels")
def list_channels(request: Request):
    """List notification channels."""
    daemon = _get_daemon(request)
    return daemon.store.list_channels()


@router.post("/api/notifications/channels")
def upsert_channel(body: ChannelRequest, request: Request):
    """Create or update a notification channel.

    webhook_url is optional: when only toggling enabled/disabled,
    the frontend may omit it — in that case, preserve the existing value.
    """
    daemon = _get_daemon(request)
    store = daemon.store

    webhook_url = body.webhook_url
    # If webhook_url is not provided, try to preserve the existing value
    if webhook_url is None:
        existing = next((c for c in store.list_channels() if c["name"] == body.name), None)
        if existing:
            webhook_url = existing["webhook_url"]
        else:
            raise HTTPException(400, detail="webhook_url is required when creating a new channel")

    return store.upsert_channel({
        "name": body.name,
        "webhook_url": webhook_url,
        "enabled": 1 if body.enabled else 0,
        "created_at": time.time(),
    })


@router.delete("/api/notifications/channels/{name}")
def delete_channel(name: str, request: Request):
    """Delete a notification channel."""
    daemon = _get_daemon(request)
    if daemon.store.delete_channel(name):
        return {"status": "deleted"}
    raise HTTPException(404, detail=f"Channel {name} not found")


@router.get("/api/notifications/rules")
def list_rules(request: Request):
    """List notification rules."""
    daemon = _get_daemon(request)
    return daemon.store.list_rules()


@router.post("/api/notifications/rules")
def upsert_rule(body: RuleRequest, request: Request):
    """Create or update a notification rule."""
    import uuid
    daemon = _get_daemon(request)
    rule_id = body.rule_id or uuid.uuid4().hex[:12]
    return daemon.store.upsert_rule({
        "rule_id": rule_id,
        "event_type": body.event_type,
        "channel_name": body.channel_name,
        "enabled": 1 if body.enabled else 0,
        "created_at": time.time(),
    })


@router.delete("/api/notifications/rules/{rule_id}")
def delete_rule(rule_id: str, request: Request):
    """Delete a notification rule."""
    daemon = _get_daemon(request)
    if daemon.store.delete_rule(rule_id):
        return {"status": "deleted"}
    raise HTTPException(404, detail=f"Rule {rule_id} not found")


@router.post("/api/notifications/test")
def test_notification(body: NotificationTestRequest, request: Request):
    """Send a test notification via webhook."""
    daemon = _get_daemon(request)
    if not hasattr(daemon, 'notification_service') or daemon.notification_service is None:
        raise HTTPException(500, detail="NotificationService not initialized")
    channels = daemon.store.list_channels()
    channel = next((c for c in channels if c["name"] == body.channel), None)
    if not channel:
        raise HTTPException(404, detail=f"Channel '{body.channel}' not found. Available channels: {[c['name'] for c in channels]}")
    success = daemon.notification_service.send_test(body.channel)
    if success:
        return {"status": "ok", "message": f"Test message sent to channel: {body.channel}"}
    else:
        raise HTTPException(400, detail=f"Failed to send test message to channel: {body.channel}. Check webhook URL.")


# --- Scheduling plans (PR-1: pure CRUD; PlanScheduler.sync_plan hook lands in PR-2) ---
#
# Storage notes:
# * ``request_payload`` is stored as a TEXT JSON string. Endpoint helpers
#   handle (de)serialization so callers always see plain dicts.
# * The DB layer doesn't enforce trigger.kind/expr grammar — that lives here.

import json as _json
import re as _re
import uuid as _uuid
from zoneinfo import ZoneInfo as _ZoneInfo, ZoneInfoNotFoundError as _ZoneInfoNotFoundError


def _validate_cron(expr: str) -> None:
    """Raise HTTPException(422) if expr is not a valid 5-field crontab string."""
    try:
        from apscheduler.triggers.cron import CronTrigger  # heavy import deferred
        CronTrigger.from_crontab(expr)
    except Exception as e:
        raise HTTPException(422, detail=f"Invalid cron expr {expr!r}: {e}")


def _validate_interval(expr: str) -> None:
    """Interval expr must be a JSON object with EXACTLY one of seconds/minutes/hours/days, value int >= 1."""
    try:
        obj = _json.loads(expr)
    except Exception:
        raise HTTPException(422, detail=f"Interval expr must be JSON: {expr!r}")
    if not isinstance(obj, dict) or len(obj) != 1:
        raise HTTPException(422, detail=f"Interval must be a single-key object, got {expr!r}")
    key, val = next(iter(obj.items()))
    if key not in ("seconds", "minutes", "hours", "days"):
        raise HTTPException(422, detail=f"Interval key must be seconds/minutes/hours/days, got {key!r}")
    if not isinstance(val, int) or val < 1:
        raise HTTPException(422, detail=f"Interval value must be int >= 1, got {val!r}")


def _validate_once(expr: str) -> None:
    """`once` expr is an ISO-8601 datetime. Plain `fromisoformat` is enough; tz is plan-level."""
    try:
        from datetime import datetime as _dt
        _dt.fromisoformat(expr)
    except Exception as e:
        raise HTTPException(422, detail=f"Invalid once expr {expr!r}: {e}")


def _validate_timezone(tz: str) -> None:
    try:
        _ZoneInfo(tz)
    except _ZoneInfoNotFoundError:
        raise HTTPException(422, detail=f"Unknown timezone {tz!r}")


_STEP_REF_RE = _re.compile(r"\$\{step\[(\d+)\]\.task_id\}")


def _scan_step_refs(value: Any) -> list[int]:
    """Return all integer indices appearing in ${step[K].task_id} occurrences within ``value``."""
    found: list[int] = []
    if isinstance(value, str):
        found.extend(int(m.group(1)) for m in _STEP_REF_RE.finditer(value))
    elif isinstance(value, dict):
        for v in value.values():
            found.extend(_scan_step_refs(v))
    elif isinstance(value, list):
        for v in value:
            found.extend(_scan_step_refs(v))
    return found


def _validate_step_refs(steps: list[PlanStepInput]) -> None:
    """Forbid ``${step[K].task_id}`` where K >= the current step's own index."""
    for i, step in enumerate(steps):
        for k in _scan_step_refs(step.request_payload):
            if k >= i:
                raise HTTPException(
                    422,
                    detail=(
                        f"Forward reference rejected at step[{i}].request_payload: "
                        f"${{step[{k}].task_id}} (must reference an earlier step)"
                    ),
                )


def _validate_triggers(triggers: list[PlanTriggerInput]) -> None:
    for t in triggers:
        if t.kind == "cron":
            _validate_cron(t.expr)
        elif t.kind == "interval":
            _validate_interval(t.expr)
        elif t.kind == "once":
            _validate_once(t.expr)
        else:
            raise HTTPException(422, detail=f"Unknown trigger kind {t.kind!r}")


def _step_to_db(step: PlanStepInput) -> dict:
    return {
        "request_kind": step.request_kind,
        "platform": step.platform,
        "task_type": step.task_type,
        "request_payload": _json.dumps(step.request_payload, ensure_ascii=False),
        "note": step.note,
    }


def _step_from_db(row: dict) -> dict:
    out = dict(row)
    raw = out.get("request_payload")
    if isinstance(raw, str):
        try:
            out["request_payload"] = _json.loads(raw)
        except Exception:
            # The string may be a Python repr (e.g. "{'app_id': '730'}")
            # instead of valid JSON — try to salvage by safe literal eval.
            import ast
            try:
                out["request_payload"] = ast.literal_eval(raw)
            except Exception:
                logger.warning(
                    "[_step_from_db] Cannot parse request_payload as JSON or Python literal, "
                    "leaving as string: %r", raw[:120]
                )
    return out


def _plan_payload(daemon, plan_id: str) -> dict:
    """Combine plan + triggers + steps into one response dict."""
    plan = daemon.store.get_plan(plan_id)
    if plan is None:
        return None
    plan["triggers"] = daemon.store.list_plan_triggers(plan_id)
    plan["steps"] = [_step_from_db(s) for s in daemon.store.list_plan_steps(plan_id)]
    return plan


# --- Plan groups ---

@router.get("/api/plan-groups")
def list_plan_groups(request: Request):
    daemon = _get_daemon(request)
    return daemon.store.list_plan_groups()


@router.post("/api/plan-groups")
def create_plan_group(body: PlanGroupCreateRequest, request: Request):
    daemon = _get_daemon(request)
    gid = "g_" + _uuid.uuid4().hex[:10]
    return daemon.store.create_plan_group({
        "group_id": gid,
        "name": body.name,
        "note": body.note,
    })


@router.patch("/api/plan-groups/{group_id}")
def patch_plan_group(group_id: str, body: PlanGroupPatchRequest, request: Request):
    daemon = _get_daemon(request)
    if daemon.store.get_plan_group(group_id) is None:
        raise HTTPException(404, detail=f"Group {group_id} not found")
    updates = {k: v for k, v in body.model_dump(exclude_unset=True).items() if v is not None or k == "note"}
    return daemon.store.update_plan_group(group_id, updates)


@router.delete("/api/plan-groups/{group_id}")
def delete_plan_group(
    group_id: str,
    request: Request,
    confirm: bool = Query(False),
):
    """Group delete with the two safety gates from FR-G-3 / FR-G-4.

    * Any plan with ``enabled=1`` blocks deletion outright (422).
    * Disabled-only plans require an explicit ``?confirm=true`` (409 otherwise);
      on confirm we cascade by calling delete_plan() per child first.
    """
    daemon = _get_daemon(request)
    if daemon.store.get_plan_group(group_id) is None:
        raise HTTPException(404, detail=f"Group {group_id} not found")

    plans = daemon.store.list_plans(group_id=group_id)
    active = [p["plan_id"] for p in plans if p.get("enabled")]
    if active:
        raise HTTPException(
            422,
            detail={
                "message": "Group has enabled plans; disable them first.",
                "blocking_plans": active,
            },
        )
    if plans and not confirm:
        raise HTTPException(
            409,
            detail={
                "message": "Group has disabled plans; pass ?confirm=true to cascade delete.",
                "disabled_plans": [p["plan_id"] for p in plans],
            },
        )
    # cascade: delete each child plan first (which clears triggers/steps + NULLs origin_plan_id)
    for p in plans:
        daemon.store.delete_plan(p["plan_id"])
    daemon.store.delete_plan_group(group_id)
    return {"status": "deleted", "group_id": group_id}


# --- Plans ---

@router.get("/api/plans")
def list_plans(request: Request, group_id: str | None = Query(None)):
    daemon = _get_daemon(request)
    plans = daemon.store.list_plans(group_id=group_id)
    # Attach triggers + steps so the list view can render trigger summaries
    # and step counts without a follow-up detail fetch per row.
    for p in plans:
        pid = p.get("plan_id")
        if not pid:
            continue
        try:
            p["triggers"] = daemon.store.list_plan_triggers(pid)
        except Exception:
            p["triggers"] = []
        try:
            p["steps"] = [_step_from_db(s) for s in daemon.store.list_plan_steps(pid)]
        except Exception:
            p["steps"] = []
    return plans


@router.get("/api/plans/{plan_id}")
def get_plan(plan_id: str, request: Request):
    daemon = _get_daemon(request)
    payload = _plan_payload(daemon, plan_id)
    if payload is None:
        raise HTTPException(404, detail=f"Plan {plan_id} not found")
    return payload


@router.post("/api/plans")
def create_plan(body: PlanWriteRequest, request: Request):
    daemon = _get_daemon(request)
    if daemon.store.get_plan_group(body.group_id) is None:
        raise HTTPException(404, detail=f"Group {body.group_id} not found")
    _validate_timezone(body.timezone)
    _validate_triggers(body.triggers)
    _validate_step_refs(body.steps)

    pid = "p_" + _uuid.uuid4().hex[:10]
    daemon.store.create_plan({
        "plan_id": pid,
        "group_id": body.group_id,
        "name": body.name,
        "enabled": body.enabled,
        "timezone": body.timezone,
        "notify_on_fire_fail": body.notify_on_fire_fail,
        "note": body.note,
    })
    for t in body.triggers:
        daemon.store.create_plan_trigger({
            "trigger_id": "t_" + _uuid.uuid4().hex[:10],
            "plan_id": pid,
            "kind": t.kind,
            "expr": t.expr,
            "enabled": t.enabled,
        })
    daemon.store.replace_plan_steps(pid, [_step_to_db(s) for s in body.steps])
    # PR-2 hook: PlanScheduler.sync_plan(pid) goes here.
    if hasattr(daemon, "plan_scheduler") and daemon.plan_scheduler is not None:
        daemon.plan_scheduler.sync_plan(pid)
    return _plan_payload(daemon, pid)


@router.put("/api/plans/{plan_id}")
def replace_plan(plan_id: str, body: PlanWriteRequest, request: Request):
    daemon = _get_daemon(request)
    if daemon.store.get_plan(plan_id) is None:
        raise HTTPException(404, detail=f"Plan {plan_id} not found")
    if daemon.store.get_plan_group(body.group_id) is None:
        raise HTTPException(404, detail=f"Group {body.group_id} not found")
    _validate_timezone(body.timezone)
    _validate_triggers(body.triggers)
    _validate_step_refs(body.steps)

    daemon.store.update_plan(plan_id, {
        "group_id": body.group_id,
        "name": body.name,
        "enabled": body.enabled,
        "timezone": body.timezone,
        "notify_on_fire_fail": body.notify_on_fire_fail,
        "note": body.note,
    })
    # Replace triggers wholesale: delete existing, then insert.
    for t in daemon.store.list_plan_triggers(plan_id):
        daemon.store.delete_plan_trigger(t["trigger_id"])
    for t in body.triggers:
        daemon.store.create_plan_trigger({
            "trigger_id": "t_" + _uuid.uuid4().hex[:10],
            "plan_id": plan_id,
            "kind": t.kind,
            "expr": t.expr,
            "enabled": t.enabled,
        })
    daemon.store.replace_plan_steps(plan_id, [_step_to_db(s) for s in body.steps])
    # PR-2 hook: PlanScheduler.sync_plan(plan_id)
    if hasattr(daemon, "plan_scheduler") and daemon.plan_scheduler is not None:
        daemon.plan_scheduler.sync_plan(plan_id)
    return _plan_payload(daemon, plan_id)


@router.patch("/api/plans/{plan_id}/enabled")
def patch_plan_enabled(plan_id: str, body: EnabledPatchRequest, request: Request):
    daemon = _get_daemon(request)
    if daemon.store.get_plan(plan_id) is None:
        raise HTTPException(404, detail=f"Plan {plan_id} not found")
    plan = daemon.store.update_plan(plan_id, {"enabled": body.enabled})
    # PR-2 hook: PlanScheduler.sync_plan(plan_id)
    if hasattr(daemon, "plan_scheduler") and daemon.plan_scheduler is not None:
        daemon.plan_scheduler.sync_plan(plan_id)
    return plan


@router.patch("/api/plans/{plan_id}/triggers/{trigger_id}/enabled")
def patch_trigger_enabled(plan_id: str, trigger_id: str, body: EnabledPatchRequest, request: Request):
    daemon = _get_daemon(request)
    # Verify trigger belongs to plan to prevent cross-plan tampering.
    matched = next((t for t in daemon.store.list_plan_triggers(plan_id) if t["trigger_id"] == trigger_id), None)
    if matched is None:
        raise HTTPException(404, detail=f"Trigger {trigger_id} not found under plan {plan_id}")
    updated = daemon.store.update_plan_trigger(trigger_id, {"enabled": body.enabled})
    # PR-2 hook: PlanScheduler.sync_plan(plan_id)
    if hasattr(daemon, "plan_scheduler") and daemon.plan_scheduler is not None:
        daemon.plan_scheduler.sync_plan(plan_id)
    return updated


@router.delete("/api/plans/{plan_id}")
def delete_plan(plan_id: str, request: Request):
    daemon = _get_daemon(request)
    # PR-2: drop scheduled jobs FIRST so a tick can't fire mid-delete.
    if hasattr(daemon, "plan_scheduler") and daemon.plan_scheduler is not None:
        # sync_plan() looks at plan.enabled; we want jobs gone regardless,
        # so flip enabled first, then sync, then delete.
        daemon.store.update_plan(plan_id, {"enabled": 0})
        daemon.plan_scheduler.sync_plan(plan_id)
    if not daemon.store.delete_plan(plan_id):
        raise HTTPException(404, detail=f"Plan {plan_id} not found")
    return {"status": "deleted", "plan_id": plan_id}


# --- Plan run / preview / tasks (PR-2) ---


@router.post("/api/plans/{plan_id}/run")
def run_plan_manual(plan_id: str, body: PlanRunRequest, request: Request):
    """Manually fire a plan immediately.

    ``instance_time`` may be:
      - omitted: use ``datetime.now(tz=plan.timezone)``
      - ISO-8601 with offset: used as-is
      - ISO-8601 naive: tz attached from plan.timezone

    Tasks are submitted with ``origin_type='plan_manual'``.
    """
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo as _Zi

    daemon = _get_daemon(request)
    plan = daemon.store.get_plan(plan_id)
    if plan is None:
        raise HTTPException(404, detail=f"Plan {plan_id} not found")
    if not hasattr(daemon, "plan_scheduler") or daemon.plan_scheduler is None:
        raise HTTPException(503, detail="Plan scheduler not initialized")

    tz = _Zi(plan.get("timezone") or "Asia/Shanghai")
    if body.instance_time:
        try:
            dt = _dt.fromisoformat(body.instance_time)
        except ValueError:
            raise HTTPException(422, detail=f"Invalid instance_time: {body.instance_time}")
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz)
    else:
        dt = _dt.now(tz=tz)

    submitted = daemon.plan_scheduler.fire(plan_id, dt, manual=True)
    return {"submitted": submitted}


@router.get("/api/plans/{plan_id}/preview")
def preview_plan(plan_id: str, request: Request,
                 instance_time: str | None = Query(None)):
    """Render every step's templates against ``instance_time`` (or now)
    without submitting any task. Cross-step references resolve to literal
    placeholders ``<step[K].task_id>`` so the user can see the shape.
    """
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo as _Zi

    daemon = _get_daemon(request)
    plan = daemon.store.get_plan(plan_id)
    if plan is None:
        raise HTTPException(404, detail=f"Plan {plan_id} not found")
    if not hasattr(daemon, "plan_scheduler") or daemon.plan_scheduler is None:
        raise HTTPException(503, detail="Plan scheduler not initialized")

    tz = _Zi(plan.get("timezone") or "Asia/Shanghai")
    if instance_time:
        try:
            dt = _dt.fromisoformat(instance_time)
        except ValueError:
            raise HTTPException(422, detail=f"Invalid instance_time: {instance_time}")
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz)
    else:
        dt = _dt.now(tz=tz)

    return daemon.plan_scheduler.preview(plan_id, dt)


@router.get("/api/plans/{plan_id}/tasks")
def list_plan_tasks(plan_id: str, request: Request,
                    limit: int = Query(default=200, le=1000),
                    offset: int = Query(default=0, ge=0),
                    status: str | None = Query(default=None)):
    """Tasks fired by this plan (any origin_type — both 'plan' and 'plan_manual')."""
    daemon = _get_daemon(request)
    if daemon.store.get_plan(plan_id) is None:
        raise HTTPException(404, detail=f"Plan {plan_id} not found")
    return daemon.store.list_tasks(
        origin_plan_id=plan_id, limit=limit, offset=offset, status=status,
    )


# --- Shutdown endpoint ---

@router.post("/api/shutdown")
def shutdown_daemon(request: Request):
    """Graceful shutdown (loopback only)."""
    daemon = _get_daemon(request)
    # Verify loopback
    client_host = request.client.host if request.client else ""
    if client_host not in ("127.0.0.1", "::1", "localhost"):
        raise HTTPException(403, detail="Shutdown only allowed from loopback")

    import threading
    # Shutdown in background thread to allow response to be sent
    threading.Thread(target=daemon.graceful_shutdown, daemon=True).start()
    return {"status": "shutting_down"}


# --- Cleanup endpoint ---

@router.post("/api/cleanup")
def trigger_cleanup(request: Request):
    """Trigger manual retention cleanup."""
    daemon = _get_daemon(request)
    # Simple cleanup: remove old tmp files
    tmp_dir = get_data_root() / "tmp"
    cleaned = 0
    if tmp_dir.exists():
        cutoff = time.time() - 86400  # 24h
        for f in tmp_dir.iterdir():
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink()
                cleaned += 1
    return {"status": "ok", "cleaned_files": cleaned}


# --- WebSocket for task logs ---

@router.websocket("/ws/tasks/{task_id}/logs")
async def ws_task_logs(websocket: WebSocket, task_id: str):
    """WebSocket endpoint for real-time task log streaming."""
    await websocket.accept()

    daemon = websocket.app.state.daemon
    task = daemon.store.get_task(task_id)
    if task is None:
        await websocket.close(code=4004, reason="Task not found")
        return

    # Find log file
    log_dir = get_data_root() / "logs" / "tasks"
    log_file = None
    for date_dir in sorted(log_dir.iterdir(), reverse=True) if log_dir.exists() else []:
        candidate = date_dir / f"{task_id}.log"
        if candidate.exists():
            log_file = candidate
            break

    import asyncio

    last_pos = 0
    ping_interval = 30
    last_ping = time.time()

    try:
        while True:
            # Check for new log content
            if log_file and log_file.exists():
                current_size = log_file.stat().st_size
                if current_size > last_pos:
                    with open(log_file, "r", encoding="utf-8") as f:
                        f.seek(last_pos)
                        new_lines = f.read()
                        last_pos = f.tell()
                    if new_lines.strip():
                        await websocket.send_text(new_lines)

            # Send ping every 30s
            if time.time() - last_ping > ping_interval:
                await websocket.send_text("")  # keepalive
                last_ping = time.time()

            # Check if task is done
            current_task = daemon.store.get_task(task_id)
            if current_task and current_task["status"] in ("succeeded", "failed", "cancelled"):
                # Send final content then close
                if log_file and log_file.exists():
                    current_size = log_file.stat().st_size
                    if current_size > last_pos:
                        with open(log_file, "r", encoding="utf-8") as f:
                            f.seek(last_pos)
                            await websocket.send_text(f.read())
                await websocket.send_text(f"\n[TASK {current_task['status'].upper()}]")
                break

            await asyncio.sleep(1)

    except WebSocketDisconnect:
        pass
    except Exception:
        pass


# --- Cookie management endpoints ---

# Track active cookie refresh sessions with rich state
# Format: {platform: {"is_refreshing": bool, "started_at": float|None, "result": str|None}}
_cookie_refresh_state: dict[str, dict] = {}


def _get_refresh_state(platform: str) -> dict:
    """Get current refresh state for a platform."""
    return _cookie_refresh_state.get(platform, {
        "is_refreshing": False,
        "started_at": None,
        "result": None,
    })


def _update_refresh_state(platform: str, result: str | None = None):
    """Update refresh state when a refresh flow completes/fails.

    Args:
        platform: Platform name
        result: One of "completed", "timeout", "cancelled", "error", or None (still running)
    """
    if platform in _cookie_refresh_state:
        _cookie_refresh_state[platform]["is_refreshing"] = False
        _cookie_refresh_state[platform]["result"] = result
    else:
        _cookie_refresh_state[platform] = {
            "is_refreshing": False,
            "started_at": None,
            "result": result,
        }


def _start_refresh_state(platform: str):
    """Mark a platform as currently refreshing."""
    _cookie_refresh_state[platform] = {
        "is_refreshing": True,
        "started_at": time.time(),
        "result": None,
    }


@router.get("/api/cookies-refresh-status")
def get_all_cookie_refresh_status():
    """Get cookie refresh status for all platforms."""
    all_states = {}
    for platform in ["bilibili", "douyin", "kuaishou", "weibo", "qimai"]:
        state = _get_refresh_state(platform)
        all_states[platform] = {
            "is_refreshing": state["is_refreshing"],
            "started_at": state["started_at"],
            "result": state["result"],
        }
    return all_states


@router.get("/api/cookies/{platform}/refresh-status")
def get_cookie_refresh_status(platform: str):
    """Get current cookie refresh status for a platform."""
    state = _get_refresh_state(platform)
    return {
        "platform": platform,
        "is_refreshing": state["is_refreshing"],
        "started_at": state["started_at"],
        "result": state["result"],
    }


@router.post("/api/cookies/{platform}/refresh")
def refresh_cookie(platform: str, request: Request):
    """Trigger BBA browser login flow to refresh cookie.

    R7 P5 统一改造：所有登录/刷新走 bba_login_session，
    共享 patchright + persistent user_data_dir + origin metadata。
    """
    state = _get_refresh_state(platform)
    if state["is_refreshing"]:
        raise HTTPException(409, detail="该平台正在进行登录流程")

    no_cookie_platforms = ["steam"]
    if platform in no_cookie_platforms:
        raise HTTPException(400, detail="该平台无需登录")

    registry = get_registry()
    if platform not in registry:
        raise HTTPException(404, detail=f"Platform {platform} not found")

    _start_refresh_state(platform)

    # 从 platform service 读取 bba_skip_stealth 配置
    from crawlhub.core.registry import create_platform_service
    _svc = create_platform_service(platform)
    _skip_stealth = getattr(_svc, "bba_skip_stealth", False) if _svc else False

    import threading

    def _do_refresh():
        try:
            from crawlhub.core.browser.playwright_runtime import bba_login_session
            result = bba_login_session(platform, skip_stealth=_skip_stealth)
            _update_refresh_state(platform, result=result)
        except Exception as exc:
            import logging as _log
            _log.getLogger(__name__).exception(
                "[refresh_cookie] %s bba_login_session failed", platform,
            )
            if _get_refresh_state(platform)["is_refreshing"]:
                _update_refresh_state(platform, result="error")

    threading.Thread(target=_do_refresh, daemon=True).start()

    return {"status": "started", "message": "请在弹出的浏览器窗口中完成登录"}


@router.post("/api/cookies/qimai/login")
def qimai_login(body: QimaiLoginRequest):
    """Login to Qimai with username/password (headless, API-based)."""
    import json as _json

    username = body.username.strip()
    password = body.password.strip()

    if not username or not password:
        return JSONResponse(
            content={"ok": False, "error": "用户名和密码不能为空"},
            status_code=200,
        )

    try:
        from crawlhub.crawlers.qimai.crawler.client import QimaiClient

        client = QimaiClient()
        result = client.login(username, password)

        # Save session cookies to crawlhub cookie store via CookieStore
        from crawlhub.core.cookies import get_cookie_store

        cookie_store = get_cookie_store()

        # Extract cookies from session
        cookies_list = []
        for cookie in client._session.cookies:
            cookies_list.append({
                "name": cookie.name,
                "value": cookie.value,
                "domain": cookie.domain or ".qimai.cn",
            })

        cookie_data = {
            "username": username,
            "cookies": cookies_list,
            "cookie_string": "; ".join(f"{c['name']}={c['value']}" for c in cookies_list),
            "logged_in": True,
            "login_time": time.time(),
        }

        # Include userinfo if available
        userinfo = client.userinfo or {}
        cookie_data["userinfo"] = userinfo

        # Save via CookieStore (will extract username as label for dedup)
        saved = cookie_store.save_cookie("qimai", cookie_data)
        # Reset probe status to green after successful login
        _reset_cookie_probe_after_save("qimai", saved.label)

        is_vip = userinfo.get("is_vip", False) if userinfo else False

        return {"ok": True, "username": username, "is_vip": is_vip}

    except Exception as e:
        logger.error(f"[ERR] Qimai login failed: {e}")
        return JSONResponse(
            content={"ok": False, "error": str(e)},
            status_code=200,
        )




def _reset_cookie_probe_after_save(platform: str, cookie_label: str) -> None:
    """Reset cookie probe status to 'valid' after a successful cookie save/refresh.

    This ensures the platform management UI shows green status immediately
    after a cookie update, without requiring a separate probe action.
    """
    try:
        from crawlhub.core.config import get_data_root
        from crawlhub.core.sqlite_store import SqliteStateStore
        db_path = get_data_root() / "crawlhub.db"
        store = SqliteStateStore(db_path)
        store.reset_probe_status(platform, cookie_label)
        logger.info(f"[COOKIE] Reset probe status to valid for {platform}/{cookie_label}")
    except Exception as e:
        logger.warning(f"[COOKIE] Failed to reset probe status: {e}")



# --- Product preview endpoints ---

@router.get("/api/tasks/{task_id}/files")
def list_task_files(task_id: str, request: Request):
    """List files in task output directory."""
    daemon = _get_daemon(request)
    task = daemon.store.get_task(task_id)
    if task is None:
        raise HTTPException(404, detail=f"Task {task_id} not found")

    output_dir = task.get("output_dir", "")
    if not output_dir or not Path(output_dir).exists():
        return []

    # Security check
    output_root = str(get_data_root() / "output")
    resolved = str(Path(output_dir).resolve())
    if not resolved.startswith(output_root):
        raise HTTPException(400, detail="Invalid path")

    files = []
    output_path = Path(output_dir)
    for f in sorted(output_path.rglob("*")):
        if f.is_file():
            rel_path = f.relative_to(output_path)
            row_count = None

            # Estimate row count for JSONL files
            if f.suffix == ".jsonl":
                try:
                    with open(f, "r", encoding="utf-8") as fh:
                        row_count = sum(1 for line in fh if line.strip())
                except Exception:
                    pass

            files.append({
                "name": str(rel_path),
                "size": f.stat().st_size,
                "modified": f.stat().st_mtime,
                "rows": row_count,
            })

    return files


@router.get("/api/tasks/{task_id}/files/{filename:path}")
def get_task_file(
    task_id: str,
    filename: str,
    request: Request,
    download: int = Query(default=0, description="If 1, force attachment download regardless of content type"),
):
    """Get a specific file from task output directory.

    Default behavior preserves preview semantics (text/json -> inline JSON, image -> inline stream).
    Pass `?download=1` to force a streaming download with Content-Disposition: attachment,
    which the browser/UA will surface as a save dialog.
    """
    daemon = _get_daemon(request)
    task = daemon.store.get_task(task_id)
    if task is None:
        raise HTTPException(404, detail=f"Task {task_id} not found")

    output_dir = task.get("output_dir", "")
    if not output_dir:
        raise HTTPException(404, detail="Task has no output directory")

    # Security: prevent path traversal
    file_path = Path(output_dir) / filename
    resolved = str(file_path.resolve())
    output_root = str(get_data_root() / "output")

    if ".." in filename or not resolved.startswith(output_root):
        raise HTTPException(400, detail="Invalid file path")

    if not file_path.exists():
        raise HTTPException(404, detail=f"File not found: {filename}")

    # Determine content type
    import mimetypes
    content_type, _ = mimetypes.guess_type(str(file_path))
    if content_type is None:
        content_type = "application/octet-stream"

    # Forced download: stream as attachment regardless of type. Used by the
    # frontend "Save As" button so jsonl/json/text files don't get hijacked
    # by the JSON-content preview branch below.
    if download:
        return StreamingResponse(
            open(file_path, "rb"),
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": f'attachment; filename="{file_path.name}"',
                "Content-Length": str(file_path.stat().st_size),
            },
        )

    # For images, return directly
    if content_type.startswith("image/"):
        return StreamingResponse(
            open(file_path, "rb"),
            media_type=content_type,
            headers={"Content-Disposition": f"inline; filename={file_path.name}"}
        )

    # For text files, return content
    if content_type.startswith("text/") or file_path.suffix in (".json", ".jsonl"):
        content = file_path.read_text(encoding="utf-8")
        return JSONResponse({"content": content, "size": len(content)})

    # For other files, return as download
    return StreamingResponse(
        open(file_path, "rb"),
        media_type=content_type,
        headers={"Content-Disposition": f"attachment; filename={file_path.name}"}
    )


# ═══════════════════════════════════════════════════════════
#  Multi-Cookie Management API
# ═══════════════════════════════════════════════════════════

class CookieProbeRequest(BaseModel):
    task_type: str


class PlatformConfigUpdateRequest(BaseModel):
    expected_interval: float | None = None
    min_floor: float | None = None
    backoff_base_seconds: float | None = None
    max_backoff_exponent: int | None = None
    # truncate_percentile: None 或 >=1.0 关闭截断；(0,1) 区间内的值
    # 表示按指数分布的该分位数 clamp 长尾。默认 0.95 砍 top 5%。
    truncate_percentile: float | None = None


    @router.get("/api/cookies/{platform}")
    def list_platform_cookies(platform: str, request: Request, search: str | None = None):
        """List all cookies for a platform with status indicators.

        ?search=keyword filters cookies whose label/account_id/note contains
        *keyword* (case-insensitive substring match).
        """
        from crawlhub.core.cookies import get_cookie_store
        from crawlhub.core.registry import get_registry

        # Validate platform exists
        registry = get_registry()
        if platform not in registry:
            raise HTTPException(404, detail=f"Platform '{platform}' not found. Available platforms: {list(registry.keys())}")

        store = get_cookie_store()
        daemon = _get_daemon(request)
        cookies = store.list_cookies(platform, search=search)

        # Get last probe results for status lights
        last_probes = daemon.store.get_all_last_probes(platform)

        results = []
        for c in cookies:
            probe = last_probes.get(c.label)
            if probe:
                status_light = "green" if probe["result"] == "valid" else "red"
                last_probe_time = probe["probe_time"]
            else:
                status_light = "gray"
                last_probe_time = None

            results.append({
                "label": c.label,
                "account_id": c.account_id,
                "cookie_count": c.cookie_count,
                "last_modified": c.last_modified,
                "status_light": status_light,
                "last_probe_time": last_probe_time,
                "note": c.note,
            })

        return {"platform": platform, "cookies": results}


class CookieNoteRequest(BaseModel):
    """Request body for setting a cookie note."""
    note: str = ""


@router.get("/api/cookies/{platform}/{label}/note")
def get_cookie_note(platform: str, label: str, request: Request):
    """Return the note for a specific cookie as JSON {"note": "..."}."""
    from crawlhub.core.cookies import get_cookie_store
    store = get_cookie_store()
    try:
        note = store.get_note(platform, label)
        return {"note": note}
    except FileNotFoundError:
        raise HTTPException(404, detail=f"Cookie not found: {platform}/{label}")


@router.put("/api/cookies/{platform}/{label}/note")
def set_cookie_note(platform: str, label: str, body: CookieNoteRequest, request: Request):
    """Set (or clear) the note for a specific cookie."""
    from crawlhub.core.cookies import get_cookie_store
    store = get_cookie_store()
    try:
        store.set_note(platform, label, body.note)
        return {"ok": True, "platform": platform, "label": label}
    except FileNotFoundError:
        raise HTTPException(404, detail=f"Cookie not found: {platform}/{label}")


@router.post("/api/cookies/{platform}/probe")
def probe_platform_cookies(platform: str, body: CookieProbeRequest, request: Request):
    """Probe all cookies for a platform using a specific action.

    Tests each cookie against the specified task_type to determine validity.
    Records probe results to database.
    """
    from crawlhub.core.cookies import get_cookie_store

    store = get_cookie_store()
    daemon = _get_daemon(request)
    cookies = store.list_cookies(platform)

    if not cookies:
        return {"platform": platform, "task_type": body.task_type, "results": []}

    registry = get_registry()
    svc_cls = registry.get(platform)
    if svc_cls is None:
        raise HTTPException(404, detail=f"Platform {platform} not found")

    results = []

    for cookie_info in cookies:
        # Probe each cookie with timeout
        try:
            from crawlhub.core.registry import create_platform_service
            svc = create_platform_service(platform)
            # Use platform-specific probe logic
            probe_result = _probe_single_cookie(svc, platform, cookie_info.label, body.task_type, store)
        except Exception as e:
            probe_result = {"label": cookie_info.label, "status": "error", "message": str(e)}

        # Record probe result to DB
        daemon.store.record_probe(
            platform=platform,
            cookie_label=cookie_info.label,
            task_type=body.task_type,
            result=probe_result["status"],
            error_message=probe_result.get("message", ""),
        )
        results.append(probe_result)

    return {"platform": platform, "task_type": body.task_type, "results": results}


@router.post("/api/cookies/{platform}/add")
def add_platform_cookie(platform: str, request: Request):
    """Trigger BBA browser login to add a new cookie account.

    R7 P5 统一改造：同 refresh_cookie，走 bba_login_session。
    """
    state = _get_refresh_state(platform)
    if state["is_refreshing"]:
        raise HTTPException(409, detail="该平台正在进行登录流程")

    no_cookie_platforms = ["steam"]
    if platform in no_cookie_platforms:
        raise HTTPException(400, detail="该平台无需登录")

    _start_refresh_state(platform)

    # 从 platform service 读取 bba_skip_stealth 配置
    from crawlhub.core.registry import create_platform_service
    _svc = create_platform_service(platform)
    _skip_stealth = getattr(_svc, "bba_skip_stealth", False) if _svc else False

    import threading

    def _do_add():
        try:
            from crawlhub.core.browser.playwright_runtime import bba_login_session
            result = bba_login_session(platform, label=None, skip_stealth=_skip_stealth)
            _update_refresh_state(platform, result=result)
        except Exception as exc:
            import logging as _log
            _log.getLogger(__name__).exception(
                "[add_platform_cookie] %s bba_login_session failed", platform,
            )
            if _get_refresh_state(platform)["is_refreshing"]:
                _update_refresh_state(platform, result="error")

    threading.Thread(target=_do_add, daemon=True).start()

    return {"status": "started", "message": "请在弹出的浏览器窗口中登录新账号"}


@router.post("/api/cookies/{platform}/refresh/{label}")
def refresh_specific_cookie(platform: str, label: str, request: Request):
    """Trigger BBA browser login to refresh a specific cookie.

    R7 P5 统一改造：同 refresh_cookie，走 bba_login_session(label=label)。
    """
    state = _get_refresh_state(platform)
    if state["is_refreshing"]:
        raise HTTPException(409, detail="该平台正在进行登录流程")

    no_cookie_platforms = ["steam"]
    if platform in no_cookie_platforms:
        raise HTTPException(400, detail="该平台无需登录")

    _start_refresh_state(platform)

    # 从 platform service 读取 bba_skip_stealth 配置
    from crawlhub.core.registry import create_platform_service
    _svc = create_platform_service(platform)
    _skip_stealth = getattr(_svc, "bba_skip_stealth", False) if _svc else False

    import threading

    def _do_refresh():
        try:
            from crawlhub.core.browser.playwright_runtime import bba_login_session
            result = bba_login_session(platform, label=label, skip_stealth=_skip_stealth)
            _update_refresh_state(platform, result=result)
        except Exception as exc:
            import logging as _log
            _log.getLogger(__name__).exception(
                "[refresh_specific_cookie] %s/%s bba_login_session failed",
                platform, label,
            )
            if _get_refresh_state(platform)["is_refreshing"]:
                _update_refresh_state(platform, result="error")

    threading.Thread(target=_do_refresh, daemon=True).start()

    return {"status": "started", "message": f"正在更新 {label} 的 Cookie，请在浏览器中完成登录"}


@router.get("/api/cookies/{platform}/{label}/probes")
def get_cookie_probe_history(platform: str, label: str, request: Request):
    """Get probe history for a specific cookie."""
    daemon = _get_daemon(request)
    history = daemon.store.get_probe_history(platform, label, limit=20)
    return {"platform": platform, "label": label, "probes": history}


@router.delete("/api/cookies/{platform}/{label}")
def delete_platform_cookie(platform: str, label: str, request: Request):
    """Delete a specific cookie file + its bound browser profile.

    R7 P6 起 cookie metadata.profile_dir 显式绑定一个 user_data_dir
    目录，cookie 删了 profile 不删 = 孤儿目录堆积 / 用户从前端再加同
    label 时 reuse 到旧 profile 数据串味。
    所以这里**先读 metadata.profile_dir，删 cookie 后再 rmtree 对应
    profile 目录**。失败不阻断（cookie 已删返回 ok），日志记录即可。
    """
    from crawlhub.core.cookies import get_cookie_store
    from crawlhub.core.config import get_data_root

    store = get_cookie_store()

    # ── 删 cookie 之前读出绑定的 profile_dir ──────────────────
    profile_to_purge: Path | None = None
    try:
        cp = store.get_cookie_path(platform, label)
        if cp.exists():
            raw = json.loads(cp.read_text(encoding="utf-8"))
            meta = raw.get("metadata") if isinstance(raw, dict) else None
            pdir = meta.get("profile_dir") if isinstance(meta, dict) else None
            if isinstance(pdir, str) and pdir.strip():
                cand = Path(pdir.strip())
                if not cand.is_absolute():
                    cand = get_data_root() / cand
                cand = cand.resolve()
                # 安全检查：只允许删 data_root/browser_profiles/<platform>/ 下的目录，
                # 防止恶意 metadata 删除任意位置。
                allowed_root = (get_data_root() / "browser_profiles" / platform).resolve()
                try:
                    cand.relative_to(allowed_root)
                    profile_to_purge = cand
                except ValueError:
                    logger.warning(
                        "[delete_cookie] %s/%s profile_dir %s outside allowed root %s, "
                        "skip purge",
                        platform, label, cand, allowed_root,
                    )
    except Exception as exc:
        logger.warning(
            "[delete_cookie] %s/%s metadata read failed: %s (skip profile purge)",
            platform, label, exc,
        )

    # ── 删 cookie 文件 ───────────────────────────────────────
    deleted = store.delete_cookie(platform, label)
    if not deleted:
        raise HTTPException(404, detail=f"Cookie {platform}/{label} not found")

    # ── 删 profile 目录 ──────────────────────────────────────
    purged = False
    if profile_to_purge is not None and profile_to_purge.is_dir():
        try:
            import shutil
            shutil.rmtree(profile_to_purge, ignore_errors=True)
            # rmtree(ignore_errors=True) 不报错但可能残留只读锁文件；
            # 第二轮兜底——遇到只读重设权限再删。
            if profile_to_purge.exists():
                import stat
                def _chmod_then_rm(func, path, _exc):
                    try:
                        os.chmod(path, stat.S_IWRITE)
                        func(path)
                    except Exception:
                        pass
                shutil.rmtree(profile_to_purge, onerror=_chmod_then_rm)
            purged = not profile_to_purge.exists()
            logger.info(
                "[delete_cookie] %s/%s profile purged: %s (success=%s)",
                platform, label, profile_to_purge, purged,
            )
        except Exception as exc:
            logger.warning(
                "[delete_cookie] %s/%s profile rmtree failed: %s",
                platform, label, exc,
            )

    return {
        "ok": True,
        "message": f"Cookie {label} deleted",
        "profile_purged": purged,
        "profile_dir": str(profile_to_purge) if profile_to_purge else None,
    }


class CookieAddRawRequest(BaseModel):
    raw_cookie: str
    label: str | None = None
    format: str = "auto"  # auto | netscape | raw_string | json


@router.post("/api/cookies/{platform}/add-raw")
def add_raw_cookie(platform: str, body: CookieAddRawRequest, request: Request):
    """Add a cookie by pasting raw cookie string.

    Supports formats:
    - raw_string: "key=value; key2=value2" (from browser DevTools)
    - netscape: Netscape/curl cookie format (tab-separated)
    - json: JSON array from EditThisCookie or Playwright storage_state
    - auto: auto-detect format
    """
    from crawlhub.core.cookies import get_cookie_store

    store = get_cookie_store()
    raw = body.raw_cookie.strip()

    if not raw:
        raise HTTPException(400, detail="raw_cookie cannot be empty")

    # Auto-detect format
    fmt = body.format
    if fmt == "auto":
        if raw.startswith("[") or raw.startswith("{"):
            fmt = "json"
        elif "\t" in raw and ("TRUE" in raw or "FALSE" in raw):
            fmt = "netscape"
        else:
            fmt = "raw_string"

    # Parse into storage_state-like format
    try:
        if fmt == "raw_string":
            # "key=value; key2=value2" format
            cookies = []
            for pair in raw.split(";"):
                pair = pair.strip()
                if "=" in pair:
                    name, value = pair.split("=", 1)
                    cookies.append({
                        "name": name.strip(),
                        "value": value.strip(),
                        "domain": f".{platform}.com",
                        "path": "/",
                    })
            if not cookies:
                raise ValueError("No valid key=value pairs found")
            data = {"cookies": cookies}

        elif fmt == "netscape":
            # Netscape format: domain\tTRUE/FALSE\tpath\tTRUE/FALSE\texpiry\tname\tvalue
            cookies = []
            for line in raw.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) >= 7:
                    cookies.append({
                        "name": parts[5],
                        "value": parts[6],
                        "domain": parts[0],
                        "path": parts[2],
                    })
            if not cookies:
                raise ValueError("No valid Netscape cookie entries found")
            data = {"cookies": cookies}

        elif fmt == "json":
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                # EditThisCookie format: [{"name": "k", "value": "v", ...}]
                data = {"cookies": parsed}
            elif isinstance(parsed, dict):
                if "cookies" in parsed:
                    # Already storage_state format
                    data = parsed
                else:
                    # Flat dict: {"key": "value", ...}
                    cookies = [{"name": k, "value": v, "domain": f".{platform}.com", "path": "/"}
                               for k, v in parsed.items()]
                    data = {"cookies": cookies}
            else:
                raise ValueError("JSON must be an array or object")
        else:
            raise HTTPException(400, detail=f"Unknown format: {fmt}")

    except (json.JSONDecodeError, ValueError) as e:
        raise HTTPException(400, detail=f"Failed to parse cookie: {e}")

    # Convert via cookie_converters if needed
    from crawlhub.core.cookie_converters import convert_storage_state
    converted = convert_storage_state(platform, data)

    # Save
    info = store.save_cookie(platform, converted, label=body.label)
    # Reset probe status to green (valid) after successful save
    daemon = _get_daemon(request)
    daemon.store.reset_probe_status(platform, info.label)
    return {
        "ok": True,
        "label": info.label,
        "account_id": info.account_id,
        "cookie_count": info.cookie_count,
        "format_detected": fmt,
    }


def _probe_single_cookie(
    svc, platform: str, label: str, task_type: str, cookie_store
) -> dict:
    """Probe a single cookie for validity by calling the platform's real API.

    R4 P13 (2026-05-25): unified probe path. Drives the platform's
    ``service.check_cookie()`` (which now goes through ``client.probe()``
    under the hood). The cookie label is selected by setting a thread-
    local override so the service's resolver picks up the requested
    cookie file regardless of throttle / dispatch state.

    Wraps the call in a thread with a hard 15s deadline so a hung HTTP
    socket cannot block the API worker indefinitely.

    Returns: ``{"label": str, "status": "valid"|"expired"|"missing"|"error", "message": str}``
    """
    import threading

    from crawlhub.core.cookie_override import (
        clear_thread_cookie_override,
        set_thread_cookie_override,
    )

    # Resolve the cookie file path so we can pin it via the override.
    try:
        cookie_path = cookie_store.get_cookie_path(platform, label)
    except Exception as e:
        return {"label": label, "status": "error", "message": f"Cookie path lookup failed: {e}"}

    if not cookie_path.exists():
        return {"label": label, "status": "missing", "message": "Cookie file not found"}

    result: dict = {"label": label, "status": "error", "message": "Probe timed out"}

    def _do_probe():
        nonlocal result
        set_thread_cookie_override(str(cookie_path))
        try:
            status = svc.check_cookie()
            # CookieStatus shape: status="valid"|"expired"|"missing", message=str
            result = {
                "label": label,
                "status": status.status,
                "message": getattr(status, "message", "") or "",
            }
        except Exception as e:
            result = {"label": label, "status": "error", "message": f"{type(e).__name__}: {e}"}
        finally:
            clear_thread_cookie_override()

    t = threading.Thread(target=_do_probe, daemon=True)
    t.start()
    t.join(timeout=15)
    return result


# ═══════════════════════════════════════════════════════════
#  Favorites API
# ═══════════════════════════════════════════════════════════

class FavoriteRequest(BaseModel):
    platform: str
    task_type: str
    logic_param: dict[str, Any] = {}
    name: str = ""
    source_task_id: str = ""


class FavoriteUpdateRequest(BaseModel):
    platform: str | None = None
    task_type: str | None = None
    logic_param: dict[str, Any] | None = None
    name: str | None = None


@router.post("/api/favorites")
def create_favorite(body: FavoriteRequest, request: Request):
    """Create a new favorite (task template)."""
    daemon = _get_daemon(request)
    fav = daemon.store.create_favorite({
        "platform": body.platform,
        "task_type": body.task_type,
        "logic_param": body.logic_param,
        "name": body.name or f"{body.platform}_{body.task_type}",
        "source_task_id": body.source_task_id,
    })
    return fav


@router.get("/api/favorites")
def list_favorites(request: Request, platform: str | None = None):
    """List all favorites, optionally filtered by platform."""
    daemon = _get_daemon(request)
    favorites = daemon.store.list_favorites(platform=platform)
    return {"favorites": favorites}


@router.get("/api/favorites/{favorite_id}")
def get_favorite(favorite_id: str, request: Request):
    """Get a single favorite by ID."""
    daemon = _get_daemon(request)
    fav = daemon.store.get_favorite(favorite_id)
    if fav is None:
        raise HTTPException(404, detail=f"Favorite {favorite_id} not found")
    return fav


@router.put("/api/favorites/{favorite_id}")
def update_favorite(favorite_id: str, body: FavoriteUpdateRequest, request: Request):
    """Update a favorite's fields."""
    daemon = _get_daemon(request)
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(400, detail="No fields to update")
    result = daemon.store.update_favorite(favorite_id, updates)
    if result is None:
        raise HTTPException(404, detail=f"Favorite {favorite_id} not found")
    return result


@router.delete("/api/favorites/{favorite_id}")
def delete_favorite(favorite_id: str, request: Request):
    """Delete a favorite."""
    daemon = _get_daemon(request)
    deleted = daemon.store.delete_favorite(favorite_id)
    if not deleted:
        raise HTTPException(404, detail=f"Favorite {favorite_id} not found")
    return {"ok": True}


# ── Platform Config & Throttle Management ────────────────────────────


@router.get("/api/platform-config")
def get_platform_config(request: Request):
    """Get all platform throttle configurations and cookie states.

    Returns per-platform: throttle config (expected_interval, min_floor,
    backoff_base, max_exponent) + cookie states (status, backoff info).
    """
    import time as _time
    from crawlhub.core.config import load_config
    from crawlhub.core.cookie_dispatcher import get_cookie_throttle

    config = load_config()
    throttle = get_cookie_throttle()
    registry = get_registry()

    platforms_data = {}
    for platform_name in registry:
        # Get throttle config for this platform (returns ThrottleConfig object)
        tc = config.get_throttle_config(platform_name)

        throttle_config = {
            "expected_interval": tc.expected_interval,
            "min_floor": tc.effective_min_floor,
            "backoff_base_seconds": tc.backoff_base_seconds,
            "max_backoff_exponent": tc.max_backoff_exponent,
            "truncate_percentile": tc.truncate_percentile,
            # 派生字段：让前端不用再算一次，直接展示"实际截断在 X 秒"
            "truncate_cap_seconds": tc.effective_truncate_cap,
        }

        # Get cookie states from throttle
        cookie_states = []
        states = throttle.get_platform_states(platform_name)
        now = _time.time()
        for state in states:
            cookie_states.append({
                "cookie_id": state.cookie_id,
                "label": state.label,
                "status": state.status.value,
                "backoff_until": state.backoff_until,
                "backoff_remaining_seconds": max(0, state.backoff_until - now) if state.backoff_until else 0,
                "last_request_time": state.last_request_at,
                "last_success_time": state.last_success_at,
                "consecutive_failures": state.backoff_count,
            })

        platforms_data[platform_name] = {
            "throttle_config": throttle_config,
            "cookie_states": cookie_states,
        }

    return {"platforms": platforms_data}


@router.put("/api/platform-config/{platform}")
def update_platform_config(platform: str, body: PlatformConfigUpdateRequest, request: Request):
    """Update throttle configuration for a platform (hot-reload).

    Body: {"expected_interval": float, "min_floor": float,
           "backoff_base_seconds": float, "max_backoff_exponent": int}
    """
    from crawlhub.core.config import load_config, save_config
    from crawlhub.core.cookie_dispatcher import get_cookie_throttle

    config = load_config()
    registry = get_registry()

    if platform not in registry:
        raise HTTPException(404, detail=f"Platform {platform} not found")

    # ────────────────────────────────────────────────────────────────
    #  exclude_unset 而非 exclude_none：用户显式传 truncate_percentile=null
    #  表示"关闭截断"，必须能传到 update_throttle_config。
    #  其他字段不允许 null，由 update_throttle_config 内部转换 float/int。
    # ────────────────────────────────────────────────────────────────
    body_dict = body.model_dump(exclude_unset=True)
    if not body_dict:
        raise HTTPException(400, detail="No valid fields to update")

    # truncate_percentile 范围校验：允许 None 或 (0, 1)；>= 1.0 视为关闭。
    if "truncate_percentile" in body_dict:
        tp = body_dict["truncate_percentile"]
        if tp is not None and not (0.0 < tp < 1.0):
            # >= 1.0 视为"关闭截断"语义，转成 None 存储；<= 0 一律拒绝
            if tp <= 0.0:
                raise HTTPException(400, detail="truncate_percentile must be in (0, 1) or null")
            body_dict["truncate_percentile"] = None

    updated_tc = config.update_throttle_config(platform, body_dict)

    # Hot-reload throttle
    throttle = get_cookie_throttle()
    throttle.reload_config(platform, updated_tc.to_dict())

    return {"ok": True, "platform": platform, "updated": body_dict}


@router.post("/api/cookies/probe-all")
def probe_all_platform_cookies(request: Request):
    """Probe all cookies across all platforms.

    R4 P13 (2026-05-25): goes through the unified probe path
    (``service.check_cookie()`` → ``client.probe()``). Updates throttle
    state for every cookie based on the verdict, mirrors the result to
    the DB, and broadcasts to dashboard clients.
    """
    from crawlhub.core.cookie_dispatcher import get_cookie_throttle
    from crawlhub.core.cookies import get_cookie_store
    from crawlhub.core.registry import create_platform_service

    registry = get_registry()
    daemon = _get_daemon(request)
    store = get_cookie_store()
    throttle = get_cookie_throttle()
    all_results = {}

    for platform_name in registry:
        platform_results: list[dict] = []
        try:
            cookies = store.list_cookies(platform_name)
            if not cookies:
                all_results[platform_name] = []
                continue
            svc = create_platform_service(platform_name)
            for cookie_info in cookies:
                r = _probe_single_cookie(
                    svc, platform_name, cookie_info.label, "probe", store,
                )
                cookie_id = f"{platform_name}:{cookie_info.label}"
                # Update throttle state based on verdict.
                if r["status"] == "valid":
                    throttle.mark_valid(cookie_id)
                elif r["status"] == "expired":
                    throttle.mark_expired(cookie_id)
                else:  # missing / error -> unknown (let task execution decide)
                    throttle.mark_unknown(cookie_id)
                # Persist to DB (probe history).
                daemon.store.record_probe(
                    platform=platform_name,
                    cookie_label=cookie_info.label,
                    task_type="probe",
                    result=r["status"],
                    error_message=r.get("message", ""),
                )
                platform_results.append({
                    "label": cookie_info.label,
                    "cookie_id": cookie_id,
                    "status": r["status"],
                    "message": r.get("message", ""),
                })
            all_results[platform_name] = platform_results
        except Exception as e:
            all_results[platform_name] = [{"error": str(e)}]

    # Broadcast via WebSocket
    try:
        daemon.broadcast_ws_message({
            "type": "probe_results",
            "data": all_results,
        })
    except Exception:
        pass

    return {"ok": True, "results": all_results}


# --- System endpoints ---

def _rss_mb() -> float:
    """Current process RSS in MB. Returns 0.0 if psutil unavailable."""
    try:
        import psutil
        return round(psutil.Process().memory_info().rss / (1024 * 1024), 1)
    except Exception:  # noqa: BLE001
        return 0.0


@router.post("/api/system/release-memory")
def release_memory(request: Request):
    """Best-effort in-process memory release.

    Does NOT touch running tasks or worker threads. Specifically:
      1. Run gc.collect() up to 3 rounds (stops early if a round frees nothing).
      2. Defensively evict _active_batches entries whose parent is in a
         terminal status in the DB (normal path already pops these in a
         finally; this only matters if a worker died ungracefully and left
         a zombie entry).
      3. Drop stale _contexts / _futures whose task is already terminal
         (same reasoning).
      4. Return before/after RSS so the UI can show "freed X MB".

    Running tasks are untouched \u2014 their ctx/future is kept because the
    corresponding DB row is still 'running'.
    """
    import gc

    daemon = _get_daemon(request)
    before = _rss_mb()

    # --- Step 1: gc.collect() up to 3 rounds ---
    gc_total = 0
    for _ in range(3):
        collected = gc.collect()
        gc_total += collected
        if collected == 0:
            break

    terminal_statuses = {
        TaskStatus.SUCCEEDED.value,
        TaskStatus.PARTIAL_SUCCEEDED.value,
        TaskStatus.FAILED.value,
        TaskStatus.CANCELLED.value,
        TaskStatus.INTERRUPTED.value,
    }

    # --- Step 2: prune _active_batches zombies ---
    batches_pruned = 0
    if daemon.batch_orchestrator is not None:
        active = daemon.batch_orchestrator._active_batches
        lock = daemon.batch_orchestrator._lock
        with lock:
            for parent_id in list(active.keys()):
                row = daemon.store.get_task(parent_id)
                if row is None or row.get("status") in terminal_statuses:
                    active.pop(parent_id, None)
                    batches_pruned += 1

    # --- Step 3: prune stale _contexts / _futures ---
    contexts_pruned = 0
    for task_id in list(daemon._contexts.keys()):
        row = daemon.store.get_task(task_id)
        if row is None or row.get("status") in terminal_statuses:
            daemon._contexts.pop(task_id, None)
            contexts_pruned += 1

    futures_pruned = 0
    for task_id in list(daemon._futures.keys()):
        fut = daemon._futures.get(task_id)
        if fut is None or fut.done():
            daemon._futures.pop(task_id, None)
            futures_pruned += 1

    # --- Step 4: one more gc pass after manual evictions ---
    gc_total += gc.collect()

    after = _rss_mb()
    freed = round(before - after, 1)

    return {
        "before_mb": before,
        "after_mb": after,
        "freed_mb": freed,
        "gc_collected": gc_total,
        "batches_pruned": batches_pruned,
        "contexts_pruned": contexts_pruned,
        "futures_pruned": futures_pruned,
    }


# --- Dashboard / throughput ---

# Window -> (lookback_seconds, bucket_seconds) used by /api/dashboard/record-rate.
# Bucket sized so each window yields ~60-96 points: enough density for a smooth
# curve, sparse enough to keep payload + chart render fast.
_RATE_WINDOWS = {
    "5min": (5 * 60, 5),         # 60 points @ 5s   -> "right now" granularity
    "1h":   (60 * 60, 60),       # 60 points @ 1min -> recent operating speed
    "24h":  (24 * 3600, 15 * 60),  # 96 points @ 15min -> daily trend
}


@router.get("/api/dashboard/record-rate")
def get_record_rate(
    request: Request,
    window: str = Query("5min", description="Time window: 5min | 1h | 24h"),
    task_id: str | None = Query(None, description="Optional: scope to a single task. Omit for global throughput."),
):
    """Return throughput (records/sec) over time for the dashboard chart.

    Speed is computed from cumulative `record_count` snapshots stored in
    `record_samples` (written by the daemon sampler every ~5s).

    Two scopes:
      * `task_id` omitted -> read the global flux series from
        `global_flux_samples` (a system-wide monotonic counter ticked by
        every TaskContext.write_record; see crawlhub/core/flux.py). Pure
        flux semantics: retry / archive / purge of any task NEVER reduce
        this series, so the dashboard speed chart is dip-free by design.
      * `task_id` provided -> read that specific task's per-task series
        from `record_samples`. For tasks shorter than ~10s this may only
        have the anchor+terminal points.

    Algorithm:
      1. Pull samples in [now - window, now], plus one anchor BEFORE the window
         (so the leftmost bucket has a valid prev reference).
      2. Compute delta_records between adjacent samples (cumulative -> delta).
         Negative deltas treated as 0 (counter never resets for global flux;
         for per-task this can defensively happen on 'fresh' retry).
      3. Attribute each delta to bucket = floor(curr.ts / bucket_seconds),
         but ONLY if curr.ts lies inside the visible window [now-lookback, now].
         This yields strict "now-lookback .. now" coverage on the x-axis.
      4. Sum deltas in the same bucket, divide by bucket_seconds -> rate.

    Returns dense series (zero-filled empty buckets) so the chart x-axis is
    contiguous regardless of activity gaps.
    """
    if window not in _RATE_WINDOWS:
        raise HTTPException(
            status_code=400,
            detail=f"window must be one of {list(_RATE_WINDOWS.keys())}, got {window!r}",
        )
    lookback, bucket_sec = _RATE_WINDOWS[window]

    daemon = _get_daemon(request)

    # If the caller scoped to a specific task, refuse to chart its samples
    # once it's in the recycle bin: the dashboard treats archived tasks as
    # invisible everywhere else, and showing rate here would conflict.
    if task_id:
        scoped = daemon.store.get_task(task_id)
        if scoped is None:
            raise HTTPException(404, detail=f"Task {task_id} not found")
        if scoped.get("archived_at") is not None:
            return {
                "window": window,
                "bucket_seconds": bucket_sec,
                "task_id": task_id,
                "points": [],
                "peak": 0.0,
                "total": 0,
                "archived": True,
            }

    now = time.time()
    window_start = now - lookback

    # Global flux vs per-task path. Different SOURCES, same downstream
    # bucketing. We normalize both to a {logical_key: [(ts, count)...]}
    # shape so the delta/bucket loop stays single-source.
    by_task: dict[str, list[tuple[float, int]]] = {}
    if task_id:
        samples = daemon.store.query_record_samples(window_start, task_id=task_id)
        for s in samples:
            by_task.setdefault(s["task_id"], []).append((s["ts"], s["record_count"]))
    else:
        # Global flux: monotonic system-wide counter, no task_id column.
        flux_samples = daemon.store.query_global_flux_samples(window_start)
        if flux_samples:
            by_task["__flux__"] = [(s["ts"], s["record_count"]) for s in flux_samples]

    # Bucket key = aligned-down ts. Range: [first_bucket, last_bucket].
    first_bucket = int(window_start // bucket_sec) * bucket_sec
    last_bucket = int(now // bucket_sec) * bucket_sec
    bucket_count = int((last_bucket - first_bucket) / bucket_sec) + 1
    deltas_per_bucket: list[int] = [0] * bucket_count

    def bucket_idx(ts: float) -> int:
        b = int(ts // bucket_sec) * bucket_sec
        return int((b - first_bucket) / bucket_sec)

    for tid, points in by_task.items():
        # Already sorted by ts within task. Compute adjacent deltas.
        for i in range(1, len(points)):
            prev_ts, prev_cnt = points[i - 1]
            curr_ts, curr_cnt = points[i]
            delta = curr_cnt - prev_cnt
            if delta <= 0:
                continue  # no growth or counter reset
            # Attribute the delta to the bucket containing curr_ts.
            # If curr_ts is outside the visible window, skip (this can happen
            # for the +1 anchor lookback hack).
            if curr_ts < window_start or curr_ts > now:
                continue
            idx = bucket_idx(curr_ts)
            if 0 <= idx < bucket_count:
                deltas_per_bucket[idx] += delta

    points_out = []
    peak = 0.0
    total = 0
    for i, d in enumerate(deltas_per_bucket):
        ts = first_bucket + i * bucket_sec
        rate = d / bucket_sec
        points_out.append({"ts": ts, "rate": round(rate, 3)})
        total += d
        if rate > peak:
            peak = rate

    avg = (total / lookback) if lookback > 0 else 0.0

    return {
        "window": window,
        "bucket_seconds": bucket_sec,
        "unit": "rows/s",
        "task_id": task_id,
        "points": points_out,
        "peak_rate": round(peak, 3),
        "avg_rate": round(avg, 3),
        "total_records": total,
    }


# ── Catch-all for undefined /api/* routes ────────────────────────────
# Must be the LAST route in this file. Captures any /api/* path not
# matched by the routes above, so the frontend catch-all (mounted at "/")
# does not swallow API 404s.
@router.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
def _api_catchall(path: str):
    from fastapi import Request, HTTPException
    raise HTTPException(404, detail=f"API endpoint '/api/{path}' not found.")
