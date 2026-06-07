"""Task data model and status enum."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any

class TaskStatus(str, Enum):
    """All possible task lifecycle states.

    Phase 1 enum rename (no behavior change):
      pending          -> queued
      completed        -> succeeded
      partial_failed   -> partial_succeeded
    Newly promoted from string-literals:
      waiting_dependency (was hardcoded string)

    v4 (2026-05-12): PAUSED status removed. Users who want to "pause and
    continue later" now go through cancel -> full_retry.
    """

    QUEUED = "queued"
    WAITING_DEPENDENCY = "waiting_dependency"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL_SUCCEEDED = "partial_succeeded"  # Some children succeeded, some did not (batch parents)
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"
    # Note: there is no 'archived' status. Recycle-bin membership is tracked
    # via the `archived_at` column on the tasks table; the task keeps
    # whichever terminal status it reached (succeeded / failed / cancelled / etc.).
    # Old DB rows that still carry status='archived' are dropped on schema
    # migration (see SqliteStateStore.initialize).

    # Note: the legacy `retryable()` classmethod was removed on 2026-05-13.
    # Retry legality is now decided by the state-machine transition table
    # (`state_machine.can_transition(status, Action.FULL_RETRY)`) — single
    # source of truth, no more drift between this enum and _TRANSITIONS.

@dataclass
class Task:
    """Core task data model.

    Maps 1:1 to the SQLite tasks table and the REST API Task object.
    """

    task_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    platform: str = ""
    task_type: str = ""  # action name, e.g. "search_videos"
    status: TaskStatus = TaskStatus.QUEUED
    progress: float = 0.0  # 0.0 ~ 1.0
    # Phase 3 (param refactor): two distinct param dicts replace the legacy
    # single `input` field.
    #   logic_param   = the original request body the user submitted, kept
    #                   verbatim (e.g. POST /api/batch payload still has
    #                   `items_from`). Used for audit / "copy as request".
    #   snapshot_param = the executable snapshot at submit time. Defaults
    #                    explicitly filled, time templates rendered, and
    #                    `items_from` resolved into a frozen `items[]` so
    #                    retry can reproduce the exact run. Used by retry.
    logic_param: dict[str, Any] = field(default_factory=dict)
    snapshot_param: dict[str, Any] = field(default_factory=dict)
    output_dir: str = ""
    result_files: list[dict[str, Any]] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    error: str | None = None
    parent_task_id: str | None = None  # batch parent-child relationship
    note: str | None = None  # User-editable memo (<= 100 chars). Only set on
                             # parent/single tasks; children inherit by convention
                             # (UI never shows / edits note on children).

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dict for JSON / DB storage."""
        d = asdict(self)
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Task:
        """Deserialize from dict."""
        data = data.copy()
        if "status" in data and isinstance(data["status"], str):
            data["status"] = TaskStatus(data["status"])
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
