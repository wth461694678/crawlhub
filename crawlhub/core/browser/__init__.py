"""Browser-backed action runtime primitives (R7)."""

from crawlhub.core.browser.cookie_injection import load_storage_state
from crawlhub.core.browser.handle import PageHandle
from crawlhub.core.browser.provider import BrowserSessionProvider
from crawlhub.core.browser.session import AntiCrawlDetected, BrowserSession
from crawlhub.core.browser.session_key import SessionKey
from crawlhub.core.browser.session_manager import BrowserSessionManager, BrowserSessionState

__all__ = [
    "AntiCrawlDetected",
    "BrowserSession",
    "BrowserSessionProvider",
    "BrowserSessionManager",
    "BrowserSessionState",
    "PageHandle",
    "SessionKey",
    "load_storage_state",
]
# R7: BrowserSessionHandle 已删除（R5 setattr/handle 路径被 hold/PageHandle 取代）
