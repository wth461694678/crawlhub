"""{{platform_display}} platform service — thin dispatch shell (R4).

Inherits :class:`crawlhub.core.platform.base_service.BaseService`, so
``execute()`` (action dispatch) and ``check_cookie()`` (probe -> status
translation) are provided for free. The only thing this file owns is
which scraper class to instantiate, plus optional cookie-status hooks.

Hard rules (enforced by ``tests/test_platform_conformance.py``):
    * MUST NOT import from ``.crawler._internal.*``  (C4)
    * MUST NOT call ``ctx.write_record`` here        (C7)
    * MUST NOT branch on action name with ``if/elif``;
      ``BaseService.execute`` already does ``getattr`` dispatch (C12)
    * Body should stay around 10-30 lines — extra logic belongs in scraper.py.
"""

from __future__ import annotations

from crawlhub.core.platform.base_service import BaseService
from crawlhub.core.registry import CookieStatus

from .crawler import {{scraper_class}}


class {{service_class}}(BaseService):
    """Service for the ``{{platform_name}}`` platform."""

    def _make_scraper(self) -> {{scraper_class}}:
        return {{scraper_class}}()

    # ---- R4-P13 cookie-status hooks (override as needed) -----------------
    # BaseService.check_cookie runs:  _check_missing -> _build_probe_client
    # -> client.probe() -> _translate_probe.  Defaults work for most
    # platforms; uncomment / adjust below only if your platform diverges.

    def _check_missing(self) -> CookieStatus | None:
        """Return a 'missing' status when no cookie file exists, else None.

        Default implementation here defers to scraper.check_cookie_valid()
        which the scaffolded scraper template implements via
        :class:`CookieResolverMixin`. Remove this override entirely if you
        do not want a missing-state preflight.
        """
        try:
            self.scraper.check_cookie_valid()
        except Exception as e:  # noqa: BLE001
            return CookieStatus(status="missing", message=str(e))
        return None

    # def _build_probe_client(self):
    #     """Return an object exposing ``probe(task_type=None)``.
    #
    #     Default (in BaseService) returns ``self.scraper.client`` — fine
    #     when the scraper exposes its client publicly. Override only if
    #     you need a dedicated probe client bound to a resolved cookie path.
    #     """
    #     return self.scraper.client
