"""
Weibo network client — pure HTTP layer.

R4 P7 (2026-05-24):
  * extends ``BaseHttpClient`` and implements ``_setup_sessions`` + ``probe``
  * absorbs the runtime constants that used to live in ``_internal/config.py``
  * cookie is provided as a raw string (legacy path) OR via a ``CookieJar``
"""

from __future__ import annotations

import re
import time
from typing import Optional

import requests

from crawlhub.core.platform import (
    BaseHttpClient, CookieJar, ProbeResult, StringCookieJar,
)


# ============================================================
# Module-level runtime constants
# (moved here from former ``_internal/config.py``)
# ============================================================

# Default empty cookie (production callers provide one at runtime).
COOKIE = ""
XSRF_TOKEN = ""

# Default game name (for the legacy run_game_monitor mode).
DEFAULT_GAME = ""

# Crawl settings
MAX_SEARCH_PAGES = 3
MAX_COMMENTS_PER_POST = 200
REQUEST_INTERVAL = 2
MAX_WORKERS = 4

# Chrome UA
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


# ============================================================
# WeiboClient
# ============================================================

class WeiboClient(BaseHttpClient):
    """Stateless network client for Weibo.

    Two-session architecture:
      - api_session:    weibo.com/ajax/ JSON API (needs X-XSRF-TOKEN header)
      - search_session: s.weibo.com SSR HTML (needs Accept: text/html)

    Construction options (in priority order):
      1. ``cookie_jar=...``   — direct CookieJar instance (preferred R4 path)
      2. ``cookie=...``       — raw cookie header string (legacy)
      3. neither              — empty/anonymous client
    """

    def __init__(
        self,
        cookie: str = COOKIE,
        xsrf_token: str = "",
        cookie_jar: CookieJar | None = None,
    ):
        # If no jar handed in, wrap the legacy raw-string cookie into one.
        if cookie_jar is None:
            cookie_jar = StringCookieJar(cookie or "")

        # Resolve the working cookie string + XSRF token.
        self.cookie = cookie_jar.as_string() if cookie_jar.as_string() else cookie
        if xsrf_token:
            self.xsrf_token = xsrf_token
        else:
            m = re.search(r'XSRF-TOKEN=([^;]+)', self.cookie or "")
            self.xsrf_token = m.group(1) if m else XSRF_TOKEN

        # BaseHttpClient stores the jar and calls _setup_sessions().
        super().__init__(cookie_jar=cookie_jar)

    # ── BaseHttpClient contract ────────────────────────────────

    def _setup_sessions(self) -> None:
        """Allocate the two requests.Sessions used by weibo crawling."""
        # Session 1: weibo.com/ajax/ JSON API
        self.api_session = requests.Session()
        self.api_session.headers.update({
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "X-Requested-With": "XMLHttpRequest",
            "X-XSRF-TOKEN": self.xsrf_token,
            "Referer": "https://weibo.com/",
            "Cookie": self.cookie,
        })

        # Session 2: s.weibo.com SSR search pages
        self.search_session = requests.Session()
        self.search_session.headers.update({
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": "https://s.weibo.com/",
            "Cookie": self.cookie,
        })

    #: JS snippet for BBA login polling — runs in browser page via
    #: ``page.evaluate()``.  Returns ``{ ok: bool, reason: str, extras: {} }``.
    #
    # 检测原理（2026-06-05 第二次改造）：
    #   不再读 SPA 内存里的 ``window.$CONFIG``——它是首次 SSR 注入的
    #   一次性快照。如果用户停留在 ``weibo.com/newlogin?...`` 这种
    #   登录后跳转页（路由跳转、未真正 navigate 重新 SSR），$CONFIG
    #   永远是登录前的旧值（user={} 或 uid=0），JS 永远拿不到 uid，
    #   stage1 永远 FAIL。
    #
    #   改用浏览器 ``fetch('/')`` 在当前 cookie 下重新拉一次 SSR HTML，
    #   从中匹配 ``"user":{...,"id":<digits>}`` 或 ``"uid":<digits>``。
    #   这跟 HTTP probe 完全同口径——服务端实时渲染，cookie 即时生效。
    #
    # 与 HTTP probe 的关系：
    #   * BROWSER_LOGIN_CHECK_JS：浏览器内 fetch('/') → 解析同一份 SSR HTML
    #   * probe()：Python requests GET https://weibo.com/ → 解析 SSR HTML
    #   两条路径走 ``check_login_from_html`` 同一个正则，对齐输出。
    #
    # 抛弃方案历史：
    #   v0 ``.woo-button-content`` DOM 检测       → 未登录元素不存在，fallthrough 误判
    #   v1 ``window.$CONFIG.user.id`` SPA 内存读  → newlogin 跳转页快照过期
    #   v2 ``fetch('/')`` 重拉 SSR  ✅            → 当前方案
    BROWSER_LOGIN_CHECK_JS = """\
(async () => {
  try {
    const resp = await fetch('/', {
      credentials: 'include',
      headers: { accept: 'text/html,*/*' }
    });
    if (!resp.ok) {
      return { ok: false, extras: {}, reason: 'fetch / status=' + resp.status };
    }
    const html = await resp.text();
    const m = html.match(/"user"\\s*:\\s*\\{[^{}]*?"id"\\s*:\\s*(\\d+)|"uid"\\s*:\\s*(\\d+)/);
    if (m) {
      const uid = m[1] || m[2];
      if (uid && Number(uid) > 0) {
        return { ok: true, extras: { uid: String(uid) }, reason: '' };
      }
    }
    return { ok: false, extras: {}, reason: 'no uid in SSR html' };
  } catch (e) {
    return {
      ok: false,
      extras: {},
      reason: 'fetch error: ' + ((e && e.message) || String(e))
    };
  }
})()
"""

    # 同一口径的 HTML 正则版（用于 HTTP probe）。匹配 SSR 注入的
    #   "user":{...,"id":<digits>,...}  或  "uid":<digits>
    # 任意一个出现即视为登录。``re.S`` 让 ``.`` 跨行（user 对象内容很长）。
    _UID_PATTERN = re.compile(
        r'"user"\s*:\s*\{[^{}]*?"id"\s*:\s*(\d+)|"uid"\s*:\s*(\d+)',
        re.S,
    )

    @classmethod
    def check_login_from_html(cls, html: str) -> tuple[bool, dict]:
        """Check weibo login status from page HTML.

        Looks for ``window.$CONFIG.user.id`` or ``$CONFIG.uid`` injected
        into the SSR HTML by https://weibo.com/.  A non-zero numeric uid
        means the cookie is valid.

        Shared by ``probe()`` (HTTP) and BBA login polling
        (``check_login_from_html`` is HTML-side; the JS counterpart is
        ``BROWSER_LOGIN_CHECK_JS`` for ``page.evaluate()``).
        """
        if not html:
            return False, {"reason": "empty html"}
        m = cls._UID_PATTERN.search(html)
        if not m:
            return False, {"reason": "no uid in $CONFIG"}
        uid_str = m.group(1) or m.group(2) or ""
        try:
            if int(uid_str) > 0:
                return True, {"uid": uid_str}
        except ValueError:
            pass
        return False, {"reason": f"uid not positive: {uid_str!r}"}

    def probe(self, task_type: str = "default") -> ProbeResult:
        """Probe cookie validity via the homepage SSR login indicator.

        GET ``https://weibo.com/`` and look for ``window.$CONFIG.user.id``
        (or ``$CONFIG.uid``) injected into the SSR HTML.  Non-zero uid →
        cookie valid.  See :meth:`check_login_from_html`.
        """
        api_path = "/ (homepage)"
        start = time.time()
        try:
            resp = self.search_session.get(
                "https://weibo.com/",
                timeout=15,
                allow_redirects=True,
            )
            latency_ms = int((time.time() - start) * 1000)

            if resp.status_code != 200:
                return ProbeResult(
                    ok=False,
                    api=api_path,
                    latency_ms=latency_ms,
                    error=f"HTTP {resp.status_code}",
                    extras={"task_type": task_type},
                )

            is_logged_in, extras = self.check_login_from_html(resp.text)
            return ProbeResult(
                ok=is_logged_in,
                api=api_path,
                latency_ms=latency_ms,
                error=None if is_logged_in else extras.get("reason", "no uid in $CONFIG"),
                extras={"task_type": task_type, **extras},
            )
        except Exception as e:
            latency_ms = int((time.time() - start) * 1000)
            return ProbeResult(
                ok=False,
                api=api_path,
                latency_ms=latency_ms,
                error=str(e),
                extras={"task_type": task_type},
            )

    # ============================================================
    # Raw request helpers (cookie-expiry guard is in caller)
    # ============================================================

    def get_api(self, url: str, params: Optional[dict] = None,
                timeout: int = 15) -> requests.Response:
        """GET to weibo.com/ajax/ endpoint. Raises on non-200."""
        resp = self.api_session.get(url, params=params, timeout=timeout)
        resp.raise_for_status()
        return resp

    def get_search(self, url: str, timeout: int = 30) -> requests.Response:
        """GET to s.weibo.com SSR page. Returns response (caller checks 302)."""
        resp = self.search_session.get(url, timeout=timeout)
        # Don't raise_for_status here — caller needs to check 302/cookie
        return resp

    @staticmethod
    def sleep(interval: float = REQUEST_INTERVAL):
        """Throttle helper."""
        time.sleep(interval)
