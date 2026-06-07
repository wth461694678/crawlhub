"""Task state-machine engine.

This module owns:

  * `ALLOWED_TRANSITIONS` — the truth-table of legal (from_status, action)
    combinations. Mirrors spec §3 1:1.
  * `transition_task` — the high-level entry point. Validates against the
    table, performs an atomic CAS update on `tasks.status`, and writes a
    row to `task_status_transitions`. Single source of truth for any
    status mutation in Phase 2+.
  * `aggregate_parent_status` — pure function that turns a list of child
    statuses + a parent's `cancellation_intent` flag into the parent's
    aggregate status (spec §1.4).
  * `aggregate_with_lock` — wraps `aggregate_parent_status` with a
    parent_id-keyed lock and writes an `aggregate_changed` transition row
    when (and only when) the aggregate result actually changes (spec §0.8,
    §5.2 dedup rule).
  * `is_dependency_ready` — the runtime gate for `waiting_dependency →
    queued` (spec §4.1).

Naming convention:
  * `action` — the verb that drove the transition. Member of `Action`.
  * `actor`  — who triggered it ('user' / 'system' / 'system_recover' /
    'worker' / 'scheduler').
  * `from_status` / `to_status` — the SQLite tasks.status column values
    (strings, matching `TaskStatus` enum values).

The store is injected (not imported directly) so unit tests can pass an
in-memory fake without spinning up SQLite.
"""

from __future__ import annotations

import threading
from typing import Any, Iterable

from crawlhub.core.models import TaskStatus


# ---------------------------------------------------------------------------
# Action constants
# ---------------------------------------------------------------------------
# Atomic-task actions (spec v4 §2.1). Parent-level sugar (cancel /
# force_succeeded / full_retry / failed_retry) is implemented in
# daemon.apply_parent_action and ultimately fans out to atomic actions
# defined here.
#
# v4 (2026-05-12): Removed PAUSE / RESUME actions. Users who want to
# "pause and continue later" go through cancel -> full_retry instead.
class Action:
    DEPS_READY = "deps_ready"
    FORCE_START = "force_start"
    SCHEDULER_DISPATCH = "scheduler_dispatch"
    CANCEL = "cancel"
    FULL_RETRY = "full_retry"
    FORCE_SUCCEEDED = "force_succeeded"
    NATURAL_COMPLETE = "natural_complete"
    NATURAL_FAIL = "natural_fail"
    NATURAL_PARTIAL = "natural_partial"  # atomic task with errors > 0 but records > 0
    SYSTEM_INTERRUPT = "system_interrupt"

    # Audit-log-only actions (no state change of `status`):
    AGGREGATE_CHANGED = "aggregate_changed"
    SET_CANCELLATION_INTENT = "set_cancellation_intent"
    CLEAR_CANCELLATION_INTENT = "clear_cancellation_intent"
    EXPAND_INTO_PHASE_B = "expand_into_phase_b"


