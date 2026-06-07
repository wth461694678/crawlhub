#!/usr/bin/env python3
"""
快手爬虫 SDK — 统一编程接口
============================
所有爬虫能力通过此模块对外暴露，支持：
  - 程序化调用（import 后直接调用方法）
  - 命令行调用（python ks_sdk.py search 王者）
  - 多线程扩展（每个线程持有独立 KuaishouSDK 实例）

用法:
  from ks_sdk import KuaishouSDK

  sdk = KuaishouSDK()
  videos = sdk.search_feed("王者荣耀")
  rooms = sdk.live_rooms(game_id="1001")
  sdk.start_danmu("b5HbRwzDZrA", duration=60)
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from crawlhub.core.platform import (
    BaseHttpClient, CookieJar, ProbeResult,
)

from ._internal.cookie_jar import KuaishouCookieJar
from ._internal.ks_session import KuaishouSession





# ════════════════════════════════════════════════════════════
#  KuaishouSDK
# ════════════════════════════════════════════════════════════

class KuaishouSDK(BaseHttpClient):
    """快手爬虫 SDK — 一个实例 = 一个浏览器身份。

    R4 P9 (2026-05-24):
      * extends ``BaseHttpClient`` (Protocol over impl)
      * ``_setup_sessions`` allocates the inner ``KuaishouSession``

    R4 P13 (2026-05-25):
      * ``probe`` is now self-contained — issues its own httpx POST to
        ``/rest/v/feed/liked`` instead of round-tripping through the
        deprecated legacy probe module (now removed).

    R4 P12 + R5 (2026-05-25):
      * SDK owns its ``KuaishouCookieJar`` and passes it down to
        ``KuaishouSession``. Probe and ``check_cookie_status`` read
        cookies via the jar (single source of truth).
    """

    def __init__(self, cookie_path=None, log_prefix="sdk", log_dir=None,
                 cookie_jar: CookieJar | None = None):
        # log_dir is optional — RequestLogger is now default-off (noop when None).
        # Resolve / build the jar up-front so SDK and Session share it.

        if cookie_jar is None:
            resolved_path = cookie_path or str(
                Path(__file__).resolve().parent / ".." / "data" / "cookie_full.json"
            )
            cookie_jar = KuaishouCookieJar(resolved_path)

        # Stash construction params on self so _setup_sessions (called
        # from BaseHttpClient.__init__) can reach them.
        self._cookie_path = cookie_path
        self._log_prefix = log_prefix
        self._log_dir = log_dir

        # BaseHttpClient stores cookie_jar (optional) and triggers
        # _setup_sessions() which builds self.session.
        super().__init__(cookie_jar=cookie_jar)

    def _setup_sessions(self) -> None:
        """Allocate the inner ``KuaishouSession`` (sharing our jar)."""
        self.session = KuaishouSession(
            cookie_path=self._cookie_path,
            log_prefix=self._log_prefix,
            log_dir=self._log_dir,
            cookie_jar=self.cookie_jar,
        )

    #: JS snippet for BBA login polling — runs in browser page via
    #: ``page.evaluate()``.  Returns ``{ ok: bool, reason: str, extras: {} }``.
    BROWSER_LOGIN_CHECK_JS = """\
