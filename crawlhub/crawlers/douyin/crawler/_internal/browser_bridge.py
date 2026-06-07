"""
Browser Bridge
==============

⚠️ DEPRECATED — DO NOT USE IN NEW CODE (2026-05-29)
─────────────────────────────────────────────────────────────────────
本模块**完全 bypass crawlhub stealth 体系**：
  - 用 playwright（不是 patchright）→ CDP 协议层 leak 没修
  - launch（不是 launch_persistent_context）→ 没有指纹连续性
  - 仅 2 个 launch arg（生产路径有 30+ 个）
  - UA 硬编码 Chrome/146（机器实际可能是 148+）→ 版本错位
  - 不注入 stealth_override.js → navigator.languages / platformVersion / chrome.app 全 leak

实测（fingerprint_audit/probe.js 验证）：用本模块启动的浏览器在 baidu.com 上
就有 5+ 个 Tier A leak。在抖音上跑会被 SDK 一秒识破。

✅ 正确的浏览器入口：
    crawlhub.core.browser.playwright_runtime:create_playwright_browser_session()
   该入口包含完整 stealth 体系（host_environment 自适应 + stealth_override.js）。

本文件保留是因为：
  1. 历史模块，docstring 示例代码还在
  2. 万一有未发现的隐式调用，先保留方便排查
  3. 整理 BBA 历史时的备查

如果你想新写"调浏览器做某件事"的代码，**严禁参考本模块的 launch 写法**。
─────────────────────────────────────────────────────────────────────

Uses Python playwright (pip install playwright) to maintain a browser session.
The browser handles bd-ticket-guard signing automatically via bdms.js.

This module was used for:
  - Sub-comment (reply) fetching: browser executes fetch() with auto guard headers
  - Cookie extraction / refresh: extract fresh cookies from browser session
  - Login flow: open browser for user to scan QR code

No AI dependency — pure Python code controlling a headless/headed Chromium.

Usage (DEPRECATED — use create_playwright_browser_session instead):
    bridge = BrowserBridge(cookie_path="data/cookie.json")
    await bridge.start()
    replies = await bridge.fetch_reply_comments(item_id, comment_id)
    await bridge.stop()
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode, quote


__all__ = ["BrowserBridge"]


class BrowserBridge:
    """Playwright-based browser bridge for Douyin API calls that require
    bd-ticket-guard signing (e.g., sub-comment/reply API).

    The browser loads Douyin's page, which initializes bdms.js.
    When we need guard-protected API calls, we execute fetch() inside
    the browser — bdms.js automatically injects guard headers.

    Args:
        cookie_path: Path to cookie JSON (shared with DouyinCookieJar).
        headless:    Run browser in headless mode (default True).
        video_url:   A Douyin video URL to load for initializing JS context.
                     If None, uses a default popular video.
    """

    DEFAULT_VIDEO_URL = "https://www.douyin.com/video/7620730594124664106"

    # Common query params (subset — browser fills in the rest)
    COMMON_PARAMS = {
        "device_platform": "webapp",
        "aid": "6383",
        "channel": "channel_pc_web",
        "update_version_code": "170400",
        "pc_client_type": "1",
        "pc_libra_divert": "Windows",
        "version_code": "170400",
        "version_name": "17.4.0",
        "cookie_enabled": "true",
        "screen_width": "2560",
        "screen_height": "1440",
        "browser_language": "zh-CN",
        "browser_platform": "Win32",
        "browser_name": "Chrome",
        "browser_version": "146.0.0.0",
        "browser_online": "true",
        "engine_name": "Blink",
        "engine_version": "146.0.0.0",
        "os_name": "Windows",
        "os_version": "10",
        "cpu_core_num": "24",
        "device_memory": "8",
        "platform": "PC",
        "downlink": "10",
        "effective_type": "4g",
        "round_trip_time": "50",
    }

    def __init__(
        self,
        cookie_path: str = None,
        headless: bool = True,
        video_url: str = None,
    ):
        _base = Path(__file__).parent.parent
        self.cookie_path = cookie_path or str(_base / "data" / "cookie.json")
        self.headless = headless
        self.video_url = video_url or self.DEFAULT_VIDEO_URL

        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._initialized = False

    def _log(self, msg: str):
        print(f"[browser_bridge] {msg}", file=sys.stderr)

    # ── Lifecycle ────────────────────────────────────────────

    async def start(self, load_cookies: bool = True):
        """Start the browser and initialize the Douyin JS context.

        Args:
            load_cookies: Whether to inject saved cookies before navigation.
        """
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            raise RuntimeError(
                "Playwright not installed. Run:\n"
                "  pip install playwright\n"
                "  playwright install chromium"
            )

        self._log("Starting browser...")
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self.headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
        )
        self._context = await self._browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/146.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1536, "height": 864},
            locale="zh-CN",
        )

        # Inject saved cookies if available
        if load_cookies:
            await self._inject_cookies()

        self._page = await self._context.new_page()

        # Navigate to a video page to initialize bdms.js context
        self._log(f"Navigating to {self.video_url} to init JS context...")
        try:
            await self._page.goto(self.video_url, wait_until="domcontentloaded", timeout=30000)
            # Wait a bit for bdms.js to fully initialize
            await self._page.wait_for_timeout(3000)
            self._initialized = True
            self._log("Browser initialized, bdms.js should be ready.")
        except Exception as e:
            self._log(f"Navigation warning (may still work): {e}")
            self._initialized = True  # Try anyway

    async def stop(self):
        """Close the browser and clean up."""
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception:
                pass
        self._browser = None
        self._context = None
        self._page = None
        self._playwright = None
        self._initialized = False
        self._log("Browser stopped.")

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, *args):
        await self.stop()

    @property
    def is_ready(self) -> bool:
        return self._initialized and self._page is not None

    # ── Cookie Management ────────────────────────────────────

    async def _inject_cookies(self):
        """Load cookies from JSON file and inject into browser context."""
        path = Path(self.cookie_path)
        if not path.exists():
            self._log(f"No cookie file found at {self.cookie_path}")
            return

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)

            cookie_dict = {}
            if isinstance(data, dict) and "cookies" in data:
                cookie_dict = data["cookies"]
            elif isinstance(data, dict) and "cookie_string" in data:
                for pair in data["cookie_string"].split(";"):
                    pair = pair.strip()
                    if "=" in pair:
                        k, _, v = pair.partition("=")
                        cookie_dict[k.strip()] = v.strip()

            if not cookie_dict:
                self._log("No cookies to inject")
                return

            # Convert to playwright cookie format
            pw_cookies = []
            for name, value in cookie_dict.items():
                pw_cookies.append({
                    "name": name,
                    "value": value,
                    "domain": ".douyin.com",
                    "path": "/",
                })

            await self._context.add_cookies(pw_cookies)
            self._log(f"Injected {len(pw_cookies)} cookies into browser")

        except Exception as e:
            self._log(f"Failed to inject cookies: {e}")

    async def extract_cookies(self) -> dict:
        """Extract current cookies from browser and save to file.

        Returns:
            dict with cookie_string, cookies dict, and extra info.
        """
        if not self._context:
            raise RuntimeError("Browser not started")

        cookies = await self._context.cookies()
        cookie_dict = {c["name"]: c["value"] for c in cookies}
        cookie_string = "; ".join(f"{k}={v}" for k, v in cookie_dict.items())

        result = {
            "cookie_string": cookie_string,
            "cookies": cookie_dict,
            "extra_headers": {},
            "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

        # Save to file
        Path(self.cookie_path).parent.mkdir(parents=True, exist_ok=True)
        with open(self.cookie_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)

        self._log(f"Extracted and saved {len(cookie_dict)} cookies")
        return result

    # ── Login Flow ───────────────────────────────────────────

    async def login_with_qr(self, timeout: int = 300) -> bool:
        """Open Douyin login page for user to scan QR code.

        Args:
            timeout: Max wait time in seconds (default 5 min).

        Returns:
            True if login was successful.
        """
        if not self._page:
            raise RuntimeError("Browser not started. Call start() first.")

        # Navigate to login page
        self._log("Opening login page...")
        await self._page.goto("https://www.douyin.com/", wait_until="domcontentloaded")

        self._log(f"Please scan QR code to login (timeout: {timeout}s)...")

        # Wait for sessionid cookie to appear (indicates successful login)
        start = time.time()
        while time.time() - start < timeout:
            cookies = await self._context.cookies()
            cookie_dict = {c["name"]: c["value"] for c in cookies}
            if cookie_dict.get("sessionid_ss") or cookie_dict.get("sessionid"):
                self._log("Login detected! Extracting cookies...")
                await self.extract_cookies()
                return True
            await self._page.wait_for_timeout(2000)

        self._log("Login timeout")
        return False

    # ── Sub-Comment (Reply) Fetching ─────────────────────────

    async def fetch_reply_comments(
        self,
        item_id: str,
        comment_id: str,
        cursor: int = 0,
        count: int = 20,
    ) -> dict:
        """Fetch sub-comments via browser's fetch() — bdms.js auto-injects guard headers.

        Args:
            item_id:    Video ID.
            comment_id: Parent comment ID.
            cursor:     Pagination cursor.
            count:      Number of replies to fetch.

        Returns:
            Raw API response dict (same format as DouyinSDK.comment_reply_list).
        """
        if not self.is_ready:
            raise RuntimeError("Browser not initialized. Call start() first.")

        # Build the API URL with query params
        params = dict(self.COMMON_PARAMS)
        params.update({
            "item_id": item_id,
            "comment_id": comment_id,
            "cut_version": "1",
            "cursor": str(cursor),
            "count": str(count),
            "item_type": "0",
        })

        query_string = urlencode(params, safe="=", quote_via=quote)
        api_url = f"https://www.douyin.com/aweme/v1/web/comment/list/reply/?{query_string}"

        # Execute fetch() in the browser context
        # bdms.js hooks XMLHttpRequest/fetch and auto-injects bd-ticket-guard headers
        js_code = f"""
        async () => {{
            try {{
                const resp = await fetch("{api_url}", {{
                    method: "GET",
                    credentials: "include",
                    headers: {{
                        "accept": "application/json, text/plain, */*",
                    }},
                }});
                if (!resp.ok) {{
                    return {{ error: `HTTP ${{resp.status}}`, status_code: -1 }};
                }}
                const text = await resp.text();
                if (!text || text.length === 0) {{
                    return {{ error: "Empty response", status_code: -1 }};
                }}
                try {{
                    return JSON.parse(text);
                }} catch (e) {{
                    return {{ error: `JSON parse error: ${{e.message}}`, raw: text.substring(0, 200), status_code: -1 }};
                }}
            }} catch (e) {{
                return {{ error: `Fetch error: ${{e.message}}`, status_code: -1 }};
            }}
        }}
        """

        try:
            result = await self._page.evaluate(js_code)
            if result and isinstance(result, dict):
                status_code = result.get("status_code")
                if status_code == 0:
                    comments = result.get("comments", [])
                    self._log(
                        f"Reply fetch OK: item={item_id} comment={comment_id} "
                        f"cursor={cursor} → {len(comments)} replies"
                    )
                elif "error" in result:
                    self._log(f"Reply fetch error: {result['error']}")
                return result
            else:
                self._log(f"Unexpected result type: {type(result)}")
                return {"status_code": -1, "error": "Unexpected response type"}

        except Exception as e:
            self._log(f"Browser evaluate failed: {e}")
            return {"status_code": -1, "error": str(e)}

    async def fetch_all_reply_comments(
        self,
        item_id: str,
        comment_id: str,
        count_per_page: int = 20,
        max_replies: Optional[int] = None,
        page_delay: float = 0.5,
    ) -> list[dict]:
        """Fetch all sub-comments for a root comment with pagination.

        Args:
            item_id:        Video ID.
            comment_id:     Parent comment ID.
            count_per_page: Items per page.
            max_replies:    Max replies to fetch (None = all).
            page_delay:     Delay between pages in seconds.

        Returns:
            List of raw comment dicts from API.
        """
        all_replies = []
        cursor = 0
        page_num = 0

        while True:
            page_num += 1
            data = await self.fetch_reply_comments(
                item_id, comment_id, cursor=cursor, count=count_per_page
            )

            comments = data.get("comments") or []
            if not comments:
                break

            all_replies.extend(comments)

            if max_replies and len(all_replies) >= max_replies:
                all_replies = all_replies[:max_replies]
                break

            has_more = data.get("has_more", 0)
            new_cursor = data.get("cursor", 0)
            if not has_more or new_cursor == cursor:
                break

            cursor = new_cursor
            await asyncio.sleep(page_delay)

        self._log(
            f"Fetched all replies for comment {comment_id}: "
            f"{len(all_replies)} total, {page_num} pages"
        )
        return all_replies

    # ── Generic Browser Fetch ────────────────────────────────

    async def browser_fetch(self, url: str) -> dict:
        """Execute a generic fetch() in the browser context.

        Useful for any API that needs guard headers.

        Args:
            url: Full API URL.

        Returns:
            Parsed JSON response dict.
        """
        if not self.is_ready:
            raise RuntimeError("Browser not initialized")

        js_code = f"""
        async () => {{
            try {{
                const resp = await fetch("{url}", {{
                    method: "GET",
                    credentials: "include",
                    headers: {{ "accept": "application/json, text/plain, */*" }},
                }});
                const text = await resp.text();
                if (!text) return {{ error: "Empty response", status_code: -1 }};
                return JSON.parse(text);
            }} catch (e) {{
                return {{ error: e.message, status_code: -1 }};
            }}
        }}
        """
        return await self._page.evaluate(js_code)

    # ── Navigate to Video ────────────────────────────────────

    async def navigate_to_video(self, aweme_id: str):
        """Navigate browser to a specific video page.

        This refreshes the JS context for that video's comment section.
        """
        url = f"https://www.douyin.com/video/{aweme_id}"
        if self._page:
            try:
                await self._page.goto(url, wait_until="domcontentloaded", timeout=20000)
                await self._page.wait_for_timeout(2000)
                self._log(f"Navigated to video {aweme_id}")
            except Exception as e:
                self._log(f"Navigation warning: {e}")

    # ── Video Search via Browser ─────────────────────────────

    async def search_videos(
        self,
        keyword: str,
        max_results: int = 20,
        sort_type: int = 0,
        publish_time: int = 0,
        filter_duration: int = 0,
        scroll_wait: float = 3.0,
    ) -> list[dict]:
        """Search videos via headless browser, intercepting API responses.

        Pure API search triggers Douyin's verify_check anti-bot mechanism.
        This method navigates a real browser to the search page, which
        naturally passes all JS-based bot checks.

        Args:
            keyword:         Search keyword.
            max_results:     Maximum number of video results.
            sort_type:       Sort order: 0=relevance, 1=most_liked, 2=newest.
            publish_time:    Publish time filter: 0=all, 1=last_day, 7=last_week, 182=last_half_year.
            filter_duration: Duration filter: 0=all, 1=under_1min, 2=1_to_5min, 3=over_5min.
            scroll_wait:     Seconds to wait after each scroll for data to load.

        Returns:
            List of dicts with keys: aweme_id, desc, like_count, comment_count,
            share_count, author_uid, author_nickname.
        """
        # Build search URL with filter params
        encoded_keyword = quote(keyword, safe='')
        search_url = f"https://www.douyin.com/search/{encoded_keyword}?type=video"

        # Append filter params if non-default
        filter_parts = []
        if sort_type:
            filter_parts.append(f"sort_type={sort_type}")
        if publish_time:
            filter_parts.append(f"publish_time={publish_time}")
        if filter_duration:
            filter_parts.append(f"filter_duration={filter_duration}")
        if filter_parts:
            search_url += "&" + "&".join(filter_parts)

        # Collect raw search result items from intercepted API responses
        collected_items: list[dict] = []
        seen_ids: set[str] = set()

        async def _on_search_response(response):
            url = response.url
            # Match actual Douyin search API paths
            if not ('web/search/item' in url
                    or 'general/search/single' in url
                    or 'general/search/stream' in url):
                return
            ct = response.headers.get('content-type', '')
            if 'json' not in ct:
                return
            try:
                body = await response.json()
                items = body.get("data", [])
                if not isinstance(items, list):
                    return
                new_count = 0
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    aweme_info = item.get("aweme_info")
                    if not aweme_info or not isinstance(aweme_info, dict):
                        continue
                    aid = str(aweme_info.get("aweme_id", ""))
                    if not aid or aid in seen_ids:
                        continue
                    seen_ids.add(aid)
                    collected_items.append(aweme_info)
                    new_count += 1
                if new_count:
                    self._log(f"  [search intercept] +{new_count} videos (total {len(collected_items)})")
                else:
                    nil_info = body.get("search_nil_info", {})
                    nil_type = nil_info.get("search_nil_type", "")
                    if nil_type:
                        self._log(f"  [search intercept] nil_type={nil_type}")
            except Exception:
                pass

        # Start a fresh browser context for search (isolated from main page)
        # We use self._context to create a new page, keeping the same cookies
        if not self._context:
            raise RuntimeError("Browser not started. Call start() first.")

        search_page = await self._context.new_page()
        search_page.on("response", _on_search_response)

        try:
            self._log(f"Navigating to search: {keyword} (max {max_results})...")
            try:
                await search_page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                self._log(f"  Navigation timeout (continuing): {e}")

            # Wait for initial results to load
            await search_page.wait_for_timeout(int(scroll_wait * 1000))

            # Scroll to load more results
            max_scroll_rounds = max(1, (max_results // 10) + 2)
            no_new_count = 0

            for i in range(max_scroll_rounds):
                if len(collected_items) >= max_results:
                    self._log(f"  Reached {len(collected_items)} >= {max_results}, stopping scroll")
                    break

                prev_count = len(collected_items)
                await search_page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await search_page.wait_for_timeout(int(scroll_wait * 1000))

                if len(collected_items) == prev_count:
                    no_new_count += 1
                    if no_new_count >= 3:
                        self._log(f"  No new results after {no_new_count} scrolls, stopping")
                        break
                else:
                    no_new_count = 0

            self._log(f"  Search done: intercepted {len(collected_items)} videos total")

        finally:
            await search_page.close()

        # Parse collected aweme_info dicts into clean result format
        results = []
        for aweme_info in collected_items[:max_results]:
            stats = aweme_info.get("statistics", {}) or {}
            author = aweme_info.get("author", {}) or {}
            results.append({
                "aweme_id": str(aweme_info.get("aweme_id", "")),
                "desc": aweme_info.get("desc", ""),
                "like_count": stats.get("digg_count", 0),
                "comment_count": stats.get("comment_count", 0),
                "share_count": stats.get("share_count", 0),
                "author_uid": str(author.get("uid", "")),
                "author_nickname": author.get("nickname", ""),
            })

        return results
