"""Abstract storage interfaces.

These ABCs define the contract for state persistence, queue management,
and blob storage. v1 uses SQLite + local filesystem; future versions
may swap in Redis / Postgres / MinIO without changing business logic.
"""

from abc import ABC, abstractmethod
from typing import Any


class StateStore(ABC):
    """Persistent store for task metadata, notification config, cookie health."""

    @abstractmethod
    def initialize(self) -> None:
        """Create tables / ensure schema is up to date."""

    @abstractmethod
    def get_task(self, task_id: str) -> dict | None:
        """Return task dict or None if not found."""

    @abstractmethod
    def list_tasks(
        self,
        platform: str | None = None,
        status: str | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
        limit: int = 100,
        offset: int = 0,
        parent_id: str | None = None,
        include_children: bool = False,
        search: str | None = None,
        only_archived: bool = False,
        include_archived: bool = False,
        origin_plan_id: str | None = None,
        sort_by: str = "created_at",
        sort_order: str = "DESC",
    ) -> list[dict]:
        """List tasks with optional filters.

        By default, only top-level tasks (parent_task_id IS NULL) and
        non-archived (archived_at IS NULL) tasks are returned.
        Use parent_id to query children of a specific batch task.
        Use include_children=True to include all tasks regardless of hierarchy.
        Use only_archived=True for the recycle-bin view.
        Use include_archived=True to bypass archived filtering entirely
        (lineage / admin queries).
        Use search for space-separated AND keyword matching against
        (task_id, note, logic_param, snapshot_param, platform, task_type)
        \u2014 substring, case-insensitive.
        Use origin_plan_id to surface only tasks fired by a given scheduling
        plan (matches both origin_type='plan' and 'plan_manual').
        Use sort_by / sort_order to control result ordering.
        Allowed sort_by values: created_at, started_at, finished_at,
        status, platform, task_type, progress, record_count.
        sort_order: ASC or DESC.
        """

    @abstractmethod
    def create_task(self, task: dict) -> dict:
        """Insert a new task record, return the created task."""

    @abstractmethod
    def update_task(self, task_id: str, updates: dict) -> dict | None:
        """Partial update of task fields. Return updated task or None."""

    @abstractmethod
    def delete_task(self, task_id: str) -> bool:
        """Deprecated alias of archive_task. Soft-delete by stamping archived_at."""

    @abstractmethod
    def archive_task(self, task_id: str) -> bool:
        """Move task (and its children, if a batch parent) to recycle bin."""

    @abstractmethod
    def restore_task(self, task_id: str) -> bool:
        """Restore task (and its children) from recycle bin."""

    @abstractmethod
    def purge_task(self, task_id: str) -> int:
        """Permanently delete task + children + transitions + samples. Returns row count.

        Caller is responsible for deleting output / log files on disk.
        """

    @abstractmethod
    def find_archived_older_than(self, cutoff_ts: float) -> list[dict]:
        """Top-level archived tasks whose archived_at < cutoff_ts. Used by auto-purge."""

    @abstractmethod
    def bulk_update_status(self, from_statuses: list[str], to_status: str, error: str | None = None) -> int:
        """Atomically move all tasks in from_statuses to to_status. Return count."""

    # --- Notification channels ---

    @abstractmethod
    def list_channels(self) -> list[dict]:
        """List all notification channels."""

    @abstractmethod
    def upsert_channel(self, channel: dict) -> dict:
        """Create or update a notification channel."""

    @abstractmethod
    def delete_channel(self, name: str) -> bool:
        """Delete a notification channel by name."""

    # --- Notification rules ---

    @abstractmethod
    def list_rules(self) -> list[dict]:
        """List all notification rules."""

    @abstractmethod
    def upsert_rule(self, rule: dict) -> dict:
        """Create or update a notification rule."""

    @abstractmethod
    def delete_rule(self, rule_id: str) -> bool:
        """Delete a notification rule."""

    # --- Cookie health ---

    @abstractmethod
    def record_cookie_failure(self, platform: str, timestamp: float) -> None:
        """Record a cookie auth failure event."""

    @abstractmethod
    def get_cookie_failure_count(self, platform: str, window_seconds: float = 86400) -> int:
        """Count failures within rolling window."""

    @abstractmethod
    def clear_cookie_failures(self, platform: str) -> None:
        """Reset failure counter for a platform."""

    # --- Scheduling plans ---
    # Hand-rolled cascade: deleting a plan must remove its triggers/steps
    # and SET NULL on tasks.origin_plan_id. Deleting a group is the API's
    # responsibility (it confirms plan-set is empty / archive-only and then
    # cascades through delete_plan).

    @abstractmethod
    def create_plan_group(self, group: dict) -> dict:
        """Insert a new plan group. Auto-stamps created_at/updated_at if absent."""

    @abstractmethod
    def get_plan_group(self, group_id: str) -> dict | None:
        """Return plan group dict or None if not found."""

    @abstractmethod
    def list_plan_groups(self) -> list[dict]:
        """List all plan groups, ordered by name."""

    @abstractmethod
    def update_plan_group(self, group_id: str, updates: dict) -> dict | None:
        """Partial update of plan group fields. Bumps updated_at."""

    @abstractmethod
    def delete_plan_group(self, group_id: str) -> bool:
        """Delete plan group. Caller is responsible for verifying it is empty."""

    @abstractmethod
    def create_plan(self, plan: dict) -> dict:
        """Insert a new plan. Stamps created_at/updated_at if absent."""

    @abstractmethod
    def get_plan(self, plan_id: str) -> dict | None:
        """Return plan dict or None if not found."""

    @abstractmethod
    def list_plans(self, group_id: str | None = None, enabled: int | None = None) -> list[dict]:
        """List plans, optionally filtered by group and/or enabled flag."""

    @abstractmethod
    def update_plan(self, plan_id: str, updates: dict) -> dict | None:
        """Partial update. Bumps updated_at. plan_id is immutable."""

    @abstractmethod
    def delete_plan(self, plan_id: str) -> bool:
        """Delete plan + its triggers/steps; SET NULL on tasks.origin_plan_id."""

    @abstractmethod
    def create_plan_trigger(self, trigger: dict) -> dict:
        """Insert a trigger. kind/expr validation happens at the API layer."""

    @abstractmethod
    def list_plan_triggers(self, plan_id: str) -> list[dict]:
        """List triggers for a plan, in insertion order."""

    @abstractmethod
    def update_plan_trigger(self, trigger_id: str, updates: dict) -> dict | None:
        """Partial update of a trigger."""

    @abstractmethod
    def delete_plan_trigger(self, trigger_id: str) -> bool:
        """Delete one trigger. Returns False if the trigger did not exist."""

    @abstractmethod
    def replace_plan_steps(self, plan_id: str, steps: list[dict]) -> list[dict]:
        """Replace all steps of a plan with the given list, reassigning step_index 0..N-1.

        Each item must include request_kind ('task' | 'batch'), platform,
        task_type, request_payload (full POST /api/task or POST /api/batch
        body); note is optional. step_id is auto-assigned. Atomic per call.
        """

    @abstractmethod
    def list_plan_steps(self, plan_id: str) -> list[dict]:
        """List plan steps in step_index order."""


