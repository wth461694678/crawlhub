"""Cookie-file to Playwright storage_state conversion."""

from __future__ import annotations

import json
import logging
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  Domain 单域原则（"好品味"，2026-06-02 清理双份注入）                        ║
# ╠══════════════════════════════════════════════════════════════════════════╣
# ║  历史教训：                                                                ║
# ║    旧版 douyin 同时注入 [".douyin.com", "www.douyin.com"] 两份。           ║
# ║    RFC 6265 domain match 是后缀匹配 ——                                   ║
# ║      ".douyin.com"  已自动覆盖 www / live / aweme 等所有子域              ║
# ║      "www.douyin.com" 是精确域，只对 www.douyin.com 本身有效              ║
# ║    后果：每个 cookie 注入两份，浏览器对 live.douyin.com 请求时             ║
# ║    `www.douyin.com` 副本必被 `DomainMismatch` 阻断 ——                    ║
# ║    R7 jsonl 显示 cookies_truly_blocked_count=61（49% 阻断率）              ║
# ║    其实是双份注入的精确域副本被正确剔除，纯噪音、纯负担。                  ║
# ║                                                                          ║
# ║  原则：单一真相源。一个 platform 只用一个 dot-prefix 父域，               ║
# ║         让浏览器自己按 RFC 6265 做 domain match。                         ║
# ╚══════════════════════════════════════════════════════════════════════════╝
_PLATFORM_DOMAINS = {
    "douyin": [".douyin.com"],
    "kuaishou": [".kuaishou.com"],
}

# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  Origin 多域注入（"localStorage 跨子域隔离"，2026-06-02 修复 msToken 缺失）║
# ╠══════════════════════════════════════════════════════════════════════════╣
# ║  历史教训：                                                                ║
# ║    旧版只把 xmst (msToken) 注入到 https://www.douyin.com 一个 origin。    ║
# ║    但 W3C 同源策略下 localStorage **按 origin 严格隔离**——               ║
# ║      www.douyin.com  的 localStorage["xmst"]                              ║
# ║      live.douyin.com 的 localStorage["xmst"]                              ║
# ║    是两个完全独立的存储空间（与 Cookie 父域共享语义完全相反）。            ║
# ║                                                                          ║
# ║    后果：crawlhub 直接 goto live.douyin.com 时，acrawler.js 在该域        ║
# ║    读 localStorage["xmst"] 永远是空字符串 → 拼 query 时漏 msToken 字段，  ║
# ║    webcast/setting / webcast/room/web/enter 等接口 100% 缺签名。          ║
# ║                                                                          ║
# ║  原则：localStorage 必须显式列举每一个**实际访问的子域**。                ║
# ║         跟 _PLATFORM_DOMAINS（cookie 走父域）的存储语义截然相反，         ║
# ║         别混淆。                                                          ║
# ╚══════════════════════════════════════════════════════════════════════════╝
_PLATFORM_ORIGINS: dict[str, list[str]] = {
    "douyin": ["https://www.douyin.com", "https://live.douyin.com"],
    "kuaishou": ["https://www.kuaishou.com", "https://live.kuaishou.com"],
}



