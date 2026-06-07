"""Per-task provider: contextmanager-style hold (R7).

═══════════════════════════════════════════════════════════════════════
  BrowserSessionProvider —— action 拿 page 的唯一入口（spec §5.3 + §8.4）
─────────────────────────────────────────────────────────────────────
  hold() 是 R7 的核心 API：

      with self.runtime.browser.hold() as page:
          page.evaluate(...)
          page.fetch_json(...)
      # 退出 with → page 立刻关 + release → ref==0 时关 chrome

  hold 与 chrome 关闭的关系：
    - 进入：manager.acquire(key) → ref_count += 1（首次创建 chrome 或复用）
    - 创建 page：session.new_owned_page() lazy new
    - 注册：把 PageHandle 加进 owned_pages set，让 daemon finally 能兜底
    - 退出：session.close_page(raw_page) + manager.release(key)
      - release 内部：ref_count -= 1；若归 0 则关 chrome

  允许嵌套 hold（同 task 拿多个 page）；ref_count 累加，最后一个 release 时关 chrome。

  R7 删除：
    - hold_manually（R7 强制 with 语法）
    - lease() 方法（R7 page 已 task 独占，无需操作权独占）
    - _active 防嵌套约束
═══════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from crawlhub.core.browser.handle import PageHandle

logger = logging.getLogger(__name__)


class BrowserSessionProvider:
    """Context-manager facade for per-task browser hold.

    Per-task instance, owned by RuntimeServices.
    Both manager and runner are injected by daemon; provider has no ownership.
    """

    def __init__(
        self,
        *,
        manager: Any,
        runner: Any,
        key: Any,
        owned_pages: set[PageHandle],
        cancel_event: Any | None = None,
    ) -> None:
        self._manager = manager
        self._runner = runner
        self._key = key
        self._owned_pages_ref = owned_pages   # RuntimeServices.owned_pages
        self._cancel_event = cancel_event

    @contextmanager
    def hold(self) -> Iterator[PageHandle]:
        """Acquire a chrome session (singleflight) + lazy create a page.

        On exit:
          1. close the page (session.close_page → idempotent)
          2. release ref (manager.release → ref==0 closes chrome)

        Nested hold is allowed; each yields a distinct PageHandle.
        """
        # 1. acquire chrome（ref_count += 1）
        session = self._runner.run(
            self._manager.acquire(self._key, timeout=60.0),
            cancel_event=self._cancel_event,
        )
        page: PageHandle | None = None
        raw_page = None
        try:
            # 2. lazy new_page
            raw_page = self._runner.run(session.new_owned_page())
            page = PageHandle(
                runner=self._runner,
                session=session,
                raw_page=raw_page,
            )

            # ╔══════════════════════════════════════════════════════════╗
            # ║  R7 Observability — CDP Recorder attach (spec §3.3)      ║
            # ║  ctx 在 daemon worker 主线程已 set，闭包传入 callback；  ║
            # ║  attach 跑在 BrowserAsyncRunner loop 里（async fn）。     ║
            # ║  失败 silent（patchright stealth 模式可能拒绝 CDP）。     ║
            # ╚══════════════════════════════════════════════════════════╝
            try:
                from crawlhub.core.observability import cdp_recorder
                from crawlhub.core.platform.base_client import CURRENT_TASK_CONTEXT
                ctx = CURRENT_TASK_CONTEXT.get()
                if ctx is not None:
                    logger.debug("[obs.cdp] hold() invoking attach for task=%s", ctx.task_id)
                    self._runner.run(cdp_recorder.attach(raw_page, ctx))
                else:
                    logger.warning("[obs.cdp] hold() ctx is None — CDP attach skipped")
            except Exception as exc:
                # observability 自身崩绝不影响业务
                logger.warning("[obs.cdp] attach skipped: %s", exc)
            # ───────────────────────────────────────────────────────────

            # 3. 注册到 task owned_pages set
            self._owned_pages_ref.add(page)
            yield page
        finally:
            # 4. 关 page（即使 yield 抛异常）
            if raw_page is not None:
                try:
                    self._runner.run(session.close_page(raw_page))
                except Exception as exc:
                    logger.warning("[BBA] hold exit close_page failed: %s", exc)
            if page is not None:
                page._mark_closed()
                self._owned_pages_ref.discard(page)
            # 5. release chrome（ref==0 → 关 chrome）
            try:
                self._runner.run(self._manager.release(self._key))
            except Exception as exc:
                logger.warning("[BBA] hold exit release failed: %s", exc)