class QueueBackend(ABC):
    """Lightweight task queue abstraction (v1: in-memory with SQLite persistence)."""

    @abstractmethod
    def enqueue(self, task_id: str, platform: str, priority: int = 0) -> None:
        """Add task to platform queue."""

    @abstractmethod
    def dequeue(self, platform: str) -> str | None:
        """Pop next task_id from platform queue, or None."""

    @abstractmethod
    def size(self, platform: str | None = None) -> int:
        """Queue depth (all platforms if None)."""

    @abstractmethod
    def remove(self, task_id: str) -> bool:
        """Remove a specific task from queue (e.g. on cancel)."""


class BlobStore(ABC):
    """Abstraction over file-system blob storage for task outputs."""

    @abstractmethod
    def get_output_dir(self, task_id: str, task_name: str) -> str:
        """Return (and create) the output directory path for a task."""

    @abstractmethod
    def write_record(self, output_dir: str, record: dict) -> None:
        """Append a single JSON record to data.jsonl in output_dir."""

    @abstractmethod
    def write_asset(self, output_dir: str, filename: str, data: bytes) -> str:
        """Write binary asset, return relative path."""

    @abstractmethod
    def read_records(
        self, output_dir: str, offset: int = 0, limit: int = 100, filter_expr: str | None = None
    ) -> list[dict]:
        """Read records from data.jsonl with pagination."""

    @abstractmethod
    def write_summary(self, output_dir: str, summary: dict) -> None:
        """Write summary.json to output_dir."""

    @abstractmethod
    def get_summary(self, output_dir: str) -> dict | None:
        """Read summary.json, return None if not exists."""

    @abstractmethod
    def list_files(self, output_dir: str) -> list[dict]:
        """List files in output_dir with size and row count info."""

    @abstractmethod
    def move_to_trash(self, output_dir: str, trash_dir: str) -> str:
        """Move output_dir to trash. Return new path."""

    @abstractmethod
    def purge(self, trash_path: str) -> None:
        """Permanently delete from trash."""

    @abstractmethod
    def disk_free_bytes(self) -> int:
        """Return free disk space in bytes on the output volume."""
