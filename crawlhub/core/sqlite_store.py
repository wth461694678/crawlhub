"""SQLite-based StateStore implementation (WAL mode).

Manages: tasks, notification_channels, notification_rules, cookie_health,
cookie_probes, favorites tables.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from crawlhub.core.interfaces import StateStore


class SqliteStateStore(StateStore):
    """SQLite WAL-mode implementation of StateStore."""

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self._local = threading.local()

    def _get_conn(self) -> sqlite3.Connection:
        """Get thread-local connection."""
        if not hasattr(self._local, "conn") or self._local.conn is None:
            conn = sqlite3.connect(self.db_path, timeout=30)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        return self._local.conn

    def initialize(self) -> None:
        """Create tables if not exist.

        If an old `tasks` table exists without the dependency columns
        (`depends_on_task_ids` / `waiting_reason`), drop it and recreate.
        Same applies to the Phase 1 status-enum rename: if the table still
        contains legacy status values (pending / completed / partial_failed),
        the table is dropped and recreated. Per project decision: no backward
        compat for the legacy task table — old task history is dropped on
upgrade. Other tables (cookies, notifications, favorites)
        are preserved.
        """
        conn = self._get_conn()
        # Detect legacy schema: tasks table exists but missing new columns,
        # or still using legacy status enum values.
        table_exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='tasks'"
        ).fetchone()
        if table_exists is not None:
            cols = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()}
            # Phase 2 state-machine columns: phase, cancellation_intent, archived_at.
            # Phase 3 (param refactor): logic_param + snapshot_param replace the
            # legacy `input` column. If any required col is missing, drop tasks.
            required_cols = {
                "depends_on_task_ids",
                "waiting_reason",
                "parent_task_id",
                "phase",
                "cancellation_intent",
                "archived_at",
                "note",
                # Scheduling-plans phase: tag tasks with the plan that
                # submitted them (NULL for ad-hoc UI / API submissions).
                "origin_type",
                "origin_plan_id",
                # Phase 3: dual-param fields. Presence of `input` (legacy)
                # without these triggers drop + recreate.
                "logic_param",
                "snapshot_param",
            }
            should_drop = not required_cols.issubset(cols)
            if not should_drop:
                # Phase 1 status-enum rename: pending->queued, completed->succeeded,
                # partial_failed->partial_succeeded. If any row still uses the old
                # values, drop the table.
                legacy_row = conn.execute(
                    "SELECT 1 FROM tasks WHERE status IN ('pending', 'completed', 'partial_failed', 'archived') LIMIT 1"
                ).fetchone()
                if legacy_row is not None:
                    should_drop = True
            if should_drop:
                conn.execute("DROP TABLE tasks")
                # transitions table is tightly coupled to tasks; drop it too
                # so we never leave orphan transition rows behind.
                conn.execute("DROP TABLE IF EXISTS task_status_transitions")
                conn.commit()
        # Phase 3: legacy plan_steps table uses task_kind / input_template /
        # items_from_template. New schema uses request_kind / request_payload.
        # If old columns are present, drop the table (no backward compat —
        # users must re-create their plan steps).
        plan_steps_exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='plan_steps'"
        ).fetchone()
        if plan_steps_exists is not None:
            ps_cols = {row["name"] for row in conn.execute("PRAGMA table_info(plan_steps)").fetchall()}
            if "task_kind" in ps_cols or "input_template" in ps_cols or "request_kind" not in ps_cols:
                conn.execute("DROP TABLE plan_steps")
                conn.commit()
        # Phase 3 (param refactor): favorites table — legacy column `input`
        # is renamed to `logic_param` (no backward compat; per project decision
        # any legacy favorites table missing `logic_param` is dropped and
        # rebuilt). Earlier `name` -> `note` migration is also subsumed: any
        # favorites table predating `logic_param` is wiped wholesale.
        fav_exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='favorites'"
        ).fetchone()
        if fav_exists is not None:
            fav_cols = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(favorites)").fetchall()
            }
            if "logic_param" not in fav_cols:
                conn.execute("DROP TABLE favorites")
                conn.commit()
        conn.executescript(_SCHEMA_SQL)
        conn.commit()

    # --- Tasks ---

    def get_task(self, task_id: str) -> dict | None:
        conn = self._get_conn()
        row = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        if row is None:
            return None
        task = _row_to_task_dict(row)
        # Check if this task has downstream dependents (someone depends on it)
        # depends_on_task_ids is stored as a JSON array; use LIKE on the raw
        # text since SQLite has no native JSON contains operator across versions.
        like_pattern = f'%"{task_id}"%'
        downstream_row = conn.execute(
            "SELECT 1 FROM tasks WHERE depends_on_task_ids LIKE ? LIMIT 1",
            (like_pattern,),
        ).fetchone()
        task["has_downstream"] = downstream_row is not None
        return task

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

        archived_at filter semantics:
          only_archived=True   -> archived_at IS NOT NULL (recycle-bin view)
          include_archived=True -> no filter (admin / lineage)
          default              -> archived_at IS NULL (normal user views)
        """
        conn = self._get_conn()
        conditions = []
        params: list[Any] = []

        # archived filter takes precedence: only_archived > include_archived > default
        if only_archived:
            conditions.append("archived_at IS NOT NULL")
        elif not include_archived:
            conditions.append("archived_at IS NULL")

        if parent_id is not None:
            # Query children of a specific parent
            conditions.append("parent_task_id = ?")
            params.append(parent_id)
        elif not include_children:
            # Default: only show top-level tasks (hide children)
            conditions.append("parent_task_id IS NULL")

        if platform:
            conditions.append("platform = ?")
            params.append(platform)
        if status:
            conditions.append("status = ?")
            params.append(status)
        if start_time:
            conditions.append("created_at >= ?")
            params.append(float(start_time))
        if end_time:
            conditions.append("created_at <= ?")
            params.append(float(end_time))

        # Scheduling-plans: surface only tasks fired by a given plan (covers
        # both origin_type='plan' and 'plan_manual', because origin_plan_id
        # is the same regardless of trigger source).
        if origin_plan_id is not None:
            conditions.append("origin_plan_id = ?")
            params.append(origin_plan_id)

        # Search: space-separated keywords, AND semantics across multiple fields.
        # Plain LIKE, case-insensitive via LOWER(). No FTS, no index — simplest thing
        # that can work at current scale. Empty/whitespace-only search is a no-op.
        # Each token must match at least one of the searchable fields
        # (task_id, note, logic_param, snapshot_param, platform, task_type)
        # — OR within a token, AND across tokens.
        if search and search.strip():
            tokens = [tok for tok in search.strip().split() if tok]
            for tok in tokens:
                pattern = f"%{tok.lower()}%"
                conditions.append(
                    "(LOWER(task_id) LIKE ? "
                    "OR LOWER(COALESCE(note, '')) LIKE ? "
                    "OR LOWER(COALESCE(logic_param, '')) LIKE ? "
                    "OR LOWER(COALESCE(snapshot_param, '')) LIKE ? "
                    "OR LOWER(COALESCE(platform, '')) LIKE ? "
                    "OR LOWER(COALESCE(task_type, '')) LIKE ?)"
                )
                params.extend([pattern, pattern, pattern, pattern, pattern, pattern])
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        _ALLOWED_SORT = {
            "created_at", "started_at", "finished_at",
            "status", "platform", "task_type",
            "progress", "record_count",
        }
        _sb = sort_by if sort_by in _ALLOWED_SORT else "created_at"
        _so = "ASC" if sort_order.upper() == "ASC" else "DESC"
        sql = f"SELECT * FROM tasks{where} ORDER BY {_sb} {_so} LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        rows = conn.execute(sql, params).fetchall()
        tasks = [_row_to_task_dict(r) for r in rows]

        # Batch-check which tasks have downstream dependents.
        # depends_on_task_ids is JSON; we scan all rows whose JSON list is
        # non-empty and decode them in Python. That's fine for the limited
        # row count we ever return here (capped by `limit`).
        if tasks:
            task_id_set = {t["task_id"] for t in tasks}
            dep_rows = conn.execute(
                "SELECT depends_on_task_ids FROM tasks "
                "WHERE depends_on_task_ids IS NOT NULL AND depends_on_task_ids != '[]'"
            ).fetchall()
            has_downstream_set: set[str] = set()
            for r in dep_rows:
                try:
                    deps = json.loads(r["depends_on_task_ids"] or "[]")
                except (json.JSONDecodeError, TypeError):
                    continue
                for d in deps:
                    if d in task_id_set:
                        has_downstream_set.add(d)
            for t in tasks:
                t["has_downstream"] = t["task_id"] in has_downstream_set

        # For batch_run parents, aggregate record_count / total_bytes from
        # live child tasks so the task list shows real-time progress.
        # Done here (read path) to avoid write-path overhead; the extra
        # query only fires when batch parents are actually in the result.
        batch_parent_ids = [t["task_id"] for t in tasks if t.get("task_type") == "batch_run"]
        if batch_parent_ids:
            placeholders = ",".join("?" * len(batch_parent_ids))
            agg_rows = conn.execute(
                f"SELECT parent_task_id, SUM(record_count) AS total_records, "
                f"SUM(total_bytes) AS total_bytes "
                f"FROM tasks "
                f"WHERE parent_task_id IN ({placeholders}) "
                f"GROUP BY parent_task_id",
                batch_parent_ids,
            ).fetchall()
            agg_map = {
                r["parent_task_id"]: (r["total_records"] or 0, r["total_bytes"] or 0)
                for r in agg_rows
            }
            for t in tasks:
                if t["task_id"] in agg_map:
                    t["record_count"] = agg_map[t["task_id"]][0]
                    t["total_bytes"] = agg_map[t["task_id"]][1]

        return tasks

    def create_task(self, task: dict) -> dict:
        conn = self._get_conn()
        # Normalize dependency list: always serialize as JSON array, even if empty.
        dep_ids = task.get("depends_on_task_ids") or []
        if not isinstance(dep_ids, list):
            raise TypeError(
                f"depends_on_task_ids must be list, got {type(dep_ids).__name__}"
            )
        dep_ids_json = json.dumps(list(dep_ids), ensure_ascii=False)
        # Note is optional, capped at 100 chars by API layer. Store as-is
        # (truncation at API edge, not here — silent truncation is evil).
        note = task.get("note")
        conn.execute(
            """INSERT INTO tasks (task_id, platform, task_type, status, progress,
               logic_param, snapshot_param, output_dir, result_files,
               created_at, started_at, finished_at, error,
               last_heartbeat, record_count, total_bytes, parent_task_id,
               depends_on_task_ids, waiting_reason, note,
               origin_type, origin_plan_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                task["task_id"],
                task["platform"],
                task["task_type"],
                task["status"],
                task.get("progress", 0.0),
                json.dumps(task.get("logic_param", {}), ensure_ascii=False),
                json.dumps(task.get("snapshot_param", {}), ensure_ascii=False),
                task.get("output_dir", ""),
                json.dumps(task.get("result_files", []), ensure_ascii=False),
                task.get("created_at", time.time()),
                task.get("started_at"),
                task.get("finished_at"),
                task.get("error"),
                task.get("last_heartbeat"),
                task.get("record_count", 0),
                task.get("total_bytes", 0),
                task.get("parent_task_id"),
                dep_ids_json,
                task.get("waiting_reason"),
                note,
                task.get("origin_type"),
                task.get("origin_plan_id"),
            ),
        )
        conn.commit()
        return task

    def update_task(self, task_id: str, updates: dict) -> dict | None:
        conn = self._get_conn()
        # Build SET clause dynamically
        set_parts = []
        params: list[Any] = []
        for key, value in updates.items():
            if key == "task_id":
                continue
            if key in ("logic_param", "snapshot_param", "result_files", "depends_on_task_ids"):
                value = json.dumps(value, ensure_ascii=False)
            set_parts.append(f"{key} = ?")
            params.append(value)

        if not set_parts:
            return self.get_task(task_id)

        params.append(task_id)
        sql = f"UPDATE tasks SET {', '.join(set_parts)} WHERE task_id = ?"
        conn.execute(sql, params)
        conn.commit()
        return self.get_task(task_id)

    def delete_task(self, task_id: str) -> bool:
        """Deprecated alias kept for the StateStore protocol.

        Equivalent to archive_task — soft-delete by stamping archived_at.
        New code should call archive_task / restore_task / purge_task
        directly so the intent is explicit at the call site.
        """
        return self.archive_task(task_id)

    # --- Recycle-bin ops (archive / restore / purge) ---

    def archive_task(self, task_id: str) -> bool:
        """Soft-delete a task into the recycle bin.

        Cascades to all children of a batch parent in the same transaction
        so the parent and its children always share a consistent
        archived/non-archived state.
        """
        conn = self._get_conn()
        now = time.time()
        # Single statement covers both the task itself and any child whose
        # parent_task_id == task_id. Atomic children of single_run tasks
        # have parent_task_id IS NULL, so the OR clause is a no-op for
        # those — only batch parents pick up extra rows here.
        conn.execute(
            "UPDATE tasks SET archived_at = ? "
            "WHERE (task_id = ? OR parent_task_id = ?) AND archived_at IS NULL",
            (now, task_id, task_id),
        )
        conn.commit()
        return True

    def restore_task(self, task_id: str) -> bool:
        """Restore a task (and its children, if it's a batch parent) from the recycle bin."""
        conn = self._get_conn()
        conn.execute(
            "UPDATE tasks SET archived_at = NULL "
            "WHERE (task_id = ? OR parent_task_id = ?) AND archived_at IS NOT NULL",
            (task_id, task_id),
        )
        conn.commit()
        return True

    def purge_task(self, task_id: str) -> int:
        """Permanently delete a task, its children, and all related rows.

        Removes from: tasks, task_status_transitions, record_samples.
        Output files / logs on disk are the caller's responsibility (see
        scheduler._archived_purge / API purge handler).

        Returns the number of task rows deleted (1 for single, 1+N for batch).
        """
        conn = self._get_conn()
        # Collect all task_ids to purge (the task itself + any children).
        rows = conn.execute(
            "SELECT task_id FROM tasks WHERE task_id = ? OR parent_task_id = ?",
            (task_id, task_id),
        ).fetchall()
        ids_to_purge = [r["task_id"] for r in rows]
        if not ids_to_purge:
            return 0
        placeholders = ",".join("?" * len(ids_to_purge))
        conn.execute(
            f"DELETE FROM record_samples WHERE task_id IN ({placeholders})",
            ids_to_purge,
        )
        conn.execute(
            f"DELETE FROM task_status_transitions WHERE task_id IN ({placeholders})",
            ids_to_purge,
        )
        cur = conn.execute(
            f"DELETE FROM tasks WHERE task_id IN ({placeholders})",
            ids_to_purge,
        )
        conn.commit()
        return cur.rowcount

    def find_archived_older_than(self, cutoff_ts: float) -> list[dict]:
        """Return top-level archived tasks whose archived_at is older than cutoff.

        Children are not returned — purge_task() cascades them automatically.
        Used by scheduler._archived_purge for periodic auto-purge.
        """
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM tasks "
            "WHERE archived_at IS NOT NULL AND archived_at < ? "
            "  AND parent_task_id IS NULL "
            "ORDER BY archived_at ASC",
            (float(cutoff_ts),),
        ).fetchall()
        return [_row_to_task_dict(r) for r in rows]

    def list_archived_top_level(self, limit: int = 1000) -> list[dict]:
        """Convenience: all top-level tasks currently in the recycle bin."""
        return self.list_tasks(only_archived=True, limit=limit)

    # --- Task dependency methods ---

    def find_waiting_downstream(self, upstream_task_id: str) -> list[dict]:
        """Find all tasks waiting on a specific upstream task.

        depends_on_task_ids is stored as a JSON array; we use a LIKE prefilter
        plus an exact membership check in Python so we don't depend on SQLite's
        json1 extension being available.
        """
        conn = self._get_conn()
        like_pattern = f'%"{upstream_task_id}"%'
        rows = conn.execute(
            "SELECT * FROM tasks WHERE status = 'waiting_dependency' "
            "AND depends_on_task_ids LIKE ?",
            (like_pattern,),
        ).fetchall()
        result: list[dict] = []
        for r in rows:
            t = _row_to_task_dict(r)
            if upstream_task_id in (t.get("depends_on_task_ids") or []):
                result.append(t)
        return result

    def get_lineage(self, task_id: str, limit: int = 50, offset: int = 0) -> dict:
        """Get upstream and downstream lineage for a task.

        Returns: {"upstream": [...], "downstream": [...]}
        - upstream: tasks this one depends on (may be multiple)
        - downstream: tasks that depend on this one (limited)
        """
        conn = self._get_conn()
        # Upstream: dereference each id in depends_on_task_ids list
        task = self.get_task(task_id)
        upstream: list[dict] = []
        for up_id in (task.get("depends_on_task_ids") or []) if task else []:
            up_task = self.get_task(up_id)
            if up_task:
                upstream.append(up_task)

        # Downstream: tasks whose depends_on_task_ids contains this task_id.
        # LIKE prefilter then exact membership check.
        like_pattern = f'%"{task_id}"%'
        rows = conn.execute(
            """SELECT * FROM tasks WHERE depends_on_task_ids LIKE ?
               ORDER BY created_at DESC LIMIT ? OFFSET ?""",
            (like_pattern, limit, offset),
        ).fetchall()
        downstream: list[dict] = []
        for r in rows:
            t = _row_to_task_dict(r)
            if task_id in (t.get("depends_on_task_ids") or []):
                downstream.append(t)

        return {"upstream": upstream, "downstream": downstream}

    def atomic_transition(
        self, task_id: str, from_status: str, to_status: str, updates: dict | None = None
    ) -> bool:
        """Atomically transition a task from one status to another.

        Returns True if the transition succeeded (exactly 1 row affected).
        This provides idempotent protection against duplicate triggers.
        """
        conn = self._get_conn()
        set_parts = ["status = ?"]
        params: list[Any] = [to_status]

        if updates:
            for key, value in updates.items():
                if key in ("task_id", "status"):
                    continue
                if key in ("logic_param", "snapshot_param", "result_files"):
                    value = json.dumps(value, ensure_ascii=False)
                set_parts.append(f"{key} = ?")
                params.append(value)

        params.extend([task_id, from_status])
        sql = f"UPDATE tasks SET {', '.join(set_parts)} WHERE task_id = ? AND status = ?"
        cursor = conn.execute(sql, params)
        conn.commit()
        return cursor.rowcount == 1

    # --- Status transitions (audit log) ---

    def insert_transition(
        self,
        task_id: str,
        from_status: str | None,
        to_status: str,
        action: str,
        actor: str,
        from_phase: str | None = None,
        to_phase: str | None = None,
        reason: str | None = None,
    ) -> int:
        """Append one row to task_status_transitions. Returns inserted id.

        This is the audit log for every state transition. Spec §5.2 requires
        every status change, aggregate recompute, intent flag change, and
        phase A->B switch to land here.
        """
        conn = self._get_conn()
        cursor = conn.execute(
            """INSERT INTO task_status_transitions
               (task_id, from_status, to_status, from_phase, to_phase,
                action, actor, reason, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                task_id,
                from_status,
                to_status,
                from_phase,
                to_phase,
                action,
                actor,
                reason,
                time.time(),
            ),
        )
        conn.commit()
        return cursor.lastrowid

    def fetch_latest_transition(self, task_id: str) -> dict | None:
        """Return the most recent transition row for a task, or None.

        Per spec §5.2, callers must NOT filter by action — the latest
        to_status is authoritative regardless of which action wrote it
        (aggregate_changed / set_cancellation_intent / expand_into_phase_b
        all carry the current aggregate status).
        """
        conn = self._get_conn()
        row = conn.execute(
            """SELECT * FROM task_status_transitions
               WHERE task_id = ?
               ORDER BY id DESC LIMIT 1""",
            (task_id,),
        ).fetchone()
        return dict(row) if row else None

    def list_transitions(self, task_id: str, limit: int = 100) -> list[dict]:
        """Return transitions for a task, newest first. Used by debug UI."""
        conn = self._get_conn()
        rows = conn.execute(
            """SELECT * FROM task_status_transitions
               WHERE task_id = ?
               ORDER BY id DESC LIMIT ?""",
            (task_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def update_task_phase(self, task_id: str, phase: str) -> None:
        """Update the lifecycle phase of a (parent) task.

        Phase values: 'pre_expansion' | 'post_expansion'. Atomic items-resolution
        (spec §1.2) flips this from pre_ to post_ inside a single transaction
        together with children INSERTs and the expand_into_phase_b transition row.
        """
        conn = self._get_conn()
        conn.execute(
            "UPDATE tasks SET phase = ? WHERE task_id = ?",
            (phase, task_id),
        )
        conn.commit()

    def update_cancellation_intent(self, task_id: str, value: bool) -> None:
        """Set or clear the cancellation_intent flag on a parent task.

        Per spec §1.4: set to True when the user clicks parent-cancel; cleared
        by resume / full_retry / failed_retry / continue / force_succeeded.
        Distinct from status — marks intent that survives child state changes.
        """
        conn = self._get_conn()
        conn.execute(
            "UPDATE tasks SET cancellation_intent = ? WHERE task_id = ?",
            (1 if value else 0, task_id),
        )
        conn.commit()

    def bulk_update_status(self, from_statuses: list[str], to_status: str, error: str | None = None) -> int:
        conn = self._get_conn()
        placeholders = ",".join("?" * len(from_statuses))
        if error:
            sql = f"UPDATE tasks SET status = ?, error = ? WHERE status IN ({placeholders})"
            params: list[Any] = [to_status, error] + from_statuses
        else:
            sql = f"UPDATE tasks SET status = ? WHERE status IN ({placeholders})"
            params = [to_status] + from_statuses
        cursor = conn.execute(sql, params)
        conn.commit()
        return cursor.rowcount

    # --- Notification channels ---

    def list_channels(self) -> list[dict]:
        conn = self._get_conn()
        rows = conn.execute("SELECT * FROM notification_channels").fetchall()
        return [dict(r) for r in rows]

    def upsert_channel(self, channel: dict) -> dict:
        conn = self._get_conn()
        conn.execute(
            """INSERT OR REPLACE INTO notification_channels (name, webhook_url, enabled, created_at)
               VALUES (?, ?, ?, ?)""",
            (
                channel["name"],
                channel["webhook_url"],
                channel.get("enabled", 1),
                channel.get("created_at", time.time()),
            ),
        )
        conn.commit()
        return channel

    def delete_channel(self, name: str) -> bool:
        conn = self._get_conn()
        cursor = conn.execute("DELETE FROM notification_channels WHERE name = ?", (name,))
        conn.commit()
        return cursor.rowcount > 0

    # --- Notification rules ---

    def list_rules(self) -> list[dict]:
        conn = self._get_conn()
        rows = conn.execute("SELECT * FROM notification_rules").fetchall()
        return [dict(r) for r in rows]

    def upsert_rule(self, rule: dict) -> dict:
        conn = self._get_conn()
        conn.execute(
            """INSERT OR REPLACE INTO notification_rules (rule_id, event_type, channel_name, enabled, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (
                rule["rule_id"],
                rule["event_type"],
                rule["channel_name"],
                rule.get("enabled", 1),
                rule.get("created_at", time.time()),
            ),
        )
        conn.commit()
        return rule

    def delete_rule(self, rule_id: str) -> bool:
        conn = self._get_conn()
        cursor = conn.execute("DELETE FROM notification_rules WHERE rule_id = ?", (rule_id,))
        conn.commit()
        return cursor.rowcount > 0

    # --- Cookie probes ---

    def record_probe(
        self,
        platform: str,
        cookie_label: str,
        task_type: str,
        result: str,
        error_message: str = "",
    ) -> dict:
        """Record a cookie probe result.

        Args:
            platform: Platform name
            cookie_label: Cookie file label
            task_type: The action/task_type used for probing
            result: 'valid', 'expired', or 'error'
            error_message: Error details if result != 'valid'
        """
        conn = self._get_conn()
        probe_time = time.time()
        conn.execute(
            """INSERT INTO cookie_probes (platform, cookie_label, probe_time, task_type, result, error_message)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (platform, cookie_label, probe_time, task_type, result, error_message),
        )
        conn.commit()
        return {
            "platform": platform,
            "cookie_label": cookie_label,
            "probe_time": probe_time,
            "task_type": task_type,
            "result": result,
            "error_message": error_message,
        }

    def get_probe_history(
        self, platform: str, cookie_label: str, limit: int = 20
    ) -> list[dict]:
        """Get probe history for a specific cookie."""
        conn = self._get_conn()
        rows = conn.execute(
            """SELECT * FROM cookie_probes
               WHERE platform = ? AND cookie_label = ?
               ORDER BY probe_time DESC LIMIT ?""",
            (platform, cookie_label, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def reset_probe_status(self, platform: str, cookie_label: str) -> None:
        """Reset probe status for a cookie by recording a 'valid' probe.

        Called when a cookie is updated/refreshed to clear the 'expired' state.
        Since the user just logged in successfully, the cookie is assumed valid.
        """
        self.record_probe(
            platform=platform,
            cookie_label=cookie_label,
            task_type="cookie_refresh",
            result="valid",
            error_message="",
        )

    def get_last_probe(
        self, platform: str, cookie_label: str
    ) -> dict | None:
        """Get the most recent probe result for a cookie."""
        conn = self._get_conn()
        row = conn.execute(
            """SELECT * FROM cookie_probes
               WHERE platform = ? AND cookie_label = ?
               ORDER BY probe_time DESC LIMIT 1""",
            (platform, cookie_label),
        ).fetchone()
        return dict(row) if row else None

    def get_all_last_probes(self, platform: str) -> dict[str, dict]:
        """Get last probe result for all cookies of a platform.

        Returns: {cookie_label: {probe_time, task_type, result, error_message}}
        """
        conn = self._get_conn()
        rows = conn.execute(
            """SELECT cp.* FROM cookie_probes cp
               INNER JOIN (
                   SELECT platform, cookie_label, MAX(probe_time) as max_time
                   FROM cookie_probes
                   WHERE platform = ?
                   GROUP BY platform, cookie_label
               ) latest ON cp.platform = latest.platform
                   AND cp.cookie_label = latest.cookie_label
                   AND cp.probe_time = latest.max_time""",
            (platform,),
        ).fetchall()
        return {row["cookie_label"]: dict(row) for row in rows}

    # --- Favorites ---

    def create_favorite(self, fav: dict) -> dict:
        """Create a new favorite (task template)."""
        conn = self._get_conn()
        favorite_id = fav.get("favorite_id") or str(uuid.uuid4())[:12]
        now = time.time()
        conn.execute(
            """INSERT INTO favorites (favorite_id, platform, task_type, logic_param, note, source_task_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                favorite_id,
                fav["platform"],
                fav["task_type"],
                json.dumps(fav.get("logic_param", {}), ensure_ascii=False),
                fav.get("note", ""),
                fav.get("source_task_id", ""),
                now,
            ),
        )
        conn.commit()
        return {"favorite_id": favorite_id, **fav, "created_at": now}

    def list_favorites(self, platform: str | None = None, search: str | None = None) -> list[dict]:
        """List all favorites, optionally filtered by platform and/or search keyword."""
        conn = self._get_conn()
        conditions: list[str] = []
        params: list[Any] = []
        if platform:
            conditions.append("platform = ?")
            params.append(platform)
        if search:
            like = f"%{search}%"
            conditions.append("(note LIKE ? OR platform LIKE ? OR task_type LIKE ?)")
            params.extend([like, like, like])
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        rows = conn.execute(
            f"SELECT * FROM favorites {where} ORDER BY created_at DESC",
            params,
        ).fetchall()
        return [_row_to_favorite_dict(r) for r in rows]

    def get_favorite(self, favorite_id: str) -> dict | None:
        """Get a single favorite by ID."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM favorites WHERE favorite_id = ?", (favorite_id,)
        ).fetchone()
        return _row_to_favorite_dict(row) if row else None

    def update_favorite(self, favorite_id: str, updates: dict) -> dict | None:
        """Update a favorite's fields."""
        conn = self._get_conn()
        set_parts = []
        params: list[Any] = []
        for key, value in updates.items():
            if key in ("favorite_id", "created_at"):
                continue
            if key == "logic_param":
                value = json.dumps(value, ensure_ascii=False)
            set_parts.append(f"{key} = ?")
            params.append(value)
        if not set_parts:
            return self.get_favorite(favorite_id)
        params.append(favorite_id)
        sql = f"UPDATE favorites SET {', '.join(set_parts)} WHERE favorite_id = ?"
        conn.execute(sql, params)
        conn.commit()
        return self.get_favorite(favorite_id)

    def delete_favorite(self, favorite_id: str) -> bool:
        """Delete a favorite."""
        conn = self._get_conn()
        cursor = conn.execute("DELETE FROM favorites WHERE favorite_id = ?", (favorite_id,))
        conn.commit()
        return cursor.rowcount > 0

    # --- Cookie health ---

    def record_cookie_failure(self, platform: str, timestamp: float) -> None:
        conn = self._get_conn()
        conn.execute(
            "INSERT INTO cookie_health (platform, failure_at) VALUES (?, ?)",
            (platform, timestamp),
        )
        conn.commit()

    def get_cookie_failure_count(self, platform: str, window_seconds: float = 86400) -> int:
        conn = self._get_conn()
        cutoff = time.time() - window_seconds
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM cookie_health WHERE platform = ? AND failure_at >= ?",
            (platform, cutoff),
        ).fetchone()
        return row["cnt"] if row else 0

    def clear_cookie_failures(self, platform: str) -> None:
        conn = self._get_conn()
        conn.execute("DELETE FROM cookie_health WHERE platform = ?", (platform,))
        conn.commit()

    # --- Scheduling plans ---
    # Hand-rolled cascade convention (matches the rest of this module):
    #   delete_plan          -> wipes its triggers + steps, NULLs origin_plan_id on tasks
    #   delete_plan_group    -> NOT cascaded here; API layer must verify the
    #                           group is empty (or user-confirmed-archive-only)
    #                           and call delete_plan() per child first.

    def create_plan_group(self, group: dict) -> dict:
        conn = self._get_conn()
        now = group.get("created_at") or time.time()
        conn.execute(
            "INSERT INTO plan_groups (group_id, name, note, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                group["group_id"],
                group["name"],
                group.get("note"),
                now,
                group.get("updated_at") or now,
            ),
        )
        conn.commit()
        return self.get_plan_group(group["group_id"])

    def get_plan_group(self, group_id: str) -> dict | None:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM plan_groups WHERE group_id = ?", (group_id,)
        ).fetchone()
        return dict(row) if row else None

    def list_plan_groups(self) -> list[dict]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM plan_groups ORDER BY name ASC"
        ).fetchall()
        return [dict(r) for r in rows]

    def update_plan_group(self, group_id: str, updates: dict) -> dict | None:
        conn = self._get_conn()
        # Only whitelisted columns can be updated; group_id is immutable.
        allowed = {"name", "note"}
        set_parts: list[str] = []
        params: list[Any] = []
        for k, v in updates.items():
            if k in allowed:
                set_parts.append(f"{k} = ?")
                params.append(v)
        if not set_parts:
            return self.get_plan_group(group_id)
        # Always bump updated_at on any successful mutation.
        set_parts.append("updated_at = ?")
        params.append(time.time())
        params.append(group_id)
        conn.execute(
            f"UPDATE plan_groups SET {', '.join(set_parts)} WHERE group_id = ?",
            params,
        )
        conn.commit()
        return self.get_plan_group(group_id)

    def delete_plan_group(self, group_id: str) -> bool:
        conn = self._get_conn()
        cur = conn.execute(
            "DELETE FROM plan_groups WHERE group_id = ?", (group_id,)
        )
        conn.commit()
        return cur.rowcount > 0

    def create_plan(self, plan: dict) -> dict:
        conn = self._get_conn()
        now = plan.get("created_at") or time.time()
        conn.execute(
            "INSERT INTO plans (plan_id, group_id, name, enabled, timezone, "
            "notify_on_fire_fail, note, last_fired_at, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                plan["plan_id"],
                plan["group_id"],
                plan["name"],
                int(plan.get("enabled", 0)),
                plan.get("timezone", "Asia/Shanghai"),
                int(plan.get("notify_on_fire_fail", 1)),
                plan.get("note"),
                plan.get("last_fired_at"),
                now,
                plan.get("updated_at") or now,
            ),
        )
        conn.commit()
        return self.get_plan(plan["plan_id"])

    def get_plan(self, plan_id: str) -> dict | None:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM plans WHERE plan_id = ?", (plan_id,)
        ).fetchone()
        return dict(row) if row else None

    def list_plans(self, group_id: str | None = None, enabled: int | None = None) -> list[dict]:
        conn = self._get_conn()
        conditions: list[str] = []
        params: list[Any] = []
        if group_id is not None:
            conditions.append("group_id = ?")
            params.append(group_id)
        if enabled is not None:
            conditions.append("enabled = ?")
            params.append(int(enabled))
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        rows = conn.execute(
            f"SELECT * FROM plans{where} ORDER BY name ASC", params
        ).fetchall()
        return [dict(r) for r in rows]

    def update_plan(self, plan_id: str, updates: dict) -> dict | None:
        conn = self._get_conn()
        # plan_id is immutable; everything else is mutable. group_id is in
        # the whitelist precisely because "move plan to other group" is a
        # first-class operation (requirements §4.2).
        allowed = {
            "group_id", "name", "enabled", "timezone",
            "notify_on_fire_fail", "note", "last_fired_at",
        }
        set_parts: list[str] = []
        params: list[Any] = []
        for k, v in updates.items():
            if k in allowed:
                if k in ("enabled", "notify_on_fire_fail"):
                    v = int(v)
                set_parts.append(f"{k} = ?")
                params.append(v)
        if not set_parts:
            return self.get_plan(plan_id)
        set_parts.append("updated_at = ?")
        params.append(time.time())
        params.append(plan_id)
        conn.execute(
            f"UPDATE plans SET {', '.join(set_parts)} WHERE plan_id = ?",
            params,
        )
        conn.commit()
        return self.get_plan(plan_id)

    def delete_plan(self, plan_id: str) -> bool:
        """Delete plan + cascade triggers/steps + SET NULL on origin_plan_id.

        Single transaction so a tasks-table failure rolls back everything.
        """
        conn = self._get_conn()
        try:
            cur = conn.execute("DELETE FROM plans WHERE plan_id = ?", (plan_id,))
            removed = cur.rowcount > 0
            if removed:
                conn.execute("DELETE FROM plan_triggers WHERE plan_id = ?", (plan_id,))
                conn.execute("DELETE FROM plan_steps WHERE plan_id = ?", (plan_id,))
                # Tasks survive the plan: keep the row, lose the back-reference.
                # origin_type stays as-is so the task's history ('this was fired
                # by *some* plan, now gone') remains inspectable.
                conn.execute(
                    "UPDATE tasks SET origin_plan_id = NULL WHERE origin_plan_id = ?",
                    (plan_id,),
                )
            conn.commit()
            return removed
        except Exception:
            conn.rollback()
            raise

    def create_plan_trigger(self, trigger: dict) -> dict:
        conn = self._get_conn()
        now = trigger.get("created_at") or time.time()
        conn.execute(
            "INSERT INTO plan_triggers (trigger_id, plan_id, kind, expr, enabled, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                trigger["trigger_id"],
                trigger["plan_id"],
                trigger["kind"],
                trigger["expr"],
                int(trigger.get("enabled", 1)),
                now,
            ),
        )
        conn.commit()
        return dict(conn.execute(
            "SELECT * FROM plan_triggers WHERE trigger_id = ?",
            (trigger["trigger_id"],),
        ).fetchone())

    def list_plan_triggers(self, plan_id: str) -> list[dict]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM plan_triggers WHERE plan_id = ? ORDER BY created_at ASC",
            (plan_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def update_plan_trigger(self, trigger_id: str, updates: dict) -> dict | None:
        conn = self._get_conn()
        allowed = {"kind", "expr", "enabled"}
        set_parts: list[str] = []
        params: list[Any] = []
        for k, v in updates.items():
            if k in allowed:
                if k == "enabled":
                    v = int(v)
                set_parts.append(f"{k} = ?")
                params.append(v)
        if not set_parts:
            row = conn.execute(
                "SELECT * FROM plan_triggers WHERE trigger_id = ?", (trigger_id,)
            ).fetchone()
            return dict(row) if row else None
        params.append(trigger_id)
        conn.execute(
            f"UPDATE plan_triggers SET {', '.join(set_parts)} WHERE trigger_id = ?",
            params,
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM plan_triggers WHERE trigger_id = ?", (trigger_id,)
        ).fetchone()
        return dict(row) if row else None

    def delete_plan_trigger(self, trigger_id: str) -> bool:
        conn = self._get_conn()
        cur = conn.execute(
            "DELETE FROM plan_triggers WHERE trigger_id = ?", (trigger_id,)
        )
        conn.commit()
        return cur.rowcount > 0

    def replace_plan_steps(self, plan_id: str, steps: list[dict]) -> list[dict]:
        """Atomic replace: wipe existing steps, insert new ones with step_index 0..N-1.

        Empty list is valid and clears all steps. Each step dict must contain
        request_kind ('task' | 'batch'), platform, task_type, request_payload
        (full POST /api/task or POST /api/batch body, as JSON-serializable
        dict OR pre-serialized JSON string). step_id is auto-generated.
        Note is optional.
        """
        conn = self._get_conn()
        try:
            conn.execute("DELETE FROM plan_steps WHERE plan_id = ?", (plan_id,))
            now = time.time()
            for idx, step in enumerate(steps):
                step_id = step.get("step_id") or ("ps_" + uuid.uuid4().hex[:10])
                # Normalize request_payload to a JSON string for TEXT column.
                # Callers (API layer via _step_to_db) normally pass dicts; be
                # defensive about already-serialized strings too.
                payload = step.get("request_payload", {})
                if not isinstance(payload, str):
                    payload = json.dumps(payload, ensure_ascii=False)
                elif not payload.strip():
                    payload = "{}"
                conn.execute(
                    "INSERT INTO plan_steps (step_id, plan_id, step_index, "
                    "request_kind, platform, task_type, request_payload, "
                    "note, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        step_id,
                        plan_id,
                        idx,
                        step["request_kind"],
                        step["platform"],
                        step["task_type"],
                        payload,
                        step.get("note"),
                        now,
                    ),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return self.list_plan_steps(plan_id)
    def list_plan_steps(self, plan_id: str) -> list[dict]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM plan_steps WHERE plan_id = ? ORDER BY step_index ASC",
            (plan_id,),
        ).fetchall()
        result: list[dict] = []
        for r in rows:
            d = dict(r)
            # Deserialize request_payload from JSON text -> dict for callers.
            raw = d.get("request_payload")
            if isinstance(raw, str):
                try:
                    d["request_payload"] = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    d["request_payload"] = {}
            result.append(d)
        return result

    # --- Throughput sampling ---

    def add_record_sample(self, task_id: str, ts: float, record_count: int) -> None:
        """Record a cumulative-count sample for throughput tracking.

        Idempotent under (task_id, ts) PK: a duplicate ts simply overwrites,
        which is fine because record_count is monotonic.
        """
        conn = self._get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO record_samples (task_id, ts, record_count) VALUES (?, ?, ?)",
            (task_id, float(ts), int(record_count)),
        )
        conn.commit()

    def query_record_samples(
        self, since_ts: float, task_id: str | None = None
    ) -> list[dict]:
        """Return samples newer than `since_ts`, ordered by (task_id, ts).

        The +1 lookback step (one sample BEFORE since_ts per task) is included
        so the rate computation has a valid `prev` reference for the leftmost
        bucket — otherwise the first delta of the window would be lost.
        """
        conn = self._get_conn()
        if task_id is not None:
            # Pull window samples + one anchor before window for delta calc.
            rows = conn.execute(
                """
                SELECT task_id, ts, record_count FROM record_samples
                WHERE task_id = ? AND ts >= (
                    SELECT COALESCE(MAX(ts), 0) FROM record_samples
                    WHERE task_id = ? AND ts < ?
                )
                ORDER BY ts ASC
                """,
                (task_id, task_id, float(since_ts)),
            ).fetchall()
        else:
            # Global: per-task anchor sample is hard to get via single query
            # without window functions on older SQLite. Cheap alternative:
            # pull all samples in [since_ts - 600, now]; the extra 10min margin
            # ensures every active task has at least one anchor before since_ts.
            rows = conn.execute(
                """
                SELECT task_id, ts, record_count FROM record_samples
                WHERE ts >= ?
                ORDER BY task_id ASC, ts ASC
                """,
                (float(since_ts) - 600.0,),
            ).fetchall()
        return [dict(r) for r in rows]

    def cleanup_old_samples(self, before_ts: float) -> int:
        """Delete samples older than `before_ts`. Returns deleted row count."""
        conn = self._get_conn()
        cur = conn.execute(
            "DELETE FROM record_samples WHERE ts < ?", (float(before_ts),)
        )
        conn.commit()
        return cur.rowcount

    # --- Global flux (system-wide throughput, decoupled from tasks) ---
    #
    # See schema comment near `global_flux_counter` for the design rationale.
    # In short: per-task `record_count` is a "progress" gauge that resets on
    # retry; global flux is a monotonic "system has downloaded N records"
    # counter that NEVER decreases (retry / purge / archive don't affect it).

    def get_global_flux_counter(self) -> dict:
        """Return the persisted global flux counter row.

        Always returns a dict with `record_count` and `updated_at` — the
        schema ships an `INSERT OR IGNORE` seed row so this never returns
        None, but we still defensive-default in case of a corrupted DB.
        """
        conn = self._get_conn()
        row = conn.execute(
            "SELECT record_count, updated_at FROM global_flux_counter WHERE id = 1"
        ).fetchone()
        if row is None:
            return {"record_count": 0, "updated_at": 0.0}
        return {
            "record_count": int(row["record_count"] or 0),
            "updated_at": float(row["updated_at"] or 0.0),
        }

    def update_global_flux_counter(self, record_count: int, updated_at: float) -> None:
        """Persist the in-memory flux total to the single-row counter table.

        Called by the sampler every tick so we recover the lifetime total on
        daemon restart with at most ~5s of loss.
        """
        conn = self._get_conn()
        conn.execute(
            "UPDATE global_flux_counter SET record_count = ?, updated_at = ? WHERE id = 1",
            (int(record_count), float(updated_at)),
        )
        conn.commit()

    def add_global_flux_sample(self, ts: float, record_count: int) -> None:
        """Append a global flux sample (one row per sampler tick).

        Idempotent under the `ts` PK — duplicate ts overwrites, fine because
        the value is monotonic.
        """
        conn = self._get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO global_flux_samples (ts, record_count) VALUES (?, ?)",
            (float(ts), int(record_count)),
        )
        conn.commit()

    def query_global_flux_samples(self, since_ts: float) -> list[dict]:
        """Return global flux samples newer than `since_ts`.

        Like `query_record_samples` we include one anchor sample BEFORE
        `since_ts` so the leftmost bucket has a valid prev reference. This
        is achieved by widening the lookback by 600s (same trick).
        """
        conn = self._get_conn()
        rows = conn.execute(
            """
            SELECT ts, record_count FROM global_flux_samples
            WHERE ts >= (
                SELECT COALESCE(MAX(ts), 0) FROM global_flux_samples
                WHERE ts < ?
            )
            ORDER BY ts ASC
            """,
            (float(since_ts),),
        ).fetchall()
        return [dict(r) for r in rows]

    def cleanup_old_global_flux_samples(self, before_ts: float) -> int:
        """Delete global flux samples older than `before_ts`. Returns count."""
        conn = self._get_conn()
        cur = conn.execute(
            "DELETE FROM global_flux_samples WHERE ts < ?", (float(before_ts),)
        )
        conn.commit()
        return cur.rowcount

    # --- Utility ---

    def close(self) -> None:
        """Close thread-local connection."""
        if hasattr(self._local, "conn") and self._local.conn:
            self._local.conn.close()
            self._local.conn = None

    def vacuum(self) -> None:
        """Run VACUUM on the database."""
        conn = self._get_conn()
        conn.execute("VACUUM")

    def count_by_status(self, status: str) -> int:
        """Count tasks with given status. Excludes archived (recycle-bin) rows."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM tasks "
            "WHERE status = ? AND archived_at IS NULL",
            (status,),
        ).fetchone()
        return row["cnt"] if row else 0

    def count_top_tasks_by_status(self, status: str) -> int:
        """Count user-facing tasks (single_run + batch_run) with given status.

        IMPORTANT — task_type semantics in this codebase:
          The `task_type` column does NOT use a fixed enum like
          {single_run, batch_run, child_run}. Only 'batch_run' is a sentinel
          value (set explicitly in batch.py). Every other task — single user
          submission OR batch child — has its `task_type` set to the ACTION
          NAME (e.g. 'get_game_info', 'search_games'). The
          single-vs-child distinction lives in `parent_task_id`:
            * parent_task_id IS NULL  -> top-level (single OR batch parent)
            * parent_task_id NOT NULL -> child of a batch

        Internal housekeeping types that are top-level but should NOT
        appear on the user dashboard: 'probe' and 'cookie_refresh'.

        Therefore, "single_run + batch_run" (= what the user calls "user-
        facing top-level tasks") means:
            parent_task_id IS NULL AND task_type NOT IN housekeeping.

        A batch_run with 200 children counts as 1 (the parent) here.
        """
        conn = self._get_conn()
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM tasks "
            "WHERE status = ? "
            "AND parent_task_id IS NULL "
            "AND archived_at IS NULL "
            "AND task_type NOT IN ('probe', 'cookie_refresh')",
            (status,),
        ).fetchone()
        return row["cnt"] if row else 0

    def sum_atomic_record_count(self) -> int:
        """[DEPRECATED 2026-06-05] Sum record_count over atomic tasks.

        Originally used to derive the dashboard "global throughput" series,
        but that derivation had a phantom-dip bug (retry resets task
        record_count -> aggregate goes backwards). Replaced by the global
        flux counter — see `crawlhub/core/flux.py` and
        `global_flux_samples` table. Kept for now because external test
        scripts still call it; do NOT use in new code paths.
        """
        conn = self._get_conn()
        row = conn.execute(
            "SELECT COALESCE(SUM(record_count), 0) AS s FROM tasks "
            "WHERE task_type NOT IN ('batch_run', 'probe', 'cookie_refresh') "
            "AND archived_at IS NULL"
        ).fetchone()
        return int(row["s"] if row else 0)


def _row_to_task_dict(row: sqlite3.Row) -> dict:
    """Convert a SQLite Row to a task dict with JSON fields deserialized."""
    d = dict(row)
    if "logic_param" in d and isinstance(d["logic_param"], str):
        try:
            d["logic_param"] = json.loads(d["logic_param"])
        except (json.JSONDecodeError, TypeError):
            d["logic_param"] = {}
    if "snapshot_param" in d and isinstance(d["snapshot_param"], str):
        try:
            d["snapshot_param"] = json.loads(d["snapshot_param"])
        except (json.JSONDecodeError, TypeError):
            d["snapshot_param"] = {}
    if "result_files" in d and isinstance(d["result_files"], str):
        try:
            d["result_files"] = json.loads(d["result_files"])
        except (json.JSONDecodeError, TypeError):
            d["result_files"] = []
    if "depends_on_task_ids" in d:
        raw = d["depends_on_task_ids"]
        if raw is None or raw == "":
            d["depends_on_task_ids"] = []
        elif isinstance(raw, str):
            try:
                parsed = json.loads(raw)
                d["depends_on_task_ids"] = parsed if isinstance(parsed, list) else []
            except (json.JSONDecodeError, TypeError):
                d["depends_on_task_ids"] = []
    # Cast cancellation_intent from SQLite INTEGER (0/1) to Python bool so
    # callers don't need to re-coerce. Default to False if the column is
    # missing (older row decoded by tests, etc.).
    if "cancellation_intent" in d:
        d["cancellation_intent"] = bool(d["cancellation_intent"])
    else:
        d["cancellation_intent"] = False
    if "phase" not in d or d["phase"] is None:
        d["phase"] = "pre_expansion"
    return d


def _row_to_favorite_dict(row: sqlite3.Row) -> dict:
    """Convert a SQLite Row to a favorite dict with JSON logic_param deserialized."""
    d = dict(row)
    if "logic_param" in d and isinstance(d["logic_param"], str):
        try:
            d["logic_param"] = json.loads(d["logic_param"])
        except (json.JSONDecodeError, TypeError):
            d["logic_param"] = {}
    return d


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    platform TEXT NOT NULL,
    task_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    progress REAL DEFAULT 0.0,
    -- Phase 3 (param refactor): two distinct param JSON columns.
    -- logic_param   : the original request body the user submitted, byte-
    --                 for-byte (e.g. POST /api/batch with `items_from`).
    --                 Used for audit / "copy as request".
    -- snapshot_param: the executable snapshot at submit time. Defaults
    --                 explicitly filled, time templates rendered, and
    --                 `items_from` resolved to a frozen `items[]` (where
    --                 possible) so retry can reproduce the exact run.
    --                 Used by retry paths.
    logic_param TEXT NOT NULL DEFAULT '{}',
    snapshot_param TEXT NOT NULL DEFAULT '{}',
    output_dir TEXT DEFAULT '',
    result_files TEXT DEFAULT '[]',
    created_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL,
    error TEXT,
    last_heartbeat REAL,
    record_count INTEGER DEFAULT 0,
    total_bytes INTEGER DEFAULT 0,
    parent_task_id TEXT,
    depends_on_task_ids TEXT DEFAULT '[]',
    waiting_reason TEXT,
    -- Phase 2 state-machine columns --
    -- phase: 'pre_expansion' (parent before items resolved) or
    --        'post_expansion' (children created, aggregate-driven).
    -- Atomic tasks stay 'pre_expansion' forever (only parents flip).
    phase TEXT NOT NULL DEFAULT 'pre_expansion',
    -- cancellation_intent: parent-only flag set by user pressing 'cancel' on a
    -- batch parent. Cleared by resume / *_retry / continue / force_succeeded.
    -- Lets aggregator return 'cancelled' even when some children are still terminal succeeded.
    cancellation_intent INTEGER NOT NULL DEFAULT 0,
    -- archived_at: timestamp when delete_task was called. Status keeps its
    -- terminal value; this column drives 'is archived?' instead of a status enum.
    archived_at REAL,
    -- User-editable memo on parent/single tasks (<= 100 chars enforced at API layer).
    -- Not shown or edited on batch children. NULL = no note.
    note TEXT,
    -- Scheduling-plans origin tagging. Both NULL for ad-hoc UI / API submissions.
    --   origin_type    : 'plan' (auto-fired by scheduler) | 'plan_manual' (user clicked
    --                    "manual run" on a plan) | NULL (no plan involved).
    --   origin_plan_id : plans.plan_id of the submitting plan; SET NULL on plan delete
    --                    is enforced in DELETE /plans handler (no FK at DB level so
    --                    DROP+RECREATE migrations don't trip on dangling refs).
    origin_type TEXT,
    origin_plan_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_tasks_platform ON tasks(platform);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_created_at ON tasks(created_at);
CREATE INDEX IF NOT EXISTS idx_tasks_parent_id ON tasks(parent_task_id);
-- depends_on_task_ids is a JSON array; status filter prunes the LIKE scan
-- enough for our scale.
CREATE INDEX IF NOT EXISTS idx_tasks_status_depends ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_archived_at ON tasks(archived_at);
-- Reverse lookup: "all tasks fired by this plan". Used by
-- GET /api/plans/{plan_id}/tasks and by GET /api/tasks?origin_plan_id=...
CREATE INDEX IF NOT EXISTS idx_tasks_origin_plan ON tasks(origin_plan_id);

-- Audit log: every status / phase / intent change of any task lands here.
-- Append-only. Ordered by `id` (autoinc) within a task to recover the
-- chronological log even when multiple rows share the same `created_at`.
CREATE TABLE IF NOT EXISTS task_status_transitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT NOT NULL,
    from_phase TEXT,
    to_phase TEXT,
    -- action: e.g. 'transition' | 'aggregate_changed' | 'set_cancellation_intent'
    --              | 'clear_cancellation_intent' | 'expand_into_phase_b' | 'cancel'
    --              | 'pause' | 'resume' | 'force_succeeded' | 'force_failed'
    --              | 'failed_retry' | 'full_retry' | 'continue'
    action TEXT NOT NULL,
    -- actor: 'user' | 'system' | 'system_recover' | 'worker' | 'scheduler'
    actor TEXT NOT NULL,
    reason TEXT,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_transitions_task_id ON task_status_transitions(task_id, id DESC);

CREATE TABLE IF NOT EXISTS notification_channels (
    name TEXT PRIMARY KEY,
    webhook_url TEXT NOT NULL,
    enabled INTEGER DEFAULT 1,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS notification_rules (
    rule_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    channel_name TEXT NOT NULL,
    enabled INTEGER DEFAULT 1,
    created_at REAL NOT NULL,
    FOREIGN KEY (channel_name) REFERENCES notification_channels(name)
);

CREATE INDEX IF NOT EXISTS idx_rules_event ON notification_rules(event_type);

CREATE TABLE IF NOT EXISTS cookie_health (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    failure_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cookie_health_platform ON cookie_health(platform);
CREATE INDEX IF NOT EXISTS idx_cookie_health_time ON cookie_health(failure_at);

CREATE TABLE IF NOT EXISTS cookie_probes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    cookie_label TEXT NOT NULL,
    probe_time REAL NOT NULL,
    task_type TEXT NOT NULL,
    result TEXT NOT NULL,
    error_message TEXT DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_probes_platform_label ON cookie_probes(platform, cookie_label);
CREATE INDEX IF NOT EXISTS idx_probes_time ON cookie_probes(probe_time);

CREATE TABLE IF NOT EXISTS favorites (
    favorite_id TEXT PRIMARY KEY,
    platform TEXT NOT NULL,
    task_type TEXT NOT NULL,
    logic_param TEXT DEFAULT '{}',
    note TEXT DEFAULT '',
    source_task_id TEXT DEFAULT '',
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_favorites_platform ON favorites(platform);

-- Throughput sampling: each row is a snapshot of a task's CUMULATIVE record_count
-- at time `ts`. Stored as cumulative (not delta) so the sampler is idempotent and
-- back-fills are trivial. Speed = (curr.record_count - prev.record_count) / (curr.ts - prev.ts).
-- Written by daemon throttle heartbeat (~ every 5s per active task) and at terminal
-- status transitions. Cleaned up lazily (keep ~25h of history; dashboard widest window is 24h).
CREATE TABLE IF NOT EXISTS record_samples (
    task_id TEXT NOT NULL,
    ts REAL NOT NULL,
    record_count INTEGER NOT NULL,
    PRIMARY KEY (task_id, ts)
);

CREATE INDEX IF NOT EXISTS idx_record_samples_ts ON record_samples(ts);
CREATE INDEX IF NOT EXISTS idx_record_samples_task_ts ON record_samples(task_id, ts);

-- =========================================================================
-- Global "flux" (system-wide download throughput).
--
-- Why decoupled from `tasks` and `record_samples`:
--   `tasks.record_count` is per-run "progress / 终态" — it is reset to 0 on
--   retry and disappears on purge. That's the right semantics for the task
--   card UI but the WRONG one for "system download speed" / "lifetime
--   downloaded count" widgets, where the user wants pure FLUX (every record
--   ever written counts; retry/archive/purge MUST NOT reduce it).
--
-- Layout:
--   global_flux_counter — single-row persisted total. Recovered at daemon
--     boot so the counter survives restarts.
--   global_flux_samples — append-only timeseries with NO task_id column.
--     Drives the dashboard speed chart. Untouched by purge_task / archive /
--     retry. Cleaned up lazily (~25h retention, same as record_samples).
-- =========================================================================
CREATE TABLE IF NOT EXISTS global_flux_counter (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    record_count INTEGER NOT NULL DEFAULT 0,
    updated_at   REAL    NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO global_flux_counter (id, record_count, updated_at) VALUES (1, 0, 0);

CREATE TABLE IF NOT EXISTS global_flux_samples (
    ts           REAL    PRIMARY KEY,
    record_count INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_global_flux_ts ON global_flux_samples(ts);

-- =========================================================================
-- Scheduling plans ("调度计划组" feature). Layout:
--   plan_groups (1) -< plans (1) -< plan_triggers   (cron / interval / once)
--                                  -< plan_steps    (ordered task templates)
-- Tasks fired by a plan carry origin_plan_id (see tasks.origin_plan_id above).
-- No FK at DB level: DELETE /plans handler is responsible for cascading
-- triggers / steps and SET NULL-ing tasks.origin_plan_id, mirroring the rest
-- of this codebase's hand-rolled cascade convention.
-- =========================================================================

CREATE TABLE IF NOT EXISTS plan_groups (
    group_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    note TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_plan_groups_name ON plan_groups(name);

CREATE TABLE IF NOT EXISTS plans (
    plan_id TEXT PRIMARY KEY,
    group_id TEXT NOT NULL,
    name TEXT NOT NULL,
    -- 0 / 1 — master switch for the plan. Triggers also have their own enabled
    -- flag; effective "will fire" = plan.enabled AND trigger.enabled.
    enabled INTEGER NOT NULL DEFAULT 0,
    -- IANA timezone name, e.g. 'Asia/Shanghai'. Validated via zoneinfo.ZoneInfo
    -- at the API layer. Cron / once expressions are interpreted in this tz.
    timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
    -- Whether to emit on_plan_step_submit_failed when a step fails to submit.
    notify_on_fire_fail INTEGER NOT NULL DEFAULT 1,
    note TEXT,
    -- Bookkeeping for UI "last fired" column. Updated by PlanScheduler.fire().
    last_fired_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    -- A plan name is unique within its group (UI-level constraint).
    UNIQUE(group_id, name)
);

CREATE INDEX IF NOT EXISTS idx_plans_group ON plans(group_id);
CREATE INDEX IF NOT EXISTS idx_plans_enabled ON plans(enabled);

CREATE TABLE IF NOT EXISTS plan_triggers (
    trigger_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    -- 'cron' | 'interval' | 'once'
    kind TEXT NOT NULL,
    -- For kind='cron'    : a 5-field crontab string, e.g. '0 9 * * 1-5'.
    -- For kind='interval': JSON like {"hours": 1} or {"minutes": 15}; exactly
    --                      one of {seconds, minutes, hours, days} with int >= 1.
    -- For kind='once'    : ISO-8601 datetime string (interpreted in plan.timezone).
    expr TEXT NOT NULL,
    -- 0 / 1 — per-trigger switch. Effective "will fire" = plan.enabled AND trigger.enabled.
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_triggers_plan ON plan_triggers(plan_id);

CREATE TABLE IF NOT EXISTS plan_steps (
    step_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    -- 0-based ordinal within the plan. Server reassigns 0..N-1 by list order on save.
    step_index INTEGER NOT NULL,
    -- 'task'  (POST /api/task body)  |  'batch' (POST /api/batch body).
    -- This is the wire-level kind, not an internal classifier — it directly
    -- selects the submit endpoint at fire time.
    request_kind TEXT NOT NULL,
    platform TEXT NOT NULL,
    -- The action / task_type to submit, e.g. 'get_game_info', 'search_games'.
    -- Mirrors body.task_type (kind=task) or body.action (kind=batch); kept
    -- as a top-level column for cheap filtering / display.
    task_type TEXT NOT NULL,
    -- Full POST /api/task or POST /api/batch JSON body, stored verbatim.
    -- May contain ${YYYYMMDD}-style time placeholders and ${step[K].task_id}
    -- cross-step references — both resolved at fire time, not save time.
    -- Forward references (K >= step_index) are rejected by API validation.
    request_payload TEXT NOT NULL DEFAULT '{}',
    note TEXT,
    created_at REAL NOT NULL,
    UNIQUE(plan_id, step_index)
);

CREATE INDEX IF NOT EXISTS idx_steps_plan ON plan_steps(plan_id, step_index);
"""



