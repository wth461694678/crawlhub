"""crawlhub.core.platform.base_client — BaseHttpClient (R4 plan §2.1).

Hard design principle: **protocol over implementation**. The base class
installs response capture through common session protocols while subclasses
remain free to allocate any HTTP-capable session inside ``_setup_sessions``
(single session, multi-session, anything). All the base class promises is:


  * a cookie_jar slot (``self._cookie_jar``)
  * a ``is_logged_in`` view that reflects the cookie_jar
  * ``probe()`` is enforced by the ``CapabilityProbe`` Protocol
  * ``_setup_sessions()`` is called exactly once, at construction time

Subclasses MUST implement ``_setup_sessions`` and ``probe``. Everything
else is shared retry / log / exception-translation helpers.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from contextvars import ContextVar
from typing import Any

from crawlhub.core.task_context import TaskContext

from .cookie_jar import CookieJar
from .probe_protocol import CapabilityProbe, ProbeResult

logger = logging.getLogger(__name__)

CURRENT_TASK_CONTEXT: ContextVar[TaskContext | None] = ContextVar(
    "crawlhub_current_task_context",
    default=None,
)

_SIMPLE_TYPES = (str, bytes, bytearray, int, float, bool, type(None))


def bind_task_context_to_object(
    obj: Any,
    ctx: TaskContext,
    seen: set[int] | None = None,
    depth: int = 2,
) -> None:
    if obj is None or isinstance(obj, _SIMPLE_TYPES) or depth < 0:
        return
    if seen is None:
        seen = set()
    obj_id = id(obj)
    if obj_id in seen:
        return
    seen.add(obj_id)

    binder = getattr(obj, "bind_task_context", None)
    if callable(binder):
        try:
            binder(ctx)
        except Exception:
            logger.debug("response capture bind failed on %r", obj, exc_info=True)
        return

    observer_setter = getattr(obj, "set_response_observer", None)
    if callable(observer_setter):
        def _observer(response: Any, *args: Any, **kwargs: Any) -> Any:
            try:
                ctx.set_last_response(response)
            except Exception:
                logger.debug("response observer failed", exc_info=True)
            return response
        try:
            observer_setter(_observer)
        except Exception:
            logger.debug("response observer install failed on %r", obj, exc_info=True)
        return

    values: list[Any] = []
    try:
        values.extend(vars(obj).values())
    except TypeError:
        return
    for value in values:
        if isinstance(value, _SIMPLE_TYPES):
            continue
        if isinstance(value, (list, tuple, set, dict)):
            continue
        bind_task_context_to_object(value, ctx, seen, depth - 1)


class BaseHttpClient(ABC, CapabilityProbe):
    """Base class for every platform's HTTP client layer.

    The ``CapabilityProbe`` mixin makes ``probe()`` a hard requirement —
    forgetting it triggers a ``TypeError`` at instantiation time, which
    is exactly what we want during R4 migration.
    """

    def __init__(self, cookie_jar: CookieJar | None = None) -> None:
        self._cookie_jar: CookieJar | None = cookie_jar
        self._task_context: TaskContext | None = None
        self._response_hook_installed: set[int] = set()
        self._setup_sessions()
        ctx = CURRENT_TASK_CONTEXT.get()
        if ctx is not None:
            self.bind_task_context(ctx)

    def bind_task_context(self, ctx: TaskContext) -> None:
        self._task_context = ctx
        self._install_response_capture_hooks()

    def _capture_response(self, response: Any, *args: Any, **kwargs: Any) -> Any:
        ctx = self._task_context or CURRENT_TASK_CONTEXT.get()
        if ctx is not None:
            try:
                ctx.set_last_response(response)
            except Exception:
                logger.debug("response capture failed", exc_info=True)
        return response

    def _install_response_capture_hooks(self) -> None:
        for session in self._iter_response_sources():
            self._install_response_capture_hook(session)

    def _iter_response_sources(self):
        for attr in ("session", "_session", "api_session", "search_session", "_client"):
            source = getattr(self, attr, None)
            if source is not None:
                yield source

    def _install_response_capture_hook(self, session: Any) -> None:
        session_id = id(session)
        if session_id in self._response_hook_installed:
            return

        hooks = getattr(session, "hooks", None)
        if isinstance(hooks, dict):
            existing = hooks.get("response") or []
            if not isinstance(existing, list):
                existing = [existing]
            if not any(getattr(h, "__crawlhub_response_capture__", False) for h in existing):
                def _requests_hook(response: Any, *args: Any, **kwargs: Any) -> Any:
                    return self._capture_response(response, *args, **kwargs)
                _requests_hook.__crawlhub_response_capture__ = True  # type: ignore[attr-defined]
                existing.append(_requests_hook)
                hooks["response"] = existing
            self._response_hook_installed.add(session_id)
            return

        event_hooks = getattr(session, "event_hooks", None)
        if isinstance(event_hooks, dict):
            existing = event_hooks.get("response") or []
            if not isinstance(existing, list):
                existing = [existing]
            if not any(getattr(h, "__crawlhub_response_capture__", False) for h in existing):
                def _httpx_hook(response: Any) -> Any:
                    return self._capture_response(response)
                _httpx_hook.__crawlhub_response_capture__ = True  # type: ignore[attr-defined]
                existing.append(_httpx_hook)
                event_hooks["response"] = existing
            self._response_hook_installed.add(session_id)
            return

        observer_setter = getattr(session, "set_response_observer", None)
        if callable(observer_setter):
            observer_setter(self._capture_response)
            self._response_hook_installed.add(session_id)

    # ---- subclass contract -------------------------------------------

    @abstractmethod
    def _setup_sessions(self) -> None:
        """Allocate HTTP session(s). Called exactly once during ``__init__``.

        Subclasses are free to attach any number of sessions:
            self._session = requests.Session()
            self._api_session, self._ssr_session = ...
        The base class installs response capture for common session protocols.
        """

    @abstractmethod
    def probe(self, task_type: str = "default") -> ProbeResult:
        """Probe whether the configured cookie/credential is usable.

        Must return a :class:`ProbeResult` with all five fields set.
        """

    # ---- shared helpers ----------------------------------------------

    @property
    def is_logged_in(self) -> bool:
        """True iff a cookie_jar is attached *and* it reports logged-in."""
        if self._cookie_jar is None:
            return False
        return self._cookie_jar.is_logged_in()

    @property
    def cookie_jar(self) -> CookieJar | None:
        """Read-only view onto the attached cookie_jar."""
        return self._cookie_jar

    def _log_request(self, method: str, url: str, **kwargs: Any) -> None:
        """Optional debug helper — subclasses may call this from inside
        their request methods to keep logging shape consistent across
        platforms."""
        logger.debug("%s %s kwargs=%s", method, url, kwargs)
