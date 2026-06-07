"""Bilibili platform service — pure dispatch glue (R3 + R4)."""

from __future__ import annotations

from crawlhub.core.platform import BaseService, FileCookieJar
from crawlhub.core.platform.probe_protocol import ProbeResult
from crawlhub.core.registry import CookieStatus

from .crawler.client import BilibiliClient
from .crawler.scraper import BilibiliScraper


class BilibiliService(BaseService):
    """Bilibili platform service — search videos, get details, scrape comments."""

    def _make_scraper(self) -> BilibiliScraper:
        return BilibiliScraper()

    # ---- R4-P13 hooks (BaseService.check_cookie template) -----------

    def _check_missing(self) -> CookieStatus | None:
        if self.scraper.resolve_cookie_path().exists():
            return None
        return CookieStatus(status="missing", message="No cookie file found. Please login first.")

    def _build_probe_client(self) -> BilibiliClient:
        return BilibiliClient(cookie_jar=FileCookieJar(self.scraper.resolve_cookie_path()))

    def _format_valid_message(self, result: ProbeResult) -> str:
        uname = (result.extras or {}).get("uname") or ""
        return f"Logged in as: {uname}" if uname else super()._format_valid_message(result)