(() => {
  const el = document.querySelector('.sidebar-login-button');
  const ok = el === null;
  return { ok, extras: {}, reason: ok ? '' : 'sidebar-login-button found' };
})()
"""

    @staticmethod
    def check_login_from_html(html: str) -> tuple[bool, dict]:
        """Check kuaishou login status from page HTML.

        If ``sidebar-login-button`` CSS class is absent → logged in.
        Shared by ``probe()`` and BBA login polling.
        """
        from crawlhub.core.platform.probe_protocol import check_login_from_html
        return check_login_from_html(html, logged_out_class="sidebar-login-button")

    def probe(self, task_type: str = "default") -> ProbeResult:
        """Probe via the canonical login-check endpoint ``/rest/v/feed/liked``.

        Reverted (2026-06-05) from the homepage-HTML approach to the
        legacy ``feed/liked`` API after empirical regression: the
        homepage SSR HTML always renders the "logged-out" sidebar
        skeleton (login state is hydrated client-side by JS), so a
        pure-HTTP fetch can never see the logged-in indicator and
        wrongly reports VALID cookies as EXPIRED.

        Decision matrix (verified on production cookies 2026-05-14, refined 2026-05-29):
            * HTTP 200 + result=1                              -> VALID  (liked feed accessible)
            * HTTP 200 + result=109                            -> EXPIRED (kuaishou returned loginUrl)
            * HTTP 200 + result=2 / Intercept-Result header
                with "risk-control"                            -> RISK_CONTROL (账号/指纹被风控)
            * any other shape                                  -> INCONCLUSIVE (with raw payload in error)

        Cookie source: we read directly from ``self.cookie_jar`` (the
        single source of truth shared with the inner ``KuaishouSession``).
        We deliberately bypass ``KuaishouSession.request`` so the probe
        carries no sign / kpf headers it doesn't need.

        TLS 通道：必须走 http_factory（curl_cffi + ja3 + IPv4 强制）。
        切勿改回 httpx —— 此 probe 带 SSO Cookie 出网，用 OpenSSL 握手
        会触发风控异步级联失效（详见 http_factory.py 模块注释）。

        Note: ``BROWSER_LOGIN_CHECK_JS`` and ``check_login_from_html``
        are kept for the BBA browser-polling path (see
        ``playwright_runtime._extract_and_probe_cookie``); only the
        HTTP probe was reverted.
        """
        from ._internal.http_factory import make_session

        api_path = "/rest/v/feed/liked"
        start = time.time()
        try:
            main_cookies = self.cookie_jar.as_dict("main") if self.cookie_jar else {}
            if not main_cookies:
                return ProbeResult(
                    ok=False, api=api_path, latency_ms=0,
                    error="no kuaishou cookie loaded",
                    extras={"task_type": task_type},
                )

            sess = make_session(cookie_jar=self.cookie_jar)
            resp = sess.post(
                "https://www.kuaishou.com" + api_path,
                json={"pcursor": "", "page": "profile"},
                cookies=main_cookies,
                headers={
                    "Referer": "https://www.kuaishou.com/",
                    "Content-Type": "application/json",
                    "Origin": "https://www.kuaishou.com",
                    "Accept": "application/json",
                },
                timeout=10,
            )
            latency_ms = int((time.time() - start) * 1000)
            data = resp.json()
            result = data.get("result")
            intercept = (resp.headers.get("intercept-result") or "").lower()

            if result == 1:
                return ProbeResult(
                    ok=True, api=api_path, latency_ms=latency_ms,
                    error=None,
                    extras={"task_type": task_type, "result": result},
                )
            if result == 109:
                return ProbeResult(
                    ok=False, api=api_path, latency_ms=latency_ms,
                    error="kuaishou cookie expired (result=109)",
                    extras={"task_type": task_type, "result": result},
                )
            # 风控：服务端在 Intercept-Result header 明示 "risk-control"
            # （实测 2026-05-29: result=2 + Intercept-Result: risk-control;2 =>
            # 账号+IP+指纹组合被风控；cookie 本身没失效，但接口被拦）
            if "risk-control" in intercept or result == 2:
                return ProbeResult(
                    ok=False, api=api_path, latency_ms=latency_ms,
                    error=(
                        f"kuaishou risk-control triggered (result={result}, "
                        f"intercept-result={intercept!r}). "
                        f"Account/fingerprint flagged. Solutions: "
                        f"(1) wait 2-6h cooldown; (2) switch IP/cookie; "
                        f"(3) headful manual access to www.kuaishou.com once."
                    ),
                    extras={
                        "task_type": task_type, "result": result,
                        "intercept_result": intercept,
                        "request_id": data.get("request_id"),
                    },
                )
            return ProbeResult(
                ok=False, api=api_path, latency_ms=latency_ms,
                error=f"inconclusive response (result={result})",
                extras={"task_type": task_type, "result": result},
            )
        except Exception as e:
            latency_ms = int((time.time() - start) * 1000)
            return ProbeResult(
                ok=False, api=api_path, latency_ms=latency_ms,
                error=f"{type(e).__name__}: {e}",
                extras={"task_type": task_type},
            )

    # ──────────────────────────────────────────────────
    #  主站 API
    # ──────────────────────────────────────────────────

    def search_feed(self, keyword: str, pcursor: str = "") -> dict:
        """搜索视频。"""
        resp = self.session.request(
            "POST", "https://www.kuaishou.com/rest/v/search/feed",
            site="main", referer="https://www.kuaishou.com/search/video",
            json={"keyword": keyword, "page": "search", "webPageArea": "", "pcursor": pcursor},
        )
        return resp.json()

    def search_user(self, keyword: str, pcursor: str = "") -> dict:
        """搜索用户。"""
        resp = self.session.request(
            "POST", "https://www.kuaishou.com/rest/v/search/user",
            site="main", referer="https://www.kuaishou.com/search/user",
            json={"keyword": keyword, "pcursor": pcursor, "searchSessionId": ""},
        )
        return resp.json()

    def user_profile(self, user_id: str) -> dict:
        """获取用户主页信息。"""
        resp = self.session.request(
            "POST", "https://www.kuaishou.com/rest/v/profile/user",
            site="main", referer=f"https://www.kuaishou.com/profile/{user_id}",
            json={"user_id": user_id},
        )
        return resp.json()

    def user_feed(self, user_id: str, pcursor: str = "") -> dict:
        """获取用户作品列表。"""
        resp = self.session.request(
            "POST", "https://www.kuaishou.com/rest/v/profile/feed",
            site="main", referer=f"https://www.kuaishou.com/profile/{user_id}",
            json={"user_id": user_id, "pcursor": pcursor, "page": "profile"},
        )
        return resp.json()

    def video_detail(self, photo_id: str) -> dict:
        """获取视频详情。"""
        body = {
            "operationName": "visionVideoDetail",
            "variables": {"photoId": photo_id, "page": "detail"},
            "query": (
                "query visionVideoDetail($photoId: String, $page: String) {\n"
                "  visionVideoDetail(photoId: $photoId, page: $page) {\n"
                "    photo { id duration caption likeCount viewCount }\n"
                "    author { id name }\n    tags { type name }\n  }\n}"
            ),
        }
        resp = self.session.request(
            "POST", "https://www.kuaishou.com/graphql",
            site="main", referer=f"https://www.kuaishou.com/short-video/{photo_id}",
            json=body, extra_headers={"accept": "*/*"},
        )
        return resp.json()

    def comment_list(self, photo_id: str, pcursor: str = "") -> dict:
        """获取视频评论列表。"""
        body = {
            "operationName": "commentListQuery",
            "variables": {"photoId": photo_id, "pcursor": pcursor},
            "query": (
                "query commentListQuery($photoId: String, $pcursor: String) {\n"
                "  visionCommentList(photoId: $photoId, pcursor: $pcursor) {\n"
                "    commentCount commentCountV2 pcursor pcursorV2\n"
                "    rootCommentsV2 {\n"
                "      commentId authorId authorName content headurl timestamp\n"
                "      hasSubComments likedCount liked status __typename\n"
                "    }\n    __typename\n  }\n}"
            ),
        }
        resp = self.session.request(
            "POST", "https://www.kuaishou.com/graphql",
            site="main", referer=f"https://www.kuaishou.com/short-video/{photo_id}",
            json=body, extra_headers={"accept": "*/*"},
        )
        return resp.json()

    def sub_comment_list(self, photo_id: str, root_comment_id: str, pcursor: str = "") -> dict:
        """获取评论的子评论（回复）列表。"""
        body = {
            "operationName": "visionSubCommentList",
            "variables": {"photoId": photo_id, "rootCommentId": root_comment_id, "pcursor": pcursor},
            "query": (
                "mutation visionSubCommentList($photoId: String, $rootCommentId: String, $pcursor: String) {\n"
                "  visionSubCommentList(photoId: $photoId, rootCommentId: $rootCommentId, pcursor: $pcursor) {\n"
                "    pcursor pcursorV2\n"
                "    subCommentsV2 {\n"
                "      commentId authorId authorName content headurl timestamp\n"
                "      hasSubComments likedCount liked status replyToUserName replyTo __typename\n"
                "    }\n    __typename\n  }\n}"
            ),
        }
        resp = self.session.request(
            "POST", "https://www.kuaishou.com/graphql",
            site="main", referer=f"https://www.kuaishou.com/short-video/{photo_id}",
            json=body, extra_headers={"accept": "*/*"},
        )
        return resp.json()

    def verify_login(self) -> dict:
        """验证登录状态。"""
        resp = self.session.request(
            "GET", "https://www.kuaishou.com/rest/v/profile/get",
            site="main", sign_path="/rest/v/profile/get", sign_query={"caver": "2"},
        )
        return resp.json()

    # Known test data for probing
    _PROBE_PHOTO_ID = "3xp334ga5a9z7wi"

    # Task type → required capabilities
    _TASK_CAPABILITY_MAP = {
        "comment": ["comment_list"],
        "video_detail": ["video_detail"],
        "search": ["search"],
    }

    def check_cookie_status(self, task_type: str = None) -> dict:
        """Check cookie health, optionally probing for a specific task type.

        When task_type is None, performs local-only checks (no HTTP requests)
        and returns estimated capabilities based on token presence.

        When task_type is provided, performs a real API probe for that specific
        task to confirm the cookie can actually do the job.

        Args:
            task_type: Optional. One of: "comment", "video_detail", "search".

        Returns:
            dict with cookie status, capabilities, and recommendations.
        """
        import time
        from pathlib import Path

        result = {
            "cookie_id": "default",
            "cookie_file": self.cookie_jar.source(),
        }

        # ── Local token inspection ───────────────────────────
        cookie_file = Path(self.cookie_jar.source())
        cookie_age_hours = None
        if cookie_file.exists():
            mtime = cookie_file.stat().st_mtime
            cookie_age_hours = round((time.time() - mtime) / 3600, 1)

        main_cookies = self.cookie_jar.as_dict("main")

        tokens = {
            "webday7_st": bool(main_cookies.get("kuaishou.server.webday7_st")),
            "passToken": bool(main_cookies.get("passToken")),
            "userId": bool(main_cookies.get("userId")),
            "did": bool(self.session.did),
        }

        session_valid = tokens["webday7_st"]
        result["session_valid"] = session_valid
        result["cookie_age_hours"] = cookie_age_hours
        result["tokens"] = tokens

        # ── No task_type: return local estimates ─────────────
        if task_type is None:
            estimated = {}
            if session_valid:
                estimated["comment"] = "likely_ok"
                estimated["video_detail"] = "likely_ok"
                estimated["search"] = "likely_ok"
            else:
                estimated["comment"] = "likely_failed"
                estimated["video_detail"] = "likely_failed"
                estimated["search"] = "likely_failed"

            result["estimated_capabilities"] = estimated

            issues = []
            if not session_valid:
                issues.append("No webday7_st session token — need login")
            if not tokens["did"]:
                issues.append("No did — identity not established")
            if not tokens["passToken"]:
                issues.append("No passToken — live features may fail")

            result["recommendation"] = "; ".join(issues) if issues else "All tokens look healthy."
            return result

        # ── task_type provided: do a real probe ──────────────
        if task_type not in self._TASK_CAPABILITY_MAP:
            result["error"] = f"Unknown task_type: {task_type}. Valid: {list(self._TASK_CAPABILITY_MAP.keys())}"
            return result

        result["task_type"] = task_type
        probe_result = self._probe_capability(task_type)
        result["capable"] = probe_result["ok"]
        result["probe_result"] = probe_result

        if probe_result["ok"]:
            result["recommendation"] = f"Cookie is ready for {task_type} tasks."
        else:
            result["recommendation"] = (
                f"Cookie cannot do {task_type}: {probe_result.get('error', 'unknown')}. "
                f"Try send_sms_code + verify_sms_code to re-login."
            )

        return result

    def _probe_capability(self, task_type: str) -> dict:
        """Actually probe a specific capability with a real API call."""
        import time
        t0 = time.time()

        if task_type == "comment":
            try:
                data = self.comment_list(self._PROBE_PHOTO_ID)
                latency = round((time.time() - t0) * 1000)
                cl = (data.get("data") or {}).get("visionCommentList")
                if cl is not None:
                    return {"ok": True, "api": "graphql/commentListQuery", "latency_ms": latency}
                else:
                    errors = data.get("errors", [])
                    err_msg = errors[0].get("message", "") if errors else "No visionCommentList in response"
                    return {"ok": False, "api": "graphql/commentListQuery",
                            "latency_ms": latency, "error": err_msg}
            except Exception as e:
                return {"ok": False, "api": "graphql/commentListQuery",
                        "latency_ms": round((time.time() - t0) * 1000), "error": str(e)}

        elif task_type == "video_detail":
            try:
                data = self.video_detail(self._PROBE_PHOTO_ID)
                latency = round((time.time() - t0) * 1000)
                vd = (data.get("data") or {}).get("visionVideoDetail")
                if vd and vd.get("photo"):
                    return {"ok": True, "api": "graphql/visionVideoDetail", "latency_ms": latency}
                else:
                    errors = data.get("errors", [])
                    err_msg = errors[0].get("message", "") if errors else "No video detail in response"
                    return {"ok": False, "api": "graphql/visionVideoDetail",
                            "latency_ms": latency, "error": err_msg}
            except Exception as e:
                return {"ok": False, "api": "graphql/visionVideoDetail",
                        "latency_ms": round((time.time() - t0) * 1000), "error": str(e)}

        elif task_type == "search":
            try:
                data = self.search_feed("test")
                latency = round((time.time() - t0) * 1000)
                if data.get("feeds") is not None or data.get("result") == 1:
                    return {"ok": True, "api": "/rest/v/search/feed", "latency_ms": latency}
                else:
                    return {"ok": False, "api": "/rest/v/search/feed",
                            "latency_ms": latency,
                            "error": f"result={data.get('result')}, msg={data.get('error_msg', '')}"}
            except Exception as e:
                return {"ok": False, "api": "/rest/v/search/feed",
                        "latency_ms": round((time.time() - t0) * 1000), "error": str(e)}

        return {"ok": False, "error": f"No probe implemented for {task_type}"}

    # ──────────────────────────────────────────────────
    #  直播站 API
    # ──────────────────────────────────────────────────

    def live_categories(self, type_index="4", page=1, page_size=20) -> dict:
        """获取直播分类列表。"""
        resp = self.session.request(
            "GET", "https://live.kuaishou.com/live_api/category/classify",
            site="live",
            params={"type": type_index, "source": "2",
                    "page": str(page), "pageSize": str(page_size)},
        )
        return resp.json()

    def live_rooms(self, game_id: str, page=1, page_size=20) -> dict:
        """获取游戏直播间列表（纯 Python 签名，无需浏览器）。"""
        query = {"filterType": "0", "gameId": str(game_id),
                 "page": str(page), "pageSize": str(page_size)}
        resp = self.session.request(
            "GET", "https://live.kuaishou.com/live_api/gameboard/list",
            site="live", referer=f"https://live.kuaishou.com/cate/SYXX/{game_id}",
            params=query,
            sign_path="/live_api/gameboard/list",
            sign_query=query,
        )
        return resp.json()


    def websocket_info(self, live_stream_id: str) -> dict:
        """获取 WebSocket 连接信息。"""
        resp = self.session.request(
            "GET", "https://live.kuaishou.com/live_api/liveroom/websocketinfo",
            site="live",
            params={"liveStreamId": live_stream_id, "caver": "2"},
            sign_path="/live_api/liveroom/websocketinfo",
            sign_query={"liveStreamId": live_stream_id, "caver": "2"},
        )
        return resp.json()

    # ──────────────────────────────────────────────────
    #  直播间解析
    # ──────────────────────────────────────────────────

    def resolve_stream_id(self, url_or_id: str) -> dict:
        """解析直播间 URL/ID → 直播间信息 dict。

        Returns:
            {
                "liveStreamId": str,
                "principalId": str,
                "isLiving": bool,
                "authorName": str,
            }
        """
        info = self._parse_live_page(url_or_id)
        # 从输入中提取纯 principal_id（_parse_live_page 内部已做同样提取）
        if url_or_id.startswith("http"):
            principal_id = url_or_id.rstrip("/").split("/")[-1]
        else:
            principal_id = url_or_id
        return {
            "liveStreamId": info.get("liveStreamId", ""),
            "principalId": principal_id,
            "isLiving": info.get("isLiving", False),
            "authorName": info.get("authorName", ""),
        }

    def _parse_live_page(self, principal_id: str) -> dict:
        """SSR 页面解析获取 liveStreamId。

        ``principal_id`` 可以是纯 ID（如 ``xzx11234``）或完整 URL
        （如 ``https://live.kuaishou.com/u/xzx11234``）。
        """
        # 兼容完整 URL 输入：提取最后一段路径作为真实 principal_id
        if principal_id.startswith("http"):
            principal_id = principal_id.rstrip("/").split("/")[-1]
        url = f"https://live.kuaishou.com/u/{principal_id}"
        result = {"liveStreamId": "", "isLiving": False, "authorName": ""}
        try:
            resp = self.session._logged_request("GET", url, headers={
                "User-Agent": self.session.profile["ua"],
                "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9",
            }, timeout=15)

            print(f"[INFO] _parse_live_page({principal_id}): HTTP {resp.status_code}, "
                  f"body_len={len(resp.text)}, url={resp.url}")

            # 保存 HTML 到 log_dir 便于离线排查
            if self._log_dir:
                try:
                    from pathlib import Path as _P
                    dump = _P(self._log_dir) / f"ssr_{principal_id}.html"
                    dump.write_text(resp.text, encoding="utf-8")
                    print(f"[INFO] _parse_live_page: HTML dumped to {dump}")
                except Exception:
                    pass

            m = re.search(r"window\.__INITIAL_STATE__\s*=\s*(.+?)</script>", resp.text, re.DOTALL)
            if not m:
                print(f"[WARN] _parse_live_page({principal_id}): __INITIAL_STATE__ not found, "
                      f"resp.status={resp.status_code}, body_len={len(resp.text)}")
                return result
            raw = m.group(1).strip().rstrip(";").replace("undefined", "null")
            raw = re.sub(r",\s*}", "}", raw)
            raw = re.sub(r",\s*]", "]", raw)
            depth, end = 0, 0
            for i, ch in enumerate(raw):
                if ch == "{": depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0: end = i + 1; break
            data = json.loads(raw[:end])
            pl = data.get("liveroom", {}).get("playList", [])
            # 优先从 playList[0] 取 isLiving，兜底取 liveroom 顶层
            raw_is_living = None
            item = None
            if pl:
                item = pl[0]
                raw_is_living = item.get("isLiving")
            if raw_is_living is None:
                raw_is_living = data.get("liveroom", {}).get("isLiving")
            # 防御性转换：JSON 中 isLiving 可能是 bool / int / str
            if isinstance(raw_is_living, str):
                result["isLiving"] = raw_is_living.lower() in ("true", "1", "yes")
            elif isinstance(raw_is_living, (int, float)):
                result["isLiving"] = bool(raw_is_living)
            elif raw_is_living is not None:
                result["isLiving"] = bool(raw_is_living)
            # 从 playList[0] 提取 liveStreamId 和 authorName
            if item:
                ls = item.get("liveStream")
                if isinstance(ls, dict):
                    result["liveStreamId"] = ls.get("id", "")
                author = item.get("author")
                if isinstance(author, dict):
                    result["authorName"] = author.get("name", "")
            if not pl:
                print(f"[WARN] _parse_live_page({principal_id}): playList is empty, "
                      f"liveroom keys={list(data.get('liveroom', {}).keys())}")
        except Exception as e:
            print(f"[WARN] SSR 解析失败: {e}")
        return result

    # ──────────────────────────────────────────────────
    #  Hybrid live (browser bootstrap → Python WS)
    # ──────────────────────────────────────────────────

    def list_live_categories_hybrid(
        self,
        browser_session,
        *,
        is_cancelled=None,
    ) -> list[dict]:
        """List ALL kuaishou live categories (paged crawl of /live_api/category/data).

        Hybrid: borrow browser ONCE to capture signature (kww + cookie),
        then httpx-replay /category/data?type=1&source=2&page=N&pageSize=12
        repeatedly until ``hasMore=False`` or no new items.

        ``browser_session`` is a ``BrowserSessionHandle`` (lease_policy=action;
        daemon manages the lease lifecycle, scraper just uses the handle).

        Only the ``kww`` header is required for this endpoint, so we tell
        the signature capture to NOT wait for hxfalcon (which takes 30s+).
        """
        from ._internal.live_protocol import (
            capture_live_signature, list_all_live_categories as _impl,
        )
        sig = capture_live_signature(browser_session, require_hxfalcon=False)
        return _impl(
            sig,
            cookie_jar=self.cookie_jar,
            is_cancelled=is_cancelled,
        )

    def search_live_categories_hybrid(
        self,
        browser_session,
        keyword: str,
    ) -> list[dict]:
        """Search live categories by keyword (/live_api/category/search).

        Hybrid: capture signature (kww-only is enough; no hxfalcon needed),
        then single-shot httpx replay.
        """
        from ._internal.live_protocol import (
            capture_live_signature, search_live_categories as _impl,
        )
        sig = capture_live_signature(browser_session, require_hxfalcon=False)
        return _impl(sig, keyword, cookie_jar=self.cookie_jar)

    def list_category_live_rooms_hybrid(
        self,
        browser_session,
        category_id: str,
        *,
        max_results: int = 100,
        page_size: int = 20,
        is_cancelled=None,
    ) -> list[dict]:
        """List live rooms inside a category (/live_api/gameboard/list?gameId=...).

        Despite the 'hybrid' name (kept for interface compatibility), this
        method now runs PURE PYTHON — the existing KuaishouSession.request()
        signs __NS_hxfalcon internally via HxFalconSigner (already implemented
        in _internal/ks_hxfalcon.py). No browser required.

        browser_session is accepted but unused (daemon injects it because
        plugin.yaml says runtime=browser_backed; we simply don't touch it).
        """
        from ._internal.live_protocol import _slim_gameboard_room, _parse_count

        rows: list[dict] = []
        seen_ids: set[str] = set()
        page_no = 1
        while len(rows) < max_results:
            if is_cancelled and is_cancelled():
                break
            body = self.live_rooms(str(category_id), page=page_no, page_size=page_size)
            data = body.get("data") if isinstance(body, dict) else None
            items = data.get("list") if isinstance(data, dict) else []
            if not items:
                break
            for it in items:
                slim = _slim_gameboard_room(it, str(category_id))
                sid = slim.get("live_stream_id") or ""
                if not sid or sid in seen_ids:
                    continue
                seen_ids.add(sid)
                rows.append(slim)
                if len(rows) >= max_results:
                    break
            if isinstance(data, dict) and data.get("hasMore") is False:
                break
            page_no += 1
            import time as _t
            _t.sleep(0.4)
        return rows[:max_results]







    def get_live_room_info_hybrid(self, browser_session, principal_id: str) -> dict:

        """Best-effort first-screen live room snapshot via daemon-managed browser.

        ``browser_session`` is a ``BrowserSessionHandle``; we go through
        the ``_internal.live_protocol`` helper to read NUXT state + DOM.
        """
        from ._internal.live_protocol import get_live_room_info as _impl
        return _impl(browser_session, principal_id)

    def collect_live_events_hybrid(
        self,
        *,
        browser_provider,
        principal_id: str,
        duration_seconds: float,
        on_event,
        is_cancelled=None,
        event_filter: set[str] | None = None,
        wait_until_live: bool = False,
        wait_timeout_seconds: float = 86400.0,
    ) -> int:
        """Hybrid live event collection.

        R7 §10.3 (updated): wait_until_live 改用 SSR 页面纯 HTTP 轮询，
        不再 hold 浏览器。bootstrap 阶段仍需浏览器拿 handover。

        Bootstrap: open the live page in the daemon's stealth browser,
        capture ``websocketinfo`` (token + ws_urls) + cookie jar.
        Run: pure-Python ``websockets`` client speaks the kuaishou
        SocketMessage protocol.

        Stop conditions (auto):
          1. duration_seconds reached
          2. is_cancelled() returns True
          3. WSS connection closed
          4. SC_ERROR with code=60200 (broadcaster ended live)
        """
        import time as _time
        from ._internal.live_protocol import (
            capture_handover,
            collect_events as collect,
        )

        # ── Optional: wait for room to go live ──
        # 纯 HTTP SSR 轮询，无需浏览器。_parse_live_page 从
        # __INITIAL_STATE__ 提取 isLiving，比 gameboard/list API 的
        # living 字段（永远 false）准确，且无需 cookie/签名。
        if wait_until_live:
            wait_deadline = _time.monotonic() + max(0.0, float(wait_timeout_seconds))
            poll_interval = 30.0
            went_live = False
            while _time.monotonic() < wait_deadline:
                if is_cancelled and is_cancelled():
                    return 0
                try:
                    info = self._parse_live_page(principal_id)
                    is_living = info.get("isLiving")
                    print(f"[INFO] wait_until_live: principal_id={principal_id} "
                          f"isLiving={is_living!r} authorName={info.get('authorName')!r} "
                          f"liveStreamId={info.get('liveStreamId')!r}")
                    if is_living:
                        went_live = True
                        print(f"[INFO] wait_until_live: {principal_id} -> live")
                        break
                    else:
                        print(f"[INFO] wait_until_live: {principal_id} -> not live, "
                              f"next poll in {poll_interval:.0f}s")
                except Exception as exc:
                    print(f"[WARN] wait_until_live: _parse_live_page failed: {exc}")
                slept = 0.0
                while slept < poll_interval:
                    if is_cancelled and is_cancelled():
                        return 0
                    _time.sleep(1.0)
                    slept += 1.0
            if not went_live:
                print(f"[INFO] wait_until_live: {principal_id} not live before timeout, exiting")
                return 0

        # ── Bootstrap：单 hold，拿 handover 后退出 ──
        with browser_provider.hold() as bba_page:
            handover = capture_handover(bba_page, principal_id)
        # hold 退出 → page/chrome 关闭，剩下纯 Python WSS

        return collect(
            handover=handover,
            duration_seconds=duration_seconds,
            on_event=on_event,
            is_cancelled=is_cancelled,
            event_filter=event_filter,
        )



    # ──────────────────────────────────────────────────
    #  身份管理
    # ──────────────────────────────────────────────────

    def qr_login(self, max_refresh=5) -> bool:

        """扫码登录（完整流程：二维码 → 扫码 → token → cookie 保存）。"""
        return self.session.qr_login(max_refresh=max_refresh)

    def sms_send_code(self, phone: str, country_code: str = "+86") -> bool:
        """向手机号发送短信验证码（手机验证码登录第一步）。"""
        return self.session.sms_send_code(phone, country_code)

    def sms_verify_code(self, phone: str, sms_code: str, country_code: str = "+86") -> bool:
        """使用短信验证码完成登录（手机验证码登录第二步）。"""
        return self.session.sms_verify_code(phone, sms_code, country_code)

    def regenerate_did(self):
        self.session.regenerate_did()

    def switch_profile(self):
        self.session.switch_profile()

    def exchange_live_token(self) -> bool:
        return self.session.exchange_live_token()

    def user_login(self) -> bool:
        return self.session.user_login()


# R4 alias — KuaishouClient is the canonical name going forward.
KuaishouClient = KuaishouSDK


# ════════════════════════════════════════════════════════════
#  命令行入口
# ════════════════════════════════════════════════════════════

def main():
    import argparse
    parser = argparse.ArgumentParser(description="快手爬虫 SDK")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("search", help="搜索视频")
    p.add_argument("keyword")

    p = sub.add_parser("user", help="用户主页")
    p.add_argument("user_id")

    sub.add_parser("verify", help="验证登录")
    sub.add_parser("login", help="扫码登录")

    p = sub.add_parser("rooms", help="直播间列表")
    p.add_argument("game_id")
    p.add_argument("--size", type=int, default=10)

    args = parser.parse_args()
    sdk = KuaishouSDK(log_prefix="cli")

    if args.command == "search":
        data = sdk.search_feed(args.keyword)
        for i, f in enumerate(data.get("feeds", [])[:10], 1):
            ph = f.get("photo", {}); au = f.get("author", {})
            print(f"  {i}. [{ph.get('id','')}] {ph.get('caption','')[:50]} "
                  f"by {au.get('name','')} ▶{ph.get('viewCount',0)}")

    elif args.command == "user":
        data = sdk.user_profile(args.user_id)
        up = data.get("userProfile", {})
        p = up.get("profile", {}); c = up.get("ownerCount", {})
        print(f"用户: {p.get('user_name','')} 粉丝:{c.get('fan',0)} 作品:{c.get('photo_public',0)}")

    elif args.command == "verify":
        data = sdk.verify_login()
        ok = "✅" if data.get("result") == 1 else "❌"
        print(f"{ok} result={data.get('result')} user={data.get('userName','')}")

    elif args.command == "login":
        if sdk.qr_login():
            print("\n── 验证登录状态 ──")
            data = sdk.verify_login()
            ok = "✅" if data.get("result") == 1 else "❌"
            print(f"{ok} result={data.get('result')} user={data.get('userName','')}")

    elif args.command == "rooms":
        data = sdk.live_rooms(args.game_id, page_size=args.size)
        for i, rm in enumerate((data.get("data") or {}).get("list", []), 1):
            au = rm.get("author", {})
            print(f"  {i}. [{rm.get('id','')}] {rm.get('caption','')[:35]} "
                  f"by {au.get('name','')} 👀{rm.get('watchingCount',0)}")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
