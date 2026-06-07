"""Thread-local cookie path override.

Why this exists
---------------
The crawler daemon may have multiple cookies registered for the same platform
(e.g. 3 bilibili accounts). When a task is dispatched, the daemon picks ONE
cookie via :class:`CookieThrottle`, applies the throttle window against that
specific cookie, and then invokes the platform service. The service in turn
asks its bridge to resolve a cookie path.

Without coordination the bridge could open a *different* cookie file than the
one the daemon throttled (e.g. it picks the most-recently-modified cookie),
producing a race we call the "ghost cookie" bug:

* daemon throttles cookie A  -> opens task slot
* bridge opens cookie B      -> the throttle/retry/fail-report logic in
                                daemon ends up annotated against the wrong
                                cookie, breaking per-cookie interval control

The fix is a **thread-local override**: the daemon publishes the resolved
cookie path into the current thread's storage *before* calling the service.
The bridge reads the override first, guaranteeing daemon and bridge agree.

Usage
-----
Daemon side (in :func:`crawlhub.core.daemon._run_task`)::

    from crawlhub.core.cookie_override import (
        set_thread_cookie_override,
        clear_thread_cookie_override,
    )

    set_thread_cookie_override(chosen_state.path)
    try:
        svc.execute(...)
    finally:
        clear_thread_cookie_override()

Bridge / crawler side::

    from crawlhub.core.cookie_override import get_thread_cookie_override

    override = get_thread_cookie_override()
    if override and Path(override).exists():
        return Path(override)

Thread safety
-------------
Backed by :class:`threading.local`, so each thread sees its own value
without locking. Calls are O(1) and never block.
"""

from __future__ import annotations

import threading

__all__ = [
    "set_thread_cookie_override",
    "clear_thread_cookie_override",
    "get_thread_cookie_override",
]


_CURRENT_COOKIE_OVERRIDE = threading.local()


def set_thread_cookie_override(path: str | None) -> None:
    """Pin the cookie path the current thread MUST use.

    The daemon calls this BEFORE invoking the service, so the bridge can
    pick up the same cookie that was just throttled. Passing ``None`` (or an
    empty string) is equivalent to :func:`clear_thread_cookie_override`.

    :param path: Absolute path string of the cookie file. ``None`` to clear.
    """
    if path:
        _CURRENT_COOKIE_OVERRIDE.path = path
    else:
        clear_thread_cookie_override()


def clear_thread_cookie_override() -> None:
    """Remove the override for the current thread.

    Idempotent: calling it without an active override is a no-op.
    Always call this in a ``finally:`` block paired with
    :func:`set_thread_cookie_override` to avoid bleeding override state into
    the next task that re-uses this worker thread.
    """
    if hasattr(_CURRENT_COOKIE_OVERRIDE, "path"):
        del _CURRENT_COOKIE_OVERRIDE.path


def get_thread_cookie_override() -> str | None:
    """Return the override pinned by the current thread.

    :returns: The cookie path string set by
        :func:`set_thread_cookie_override`, or ``None`` if no override is
        active for this thread.
    """
    return getattr(_CURRENT_COOKIE_OVERRIDE, "path", None)
