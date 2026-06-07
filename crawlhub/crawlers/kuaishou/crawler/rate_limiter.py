"""
Global Rate Limiter (Token Bucket)
===================================
Thread-safe, singleton-based rate limiter for controlling API request frequency.

Usage:
    from crawler.rate_limiter import GlobalRateLimiter

    limiter = GlobalRateLimiter.instance()
    limiter.set_qps(1.0)   # 1 request per second
    limiter.set_qps(0)     # disable rate limiting

    limiter.acquire()       # block until a token is available
"""

from __future__ import annotations

import threading
import time


class GlobalRateLimiter:
    """Token-bucket rate limiter (singleton).

    - max_qps > 0 : allow at most max_qps requests per second (across all threads).
    - max_qps == 0: no rate limiting (acquire() returns immediately).
    """

    _instance: GlobalRateLimiter | None = None
    _init_lock = threading.Lock()

    def __init__(self):
        self._lock = threading.Lock()
        self._max_qps: float = 1.0       # default: 1 req/s
        self._min_interval: float = 1.0   # 1 / max_qps
        self._last_time: float = 0.0      # timestamp of last acquire()
        self._enabled: bool = True

    @classmethod
    def instance(cls) -> GlobalRateLimiter:
        """Get or create the singleton instance."""
        if cls._instance is None:
            with cls._init_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def set_qps(self, max_qps: float):
        """Set the global QPS limit.

        Args:
            max_qps: Maximum requests per second. 0 = no limit.
        """
        with self._lock:
            self._max_qps = max(0.0, float(max_qps))
            if self._max_qps <= 0:
                self._enabled = False
                self._min_interval = 0.0
            else:
                self._enabled = True
                self._min_interval = 1.0 / self._max_qps

    @property
    def max_qps(self) -> float:
        return self._max_qps

    @property
    def enabled(self) -> bool:
        return self._enabled

    def acquire(self):
        """Block until a request is allowed under the rate limit.

        If rate limiting is disabled (max_qps=0), returns immediately.
        """
        if not self._enabled:
            return

        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_time
            if elapsed < self._min_interval:
                wait = self._min_interval - elapsed
                # Release lock while sleeping so other threads can queue up
                # (they'll wait for the lock and re-check timing)
            else:
                self._last_time = now
                return

        # Sleep outside the lock, then re-acquire to update timestamp
        time.sleep(wait)

        with self._lock:
            self._last_time = time.monotonic()

    def status(self) -> dict:
        """Return current rate limiter status as a dict."""
        return {
            "max_qps": self._max_qps,
            "enabled": self._enabled,
            "min_interval_s": round(self._min_interval, 3),
        }
