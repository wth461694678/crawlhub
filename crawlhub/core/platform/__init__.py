"""crawlhub.core.platform — runtime base layer for platform crawlers (R4).

Re-exports the public surface so platforms can write::

    from crawlhub.core.platform import (
        BaseHttpClient, BaseService, BaseRecord,
        CookieJar, StringCookieJar, FileCookieJar, MultiTokenCookieJar,
        ProbeResult, CapabilityProbe,
    )

All 6 production platforms migrate onto this layer in R4 P5~P10. See
``docs/superpowers/specs/R4-architecture-refactor.md``.
"""
from __future__ import annotations

from .base_client import BaseHttpClient
from .base_models import BaseRecord
from .base_service import BaseService
from .cookie_jar import CookieJar, StringCookieJar

from .file_cookie_jar import FileCookieJar
from .multi_token_cookie_jar import MultiTokenCookieJar
from .probe_protocol import CapabilityProbe, ProbeResult
from .runtime_service import RuntimeAwareService, RuntimeServices, get_current_runtime


__all__ = [
    "BaseHttpClient",
    "BaseService",
    "BaseRecord",

    "CookieJar",
    "StringCookieJar",
    "FileCookieJar",
    "MultiTokenCookieJar",
    "CapabilityProbe",
    "ProbeResult",
    "RuntimeAwareService",
    "RuntimeServices",
    "get_current_runtime",
]
# R7: BrowserBackedService / BrowserBackedScraper / browser_service.py 已删除
#     R5 setattr 路径完全废弃，BBA action 通过 RuntimeAwareService + hold/PageHandle
