"""Douyin live HTTP/WSS protocol helpers.

Private implementation for live/search, live/enter, and webcast WSS event
collection. Public crawler code should go through ``DouyinSDK`` methods.
"""
from __future__ import annotations

import asyncio
import gzip
import json
import logging
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse

import httpx
import websockets

from crawlhub.core.browser.host_environment import REAL_ACCEPT_LANGUAGE

logger = logging.getLogger(__name__)

SEARCH_API = "https://www.douyin.com/aweme/v1/web/live/search/"
ENTER_API = "https://live.douyin.com/webcast/room/web/enter/"
SEARCH_PAGE_SIZE = 15
PUSH_PATH_HINTS = ("/webcast/im/push/v2/",)
UNSUPPORTED_WSS_HINTS = ("frontier-im.douyin.com/ws/v2", "frontier-pc.douyin.com/ws/v2")
HEARTBEAT_FRAME = bytes.fromhex("3a026862")
HEARTBEAT_INTERVAL = 10.0


@dataclass
class SignatureSnapshot:
    a_bogus: str
    ms_token: str
    webid: str
    uifid: str
    cookie: str
    ua: str
    headers: dict[str, str]


@dataclass
class EnterCapture:
    """完整捕获的 enter API 请求，调用方直接 httpx 重放即可.

    比 SignatureSnapshot 多了 url 和 完整 headers——
    enter API 强校验 room_id_str + x-secsdk-csrf-token + sec-ch-ua 三件套，
    不能像 search 那样"凑参数"，必须照抄。
    """
    url: str
    headers: dict[str, str]


@dataclass
class PbField:
    num: int
    wire_type: int
    value: Any
    raw: bytes


def parse_web_rid(value: str) -> str:
    raw = str(value or "").strip()
    m = re.search(r"live\.douyin\.com/(\d+)", raw)
    if m:
        return m.group(1)
    if raw.isdigit():
        return raw
    raise ValueError(f"Cannot parse web_rid from {value!r}")


def _run_browser(page_handle: Any, coro):
    """R7: page_handle 是 PageHandle；.runner 是 sanctioned 公开属性。"""
    return page_handle.runner.run(coro)


def _parse_cn_count(val: Any) -> int:
    """解析直播平台的中文计数字符串 → int。

    支持：
      - 纯数字 / int / float：原样返回
      - "11w+" / "1.5万" / "11万+" / "1w" / "2.3w"  → 万级
      - "1.2亿" / "1亿+"                              → 亿级
      - "1.5k" / "1.5k+"                              → 千级
      - 其他无法解析的：返回 0

    "+" 后缀代表"约/起"，按前面的数字计数即可（不做向上取整）。
    """
    if val is None:
        return 0
    if isinstance(val, (int, float)):
        return int(val)
    s = str(val).strip().lower()
    if not s:
        return 0
    # 去尾部 '+' / '约' / 空白
    s = s.rstrip("+约 ")
    try:
        return int(s)
    except ValueError:
        pass
    multiplier = 1
    if s.endswith("万") or s.endswith("w"):
        multiplier = 10000
        s = s[:-1]
    elif s.endswith("亿"):
        multiplier = 100_000_000
        s = s[:-1]
    elif s.endswith("k"):
        multiplier = 1000
        s = s[:-1]
    try:
        return int(float(s) * multiplier)
    except (ValueError, TypeError):
        return 0


async def _capture_live_search_request(page_wrapper: Any, keyword: str, timeout_seconds: float = 18.0) -> SignatureSnapshot:
    """R7: page_wrapper 是 PlaywrightPageWrapper（PageHandle.raw 给的）.

    R5 时代这个函数接收 BrowserSession 并自己 _acquire_page；R7 调用方已经
    通过 hold() 持有 page，直接传 PlaywrightPageWrapper 进来用，去掉
    嵌套 acquire 层。
    """
    captured: dict[str, Any] = {}
    # R7: 直接用 page_wrapper，去掉 _acquire_page 嵌套
    if True:
        page = page_wrapper.page

        async def on_route(route):
            req = route.request
            if "/aweme/v1/web/live/search/" in req.url and not captured:
                headers = await req.all_headers()
                captured.update({"url": req.url, "headers": headers})
            await route.continue_()

        await page.route("**/*", on_route)
        captcha_detected = False
        try:
            await page.goto("https://www.douyin.com/", wait_until="domcontentloaded")
            await page.wait_for_timeout(1800)
            await page.goto(
                f"https://www.douyin.com/search/{quote(keyword)}?source=switch_tab&type=live",
                wait_until="domcontentloaded",
            )
            start = time.monotonic()
            while time.monotonic() - start < timeout_seconds:
                if captured:
                    break
                # ── fail-fast: 风控验证页检测 ──
                # 抖音风控触发后页面 title 变成"验证码中间页"或类似；
                # 且会在 DOM 嵌入 rmc.bytedance.com/verifycenter/captcha
                # iframe；此时永远不会发 /aweme/v1/web/live/search/，
                # 傻等 18s 没意义。
                try:
                    title = (await page.title()) or ""
                    cur_url = page.url or ""
                except Exception:
                    title = ""
                    cur_url = ""
                title_lower = title.lower()
                if (
                    "验证码中间页" in title
                    or "验证中间页" in title  # 历史命名兜底
                    or "captcha" in title_lower
                    or "verify" in title_lower
                    or "verifycenter" in cur_url
                    or "captcha" in cur_url.lower()
                ):
                    captcha_detected = True
                    break
                await page.wait_for_timeout(250)
        finally:
            try:
                await page.unroute("**/*", on_route)
            except Exception:
                pass
    if not captured:
        if captcha_detected:
            raise RuntimeError(
                "douyin captcha challenge detected — account/fingerprint risk-controlled. "
                "Run scripts/dy_live/refresh_cookie_headful.py to manually solve once, "
                "or wait 2-6 hours for cooldown. (See docs/douyin_captcha_handling.md)"
            )
        raise RuntimeError("failed to capture Douyin live/search request")
    parsed = urlparse(captured["url"])
    qs = parse_qs(parsed.query, keep_blank_values=True)
    headers = captured.get("headers") or {}
    return SignatureSnapshot(
        a_bogus=unquote(qs.get("a_bogus", [""])[0]),
        ms_token=qs.get("msToken", [""])[0],
        webid=qs.get("webid", [""])[0],
        uifid=qs.get("uifid", [""])[0],
        cookie=headers.get("cookie", ""),
        ua=headers.get("user-agent", ""),
        headers={str(k): str(v) for k, v in headers.items()},
    )


