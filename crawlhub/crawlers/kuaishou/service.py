"""Kuaishou platform service — pure dispatch glue (R3 + R4)."""

from __future__ import annotations

from crawlhub.core.platform.runtime_service import RuntimeAwareService
from crawlhub.core.registry import CookieStatus

from .crawler.client import KuaishouSDK
from .crawler.scraper import KuaishouScraper


class KuaishouService(RuntimeAwareService):
    """Kuaishou platform service — search, video detail, comment scraping."""

    # 快手 websig4 SDK 检测 --disable-extensions 等自动化指纹，
    # 导致 QR 登录失败（channelType='UNKNOWN'），BBA 登录必须跳过 stealth。
    # 已改为全量 stealth 模式（2026-06-05）。
    bba_skip_stealth: bool = False

    def _make_scraper(self) -> KuaishouScraper:
        return KuaishouScraper()

    # ---- R4-P13 hooks (BaseService.check_cookie template) -----------

    def _check_missing(self) -> CookieStatus | None:
        try:
            self.scraper.check_cookie_valid()
        except Exception as e:  # noqa: BLE001
            return CookieStatus(status="missing", message=str(e))
        return None

    def _build_probe_client(self) -> KuaishouSDK:
        return KuaishouSDK(
            cookie_path=str(self.scraper.resolve_cookie_path()),
            log_prefix="ks_probe",
        )
