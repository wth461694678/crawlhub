"""Per-hold page handle (R7).

═══════════════════════════════════════════════════════════════════════
  PageHandle 是 scraper 操作浏览器的唯一对象（spec §8.1）
─────────────────────────────────────────────────────────────────────
  通过 with runtime.browser.hold() as page 获得；hold 退出后失效。

  全部 12 个公开方法：
    - 同步浏览器操作（runner.run 内嵌）：
        goto / evaluate / fetch_json / fetch_in_page / local_storage
        / capture_websocket / request / sign_then_request / signed_request
    - Runtime params（per-hold 缓存）：
        set_runtime_param / get_runtime_param / invalidate_runtime_param
    - Anti-crawl 信号：
        report_anti_crawl
    - Sanctioned escape hatch（_internal helpers 用）：
        .raw → PlaywrightPageWrapper
        .runner → BrowserAsyncRunner

  生命周期方法：
    _mark_closed()                 hold.__exit__ 调用
    _fallback_close_and_release()  daemon finally 兜底调用
═══════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import inspect
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

from crawlhub.core.failure_detector import FailureMode

logger = logging.getLogger(__name__)

T = TypeVar("T")


@dataclass
class _RuntimeParamEntry:
    value: Any
    expires_at: float


class PageHandle:
    """A single page owned by one hold within one task.

    R7: 通过 BrowserSessionProvider.hold() 创建；with 退出时自动关。
    scraper 应该把 with hold() 包住所有需要 page 的代码，不主动调 close。
    """

    def __init__(
        self,
        *,
        runner: Any,            # BrowserAsyncRunner
        session: Any,           # BrowserSession (owner)
        raw_page: Any,          # PlaywrightPageWrapper
    ) -> None:
        self._runner = runner
        self._session = session
        self._raw_page = raw_page
        self._closed = False
        self._runtime_params: dict[str, _RuntimeParamEntry] = {}
        self._last_response: Any = None  # per-handle，多 page 并发不污染

    # ════════════════════════════════════════════════════════════════
    #  Sanctioned escape hatch（_internal helpers 用）
    # ════════════════════════════════════════════════════════════════

    @property
    def raw(self) -> Any:
        """Return PlaywrightPageWrapper. For _internal helpers only."""
        self._check_open()
        return self._raw_page

    @property
    def runner(self) -> Any:
        """Return BrowserAsyncRunner. For _internal helpers only."""
        self._check_open()
        return self._runner

    # ════════════════════════════════════════════════════════════════
    #  浏览器操作（同步，runner.run 内嵌）
    # ════════════════════════════════════════════════════════════════

    def goto(self, url: str) -> None:
        self._check_open()
        return self._runner.run(self._raw_page.goto(url))

    def evaluate(self, script: str, arg: Any | None = None) -> Any:
        self._check_open()
        return self._runner.run(self._raw_page.evaluate(script, arg))

    def local_storage(self) -> dict[str, str]:
        self._check_open()
        return self._runner.run(self._raw_page.local_storage())

    def fetch_json(
        self,
        url: str,
        params: dict[str, Any],
        *,
        referer: str | None = None,
        task_context: object | None = None,
    ) -> dict[str, Any]:
        """HTTP fetch via page.evaluate. 全程走 request_gate 节流."""
        self._check_open()
        async def _do():
            await self._request_acquire()
            try:
                result = await self._raw_page.fetch_json(url, params, referer=referer)
            except Exception:
                self._report_failure(FailureMode.NETWORK_ERROR)
                raise
            self._report_success()
            # 同步 last_response 到 ctx（capture 链路）
            self._sync_last_response(task_context)
            return result
        return self._runner.run(_do())

    def fetch_in_page(self, fetch_script: str, arg: Any | None = None) -> Any:
        """run a fetch script inside the page (browser-side fetch)."""
        self._check_open()
        async def _do():
            await self._request_acquire()
            try:
                result = await self._raw_page.evaluate(fetch_script, arg)
            except Exception:
                self._report_failure(FailureMode.NETWORK_ERROR)
                raise
            self._report_success()
            return result
        return self._runner.run(_do())

    def request(
        self,
        operation: Callable[..., T],
        *,
        task_context: object | None = None,
        failure_mode: FailureMode = FailureMode.NETWORK_ERROR,
    ) -> T:
        """Pure HTTP request (no page operation). Throttled by request_gate."""
        self._check_open()
        async def _do():
            await self._request_acquire()
            try:
                result = await self._call(operation, task_context)
            except Exception:
                self._report_failure(failure_mode)
                raise
            self._report_success()
            return result
        return self._runner.run(_do())

    def signed_request(
        self,
        sign_script: str,
        operation: Callable[..., T],
        *,
        task_context: object | None = None,
    ) -> T:
        """sign on page → release immediately → run HTTP request.

        签名阶段占用 page evaluate；HTTP 阶段不占（page 已闲，可被其它 lease 借）。
        R7 下没有 lease 竞争——同 task 独占 page，所以"先后顺序"是逻辑而非保护。
        """
        self._check_open()
        async def _do():
            await self._request_acquire()
            try:
                signature = await self._raw_page.evaluate(sign_script)
                result = await self._call(operation, signature, task_context)
            except Exception:
                self._report_failure(FailureMode.NETWORK_ERROR)
                raise
            self._report_success()
            return result
        return self._runner.run(_do())

    def sign_then_request(
        self,
        sign_script: str,
        operation: Callable[..., T],
        *,
        task_context: object | None = None,
    ) -> T:
        """Alias for signed_request (R5 compatibility)."""
        return self.signed_request(sign_script, operation, task_context=task_context)

    def capture_websocket(
        self,
        url_substring: str,
        *,
        trigger_url: str | None = None,
        timeout_seconds: float = 15.0,
    ) -> str:
        """Capture WSS URL containing url_substring (bootstrap helper)."""
        self._check_open()
        return self._runner.run(
            self._raw_page.capture_websocket(
                url_substring,
                trigger_url=trigger_url,
                timeout_seconds=timeout_seconds,
            )
        )

    # ════════════════════════════════════════════════════════════════
    #  Runtime params（per-handle 缓存；hold 结束即清空）
    # ════════════════════════════════════════════════════════════════

    def set_runtime_param(self, key: str, value: Any, ttl_seconds: float) -> None:
        self._runtime_params[key] = _RuntimeParamEntry(
            value=value,
            expires_at=time.monotonic() + max(0.0, float(ttl_seconds)),
        )

    def get_runtime_param(self, key: str) -> Any | None:
        entry = self._runtime_params.get(key)
        if entry is None:
            return None
        if time.monotonic() >= entry.expires_at:
            self._runtime_params.pop(key, None)
            return None
        return entry.value

    def invalidate_runtime_param(self, key: str) -> None:
        self._runtime_params.pop(key, None)

    # ════════════════════════════════════════════════════════════════
    #  Anti-crawl sentinel pass-through（同步，不走 runner）
    # ════════════════════════════════════════════════════════════════

    def report_anti_crawl(
        self,
        signal: str,
        *,
        platform: str = "",
        detail: str = "",
    ) -> None:
        """Raise AntiCrawlDetected (delegates to BrowserSession).

        scraper 拿到 200 OK + verify_check 时调；同步抛异常让 daemon 错误
        归类走 FailureMode.ANTI_CRAWL。
        """
        return self._session.report_anti_crawl(
            signal, platform=platform, detail=detail,
        )

    # ════════════════════════════════════════════════════════════════
    #  生命周期（由 Provider 调用）
    # ════════════════════════════════════════════════════════════════

    def _mark_closed(self) -> None:
        """Called by Provider.hold.__exit__ after session.close_page(raw_page)."""
        self._closed = True
        self._runtime_params.clear()

    async def _fallback_close_and_release(
        self,
        manager: Any,
        key: Any,
    ) -> None:
        """Daemon finally fallback. Closes the page if not already closed; releases ref.

        Called only when a PageHandle leaks from hold's finally path (scraper
        bug / unusual exception path). Idempotent.
        """
        if not self._closed:
            try:
                await self._session.close_page(self._raw_page)
            except Exception as exc:
                logger.warning("[BBA] fallback close_page exc=%s", exc)
            self._closed = True
        # 释放 ref（hold 漏 release 时 ref 永远不归 0，chrome 永远不关）
        try:
            await manager.release(key)
        except Exception as exc:
            logger.warning("[BBA] fallback release exc=%s", exc)

    # ════════════════════════════════════════════════════════════════
    #  Internal
    # ════════════════════════════════════════════════════════════════

    def _check_open(self) -> None:
        if self._closed:
            raise RuntimeError("PageHandle has been closed (hold context exited)")

    async def _request_acquire(self) -> None:
        gate = self._session.request_gate
        if gate is not None:
            await gate.acquire_async()

    def _report_success(self) -> None:
        gate = self._session.request_gate
        if gate is not None:
            gate.report_success()

    def _report_failure(self, failure_mode: FailureMode) -> None:
        gate = self._session.request_gate
        if gate is None:
            return
        expired = gate.report_failure(failure_mode)
        if (
            expired
            and failure_mode == FailureMode.COOKIE_EXPIRED
            and self._session.on_cookie_expired is not None
        ):
            self._session.on_cookie_expired()

    def _sync_last_response(self, task_context: object | None) -> None:
        """Push raw_page.last_response into ctx.set_last_response."""
        adapter_resp = getattr(self._raw_page, "last_response", None)
        if adapter_resp is None:
            return
        self._last_response = adapter_resp
        if task_context is None:
            return
        setter = getattr(task_context, "set_last_response", None)
        if not callable(setter):
            return
        setter(adapter_resp)

    async def _call(self, operation: Callable[..., T], *args: object) -> T:
        # Allow operation to ignore extra args (sign script result + task_context)
        try:
            sig = inspect.signature(operation)
            argc = len(sig.parameters)
        except (TypeError, ValueError):
            argc = 0
        result = operation(*args[:argc])
        if inspect.isawaitable(result):
            result = await result
        return result
