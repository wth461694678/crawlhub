"""Cookie health tracker — pure state, no I/O (R4 R5).

Lives next to ``DouyinCookieJar`` but is a separate concern: counts
consecutive request failures so callers can decide whether to refresh.
Stays cheap and synchronous — no file I/O, no HTTP.

Usage::

    tracker = HealthTracker(log_prefix="dy_sdk.health")
    # in the request hot path:
    if status == 0:
        tracker.record_success()
    else:
        tracker.record_failure()

    if tracker.is_stale():
        # caller decides what to do (refresh, escalate, ...)
        ...
"""
from __future__ import annotations

import sys
import time


class HealthTracker:
    """Counts consecutive failures and exposes a ``is_stale()`` verdict.

    Threshold is fixed at 3 (matches the legacy ``CookieManager`` value).
    The class is intentionally minimal — no persistence, no callbacks.
    """

    MAX_CONSECUTIVE_FAILURES = 3

    def __init__(self, log_prefix: str = "health") -> None:
        self._log_prefix = log_prefix
        self._consecutive_failures = 0
        self._last_success_time: float = 0.0

    def _log(self, msg: str) -> None:
        print(f"[{self._log_prefix}] {msg}", file=sys.stderr)

    # ── Mutations ───────────────────────────────────────────

    def record_success(self) -> None:
        """Reset failure counter and stamp the last-success timestamp."""
        self._consecutive_failures = 0
        self._last_success_time = time.time()

    def record_failure(self) -> None:
        """Increment failure counter. Logs once we cross the staleness line."""
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.MAX_CONSECUTIVE_FAILURES:
            self._log(
                f"[WARN] {self._consecutive_failures} consecutive failures - "
                f"cookies may be stale, consider refreshing."
            )

    def reset(self) -> None:
        """Clear the failure counter without recording a success time."""
        self._consecutive_failures = 0

    # ── Read accessors ──────────────────────────────────────

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    @property
    def last_success_time(self) -> float:
        return self._last_success_time

    def is_stale(self) -> bool:
        """True iff ``consecutive_failures >= MAX_CONSECUTIVE_FAILURES``."""
        return self._consecutive_failures >= self.MAX_CONSECUTIVE_FAILURES