def capture_live_search_signature(page_handle: Any, keyword: str) -> SignatureSnapshot:
    """R7: page_handle 是 PageHandle；.raw 是 PlaywrightPageWrapper。"""
    return _run_browser(page_handle, _capture_live_search_request(page_handle.raw, keyword))


async def _capture_live_enter_request(
    page_wrapper: Any, web_rid: str, timeout_seconds: float = 18.0,
) -> SignatureSnapshot:
    """抓 enter API（live.douyin.com/webcast/room/web/enter/）的真实签名.

    跟 _capture_live_search_request 的关键区别：
      - 浏览器导航到 live.douyin.com/{web_rid}（不是 www.douyin.com/search/...）
      - 拦截的请求是 /webcast/room/web/enter/（不是 /aweme/v1/web/live/search/）

    必须独立抓 enter 签名，因为：
      - search 接口在 www.douyin.com 子域，enter 接口在 live.douyin.com 子域
      - 抖音的 a_bogus 算法跟"完整 URL 参数 + 域 cookie 上下文"绑定，
        跨子域复用 a_bogus 会被验签失败 → 200 OK + 空 body
    """
    captured: dict[str, Any] = {}
    page = page_wrapper.page

    async def on_route(route):
        req = route.request
        if "/webcast/room/web/enter/" in req.url and not captured:
            headers = await req.all_headers()
            captured.update({"url": req.url, "headers": headers})
        await route.continue_()

    await page.route("**/*", on_route)
    captcha_detected = False
    try:
        # 直接导航到目标直播间 URL，抖音前端会自动发 enter API
        await page.goto(
            f"https://live.douyin.com/{web_rid}",
            wait_until="domcontentloaded",
        )
        start = time.monotonic()
        while time.monotonic() - start < timeout_seconds:
            if captured:
                break
            try:
                title = (await page.title()) or ""
                cur_url = page.url or ""
            except Exception:
                title = ""
                cur_url = ""
            title_lower = title.lower()
            if (
                "验证码中间页" in title
                or "验证中间页" in title
                or "captcha" in title_lower
                or "verify" in title_lower
                or "verifycenter" in cur_url
                or "captcha" in cur_url.lower()
            ):
                captcha_detected = True
                break
            await page.wait_for_timeout(250)
    finally:
        try:
            await page.unroute("**/*", on_route)
        except Exception:
            pass

    if not captured:
        if captcha_detected:
            raise RuntimeError(
                "douyin captcha challenge detected during enter capture — "
                "account/fingerprint risk-controlled. Run "
                "scripts/dy_live/refresh_cookie_headful.py to solve once."
            )
        raise RuntimeError(f"failed to capture Douyin enter request for web_rid={web_rid}")

    return EnterCapture(
        url=captured["url"],
        headers={str(k): str(v) for k, v in (captured.get("headers") or {}).items()},
    )


def capture_live_enter(page_handle: Any, web_rid: str) -> EnterCapture:
    """抓 enter API 完整请求（url+headers），返回 EnterCapture 给调用方直接重放.

    enter API 强校验 room_id_str（不能空）+ x-secsdk-csrf-token + sec-ch-ua 三件套，
    不能"凑参数重发"，必须照抄浏览器的真实请求。
    """
    return _run_browser(page_handle, _capture_live_enter_request(page_handle.raw, web_rid))


async def _capture_push_wss_url_via_daemon_page(
    page_wrapper: Any,
    web_rid: str,
    timeout_seconds: float = 30.0,
) -> tuple[str, str]:
    """用 daemon 主浏览器（PageHandle）的 page 抓 webcast push WSS URL.

    R7 §10.x 修订（2026-05-31）：
      旧实现自己 spin up 独立 playwright，绕过了 crawlhub 统一调过的 stealth 体系
      （旧 stealth.min.js / 硬编码 UA Chrome 147 / 不读 config.browser.bba_headful /
      不用 host_environment / --headless=new 硬注入）——结果被抖音 SDK 识别
      降级到 frontier-im WSS（无弹幕推送）.

      新实现直接用 daemon 主浏览器的 page——它已经走 patchright + channel=chrome
      + headful + stealth_override.js + host_environment 自适应 UA 整套配方，
      跟 get_live_room_info（已修复证明能拿 webcast/im/push/v2 完整 data）共享
      同一个浏览器上下文.
    """
    import sys
    page = page_wrapper.page
    captured: dict[str, str] = {}
    seen_ws: list[str] = []

    def on_ws(ws):
        url = str(getattr(ws, "url", "") or "")
        seen_ws.append(url)
        print(f"[douyin.live] websocket opened: {url[:140]}", file=sys.stderr)
        if "url" not in captured and any(hint in url for hint in PUSH_PATH_HINTS):
            captured["url"] = url

    page.on("websocket", on_ws)
    try:
        await page.goto(f"https://live.douyin.com/{web_rid}", wait_until="domcontentloaded")
        start = time.monotonic()
        while time.monotonic() - start < timeout_seconds:
            if "url" in captured:
                break
            await page.wait_for_timeout(300)
        cookie_header = ""
        try:
            await page.wait_for_timeout(500)
            cookies = await page.context.cookies()
            cookie_header = "; ".join(
                f"{c.get('name')}={c.get('value')}"
                for c in cookies
                if c.get("name") and c.get("value") is not None
            )
        except Exception:
            cookie_header = ""
    finally:
        try:
            page.remove_listener("websocket", on_ws)
        except Exception:
            pass

    if "url" not in captured:
        if any(any(h in u for h in UNSUPPORTED_WSS_HINTS) for u in seen_ws):
            raise RuntimeError(
                "Douyin live room uses frontier-im WSS (互动赛事), not supported yet. "
                f"Seen WSS: {seen_ws}"
            )
        ws_summary = "; ".join(u[:80] for u in seen_ws[:10]) or "(none)"
        raise RuntimeError(
            f"failed to capture Douyin push WSS URL within {timeout_seconds}s; observed: {ws_summary}"
        )
    return captured["url"], cookie_header


# R7 修订（2026-05-31）：_resolve_cookies_for_playwright 删除。
#   之前用于把 cookie 文件灌进独立 spin up 的 playwright；现在 push WSS 改用
#   daemon 主浏览器（已注入 cookie），不再需要这个函数。
#   ROOT_BROWSER_DIR 也删除（之前用于找旧 stealth.min.js）.


def capture_push_wss(page_handle: Any, web_rid: str) -> tuple[str, str]:
    """用 daemon 主浏览器的 page 抓 webcast push WSS URL + cookie header.

    R7 §10.x 修订（2026-05-31）：复用 daemon 主浏览器（已统一调过 stealth），
    不再 spin up 独立 playwright. 详见 _capture_push_wss_url_via_daemon_page docstring.
    """
    return _run_browser(
        page_handle,
        _capture_push_wss_url_via_daemon_page(page_handle.raw, web_rid),
    )