# ---------------------------------------------------------------------------
# Transition table (spec §3)
# ---------------------------------------------------------------------------
# Map: (from_status, action) -> to_status
# Direct encoding of the spec §3 cross-table. Read top→bottom is "to" axis,
# but here we key on (from, action) for O(1) validation in transition_task.
_S = TaskStatus  # local alias
ALLOWED_TRANSITIONS: dict[tuple[str, str], str] = {
    # --- waiting_dependency -> queued ---
    (_S.WAITING_DEPENDENCY.value, Action.DEPS_READY): _S.QUEUED.value,
    (_S.WAITING_DEPENDENCY.value, Action.FORCE_START): _S.QUEUED.value,

    # --- queued -> running ---
    (_S.QUEUED.value, Action.SCHEDULER_DISPATCH): _S.RUNNING.value,

    # --- running -> succeeded / partial_succeeded / failed ---
    (_S.RUNNING.value, Action.NATURAL_COMPLETE): _S.SUCCEEDED.value,
    (_S.RUNNING.value, Action.NATURAL_PARTIAL): _S.PARTIAL_SUCCEEDED.value,
    (_S.RUNNING.value, Action.NATURAL_FAIL): _S.FAILED.value,

    # --- cancel from non-terminal states ---
    (_S.WAITING_DEPENDENCY.value, Action.CANCEL): _S.CANCELLED.value,
    (_S.QUEUED.value, Action.CANCEL): _S.CANCELLED.value,
    (_S.RUNNING.value, Action.CANCEL): _S.CANCELLED.value,
    (_S.INTERRUPTED.value, Action.CANCEL): _S.CANCELLED.value,

    # --- full_retry from terminal-ish states ---
    (_S.SUCCEEDED.value, Action.FULL_RETRY): _S.QUEUED.value,
    (_S.PARTIAL_SUCCEEDED.value, Action.FULL_RETRY): _S.QUEUED.value,
    (_S.FAILED.value, Action.FULL_RETRY): _S.QUEUED.value,
    (_S.CANCELLED.value, Action.FULL_RETRY): _S.QUEUED.value,
    (_S.INTERRUPTED.value, Action.FULL_RETRY): _S.QUEUED.value,

    # --- force_succeeded: nearly universal source (excluding succeeded itself) ---
    (_S.WAITING_DEPENDENCY.value, Action.FORCE_SUCCEEDED): _S.SUCCEEDED.value,
    (_S.QUEUED.value, Action.FORCE_SUCCEEDED): _S.SUCCEEDED.value,
    (_S.RUNNING.value, Action.FORCE_SUCCEEDED): _S.SUCCEEDED.value,
    (_S.FAILED.value, Action.FORCE_SUCCEEDED): _S.SUCCEEDED.value,
    (_S.CANCELLED.value, Action.FORCE_SUCCEEDED): _S.SUCCEEDED.value,
    (_S.INTERRUPTED.value, Action.FORCE_SUCCEEDED): _S.SUCCEEDED.value,
    (_S.PARTIAL_SUCCEEDED.value, Action.FORCE_SUCCEEDED): _S.SUCCEEDED.value,

    # --- system_interrupt: daemon shutdown stamps ---
    (_S.QUEUED.value, Action.SYSTEM_INTERRUPT): _S.INTERRUPTED.value,
    (_S.RUNNING.value, Action.SYSTEM_INTERRUPT): _S.INTERRUPTED.value,
}


# Actions that clear cancellation_intent on the parent when applied via
# parent-level fan-out (spec §1.4 / §2.2). Applied by daemon.apply_parent_action.
#
# v4: RESUME removed (parent action was deleted). FULL_RETRY now covers
# the "user wants to continue after cancellation" path.
INTENT_CLEARING_ACTIONS = frozenset({
    Action.FULL_RETRY,
    Action.FORCE_SUCCEEDED,
    # 'failed_retry' is parent-level sugar that fans out to FULL_RETRY at
    # the child level — clearing is done by daemon.apply_parent_action,
    # not here. INTENT_CLEARING_ACTIONS is about *atomic* actions applied
    # to children.
})


