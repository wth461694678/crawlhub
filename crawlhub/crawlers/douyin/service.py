"""Douyin platform service — pure dispatch glue (R3 + R4)."""

from __future__ import annotations

from crawlhub.core.platform.runtime_service import RuntimeAwareService
from crawlhub.core.registry import CookieStatus

from .crawler.client import DouyinSDK
from .crawler.scraper import DouyinScraper


class DouyinService(RuntimeAwareService):
    """Douyin platform service — search, video detail, comment scraping."""

    def _make_scraper(self) -> DouyinScraper:
        return DouyinScraper()

    # ---- R4-P13 hooks (BaseService.check_cookie template) -----------

    def _check_missing(self) -> CookieStatus | None:
        try:
            self.scraper.check_cookie_valid()
        except Exception as e:  # noqa: BLE001
            return CookieStatus(status="missing", message=str(e))
        return None

    def _build_probe_client(self) -> DouyinSDK:
        return DouyinSDK(
            cookie_path=str(self.scraper.resolve_cookie_path()),
            log_prefix="dy_probe",
        )