def _quote_pairs(pairs: list[tuple[str, str]]) -> str:
    return "&".join(f"{k}={quote(v, safe='')}" for k, v in pairs)


def _search_query(keyword: str, offset: int, sig: SignatureSnapshot) -> str:
    pairs = [
        ("device_platform", "webapp"), ("aid", "6383"), ("channel", "channel_pc_web"),
        ("search_channel", "aweme_live"), ("keyword", keyword), ("search_source", "switch_tab"),
        ("query_correct_type", "1"), ("is_filter_search", "0"), ("from_group_id", ""),
        ("disable_rs", "0"), ("offset", str(offset)), ("count", str(SEARCH_PAGE_SIZE)),
        ("need_filter_settings", "1"), ("list_type", "single"),
        ("pc_search_top_1_params", '{"enable_ai_search_top_1":1}'),
        ("update_version_code", "170400"), ("pc_client_type", "1"), ("pc_libra_divert", "Windows"),
        ("support_h265", "1"), ("support_dash", "1"), ("cpu_core_num", "4"),
        ("version_code", "170400"), ("version_name", "17.4.0"), ("cookie_enabled", "true"),
        ("screen_width", "1920"), ("screen_height", "1080"), ("browser_language", "zh-CN"),
        ("browser_platform", "Win32"), ("browser_name", "Chrome"), ("browser_version", "147.0.0.0"),
        ("browser_online", "true"), ("engine_name", "Blink"), ("engine_version", "147.0.0.0"),
        ("os_name", "Windows"), ("os_version", "10"), ("device_memory", "8"), ("platform", "PC"),
        ("downlink", "10"), ("effective_type", "4g"), ("round_trip_time", "50"),
        ("webid", sig.webid), ("uifid", sig.uifid), ("msToken", sig.ms_token), ("a_bogus", sig.a_bogus),
    ]
    return _quote_pairs(pairs)


def _headers(referer: str, sig: SignatureSnapshot) -> dict[str, str]:
    """生产同款 headers：sec-ch-ua 三件套 + uifid 跟真实浏览器一致.

    P1-A 修复（2026-05-31）：参考 kuaishou _build_live_api_headers 的"完整浏览器
    指纹"实践，给抖音 search_live_rooms 补齐缺失的 header，避免抖音收紧验签时
    重蹈 get_live_room_info 的覆辙（200 OK + 空 body）.

    sec-ch-ua 三件套 + uifid 由真实浏览器实测对比得出（_probe_douyin_search.py
    diff: missing ['sec-ch-ua', 'sec-ch-ua-mobile', 'sec-ch-ua-platform', 'uifid']).
    """
    headers = {
        "user-agent": sig.ua,
        "accept": "application/json, text/plain, */*",
        "accept-language": REAL_ACCEPT_LANGUAGE,
        "referer": referer,
        "cookie": sig.cookie,
        # 真实浏览器 Client Hints 三件套——现代 Chrome 必发
        "sec-ch-ua": '"Chromium";v="148", "Google Chrome";v="148", "Not/A)Brand";v="99"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
    }
    # uifid 作为独立 header（query 里也带一份，但浏览器会同时塞 header 里）
    if sig.uifid:
        headers["uifid"] = sig.uifid
    return headers


def _slim_search_room(item: dict[str, Any], keyword: str) -> dict[str, Any] | None:
    if item.get("type") != 1:
        return None
    lives = item.get("lives") or {}
    rawdata = lives.get("rawdata") or "{}"
    try:
        room = json.loads(rawdata) if isinstance(rawdata, str) else rawdata
    except Exception:
        room = {}
    owner = room.get("owner") or {}
    cover = room.get("cover") or {}
    stream = room.get("stream_url") or {}
    flv = stream.get("flv_pull_url") or {}
    hls = stream.get("hls_pull_url_map") or {}
    return {
        "web_rid": str(owner.get("web_rid") or ""),
        "room_id": str(room.get("id_str") or room.get("id") or ""),
        "title": str(room.get("title") or ""),
        # user_count: 同 get_live_room_info — 服务端有时只给 _str 文案
        "user_count": (
            int(room.get("user_count") or 0)
            or _parse_cn_count(room.get("user_count_str") or "")
        ),
        "status": int(room.get("status") or 0),
        "author_nickname": str(owner.get("nickname") or ""),
        "author_uid": str(owner.get("id_str") or owner.get("id") or ""),
        "author_sec_uid": str(owner.get("sec_uid") or ""),
        "cover_url": (cover.get("url_list") or [""])[0] or "",
        "stream_flv": flv.get("FULL_HD1") or flv.get("HD1") or "",
        "stream_hls": hls.get("FULL_HD1") or hls.get("HD1") or "",
        "keyword": keyword,
    }


def search_live_rooms(
    page_handle: Any,
    keyword: str,
    max_results: int,
    live_only: bool = False,
    sort_by: str = "default",
) -> list[dict[str, Any]]:
    """Search Douyin live rooms.

    The Douyin web live search API does not expose any server-side
    sort/filter parameter for live rooms. We over-fetch (up to 3x
    max_results when filtering) and apply filtering + sorting on the
    client side.

    Args:
        browser_session: Daemon-managed browser session for signature capture.
        keyword: Search keyword.
        max_results: Number of rooms to return after filtering.
        live_only: If True, drop rooms where ``status != 2`` (not live).
        sort_by: ``"default"`` (server order) | ``"user_count"`` (client-side
            sort by descending user_count, popularity-ranked).
    """
    sig = capture_live_search_signature(page_handle, keyword)
    rooms: list[dict[str, Any]] = []
    offset = 0
    # When filtering, fetch more to compensate for dropped offline rooms
    fetch_target = max_results * 3 if live_only else max_results
    with httpx.Client(http2=True, timeout=20.0) as client:
        while len(rooms) < fetch_target:
            url = SEARCH_API + "?" + _search_query(keyword, offset, sig)
            resp = client.get(url, headers=_headers(f"https://www.douyin.com/search/{quote(keyword)}?source=switch_tab&type=live", sig))
            body = resp.json()
            nil = body.get("search_nil_info") or {}
            if nil.get("search_nil_type") == "verify_check":
                page_handle.report_anti_crawl(signal="verify_check", platform="douyin", detail=f"live_search keyword={keyword}")
            data = body.get("data") or []
            for item in data:
                room = _slim_search_room(item, keyword)
                if room is None:
                    continue
                if live_only and int(room.get("status") or 0) != 2:
                    continue
                rooms.append(room)
                if len(rooms) >= fetch_target:
                    break
            if not body.get("has_more") or not data:
                break
            offset = int(body.get("cursor") or (offset + SEARCH_PAGE_SIZE))
            time.sleep(0.4)

    # Client-side sort
    if sort_by == "user_count":
        rooms.sort(key=lambda r: int(r.get("user_count") or 0), reverse=True)

    return rooms[:max_results]


