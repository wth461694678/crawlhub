"""
Bilibili API Client
===================
HTTP client for Bilibili Web API calls.
Uses WBI signing for authenticated endpoints.

R4 P6 (2026-05-24): BilibiliClient now extends ``BaseHttpClient`` and
talks cookies via the ``CookieJar`` Protocol (``FileCookieJar`` by
default). The legacy name ``BilibiliAPI`` is kept as an alias so that
in-flight call sites keep working.
"""

from __future__ import annotations

import hashlib
import time
import urllib.parse
from functools import reduce
from pathlib import Path
from typing import Optional

import requests

from crawlhub.core.platform import (
    BaseHttpClient, CookieJar, FileCookieJar, ProbeResult, StringCookieJar,
)

from ._internal.live_protocol import collect_events as collect_live_protocol_events



# ═══════════════════════════════════════════════════════════
#  BV / AV Conversion
# ═══════════════════════════════════════════════════════════

XOR_CODE = 23442827791579
MASK_CODE = 2251799813685247
ALPHABET = "FcwAPNKTMug3GV5Lj7EJnHpWsx4tb8haYeviqBz6rkCy12mUSDQX9RdoZf"
ENCODE_MAP = (8, 7, 0, 5, 1, 3, 2, 4, 6)
DECODE_MAP = tuple(reversed(ENCODE_MAP))
BASE = len(ALPHABET)
PREFIX = "BV1"


def bv2av(bvid: str) -> int:
    """Convert BV ID to AV ID."""
    assert bvid[:3] == PREFIX, f"Invalid BV prefix: {bvid}"
    bvid_body = bvid[3:]
    tmp = 0
    for i in range(len(ENCODE_MAP)):
        idx = ALPHABET.index(bvid_body[DECODE_MAP[i]])
        tmp = tmp * BASE + idx
    return (tmp & MASK_CODE) ^ XOR_CODE


def av2bv(avid: int) -> str:
    """Convert AV ID to BV ID (B 站 2022 新版算法)."""
    MAX_AID = 1 << 51
    bvid = list("BV1000000000")
    tmp = (MAX_AID | avid) ^ XOR_CODE
    for i in range(9):
        bvid[ENCODE_MAP[i] + 3] = ALPHABET[tmp % BASE]
        tmp //= BASE
    return "".join(bvid)


def extract_room_id(input_str: str | int) -> int:
    """Extract live room id from number or live.bilibili.com URL."""
    if isinstance(input_str, int):
        return input_str
    raw = str(input_str or "").strip()
    if raw.isdigit():
        return int(raw)
    import re
    match = re.search(r"live\.bilibili\.com/(?:h5/)?(\d+)", raw)
    if match:
        return int(match.group(1))
    match = re.search(r"(\d+)", raw)
    if match:
        return int(match.group(1))
    raise ValueError(f"Cannot extract room_id from: {input_str!r}")


def extract_video_id(input_str: str) -> Optional[str]:
    """Extract BV ID from various input formats.


    Supports:
      - BV号: BV1xx411c7mD
      - AV号: av170001 or 170001
      - Full URL: https://www.bilibili.com/video/BV1xx411c7mD
    """
    import re
    input_str = input_str.strip()

    # Try BV ID
    match = re.search(r"(BV[0-9A-Za-z]{10})", input_str)
    if match:
        return match.group(1)

    # Try AV ID
    match = re.search(r"(?:av)?(\d{3,})", input_str, re.IGNORECASE)
    if match:
        avid = int(match.group(1))
        return av2bv(avid)

    return None


# ═══════════════════════════════════════════════════════════
#  WBI Signing
# ═══════════════════════════════════════════════════════════

_WBI_MIXIN_TABLE = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]


def _get_mixin_key(raw: str) -> str:
    """Shuffle raw key (img_key + sub_key) via mixin table, take first 32 chars."""
    return reduce(lambda s, i: s + raw[i], _WBI_MIXIN_TABLE, "")[:32]


def _sign_wbi(params: dict, mixin_key: str) -> dict:
    """Sign request params with WBI mixin_key.

    Adds wts (timestamp) and w_rid (MD5 signature) to params.
    """
    params = dict(params)
    params["wts"] = int(time.time())
    query = urllib.parse.urlencode(sorted(params.items()))
    w_rid = hashlib.md5((query + mixin_key).encode()).hexdigest()
    params["w_rid"] = w_rid
    return params


