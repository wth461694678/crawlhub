"""Request-level cookie throttle gate.

`RequestGate` is deliberately small: it owns time-permit accounting only.
It must not know about browser sessions, login, retry policy, or task status.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from crawlhub.core.failure_detector import FailureMode

T = TypeVar("T")


class RequestPermitTimeout(TimeoutError):
    """Raised when request-level throttle permission cannot be acquired in time."""


@dataclass
class _RequestGateBase:
    throttle: Any
    cookie_id: str
    platform: str | None = None

    def report_success(self) -> None:
        """Report a successful real request to CookieThrottle."""
        self.throttle.report_success(self.cookie_id)

    def report_failure(self, failure_mode: FailureMode) -> bool:
        """Report a failed real request to CookieThrottle."""
        return bool(self.throttle.report_failure(self.cookie_id, failure_mode))


@dataclass
class SyncRequestGate(_RequestGateBase):
    """Blocking request gate for synchronous clients."""

    def acquire(self) -> None:
        """Block until CookieThrottle grants this request's time permit."""
        self.throttle.acquire(self.cookie_id, self.platform)

    def run(
        self,
        operation: Callable[[], T],
        *,
        failure_mode: FailureMode = FailureMode.NETWORK_ERROR,
    ) -> T:
        """Acquire, execute one real request, then report success/failure."""
        self.acquire()
        try:
            result = operation()
        except Exception:
            self.report_failure(failure_mode)
            raise
        self.report_success()
        return result


@dataclass
class AsyncRequestGate(_RequestGateBase):
    """Async request gate that offloads blocking CookieThrottle.acquire."""

    async def acquire_async(self, *, timeout: float | None = None) -> None:
        """Await a request permit without blocking the event loop."""
        task = asyncio.to_thread(self.throttle.acquire, self.cookie_id, self.platform)
        try:
            if timeout is None:
                await task
                return
            await asyncio.wait_for(task, timeout=timeout)
        except TimeoutError as exc:
            raise RequestPermitTimeout(
                f"Timed out waiting for request permit: cookie_id={self.cookie_id!r}"
            ) from exc

    async def run(
        self,
        operation: Callable[[], Awaitable[T] | T],
        *,
        failure_mode: FailureMode = FailureMode.NETWORK_ERROR,
        timeout: float | None = None,
    ) -> T:
        """Acquire, execute one async-capable request, then report accounting."""
        await self.acquire_async(timeout=timeout)
        try:
            result = operation()
            if inspect.isawaitable(result):
                result = await result
        except Exception:
            self.report_failure(failure_mode)
            raise
        self.report_success()
        return result
