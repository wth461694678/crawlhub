"""
Douyin Web SDK
==============
Low-level HTTP client for Douyin web APIs.
Uses pure Python a_bogus signing (no browser required).

Key APIs:
  - /aweme/v1/web/general/search/single/   (keyword search)
  - /aweme/v1/web/comment/list/             (comment list)
  - /aweme/v1/web/comment/list/reply/       (sub-comment / reply list)
  - /aweme/v1/web/aweme/detail/             (video detail)
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode, quote, quote_plus

import httpx

from crawlhub.core.platform import (
    BaseHttpClient, CookieJar, ProbeResult,
)

from ._internal.abogus import ABogus, DEFAULT_USER_AGENT
from ._internal.cookie_jar import DouyinCookieJar
from ._internal.health_tracker import HealthTracker
from ._internal.refresh_orchestrator import RefreshOrchestrator
from ._internal.live_protocol import (
    capture_push_wss,
    collect_events as collect_live_protocol_events,
    get_live_room_info as get_douyin_live_room_info,
    parse_web_rid,
    search_live_rooms as search_douyin_live_rooms,
)



class DouyinSDK(BaseHttpClient):
    """Douyin Web API client with pure Python a_bogus signing.

    No browser or Node.js required. Uses httpx for HTTP/2 support
    and ABogus for URL parameter signing.

    R4 P8 (2026-05-24):
      * extends ``BaseHttpClient`` (Protocol over impl - httpx is fine)
      * ``_setup_sessions`` allocates the httpx.Client
      * ``probe`` reuses the SSR-based login check (no API signing needed)

    R4 P13 (2026-05-25):
      * ``probe`` is now the single source of truth for douyin cookie
        verdicts — it requests ``/jingxuan`` and parses the SSR-embedded
        ``user.isLogin`` field.  No API signing required.

    R4 P12 + R5 (2026-05-25):
      * God-class ``CookieManager`` decomposed into three peers:
          - ``DouyinCookieJar``       (pure data layer, file-backed)
          - ``HealthTracker``         (consecutive-failure counter)
          - ``RefreshOrchestrator``   (silent/interactive/ttwid refresh)
      * The SDK builds a ``DouyinCookieJar`` automatically when
        ``cookie_jar`` is not passed in. Callers may inject a custom
        jar (or any ``CookieJar``-compatible object) for testing.

    Args:
        cookie_path: Path to saved cookie JSON file.
        log_prefix:  Prefix for log messages.
        cookie_jar:  Optional pre-built jar; if omitted, a
                     ``DouyinCookieJar`` is created from ``cookie_path``.
    """

    BASE_URL = "https://www.douyin.com"

    # Common query params that Douyin web app sends with every request
    COMMON_PARAMS = {
        "device_platform": "webapp",
        "aid": "6383",
        "channel": "channel_pc_web",
        "update_version_code": "170400",
        "pc_client_type": "1",
        "pc_libra_divert": "Windows",
        "support_h265": "1",
        "support_dash": "1",
        "cpu_core_num": "24",
        "version_code": "170400",
        "version_name": "17.4.0",
        "cookie_enabled": "true",
        "screen_width": "1728",
        "screen_height": "1079",
        "browser_language": "zh-CN",
        "browser_platform": "Win32",
        "browser_name": "Chrome",
        "browser_version": "147.0.0.0",
        "browser_online": "true",
        "engine_name": "Blink",
        "engine_version": "147.0.0.0",
        "os_name": "Windows",
        "os_version": "10",
        "device_memory": "32",
        "platform": "PC",
        "downlink": "10",
        "effective_type": "4g",
        "round_trip_time": "0",
    }

    DEFAULT_HEADERS = {
        "accept": "application/json, text/plain, */*",
        "user-agent": DEFAULT_USER_AGENT,
        "sec-ch-ua": '"Google Chrome";v="147", "Not.A/Brand";v="8", "Chromium";v="147"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "referer": "https://www.douyin.com/",
    }

    def __init__(self, cookie_path: str = None, log_prefix: str = "dy_sdk",
                 cookie_jar: CookieJar | None = None):
        self._log_prefix = log_prefix

        # ── Cookie data layer ────────────────────────────────
        # If caller passed in a jar that's already a DouyinCookieJar
        # (or any object exposing the same API), use it directly.
        # Otherwise build one from cookie_path. The strongly-typed jar
        # lives on ``self._jar`` (douyin-internal); BaseHttpClient also
        # holds a CookieJar-typed view via its ``cookie_jar`` property.
        if cookie_jar is None or not isinstance(cookie_jar, DouyinCookieJar):
            _base = Path(__file__).parent
            resolved_path = cookie_path or str(_base / "data" / "cookie.json")
            self._jar: DouyinCookieJar = DouyinCookieJar(resolved_path)
        else:
            self._jar = cookie_jar

        # ── Health & refresh ─────────────────────────────────
        self.health = HealthTracker(log_prefix=f"{log_prefix}.health")
        self.refresh = RefreshOrchestrator(
            jar=self._jar,
            health=self.health,
            log_prefix=f"{log_prefix}.refresh",
        )

        # ── a_bogus signer ───────────────────────────────────
        self._abogus = ABogus(user_agent=DEFAULT_USER_AGENT)

        # BaseHttpClient stores the jar (the douyin one — it satisfies
        # CookieJar Protocol) and calls _setup_sessions().
        super().__init__(cookie_jar=self._jar)

    def _setup_sessions(self) -> None:
        """Allocate the httpx.Client (HTTP/2)."""
        self._client = httpx.Client(
            http2=True,
            timeout=15.0,
            follow_redirects=True,
        )

    #: JS snippet for BBA login polling — runs in browser page via
    #: ``page.evaluate()``.  Returns ``{ ok: bool, reason: str, extras: {} }``.
    #: Much faster and non-flickering compared to ``page.content()`` + Python
    #: parsing (no full DOM serialization).
    BROWSER_LOGIN_CHECK_JS = """\