# 2026-05-31: _enter_query 已废弃。enter API 必须用浏览器真实捕获的 URL 重放
# （见 capture_live_enter），凑参数不可能通过抖音验签（room_id_str 必填 +
# x-secsdk-csrf-token + sec-ch-ua 三件套全部要带）。


def _slim_enter_room(resp: dict[str, Any], web_rid: str) -> dict[str, Any]:
    data = resp.get("data") or {}
    data_list = data.get("data")
    room = data_list[0] if isinstance(data_list, list) and data_list else data.get("room") or {}
    owner = room.get("owner") or {}
    stats = room.get("stats") or {}
    cover = room.get("cover") or {}
    stream = room.get("stream_url") or {}
    flv = stream.get("flv_pull_url") or {}
    hls = stream.get("hls_pull_url_map") or {}
    return {
        "web_rid": web_rid,
        "room_id": str(room.get("id_str") or room.get("id") or ""),
        "status": int(room.get("status") or 0),
        "status_str": "LIVE" if room.get("status") == 2 else "OFFLINE",
        "title": str(room.get("title") or ""),
        # user_count: 服务端有时只返回 user_count_str（如 "11w+"），数值字段缺失。
        # 优先用数字字段，否则解析中文文案。
        "user_count": (
            int(room.get("user_count") or stats.get("user_count") or 0)
            or _parse_cn_count(
                room.get("user_count_str") or stats.get("user_count_str") or ""
            )
        ),
        "user_count_str": str(room.get("user_count_str") or stats.get("user_count_str") or ""),
        "like_count": int(stats.get("like_count") or 0),
        "owner_uid": str(owner.get("id_str") or owner.get("id") or ""),
        "owner_sec_uid": str(owner.get("sec_uid") or ""),
        "owner_nickname": str(owner.get("nickname") or ""),
        "cover_url": (cover.get("url_list") or [""])[0] or "",
        "stream_flv_origin": flv.get("FULL_HD1") or flv.get("HD1") or "",
        "stream_hls_origin": hls.get("FULL_HD1") or hls.get("HD1") or "",
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }


def get_live_room_info(page_handle: Any, web_rid_or_url: str) -> dict[str, Any]:
    """Fetch Douyin live room info via real-browser-captured request replay.

    实现原则（2026-05-31 终版）：
      抖音 enter API（live.douyin.com）强校验：
        - room_id_str（不能空）
        - x-secsdk-csrf-token header
        - sec-ch-ua / sec-ch-ua-mobile / sec-ch-ua-platform 三件套
        - a_bogus（基于完整 URL + cookie 上下文）
      凑参数发请求一定挂。唯一可靠路径：浏览器去 live.douyin.com/{web_rid}
      触发抖音 SDK 自己发 enter API，route 拦截拿到真实 URL+headers，直接重放。

    失败检测：
      非 200 / 非 JSON 响应统一报 anti_crawl，让 daemon 触发退避.
    """
    web_rid = parse_web_rid(web_rid_or_url)
    capture = capture_live_enter(page_handle, web_rid)
    with httpx.Client(timeout=15.0) as client:
        resp = client.get(capture.url, headers=capture.headers)
    text = resp.text or ""
    status = resp.status_code

    # 1) 非 200 → 风控/网关错误
    if status != 200:
        page_handle.report_anti_crawl(
            signal=f"http_{status}",
            platform="douyin",
            detail=f"live_info web_rid={web_rid} body_preview={text[:200]!r}",
        )

    # 2) 200 但响应体不是 JSON → 风控空响应/HTML 验证页
    text_stripped = text.lstrip()
    if not text_stripped or not text_stripped.startswith(("{", "[")):
        page_handle.report_anti_crawl(
            signal="non_json_body",
            platform="douyin",
            detail=f"live_info web_rid={web_rid} body_len={len(text)} body_preview={text[:200]!r}",
        )

    # 3) 合法 JSON 才解析
    try:
        body = json.loads(text)
    except json.JSONDecodeError as exc:
        page_handle.report_anti_crawl(
            signal="json_decode_error",
            platform="douyin",
            detail=f"live_info web_rid={web_rid} body_preview={text[:200]!r} err={exc}",
        )
        raise

    if body.get("status_code") not in (0, None):
        page_handle.report_anti_crawl(
            signal=str(body.get("status_code")),
            platform="douyin",
            detail=f"live_info web_rid={web_rid}",
        )
    return _slim_enter_room(body, web_rid)


def read_varint(buf: bytes, offset: int = 0) -> tuple[int, int]:
    out = 0
    shift = 0
    i = offset
    while i < len(buf):
        b = buf[i]
        i += 1
        out |= (b & 0x7F) << shift
        if not (b & 0x80):
            return out, i
        shift += 7
    raise EOFError("unterminated varint")


def write_varint(value: int) -> bytes:
    out = bytearray()
    while True:
        b = value & 0x7F
        value >>= 7
        out.append(b | 0x80 if value else b)
        if not value:
            return bytes(out)


def parse_fields(buf: bytes) -> list[PbField]:
    """Parse protobuf wire fields, **forgiving unknown wire types**.

    根据 protobuf wire format 规范，wire type 3/4 是已废弃的 GROUP_START/END，
    6/7 是保留位。理论上服务端不该发，但抖音 2026 实测在 ``WebcastSocialMessage``
    内嵌的 User proto 里出现了 wire type 6（疑似 packed repeated 或厂内
    自定义编码）。

    旧实现遇到未知 wire 直接 raise，导致**整帧 + 整 action 死亡**。
    新实现按 protobuf 兼容性原则跳过未知字段：
      - wire 0/1/2/5：标准解析
      - 其它：把当前位置 1 byte 当作不可读字节跳过，继续往后

    这样即使未来又出新 wire type，单条 message 顶多丢字段，不会让
    整个连接 / 整个 action 崩。
    """
    fields: list[PbField] = []
    i = 0
    while i < len(buf):
        start = i
        try:
            key, i = read_varint(buf, i)
        except Exception:
            # tag varint 都读不到，整帧损坏，停止解析返回已读部分
            break
        num = key >> 3
        wt = key & 7
        if wt == 0:
            try:
                value, i = read_varint(buf, i)
            except Exception:
                break
        elif wt == 2:
            try:
                size, i = read_varint(buf, i)
            except Exception:
                break
            if i + size > len(buf):
                break  # 截断，停下
            value = buf[i : i + size]
            i += size
        elif wt == 1:
            if i + 8 > len(buf):
                break
            value = buf[i : i + 8]
            i += 8
        elif wt == 5:
            if i + 4 > len(buf):
                break
            value = buf[i : i + 4]
            i += 4
        else:
            # 未知 wire (3/4/6/7)：跳过整个字段是不可能的（不知道长度），
            # 但跳过 1 byte 让外层 caller 至少拿到前面的字段。
            # 注：这等价于"丢弃当前及之后的字段"，因为后面的偏移大概率错位。
            # 上层调用方应该已经拿到了它关心的字段（比如 method、user_id），
            # 不至于因为某个尾部字段炸掉而完全没结果。
            break
        fields.append(PbField(num, wt, value, buf[start:i]))
    return fields


