"""Cookie resolver mixin for platform crawlers.

The canonical home for cookie path resolution. The mixin can be applied
to any class (typically the platform ``Scraper``) so that every platform
shares the same path-resolution / thread-override / multi-cookie selection
logic without going through any extra Bridge-style indirection.

Usage
------

::

    from crawlhub.core.cookie_resolver import (
        CookieResolverMixin,
        CookieNotReady,
        BridgeImportError,
    )

    class SteamScraper(CookieResolverMixin):
        PLATFORM_NAME = "steam"

        def check_cookie_valid(self) -> bool:
            ...

Notes
-----

* The mixin uses the ``PLATFORM_NAME`` class variable as the platform
  identifier — declarative, no extra method indirection.
* ``check_cookie_valid()`` remains the only required hook subclasses must
  implement.
* ``CookieNotReady`` and ``BridgeImportError`` are defined here as the
  single source of truth for cookie-flow exceptions.
"""

from __future__ import annotations

import logging
from abc import abstractmethod
from pathlib import Path

from crawlhub.core.config import get_data_root
from crawlhub.core.cookie_dispatcher import get_cookie_throttle
from crawlhub.core.cookie_override import get_thread_cookie_override

logger = logging.getLogger(__name__)

# Root directory for crawler data/cookie files
_CRAWLERS_ROOT = get_data_root() / "crawlers"


class CookieNotReady(Exception):
    """Raised when cookie is missing or expired for a platform."""

    def __init__(self, platform: str, message: str = ""):
        self.platform = platform
        self.message = message or (
            f"Cookie for {platform} is not ready. Please refresh cookie first."
        )
        super().__init__(self.message)


class BridgeImportError(Exception):
    """Raised when underlying crawler module cannot be imported."""

    def __init__(self, platform: str, original_error: Exception):
        self.platform = platform
        self.original_error = original_error
        super().__init__(
            f"Cannot import {platform} crawler module: {original_error}. "
            f"Ensure the crawler package is installed."
        )


class CookieResolverMixin:
    """Cookie path resolution + smart selection mixin.

    Concrete classes MUST set ``PLATFORM_NAME`` and implement
    ``check_cookie_valid``. Everything else has sensible defaults that
    cover the standard cookie-store + thread-override flow.
    """

    #: Platform identifier (e.g. ``"steam"``, ``"qimai"``). Subclasses MUST set.
    PLATFORM_NAME: str = ""

    # ------------------------------------------------------------------
    # Required hook
    # ------------------------------------------------------------------

    @abstractmethod
    def check_cookie_valid(self) -> bool:
        """Check if the platform cookie is valid and usable.

        Returns True if cookie exists and is likely valid.
        Raises ``CookieNotReady`` if cookie is missing or expired.
        """

    # ------------------------------------------------------------------
    # Internal helper
    # ------------------------------------------------------------------

    def _platform(self) -> str:
        """Return ``PLATFORM_NAME`` or raise if subclass forgot to set it."""
        platform = getattr(self, "PLATFORM_NAME", "") or ""
        if not platform:
            raise RuntimeError(
                f"{type(self).__name__} must set PLATFORM_NAME class variable"
            )
        return platform

    # ------------------------------------------------------------------
    # Cookie path resolution
    # ------------------------------------------------------------------

    def get_cookie_path(self) -> Path:
        """Get the standard cookie file path for this platform.

        Uses CookieStore to find the first (most recent) valid cookie.
        Falls back to legacy path if no multi-cookie found.
        """
        from crawlhub.core.cookies import get_cookie_store

        platform = self._platform()
        store = get_cookie_store()
        first_path = store.get_first_cookie_path(platform)
        if first_path and first_path.exists():
            return first_path
        return get_data_root() / "cookies" / f"{platform}.json"

    def get_crawler_cookie_path(self) -> Path:
        """Get the cookie path used by the underlying crawler.

        Override this if the crawler stores cookies in a different location.
        """
        return _CRAWLERS_ROOT / self._platform() / "data" / "cookie.json"

    def select_best_cookie_path(self) -> Path:
        """Select the best available cookie using CookieThrottle smart selection.

        Priority: VALID > UNKNOWN > EXPIRED (last resort).
        Falls back to ``get_cookie_path()`` if no cookies registered in throttle.

        Raises:
            CookieNotReady: If all cookies are expired and no fallback available.
        """
        throttle = get_cookie_throttle()
        platform = self._platform()

        # Ensure cookies are loaded into throttle
        if throttle.cookie_count(platform) == 0:
            throttle.load_platform_cookies(platform)

        # Check if all expired -> block submission
        if throttle.all_expired(platform):
            raise CookieNotReady(
                platform,
                f"All cookies for {platform} are expired. Please refresh cookies.",
            )

        # Select best cookie
        state = throttle.select_best_cookie(platform)
        if state and state.path and Path(state.path).exists():
            return Path(state.path)

        # Fallback to legacy path resolution
        return self.get_cookie_path()

    def get_effective_cookie_path(self) -> Path:
        """Get the effective cookie path for task execution.

        Tries crawler-specific path first, then smart selection from CookieStore.
        """
        crawler_path = self.get_crawler_cookie_path()
        if crawler_path.exists():
            return crawler_path
        return self.select_best_cookie_path()

    def resolve_cookie_path(self) -> Path:
        """Resolve the cookie path the current task MUST use.

        Priority:
        1. Thread-local override (set by daemon BEFORE throttle.acquire) —
           ensures daemon's throttled cookie == scraper's actual cookie.
        2. Crawler-specific path (legacy, when no override is set).
        3. Standard cookie store fallback.

        All scraper execution paths MUST use this method instead of manually
        chaining ``get_crawler_cookie_path()`` and ``get_cookie_path()``.
        Otherwise the throttle / retry / failure-report logic in daemon will
        be reporting against a different cookie than the one actually being
        used, breaking per-cookie interval control.
        """
        override = get_thread_cookie_override()
        if override:
            p = Path(override)
            if p.exists():
                return p
            # Override set but file gone — log and fall through to legacy chain
            logger.warning(
                "[cookie_resolver] thread cookie override %s does not exist, falling back",
                override,
            )

        crawler_path = self.get_crawler_cookie_path()
        if crawler_path.exists():
            return crawler_path
        return self.get_cookie_path()

    def ensure_cookie(self) -> None:
        """Pre-flight cookie check. Raises ``CookieNotReady`` if invalid."""
        if not self.check_cookie_valid():
            raise CookieNotReady(self._platform())
