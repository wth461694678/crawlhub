"""Singleflight browser-session manager (R7).

═══════════════════════════════════════════════════════════════════════
  R7 简化（spec R7-browser-page-action-lifecycle.md §8.3）
─────────────────────────────────────────────────────────────────────
  BrowserSessionManager 是 chrome 进程的 lifecycle 真理源：
    - SessionKey → BrowserSession 复用（singleflight 创建）
    - ref_count 仅作并发锁（防 release 与新 acquire race）
    - release(key) 是关 chrome 的唯一调用点（除 close_all_sessions）

  状态机简化到 4 态（删 R5 的 DRAINING/CLOSING）：
    CREATING → HEALTHY → CLOSED
                 │
                 └→ UNHEALTHY → CLOSED（cookie_expired 等）

  R7 删除：
    - expire_old_sessions / _is_expired（无 reaper）
    - alive_sessions（无 page 级回收逻辑）
    - DRAINING / CLOSING 中间态
    - max_session_concurrency 配置（同 SessionKey 强制 singleflight）
    - BrowserSessionLease 类（不再有 lease 概念）
═══════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any

from crawlhub.core.browser.session_key import SessionKey
from crawlhub.core.plugin_manifest import BrowserConfig

logger = logging.getLogger(__name__)


class BrowserSessionState(str, Enum):
    """R7: 4 态精简。"""
    CREATING = "creating"
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"  # cookie expired 等；新 acquire 失败，ref==0 后关
    CLOSED = "closed"


@dataclass
class _SessionEntry:
    session: Any | None
    state: BrowserSessionState
    ref_count: int = 0  # R7: 并发锁，非超时计数


class BrowserSessionManager:
    """Singleflight chrome creation + ref_count concurrency lock.

    R7 contract:
      - acquire(key) returns BrowserSession (not Lease wrapper)
      - release(key) decrements ref_count; if ref==0, closes chrome + removes entry
      - close_all_sessions() called only by daemon graceful_shutdown
    """

    def __init__(
        self,
        *,
        factory: Callable[[SessionKey], Awaitable[Any] | Any],
        config: BrowserConfig | None = None,
    ) -> None:
        self._factory = factory
        self._config = config or BrowserConfig()
        self._lock = asyncio.Lock()
        self._sessions: dict[SessionKey, _SessionEntry] = {}
        self._creating: dict[SessionKey, asyncio.Task[_SessionEntry]] = {}
        self._shutdown = False

    # ════════════════════════════════════════════════════════════════
    #  公开 API
    # ════════════════════════════════════════════════════════════════

    async def acquire(
        self,
        key: SessionKey,
        *,
        timeout: float = 60.0,
    ) -> Any:
        """Singleflight create or reuse; ref_count += 1 atomically.

        Returns: BrowserSession (raw, scraper 通过 provider.hold 拿)
        Raises:
          RuntimeError if manager is shutting down or session unhealthy
          TimeoutError if chrome 冷启动 > timeout（默认 60s）
        """
        # 锁内：要么命中 HEALTHY entry 直接 ref++，要么启动/复用 singleflight task
        async with self._lock:
            if self._shutdown:
                raise RuntimeError("BrowserSessionManager is shutting down")
            current = self._sessions.get(key)
            if current is not None and current.state == BrowserSessionState.HEALTHY:
                current.ref_count += 1
                return current.session
            task = self._creating.get(key)
            if task is None:
                task = asyncio.create_task(self._create_entry(key))
                self._creating[key] = task

        # 锁外：等 future（超时控制）
        try:
            entry = await asyncio.wait_for(task, timeout=timeout)
        except TimeoutError:
            task.cancel()
            async with self._lock:
                self._creating.pop(key, None)
            raise

        # 锁内：ref++ 原子操作
        async with self._lock:
            if entry.state != BrowserSessionState.HEALTHY:
                raise RuntimeError(f"Session unhealthy: {entry.state}")
            entry.ref_count += 1
            return entry.session

    async def release(self, key: SessionKey) -> None:
        """ref_count -= 1; if ref==0 → close chrome + remove from registry.

        关 chrome 的唯一调用点（除 close_all_sessions 外）。
        """
        entry_to_close: _SessionEntry | None = None
        async with self._lock:
            entry = self._sessions.get(key)
            if entry is None:
                return
            entry.ref_count = max(0, entry.ref_count - 1)
            if entry.ref_count == 0:
                # 从 registry 移除，下一个 acquire 必触发重建
                del self._sessions[key]
                entry_to_close = entry

        if entry_to_close is not None:
            # 锁外 close（chrome 关闭慢不应拖累其它 SessionKey）
            try:
                await entry_to_close.session.close()
            except Exception as exc:
                logger.warning("[BBA] session close exc=%s", exc)
            entry_to_close.state = BrowserSessionState.CLOSED

    async def mark_unhealthy(
        self,
        key: SessionKey,
        *,
        reason: str = "",
    ) -> None:
        """Cookie expired 等触发。新 acquire 会失败；已有 ref 用完 release 时关。

        不立即 close——让现有 hold 走完自然 release。
        当最后一个 release 把 ref 减到 0 时，会触发 close（同正常路径）。

        但是 release 路径只对 HEALTHY entry 移除并 close。UNHEALTHY entry
        需要 release 内特殊处理：UNHEALTHY + ref==0 也要触发 close。
        """
        async with self._lock:
            entry = self._sessions.get(key)
            if entry is None or entry.state == BrowserSessionState.CLOSED:
                return
            entry.state = BrowserSessionState.UNHEALTHY
            marker = getattr(entry.session, "mark_unhealthy", None)
            if callable(marker):
                marker(reason)
            # 如果当前已经 ref==0，立刻关
            if entry.ref_count == 0:
                del self._sessions[key]
                entry_to_close = entry
            else:
                entry_to_close = None

        if entry_to_close is not None:
            try:
                await entry_to_close.session.close()
            except Exception as exc:
                logger.warning("[BBA] unhealthy session close exc=%s", exc)
            entry_to_close.state = BrowserSessionState.CLOSED

    async def close_all_sessions(self) -> None:
        """Called only by daemon graceful_shutdown.

        直接关所有 chrome，不等 ref（in-hold task 会抛 TargetClosedError，
        failure_detector 在 _shutdown_flag 期间会识别为 NETWORK_ERROR）。
        """
        async with self._lock:
            self._shutdown = True
            for task in self._creating.values():
                task.cancel()
            self._creating.clear()
            to_close = list(self._sessions.items())
            self._sessions.clear()

        for key, entry in to_close:
            try:
                await entry.session.close()
            except Exception as exc:
                logger.warning("[BBA] shutdown close exc=%s key=%s", exc, key)
            entry.state = BrowserSessionState.CLOSED

    # ════════════════════════════════════════════════════════════════
    #  Observability
    # ════════════════════════════════════════════════════════════════

    def ref_count(self, key: SessionKey) -> int:
        entry = self._sessions.get(key)
        return 0 if entry is None else entry.ref_count

    def state(self, key: SessionKey) -> BrowserSessionState | None:
        entry = self._sessions.get(key)
        return None if entry is None else entry.state

    def is_creating(self, key: SessionKey) -> bool:
        return key in self._creating

    def session_count(self) -> int:
        return len(self._sessions)

    # ════════════════════════════════════════════════════════════════
    #  Internal
    # ════════════════════════════════════════════════════════════════

    async def _create_entry(self, key: SessionKey) -> _SessionEntry:
        entry = _SessionEntry(session=None, state=BrowserSessionState.CREATING)
        try:
            raw = self._factory(key)
            session = await raw if asyncio.iscoroutine(raw) else raw
            entry = _SessionEntry(
                session=session,
                state=BrowserSessionState.HEALTHY,
                ref_count=0,
            )
            async with self._lock:
                self._sessions[key] = entry
            return entry
        except BaseException:
            entry.state = BrowserSessionState.CLOSED
            raise
        finally:
            async with self._lock:
                if self._creating.get(key) is asyncio.current_task():
                    self._creating.pop(key, None)
