"""Weibo platform service — pure dispatch glue (R3 + R4)."""

from __future__ import annotations

from crawlhub.core.platform import BaseService
from crawlhub.core.registry import CookieStatus

from .crawler.scraper import WeiboScraper


class WeiboService(BaseService):
    """Weibo platform service — search/user-info/comments/user-posts."""

    def _make_scraper(self) -> WeiboScraper:
        return WeiboScraper()

    # ---- R4-P13 hooks (BaseService.check_cookie template) -----------

    def _check_missing(self) -> CookieStatus | None:
        try:
            self.scraper.check_cookie_valid()
        except Exception as e:  # noqa: BLE001
            return CookieStatus(status="missing", message=str(e))
        return None