def first_bytes(fields: Iterable[PbField], num: int) -> bytes | None:
    for f in fields:
        if f.num == num and f.wire_type == 2:
            return bytes(f.value)
    return None


def first_var(fields: Iterable[PbField], num: int) -> int | None:
    for f in fields:
        if f.num == num and f.wire_type == 0:
            return int(f.value)
    return None


def as_text(value: bytes | None) -> str:
    if not value:
        return ""
    try:
        return value.decode("utf-8")
    except Exception:
        return ""


def enc_var_field(num: int, value: int) -> bytes:
    return write_varint((num << 3) | 0) + write_varint(value)


def enc_bytes_field(num: int, value: str | bytes) -> bytes:
    raw = value.encode("utf-8") if isinstance(value, str) else value
    return write_varint((num << 3) | 2) + write_varint(len(raw)) + raw


def build_ack_frame(log_id: int, internal_ext: str) -> bytes:
    return enc_var_field(2, log_id) + enc_bytes_field(7, "ack") + enc_bytes_field(8, internal_ext)


def _parse_user(payload: bytes) -> dict[str, Any]:
    """Parse Douyin User proto (webcast/push/User).

    Known fields (DOM+PB cross-validated 2026-06-06):
      field 1 = uid (varint)
      field 2 = sec_uid (bytes/string)
      field 3 = nickname (bytes/string)
      field 4 = avatar_thumb_url (bytes/string)
      field 9 = bio (bytes/string)
      field 14 = badge_urls (repeated sub-message, field 1 = url_list)
      field 16 = follow_status (varint)
      field 18 = effects_info (sub-message)
      field 22 = chat_label_url (bytes/string, badge/label image URL)
    """
    fields = parse_fields(payload) if payload else []
    return {
        "uid": str(first_var(fields, 1) or ""),
        "nickname": as_text(first_bytes(fields, 3)),
    }


def _parse_enter_effect(payload: bytes) -> dict[str, Any]:
    """Parse MemberMessage field 15 — enter_effect sub-message.

    DOM+PB cross-validated 2026-06-06:
      f4.f1 = effect_resource_name (e.g. "vcd_aweme_fansclub_enter_effect_new",
              "honor_live_room_enter_effect")
      f4.f2 = display_template (e.g. "{0:user} 加入了直播间")
      f9  = constant "2222" (purpose unknown)
      f16 = KV pairs: effect_source / effect_id / to_user_id
      f18 = 40000 (likely animation duration ms)
      f19 = 60 (likely animation frame parameter)
      f23 = effect_source: "fansclub" / "honor_level" / "star_guard"
    """
    if not payload:
        return {}
    fields = parse_fields(payload)
    effect_source = as_text(first_bytes(fields, 23))
    # f4 sub-message
    f4_raw = first_bytes(fields, 4) or b""
    f4 = parse_fields(f4_raw) if f4_raw else []
    resource_name = as_text(first_bytes(f4, 1))
    display_template = as_text(first_bytes(f4, 2))
    # f16 KV pairs (repeated sub-message: f1=key, f2=value)
    kv_pairs: dict[str, str] = {}
    for f in fields:
        if f.num == 16 and f.wire_type == 2:
            kv_fields = parse_fields(bytes(f.value))
            k = as_text(first_bytes(kv_fields, 1))
            v = as_text(first_bytes(kv_fields, 2))
            if k:
                kv_pairs[k] = v
    # effect_id → human-readable label
    effect_id = kv_pairs.get("effect_id", "")
    effect_id_label = ""
    if effect_id == "101":
        effect_id_label = "honor"
    elif effect_id == "201":
        effect_id_label = "fansclub"
    elif effect_id == "301":
        effect_id_label = "star_guard"
    return {
        "effect_source": effect_source,
        "resource_name": resource_name,
        "display_template": display_template,
        "effect_id": effect_id,
        "effect_id_label": effect_id_label,
        "kv_pairs": kv_pairs,
        "anim_duration_ms": first_var(fields, 18) or 0,
    }


def _parse_render_kv(fields: list[PbField]) -> dict[str, Any]:
    """Parse MemberMessage field 22 — render_kv (repeated KV pairs).

    DOM+PB cross-validated 2026-06-06:
      Field 22 is a repeated sub-message. Each entry has:
        f1 = key (string), f2 = value (string)
      Known keys:
        msg_content_type: "fansclub" / "honor_level" / "guard"
        msg_show_type: "enterroom_normal"
        enter_tip_type: "0" or "32" (string-encoded int)
    """
    if not fields:
        return {}
    kv: dict[str, str] = {}
    for f in fields:
        if f.num == 22 and f.wire_type == 2:
            sub = parse_fields(bytes(f.value))
            k = as_text(first_bytes(sub, 1))
            v = as_text(first_bytes(sub, 2))
            if k:
                kv[k] = v
    result: dict[str, Any] = {"kv": kv}
    # Convenience fields
    if "msg_content_type" in kv:
        result["content_type"] = kv["msg_content_type"]
    if "msg_show_type" in kv:
        result["show_type"] = kv["msg_show_type"]
    if "enter_tip_type" in kv:
        try:
            result["tip_type"] = int(kv["enter_tip_type"])
        except ValueError:
            result["tip_type"] = 0
    return result


