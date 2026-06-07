"""Douyin cookie refresh orchestrator (R4 R5).

Coordinates the silent-then-interactive cookie refresh strategy. Stays
fully decoupled from the cookie data layer — it never touches the file
directly: all writes go through ``DouyinCookieJar.replace_all`` +
``DouyinCookieJar.save``.

Three external dependencies, all pure:

* ``DouyinCookieJar`` — read current cookies, write fresh cookies
* ``HealthTracker``  — reset failure counter on success
* ``httpx`` / ``playwright`` — server probing & browser automation

Public methods::

    auto_refresh(login_timeout=300)   # silent → interactive fallback
    refresh_ttwid()                   # ttwid-only refresh via bytedance API
    _try_silent_refresh()             # headless: inject + verify
    _interactive_login(timeout)       # headed: wait for sessionid
    _verify_session(cookie_string)    # real API call to confirm session
"""
from __future__ import annotations

import sys
import time
from http.cookies import SimpleCookie

import httpx

from .cookie_jar import DouyinCookieJar
from .health_tracker import HealthTracker

__all__ = ["RefreshOrchestrator"]


class RefreshOrchestrator:
    """Encapsulates the douyin cookie refresh state machine."""

    # API endpoints for token refresh
    TTWID_API = "https://ttwid.bytedance.com/ttwid/union/register/"
    TTWID_DATA = (
        '{"region":"cn","aid":1768,"needFid":false,"service":"www.ixigua.com",'
        '"migrate_info":{"ticket":"","source":"node"},"cbUrlProtocol":"https","union":true}'
    )

    _USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/146.0.0.0 Safari/537.36"
    )

    def __init__(
        self,
        jar: DouyinCookieJar,
        health: HealthTracker,
        log_prefix: str = "dy_refresh",
    ) -> None:
        self._jar = jar
        self._health = health
        self._log_prefix = log_prefix

    def _log(self, msg: str) -> None:
        print(f"[{self._log_prefix}] {msg}", file=sys.stderr)

    # ── Synchronous: ttwid token refresh ────────────────────

    def refresh_ttwid(self) -> bool:
        """Refresh ttwid token via bytedance API. Persists immediately."""
        try:
            resp = httpx.post(
                self.TTWID_API,
                data=self.TTWID_DATA,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=10.0,
            )
            if resp.status_code == 200:
                set_cookie = resp.headers.get("set-cookie", "")
                if set_cookie:
                    sc = SimpleCookie()
                    sc.load(set_cookie)
                    if "ttwid" in sc:
                        new_ttwid = sc["ttwid"].value
                        self._jar.update_token("ttwid", new_ttwid)
                        self._jar.save()
                        self._log(f"ttwid refreshed: {new_ttwid[:30]}...")
                        return True
            self._log("ttwid refresh failed: no ttwid in response")
            return False
        except Exception as e:
            self._log(f"ttwid refresh error: {e}")
            return False

    # ── Async: full cookie refresh (silent → interactive) ──

    async def auto_refresh(self, login_timeout: int = 300) -> bool:
        """Smart refresh — silent first, interactive fallback.

        Args:
            login_timeout: max seconds for manual login.
        Returns:
            True on success.
        """
        try:
            from playwright.async_api import async_playwright  # noqa: F401
        except ImportError:
            self._log(
                "[ERR] Playwright not installed. Run:\n"
                "   pip install playwright && playwright install chromium"
            )
            return False

        self._log("Step 1: Trying silent cookie refresh (headless)...")
        if await self._try_silent_refresh():
            self._log("[OK] Silent refresh succeeded - cookies are fresh!")
            return True

        self._log("[WARN] Silent refresh failed - session expired or no saved cookies.")

        self._log(
            f"Step 2: Opening browser for manual login (timeout {login_timeout}s)..."
        )
        if await self._interactive_login(login_timeout):
            self._log("[OK] Manual login succeeded - cookies saved!")
            return True

        self._log("[FAIL] Cookie refresh failed.")
        return False

    async def _try_silent_refresh(self) -> bool:
        """Headless: inject old cookies, navigate, verify session is alive."""
        from playwright.async_api import async_playwright
        from crawlhub.core.browser.playwright_runtime import _STEALTH_LAUNCH_ARGS

        existing = self._jar.cookies
        if not existing:
            self._log("  No saved cookies to inject - skipping silent refresh")
            return False

        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=True,
                    # 统一走 _STEALTH_LAUNCH_ARGS，避免 Windows 上 unsafe-flag infobar。
                    args=list(_STEALTH_LAUNCH_ARGS),
                )
                context = await browser.new_context(
                    user_agent=self._USER_AGENT,
                    viewport={"width": 1536, "height": 864},
                    locale="zh-CN",
                )

                # Inject saved cookies
                pw_cookies = [
                    {"name": k, "value": v, "domain": ".douyin.com", "path": "/"}
                    for k, v in existing.items()
                ]
                await context.add_cookies(pw_cookies)
                self._log(f"  Injected {len(pw_cookies)} saved cookies")

                page = await context.new_page()

                # Intercept API responses to capture msToken / webid
                captured_params: dict[str, str] = {}
                _re = __import__("re")

                async def _on_response(response):
                    try:
                        url = response.url
                        if "webid=" in url:
                            m = _re.search(r"webid=(\d+)", url)
                            if m:
                                captured_params["webid"] = m.group(1)
                        if "msToken=" in url:
                            m = _re.search(r"msToken=([^&]+)", url)
                            if m and len(m.group(1)) > 10:
                                captured_params["msToken"] = m.group(1)
                    except Exception:
                        pass

                page.on("response", _on_response)

                try:
                    await page.goto(
                        "https://www.douyin.com/",
                        wait_until="domcontentloaded",
                        timeout=20000,
                    )
                    await page.wait_for_timeout(5000)
                except Exception as e:
                    self._log(f"  Navigation error (may still work): {e}")

                cookies = await context.cookies()
                cookie_dict = {c["name"]: c["value"] for c in cookies}

                has_session = bool(
                    cookie_dict.get("sessionid_ss") or cookie_dict.get("sessionid")
                )
                if not has_session:
                    self._log("  No sessionid found - session expired")
                    await browser.close()
                    return False

                # Real API verification
                self._log("  Verifying session with real API request...")
                verify_str = "; ".join(f"{k}={v}" for k, v in cookie_dict.items())
                if not await self._verify_session(verify_str):
                    self._log("  Session cookie exists but server rejected it - expired")
                    await browser.close()
                    return False

                self._log(f"  Session alive! Extracting {len(cookie_dict)} cookies...")

                # Merge captured extra params into existing extra_params
                merged_extra_params = dict(self._jar.extra_params)
                if captured_params:
                    merged_extra_params.update(captured_params)
                    self._log(f"  Captured extra params: {list(captured_params.keys())}")

                # Persist via the jar's write path
                self._jar.replace_all(
                    cookie_dict,
                    extra_headers=self._jar.extra_headers,
                    extra_params=merged_extra_params,
                )
                self._jar.save()
                self._health.reset()

                await browser.close()
            return True

        except Exception as e:
            self._log(f"  Silent refresh error: {e}")
            return False

    async def _interactive_login(self, timeout: int = 300) -> bool:
        """Headed: wait for the user to log in, then snapshot fresh cookies."""
        from playwright.async_api import async_playwright
        from crawlhub.core.browser.playwright_runtime import _STEALTH_LAUNCH_ARGS

        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=False,
                    # 统一走 _STEALTH_LAUNCH_ARGS，避免 Windows 上 unsafe-flag infobar。
                    args=list(_STEALTH_LAUNCH_ARGS),
                )
                context = await browser.new_context(
                    user_agent=self._USER_AGENT,
                    viewport={"width": 1280, "height": 800},
                    locale="zh-CN",
                )

                # Inject any old cookies (might help skip some bootstrap)
                existing = self._jar.cookies
                if existing:
                    pw_cookies = [
                        {"name": k, "value": v, "domain": ".douyin.com", "path": "/"}
                        for k, v in existing.items()
                    ]
                    await context.add_cookies(pw_cookies)

                page = await context.new_page()

                captured_params: dict[str, str] = {}
                _re = __import__("re")

                async def _on_response(response):
                    try:
                        url = response.url
                        if "webid=" in url:
                            m = _re.search(r"webid=(\d+)", url)
                            if m:
                                captured_params["webid"] = m.group(1)
                        if "msToken=" in url:
                            m = _re.search(r"msToken=([^&]+)", url)
                            if m and len(m.group(1)) > 10:
                                captured_params["msToken"] = m.group(1)
                    except Exception:
                        pass

                page.on("response", _on_response)

                self._log("  Opening https://www.douyin.com/ ...")
                try:
                    await page.goto(
                        "https://www.douyin.com/",
                        wait_until="domcontentloaded",
                        timeout=30000,
                    )
                except Exception as e:
                    self._log(f"  Navigation warning: {e}")

                self._log(
                    f"  Waiting for login (scan QR code or use SMS)... "
                    f"timeout={timeout}s"
                )

                start = time.time()
                while time.time() - start < timeout:
                    cookies = await context.cookies()
                    cookie_dict = {c["name"]: c["value"] for c in cookies}
                    if cookie_dict.get("sessionid_ss") or cookie_dict.get("sessionid"):
                        self._log("  Login detected!")

                        await page.wait_for_timeout(3000)
                        cookies = await context.cookies()
                        cookie_dict = {c["name"]: c["value"] for c in cookies}

                        merged_extra_params = dict(self._jar.extra_params)
                        if captured_params:
                            merged_extra_params.update(captured_params)
                            self._log(
                                f"  Captured extra params: {list(captured_params.keys())}"
                            )

                        self._jar.replace_all(
                            cookie_dict,
                            extra_headers=self._jar.extra_headers,
                            extra_params=merged_extra_params,
                        )
                        self._jar.save()
                        self._health.reset()

                        await browser.close()
                        return True

                    await page.wait_for_timeout(2000)

                self._log("  Login timeout - no sessionid detected")
                await browser.close()
                return False

        except Exception as e:
            self._log(f"  Interactive login error: {e}")
            return False

    async def _verify_session(self, cookie_string: str) -> bool:
        """Real API probe — returns True iff server accepts the session."""
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    "https://www.douyin.com/aweme/v1/web/im/user/info/",
                    headers={
                        "cookie": cookie_string,
                        "user-agent": self._USER_AGENT,
                        "referer": "https://www.douyin.com/",
                    },
                    params={"aid": "6383"},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    status = data.get("status_code", -1)
                    if status == 0:
                        self._log("  [OK] Session verified - logged in")
                        return True
                    self._log(f"  [FAIL] Session invalid - API status_code={status}")
                    return False
                self._log(f"  [FAIL] Session verify HTTP {resp.status_code}")
                return False
        except Exception as e:
            self._log(f"  Session verify error: {e}")
            return False
