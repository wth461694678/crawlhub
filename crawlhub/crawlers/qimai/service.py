"""Qimai (App Store analytics) platform service — pure dispatch glue (R3 + R4)."""

from __future__ import annotations

from pathlib import Path

from crawlhub.core.config import get_data_root
from crawlhub.core.platform.runtime_service import RuntimeAwareService
from crawlhub.core.platform.probe_protocol import ProbeResult
from crawlhub.core.registry import CookieStatus

from .crawler.scraper import QimaiScraper


class QimaiService(RuntimeAwareService):
    """Qimai platform service — dispatches App Store rank/search actions."""

    def _make_scraper(self) -> QimaiScraper:
        cookie_path = get_data_root() / "cookies" / "qimai.json"
        return QimaiScraper(
            cookie_path=str(cookie_path) if cookie_path.exists() else None,
        )

    # ---- R4-P13 hooks (BaseService.check_cookie template) -----------

    def _check_missing(self) -> CookieStatus | None:
        cookie_path: Path | None = (
            Path(self.scraper._cookie_path) if self.scraper._cookie_path else None
        )
        if cookie_path is None or not cookie_path.exists():
            try:
                cookie_path = self.scraper.resolve_cookie_path()
            except Exception:  # noqa: BLE001
                cookie_path = None
        if cookie_path is None or not cookie_path.exists():
            return CookieStatus(status="missing", message="No qimai cookie file found.")
        return None

    def _format_valid_message(self, result: ProbeResult) -> str:
        userinfo = getattr(self.scraper.client, "userinfo", None) or {}
        uname = userinfo.get("username") if isinstance(userinfo, dict) else None
        return f"Logged in: {uname}" if uname else super()._format_valid_message(result)