# ═══════════════════════════════════════════════════════════
#  BilibiliClient (extends BaseHttpClient)
# ═══════════════════════════════════════════════════════════

class BilibiliClient(BaseHttpClient):
    """Bilibili Web API client.

    Cookie-based auth via SESSDATA, fed through a :class:`CookieJar`.
    Supports WBI signing for endpoints that require it.

    Construction (in priority order):
      1. ``cookie_jar=...``  — direct CookieJar instance (preferred)
      2. ``cookie_path=...`` — path string, wrapped into ``FileCookieJar``
      3. neither             — empty in-memory jar (anonymous)
    """

    BASE_HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        "Referer": "https://www.bilibili.com/",
        "Origin": "https://www.bilibili.com",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }

    def __init__(
        self,
        cookie_jar: CookieJar | None = None,
        cookie_path: str | Path | None = None,
        cookie_manager: object | None = None,  # legacy back-compat (CookieManager)
    ):
        # Resolve a CookieJar from whatever the caller handed us.
        if cookie_jar is None:
            if cookie_manager is not None:
                # Legacy: wrap a CookieManager in a tiny adapter so we can keep
                # the old constructor working.  CookieManager exposes
                # get_dict() / save() but not the CookieJar protocol.
                cookie_jar = _CookieManagerAdapter(cookie_manager)
            elif cookie_path is not None:
                cookie_jar = FileCookieJar(Path(cookie_path))
            else:
                # Empty in-memory jar — anonymous client.
                cookie_jar = StringCookieJar("")

        # Stash the cookie path (used by check_cookie_status's age reporting).
        self._cookie_path: Path = (
            Path(cookie_path) if cookie_path is not None
            else Path(cookie_jar.source()) if cookie_jar is not None else Path()
        )

        self._wbi_mixin_key: Optional[str] = None
        self._wbi_key_ts: float = 0

        # BaseHttpClient stores the jar and calls _setup_sessions().
        super().__init__(cookie_jar=cookie_jar)

    # ── BaseHttpClient contract ────────────────────────────────

    def _setup_sessions(self) -> None:
        """Allocate the requests.Session and seed cookies from the jar."""
        self.session = requests.Session()
        self.session.headers.update(self.BASE_HEADERS)
        self._sync_cookies_from_jar()

    def _sync_cookies_from_jar(self) -> None:
        """Push cookie jar contents into ``self.session.cookies`` (domain-bound)."""
        self.session.cookies.clear()
        jar = self._cookie_jar
        if jar is None:
            return
        for k, v in jar.as_dict().items():
            self.session.cookies.set(k, v, domain=".bilibili.com")

    #: JS snippet for BBA login polling — runs in browser page via
    #: ``page.evaluate()``.  Returns ``{ ok: bool, reason: str, extras: {} }``.
    BROWSER_LOGIN_CHECK_JS = """\
(() => {
  const el = document.querySelector('.header-login-entry');
  const ok = el === null;
  return { ok, extras: {}, reason: ok ? '' : 'header-login-entry found' };
})()
"""

    @staticmethod
    def check_login_from_html(html: str) -> tuple[bool, dict]:
        """Check bilibili login status from page HTML.

        If ``header-login-entry`` CSS class is absent → logged in.
        Shared by ``probe()`` and BBA login polling.
        """
        from crawlhub.core.platform.probe_protocol import check_login_from_html
        return check_login_from_html(html, logged_out_class="header-login-entry")

    def probe(self, task_type: str = "default") -> ProbeResult:
        """Probe cookie validity via the homepage HTML login indicator.

        GET ``https://www.bilibili.com/`` and check for the
        ``header-login-entry`` CSS class.  If present → not logged in;
        if absent → cookie valid.

        WBI signing keys are refreshed lazily on first use — probe no
        longer calls the ``/nav`` endpoint to pre-populate them.
        """
        start = time.time()
        try:
            resp = self.session.get(
                "https://www.bilibili.com/",
                timeout=15,
                headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                },
            )
            latency_ms = int((time.time() - start) * 1000)

            if resp.status_code != 200:
                return ProbeResult(
                    ok=False,
                    api="/ (homepage)",
                    latency_ms=latency_ms,
                    error=f"HTTP {resp.status_code}",
                    extras={"task_type": task_type},
                )

            is_logged_in, extras = self.check_login_from_html(resp.text)
            return ProbeResult(
                ok=is_logged_in,
                api="/ (homepage)",
                latency_ms=latency_ms,
                error=None if is_logged_in else extras.get("reason", "login button present"),
                extras={"task_type": task_type, **extras},
            )
        except Exception as e:
            latency_ms = int((time.time() - start) * 1000)
            return ProbeResult(
                ok=False,
                api="/ (homepage)",
                latency_ms=latency_ms,
                error=str(e),
                extras={"task_type": task_type},
            )

    # ── WBI key handling ───────────────────────────────────────

    def _refresh_wbi_keys(self) -> str:
        """Fetch WBI keys from nav API and compute mixin_key.

        Keys are cached for 30 minutes.
        """
        now = time.time()
        if self._wbi_mixin_key and (now - self._wbi_key_ts) < 1800:
            return self._wbi_mixin_key

        resp = self.session.get(
            "https://api.bilibili.com/x/web-interface/nav",
            timeout=10,
        )
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"Failed to fetch WBI keys: {data.get('message')}")

        wbi_img = data["data"].get("wbi_img", {})
        img_url = wbi_img.get("img_url", "")
        sub_url = wbi_img.get("sub_url", "")

        img_key = img_url.rsplit("/", 1)[-1].split(".")[0] if img_url else ""
        sub_key = sub_url.rsplit("/", 1)[-1].split(".")[0] if sub_url else ""

        raw = img_key + sub_key
        self._wbi_mixin_key = _get_mixin_key(raw)
        self._wbi_key_ts = now
        return self._wbi_mixin_key

    def _sign_params(self, params: dict) -> dict:
        """Sign params with WBI. Auto-refreshes keys if needed."""
        mixin_key = self._refresh_wbi_keys()
        return _sign_wbi(params, mixin_key)

    # ── Login / cookie status ──────────────────────────────────

    def check_login(self) -> dict:
        """Check login status via nav API (legacy shape, kept for service.check_cookie)."""
        try:
            resp = self.session.get(
                "https://api.bilibili.com/x/web-interface/nav",
                timeout=10,
            )
            data = resp.json()
            if data.get("code") == 0:
                d = data["data"]
                wbi_img = d.get("wbi_img", {})
                if wbi_img.get("img_url") and wbi_img.get("sub_url"):
                    img_key = wbi_img["img_url"].rsplit("/", 1)[-1].split(".")[0]
                    sub_key = wbi_img["sub_url"].rsplit("/", 1)[-1].split(".")[0]
                    self._wbi_mixin_key = _get_mixin_key(img_key + sub_key)
                    self._wbi_key_ts = time.time()
                return {
                    "is_login": d.get("isLogin", False),
                    "uid": d.get("mid", 0),
                    "uname": d.get("uname", ""),
                }
            return {"is_login": False, "uid": 0, "uname": "", "error": data.get("message")}
        except Exception as e:
            return {"is_login": False, "uid": 0, "uname": "", "error": str(e)}

    def check_cookie_status(self, task_type: str = None) -> dict:
        """Check cookie health, optionally probing for a specific task type."""
        import time as _time

        cookies = self._cookie_jar.as_dict() if self._cookie_jar else {}
        sessdata_present = bool(cookies.get("SESSDATA"))

        result = {
            "cookie_id": "default",
            "cookie_file": str(self._cookie_path),
        }

        cookie_age_hours = None
        if self._cookie_path and self._cookie_path.exists():
            mtime = self._cookie_path.stat().st_mtime
            cookie_age_hours = round((_time.time() - mtime) / 3600, 1)

        tokens = {
            "SESSDATA": sessdata_present,
            "bili_jct": bool(cookies.get("bili_jct")),
            "DedeUserID": bool(cookies.get("DedeUserID")),
        }

        result["session_valid"] = sessdata_present
        result["cookie_age_hours"] = cookie_age_hours
        result["tokens"] = tokens

        if task_type is None:
            estimated = {
                "comment": "likely_ok",
                "video_detail": "likely_ok",
                "search": "likely_ok",
                "danmaku": "likely_ok" if sessdata_present else "likely_degraded",
            }
            result["estimated_capabilities"] = estimated

            issues = []
            if not sessdata_present:
                issues.append("No SESSDATA - some features may be limited")
            if not tokens["bili_jct"]:
                issues.append("No bili_jct - write operations will fail")
            result["recommendation"] = "; ".join(issues) if issues else "All tokens look healthy."
            return result

        _PROBE_BVID = "BV1GJ411x7h7"

        if task_type == "comment":
            try:
                resp = self.get_comments(_PROBE_BVID, page=1, page_size=1)
                if resp.get("code") == 0:
                    return {"ok": True, "api": "/x/v2/reply/wbi/main"}
                return {"ok": False, "api": "/x/v2/reply/wbi/main",
                        "error": f"code={resp.get('code')}: {resp.get('message')}"}
            except Exception as e:
                return {"ok": False, "api": "/x/v2/reply/wbi/main", "error": str(e)}

        elif task_type == "video_detail":
            try:
                data = self.get_video_detail(_PROBE_BVID)
                if data.get("bvid"):
                    return {"ok": True, "api": "/x/web-interface/view"}
                return {"ok": False, "api": "/x/web-interface/view", "error": "No bvid in response"}
            except Exception as e:
                return {"ok": False, "api": "/x/web-interface/view", "error": str(e)}

        elif task_type == "search":
            try:
                resp = self.search_videos("test", page=1, page_size=1)
                if resp.get("code") == 0:
                    return {"ok": True, "api": "/x/web-interface/search/type"}
                return {"ok": False, "api": "/x/web-interface/search/type",
                        "error": f"code={resp.get('code')}: {resp.get('message')}"}
            except Exception as e:
                return {"ok": False, "api": "/x/web-interface/search/type", "error": str(e)}

        elif task_type == "danmaku":
            return {
                "ok": True,
                "api": "websocket (no direct probe)",
                "note": "Danmaku uses blivedm WebSocket.",
                "SESSDATA": sessdata_present,
            }

        return {"ok": False, "error": f"No probe implemented for {task_type}"}

    # ── API Methods ──────────────────────────────────────────

    def get_video_detail_raw(self, bvid_or_aid: str) -> dict:
        """Get raw video detail payload (the full ``data["data"]`` dict from
        web-interface/view).

        Accepts a BV id, an "av<number>" form, or a numeric aid string.
        Returns the upstream payload unchanged so callers (e.g. VideoDetail.from_api)
        can extract whatever fields they need.

        Raises ValueError when the API returns a non-zero code.
        """
        bvid = (bvid_or_aid or "").strip()
        params: dict[str, str] = {}
        if bvid.startswith("BV"):
            params["bvid"] = bvid
        elif bvid.startswith("av") or bvid.isdigit():
            params["aid"] = bvid.replace("av", "")
        else:
            params["bvid"] = bvid

        resp = self.session.get(
            "https://api.bilibili.com/x/web-interface/view",
            params=params,
            timeout=15,
        )
        data = resp.json()
        if data.get("code") != 0:
            raise ValueError(f"API error {data.get('code')}: {data.get('message')}")
        return data.get("data") or {}

    def get_video_detail(self, bvid: str) -> dict:
        """Get video detail info."""
        from datetime import datetime
        resp = self.session.get(
            "https://api.bilibili.com/x/web-interface/view",
            params={"bvid": bvid},
            timeout=10,
        )
        data = resp.json()
        if data.get("code") != 0:
            raise ValueError(f"API error {data.get('code')}: {data.get('message')}")

        item = data["data"]
        stat = item.get("stat", {})
        owner = item.get("owner", {})
        pages = item.get("pages", [])

        return {
            "bvid": item.get("bvid", ""),
            "aid": item.get("aid", 0),
            "title": item.get("title", ""),
            "desc": item.get("desc", ""),
            "author_name": owner.get("name", ""),
            "author_uid": owner.get("mid", 0),
            "duration": item.get("duration", 0),
            "pubdate": datetime.fromtimestamp(item.get("pubdate", 0)).isoformat(),
            "view_count": stat.get("view", 0),
            "like_count": stat.get("like", 0),
            "coin_count": stat.get("coin", 0),
            "favorite_count": stat.get("favorite", 0),
            "share_count": stat.get("share", 0),
            "danmaku_count": stat.get("danmaku", 0),
            "reply_count": stat.get("reply", 0),
            "tname": item.get("tname", ""),
            "pages": [
                {"cid": p.get("cid"), "part": p.get("part", ""), "page": p.get("page", 1)}
                for p in pages
            ],
        }

    def get_comments(
        self,
        bvid: str,
        page: int = 1,
        page_size: int = 20,
        mode: int = 3,
    ) -> dict:
        """Get root-level comments for a video (WBI signed API)."""
        aid = bv2av(bvid)
        params = {
            "oid": aid,
            "type": 1,
            "mode": mode,
            "plat": 1,
            "pn": page,
            "ps": page_size,
        }
        signed_params = self._sign_params(params)
        resp = self.session.get(
            "https://api.bilibili.com/x/v2/reply/wbi/main",
            params=signed_params,
            timeout=10,
        )
        return resp.json()

    def get_sub_comments(
        self,
        bvid: str,
        root_comment_id: int,
        page: int = 1,
        page_size: int = 20,
    ) -> dict:
        """Get sub-comments (replies) for a root comment."""
        aid = bv2av(bvid)
        resp = self.session.get(
            "https://api.bilibili.com/x/v2/reply/reply",
            params={
                "oid": aid,
                "type": 1,
                "root": root_comment_id,
                "pn": page,
                "ps": page_size,
            },
            timeout=10,
        )
        return resp.json()

    def search_videos(
        self,
        keyword: str,
        page: int = 1,
        page_size: int = 50,
        order: str = "totalrank",
        duration: int = 0,
        pubtime_begin_s: Optional[int] = None,
        pubtime_end_s: Optional[int] = None,
    ) -> dict:
        """Search videos by keyword."""
        params = {
            "search_type": "video",
            "keyword": keyword,
            "page": page,
            "page_size": page_size,
            "order": order,
            "duration": duration,
        }
        if pubtime_begin_s is not None and pubtime_end_s is not None:
            params["pubtime_begin_s"] = int(pubtime_begin_s)
            params["pubtime_end_s"] = int(pubtime_end_s)

        resp = self.session.get(
            "https://api.bilibili.com/x/web-interface/search/type",
            params=params,
            timeout=15,
        )
        return resp.json()

    def search_live_rooms(
        self,
        keyword: str,
        page: int = 1,
        page_size: int = 50,
    ) -> dict:
        """Search live rooms by keyword."""
        resp = self.session.get(
            "https://api.bilibili.com/x/web-interface/search/type",
            params={
                "search_type": "live",
                "keyword": keyword,
                "page": page,
                "page_size": page_size,
            },
            timeout=10,
        )
        return resp.json()

    def get_live_room_snapshot(self, room_id: int) -> dict:
        """Return a complete live room snapshot from Bilibili live APIs."""
        info = self.session.get(
            "https://api.live.bilibili.com/xlive/web-room/v1/index/getInfoByRoom",
            params={"room_id": int(room_id)},
            timeout=10,
        ).json()
        if info.get("code") != 0:
            raise RuntimeError(f"getInfoByRoom code={info.get('code')}: {info.get('message')}")
        data = info.get("data") or {}
        room = data.get("room_info") or {}
        anchor = data.get("anchor_info") or {}
        base = anchor.get("base_info") or {}
        relation = anchor.get("relation_info") or {}
        return {
            "room_id": int(room.get("room_id") or room_id or 0),
            "short_id": int(room.get("short_id") or 0),
            "uid": int(room.get("uid") or 0),
            "title": str(room.get("title") or ""),
            "live_status": int(room.get("live_status") or 0),
            "is_live": int(room.get("live_status") or 0) == 1,
            "live_time": str(room.get("live_time") or ""),
            "online": int(room.get("online") or 0),
            "area_id": int(room.get("area_id") or 0),
            "area_name": str(room.get("area_name") or ""),
            "parent_area_id": int(room.get("parent_area_id") or 0),
            "parent_area_name": str(room.get("parent_area_name") or ""),
            "uname": str(base.get("uname") or ""),
            "face": str(base.get("face") or ""),
            "follower_num": int(relation.get("attention") or 0),
            "cover": str(room.get("cover") or ""),
            "keyframe": str(room.get("keyframe") or ""),
            "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }

    def get_live_danmu_info(self, room_id: int) -> dict:
        """Return token and host_list used by Bilibili live WebSocket."""
        signed = self._sign_params({"id": int(room_id), "type": 0})
        resp = self.session.get(
            "https://api.live.bilibili.com/xlive/web-room/v1/index/getDanmuInfo",
            params=signed,
            timeout=10,
        )
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"getDanmuInfo code={data.get('code')}: {data.get('message')}")
        return data.get("data") or {}

    def collect_live_events(
        self,
        *,
        room_id: int,
        duration_seconds: float,
        on_event,
        is_cancelled=None,
        event_filter: set[str] | None = None,
        wait_until_live: bool = False,
        wait_timeout_seconds: float = 86400.0,
    ) -> int:
        """Collect Bilibili live events via pure Python WebSocket.

        Stop conditions (auto):
          1. duration_seconds reached
          2. is_cancelled() returns True
          3. WSS connection closed
          4. ``PREPARING`` cmd received with matching roomid

        Args:
            wait_until_live: If True, poll get_live_room_snapshot until
                live_status=1 before connecting WSS.
            wait_timeout_seconds: Cap on the wait loop (default 24h).
            event_filter: Optional set of raw_cmd values (e.g.
                {"DANMU_MSG", "SEND_GIFT"}). Other cmds dropped pre-callback.
        """
        # ── Optional: wait for live ──
        if wait_until_live:
            wait_deadline = time.monotonic() + max(0.0, float(wait_timeout_seconds))
            poll_interval = 30.0
            while time.monotonic() < wait_deadline:
                if is_cancelled and is_cancelled():
                    return 0
                try:
                    snap = self.get_live_room_snapshot(room_id)
                    if int(snap.get("live_status") or 0) == 1:
                        break
                except Exception:
                    pass
                slept = 0.0
                while slept < poll_interval:
                    if is_cancelled and is_cancelled():
                        return 0
                    time.sleep(1.0)
                    slept += 1.0
            else:
                return 0

        snapshot = self.get_live_room_snapshot(room_id)
        real_room_id = int(snapshot.get("room_id") or room_id)
        info = self.get_live_danmu_info(real_room_id)
        token = str(info.get("token") or "")
        host_list = info.get("host_list") or []
        if not host_list:
            raise RuntimeError("getDanmuInfo returned empty host_list")
        first_host = host_list[0] or {}
        host = str(first_host.get("host") or "broadcastlv.chat.bilibili.com")
        port = int(first_host.get("wss_port") or first_host.get("ws_port") or 2245)
        # Extract uid and buvid from cookie for WSS auth
        jar_dict = self._cookie_jar.as_dict() if self._cookie_jar else {}
        uid = int(jar_dict.get("DedeUserID") or 0)
        buvid = str(jar_dict.get("buvid3") or "")
        return collect_live_protocol_events(
            room_id=real_room_id,
            token=token,
            host=host,
            port=port,
            duration_seconds=duration_seconds,
            on_event=on_event,
            is_cancelled=is_cancelled,
            uid=uid,
            buvid=buvid,
            event_filter=event_filter,
        )

    # ─────────────────────────────────────────────────────────────────

    #  Video AI Summary — wbi-signed single-shot
    # ─────────────────────────────────────────────────────────────────
    def get_video_ai_summary_raw(
        self,
        bvid: str,
        cid: int,
        up_mid: int,
        timeout: int = 15,
    ) -> dict:
        """Fetch the raw ``view/conclusion/get`` payload (wbi signed).

        ``bvid + cid + up_mid`` are all required by the API as signing
        material; we sign them via the shared WBI mixin key (auto-refreshed
        upstream) and forward the full upstream JSON unchanged so the
        scraper layer can decide how to interpret ``data.code`` /
        ``data.model_result``.
        """
        signed = self._sign_params({
            "bvid": bvid,
            "cid": int(cid),
            "up_mid": int(up_mid),
        })
        resp = self.session.get(
            "https://api.bilibili.com/x/web-interface/view/conclusion/get",
            params=signed,
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json() or {}


class _CookieManagerAdapter:
    """Tiny adapter so legacy ``CookieManager`` instances satisfy the
    ``CookieJar`` Protocol without forcing every call site to migrate.

    Only used when somebody passes ``BilibiliClient(cookie_manager=cm)``.
    Production code paths (scraper, service) now build a FileCookieJar
    directly.
    """

    def __init__(self, cm: object) -> None:
        self._cm = cm

    def is_logged_in(self) -> bool:
        return bool(getattr(self._cm, "sessdata", ""))

    def as_string(self, site: str | None = None) -> str:
        # site is ignored: bilibili is single-site.
        return getattr(self._cm, "cookie_string", "")

    def as_dict(self, site: str | None = None) -> dict[str, str]:
        # site is ignored: bilibili is single-site.
        get_dict = getattr(self._cm, "get_dict", None)
        return get_dict() if callable(get_dict) else {}

    def source(self) -> str:
        return str(getattr(self._cm, "cookie_path", "memory"))


# ── Back-compat alias ────────────────────────────────────────
# Until every call site is rewritten, keep the old name reachable.
BilibiliAPI = BilibiliClient
