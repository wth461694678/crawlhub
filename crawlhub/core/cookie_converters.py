"""Cookie format converters: Playwright storage_state -> crawler native format.

Each platform's crawler expects a different cookie JSON structure.
This module converts the unified Playwright storage_state output into
the specific format each crawler's loader expects.

Conversion targets:
- Bilibili: flat dict {"SESSDATA": "xxx", "bili_jct": "xxx", ...}
- Douyin:   {"cookie_string": "k=v; ...", "cookies": {"k": "v", ...}}
- Kuaishou: {"main": {"did": "xxx", ...}, "live": {"did": "xxx", ...}}
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def convert_storage_state(platform: str, data: dict[str, Any]) -> dict[str, Any]:
    """Convert Playwright storage_state to crawler-native cookie format.

    Args:
        platform: Platform name (bilibili, douyin, kuaishou, etc.)
        data: Playwright storage_state dict with "cookies" list.

    Returns:
        Converted dict in the format expected by the platform's crawler.
        If platform is unknown or data is already in target format, returns as-is.
    """
    converters = {
        "bilibili": _convert_bilibili,
        "douyin": _convert_douyin,
        "kuaishou": _convert_kuaishou,
    }

    converter = converters.get(platform)
    if converter is None:
        # Unknown platform, return as-is
        return data

    # Only convert if data looks like Playwright storage_state format
    # (has "cookies" key with a list value)
    if not _is_storage_state_format(data):
        logger.debug("[cookie_converters] Data for %s is not storage_state format, returning as-is", platform)
        return data

    try:
        result = converter(data)
        logger.info("[cookie_converters] Converted %s cookie: storage_state -> native format", platform)
        return result
    except Exception as e:
        logger.warning("[cookie_converters] Failed to convert %s cookie: %s. Returning as-is.", platform, e)
        return data


def _is_storage_state_format(data: dict[str, Any]) -> bool:
    """Check if data is in Playwright storage_state format.

    Storage state format: {"cookies": [{"name": ..., "value": ..., "domain": ...}, ...], "origins": [...]}
    """
    if not isinstance(data, dict):
        return False
    cookies = data.get("cookies")
    if not isinstance(cookies, list):
        return False
    # Check first item has "name" and "value" keys (Playwright cookie item)
    if len(cookies) > 0:
        first = cookies[0]
        if isinstance(first, dict) and "name" in first and "value" in first:
            return True
    # Empty cookies list is still storage_state format
    return len(cookies) == 0 and "origins" in data


def _convert_bilibili(data: dict[str, Any]) -> dict[str, Any]:
    """Convert Playwright storage_state to Bilibili flat dict format.

    Input:  {"cookies": [{"name": "SESSDATA", "value": "xxx", "domain": ".bilibili.com"}, ...]}
    Output: {"SESSDATA": "xxx", "bili_jct": "xxx", "DedeUserID": "xxx", ...}
    """
    cookies = data.get("cookies", [])
    result = {}
    for cookie in cookies:
        name = cookie.get("name", "")
        value = cookie.get("value", "")
        if name:
            result[name] = value
    return result


def _convert_douyin(data: dict[str, Any]) -> dict[str, Any]:
    """Convert Playwright storage_state to Douyin format.

    Input:  {"cookies": [{"name": "k", "value": "v", "domain": ".douyin.com"}, ...]}
    Output: {"cookie_string": "k=v; ...", "cookies": {"k": "v", ...}}
    """
    cookies = data.get("cookies", [])
    cookie_dict = {}
    for cookie in cookies:
        name = cookie.get("name", "")
        value = cookie.get("value", "")
        if name:
            cookie_dict[name] = value

    cookie_string = "; ".join(f"{k}={v}" for k, v in cookie_dict.items())

    return {
        "cookie_string": cookie_string,
        "cookies": cookie_dict,
    }


# ─── Kuaishou cookie 分桶规则（按 cookie name 精准路由，不依赖 domain）───
#
# 历史 bug（2026-06-03 修）：旧实现把所有 ".kuaishou.com" 父域 cookie 都同时
# 塞进 main + live 桶，导致 main 域 SSO token（webday7_st / passToken）被错误
# 注入到 .live.kuaishou.com 域。后续 BBA 任务启动浏览器时 → 浏览器把 main SSO
# 发到 live 子站请求 → 服务端识别"webday7 token 跨域"判定异常 → 异步级联失效
# 整个 SSO 域 → 用户掉登录态 + 滑块验证码（实测见 console "pullTokenFail"）。
#
# 修复：cookie name 是分桶**唯一可信来源**——浏览器开发者工具显示的 domain
# 视图不能反推 cookie 真实 domain 字段（httponly cookie 在 JS 也不可见）。
#
# 分桶规则（按真实业务语义，不按 domain 字段）：
#
#   * MAIN_ONLY   — server 只在 www.kuaishou.com 写入，且业务上禁止跨子域
#                   （webday7 SSO 一族 + passToken）。
#   * LIVE_ONLY   — server 只在 passport.kuaishou.com / live.kuaishou.com
#                   写入（live web_st 一族 + bfb1s + client_key）。
#   * SHARED      — 设备 / 用户 / 平台标识，main 和 live 子站都要发回 server
#                   才能维持身份连续性（did / userId / kpf / clientid 等）。
#
# 名单不在这里覆盖的 cookie 默认按 domain 路由（live.kuaishou.com → live，
# 其他 → main）保持兜底兼容。
_KS_MAIN_ONLY: frozenset[str] = frozenset({
    "passToken",
    "kuaishou.server.webday7_st",
    "kuaishou.server.webday7_ph",
})

_KS_LIVE_ONLY: frozenset[str] = frozenset({
    "kuaishou.live.web_st",
    "kuaishou.live.web_ph",
    "kuaishou.live.bfb1s",
    "client_key",
})

_KS_SHARED: frozenset[str] = frozenset({
    "did",
    "didv",
    "userId",
    "kpf",
    "kpn",
    "clientid",
    "kwfv1",
    "kwpsecproductname",
    "kwssectoken",
    "kwscode",
    "ktrace-context",
})


def _classify_kuaishou_cookie(name: str, domain: str) -> str:
    """Return one of "main_only" / "live_only" / "shared" / "unknown".

    Name-based rules take precedence over domain. Prefix rules
    ("kuaishou.server."/"kuaishou.live.") catch any future variants.
    """
    if name in _KS_MAIN_ONLY or name.startswith("kuaishou.server."):
        return "main_only"
    if name in _KS_LIVE_ONLY or name.startswith("kuaishou.live."):
        return "live_only"
    if name in _KS_SHARED:
        return "shared"
    return "unknown"


def _convert_kuaishou(data: dict[str, Any]) -> dict[str, Any]:
    """Convert Playwright storage_state to Kuaishou nested format.

    Input:  {"cookies": [{"name": "did", "value": "xxx", "domain": ".kuaishou.com"}, ...]}
    Output: {"main": {"did": "xxx", ...}, "live": {"did": "xxx", ...}}

    Routing strategy: classify by cookie name (see ``_classify_kuaishou_cookie``).
    Falls back to domain-based routing for unknown names (defensive default
    keeps unrecognised cookies reachable rather than silently dropped).
    """
    cookies = data.get("cookies", [])
    main_cookies: dict[str, str] = {}
    live_cookies: dict[str, str] = {}

    for cookie in cookies:
        name = cookie.get("name", "")
        value = cookie.get("value", "")
        domain = cookie.get("domain", "")
        if not name:
            continue

        bucket = _classify_kuaishou_cookie(name, domain)

        if bucket == "main_only":
            main_cookies[name] = value
        elif bucket == "live_only":
            live_cookies[name] = value
        elif bucket == "shared":
            # Generic device/user/platform markers — both subsites need them.
            main_cookies[name] = value
            live_cookies[name] = value
        else:
            # Unknown name: fall back to domain hint, default to main.
            if "live.kuaishou.com" in domain or "passport.kuaishou.com" in domain:
                live_cookies[name] = value
            else:
                main_cookies[name] = value

    return {
        "main": main_cookies,
        "live": live_cookies,
    }
