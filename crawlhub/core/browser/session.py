"""Chrome process wrapper (R7).

═══════════════════════════════════════════════════════════════════════
  R7 简化（spec R7-browser-page-action-lifecycle.md §8.2）
─────────────────────────────────────────────────────────────────────
  BrowserSession 是一个 chrome 进程的轻封装：
    - _owned_pages: set 跟踪所有打开的 page wrapper
    - new_owned_page() 在 chrome 内 lazy 创建一个新 page
    - close_page(page) 关一个 page（幂等）
    - is_empty() 检测 chrome 是否还有 page
    - close() 关 chrome 进程（关所有 page + context_handle）

  与 R5/R6 的区别：
    - 删除 _pool / _inflight / _POISON / _page_factory / _supervise_lock
    - 删除 _release_page / _acquire_page / close_idle_pages
    - 删除 last_response / runtime_param（挪到 PageHandle 实例上）
    - 删除 fetch_json / signed_request 等快捷方法（挪到 PageHandle）

  唯一对外保留的同步方法：report_anti_crawl（由 PageHandle 代理调用）
═══════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════════
#  Anti-crawl sentinel exception（保留 R5/R6 协议）
# ────────────────────────────────────────────────────────────────────
#  Raised when a 200-OK response carries a platform-specific "soft-block"
#  marker (douyin verify_check, bilibili -352 etc).
#  scraper / PageHandle 检测到风控空响应 → 调 PageHandle.report_anti_crawl
#  → 委托给 BrowserSession.report_anti_crawl → 抛 AntiCrawlDetected →
#  daemon 错误归类为 FailureMode.ANTI_CRAWL → backoff base*2。
# ════════════════════════════════════════════════════════════════════


class AntiCrawlDetected(RuntimeError):
    """Platform returned a soft-block sentinel (e.g. douyin verify_check)."""

    def __init__(self, platform: str, signal: str, *, detail: str = "") -> None:
        self.platform = platform
        self.signal = signal
        self.detail = detail
        msg = f"[anti_crawl] platform={platform} signal={signal}"
        if detail:
            msg += f" detail={detail[:200]}"
        super().__init__(msg)


class BrowserSession:
    """Chrome process wrapper. Owns a set of pages and the context handle.

    R7 lifetime contract:
      - 由 BrowserSessionManager.acquire 触发 factory 创建
      - new_owned_page / close_page 由 BrowserSessionProvider.hold 调用
      - close 由 BrowserSessionManager.release（ref==0 时）或
        close_all_sessions（graceful_shutdown 时）调用——无第三个入口
    """

    def __init__(
        self,
        *,
        context_handle: Any,
        request_gate: Any | None = None,
        on_cookie_expired: Any | None = None,
    ) -> None:
        self._context_handle = context_handle
        self.request_gate = request_gate
        self.on_cookie_expired = on_cookie_expired
        self._owned_pages: set[Any] = set()
        self._lock = asyncio.Lock()
        self._closing = False
        self._unhealthy_reason: str | None = None

    # ════════════════════════════════════════════════════════════════
    #  公开 API：page 生命周期
    # ════════════════════════════════════════════════════════════════

    async def new_owned_page(self) -> Any:
        """Create a fresh page in this chrome; register in _owned_pages.

        Returns: PlaywrightPageWrapper（由 context_handle.new_page_wrapper 提供）
        Raises:
          RuntimeError if session is closing or unhealthy
        """
        async with self._lock:
            if self._closing:
                raise RuntimeError("BrowserSession is closing")
            if self._unhealthy_reason:
                raise RuntimeError(f"BrowserSession unhealthy: {self._unhealthy_reason}")
            wrapper = await self._context_handle.new_page_wrapper()
            self._owned_pages.add(wrapper)
            return wrapper

    async def close_page(self, page: Any) -> None:
        """Close one page; remove from owned set. Idempotent.

        N4 注释：锁外 await page.close() 是有意的——避免 chrome 关 page 慢
        拖死其它 new_owned_page 的并发。
        """
        async with self._lock:
            if page not in self._owned_pages:
                return
            self._owned_pages.discard(page)
        try:
            await page.close()
        except Exception as exc:
            logger.debug("page close swallow: %s", exc)

    def is_empty(self) -> bool:
        """Whether this chrome has no pages left (used by manager.release)."""
        return len(self._owned_pages) == 0

    # ════════════════════════════════════════════════════════════════
    #  健康状态（cookie expired 等）
    # ════════════════════════════════════════════════════════════════

    def mark_unhealthy(self, reason: str = "") -> None:
        """Mark this session unhealthy; new new_owned_page calls will fail.

        Existing pages keep working until hold ends; ref==0 then triggers close.
        """
        self._unhealthy_reason = reason or "unhealthy"

    # ════════════════════════════════════════════════════════════════
    #  关 chrome 进程（manager.release / close_all_sessions 唯一入口）
    # ════════════════════════════════════════════════════════════════

    async def close(self) -> None:
        """Close all pages, then close context_handle (context + playwright).

        Idempotent: 重复 close 安全 no-op。
        """
        if self._closing:
            return
        self._closing = True

        # 关所有 page（即使有 close_page 漏掉的）
        pages = list(self._owned_pages)
        self._owned_pages.clear()
        for page in pages:
            try:
                await page.close()
            except Exception as exc:
                logger.debug("session close: page close swallow: %s", exc)

        # 关 context_handle (context + playwright.stop)
        if self._context_handle is not None:
            try:
                await self._context_handle.close()
            except Exception as exc:
                logger.debug("session close: context close swallow: %s", exc)

    # ════════════════════════════════════════════════════════════════
    #  Anti-crawl 信号转发（同步入口，由 PageHandle.report_anti_crawl 代理）
    # ════════════════════════════════════════════════════════════════

    def report_anti_crawl(
        self,
        signal: str,
        *,
        platform: str = "",
        detail: str = "",
    ) -> None:
        """Raise AntiCrawlDetected. Daemon 异常归类走 FailureMode.ANTI_CRAWL.

        实现选择：只抛异常，不在这里 _report_failure——daemon 的异常处理
        路径是 cookie 健康的唯一真理源（同 R5 设计）。
        """
        logger.warning(
            "[BBA] anti_crawl.detected signal=%s platform=%s detail=%s",
            signal, platform or "<unknown>", detail[:200] if detail else "",
        )
        raise AntiCrawlDetected(
            platform=platform or "<unknown>",
            signal=signal,
            detail=detail,
        )

    # ════════════════════════════════════════════════════════════════
    #  Observable for tests
    # ════════════════════════════════════════════════════════════════

    @property
    def owned_pages_count(self) -> int:
        return len(self._owned_pages)
