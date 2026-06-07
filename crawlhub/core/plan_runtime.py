"""Plan runtime helpers: step-reference resolution + dep extraction.

These helpers are pure functions used by ``plan_scheduler.fire()``. Keeping
them isolated (no daemon / no DB) makes the unit tests trivial and the
fire path easy to reason about.

Resolution order (per requirements.md §7.2):
    1. ``resolve_step_refs`` — replace ``${step[K].task_id}`` placeholders
       with concrete IDs of already-submitted prior steps.
    2. ``time_template.render_obj`` (NOT in this module) — render
       ``${YYYY-MM-DD}`` etc. against the instance datetime.
    3. ``extract_resolved_deps`` — pluck ``depends_on_task_ids`` from the
       resolved input so the daemon honors plan-declared deps.
"""

from __future__ import annotations

import re
from typing import Any


_STEP_REF_RE = re.compile(r"\$\{step\[(\d+)\]\.task_id\}")


def resolve_step_refs(obj: Any, submitted_task_ids: list[str]) -> Any:
    """Walk ``obj`` and replace every ``${step[K].task_id}`` literal with
    the K-th already-submitted task ID.

    Parameters
    ----------
    obj
        Arbitrary input — typically the step's ``request_payload``
        (already deserialized to dict/list/etc.).
    submitted_task_ids
        IDs of prior steps in this fire, in order. ``submitted_task_ids[K]``
        is the resolved ID for placeholder ``${step[K].task_id}``.

    Raises
    ------
    ValueError
        If any placeholder references K >= len(submitted_task_ids). This
        catches both forward references (``step[i]`` inside step ``i``'s
        own template) and out-of-range references — they collapse into the
        same error class because at fire time we only know "what's been
        submitted so far".
    """
    if isinstance(obj, str):
        return _resolve_string(obj, submitted_task_ids)
    if isinstance(obj, dict):
        return {k: resolve_step_refs(v, submitted_task_ids) for k, v in obj.items()}
    if isinstance(obj, list):
        return [resolve_step_refs(v, submitted_task_ids) for v in obj]
    if isinstance(obj, tuple):
        return tuple(resolve_step_refs(v, submitted_task_ids) for v in obj)
    return obj


def _resolve_string(s: str, submitted: list[str]) -> str:
    def _sub(m: re.Match) -> str:
        k = int(m.group(1))
        if k >= len(submitted):
            raise ValueError(
                f"step[{k}] not yet submitted (only {len(submitted)} prior steps)"
            )
        return submitted[k]

    return _STEP_REF_RE.sub(_sub, s)


def extract_resolved_deps(input_obj: Any) -> list[str]:
    """Pull ``depends_on_task_ids`` from a resolved input object.

    The contract: only a top-level ``depends_on_task_ids`` key, only values
    that are lists of strings, are honored. Anything else returns an empty
    list — defensively, so a malformed template can't accidentally inject
    a non-task-id into the daemon's dep set.
    """
    if not isinstance(input_obj, dict):
        return []
    raw = input_obj.get("depends_on_task_ids")
    if not isinstance(raw, list):
        return []
    return [v for v in raw if isinstance(v, str)]