(() => {
  const rd = document.getElementById('RENDER_DATA');
  if (rd) {
    try {
      const decoded = decodeURIComponent(rd.textContent);
      const m = decoded.match(/"isLogin"\\s*:\\s*(true|false)/);
      if (m) {
        const ok = m[1] === 'true';
        let uid = '';
        const um = decoded.match(/"uid"\\s*:\\s*"(\\d+)"/);
        if (um) uid = um[1];
        return { ok, extras: uid ? { uid } : {}, reason: ok ? '' : 'isLogin=false' };
      }
    } catch(e) {}
  }
  try {
    const st = window.__INITIAL_STATE__;
    if (st && st.user && typeof st.user.isLogin === 'boolean') {
      const ok = st.user.isLogin === true;
      let uid = '';
      try { uid = String((st.user.info && st.user.info.uid) || ''); } catch(e) {}
      return { ok, extras: uid ? { uid } : {}, reason: ok ? '' : 'isLogin=false' };
    }
  } catch(e) {}
  return { ok: false, extras: {}, reason: 'isLogin field not found in SSR data' };
})()
"""

    @staticmethod
    def check_login_from_html(html: str) -> tuple[bool, dict]:
        """Check douyin login status from page HTML.

        Parses the SSR-embedded ``user.isLogin`` from ``RENDER_DATA``
        or ``__INITIAL_STATE__``.

        Shared by ``probe()`` and BBA login polling.
        """
        import re as _re
        import urllib.parse as _urlparse

        is_login = None
        uid = ""

        # Method 1: <script id="RENDER_DATA">…</script>
        m = _re.search(
            r'<script[^>]*id=["\']RENDER_DATA["\'][^>]*>(.*?)</script>',
            html, _re.DOTALL,
        )
        if m:
            try:
                decoded = _urlparse.unquote(m.group(1))
                lm = _re.search(r'"isLogin"\s*:\s*(true|false)', decoded)
                if lm:
                    is_login = lm.group(1) == "true"
                um = _re.search(r'"uid"\s*:\s*"(\d+)"', decoded)
                if um:
                    uid = um.group(1)
            except Exception:
                pass

        # Method 2: window.__INITIAL_STATE__ = {…}
        if is_login is None:
            m = _re.search(
                r'self\.__INITIAL_STATE__\s*=\s*({.*?})\s*;?\s*</script',
                html, _re.DOTALL,
            )
            if m:
                try:
                    import json as _json
                    state = _json.loads(m.group(1))
                    user = state.get("user", {})
                    if isinstance(user, dict):
                        is_login = user.get("isLogin") is True
                        uid = str(user.get("info", {}).get("uid", ""))
                except Exception:
                    pass

        # Method 3: last resort regex on raw HTML
        if is_login is None:
            lm = _re.search(r'isLogin%22%3A(true|false)', html)
            if lm:
                is_login = lm.group(1) == "true"
            else:
                lm = _re.search(r'"isLogin"\s*:\s*(true|false)', html)
                if lm:
                    is_login = lm.group(1) == "true"

        if is_login is None:
            return False, {"reason": "isLogin field not found in SSR data"}

        extras = {}
        if uid:
            extras["uid"] = uid
        if is_login:
            return True, extras
        else:
            return False, {"reason": "isLogin=false (cookie expired or not logged in)", **extras}

    def probe(self, task_type: str = "default") -> ProbeResult:
        """Probe cookie validity via SSR page ``isLogin`` field.

        Requests ``/jingxuan`` and parses the SSR-embedded ``user.isLogin``
        from ``RENDER_DATA`` / ``__INITIAL_STATE__``.  No API signing
        required — a plain GET with cookies suffices.

        ``task_type`` is accepted for ``CapabilityProbe`` Protocol
        compatibility but is ignored; login state is the same for all
        task types.
        """
        t0 = time.time()
        url = f"{self.BASE_URL}/jingxuan"
        headers = dict(self.DEFAULT_HEADERS)
        headers["cookie"] = self._jar.as_string()

        try:
            resp = self._client.get(url, headers=headers, follow_redirects=True)
            latency = round((time.time() - t0) * 1000)

            if resp.status_code != 200:
                return ProbeResult(
                    ok=False, api="/jingxuan", latency_ms=latency,
                    error=f"HTTP {resp.status_code}",
                )

            is_logged_in, extras = self.check_login_from_html(resp.text)
            return ProbeResult(
                ok=is_logged_in,
                api="/jingxuan",
                latency_ms=latency,
                error=None if is_logged_in else extras.get("reason", "not logged in"),
                extras=extras,
            )

        except Exception as e:
            return ProbeResult(
                ok=False, api="/jingxuan",
                latency_ms=round((time.time() - t0) * 1000),
                error=str(e),
            )

    def _log(self, msg: str):
        print(f"[{self._log_prefix}] {msg}", file=sys.stderr)

    # ── Cookie Management (delegate to DouyinCookieJar) ──────

    def check_login(self) -> dict:
        """Check if current cookies are valid (logged in).

        Returns:
            dict with 'is_login' field.
        """
        if not self._jar.is_logged_in():
            return {"is_login": False, "error": "No session cookie found"}
        # Try an actual API call to verify
        try:
            data = self._get("/aweme/v1/web/query/user/", {
                "publish_video_strategy_type": "2",
            })
            user_info = data.get("user", {}) or data.get("data", {}).get("user", {})
            is_login = bool(user_info.get("uid") or user_info.get("nickname"))
            return {
                "is_login": is_login,
                "nickname": user_info.get("nickname", ""),
                "uid": user_info.get("uid", ""),
            }
        except Exception as e:
            self._log(f"Login check failed: {e}")
            return {"is_login": False, "error": str(e)}

    def search_live_rooms(
        self,
        browser_session,
        keyword: str,
        max_results: int = 20,
        live_only: bool = False,
        sort_by: str = "default",
    ) -> list[dict]:
        """Search Douyin live rooms using daemon-managed browser bootstrap.

        Args:
            live_only: Drop offline rooms (status != 2) client-side.
            sort_by: ``"default"`` | ``"user_count"`` (descending popularity).
        """
        return search_douyin_live_rooms(
            browser_session, keyword, int(max_results),
            live_only=bool(live_only), sort_by=str(sort_by),
        )

    def get_live_room_info(self, browser_session, web_rid_or_url: str) -> dict:
        """Fetch Douyin live room detail using daemon-managed browser bootstrap."""
        return get_douyin_live_room_info(browser_session, web_rid_or_url)

    def collect_live_events(
        self,
        *,
        browser_provider,
        web_rid: str,
        duration_seconds: float,
        on_event,
        is_cancelled=None,
        event_filter: set[str] | None = None,
        wait_until_live: bool = False,
        wait_timeout_seconds: float = 86400.0,
    ) -> int:
        """Collect Douyin live WSS events after browser-assisted bootstrap.

        R7 §10.3: wait_until_live 单 hold 包整个 while 循环（避免反复冷启动 chrome）。
        bootstrap 完后 hold 退出 → chrome 关闭 → 24h WSS 纯 Python 跑。

        Stop conditions (auto):
          1. duration_seconds reached
          2. is_cancelled() returns True
          3. WSS connection closed
          4. WebcastControlMessage status=3 (broadcaster ended live)
        """
        parsed_web_rid = parse_web_rid(web_rid)

        # ── Optional: wait for room to go live ──
        # R7 §10.3: 整个 wait 循环包在一个 hold 里
        # 期间 page 持续活着（无冷启停），sleep 30s 期间 page 闲置但 owned
        if wait_until_live:
            wait_deadline = time.monotonic() + max(0.0, float(wait_timeout_seconds))
            poll_interval = 30.0
            went_live = False
            with browser_provider.hold() as bba_page:
                while time.monotonic() < wait_deadline:
                    if is_cancelled and is_cancelled():
                        return 0
                    try:
                        info = get_douyin_live_room_info(bba_page, parsed_web_rid)
                        if int(info.get("status") or 0) == 2:
                            went_live = True
                            break
                    except Exception:
                        pass
                    # Sleep in 1s steps so cancellation is responsive
                    slept = 0.0
                    while slept < poll_interval:
                        if is_cancelled and is_cancelled():
                            return 0
                        time.sleep(1.0)
                        slept += 1.0
            if not went_live:
                return 0
            # hold 退出 → page/chrome 关闭

        # ── Bootstrap：单 hold，拿 WSS handover 后退出 ──
        with browser_provider.hold() as bba_page:
            wss_url, cookie_header = capture_push_wss(bba_page, parsed_web_rid)
        # hold 退出 → page/chrome 关闭，剩下 24h 纯 Python WSS

        return collect_live_protocol_events(
            wss_url=wss_url,
            cookie_header=cookie_header,
            web_rid=parsed_web_rid,
            duration_seconds=duration_seconds,
            on_event=on_event,
            is_cancelled=is_cancelled,
            event_filter=event_filter,
        )

    def save_cookies_from_browser(self, cookie_string: str,
                                   extra_headers: dict = None,
                                   cookie_path: str = None):
        """Save cookies intercepted from browser (e.g. via Playwright).

        Replaces the in-memory cookie blob with the freshly captured one
        and persists it to disk via the jar's write path.
        """
        # Parse the raw cookie header into a dict
        cookie_dict: dict[str, str] = {}
        for chunk in (cookie_string or "").split(";"):
            chunk = chunk.strip()
            if "=" in chunk:
                k, v = chunk.split("=", 1)
                cookie_dict[k.strip()] = v.strip()

        self._jar.replace_all(
            cookie_dict,
            extra_headers=extra_headers or {},
            extra_params=self._jar.extra_params,
        )
        self._jar.save()
        self.health.reset()
        self._log("Cookies saved from browser capture")

    # ── HTTP Helpers ─────────────────────────────────────────

    def _build_params(self, extra_params: dict = None) -> dict:
        """Build full params dict (common + dynamic + extra)."""
        params = dict(self.COMMON_PARAMS)
        jar = self._jar

        # Add dynamic params from cookies / extra_params
        if jar.webid:
            params["webid"] = jar.webid
        if jar.uifid:
            params["uifid"] = jar.uifid
        if jar.verify_fp:
            params["verifyFp"] = jar.verify_fp
            params["fp"] = jar.verify_fp
        if jar.ms_token:
            params["msToken"] = jar.ms_token
        else:
            params["msToken"] = ""

        if extra_params:
            params.update(extra_params)

        return params

    def _sign_and_build_url(self, path: str, params: dict, method: str = "GET") -> str:
        """Build URL with common params and a_bogus signature."""
        full_params = self._build_params(params)

        # Generate a_bogus from the query string (before adding a_bogus itself)
        params_str = urlencode(full_params, safe='=', quote_via=quote)
        a_bogus = self._abogus.get_value(params_str, method)
        full_params["a_bogus"] = a_bogus

        return f"{self.BASE_URL}{path}?{urlencode(full_params, safe='=', quote_via=quote)}"

    def _get(self, path: str, params: dict = None, referer: str = None) -> dict:
        """Make a signed GET request and return JSON response."""
        url = self._sign_and_build_url(path, params or {})

        headers = dict(self.DEFAULT_HEADERS)
        headers["cookie"] = self._jar.as_string()
        if referer:
            headers["referer"] = referer

        try:
            resp = self._client.get(url, headers=headers)
            resp.raise_for_status()

            # Check content type - Douyin may return HTML instead of JSON
            content_type = resp.headers.get("content-type", "")
            if "text/html" in content_type:
                self._log(f"Got HTML instead of JSON from {path}, body[:200]: {resp.text[:200]}")
                self.health.record_failure()
                return {"status_code": -1, "status_msg": "Received HTML instead of JSON"}

            body = resp.text.strip()
            if not body:
                self._log(f"Empty response from {path}")
                return {"status_code": -1, "status_msg": "Empty response"}

            # Handle chunked transfer encoding prefix (e.g. "706\n{json}\n0\n")
            if body[0] != '{' and body[0] != '[':
                json_start = body.find('{')
                if json_start == -1:
                    json_start = body.find('[')
                if json_start != -1:
                    body = body[json_start:]
                    json_end = max(body.rfind('}'), body.rfind(']'))
                    if json_end != -1:
                        body = body[:json_end + 1]

            import json as json_mod
            data = json_mod.loads(body)

            # Check API-level status
            status_code = data.get("status_code")
            if status_code == 0:
                self.health.record_success()
            elif status_code in (8, 9, 2483):
                # Common error codes indicating auth issues
                self.health.record_failure()
                self._log(
                    f"API error: status_code={status_code}, "
                    f"msg={data.get('status_msg', '')}"
                )

            return data

        except httpx.HTTPStatusError as e:
            self.health.record_failure()
            self._log(f"HTTP error from {path}: {e.response.status_code}")
            raise
        except Exception as e:
            self.health.record_failure()
            self._log(f"Request failed for {path}: {e}")
            raise

    # ── Comments ─────────────────────────────────────────────


    def comment_list(self, aweme_id: str, cursor: int = 0,
                    count: int = 20, sort: int = 0) -> dict:
        """Get root comments for a video."""
        params = {
            "aweme_id": aweme_id,
            "cursor": str(cursor),
            "count": str(count),
            "item_type": str(sort),
            "insert_ids": "",
            "whale_cut_token": "",
            "cut_version": "1",
            "rcFT": "",
            "pc_img_format": "webp",
        }
        referer = f"https://www.douyin.com/video/{aweme_id}"
        return self._get("/aweme/v1/web/comment/list/", params, referer=referer)

    def comment_reply_list(self, aweme_id: str, comment_id: str,
                           cursor: int = 0, count: int = 20) -> dict:
        """Get replies (sub-comments) for a root comment."""
        params = {
            "item_id": aweme_id,
            "comment_id": comment_id,
            "cut_version": "1",
            "cursor": str(cursor),
            "count": str(count),
            "item_type": "0",
            "version_code": "170400",
            "version_name": "17.4.0",
        }
        referer = f"https://www.douyin.com/video/{aweme_id}"
        return self._get("/aweme/v1/web/comment/list/reply/", params, referer=referer)

    # ── Video Detail ─────────────────────────────────────────

    def video_detail(self, aweme_id: str) -> dict:
        """Get video detail info."""
        params = {
            "aweme_id": aweme_id,
            "request_source": "600",
            "origin_type": "quick_player",
        }
        referer = f"https://www.douyin.com/video/{aweme_id}"
        return self._get("/aweme/v1/web/aweme/detail/", params, referer=referer)


# R4 alias - DouyinClient is the canonical name going forward;
# DouyinSDK is preserved for backward compatibility with R3 callers.
DouyinClient = DouyinSDK