def _message_to_event(method: str, payload: bytes) -> dict[str, Any]:
    """Map a Douyin Webcast Message to a normalized event dict.

    Always sets ``raw_cmd`` = method name for cross-platform consistency
    with B站 event_filter API.

    Recognized methods (cmd):
      - WebcastChatMessage         弹幕
      - WebcastRoomStatsMessage    在线人数/统计
      - WebcastMemberMessage       进房
      - WebcastLikeMessage         点赞
      - WebcastGiftMessage         礼物
      - WebcastSocialMessage       关注/分享
      - WebcastRoomUserSeqMessage  在线观众列表
      - WebcastFansclubMessage     粉丝团动作
      - WebcastRoomRankMessage     榜单变化
      - WebcastRoomMessage         房间状态
      - WebcastControlMessage      直播间控制（含下播信号 status=3）
    """
    fields = parse_fields(payload) if payload else []

    if method == "WebcastChatMessage":
        # DOM+PB cross-validated 2026-06-06:
        #   field 2 = user (User proto)
        #   field 3 = content (弹幕文本)
        #   field 9 = rich_text/emoji (sub-message)
        #   field 15 = server_timestamp
        #   field 41 = @mention/reply
        # DOM vs WS: ~52% of WS ChatMessage appear in DOM (rest filtered by client)
        user = _parse_user(first_bytes(fields, 2) or b"")
        return {"event_type": "chat", "raw_cmd": method,
                "uid": user["uid"], "nickname": user["nickname"],
                "content": as_text(first_bytes(fields, 3)),
                "payload": {"server_timestamp": first_var(fields, 15) or 0}}

    if method == "WebcastRoomStatsMessage":
        # DOM+PB cross-validated 2026-06-06:
        #   field 2/3 = display_short text (e.g. "3万")
        #   field 4 = full display text (e.g. "3万在线观众")
        #   field 5 = online_count (numeric)
        #   field 6 = server_timestamp
        return {"event_type": "room_stats", "raw_cmd": method,
                "online_count": first_var(fields, 5) or first_var(fields, 9) or 0,
                "content": as_text(first_bytes(fields, 4)),
                "payload": {
                    "display_short": as_text(first_bytes(fields, 2)),
                    "server_timestamp": first_var(fields, 6) or 0,
                }}

    if method == "WebcastMemberMessage":
        # DOM+PB cross-validated 2026-06-06:
        #   field 2 = user (User proto)
        #   field 3 = current_online_count (NOT sequence number — matches RoomUserSeqMessage)
        #   field 10 = action: 1=enter, 2=follow_enter, 3=share_enter
        #   field 15 = enter_effect (sub-message, see _parse_enter_effect)
        #   field 18 = display_template (sub-message, f2="{0:user} 来了")
        #   field 19 = user_badge/label info (sub-message, f1=chat_label_image_url)
        #   field 22 = render_kv (sub-message with msg_content_type, msg_show_type, enter_tip_type)
        user = _parse_user(first_bytes(fields, 2) or b"")
        action = first_var(fields, 10) or 0
        action_label = {1: "enter", 2: "follow_enter", 3: "share_enter"}.get(int(action), f"action_{action}")
        online_count = first_var(fields, 3) or 0
        enter_effect = _parse_enter_effect(first_bytes(fields, 15) or b"")
        render_kv = _parse_render_kv(fields)
        return {"event_type": "member", "raw_cmd": method,
                "uid": user["uid"], "nickname": user["nickname"],
                "online_count": online_count,
                "payload": {
                    "action": int(action),
                    "action_label": action_label,
                    "enter_effect": enter_effect,
                    "render_kv": render_kv,
                }}

    if method == "WebcastLikeMessage":
        # DOM+PB cross-validated 2026-06-06:
        #   field 2 = like_count (本次点赞数)
        #   field 3 = total_likes (累计总赞)
        #   field 5 = user (User proto)
        # DOM vs WS: ~60% of WS LikeMessage appear in DOM
        user = _parse_user(first_bytes(fields, 5) or b"")
        return {"event_type": "like", "raw_cmd": method,
                "uid": user["uid"], "nickname": user["nickname"],
                "like_count": first_var(fields, 2) or 0,
                "payload": {"total_likes": first_var(fields, 3) or 0}}

    if method == "WebcastGiftMessage":
        # DOM+PB cross-validated 2026-06-06:
        #   field 2 = gift_id
        #   field 4 = fan_ticket_count (NOT gift count!)
        #   field 5 = group_count (actual gift count)
        #   field 7 = user (User proto)
        #   field 9 = repeat_count
        #   field 15 = GiftStruct: f2=gift_name, f4=diamond_count, f9=describe
        # DOM vs WS: ~29% of WS GiftMessage appear in DOM (free/0-diamond gifts filtered)
        user = _parse_user(first_bytes(fields, 7) or b"")
        gift_struct_raw = first_bytes(fields, 15) or b""
        gift_fields = parse_fields(gift_struct_raw) if gift_struct_raw else []
        diamond_count = first_var(gift_fields, 4) or 0
        return {"event_type": "gift", "raw_cmd": method,
                "uid": user["uid"], "nickname": user["nickname"],
                "gift_id": str(first_var(fields, 2) or ""),
                "gift_count": first_var(fields, 5) or 0,
                "payload": {
                    "fan_ticket_count": first_var(fields, 4) or 0,
                    "repeat_count": first_var(fields, 9) or 0,
                    "gift_name": as_text(first_bytes(gift_fields, 2)),
                    "diamond_count": diamond_count,
                    "is_free_gift": diamond_count == 0,
                    "describe": as_text(first_bytes(gift_fields, 9)),
                }}

    if method == "WebcastSocialMessage":
        # DOM+PB cross-validated 2026-06-06:
        #   field 1 = common (Common proto)
        #   field 2 = user (User proto, the actor)
        #   field 4 = action: 1=follow, 2=share, 3=share_to_live
        #   field 5 = uid_str (followed anchor's uid, NOT the actor)
        #   field 6 = follow_count: anchor's total followers (absolute, monotonically increasing)
        # Note: This message type did NOT appear in the DOM-validation CRA recording;
        #   schema from earlier frame analysis remains.
        user = _parse_user(first_bytes(fields, 2) or b"")
        action = first_var(fields, 4) or 0
        anchor_uid_str = as_text(first_bytes(fields, 5))
        anchor_total_followers = first_var(fields, 6) or 0
        action_label = {1: "follow", 2: "share", 3: "share_to_live"}.get(int(action), f"action_{action}")
        return {
            "event_type": "social", "raw_cmd": method,
            "uid": user["uid"],
            "nickname": user["nickname"],
            "content": action_label,
            "payload": {
                "action": int(action),
                "action_label": action_label,
                # 主播信息
                "anchor_uid_str": anchor_uid_str,
                "anchor_total_followers": int(anchor_total_followers),
            },
        }

    if method == "WebcastFansclubMessage":
        # DOM+PB cross-validated 2026-06-06:
        #   field 2 = club_action: 1=join, 6=level_up
        #   field 3 = content (upgrade text)
        #   field 4 = user (User proto)
        #   field 6 = club_detail (sub-message)
        # Note: This message type did NOT appear in the DOM-validation CRA recording;
        #   schema from earlier frame analysis remains.
        user = _parse_user(first_bytes(fields, 4) or b"")
        club_action = first_var(fields, 2) or 0
        action_label = {1: "join", 6: "level_up"}.get(int(club_action), f"action_{club_action}")
        return {"event_type": "fansclub", "raw_cmd": method,
                "uid": user["uid"], "nickname": user["nickname"],
                "content": as_text(first_bytes(fields, 3)),
                "payload": {"club_action": int(club_action), "action_label": action_label}}

    if method == "WebcastRoomUserSeqMessage":
        # DOM+PB cross-validated 2026-06-06:
        #   field 3 = online_count (numeric, matches MemberMessage field 3)
        #   field 8 = display_text (e.g. "10万+")
        #   field 10 = display_short (e.g. "3.1万")
        return {"event_type": "rank", "raw_cmd": method,
                "online_count": first_var(fields, 3) or 0,
                "payload": {
                    "display_text": as_text(first_bytes(fields, 8)),
                    "display_short": as_text(first_bytes(fields, 10)),
                }}

    if method == "WebcastRoomRankMessage":
        return {"event_type": "rank", "raw_cmd": method, "payload": {}}

    if method == "WebcastRoomMessage":
        # 房间元信息变更
        return {"event_type": "room_change", "raw_cmd": method,
                "content": as_text(first_bytes(fields, 4)), "payload": {}}

    if method == "WebcastControlMessage":
        # 直播间控制信号 — status=3 表示主播下播
        # 协议参考社区共识（webmssdk.es5.js 反混淆）
        status = first_var(fields, 1) or 0
        return {"event_type": "control", "raw_cmd": method,
                "online_count": int(status),  # 复用 online_count 携带 status
                "payload": {"status": int(status)}}

    if method == "WebcastEmojiChatMessage":
        # emoji 弹幕
        user = _parse_user(first_bytes(fields, 2) or b"")
        return {"event_type": "chat", "raw_cmd": method,
                "uid": user["uid"], "nickname": user["nickname"],
                "content": as_text(first_bytes(fields, 3)), "payload": {}}

    if method == "WebcastScreenChatMessage":
        # 屏幕飘屏弹幕
        user = _parse_user(first_bytes(fields, 2) or b"")
        return {"event_type": "chat", "raw_cmd": method,
                "uid": user["uid"], "nickname": user["nickname"],
                "content": as_text(first_bytes(fields, 3)), "payload": {}}

    # 兜底：未识别的 method
    return {"event_type": "raw", "raw_cmd": method,
            "payload": {"method": method, "payload_len": len(payload)}}