class IllegalTransitionError(Exception):
    """Raised when can_transition / transition_task rejects an action."""

    def __init__(self, task_id: str, current_status: str, action: str):
        self.task_id = task_id
        self.current_status = current_status
        self.action = action
        legal = sorted(
            a for (s, a) in ALLOWED_TRANSITIONS.keys() if s == current_status
        )
        super().__init__(
            f"Illegal transition for task {task_id}: "
            f"current_status='{current_status}', action='{action}'. "
            f"Legal actions from '{current_status}': {legal or '<terminal>'}."
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def can_transition(current_status: str, action: str) -> bool:
    """O(1) check: is `action` legal from `current_status`?"""
    return (current_status, action) in ALLOWED_TRANSITIONS


def lookup_target(current_status: str, action: str) -> str | None:
    """Return the target status, or None if the transition is illegal."""
    return ALLOWED_TRANSITIONS.get((current_status, action))


def transition_task(
    store: Any,
    task_id: str,
    action: str,
    actor: str,
    reason: str | None = None,
    extra_updates: dict | None = None,
) -> dict:
    """Validate + apply + audit-log a single status transition.

    Returns the updated task dict.

    Concurrency: relies on `store.atomic_transition` (CAS on status) so two
    concurrent callers can't both successfully apply the same transition.
    The loser raises IllegalTransitionError because by the time it retries
    the read, current_status no longer matches.

    `extra_updates` lets callers stamp `started_at` / `finished_at` /
    `error` etc. in the same UPDATE so it's still atomic with the status
    flip. Forbidden keys: 'status', 'task_id'.
    """
    task = store.get_task(task_id)
    if task is None:
        raise IllegalTransitionError(task_id, "<missing>", action)

    current = task["status"]
    target = lookup_target(current, action)
    if target is None:
        raise IllegalTransitionError(task_id, current, action)

    updates = dict(extra_updates or {})
    # CAS update — guarantees only one transition wins under concurrency.
    ok = store.atomic_transition(
        task_id=task_id,
        from_status=current,
        to_status=target,
        updates=updates,
    )
    if not ok:
        # Status changed under our feet; re-read and surface the new state.
        fresh = store.get_task(task_id) or {}
        raise IllegalTransitionError(task_id, fresh.get("status", "<lost>"), action)

    # Audit log row — must succeed; if it doesn't, the status update has
    # already committed and we leak the gap. Practically, sqlite append-only
    # writes don't fail except on disk-full / locked; surface the exception.
    store.insert_transition(
        task_id=task_id,
        from_status=current,
        to_status=target,
        action=action,
        actor=actor,
        reason=reason,
    )

    return store.get_task(task_id)


# ---------------------------------------------------------------------------
# Dependency-ready gate (spec §4.1)
# ---------------------------------------------------------------------------

def is_dependency_ready(upstream_status: str, require_full_success: bool) -> bool:
    """Pure predicate: can the downstream advance given upstream's effective status?

    `upstream_status` should be the *effective* status — i.e. for parent
    tasks in post_expansion phase, callers must pass the aggregated status
    (not the raw `tasks.status` column, which carries stale data from
    pre-expansion). Atomic tasks pass `tasks.status` directly.
    """
    if upstream_status == TaskStatus.SUCCEEDED.value:
        return True
    if upstream_status == TaskStatus.PARTIAL_SUCCEEDED.value:
        return not require_full_success
    return False


def effective_status(task: dict, store: Any | None = None) -> str:
    """Resolve a task's "effective" status for dependency-ready / UI purposes.

    For atomic tasks and pre-expansion parents this equals `tasks.status`.
    For post-expansion parents, the column may carry a stale value (because
    we only stamp it via `aggregate_changed` transitions, and the most
    recent transition row is canonical per spec §5.2). If a `store` is
    provided we cross-check against `fetch_latest_transition` and prefer
    that to_status.
    """
    raw = task.get("status") or ""
    phase = task.get("phase") or "pre_expansion"
    if phase != "post_expansion" or store is None:
        return raw
    latest = store.fetch_latest_transition(task["task_id"])
    if latest is None:
        return raw
    return latest.get("to_status") or raw


# ---------------------------------------------------------------------------
# Parent-status aggregation (spec §1.4)
# ---------------------------------------------------------------------------

def aggregate_parent_status(
    children: Iterable[dict],
    cancellation_intent: bool,
) -> str:
    """Compute the parent's aggregate status from its children. Pure function.

    Encodes spec §1.4 priorities 1-10. Idempotent.

    Inputs:
      children: iterable of dicts each carrying at minimum a 'status' key.
      cancellation_intent: parent's flag.

    Edge cases:
      * Empty children list → 'queued' (rare; happens only if items
        resolution returned 0 items, which the daemon should reject earlier).
      * Unknown status strings are ignored from counts but still count
        toward `total`, so they participate in the priority-6/7/8
        all-equal checks but not the typed counts. Conservative: we'd
        rather report 'partial_succeeded' than crash.
    """
    counts: dict[str, int] = {
        TaskStatus.WAITING_DEPENDENCY.value: 0,
        TaskStatus.QUEUED.value: 0,
        TaskStatus.RUNNING.value: 0,
        TaskStatus.SUCCEEDED.value: 0,
        TaskStatus.FAILED.value: 0,
        TaskStatus.CANCELLED.value: 0,
        TaskStatus.INTERRUPTED.value: 0,
    }
    total = 0
    for child in children:
        total += 1
        s = child.get("status")
        if s in counts:
            counts[s] += 1

    if total == 0:
        return TaskStatus.QUEUED.value

    # Priority 1: any child running → parent running (regardless of intent).
    if counts[TaskStatus.RUNNING.value] > 0:
        return TaskStatus.RUNNING.value

    # Priority 2: any child queued/waiting → parent queued/waiting (the more "active" one).
    if counts[TaskStatus.QUEUED.value] > 0:
        return TaskStatus.QUEUED.value
    if counts[TaskStatus.WAITING_DEPENDENCY.value] > 0:
        return TaskStatus.WAITING_DEPENDENCY.value

    # Priority 4: interrupted (system error) — was previously above paused.
    # v4: paused removed; interrupted retains its non-terminal slot.
    if counts[TaskStatus.INTERRUPTED.value] > 0:
        return TaskStatus.INTERRUPTED.value

    # Past this point all children are terminal: succeeded / failed / cancelled.
    succ = counts[TaskStatus.SUCCEEDED.value]
    fail = counts[TaskStatus.FAILED.value]
    canc = counts[TaskStatus.CANCELLED.value]

    # Priority 6/7: pure-success or pure-failure.
    if succ == total:
        return TaskStatus.SUCCEEDED.value
    if fail == total:
        return TaskStatus.FAILED.value

    # Priority 8: all cancelled (spec §1.5 option B — intent-independent).
    if canc == total:
        return TaskStatus.CANCELLED.value

    # Priority 9: mixed terminal + intent flag → cancelled.
    if cancellation_intent:
        return TaskStatus.CANCELLED.value

    # Priority 10: catch-all for any mixed-terminal case (spec §1.5.1).
    # Includes (1 fail + 2 cancel + intent=false) per the spec rebuild.
    return TaskStatus.PARTIAL_SUCCEEDED.value


# ---------------------------------------------------------------------------
# Per-parent serialization for aggregate writes
# ---------------------------------------------------------------------------
# We keep one Lock per parent_id so that two child-status callbacks for the
# same parent serialize, but different parents proceed concurrently. Locks
# are stored in a dict guarded by `_locks_guard`.
_parent_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _get_parent_lock(parent_id: str) -> threading.Lock:
    with _locks_guard:
        lock = _parent_locks.get(parent_id)
        if lock is None:
            lock = threading.Lock()
            _parent_locks[parent_id] = lock
        return lock


def aggregate_with_lock(
    store: Any,
    parent_id: str,
    children: Iterable[dict] | None = None,
) -> dict:
    """Recompute parent's aggregate status under a parent-scoped lock.

    Returns: {'old_status': str, 'new_status': str, 'changed': bool}

    Behavior (spec §0.8, §5.2 dedup):
      * Reads parent + children inside the lock (so the snapshot is
        consistent w.r.t. the latest child write that triggered us).
      * Computes aggregate_parent_status.
      * Compares to the *previous* aggregate result (the to_status of the
        most recent transition row for the parent). If equal, no row is
        written — this is the dedup that prevents N concurrent worker
        callbacks from spamming N identical rows.
      * If different, writes one `aggregate_changed` transition.
      * Does NOT update `tasks.status` of the parent (post_expansion
        parents have stale `tasks.status` by design — transition log is
        the source of truth, and `effective_status` consults it).
    """
    parent_lock = _get_parent_lock(parent_id)
    with parent_lock:
        parent = store.get_task(parent_id)
        if parent is None:
            return {"old_status": None, "new_status": None, "changed": False}

        if children is None:
            children = store.list_tasks(parent_id=parent_id, limit=10000)

        intent = bool(parent.get("cancellation_intent", False))
        new_status = aggregate_parent_status(children, intent)

        # Dedup against the previous aggregate result.
        latest = store.fetch_latest_transition(parent_id)
        old_status = latest.get("to_status") if latest else parent.get("status")

        if old_status == new_status:
            return {"old_status": old_status, "new_status": new_status, "changed": False}

        store.insert_transition(
            task_id=parent_id,
            from_status=old_status,
            to_status=new_status,
            action=Action.AGGREGATE_CHANGED,
            actor="system",
            reason=None,
        )
        # Mirror to tasks.status for cheap reads (lists, indexes). Not the
        # source of truth but keeps SELECT-by-status filtering working.
        store.update_task(parent_id, {"status": new_status})

        return {"old_status": old_status, "new_status": new_status, "changed": True}
