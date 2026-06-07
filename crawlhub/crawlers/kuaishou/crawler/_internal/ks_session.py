"""
快手浏览器身份层（KuaishouSession）
====================================
统一管理一个"浏览器标签页"的完整身份：
  - TLS 指纹（curl_cffi impersonate）
  - Cookie Jar（main/live 域名统一管理，由 KuaishouCookieJar 提供）
  - 签名函数（kwscode/kwssectoken/kwfv1/hxfalcon 唯一来源）
  - Headers 工厂（UA/sec-ch-ua/referer 等一致性保证）
  - 请求日志（JSONL 自动记录）

R4 P12 + R5 (2026-05-25):
  Cookie data layer 已剥离到 ``KuaishouCookieJar``。本文件不再直接做
  文件 IO（load / save），所有 cookie 读写都通过 jar 完成。
  ``raw_cookies`` / ``cookie_path`` 保留为 property，转发到 jar，
  以兼容 client.py 的旧调用点。

用法：
  from ks_session import KuaishouSession

  session = KuaishouSession(cookie_path="data/cookie_full.json", log_dir="...")
  data = session.request("POST", "https://www.kuaishou.com/rest/v/search/feed",
                         site="main", json={"keyword": "王者"})
"""

import json
import os
import random
import string
import time
import uuid
from pathlib import Path
from urllib.parse import quote

# ──────────────────────────────────────────────────────────
#  HTTP 出网：唯一通道（curl_cffi + ja3 + 强制 IPv4）
#  规则：禁止在本文件 import httpx / requests 直发带 cookie 的请求。
#  历史血泪（2026-06-02 cfa680006e4f）：std_requests 静默 fallback +
#  httpx 旁路出网 = SSO 域全平台掉线。详见 http_factory.py 模块注释。
# ──────────────────────────────────────────────────────────
from .http_factory import make_session as _make_curl_session
from .browser_profiles import pick_profile, human_delay, exponential_backoff, RequestLogger
from .cookie_jar import KuaishouCookieJar
from .ks_hxfalcon import HxFalconSigner
from .ks_generate_kws import (
    generate_kwscode_kwssectoken,
    generate_kwfv1,
    aes_encrypt,
    random_string,
    KWS_AES_KEY,
    KWF_AES_KEY,
)

# ──────────────────────────────────────────────────────────
#  站点配置（消除 if/else 分支，用数据驱动）
# ──────────────────────────────────────────────────────────
SITE_CONFIG = {
    "main": {
        "origin": "https://www.kuaishou.com",
        "default_referer": "https://www.kuaishou.com/new-reco",
        "product_name": "KUAISHOU_VISION",
        "kwpsec_product": "kuaishou-vision",
        "kpn": "KUAISHOU_VISION",
        "domain": ".kuaishou.com",
        "href": "https://www.kuaishou.com/",
    },
    "live": {
        "origin": "https://live.kuaishou.com",
        "default_referer": "https://live.kuaishou.com/",
        "product_name": "PCLive",
        "kwpsec_product": "PCLive",
        "kpn": "GAME_ZONE",
        "domain": "live.kuaishou.com",
        "href": "https://live.kuaishou.com/",
    },
}