def decode_push_frame(frame: bytes) -> dict[str, Any]:
    outer = parse_fields(frame)
    headers = {}
    for entry in [bytes(f.value) for f in outer if f.num == 6 and f.wire_type == 2]:
        fs = parse_fields(entry)
        headers[as_text(first_bytes(fs, 1))] = as_text(first_bytes(fs, 2))
    payload_type = as_text(first_bytes(outer, 7))
    payload = first_bytes(outer, 8) or b""
    log_id = first_var(outer, 2) or 0
    if payload_type == "hb":
        return {"payload_type": "hb", "log_id": log_id, "headers": headers, "events": []}
    if payload:
        try:
            payload = gzip.decompress(payload)
        except Exception:
            # 个别帧 gzip 损坏 → 跳过整帧但不抛错
            return {
                "payload_type": payload_type, "log_id": log_id,
                "headers": headers, "events": [],
                "decode_error": "gzip_failed", "raw_size": len(frame),
            }
    events = []
    skipped = 0
    root = parse_fields(payload) if payload else []
    for msg_blob in [bytes(f.value) for f in root if f.num == 1 and f.wire_type == 2]:
        # ── 单 message 失败兜底（层 2）──
        # 每条 message 独立 try：一条解码失败不影响其他 9 条。
        # 失败的 message 会留下 event_type='raw_decode_error' 事件，便于
        # 监控 / 离线分析 / 后续协议升级。
        try:
            msg = parse_fields(msg_blob)
            method = as_text(first_bytes(msg, 1)) or ""
            body = first_bytes(msg, 2) or b""
            try:
                events.append(_message_to_event(method, body))
            except Exception as e:
                _log_message_decode_error(method, body, e)
                events.append({
                    "event_type": "raw_decode_error",
                    "raw_cmd": method,
                    "payload": {
                        "method": method,
                        "error": f"{type(e).__name__}: {e}",
                        "body_size": len(body),
                        "body_head_hex": body[:32].hex(),
                    },
                })
                skipped += 1
        except Exception as e:
            _log_message_decode_error("<unknown>", msg_blob, e)
            events.append({
                "event_type": "raw_decode_error",
                "raw_cmd": "",
                "payload": {
                    "error": f"{type(e).__name__}: {e}",
                    "blob_size": len(msg_blob),
                    "blob_head_hex": msg_blob[:32].hex(),
                },
            })
            skipped += 1
    out = {"payload_type": payload_type, "log_id": log_id, "headers": headers, "events": events}
    if skipped:
        out["skipped_messages"] = skipped
    return out


