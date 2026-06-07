"""Phase 3 (param refactor): build executable snapshot params from raw
request bodies.

The store keeps two flavors of params per task:
  * logic_param   -- exactly what the user POSTed (incl. ``items_from``,
                     missing-default-keys, time templates, etc.).
  * snapshot_param -- the executable view: defaults filled, time templates
                      rendered, ``items_from`` resolved into a frozen
                      ``items[]`` (or left as items[] for batch parents
                      whose items are still being resolved asynchronously).

This module owns the *single* source of truth for the default-fill +
template-render rules. Daemon.submit_task / submit_batch and the batch
orchestrator's create_batch / waiting-task-finalize all funnel through
here so the snapshot semantics never drift between paths.

Time template rendering is intentionally simple: we walk the dict once
and substitute ``${YYYYMMDD}`` / ``${YYYY-MM-DD}`` / ``${HH}`` / ``${MM}``
/ ``${SS}`` against the *submit moment*. Cross-step references like
``${step[K].task_id}`` are NOT resolved here -- those belong to the plan
scheduler's fire path, which renders the request_payload before passing
it down to submit_task / submit_batch.
"""

from __future__ import annotations

import re
import time
from typing import Any

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

# Defaults applied to *batch* snapshots only. Kept here so a single grep
# locates every implicit default. Single-task snapshots get only the
# common-params + cookie defaults (see _fill_task_defaults).
_BATCH_DEFAULTS: dict[str, Any] = {
    "concurrency": 1,
    "fail_strategy": "continue",
    "allow_partial_upstream": True,
    "cookie_policy": {},
}

# common_params defaults applied to *both* task and batch snapshots.
_COMMON_PARAMS_DEFAULTS: dict[str, Any] = {
    "treat_empty_as_success": True,
}


# ---------------------------------------------------------------------------
# Time template rendering
# ---------------------------------------------------------------------------

# Patterns are intentionally narrow: only the set documented in the plan-
# step schema. Unknown ${...} tokens pass through untouched (the plan
# scheduler may render them later).
_TIME_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\$\{YYYYMMDD\}"), "%Y%m%d"),
    (re.compile(r"\$\{YYYY-MM-DD\}"), "%Y-%m-%d"),
    (re.compile(r"\$\{YYYY\}"), "%Y"),
    (re.compile(r"\$\{MM\}"), "%m"),
    (re.compile(r"\$\{DD\}"), "%d"),
    (re.compile(r"\$\{HH\}"), "%H"),
    (re.compile(r"\$\{mm\}"), "%M"),  # note: minute uses lowercase mm to disambiguate
    (re.compile(r"\$\{SS\}"), "%S"),
]


def render_time_templates(value: Any, *, now: float | None = None) -> Any:
    """Recursively render time placeholders inside any JSON-ish value.

    Only string leaves are touched. Lists / dicts are walked. The ``now``
    parameter is taken so callers (esp. tests) can pin the moment.
    """
    if value is None:
        return None
    if isinstance(value, str):
        if "${" not in value:
            return value
        ts = time.localtime(now) if now is not None else time.localtime()
        out = value
        for pat, fmt in _TIME_PATTERNS:
            out = pat.sub(time.strftime(fmt, ts), out)
        return out
    if isinstance(value, list):
        return [render_time_templates(v, now=now) for v in value]
    if isinstance(value, dict):
        return {k: render_time_templates(v, now=now) for k, v in value.items()}
    return value


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_task_snapshot(logic_param: dict[str, Any], *, now: float | None = None) -> dict[str, Any]:
    """Build snapshot_param for a single (non-batch) task.

    Rules:
      * Time templates rendered against ``now``.
      * ``common_params.treat_empty_as_success`` defaulted to True.
      * No other implicit defaults -- per-platform action params stay
        whatever the caller passed in (we don't have a platform-level
        default registry yet, and silently inserting a value here would
        be more confusing than helpful).

    Returns a *new* dict; the input is not mutated.
    """
    rendered = render_time_templates(dict(logic_param or {}), now=now)
    common = dict(rendered.get("common_params") or {})
    for k, v in _COMMON_PARAMS_DEFAULTS.items():
        common.setdefault(k, v)
    rendered["common_params"] = common
    return rendered


def build_batch_snapshot(
    payload: dict[str, Any],
    *,
    resolved_items: list[str] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Build snapshot_param for a batch parent task.

    Args:
        payload: The original POST /api/batch body (i.e. logic_param).
        resolved_items: When the items have been materialized at submit
            time (file mode, list mode, or already-ready upstream), pass
            them here; the snapshot will carry items[] + items_count and
            DROP items_from. When items resolution is asynchronous (e.g.
            upstream not yet ready), pass None -- the snapshot will carry
            an empty items[] and the caller is responsible for filling it
            in via ``finalize_batch_snapshot`` once items resolve.
        now: pin the time-template rendering moment (for tests).

    Returns a *new* dict -- the payload is not mutated.
    """
    rendered = render_time_templates(dict(payload or {}), now=now)
    snapshot: dict[str, Any] = {}
    # Carry over everything except items_from -- snapshot is the post-
    # resolution view.
    for k, v in rendered.items():
        if k == "items_from":
            continue
        snapshot[k] = v
    # Apply batch-level defaults.
    for k, v in _BATCH_DEFAULTS.items():
        snapshot.setdefault(k, v)
    # common_params defaults.
    common = dict(snapshot.get("common_params") or {})
    for k, v in _COMMON_PARAMS_DEFAULTS.items():
        common.setdefault(k, v)
    snapshot["common_params"] = common
    # Items: prefer the resolved list if given, otherwise whatever the
    # payload carried (which is empty for upstream-resolved batches).
    if resolved_items is not None:
        snapshot["items"] = list(resolved_items)
        snapshot["items_count"] = len(resolved_items)
    else:
        snapshot.setdefault("items", [])
        snapshot["items_count"] = len(snapshot["items"])
    return snapshot


def finalize_batch_snapshot(
    existing_snapshot: dict[str, Any], items: list[str]
) -> dict[str, Any]:
    """Fill in items[] / items_count on an async-resolved batch snapshot.

    Used by BatchOrchestrator.create_children_for_waiting_task once the
    upstream tasks complete and items are known. Returns a new dict; the
    original is not mutated.
    """
    snapshot = dict(existing_snapshot or {})
    snapshot["items"] = list(items)
    snapshot["items_count"] = len(items)
    # items_from must NOT be in the snapshot. Strip defensively in case
    # a caller hands us a raw payload by mistake.
    snapshot.pop("items_from", None)
    return snapshot
