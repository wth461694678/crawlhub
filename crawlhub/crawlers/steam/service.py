"""Steam platform service — pure dispatch glue (R3 / R4)."""

from __future__ import annotations

from crawlhub.core.platform import BaseService
from crawlhub.core.registry import CookieStatus

from .crawler.scraper import SteamScraper


class SteamService(BaseService):
    """Steam platform service — extends BaseService.

    Steam scraping works without login for the read-only public APIs we
    use, so ``check_cookie`` reports ``valid`` whenever a cookie file is
    available *or* falls back to ``missing`` with an informational
    message instead of raising. We deliberately do NOT call ``probe()``
    here because that would block task scheduling on a live network ping.
    """

    def _make_scraper(self) -> SteamScraper:
        return SteamScraper()

    def check_cookie(self) -> CookieStatus:
        from crawlhub.core.cookies import get_cookie_store

        store = get_cookie_store()
        cookie_path = store.get_first_cookie_path("steam")
        if cookie_path and cookie_path.exists():
            return CookieStatus(status="valid", message="Cookie file exists. Steam scraping available.")

        cookie_path = self.scraper.client.cookie_path
        if cookie_path and cookie_path.exists():
            return CookieStatus(status="valid", message="Cookie file exists. Steam scraping available.")

        return CookieStatus(status="missing", message="No cookie file. Steam works without login.")
