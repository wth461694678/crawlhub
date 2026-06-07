"""Failure mode detection for crawl requests.

Classifies request failures into distinct categories to enable
differentiated retry/backoff strategies:
- COOKIE_EXPIRED: Cookie is invalid, should switch to another cookie
- RATE_LIMITED: Too many requests, should trigger exponential backoff
- ANTI_CRAWL: Anti-bot detection triggered, should trigger aggressive backoff
- NETWORK_ERROR: Transient network issue, should retry in-place
- UNKNOWN: Unclassified failure
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable

import requests

logger = logging.getLogger("crawlhub.failure_detector")

# Try to import httpx for its exception types
try:
    import httpx
    _HTTPX_AVAILABLE = True
except ImportError:
    _HTTPX_AVAILABLE = False


class FailureMode(str, Enum):
    """Categorized failure modes for crawl requests."""

    COOKIE_EXPIRED = "cookie_expired"
    RATE_LIMITED = "rate_limited"
    ANTI_CRAWL = "anti_crawl"
    NETWORK_ERROR = "network_error"
    UNKNOWN = "unknown"


@dataclass
class FailureResult:
    """Result of failure detection with mode and reason."""

    mode: FailureMode
    reason: str

    def __str__(self) -> str:
        return f"[{self.mode.value}] {self.reason}"


# Type for platform-specific detection functions
# Signature: (response, exception) -> FailureResult | None
PlatformDetector = Callable[
    [requests.Response | None, Exception | None], FailureResult | None
]

# Registry of platform-specific detectors
_platform_detectors: dict[str, list[PlatformDetector]] = {}


def register_platform_detector(platform: str, detector: PlatformDetector) -> None:
    """Register a platform-specific failure detection rule.

    Platform detectors are checked BEFORE generic rules, allowing
    platforms to override default behavior for their specific response formats.

    Args:
        platform: Platform name (e.g. "douyin", "bilibili")
        detector: Function that returns FailureResult or None (skip)
    """
    if platform not in _platform_detectors:
        _platform_detectors[platform] = []
    _platform_detectors[platform].append(detector)


def format_response_dump(
    exc_or_response: Any,
    max_text: int = 2000,
) -> str | None:
    """Extract a compact 'HTTP <status> | body=<text>' dump from an exception
    or a raw Response object, for logging when a task transitions to FAILED.

    Accepts:
      - An Exception that may carry `.response` (requests.HTTPError, httpx.HTTPStatusError, etc.)
      - A raw requests.Response / httpx.Response
      - None / object without response → returns None

    The body is truncated to ``max_text`` chars (default 2 KB) so that
    multi-MB error pages don't blow up task logs / SQLite.
    """
    # Resolve to a response-like object
    resp = exc_or_response
    if resp is None:
        return None
    if not hasattr(resp, "status_code"):
        resp = getattr(exc_or_response, "response", None)
        if resp is None or not hasattr(resp, "status_code"):
            return None

    try:
        status = resp.status_code
    except Exception:
        status = "?"

    # Body extraction (response.text is sync for requests; httpx.Response.text is also sync once read)
    try:
        text = resp.text or ""
    except Exception as e:
        text = f"<unreadable: {type(e).__name__}>"

    if len(text) > max_text:
        text = text[:max_text] + f"... <truncated, total={len(text)}B>"

    # Single-line, escape newlines so it stays one log line
    text_oneline = text.replace("\r", " ").replace("\n", " \\n ")
    return f"HTTP {status} | body={text_oneline}"


def detect_failure(
    response: requests.Response | None = None,
    exception: Exception | None = None,
    platform: str | None = None,
) -> FailureResult:
    """Detect the failure mode from a response and/or exception.

    Detection priority:
    1. Platform-specific detectors (if platform is provided)
    2. Exception-based detection (network errors)
    3. HTTP status code detection
    4. Response body keyword detection
    5. Fallback to UNKNOWN

    Args:
        response: The HTTP response (may be None if exception occurred)
        exception: The exception raised (may be None if response received)
        platform: Platform name for platform-specific rules

    Returns:
        FailureResult with mode and human-readable reason
    """
    # 1. Platform-specific detectors first
    if platform and platform in _platform_detectors:
        for detector in _platform_detectors[platform]:
            try:
                result = detector(response, exception)
                if result is not None:
                    logger.debug(
                        "[failure_detector] Platform detector matched: %s", result
                    )
                    return result
            except Exception as e:
                logger.warning(
                    "[failure_detector] Platform detector error: %s", e
                )

    # 2. Exception-based detection (network errors)
    if exception is not None:
        result = _detect_from_exception(exception)
        if result is not None:
            return result

    # 3. HTTP status code + response body detection
    if response is not None:
        result = _detect_from_response(response)
        if result is not None:
            return result

    # 4. Fallback
    reason = "Unknown failure"
    if exception:
        reason = f"Unclassified exception: {type(exception).__name__}: {exception}"
    elif response is not None:
        reason = f"Unclassified HTTP {response.status_code}"
    return FailureResult(mode=FailureMode.UNKNOWN, reason=reason)


def _detect_from_exception(exc: Exception) -> FailureResult | None:
    """Detect failure mode from exception type."""
    # ════════════════════════════════════════════════════════════════
    #  R7 §13: daemon shutdown 期间的 chrome/page 关闭异常
    # ────────────────────────────────────────────────────────────────
    #  graceful_shutdown 主动关 chrome → in-hold scraper 调 page 操作
    #  抛 TargetClosedError/ConnectionClosedError——这是 daemon 主动行为，
    #  不应污染 anti_crawl 健康统计（如果识别为 ANTI_CRAWL 会触发 backoff
    #  base*2，下次 daemon 启动后正常任务被错误退避）。
    #
    #  统一标 NETWORK_ERROR：不参与 cookie 健康度，task 失败但语义干净。
    # ════════════════════════════════════════════════════════════════
    try:
        from crawlhub.core import daemon as _daemon_mod
        daemon_inst = _daemon_mod._daemon
        if daemon_inst is not None and getattr(daemon_inst, "_shutdown_flag", None) is not None:
            if daemon_inst._shutdown_flag.is_set():
                exc_name = type(exc).__name__
                exc_msg = str(exc)
                if (
                    "TargetClosed" in exc_name
                    or "ConnectionClosed" in exc_name
                    or "BrowserClosed" in exc_name
                    or "page.closed" in exc_msg.lower()
                    or "browser has been closed" in exc_msg.lower()
                ):
                    return FailureResult(
                        mode=FailureMode.NETWORK_ERROR,
                        reason=f"Daemon shutdown closed browser: {exc_name}",
                    )
    except Exception:
        # 任何 daemon 不可访问 → 跳过（不要让 detector 自身报错）
        pass

    # ── Anti-crawl sentinel (browser-backed soft-block) ──
    # AntiCrawlDetected is raised by BrowserSession.report_anti_crawl
    # when a scraper observes a platform-specific soft-block marker
    # (e.g. douyin search_nil_type=verify_check). It MUST take priority
    # over generic network/OSError matching because the exception class
    # itself carries the verdict — no string sniffing needed.
    #
    # Late import to avoid circular dependency
    # (browser.session imports failure_detector for FailureMode).
    try:
        from crawlhub.core.browser.session import AntiCrawlDetected
        if isinstance(exc, AntiCrawlDetected):
            return FailureResult(
                mode=FailureMode.ANTI_CRAWL,
                reason=f"Anti-crawl sentinel: {exc.signal} ({exc.platform})",
            )
    except ImportError:
        pass

    # Network-level errors (requests library)
    network_exceptions = (
        requests.exceptions.ConnectionError,
        requests.exceptions.Timeout,
        ConnectionRefusedError,
        ConnectionResetError,
        ConnectionAbortedError,
        OSError,
    )

    # httpx exceptions (if available)
    if _HTTPX_AVAILABLE:
        if isinstance(exc, (httpx.TimeoutException, httpx.ConnectTimeout, httpx.ReadTimeout)):
            return FailureResult(
                mode=FailureMode.NETWORK_ERROR,
                reason=f"Request timeout: {exc}",
            )
        if isinstance(exc, (httpx.ConnectError, httpx.NetworkError)):
            return FailureResult(
                mode=FailureMode.NETWORK_ERROR,
                reason=f"Connection error: {exc}",
            )

    if isinstance(exc, requests.exceptions.Timeout):
        return FailureResult(
            mode=FailureMode.NETWORK_ERROR,
            reason=f"Request timeout: {exc}",
        )

    if isinstance(exc, requests.exceptions.ConnectionError):
        return FailureResult(
            mode=FailureMode.NETWORK_ERROR,
            reason=f"Connection error: {exc}",
        )

    if isinstance(exc, network_exceptions):
        return FailureResult(
            mode=FailureMode.NETWORK_ERROR,
            reason=f"Network error ({type(exc).__name__}): {exc}",
        )

    return None


def _detect_from_response(response: requests.Response) -> FailureResult | None:
    """Detect failure mode from HTTP response status and body."""
    status = response.status_code

    # Try to get response text safely
    try:
        body = response.text[:2000].lower()  # Limit to first 2KB for keyword search
    except Exception:
        body = ""

    # --- HTTP Status Code based detection ---

    # Cookie expired: 401/403
    if status in (401, 403):
        # Check if it's rate limiting disguised as 403
        if _has_rate_limit_keywords(body):
            return FailureResult(
                mode=FailureMode.RATE_LIMITED,
                reason=f"HTTP {status} with rate limit indicators",
            )
        return FailureResult(
            mode=FailureMode.COOKIE_EXPIRED,
            reason=f"HTTP {status}: Authentication/authorization failed",
        )

    # Rate limited: 429
    if status == 429:
        return FailureResult(
            mode=FailureMode.RATE_LIMITED,
            reason="HTTP 429: Too Many Requests",
        )

    # Anti-crawl: 418 (I'm a teapot - commonly used for bot detection)
    if status == 418:
        return FailureResult(
            mode=FailureMode.ANTI_CRAWL,
            reason="HTTP 418: Bot detection triggered",
        )

    # Anti-crawl: 503 with anti-crawl indicators
    if status == 503:
        if _has_anti_crawl_keywords(body):
            return FailureResult(
                mode=FailureMode.ANTI_CRAWL,
                reason="HTTP 503 with anti-crawl indicators",
            )
        # Could be temporary server issue
        return FailureResult(
            mode=FailureMode.NETWORK_ERROR,
            reason="HTTP 503: Service Unavailable",
        )

    # --- Response body keyword detection (for 200 responses with error content) ---

    if status == 200 or (200 <= status < 300):
        # Some platforms return 200 with error in body
        if _has_cookie_expired_keywords(body):
            return FailureResult(
                mode=FailureMode.COOKIE_EXPIRED,
                reason="Response body indicates cookie/session expired",
            )
        if _has_rate_limit_keywords(body):
            return FailureResult(
                mode=FailureMode.RATE_LIMITED,
                reason="Response body indicates rate limiting",
            )
        if _has_anti_crawl_keywords(body):
            return FailureResult(
                mode=FailureMode.ANTI_CRAWL,
                reason="Response body indicates anti-crawl detection",
            )
        # Empty response body can indicate anti-crawl
        if len(body.strip()) == 0:
            return FailureResult(
                mode=FailureMode.ANTI_CRAWL,
                reason="Empty response body (possible anti-crawl)",
            )

    # For other 4xx/5xx errors, try keyword detection
    if status >= 400:
        if _has_cookie_expired_keywords(body):
            return FailureResult(
                mode=FailureMode.COOKIE_EXPIRED,
                reason=f"HTTP {status} with cookie expiry indicators",
            )
        if _has_rate_limit_keywords(body):
            return FailureResult(
                mode=FailureMode.RATE_LIMITED,
                reason=f"HTTP {status} with rate limit indicators",
            )
        if _has_anti_crawl_keywords(body):
            return FailureResult(
                mode=FailureMode.ANTI_CRAWL,
                reason=f"HTTP {status} with anti-crawl indicators",
            )

    return None


# --- Keyword detection helpers ---

_COOKIE_EXPIRED_KEYWORDS = [
    "login", "expired", "unauthorized", "unauthenticated",
    "session expired", "token expired", "please login",
    # Chinese keywords
    "登录", "登陆", "未登录", "请登录", "会话过期",
    "身份验证失败", "认证失败", "token失效",
]

_RATE_LIMIT_KEYWORDS = [
    "rate limit", "too many requests", "throttled",
    "request limit", "quota exceeded", "slow down",
    # Chinese keywords
    "频率", "请求过多", "访问频繁", "操作太频繁",
    "请稍后再试", "频率限制",
]

_ANTI_CRAWL_KEYWORDS = [
    "captcha", "verify", "verification",
    "robot", "bot detected", "automated",
    "challenge", "security check", "access denied",
    "waf", "firewall", "blocked",
    # Chinese keywords
    "验证码", "人机验证", "安全验证",
    "访问被拒绝", "异常访问", "风控",
]


def _has_cookie_expired_keywords(body: str) -> bool:
    """Check if response body contains cookie expiry indicators."""
    return any(kw in body for kw in _COOKIE_EXPIRED_KEYWORDS)


def _has_rate_limit_keywords(body: str) -> bool:
    """Check if response body contains rate limiting indicators."""
    return any(kw in body for kw in _RATE_LIMIT_KEYWORDS)


def _has_anti_crawl_keywords(body: str) -> bool:
    """Check if response body contains anti-crawl indicators."""
    return any(kw in body for kw in _ANTI_CRAWL_KEYWORDS)
