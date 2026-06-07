"""
七麦数据（qimai.cn）爬虫客户端

逆向自 webpack 模块 65165（axios 拦截器）和 21725（加密工具）
签名算法：analysis 参数生成

支持接口：
    1. 登录鉴权（POST /accountV1/login）
    2. 全球榜单一览（GET /rank/globalrank）
    3. 游戏名称查ID（GET /search/index）
    4. 游戏ID查排名（GET /app/rankMore）
    5. 用户信息（GET /account/userinfo）

使用方式：
    client = QimaiClient()
    client.login("username", "password")
    results = client.search_app("逆战")
    data = client.get_rank_daily("1645516720", "2026-03-03", "2026-03-09")
"""

import base64
import time
import json
import csv
import io
import logging
import re
import sys
from typing import Optional, Dict, List, Any, Union
from urllib.parse import quote

import requests

from crawlhub.core.platform import (
    BaseHttpClient, CookieJar, ProbeResult, StringCookieJar,
)

logger = logging.getLogger(__name__)


class QimaiClient(BaseHttpClient):
    """七麦数据 API 客户端，维护 cookie 和 analysis 签名生成逻辑

    R4 P10 (2026-05-24):
      * extends ``BaseHttpClient`` and implements ``_setup_sessions`` + ``probe``
      * cookie/credential plumbing untouched — analysis signature / login flow
        live entirely in this class (no _internal/ split needed)
    """

    # ============ 逆向常量 ============

    # XOR 密钥种子（逆向自混淆变量 Bt = "qimai@2022&Technology"）
    _XOR_KEY_SOURCE = "qimai@2022&Technology"
    # 分隔符
    _SEPARATOR = "@#"
    # 签名版本号
    _VERSION = 3
    # 基准时间戳（≈2022-08-23，硬编码在拦截器中）
    _BASE_TIMESTAMP = 1661224081041
    # API 基础 URL
    _BASE_URL = "https://api.qimai.cn"

    _DEFAULT_HEADERS = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/131.0.0.0 Safari/537.36",
        "Referer": "https://www.qimai.cn/",
        "Origin": "https://www.qimai.cn",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }

    def __init__(
        self,
        username: Optional[str] = None,
        password: Optional[str] = None,
        synct: Optional[Union[int, float]] = None,
        syncd: Optional[Union[int, float]] = None,
        cookie_string: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
        cookie_jar: CookieJar | None = None,
    ):
        """
        初始化七麦客户端

        参数:
            username: 登录账号（可选，提供后自动登录）
            password: 登录密码（可选，提供后自动登录）
            synct: 服务端时间戳 cookie 值（从浏览器 cookie 'synct' 获取）
            syncd: 时间差 cookie 值（从浏览器 cookie 'syncd' 获取）
            cookie_string: 完整的 cookie 字符串（可选，会自动解析 synct/syncd）
            headers: 自定义请求头（可选）
            cookie_jar: R4 CookieJar instance (preferred); falls back to StringCookieJar
        """
        # Stash construction params; super().__init__ will trigger _setup_sessions
        # which depends on these.
        self._extra_headers = headers
        self._init_cookie_string = cookie_string
        self._xor_key = self._fnv1a_hash(self._XOR_KEY_SOURCE, return_hex=True)
        self._logged_in = False
        self._userinfo: Optional[Dict[str, Any]] = None
        self._synct = synct
        self._syncd = syncd
        self._difftime: Optional[float] = None

        if cookie_jar is None:
            cookie_jar = StringCookieJar(cookie_string or "")

        # BaseHttpClient stores the jar and calls _setup_sessions().
        super().__init__(cookie_jar=cookie_jar)

        # 如果提供了账号密码，自动登录（必须在 session 已建好之后）
        if username and password:
            self.login(username, password)

    # ── BaseHttpClient contract ────────────────────────────────

    def _setup_sessions(self) -> None:
        """Allocate the single requests.Session for qimai API calls."""
        self._session = requests.Session()
        self._session.headers.update(self._DEFAULT_HEADERS)
        if self._extra_headers:
            self._session.headers.update(self._extra_headers)

        # Resolve cookie string from jar if not explicitly given.
        cookie_str = self._init_cookie_string
        if not cookie_str and self._cookie_jar is not None:
            cookie_str = self._cookie_jar.as_string() or ""

        if cookie_str:
            self._parse_cookies(cookie_str)
            # Seed the session's Cookie header so signed requests carry auth.
            self._session.headers["Cookie"] = cookie_str

        # 缓存时间差（必须在 synct/syncd 解析完之后）
        self._update_difftime()

    #: JS snippet for BBA login polling — runs in browser page via
    #: ``page.evaluate()``.  Returns ``{ ok: bool, reason: str, extras: {} }``.
    BROWSER_LOGIN_CHECK_JS = """\
(() => {
  const el = document.querySelector('.header_register');
  const ok = el === null;
  return { ok, extras: {}, reason: ok ? '' : 'header_register class found' };
})()
"""

    @staticmethod
    def check_login_from_html(html: str) -> tuple[bool, dict]:
        """Check qimai login status from page HTML.

        If ``header_register`` CSS class (the login/register container) is absent
        → logged in.
        Shared by ``probe()`` and BBA login polling.
        """
        from crawlhub.core.platform.probe_protocol import check_login_from_html
        return check_login_from_html(html, logged_out_class="header_register")

    def probe(self, task_type: str = "default") -> ProbeResult:
        """Probe cookie validity via the homepage HTML login indicator.

        GET ``https://www.qimai.cn/`` and check for the ``header_register`` CSS
        class (the login/register button container).  If present → not
        logged in; if absent → cookie valid.

        No synct/syncd header_registering needed — this is a plain HTML page fetch.
        """
        api_path = "/ (homepage)"
        start = time.time()
        try:
            resp = self._session.get(
                "https://www.qimai.cn/",
                timeout=15,
                headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                },
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
            if is_logged_in:
                self._logged_in = True
            return ProbeResult(
                ok=is_logged_in,
                api=api_path,
                latency_ms=latency_ms,
                error=None if is_logged_in else extras.get("reason", "login button present"),
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

    # ============ 加密原语（还原自 webpack 模块 21725）============

    @staticmethod
    def _fnv1a_hash(s: str, return_hex: bool = False, seed: int = 2166136261) -> Union[int, str]:
        """
        FNV-1a 哈希算法

        对应 mod.nF / 原始函数 y(n)
        当 return_hex=True 时，返回 "xyz" + hex(hash) + "efgh" 的后16位字符串
        """
        h = seed
        for ch in s:
            h ^= ord(ch)
            h = (h + ((h << 1) & 0xFFFFFFFF) + ((h << 4) & 0xFFFFFFFF) +
                 ((h << 7) & 0xFFFFFFFF) + ((h << 8) & 0xFFFFFFFF) +
                 ((h << 24) & 0xFFFFFFFF)) & 0xFFFFFFFF
        if return_hex:
            hex_str = format(h, 'x')
            full = "xyz" + hex_str + "efgh"
            return full[-16:]
        return h

    @staticmethod
    def _xor_encrypt(s: str, key: str) -> str:
        """
        XOR 加密（标准版）

        对应 mod.i / 原始函数 v(n, t)
        """
        key_len = len(key)
        return "".join(
            chr(ord(c) ^ ord(key[i % key_len]))
            for i, c in enumerate(s)
        )

    @staticmethod
    def _xor_encrypt_offset10(s: str, key: str) -> str:
        """
        XOR 加密（offset 10 版本）

        对应 mod.oZ / 原始函数 h(n, t)
        """
        key_len = len(key)
        return "".join(
            chr(ord(c) ^ ord(key[(i + 10) % key_len]))
            for i, c in enumerate(s)
        )

    @staticmethod
    def _base64_encode(s: str) -> str:
        """Base64 编码（对应 mod.cv）"""
        return base64.b64encode(s.encode("utf-8")).decode("ascii")

    @staticmethod
    def _base64_decode(s: str) -> str:
        """Base64 解码（对应 mod.Jx）"""
        return base64.b64decode(s).decode("utf-8")

    # ============ Cookie / 时间同步 ============

    def _parse_cookies(self, cookie_string: str):
        """从 cookie 字符串中解析 synct 和 syncd"""
        synct_match = re.search(r'(?:^|;\s*)synct=([^;]*)', cookie_string)
        syncd_match = re.search(r'(?:^|;\s*)syncd=([^;]*)', cookie_string)
        if synct_match and self._synct is None:
            self._synct = float(synct_match.group(1))
        if syncd_match and self._syncd is None:
            self._syncd = float(syncd_match.group(1))

    def _update_difftime(self):
        """计算客户端与服务端的时间差"""
        if self._syncd is not None:
            self._difftime = -self._syncd
        elif self._synct is not None:
            self._difftime = int(time.time() * 1000) - 1000 * self._synct
        else:
            self._difftime = 0

    def set_cookies(self, synct: Optional[float] = None, syncd: Optional[float] = None):
        """手动更新时间同步 cookie 值"""
        if synct is not None:
            self._synct = synct
        if syncd is not None:
            self._syncd = syncd
        self._update_difftime()

    # ============ analysis 签名生成 ============

    def _generate_analysis(self, params: Dict[str, str], api_path: str) -> str:
        """
        生成 analysis 签名参数

        流程：
        1. 收集所有参数值（排除 analysis），排序拼接
        2. base64 encode
        3. 拼接: base64(参数值) + @# + API路径 + @# + 时间戳 + @# + 版本号
        4. XOR(offset=10) 加密
        5. base64 encode → 最终 analysis
        """
        now_ms = int(time.time() * 1000)
        relative_time = now_ms - (self._difftime or 0) - self._BASE_TIMESTAMP

        param_values = []
        for key, value in params.items():
            if key == "analysis":
                continue
            param_values.append(str(value))
        sorted_values = "".join(sorted(param_values))

        encoded_values = self._base64_encode(sorted_values)

        header_register_string = (
            f"{encoded_values}"
            f"{self._SEPARATOR}{api_path}"
            f"{self._SEPARATOR}{relative_time}"
            f"{self._SEPARATOR}{self._VERSION}"
        )

        xor_result = self._xor_encrypt_offset10(header_register_string, self._xor_key)
        analysis = self._base64_encode(xor_result)

        return analysis

    # ============ 底层请求方法 ============

    def _get_request(self, api_path: str, params: Dict[str, str]) -> Dict[str, Any]:
        """
        发起带 analysis 签名的 GET 请求

        参数:
            api_path: API 路径
            params: 查询参数（不含 analysis）

        返回:
            API 响应的 JSON 数据
        """
        analysis = self._generate_analysis(params, api_path)
        url = f"{self._BASE_URL}{api_path}"

        full_params = dict(params)
        full_params["analysis"] = analysis

        resp = self._session.get(url, params=full_params, timeout=30)
        resp.raise_for_status()

        data = resp.json()
        code = data.get("code")
        if code == 20000:
            # code=20000 means "no ranking data for this app" — not an error.
            # Return the raw response so callers can handle the empty case.
            return data
        if code != 10000:
            raise ValueError(
                f"API 返回错误: code={code}, msg={data.get('msg')}"
            )
        return data

    def _post_request(self, api_path: str, form_data: Dict[str, str]) -> Dict[str, Any]:
        """
        发起带 analysis 签名的 POST 请求（x-www-form-urlencoded）

        参数:
            api_path: API 路径
            form_data: 表单数据（不含 analysis）

        返回:
            API 响应的 JSON 数据
        """
        analysis = self._generate_analysis(form_data, api_path)
        url = f"{self._BASE_URL}{api_path}"

        full_data = dict(form_data)
        full_data["analysis"] = analysis

        resp = self._session.post(url, data=full_data, timeout=30)
        resp.raise_for_status()

        data = resp.json()
        if data.get("code") != 10000:
            raise ValueError(
                f"API 返回错误: code={data.get('code')}, msg={data.get('msg')}"
            )
        return data

    # 兼容旧接口名
    _request = _get_request

    # ============ 接口1: 登录鉴权 ============

    def login(self, username: str, password: str) -> Dict[str, Any]:
        """
        账号密码登录

        端点: POST /accountV1/login
        登录成功后 session 会自动保存 PHPSESSID / USERINFO / AUTHKEY 等 cookie

        参数:
            username: 手机号或用户名
            password: 密码

        返回:
            登录响应数据（含 userinfo）
        """
        form_data = {
            "username": username,
            "password": password,
        }
        data = self._post_request("/accountV1/login", form_data)

        self._logged_in = True
        self._userinfo = data.get("userinfo")
        return data

    @property
    def is_logged_in(self) -> bool:
        """当前是否已登录"""
        return self._logged_in

    @property
    def userinfo(self) -> Optional[Dict[str, Any]]:
        """当前登录的用户信息"""
        return self._userinfo

    # ============ 接口2: 全球榜单一览 ============

    def get_global_rank(
        self,
        date: str,
        genre: str = "6014",
        device: str = "iphone",
        area: str = "0",
        brand: str = "grossing",
    ) -> Dict[str, Any]:
        """
        获取全球榜单排名数据

        端点: GET /rank/globalrank

        参数:
            date: 日期（如 "2026-03-05"）
            genre: 分类ID，默认 "6014"（游戏）
                   常用值: "36"=全部, "6014"=游戏, "6018"=图书, "6015"=商务
            device: 设备类型，默认 "iphone"，可选 "ipad"
            area: 地区代码，默认 "0"（全部地区）
            brand: 榜单类型，"free"=免费榜, "paid"=付费榜, "grossing"=畅销榜

        返回:
            完整的 API 响应数据，包含 globalRankInfo（各国排名列表）
        """
        params = {
            "date": date,
            "genre": genre,
            "device": device,
            "area": area,
            "brand": brand,
        }
        return self._get_request("/rank/globalrank", params)

    def get_global_rank_apps(
        self,
        date: str,
        genre: str = "6014",
        device: str = "iphone",
        area: str = "0",
        brand: str = "grossing",
    ) -> List[Dict[str, Any]]:
        """
        获取全球榜单应用列表（简化版）

        返回格式:
            [
                {
                    "country": "中国",
                    "country_code": "cn",
                    "apps": [
                        {"rank": 1, "app_id": "989673964", "app_name": "王者荣耀", "icon": "..."},
                        ...
                    ]
                },
                ...
            ]
        """
        data = self.get_global_rank(date, genre, device, area, brand)
        result = []
        for group in data.get("globalRankInfo", []):
            country_info = {
                "country": group.get("country_name", ""),
                "country_code": group.get("country_code", ""),
                "apps": [],
            }
            for app in group.get("list", []):
                country_info["apps"].append({
                    "rank": app.get("times"),
                    "app_id": app.get("app_id"),
                    "app_name": app.get("app_name"),
                    "icon": app.get("artwork_s"),
                })
            result.append(country_info)
        return result

    # ============ 接口3: 游戏名称查ID ============

    def search_app(
        self,
        keyword: str,
        country: str = "cn",
        version: str = "ios14",
    ) -> Dict[str, Any]:
        """
        根据关键词搜索应用

        端点: GET /search/index

        参数:
            keyword: 搜索关键词（如 "逆战"）
            country: 国家代码，默认 "cn"
            version: iOS 版本，默认 "ios14"

        返回:
            完整的 API 响应数据（含 appList、wordInfo 等）
        """
        params = {
            "search": keyword,
            "country": country,
            "version": version,
        }
        return self._get_request("/search/index", params)

    def search_app_simple(
        self,
        keyword: str,
        country: str = "cn",
    ) -> List[Dict[str, str]]:
        """
        根据关键词搜索应用（简化版，只返回 appId + appName 列表）

        参数:
            keyword: 搜索关键词
            country: 国家代码

        返回:
            应用列表，格式:
            [
                {"app_id": "1645516720", "app_name": "逆战：未来", "subtitle": "...", "icon": "..."},
                ...
            ]
        """
        data = self.search_app(keyword, country)
        result = []
        for item in data.get("appList", []):
            info = item.get("appInfo")
            # 七麦 appList 偶尔夹带非 app 项（专题卡片/活动位/推荐组），
            # 结构里没有 appInfo 子字段。直接跳过，避免落入空记录。
            if not info or not isinstance(info, dict) or not info.get("appId"):
                logger.debug(
                    "[qimai] skip non-app item in appList: keys=%s",
                    list(item.keys()) if isinstance(item, dict) else type(item).__name__,
                )
                continue
            result.append({
                "app_id": info.get("appId", ""),
                "app_name": info.get("appName", ""),
                "subtitle": info.get("subtitle", ""),
                "icon": info.get("icon", ""),
            })
        return result

    def get_app_id(self, keyword: str, country: str = "cn") -> Optional[str]:
        """
        根据游戏名称精确查找 appId（返回第一个匹配结果的 ID）

        参数:
            keyword: 搜索关键词（越精确越好）
            country: 国家代码

        返回:
            appId 字符串，未找到返回 None
        """
        apps = self.search_app_simple(keyword, country)
        if apps:
            return apps[0]["app_id"]
        return None

    # ============ 接口4: 游戏ID查排名 / 基础信息 ============

    def get_rank_daily(
        self,
        app_id: str,
        sdate: str,
        edate: str,
        country: str = "cn",
        brand: str = "free",
        subclass: str = "all",
    ) -> Dict[str, Any]:
        """
        获取应用的按天排名数据

        端点: GET /app/rankMore

        参数:
            app_id: 应用 ID（如 "1645516720"）
            sdate: 开始日期（如 "2026-03-03"）
            edate: 结束日期（如 "2026-03-09"）
            country: 国家/地区代码，默认 "cn"
            brand: 榜单类型，"free"=免费榜, "paid"=付费榜, "grossing"=畅销榜
            subclass: 子分类，默认 "all"（全部分类）

        返回:
            完整的 API 响应数据，包含 list 和 table 两个字段
        """
        params = {
            "appid": app_id,
            "country": country,
            "export_type": "app_rank",
            "brand": brand,
            "device": "iphone",
            "day": "1",
            "appRankShow": "1",
            "subclass": subclass,
            "simple": "1",
            "rankType": "day",
            "sdate": sdate,
            "edate": edate,
            "rankEchartType": "1",
        }
        return self._get_request("/app/rankMore", params)

    def get_rank_hourly(
        self,
        app_id: str,
        sdate: str,
        edate: str,
        country: str = "cn",
        brand: str = "free",
        subclass: str = "all",
    ) -> Dict[str, Any]:
        """
        获取应用的按小时排名数据

        端点: GET /app/rankMore
        """
        params = {
            "appid": app_id,
            "country": country,
            "export_type": "app_rank",
            "brand": brand,
            "device": "iphone",
            "day": "0",
            "appRankShow": "1",
            "subclass": subclass,
            "simple": "1",
            "rankType": "day",
            "sdate": sdate,
            "edate": edate,
            "rankEchartType": "0",
        }
        return self._get_request("/app/rankMore", params)

    def get_rank_table(
        self,
        app_id: str,
        sdate: str,
        edate: str,
        country: str = "cn",
        brand: str = "free",
    ) -> List[Dict[str, Any]]:
        """
        获取按天排名的 table 数据（按日期 + 分类ID 的结构化数据）

        返回格式:
            [{"date": 1772467200000, "6014": 48, "7001": 15}, ...]
        """
        data = self.get_rank_daily(app_id, sdate, edate, country, brand)
        return data["data"]["table"]

    def get_rank_list(
        self,
        app_id: str,
        sdate: str,
        edate: str,
        country: str = "cn",
        brand: str = "free",
    ) -> List[Dict[str, Any]]:
        """
        获取按天排名的 list 数据（按分类的排名序列）

        返回格式:
            [{"name": "游戏(免费)", "genre_id": "6014", "data": [[48, 1], ...]}, ...]
        """
        data = self.get_rank_daily(app_id, sdate, edate, country, brand)
        return data["data"]["list"]

    # ============ 接口5: 用户信息 ============

    def get_userinfo(self) -> Dict[str, Any]:
        """
        获取当前登录用户信息

        端点: GET /account/userinfo

        返回:
            用户信息字典，包含 username, realname, isVip, isSvip 等字段
        """
        data = self._get_request("/account/userinfo", {})
        self._userinfo = data.get("userinfo")
        self._logged_in = data.get("is_logout", 1) == 0
        return data

    # ============ 数据导出 ============

    def export_rank_csv(
        self,
        app_id: str,
        sdate: str,
        edate: str,
        output_path: Optional[str] = None,
        country: str = "cn",
        brand: str = "free",
    ) -> str:
        """
        导出按天排名数据为 CSV 格式

        参数:
            app_id: 应用 ID
            sdate: 开始日期
            edate: 结束日期
            output_path: 输出文件路径（可选，不提供则返回 CSV 字符串）
            country: 国家/地区代码
            brand: 榜单类型

        返回:
            CSV 内容字符串
        """
        data = self.get_rank_daily(app_id, sdate, edate, country, brand)
        table = data["data"]["table"]
        rank_list = data["data"]["list"]

        if not table:
            return ""

        # 构建分类 ID → 名称 的映射
        genre_map = {}
        for item in rank_list:
            genre_map[item["genre_id"]] = item["name"]

        genre_ids = [k for k in table[0].keys() if k != "date"]

        output = io.StringIO()
        writer = csv.writer(output)

        header = ["日期"] + [genre_map.get(gid, gid) for gid in genre_ids]
        writer.writerow(header)

        for row in table:
            date_str = time.strftime(
                "%Y-%m-%d",
                time.localtime(row["date"] / 1000)
            )
            values = [date_str] + [row.get(gid, "-") for gid in genre_ids]
            writer.writerow(values)

        csv_content = output.getvalue()

        if output_path:
            with open(output_path, "w", encoding="utf-8-sig", newline="") as f:
                f.write(csv_content)

        return csv_content

    def export_rank_json(
        self,
        app_id: str,
        sdate: str,
        edate: str,
        output_path: Optional[str] = None,
        country: str = "cn",
        brand: str = "free",
    ) -> Dict[str, Any]:
        """
        导出按天排名数据为 JSON 格式
        """
        data = self.get_rank_daily(app_id, sdate, edate, country, brand)
        table = data["data"]["table"]
        rank_list = data["data"]["list"]

        genre_map = {}
        for item in rank_list:
            genre_map[item["genre_id"]] = item["name"]

        result = {
            "app_id": app_id,
            "country": country,
            "brand": brand,
            "sdate": sdate,
            "edate": edate,
            "categories": genre_map,
            "data": [],
        }

        for row in table:
            date_str = time.strftime(
                "%Y-%m-%d",
                time.localtime(row["date"] / 1000)
            )
            entry = {"date": date_str}
            for gid in genre_map:
                entry[genre_map[gid]] = row.get(gid, None)
            result["data"].append(entry)

        if output_path:
            with open(output_path, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=2)

        return result

    def export_global_rank_csv(
        self,
        date: str,
        output_path: Optional[str] = None,
        genre: str = "6014",
        device: str = "iphone",
        area: str = "0",
        brand: str = "grossing",
    ) -> str:
        """
        导出全球榜单为 CSV 格式

        返回:
            CSV 内容字符串
        """
        data = self.get_global_rank(date, genre, device, area, brand)

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["国家", "排名", "应用ID", "应用名称"])

        for group in data.get("globalRankInfo", []):
            country_name = group.get("country_name", "")
            for app in group.get("list", []):
                writer.writerow([
                    country_name,
                    app.get("times"),
                    app.get("app_id"),
                    app.get("app_name"),
                ])

        csv_content = output.getvalue()

        if output_path:
            with open(output_path, "w", encoding="utf-8-sig", newline="") as f:
                f.write(csv_content)

        return csv_content