async def collect_events_async(
    *,
    wss_url: str,
    cookie_header: str,
    web_rid: str,
    duration_seconds: float,
    on_event: Callable[[dict[str, Any]], None],
    is_cancelled: Callable[[], bool] | None = None,
    event_filter: set[str] | None = None,
) -> int:
    """Collect Douyin live WSS events.

    Args:
        wss_url: Captured /webcast/im/push/v2/ URL with full signing params.
        cookie_header: Cookie header from the Playwright context.
        web_rid: Live room web_rid.
        duration_seconds: Hard timeout cap.
        on_event: Callback for each event.
        is_cancelled: External cancellation hook.
        event_filter: If provided, only emit events whose ``raw_cmd`` (= webcast
            method name like ``WebcastChatMessage``) is in this set.
            None = all events. ``WebcastControlMessage`` is always evaluated
            for live-end signal regardless of filter.

    Returns:
        Number of events emitted (filtered count).

    Stop conditions (auto, no user toggle):
      1. duration_seconds reached
      2. is_cancelled() returns True
      3. WSS connection closed
      4. ``WebcastControlMessage`` with status=3 (broadcaster ended live)
    """
    deadline = (time.monotonic() + float(duration_seconds)) if duration_seconds and float(duration_seconds) > 0 else float("inf")
    count = 0
    # ─────────────────────────────────────────────────────────────
    # 2026-05-31 P1-A 修复：完整浏览器 headers 给 WSS 握手
    #   旧实现只发 Cookie + 'Mozilla/5.0'，握手能通但抖音侧软风控降级：
    #   实测 8 秒内只收 2 帧 / 1419 字节（基本只有系统心跳）
    #   完整 headers 实测 8 秒内收 7 帧 / 5958 字节（4 倍弹幕事件）
    #   不会硬挂，但会丢大量事件数据——这是隐性数据损失.
    # 2026-06-02：删除 Pragma/Cache-Control: no-cache 反模式 header
    #   （cache 概念跟 wss 握手无关，真用户浏览器不带这俩，是反爬识别面）
    #   accept-language 与全局基线对齐，避免 crawlhub 内部不一致.
    # ─────────────────────────────────────────────────────────────
    _UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
    headers: dict[str, str] = {
        "User-Agent": _UA,
        "Origin": "https://live.douyin.com",
        "Accept-Language": REAL_ACCEPT_LANGUAGE,
        "Accept-Encoding": "gzip, deflate, br, zstd",
    }
    if cookie_header:
        headers["Cookie"] = cookie_header
    try:
        ws_cm = websockets.connect(wss_url, additional_headers=headers, ping_interval=None, close_timeout=3)
    except TypeError:
        ws_cm = websockets.connect(wss_url, extra_headers=headers, ping_interval=None, close_timeout=3)
    async with ws_cm as ws:
        await ws.send(HEARTBEAT_FRAME)
        next_hb = time.monotonic() + HEARTBEAT_INTERVAL
        while time.monotonic() < deadline:
            if is_cancelled and is_cancelled():
                break
            now = time.monotonic()
            if now >= next_hb:
                await ws.send(HEARTBEAT_FRAME)
                next_hb = now + HEARTBEAT_INTERVAL
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=min(1.0, max(0.05, deadline - now)))
            except asyncio.TimeoutError:
                continue
            except Exception:
                break
            if isinstance(raw, str):
                raw = raw.encode("utf-8")
            # ── 层 3：整帧 decode 失败兜底 ──
            # 即使 decode_push_frame 内部已经做了"单 message 容错"，
            # 也可能因为协议升级 / gzip 损坏 / 网络截断导致整帧炸。
            # 此时不应让 collect_events_async 抛出去，否则 daemon 会判定
            # 整个 action 失败、几小时弹幕全丢。
            # 策略：保存原始帧 bytes 到 ./tmp/dy_bad_frames/<web_rid>/<ts>.bin
            # （如可写），打 warning 计数，继续 recv 下一帧。
            try:
                decoded = decode_push_frame(raw)
            except Exception as e:
                _save_bad_frame(web_rid, raw, exc=e)
                continue
            internal_ext = decoded.get("headers", {}).get("im-internal-ext", "")
            if decoded.get("log_id") and internal_ext:
                await ws.send(build_ack_frame(int(decoded["log_id"]), internal_ext))
            for event in decoded.get("events") or []:
                event["web_rid"] = web_rid
                cmd = str(event.get("raw_cmd") or "")

                # ── Stop signal: WebcastControlMessage status=3 = live ended ──
                if cmd == "WebcastControlMessage":
                    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
                    status = int(payload.get("status") or 0)
                    if status == 3:
                        # Always emit so caller knows why
                        if event_filter is None or cmd in event_filter:
                            on_event(event)
                            count += 1
                        return count

                # ── event_filter ──
                if event_filter is not None and cmd not in event_filter:
                    continue

                on_event(event)
                count += 1
    return count


def collect_events(**kwargs: Any) -> int:
    return asyncio.run(collect_events_async(**kwargs))


# ── bad-frame 兜底：用于 collect_events_async 整帧 decode 失败 ──
_BAD_FRAME_DIR = Path.home() / ".crawlhub" / "bad_frames" / "douyin"
_BAD_FRAME_COUNTER: dict[str, int] = {}
_BAD_FRAME_LIMIT_PER_ROOM = 50  # 单房间最多保留 50 帧坏数据，防磁盘炸

# ── 单 message decode-error 限频日志 ──
# 高峰直播间 SocialMessage 一秒可能几十条，不能每条都 log。
# 按 (method, error_class) 分桶，每桶进程生命周期内最多 log 5 次 + 每 60 秒再 log 1 次。
_MSG_LOG_BUCKETS: dict[tuple[str, str], dict[str, float]] = {}
_MSG_LOG_BURST_LIMIT = 5
_MSG_LOG_PERIOD_SECONDS = 60.0


def _log_message_decode_error(method: str, body: bytes, exc: BaseException) -> None:
    """单 message 解码失败 → WARN 日志（限频）。

    输出包含 method / size / hex_head / error，能直接复现：
        WARN [douyin.live] message decode failed: method=WebcastSocialMessage
              body_size=4739 body_head_hex=0acd110a... err=ValueError: unsupported wire type 6
    """
    err_class = type(exc).__name__
    bucket = _MSG_LOG_BUCKETS.setdefault((method, err_class), {"count": 0, "last": 0.0})
    now = time.monotonic()
    should_log = False
    if bucket["count"] < _MSG_LOG_BURST_LIMIT:
        should_log = True
        bucket["count"] += 1
    elif now - bucket["last"] >= _MSG_LOG_PERIOD_SECONDS:
        should_log = True
    if should_log:
        bucket["last"] = now
        head_hex = body[:32].hex() if body else ""
        logger.warning(
            "[douyin.live] message decode failed: method=%s body_size=%d "
            "body_head_hex=%s err=%s: %s",
            method or "<empty>", len(body), head_hex, err_class, exc,
        )


def _save_bad_frame(web_rid: str, raw: bytes, *, exc: BaseException) -> None:
    """整帧 decode 失败时落盘 + 打日志。

    日志：WARN 级别，内容包含 web_rid / size / hex_head / error，
    便于运行时一眼定位。落盘是给离线诊断用，单房间最多 50 帧。
    """
    n = _BAD_FRAME_COUNTER.get(web_rid, 0) + 1
    _BAD_FRAME_COUNTER[web_rid] = n
    head_hex = raw[:48].hex() if raw else ""
    saved_path: str | None = None
    if n <= _BAD_FRAME_LIMIT_PER_ROOM:
        try:
            room_dir = _BAD_FRAME_DIR / str(web_rid)
            room_dir.mkdir(parents=True, exist_ok=True)
            ts = time.strftime("%Y%m%d_%H%M%S")
            path = room_dir / f"{ts}_{n:03d}_{type(exc).__name__}.bin"
            path.write_bytes(raw)
            meta = path.with_suffix(".txt")
            meta.write_text(
                f"web_rid={web_rid}\nsize={len(raw)}\nerror={type(exc).__name__}: {exc}\n",
                encoding="utf-8",
            )
            saved_path = str(path)
        except Exception:
            saved_path = None
    logger.warning(
        "[douyin.live] frame decode failed: web_rid=%s size=%d head_hex=%s "
        "error=%s: %s saved=%s (#%d in this room)",
        web_rid, len(raw), head_hex, type(exc).__name__, exc, saved_path or "<none>", n,
    )