# ──────────────────────────────────────────────────────────
#  KuaishouSession — 一个浏览器标签页的完整身份
# ──────────────────────────────────────────────────────────
class KuaishouSession:
    """一个浏览器标签页的完整身份。

    所有 HTTP 请求通过此类发出，保证：
      1. TLS 指纹一致（curl_cffi impersonate）
      2. Cookie jar 统一管理（KuaishouCookieJar，dual-site）
      3. 签名函数唯一来源
      4. Headers 一致（UA/sec-ch-ua/referer）
      5. 请求日志自动记录到 JSONL
    """

    def __init__(self, cookie_path=None, profile=None, log_prefix="session",
                 log_dir=None, cookie_jar=None):
        # log_dir is optional — RequestLogger is default-off (noop when None).
        #
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        #  初始化顺序铁律（R7 P5 修复，2026-06-02）
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        #  依赖按拓扑顺序排列：
        #    profile      (无依赖)
        #    cookie_jar   (无依赖)
        #    _session     ← 依赖 profile + cookie_jar（read metadata 注入身份头）
        #    signer       (无依赖)
        #    did          ← 依赖 cookie_jar
        #    web_at       ← 依赖 cookie_jar
        #
        #  历史血泪：旧版 _make_session 只读 self.profile，所以"_session 在
        #  cookie_jar 之前赋值"的隐患埋了很久没炸。R7 P5 让 _make_session
        #  开始读 self.cookie_jar 来注入身份头，这条隐患立刻表现为
        #  AttributeError("'KuaishouSession' object has no attribute 'cookie_jar'")
        #  —— 而 dashboard cookie probe 上看到的是 expired + 'Kuaishou(被截断)。
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

        # ── 浏览器指纹 ──
        self.profile = profile or pick_profile()

        # ── Cookie Jar（单一数据源）── 必须先于 _make_session
        if cookie_jar is None:
            resolved_path = cookie_path or os.path.join(
                os.path.dirname(__file__), "..", "data", "cookie_full.json"
            )
            self.cookie_jar = KuaishouCookieJar(resolved_path)
        else:
            self.cookie_jar = cookie_jar

        # ── HTTP Session ── 依赖 profile + cookie_jar
        self._session = self._make_session()

        # ── 签名器 ──
        self.signer = HxFalconSigner()

        # ── did：复用 cookie 文件中的原 did ──
        # 历史教训（2026-05-27）：早期版本在此处强制 `self.did = "web_" + uuid.uuid4().hex`，
        # 想法是"换 did 防被封"，但带来的副作用是 did 与 cookie 文件中的
        # kwfv1/kwscode/kwssectoken/passToken/userId 全部错位 —— 服务端风控
        # 看到"同一 userId 配新 did"立即软降级，graphql 返回 schema-shape 正常
        # 但全部字段为 null（visionVideoDetail.photo=null / commentList=null）。
        # 修复：仅当 cookie 文件中没有 did 时才本地生成；正常路径直接复用原 did。
        # 主动换 did 仍可通过 regenerate_did() 显式触发（anti_crawl_recover 走这条）。
        self.did = self.cookie_jar.get_did()
        if not self.did:
            self.did = "web_" + uuid.uuid4().hex
            print(f"[DID] generated new (no did in cookie): {self.did[:20]}...")
        else:
            print(f"[DID] reused from cookie: {self.did[:20]}...")

        # ── Sentry Tracing（session 级复用 traceId）──
        self.sentry_trace_id = self._uuid4_hex()

        # ── 请求日志（使用全新 did 初始化 context）──
        self.logger = RequestLogger(log_dir=log_dir, prefix=log_prefix)
        self.logger.set_context(did=self.did, profile_name=self.profile["name"])
        self._response_observer = None

        # ── passToken 换取的 access token（直播站 userLogin 需要）──

        self.web_at = self.cookie_jar.as_dict("live").get("kuaishou.live.web.at", "")

    # ══════════════════════════════════════════════════════
    #  Cookie 兼容 property（转发到 jar）
    # ══════════════════════════════════════════════════════

    @property
    def cookie_path(self) -> Path:
        """转发到 jar.path（兼容旧 client.py）。"""
        return self.cookie_jar.path

    @property
    def raw_cookies(self) -> dict:
        """转发到 jar.data（兼容旧 client.py）。

        返回 dual-site dict ``{"main": {...}, "live": {...}}``。
        注意：这是 dict 副本，外部修改不会回写到 jar。
        """
        return self.cookie_jar.data

    # ══════════════════════════════════════════════════════
    #  公开 API — 请求
    # ══════════════════════════════════════════════════════

    def request(self, method, url, *, site="main", referer=None,
                sign_path=None, sign_query=None, sign_body=None,
                extra_headers=None, **kwargs):
        """带自动签名 + 日志的 HTTP 请求。"""
        cfg = SITE_CONFIG.get(site, SITE_CONFIG["main"])

        # ── 构建 cookie ──
        cookies = self._build_cookies(site)
        cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items() if v)

        # ── 构建 headers ──
        p = self.profile
        is_post = method.upper() == "POST" or "json" in kwargs or "data" in kwargs
        headers = {
            "accept": "application/json, text/plain, */*",
            "accept-language": "zh-CN,zh;q=0.9",
            "referer": self._safe_referer(referer or cfg["default_referer"]),
            "user-agent": p["ua"],
            "sec-ch-ua": p["sec_ch_ua"],
            "sec-ch-ua-mobile": p["sec_ch_ua_mobile"],
            "sec-ch-ua-platform": p["sec_ch_ua_platform"],
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "cookie": cookie_str,
            "kww": cookies.get("kwfv1", ""),
        }

        # GET 不带 origin（真实浏览器行为：只有 POST/PUT 才带）
        if is_post:
            headers["origin"] = cfg["origin"]

        # sentry-trace
        span_id = self._uuid4_hex()[16:]
        headers["sentry-trace"] = f"{self.sentry_trace_id}-{span_id}-0"
        headers["baggage"] = "sentry-environment=prod,sentry-release=80c170a"

        # POST json 需要 content-type
        if "json" in kwargs:
            headers["content-type"] = "application/json"

        # hxfalcon 签名
        if sign_path:
            hxf = self.signer.sign(
                url=sign_path,
                query=sign_query or {},
                request_body=sign_body or {},
            )
            params = kwargs.get("params", {})
            params["__NS_hxfalcon"] = hxf.get("hxfalcon", "")
            params.setdefault("caver", "2")
            kwargs["params"] = params

        # 合并额外 headers
        if extra_headers:
            headers.update(extra_headers)

        kwargs["headers"] = headers
        kwargs.setdefault("timeout", 15)

        return self._logged_request(method, url, **kwargs)

    # ══════════════════════════════════════════════════════
    #  公开 API — 身份管理
    # ══════════════════════════════════════════════════════

    def regenerate_did(self):
        """生成全新 did 并刷新所有关联签名 cookie。"""
        old = self.did
        self.did = "web_" + uuid.uuid4().hex
        print(f"[DID] {old[:20]}... → {self.did[:20]}...")
        self.logger.set_context(did=self.did)

    def switch_profile(self):
        """切换浏览器指纹（TLS + UA + sec-ch-ua 全换）。"""
        old_name = self.profile["name"]
        self.profile = pick_profile(exclude_name=old_name)
        self._session = self._make_session()
        print(f"[PROFILE] {old_name} → {self.profile['name']}")
        self.logger.set_context(profile_name=self.profile["name"])

    def exchange_live_token(self):
        """用主站 passToken 换取直播站 kuaishou.live.web_st + web.at。

        Returns:
            True 成功, False 失败
        """
        main_cookies = self.cookie_jar.as_dict("main")
        pass_token = main_cookies.get("passToken", "")
        if not pass_token:
            print("[WARN] passToken 为空，无法换取直播站登录态")
            return False

        # 构建 passToken 请求专用 cookie（跨域请求需手动拼）
        cfg = SITE_CONFIG["live"]
        kwscode, kwssectoken = generate_kwscode_kwssectoken(
            href=cfg["href"], did=self.did, product_name=cfg["product_name"],
        )
        kwfv1 = generate_kwfv1(
            href=cfg["href"], did=self.did, product_name=cfg["product_name"],
        )
        req_cookies = {
            "did": self.did,
            "userId": main_cookies.get("userId", ""),
            "passToken": pass_token,
            "kwpsecproductname": cfg["kwpsec_product"],
            "kwfv1": kwfv1,
            "kwssectoken": kwssectoken,
            "kwscode": kwscode,
        }
        req_cookies = {k: v for k, v in req_cookies.items() if v}

        p = self.profile
        t0 = time.time()
        try:
            resp = self._session.post(
                "https://id.kuaishou.com/pass/kuaishou/login/passToken",
                headers={
                    "Accept": "*/*",
                    "Accept-Language": "zh-CN,zh;q=0.9",
                    "Content-type": "application/x-www-form-urlencoded",
                    "Origin": "https://live.kuaishou.com",
                    "Referer": "https://live.kuaishou.com/",
                    "User-Agent": p["ua"],
                    "Sec-Ch-Ua": p["sec_ch_ua"],
                    "Sec-Ch-Ua-Mobile": p["sec_ch_ua_mobile"],
                    "Sec-Ch-Ua-Platform": p["sec_ch_ua_platform"],
                    "Sec-Fetch-Dest": "empty",
                    "Sec-Fetch-Mode": "cors",
                    "Sec-Fetch-Site": "same-site",
                },
                cookies=req_cookies,
                data={
                    "sid": "kuaishou.live.web",
                    "channelType": "UNKNOWN",
                    "encryptHeaders": "",
                },
                timeout=30,
            )
            elapsed = round((time.time() - t0) * 1000)
            data = resp.json()
            self.logger.log_request(
                method="POST",
                url="https://id.kuaishou.com/pass/kuaishou/login/passToken",
                status_code=resp.status_code,
                result=data.get("result"),
                elapsed_ms=elapsed,
            )

            if data.get("result") != 1:
                print(f"[WARN] passToken 换取失败: result={data.get('result')}")
                return False

            # 提取 token 写到 jar live-site
            live_updates: dict[str, str] = {}
            for key in ("kuaishou.live.web_st", "kuaishou.live.web_ph"):
                val = data.get(key, "")
                if val:
                    live_updates[key] = val

            self.web_at = data.get("kuaishou.live.web.at", "")
            if self.web_at:
                live_updates["kuaishou.live.web.at"] = self.web_at
                print(f"[INFO] [OK] 直播站登录态获取成功! (web.at={self.web_at[:40]}...)")
            else:
                print("[WARN] passToken 响应中未包含 kuaishou.live.web.at")

            if live_updates:
                self.cookie_jar.update_site("live", live_updates)
                self.cookie_jar.save()
            return bool(self.web_at)

        except Exception as e:
            print(f"[WARN] passToken 换取异常: {e}")
            return False

    def user_login(self):
        """调用 /live_api/baseuser/userLogin 注册用户在线状态。"""
        if not self.web_at:
            print("[WARN] web.at 为空，跳过 userLogin")
            return False

        body = {"userLoginInfo": {"authToken": self.web_at, "sid": "kuaishou.live.web"}}
        resp = self.request(
            "POST",
            "https://live.kuaishou.com/live_api/baseuser/userLogin",
            site="live",
            json=body,
            sign_path="/live_api/baseuser/userLogin",
            sign_query={"caver": "2"},
            sign_body=body,
        )
        data = resp.json()
        result = data.get("data", {}).get("result")
        if result == 1:
            print("[INFO] [OK] userLogin 成功!")
            return True
        print(f"[WARN] userLogin 失败: {data}")
        return False

    def anti_crawl_recover(self, max_retries=3):
        """反爬 400002 恢复流程：换 did → passToken → userLogin。"""
        for attempt in range(1, max_retries + 1):
            print(f"\n[ANTI-CRAWL] 第 {attempt}/{max_retries} 次重试...")

            if attempt >= 2:
                self.switch_profile()

            self.regenerate_did()
            human_delay(0.5, 1.5, "passToken 前")
            self.exchange_live_token()
            human_delay(0.5, 1.0, "userLogin 前")
            self.user_login()

            if attempt < max_retries:
                exponential_backoff(attempt, base=3.0, max_delay=30.0)

            return attempt

        return -1

    def get_cookies_str(self, site="live"):
        """获取指定站点的 cookie 字符串（供 WebSocket 握手等场景使用）。"""
        cookies = self._build_cookies(site)
        return "; ".join(f"{k}={v}" for k, v in cookies.items() if v)

    def get_raw_session(self):
        """获取底层 HTTP session（供 WebSocket 等特殊场景使用）。"""
        return self._session

    # ══════════════════════════════════════════════════════
    #  内部方法
    # ══════════════════════════════════════════════════════

    def _make_session(self):
        """创建 HTTP session。委托给 http_factory（唯一出网通道）。

        删除历史的 std_requests 静默 fallback：fallback 是把已知风险
        （TLS 指纹失配 → 触发风控 → SSO 域全平台掉线）藏到日志里。
        没有 curl_cffi 直接让 ImportError 在 import 阶段炸掉，比"先跑后掉线"
        漂亮一万倍。

        R7 P5（2026-06-02）：必须传 cookie_jar，让 cffi 获得 BBA 抓的真实
        wire 身份头（user-agent / sec-ch-ua / sec-ch-ua-platform / ...）。
        cookie_jar.metadata 缺失 → http_factory raise CookieMetadataMissing，
        逼 caller 先跑 BBA 登录补 metadata。
        """
        return _make_curl_session(
            impersonate=self.profile["impersonate"],
            cookie_jar=self.cookie_jar,
        )

    def _build_cookies(self, site="main"):
        """构建指定站点的完整 cookie dict（含实时刷新的签名 cookie）。

        统一入口，消除 ks_cli / ks_danmu_scraper 各自维护 cookie 的冗余。
        """
        cfg = SITE_CONFIG[site]
        # Read from jar; fall back to main if site missing
        raw = self.cookie_jar.as_dict(site)
        if not raw and site != "main":
            raw = self.cookie_jar.as_dict("main")

        # 签名 cookie 每次请求重新生成（避免过期）
        kwscode, kwssectoken = generate_kwscode_kwssectoken(
            href=cfg["href"], did=self.did, product_name=cfg["product_name"],
        )
        kwfv1 = generate_kwfv1(
            href=cfg["href"], did=self.did, product_name=cfg["product_name"],
        )

        # 基础 cookie + 签名 cookie 合并
        cookies = {
            "did": self.did,
            "userId": raw.get("userId", ""),
            "clientid": raw.get("clientid", "3"),
            "kpn": cfg["kpn"],
            "kwpsecproductname": cfg["kwpsec_product"],
            "kwscode": kwscode,
            "kwssectoken": kwssectoken,
            "kwfv1": kwfv1,
        }

        # 站点特有 cookie
        if site == "main":
            cookies["kpf"] = raw.get("kpf", "PC_WEB")
            cookies["kuaishou.server.webday7_st"] = raw.get("kuaishou.server.webday7_st", "")
            cookies["kuaishou.server.webday7_ph"] = raw.get("kuaishou.server.webday7_ph", "")
        elif site == "live":
            cookies["kuaishou.live.bfb1s"] = raw.get("kuaishou.live.bfb1s", "")
            cookies["client_key"] = raw.get("client_key", "65890b29")
            cookies["kuaishou.live.web_st"] = raw.get("kuaishou.live.web_st", "")
            cookies["kuaishou.live.web_ph"] = raw.get("kuaishou.live.web_ph", "")

        return cookies

    def set_response_observer(self, observer):
        self._response_observer = observer

    def _logged_request(self, method, url, **kwargs):
        """带 JSONL 日志记录的 HTTP 请求。"""
        kwargs.pop("impersonate", None)

        t0 = time.time()
        try:
            fn = self._session.get if method.upper() == "GET" else self._session.post
            resp = fn(url, **kwargs)
            elapsed = round((time.time() - t0) * 1000)

            result_val = None
            try:
                data = resp.json()
                result_val = data.get("result") or data.get("data", {}).get("result")
            except Exception:
                pass

            self.logger.log_request(
                method=method.upper(), url=url,
                status_code=resp.status_code, result=result_val,
                elapsed_ms=elapsed,
                anti_crawl=(result_val == 400002),
            )
            if self._response_observer is not None:
                try:
                    self._response_observer(resp)
                except Exception:
                    pass
            return resp


        except Exception as e:
            elapsed = round((time.time() - t0) * 1000)
            self.logger.log_request(
                method=method.upper(), url=url,
                elapsed_ms=elapsed, error=str(e),
            )
            raise

    # ══════════════════════════════════════════════════════
    #  扫码登录
    # ══════════════════════════════════════════════════════

    _ID_BASE = "https://id.kuaishou.com"
    _SID = "kuaishou.server.webday7"

    def qr_login(self, max_refresh=5):
        """完整扫码登录流程：获取二维码 → 等扫码 → 换 token → 保存 cookie。"""
        import hashlib
        self._init_web_cookies()

        for attempt in range(1, max_refresh + 1):
            print(f"\n  [第 {attempt}/{max_refresh} 次] 获取二维码...")
            qr = self._fetch_qr()
            if not qr:
                return False
            qr_token, qr_sig, _ = qr

            ok, qr_sig, expired = self._poll_qr_scan(qr_token, qr_sig)
            if ok:
                if self._accept_and_callback(qr_token, qr_sig):
                    # 登录成功 → 构造 cookie 写入 jar → 换取直播站 token → 持久化
                    self._build_and_save_cookies()
                    print("\n  🔄 换取直播站登录态...")
                    self.exchange_live_token()
                    self.cookie_jar.save()
                    return True
                return False
            if expired:
                print("  ⏰ 二维码已过期，自动刷新...")
                continue
            print("  ⏰ 扫码超时")
            return False

        print(f"  [FAIL] 已刷新 {max_refresh} 次二维码仍未扫码")
        return False

    def _login_headers(self):
        """登录流程专用 headers。"""
        p = self.profile
        return {
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": "https://www.kuaishou.com",
            "Referer": "https://www.kuaishou.com/",
            "User-Agent": p["ua"],
            "Sec-Ch-Ua": p["sec_ch_ua"],
            "Sec-Ch-Ua-Mobile": p["sec_ch_ua_mobile"],
            "Sec-Ch-Ua-Platform": p["sec_ch_ua_platform"],
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-site",
        }

    def _init_web_cookies(self):
        """访问主站获取初始 cookie（did 等）。"""
        print("  🌐 获取初始 Cookie...")
        try:
            self._session.get("https://www.kuaishou.com/new-reco", timeout=30)
        except Exception:
            pass
        jar = self._session.cookies
        existing = set(jar.keys()) if hasattr(jar, 'keys') else set(jar)
        for k, v in [("kpf", "PC_WEB"), ("kpn", "KUAISHOU_VISION"), ("clientid", "3")]:
            if k not in existing:
                jar.set(k, v, domain=".kuaishou.com")

    def _fetch_qr(self):
        """获取二维码。返回 (qr_token, qr_sig, qr_url) 或 None。"""
        import base64
        resp = self._session.post(
            f"{self._ID_BASE}/rest/c/infra/ks/qr/start",
            headers=self._login_headers(),
            data={"sid": self._SID, "channelType": "UNKNOWN", "isWebSig4": "true"},
            timeout=30,
        )
        data = resp.json()
        if data.get("result") != 1:
            print(f"  [FAIL] 获取二维码失败: result={data.get('result')}")
            return None

        qr_token = data["qrLoginToken"]
        qr_sig = data["qrLoginSignature"]
        qr_url = data.get("qrUrl", "")

        # 保存二维码图片（与 jar 文件同目录）
        image_data = data.get("imageData", "")
        if image_data:
            qr_path = self.cookie_jar.path.parent / "qrcode.png"
            qr_path.parent.mkdir(parents=True, exist_ok=True)
            qr_path.write_bytes(base64.b64decode(image_data))
            print(f"  📷 二维码已保存: {qr_path.resolve()}")
            try:
                import subprocess, sys
                if sys.platform == "darwin":
                    subprocess.Popen(["open", str(qr_path)])
                elif sys.platform == "win32":
                    os.startfile(str(qr_path))
            except Exception:
                pass

        # 终端 ASCII 二维码
        try:
            import qrcode as qr_mod
            qr = qr_mod.QRCode(border=1)
            qr.add_data(qr_url)
            qr.make(fit=True)
            qr.print_ascii(invert=True)
        except ImportError:
            if qr_url:
                print(f"  🔗 扫码链接: {qr_url}")

        print("\n  >>> 请使用快手 APP 扫描二维码 <<<\n")
        return qr_token, qr_sig, qr_url

    def _poll_qr_scan(self, qr_token, qr_sig, timeout_sec=120):
        """轮询扫码结果。返回 (success, updated_sig, expired)。"""
        start = time.time()
        while time.time() - start < timeout_sec:
            try:
                resp = self._session.post(
                    f"{self._ID_BASE}/rest/c/infra/ks/qr/scanResult",
                    headers=self._login_headers(),
                    data={
                        "qrLoginToken": qr_token,
                        "qrLoginSignature": qr_sig,
                        "channelType": "UNKNOWN",
                        "isWebSig4": "true",
                    },
                    timeout=35,
                )
            except Exception:
                continue

            data = resp.json()
            result = data.get("result")
            status = data.get("status")

            if result == 1 and "user" in data:
                u = data["user"]
                print(f"  [OK] 已扫码！用户: {u.get('user_name')} (id={u.get('user_id')})")
                return True, data.get("qrLoginSignature", qr_sig), False

            if result == 1 and status == "SCANNED":
                print("  📱 已扫码，等待确认...")
                qr_sig = data.get("qrLoginSignature", qr_sig)
                continue

            if result == 707 or (result == 1 and status == "EXPIRED"):
                return False, qr_sig, True

            if result == 1 and status in ("NEW", None):
                qr_sig = data.get("qrLoginSignature", qr_sig)

            time.sleep(2)

        return False, qr_sig, False

    def _accept_and_callback(self, qr_token, qr_sig):
        """acceptResult → qr/callback → 提取 token。"""
        import hashlib

        # Step 1: acceptResult
        resp = self._session.post(
            f"{self._ID_BASE}/rest/c/infra/ks/qr/acceptResult",
            headers=self._login_headers(),
            data={
                "qrLoginToken": qr_token,
                "qrLoginSignature": qr_sig,
                "sid": self._SID,
                "channelType": "UNKNOWN",
                "isWebSig4": "true",
            },
            timeout=30,
        )
        data = resp.json()
        if data.get("result") != 1:
            print(f"  [FAIL] acceptResult 失败: result={data.get('result')}")
            return False

        qr_token_cb = data.get("qrToken")
        if not qr_token_cb:
            print("  [FAIL] 未获取到 qrToken")
            return False

        # Step 2: qr/callback
        resp = self._session.post(
            f"{self._ID_BASE}/pass/kuaishou/login/qr/callback",
            headers=self._login_headers(),
            data={
                "qrToken": qr_token_cb,
                "sid": self._SID,
                "channelType": "UNKNOWN",
                "isWebSig4": "true",
            },
            timeout=30,
        )
        data = resp.json()
        if data.get("result") != 1:
            print(f"  [FAIL] qr/callback 失败: result={data.get('result')}")
            return False

        # 提取 token
        st = data.get(f"{self._SID}_st", "")
        user_id = data.get("userId", "")
        pass_token = data.get("passToken") or ""

        # passToken 可能通过 httpOnly Set-Cookie 设置到 jar 中
        jar = self._session.cookies
        if not pass_token:
            pass_token = jar.get("passToken", "")

        # 写入 session cookie jar
        if st:
            jar.set(f"{self._SID}_st", st, domain=".kuaishou.com", path="/")
        if pass_token:
            jar.set("passToken", pass_token, domain=".kuaishou.com", path="/")
            ph = hashlib.md5(pass_token.encode()).hexdigest()
            jar.set(f"{self._SID}_ph", ph, domain=".kuaishou.com", path="/")
            print(f"  🔑 passToken: {pass_token[:30]}...")
        else:
            print("  [WARN] passToken 未获取到")

        print(f"  👤 userId: {user_id}")
        return True

    def _build_and_save_cookies(self):
        """从 session jar 读取 cookies → 构造 dual-site dict → 写入 KuaishouCookieJar 并持久化。"""
        jar_items = self._session.cookies
        # 用迭代方式构建 jar dict，避免 RequestsCookieJar 重复 cookie 名报 CookieConflictError
        jar = {}
        for cookie in jar_items:
            jar[cookie.name] = cookie.value
        did = jar.get("did", self.did or "web_" + uuid.uuid4().hex)
        self.did = did

        new_data = {
            "main": {
                "kpf": jar.get("kpf", "PC_WEB"),
                "clientid": jar.get("clientid", "3"),
                "did": did,
                "userId": jar.get("userId", ""),
                "kuaishou.server.webday7_st": jar.get("kuaishou.server.webday7_st", ""),
                "kuaishou.server.webday7_ph": jar.get("kuaishou.server.webday7_ph", ""),
                "passToken": jar.get("passToken", ""),
                "kwpsecproductname": "kuaishou-vision",
                "kpn": "KUAISHOU_VISION",
            },
            "live": {
                "did": did,
                "userId": jar.get("userId", ""),
                "kuaishou.live.bfb1s": "",
                "clientid": jar.get("clientid", "3"),
                "client_key": "65890b29",
                "kpn": "GAME_ZONE",
                "kuaishou.live.web_st": "",
                "kuaishou.live.web_ph": "",
                "kwpsecproductname": "PCLive",
            },
        }
        self.cookie_jar.replace_all(new_data)
        self.cookie_jar.save()
        self.logger.set_context(did=self.did)
        print(f"  💾 Cookie 已保存 (did={did[:25]}...)")

    # ══════════════════════════════════════════════════════
    #  手机验证码登录（两步：发送 → 验证）
    # ══════════════════════════════════════════════════════

    def sms_send_code(self, phone: str, country_code: str = "+86") -> bool:
        """向手机号发送短信验证码。"""
        self._init_web_cookies()
        resp = self._session.post(
            f"{self._ID_BASE}/pass/kuaishou/sms/requestMobileCode",
            headers=self._login_headers(),
            data={
                "sid": self._SID,
                "type": 53,          # 53 = LOGIN，42 = REGISTER
                "countryCode": country_code,
                "phone": phone,
            },
            timeout=30,
        )
        data = resp.json()
        ok = data.get("result") == 1
        if ok:
            print(f"  [OK] 验证码已发送至 {country_code}{phone}")
        else:
            print(f"  [FAIL] 发送验证码失败: result={data.get('result')} msg={data.get('error_msg', '')}")
        return ok

    def sms_verify_code(self, phone: str, sms_code: str, country_code: str = "+86") -> bool:
        """使用短信验证码完成登录。"""
        resp = self._session.post(
            f"{self._ID_BASE}/pass/kuaishou/login/mobileCode",
            headers=self._login_headers(),
            data={
                "sid": self._SID,
                "countryCode": country_code,
                "phone": phone,
                "smsCode": sms_code,
                "setCookie": "true",
            },
            timeout=30,
        )
        data = resp.json()
        if data.get("result") != 1:
            print(f"  [FAIL] 验证码登录失败: result={data.get('result')} msg={data.get('error_msg', '')}")
            return False

        # 提取 token 并写入 session cookie jar
        import hashlib
        st = data.get(f"{self._SID}_st", "")
        pass_token = data.get("passToken", "")
        user_id = data.get("userId", "")

        jar = self._session.cookies
        if st:
            jar.set(f"{self._SID}_st", st, domain=".kuaishou.com", path="/")
        if pass_token:
            ph = hashlib.md5(pass_token.encode()).hexdigest()
            jar.set("passToken", pass_token, domain=".kuaishou.com", path="/")
            jar.set(f"{self._SID}_ph", ph, domain=".kuaishou.com", path="/")
        if user_id:
            jar.set("userId", user_id, domain=".kuaishou.com", path="/")

        print(f"  [OK] 登录成功! userId={user_id}")

        # 构建并保存 cookie，然后换取直播站 token
        self._build_and_save_cookies()
        print("  🔄 换取直播站登录态...")
        self.exchange_live_token()
        self.cookie_jar.save()
        return True

    @staticmethod
    def _safe_referer(referer):
        """编码 referer 中的非 ASCII 字符（避免 latin-1 编码错误）。"""
        return quote(referer, safe="/:?#[]@!$&'()*+,;=-._~")

    @staticmethod
    def _uuid4_hex():
        """生成符合 Sentry SDK uuid4 格式的 32 位 hex。"""
        parts = [random.randint(0, 0xFFFF) for _ in range(8)]
        parts[3] = (parts[3] & 0x0FFF) | 0x4000
        parts[4] = (parts[4] & 0x3FFF) | 0x8000
        return "".join(f"{p:04x}" for p in parts)
