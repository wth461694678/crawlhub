"""Dedicated asyncio loop thread for browser-backed actions."""

from __future__ import annotations

import asyncio
import concurrent.futures
import gc
import threading

from collections.abc import Coroutine

from typing import TypeVar

T = TypeVar("T")


# ────────────────────────────────────────────────────────────────────
#  Per-task log handoff
# --------------------------------------------------------------------
#  BrowserAsyncRunner runs coroutines on a *separate* thread. Without
#  handoff, anything that logger/print does inside the coroutine routes
#  through the runner thread's stdout/stderr — which has NO per-task
#  writer registered, so output silently disappears into daemon.log.
#
#  Fix: snapshot the caller thread's _thread_streams writers and rebind
#  them on the runner thread for the duration of the coroutine. Restore
#  the previous values when done. Single source of truth, zero special
#  cases, idempotent.
# ────────────────────────────────────────────────────────────────────


def _snapshot_caller_streams() -> tuple[object, object]:
    """Capture the calling thread's (stdout_writer, stderr_writer).

    Late import: ``crawlhub.core.daemon`` pulls in heavy modules; importing
    it at module-import time creates a cycle (daemon → browser → daemon).
    """
    try:
        from crawlhub.core.daemon import _thread_streams  # type: ignore
    except Exception:
        return (None, None)
    return (
        getattr(_thread_streams, "stdout", None),
        getattr(_thread_streams, "stderr", None),
    )


def _bind_streams(stdout_writer: object, stderr_writer: object) -> tuple[object, object]:
    """Bind writers on the *current* thread; return previous values for restore."""
    try:
        from crawlhub.core.daemon import _thread_streams  # type: ignore
    except Exception:
        return (None, None)
    prev = (
        getattr(_thread_streams, "stdout", None),
        getattr(_thread_streams, "stderr", None),
    )
    if stdout_writer is not None:
        _thread_streams.stdout = stdout_writer
    if stderr_writer is not None:
        _thread_streams.stderr = stderr_writer
    return prev


def _restore_streams(prev: tuple[object, object]) -> None:
    try:
        from crawlhub.core.daemon import _thread_streams  # type: ignore
    except Exception:
        return
    prev_stdout, prev_stderr = prev
    # Restore or clear — never leave runner thread polluted between tasks.
    if prev_stdout is None:
        if hasattr(_thread_streams, "stdout"):
            delattr(_thread_streams, "stdout")
    else:
        _thread_streams.stdout = prev_stdout
    if prev_stderr is None:
        if hasattr(_thread_streams, "stderr"):
            delattr(_thread_streams, "stderr")
    else:
        _thread_streams.stderr = prev_stderr


class BrowserAsyncRunner:
    """Run async browser operations from synchronous daemon worker threads."""

    def __init__(self) -> None:
        self._ready = threading.Event()
        self._closed = threading.Event()
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread = threading.Thread(target=self._loop_main, name="browser-async", daemon=True)
        self._thread.start()
        self._ready.wait()

    def run(
        self,
        coro: Coroutine[object, object, T],
        *,
        cancel_event: threading.Event | None = None,
    ) -> T:
        """Run a coroutine on the browser loop and block for its result.

        ──────────────────────────────────────────────────────────────
        |  日志归集：把调用线程的 per-task stdout/stderr 写入器借给
        |  runner 线程使用——这样 BrowserSession / PlaywrightPageWrapper
        |  里的 logger 输出（包括 [BBA] fetch.* 戳）才能落到任务级
        |  log file，而不是 daemon.log。
        ──────────────────────────────────────────────────────────────
        """
        try:
            loop = self._get_loop()
        except Exception:
            coro.close()
            raise

        caller_streams = _snapshot_caller_streams()
        wrapped: Coroutine[object, object, T] = self._with_caller_streams(coro, caller_streams)
        if cancel_event is not None:
            wrapped = self._with_cancel_event(wrapped, cancel_event)
        future = asyncio.run_coroutine_threadsafe(wrapped, loop)
        return self._wait_future(future)

    async def _with_caller_streams(
        self,
        coro: Coroutine[object, object, T],
        caller_streams: tuple[object, object],
    ) -> T:
        """Bind caller's per-task writers to runner thread for coro's lifetime."""
        stdout_writer, stderr_writer = caller_streams
        if stdout_writer is None and stderr_writer is None:
            # Nothing to bind (caller wasn't a task worker thread).
            return await coro
        prev = _bind_streams(stdout_writer, stderr_writer)
        try:
            return await coro
        finally:
            _restore_streams(prev)


    def shutdown(self, *, cancel_timeout: float = 5.0) -> None:

        """Reject new work, cancel in-flight tasks, and stop the loop."""
        with self._lock:
            if self._closed.is_set():
                return
            self._closed.set()
            loop = self._loop
        if loop is None:
            return
        cancel_future = asyncio.run_coroutine_threadsafe(self._cancel_inflight(), loop)
        try:
            cancel_future.result(timeout=cancel_timeout)
        except Exception:
            pass
        loop.call_soon_threadsafe(loop.stop)
        self._thread.join(timeout=cancel_timeout)

    def _get_loop(self) -> asyncio.AbstractEventLoop:
        if self._closed.is_set():
            raise RuntimeError("BrowserAsyncRunner has been shut down")
        if self._loop is None:
            raise RuntimeError("BrowserAsyncRunner loop is not ready")
        return self._loop

    def _wait_future(self, future: concurrent.futures.Future[T]) -> T:
        while True:
            try:
                return future.result(timeout=0.05)
            except concurrent.futures.TimeoutError:
                continue
            except concurrent.futures.CancelledError as exc:
                raise asyncio.CancelledError from exc

    async def _with_cancel_event(
        self,
        coro: Coroutine[object, object, T],
        cancel_event: threading.Event,
    ) -> T:
        task = asyncio.create_task(coro)
        while not task.done():
            if cancel_event.is_set():
                task.cancel()
                break
            await asyncio.sleep(0.01)
        return await task


    def _loop_main(self) -> None:

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.run_until_complete(asyncio.sleep(0.1))
            gc.collect()
            loop.run_until_complete(asyncio.sleep(0))
            loop.close()


    async def _cancel_inflight(self) -> None:
        current = asyncio.current_task()
        tasks = [task for task in asyncio.all_tasks() if task is not current and not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.sleep(0)
