"""Internal helpers for the Douyin crawler.

This package contains modules that are not part of the public API
and may change without notice.

R3 / R4 contract: ``service.py`` and any sibling ``bridge.py`` MUST NOT
import from ``_internal/*`` (enforced by C4 / C5 / C16 in
``tests/test_platform_conformance.py``). Re-export anything that needs
to cross the boundary via ``crawler/__init__.py``.

Modules:
    abogus              - Pure Python a_bogus signer (no browser/Node required)
    browser_bridge      - Playwright browser bridge for search
    cookie_jar          - DouyinCookieJar (pure data layer, R4 R5)
    health_tracker      - HealthTracker (consecutive-failure counter, R4 R5)
    refresh_orchestrator- RefreshOrchestrator (silent/interactive/ttwid, R4 R5)
"""

from .abogus import ABogus
from .browser_bridge import BrowserBridge
from .cookie_jar import DouyinCookieJar
from .health_tracker import HealthTracker
from .refresh_orchestrator import RefreshOrchestrator

__all__ = [
    "ABogus",
    "BrowserBridge",
    "DouyinCookieJar",
    "HealthTracker",
    "RefreshOrchestrator",
]
