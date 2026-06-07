"""The single canonical place that maps a `source ref` -> physical jsonl path.

A source ref looks like one of:
    {"run_id": "abc123"}      # produced by an upstream task
    {"path": "/abs/file.jsonl"}  # external file (escape hatch)

Future fields (`partition`, `attempt`, `template`) are reserved for the
three-layer scheduling model; they are explicitly rejected here so callers
fail fast instead of silently ignoring unsupported keys.

# TODO(三层模型): 此处是 ref -> 物理路径的唯一映射点；未来扩展
# partition/attempt/template 时只改这一处。
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from crawlhub.core.models import TaskStatus
from crawlhub.core.sql_errors import ArtifactNotFoundError, ArtifactNotReadyError

if TYPE_CHECKING:
    from crawlhub.core.sqlite_store import SqliteStateStore


# Tasks in these statuses have a usable data.jsonl. PARTIAL_FAILED is included
# because its successful children still produced records — the downstream may
# legitimately want to consume what's there. Pure FAILED / CANCELLED produce
# no usable artifact.
_READY_STATUSES = {
    TaskStatus.SUCCEEDED.value,
    TaskStatus.PARTIAL_SUCCEEDED.value,
    # B6: legacy 'archived' status removed. Archived tasks keep their
    # terminal status (succeeded / partial_succeeded), so they're already
    # covered by the entries above. The `archived_at` column tracks the
    # archive flag, not the status enum.
}

# Statuses where the task is still in flight; downstream should wait, not error.
_PENDING_STATUSES = {
    TaskStatus.QUEUED.value,
    TaskStatus.RUNNING.value,
    "waiting_dependency",
    TaskStatus.INTERRUPTED.value,
}

# Reserved keys for the future three-layer scheduling model.
_RESERVED_FUTURE_KEYS = ("partition", "attempt", "template", "instance")


def resolve_artifact(
    ref: dict[str, Any],
    store: "SqliteStateStore",
    *,
    alias: str | None = None,
) -> Path:
    """Resolve a source ref to an absolute jsonl path.

    Args:
        ref: source binding dict, must contain exactly one of {run_id, path}.
        store: SqliteStateStore used to look up task records by run_id.
        alias: optional source alias (binding key); attached to errors so the
            caller / UI can point at the offending source.

    Returns:
        Absolute Path to the jsonl file. Caller is free to assume the file
        exists at the moment this function returns; callers should not cache
        the path across long delays.

    Raises:
        ArtifactNotFoundError: run_id missing in store, or path doesn't exist.
        ArtifactNotReadyError: run_id exists but the task hasn't completed yet.
        ValueError: ref shape is invalid (both run_id+path, neither, or uses
            reserved future keys).
    """
    if not isinstance(ref, dict):
        raise ValueError(f"source ref must be a dict, got {type(ref).__name__}")

    # Reject reserved future keys early — better than silently ignoring them.
    for k in _RESERVED_FUTURE_KEYS:
        if k in ref:
            raise ValueError(
                f"source ref key '{k}' is reserved for the future scheduling "
                f"model and is not supported in this version. "
                f"Use 'run_id' (current task) or 'path' (external file) instead."
            )

    has_run_id = "run_id" in ref and ref["run_id"]
    has_path = "path" in ref and ref["path"]

    if has_run_id and has_path:
        raise ValueError("source ref must have exactly one of {run_id, path}, not both")
    if not has_run_id and not has_path:
        raise ValueError("source ref must have one of {run_id, path}")

    if has_path:
        return _resolve_path(str(ref["path"]), alias=alias)
    return _resolve_run_id(str(ref["run_id"]), store, alias=alias)


def _resolve_path(path_str: str, *, alias: str | None) -> Path:
    p = Path(path_str)
    if not p.is_absolute():
        # Reject relative paths — they would be ambiguous between cwd of the
        # daemon process and the user's intent.
        raise ValueError(f"source path must be absolute: {path_str}")
    if not p.exists():
        raise ArtifactNotFoundError(
            f"file not found: {path_str}",
            source=alias,
        )
    if not p.is_file():
        raise ArtifactNotFoundError(
            f"path is not a regular file: {path_str}",
            source=alias,
        )
    return p


def _resolve_run_id(
    run_id: str,
    store: "SqliteStateStore",
    *,
    alias: str | None,
) -> Path:
    task = store.get_task(run_id)
    if task is None:
        raise ArtifactNotFoundError(
            f"task not found: run_id={run_id}",
            source=alias,
        )

    status = task.get("status", "")
    if status not in _READY_STATUSES:
        # Distinguish "still in flight" from "terminally failed without data"
        # so the caller can decide between waiting and giving up.
        if status in _PENDING_STATUSES:
            reason = f"upstream task is still {status}"
        else:
            reason = f"upstream task ended in {status} with no usable artifact"
        raise ArtifactNotReadyError(
            f"{reason}: run_id={run_id}",
            source=alias,
        )

    output_dir = task.get("output_dir", "")
    if not output_dir:
        raise ArtifactNotFoundError(
            f"task has no output_dir: run_id={run_id}",
            source=alias,
        )

    data_path = Path(output_dir) / "data.jsonl"
    if not data_path.exists():
        raise ArtifactNotFoundError(
            f"data.jsonl missing under output_dir: {data_path}",
            source=alias,
        )
    return data_path


__all__ = ["resolve_artifact"]
