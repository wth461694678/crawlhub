# -*- coding: utf-8 -*-
"""Internal context flag for telemetry — distinguishes between batch-child
runs that were triggered by their parent (parent-driven first run, no
per-child completion event) and runs triggered by a local retry of a
single child (which DO emit a per-child completion event).

We use ``contextvars.ContextVar`` instead of ``threading.local`` so the
flag is naturally inherited across function calls within the same
worker invocation while remaining isolated per-task. Each worker thread
runs one task at a time, so a thread-local would also work, but
ContextVar gives us cleaner reset semantics via tokens.
"""

from __future__ import annotations

from contextvars import ContextVar


# True only while a batch child is being executed via _run_batch_child.
# False (default) for top-level tasks, batch parents, and any path that
# represents an explicit retry of a single child via /api/tasks/{id}/retry.
PARENT_DRIVEN_RUN: ContextVar[bool] = ContextVar(
    "crawlhub_parent_driven_run",
    default=False,
)
