"""crawlhub.core.platform.base_service — BaseService (R4 plan §2.2).

Wraps ``BasePlatformService`` (the legacy registry-side base) so that
each real platform's ``service.py`` shrinks to ~30 lines:

    class FooService(BaseService):
        def _make_scraper(self) -> FooScraper:
            return FooScraper()

Three responsibilities live here so subclasses don't repeat them:

  * ``execute(action, params, ctx)`` — dispatches to ``scraper.<action>``
    via ``getattr`` (matches R3 / C12 form). Unknown action -> ValueError.
  * ``check_cookie()`` — full template method orchestrating the unified
    R4-P13 probe path: missing-preflight -> build probe client ->
    ``client.probe()`` -> translate :class:`ProbeResult` to
    :class:`CookieStatus`. Subclasses customise via three small hooks
    (``_check_missing`` / ``_build_probe_client`` / ``_format_valid_message``).

Note on CookieStatus shape: R4 plan §2.2 sketched ``CookieStatus(valid=...)``
but the *real* registry shape is ``CookieStatus(status="valid"|"expired"|"missing", message=...)``.
We honour the real shape so this layer stays drop-in compatible with
the 6 already-deployed platforms.
"""
from __future__ import annotations

import logging
from typing import Any

from crawlhub.core.registry import BasePlatformService, CookieStatus
from crawlhub.core.task_context import TaskContext

from .base_client import CURRENT_TASK_CONTEXT, bind_task_context_to_object
from .probe_protocol import ProbeResult

logger = logging.getLogger(__name__)


class BaseService(BasePlatformService):
    """Thin shell every platform service should extend.

    Subclasses implement only ``_make_scraper``; everything else is
    shared. The scraper instance is created lazily on first access.
    """

    def __init__(self, manifest: Any | None = None) -> None:
        super().__init__(manifest=manifest)
        self._scraper: Any | None = None

    # ---- subclass contract -------------------------------------------

    def _make_scraper(self) -> Any:
        """Construct and return the platform's scraper instance.

        Subclasses must override.
        """
        raise NotImplementedError("Subclasses must implement _make_scraper()")

    # ---- shared behaviour --------------------------------------------

    @property
    def scraper(self) -> Any:
        """Lazy scraper accessor — builds on first use, then caches."""
        if self._scraper is None:
            self._scraper = self._make_scraper()
        return self._scraper

    def execute(self, action: str, params: dict[str, Any], ctx: TaskContext) -> None:
        """Dispatch ``action`` to ``self.scraper.<action>(ctx, params)``.

        Unknown actions raise ``ValueError`` with the action name in the
        message — matches the R3 / C12 conformance contract. Validation
        of declared actions (manifest membership) is intentionally NOT
        done here because some platforms register synthetic actions at
        runtime; the registry-level dispatcher already gates declared
        actions.
        """
        scraper = self.scraper
        handler = getattr(scraper, action, None)
        if handler is None or not callable(handler):
            raise ValueError(
                f"Unknown action '{action}' on platform '{self.platform_name()}'. "
                f"Scraper has no callable attribute named '{action}'."
            )
        token = CURRENT_TASK_CONTEXT.set(ctx)
        try:
            bind_task_context_to_object(scraper, ctx)
            handler(ctx, params)
        finally:
            CURRENT_TASK_CONTEXT.reset(token)

    def check_cookie(self) -> CookieStatus:
        """Three-state cookie probe — unified template (R4-P13).

        Flow (each step swappable via a hook):

        1. ``_check_missing()`` — return a ``CookieStatus("missing", ...)``
           if the cookie is structurally absent; return ``None`` to skip
           the preflight (default).
        2. ``_build_probe_client()`` — return any object exposing
           ``probe(task_type)``. Default: ``self.scraper.client``.
        3. ``client.probe()`` is invoked and the result is translated to
           ``CookieStatus`` via ``_translate_probe`` /
           ``_format_valid_message``.
        4. Probe-time exceptions degrade to ``status="expired"`` so the
           dashboard surfaces a clear actionable verdict.

        Subclasses override only the hooks they need; the orchestration
        stays consistent across all 6 platforms (zero technical debt).
        """
        missing = self._check_missing()
        if missing is not None:
            return missing
        try:
            client = self._build_probe_client()
            result: ProbeResult = client.probe()
        except Exception as e:  # noqa: BLE001
            logger.debug(
                "[%s] cookie probe raised: %s", self.platform_name(), e
            )
            return CookieStatus(status="expired", message=f"Cookie probe failed: {e}")
        return self._translate_probe(result)

    # ---- check_cookie hooks (override as needed) ---------------------

    def _check_missing(self) -> CookieStatus | None:
        """Return a missing-state ``CookieStatus`` or ``None`` to skip.

        Default: no missing-state preflight. Platforms that want to
        distinguish "no cookie file" from "session expired" override
        this hook.
        """
        return None

    def _build_probe_client(self) -> Any:
        """Return an object exposing ``probe(task_type)``.

        Default: ``self.scraper.client`` — fits weibo / qimai whose
        scrapers expose the underlying client directly. Platforms whose
        scraper hides the client (bilibili / douyin / kuaishou) override
        this to construct a dedicated probe client bound to the resolved
        cookie path.
        """
        return self.scraper.client

    def _format_valid_message(self, result: ProbeResult) -> str:
        """Compose the user-facing message for a successful probe.

        Default: ``"OK (Nms)"``. Override to surface platform-specific
        details (e.g. logged-in username from ``result.extras``).
        """
        return f"OK ({result.latency_ms}ms)"

    def _translate_probe(self, result: ProbeResult) -> CookieStatus:
        if result.ok:
            return CookieStatus(
                status="valid",
                message=self._format_valid_message(result),
            )
        return CookieStatus(
            status="expired",
            message=result.error or "probe failed",
        )
