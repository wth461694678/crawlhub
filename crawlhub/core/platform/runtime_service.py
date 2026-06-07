"""Runtime service helpers for actions that need controlled platform resources."""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from crawlhub.core.task_context import TaskContext

from .base_client import CURRENT_TASK_CONTEXT, bind_task_context_to_object
from .base_service import BaseService


@dataclass
class RuntimeServices:
    """Resources injected by the daemon for advanced platform actions.

    R7: 增加 owned_pages set 字段，用于 daemon finally 兜底关泄漏的 PageHandle.
    去掉 frozen=True 以允许 set 字段（mutable）.
    """

    browser: Any | None = None
    cookie_id: str | None = None
    cookie_path: str = ""
    transport: str = "http"
    # R7: hold 进入时 add PageHandle，退出时 discard；daemon finally 兜底关残留
    owned_pages: set = field(default_factory=set)


CURRENT_RUNTIME_CONTEXT: ContextVar[RuntimeServices | None] = ContextVar(
    "CURRENT_RUNTIME_CONTEXT",
    default=None,
)


def get_current_runtime() -> RuntimeServices | None:
    """Return the RuntimeServices bound to the current task execution."""

    return CURRENT_RUNTIME_CONTEXT.get()


class RuntimeAwareService(BaseService):
    """Base service for actions that receive RuntimeServices from the daemon."""

    def execute_with_runtime(
        self,
        action: str,
        params: dict[str, Any],
        ctx: TaskContext,
        runtime: RuntimeServices,
    ) -> None:
        scraper = self.scraper
        handler = getattr(scraper, action, None)
        if handler is None or not callable(handler):
            raise ValueError(
                f"Unknown action '{action}' on platform '{self.platform_name()}'. "
                f"Scraper has no callable attribute named '{action}'."
            )
        ctx_token = CURRENT_TASK_CONTEXT.set(ctx)
        runtime_token = CURRENT_RUNTIME_CONTEXT.set(runtime)
        try:
            bind_task_context_to_object(scraper, ctx)
            handler(ctx, params)
        finally:
            CURRENT_RUNTIME_CONTEXT.reset(runtime_token)
            CURRENT_TASK_CONTEXT.reset(ctx_token)