def load_storage_state(platform: str, cookie_path: str | Path) -> dict[str, Any]:
    """Load a crawlhub cookie file as Playwright storage_state."""
    path = Path(cookie_path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    if _is_storage_state(raw):
        return raw
    # Kuaishou dual-domain: split main/live cookies to correct domains
    if platform == "kuaishou" and isinstance(raw, dict) and ("main" in raw or "live" in raw):
        return _kuaishou_storage_state(raw)
    cookies, extra_params = _extract_cookie_payload(raw)
    return {
        "cookies": _to_playwright_cookies(platform, cookies),
        "origins": _to_playwright_origins(platform, cookies, extra_params),
    }


def _kuaishou_storage_state(raw: dict[str, Any]) -> dict[str, Any]:
    """Convert Kuaishou's {"main": {...}, "live": {...}} to Playwright storage_state.

    Routing rules (see ``cookie_converters._classify_kuaishou_cookie``):

      * main bucket → ``.kuaishou.com`` parent domain (covers www / id /
        passport subdomains by browser cookie scoping rules).
      * live bucket → ``.live.kuaishou.com`` (only the live subdomain).

    Defensive filtering (2026-06-03):
      Old ``_convert_kuaishou`` (pre-fix) wrote main-domain SSO tokens
      (``passToken``, ``kuaishou.server.webday7_*``) into the live bucket.
      Injecting those into ``.live.kuaishou.com`` makes the browser send
      a main-domain session cookie to live-subdomain requests, which the
      kuaishou backend treats as a session hijack signal and async-
      invalidates the SSO domain. We filter them out here so legacy
      cookie files (written by the old converter) don't blow up the
      next BBA task.
    """
    # Cookie names that must NEVER appear on .live.kuaishou.com — duplicates
    # of cookie_converters._KS_MAIN_ONLY (kept inline to avoid a cross-module
    # import dependency in the cookie-injection hot path).
    _MAIN_ONLY_FORBIDDEN_ON_LIVE = (
        "passToken",
        "kuaishou.server.webday7_st",
        "kuaishou.server.webday7_ph",
    )

    pw_cookies: list[dict[str, Any]] = []
    _HTTPONLY = {"passToken", "kuaishou.server.webday7_st", "kuaishou.server.webday7_ph",
                "kuaishou.live.web_st", "kuaishou.live.web_ph"}
    for name, value in (raw.get("main") or {}).items():
        if not value:
            continue
        pw_cookies.append({
            "name": str(name), "value": str(value),
            "domain": ".kuaishou.com", "path": "/",
            "httpOnly": name in _HTTPONLY,
            "secure": True, "sameSite": "Lax",
        })
    _quarantined: list[str] = []
    for name, value in (raw.get("live") or {}).items():
        if not value:
            continue
        if name in _MAIN_ONLY_FORBIDDEN_ON_LIVE or str(name).startswith("kuaishou.server."):
            # Legacy file pollution — skip silently with a warning so the
            # caller can see it once and re-login to overwrite the bad file.
            _quarantined.append(str(name))
            continue
        pw_cookies.append({
            "name": str(name), "value": str(value),
            "domain": ".live.kuaishou.com", "path": "/",
            "httpOnly": name in _HTTPONLY,
            "secure": True, "sameSite": "Lax",
        })
    if _quarantined:
        logger.warning(
            "[cookie_injection] kuaishou cookie file has %d main-only cookie(s) "
            "in live bucket (legacy converter bug); skipping injection to "
            ".live.kuaishou.com. Names: %s. Re-login to clean up.",
            len(_quarantined), ",".join(_quarantined),
        )
    return {"cookies": pw_cookies, "origins": []}




def _is_storage_state(raw: Any) -> bool:
    if not isinstance(raw, dict):
        return False
    cookies = raw.get("cookies")
    return isinstance(cookies, list) and all(
        isinstance(item, dict) and "name" in item and "value" in item and "domain" in item
        for item in cookies
    )


def _extract_cookie_payload(raw: Any) -> tuple[dict[str, str], dict[str, str]]:
    cookies: dict[str, str] = {}
    extra_params: dict[str, str] = {}
    if isinstance(raw, dict):
        extra_params = {str(k): str(v) for k, v in (raw.get("extra_params") or {}).items() if v is not None}

        # ── Kuaishou dual-domain format: {"main": {...}, "live": {...}} ──
        # Both sub-dicts are flat {cookie_name: cookie_value} mappings.
        # We merge them into a single flat dict for _to_playwright_cookies;
        # domain assignment for live.kuaishou.com is handled downstream
        # by _to_playwright_cookies_kuaishou.
        if "main" in raw and isinstance(raw["main"], dict):
            cookies.update({str(k): str(v) for k, v in raw["main"].items() if v is not None})
        if "live" in raw and isinstance(raw["live"], dict):
            cookies.update({str(k): str(v) for k, v in raw["live"].items() if v is not None})
        if cookies and ("main" in raw or "live" in raw):
            return cookies, extra_params

        blob = raw.get("cookies")
        if isinstance(blob, dict):
            cookies.update({str(k): str(v) for k, v in blob.items() if v is not None})
        elif isinstance(blob, list):
            cookies.update(_cookies_from_list(blob))
        if not cookies and isinstance(raw.get("cookie_string"), str):
            cookies.update(_cookies_from_string(raw["cookie_string"]))
        if not cookies:
            for key, value in raw.items():
                if key in {"extra_headers", "extra_params", "saved_at", "cookie_string"}:
                    continue
                if isinstance(value, (str, int, float)):
                    cookies[str(key)] = str(value)
    elif isinstance(raw, list):
        cookies.update(_cookies_from_list(raw))
    return cookies, extra_params



def _cookies_from_list(items: list[Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        value = item.get("value")
        if name and value is not None:
            out[str(name)] = str(value)
    return out


def _cookies_from_string(cookie_string: str) -> dict[str, str]:
    parsed = SimpleCookie()
    parsed.load(cookie_string)
    return {key: morsel.value for key, morsel in parsed.items() if morsel.value}


def _to_playwright_cookies(platform: str, cookies: dict[str, str]) -> list[dict[str, Any]]:
    domains = _PLATFORM_DOMAINS.get(platform, [f".{platform}.com"])
    out: list[dict[str, Any]] = []
    for domain in domains:
        for name, value in cookies.items():
            if not value:
                continue
            out.append({
                "name": name,
                "value": value,
                "domain": domain,
                "path": "/",
                "httpOnly": False,
                "secure": True,
                "sameSite": "Lax",
            })
    return out


def _to_playwright_origins(
    platform: str,
    cookies: dict[str, str],
    extra_params: dict[str, str],
) -> list[dict[str, Any]]:
    origins = _PLATFORM_ORIGINS.get(platform) or []
    if not origins:
        return []
    ms_token = cookies.get("msToken") or extra_params.get("msToken")
    items: list[dict[str, str]] = []
    if ms_token:
        items.append({"name": "xmst", "value": ms_token})
    if cookies.get("sessionid_ss") or cookies.get("sessionid"):
        items.append({"name": "HasUserLogin", "value": "1"})
    if not items:
        return []
    # localStorage 跨子域隔离 —— 同一份 items 必须独立写入每个访问目标域，
    # 否则 acrawler.js 在 live.douyin.com 读不到 xmst，msToken 拼不出来。
    return [{"origin": origin, "localStorage": items} for origin in origins]
