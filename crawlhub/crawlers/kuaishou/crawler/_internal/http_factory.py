"""
快手出网通道唯一工厂（curl_cffi + ja3 指纹 + 强制 IPv4 + 身份头注入）
=====================================================================

设计哲学（Linus 派）：
    "好代码就是不需要例外的代码。"

快手包内**所有**带 cookie 的 HTTP 出网点必须从这里拿 session。
禁止直接 ``import httpx`` / ``import requests`` 出网。

为什么不能用 httpx / 标准 requests：
    1. TLS 指纹 (ja3/ja4) — httpx/requests 用 OpenSSL，与 Chrome 的 BoringSSL
       握手序列不同。带 SSO Cookie 用 OpenSSL 出网 = 被风控判定为"会话被偷"，
       服务端会**异步级联**失效整个 SSO 域 token（kuaishou.server.webday7_st、
       kwfv1、kwssectoken 在数秒后全部 invalidate），导致该账号在所有平台掉登录态。
    2. happy-eyeballs 卡死 — libcurl 默认 IPv6 优先 + IPv4 fallback；某些
       本机 DNS 环境（实测 Win 测试机）AAAA 解析失败时 fallback 卡 ~2s 后报
       curl(7)。强制 IPv4 在 Mac 上验证 0 副作用，在 Win 上是救命稻草。

──────────────────────────────────────────────────────────────────────
为什么 setopt 必须每次请求前重新调：
    curl_cffi.Session.request() 在请求结束后会调 ``curl_easy_reset()``
    （libcurl 安全实践，避免 cookie/header 跨请求污染），它把**所有**
    通过 ``sess.curl.setopt`` 设的选项都擦掉，包括 IPRESOLVE。
    实测（2026-06-02）：
      - 同一 Session，init 后只 setopt 一次 → 第 1 次请求 OK，第 2 次起被擦掉，全 fail
      - 同一 Session，每次请求前都 setopt → N 次请求全 OK
    本模块用 ``_Ipv4ForcedSession`` 子类把 setopt 嵌入到 request 前置钩子，
    使用方拿到的就是"始终强制 IPv4"的 Session，零感知。
──────────────────────────────────────────────────────────────────────

═══════════════════════════════════════════════════════════════════════
  R7 P5：身份头强绑定（2026-06-02）
─────────────────────────────────────────────────────────────────────
  问题背景：
    crawlhub 用 BBA Chrome 登录拿 cookie，再切到 curl_cffi 用同一份
    cookie 出网。两阶段必须身份对齐——同 cookie 不同 UA / 不同
    sec-ch-ua-platform 是反爬的天然分类信号。

  curl_cffi default headers 行为（2026-06-02 实测推翻 LEARNINGS-006）：
    impersonate=chrome136 不会强制覆盖 caller headers，规则是 caller
    优先级 > default。caller 不传时 default 兜底注入；caller 传了直接
    覆盖。default 配方里所有 chromeNNN 配方的 UA 都是 **Mac**——这意味着
    Win 宿主上跑 cffi 不传 UA 会自动变成 Mac UA，跟 BBA 阶段的 Win UA
    自相矛盾，反爬秒杀。

  解法（强制契约）：
    make_session 必须接 cookie_jar。jar 的 metadata 由 BBA 第一阶段抓
    wire 真实身份头落盘（playwright_runtime context.on('request') listener）。
    cffi session 创建时从 metadata 读身份头注入到 session.headers，覆盖
    impersonate 的 default。

    metadata 缺失（cookie 没经过 BBA / BBA 抓失败）→ 直接 raise
    CookieMetadataMissing —— 不允许"裸 default Mac UA 出网"这条脏路径。

  为什么不留 strict=False 兜底：
    "好代码就是不需要例外的代码"。留兜底 = 把已知风险藏到日志里。
    ks_session 失败时立刻知道"BBA 必须先跑"比"跑通了一会儿后掉线"漂亮。
═══════════════════════════════════════════════════════════════════════

ENV 钩子（不引入分支，仅供故障演练 / 未来灰度）：
    KUAISHOU_HTTP_FORCE_IPV4=0  关闭 IPv4 强制（**默认 on**）

历史血泪（2026-06-02）：
    cfa680006e4f 任务跑完后整个账号 SSO 域全平台掉线 —— 根因是
    live_protocol._http_get_json 用 httpx 直发了 5 条带 SSO Cookie 的请求。
"""

from __future__ import annotations

import os
from typing import Any

# curl_cffi 是硬依赖。没有它就没有 ja3，没有 ja3 = 自杀。
# 不允许 try/except ImportError fallback —— 那是把已知风险藏到日志里。
from curl_cffi import requests as _curl_requests
from curl_cffi.const import CurlOpt as _CurlOpt


# ──────────────────────────────────────────────────────────
#  常量
# ──────────────────────────────────────────────────────────
# libcurl: CURL_IPRESOLVE_V4 = 1 (见 curl/include/curl/curl.h)
_CURL_IPRESOLVE_V4: int = 1

# 默认 ja3 配方：Chrome 136 是 curl_cffi 0.14 当前最高 stable target
DEFAULT_IMPERSONATE: str = "chrome136"

# ENV 钩子（默认 on；显式 "0"/"false" 才关）
_FORCE_IPV4: bool = os.environ.get(
    "KUAISHOU_HTTP_FORCE_IPV4", "1"
).lower() not in ("0", "false", "no", "off")


# ──────────────────────────────────────────────────────────
#  异常
# ──────────────────────────────────────────────────────────
class CookieMetadataMissing(RuntimeError):
    """Cookie jar 没有 origin metadata（身份头）→ 拒绝创建 cffi session.

    触发场景：
      1. 用户手动塞了一个 cookie 文件，没经过 BBA 浏览器登录流程
      2. BBA 跑了但 origin headers capture 失败（host 没匹配 / listener
         没装上 / capture 抛异常）

    修复路径：
      - 用 BBA 重新登录该 cookie（首次 navigation 会触发 capture）
      - 或调试期可手动调 KuaishouCookieJar.update_origin_headers(...,
        source="synthesized") 兜底注入（仅限调试）
    """

    def __init__(self, source: str = "<unknown>"):
        self.source = source
        super().__init__(
            f"cookie_jar at {source!r} has no origin metadata. "
            f"Run BBA login first to capture wire identity headers."
        )


# ──────────────────────────────────────────────────────────
#  IPv4-Forced Session — 每次 request 前 re-setopt
# ──────────────────────────────────────────────────────────
class _Ipv4ForcedSession(_curl_requests.Session):
    """curl_cffi.Session 的子类，强制 IPv4 解析。

    必要性：curl_cffi.Session 在每次 request 后调用 ``curl_easy_reset()``
    擦除所有 setopt（libcurl 安全实践）。如果只在 ``__init__`` 时 setopt
    一次，从第二次请求起就被擦掉，行为等同 DEFAULT。
    所以必须在每次 request **之前**重新 setopt。

    用 override request() 而非 __getattr__ / monkey-patch，
    因为 ``get`` / ``post`` / ``put`` 等便捷方法都委托到 ``request``。
    一处改动覆盖所有出口。
    """

    def request(self, *args: Any, **kwargs: Any) -> Any:
        if _FORCE_IPV4:
            self.curl.setopt(_CurlOpt.IPRESOLVE, _CURL_IPRESOLVE_V4)
        return super().request(*args, **kwargs)


# ──────────────────────────────────────────────────────────
#  公开 API — 唯一工厂
# ──────────────────────────────────────────────────────────
def make_session(
    *,
    cookie_jar: Any,
    impersonate: str = DEFAULT_IMPERSONATE,
) -> Any:
    """生产配齐 ja3 + IPv4 强制 + 身份头对齐的 curl_cffi session.

    Args:
        cookie_jar: 必须实现 ``has_origin_headers()`` / ``get_origin_headers()``
                    / ``source()`` 三个方法（见 KuaishouCookieJar）。这是身份
                    头的唯一真相源，没它就没法对齐 BBA 阶段。
        impersonate: curl_cffi 的 ja3 配方名。非法值由 curl_cffi 在第一次
                     请求时抛 InvalidImpersonate。

    Returns:
        ``_Ipv4ForcedSession`` 实例。session.headers 已注入 metadata 中的
        身份头（user-agent / sec-ch-ua / sec-ch-ua-mobile /
        sec-ch-ua-platform / accept-language），覆盖 impersonate 的 default
        Mac UA。

    Raises:
        CookieMetadataMissing: jar 没 origin metadata。修复路径见异常 docstring。

    Notes:
        - 不在此处做 connectivity probe —— fail-fast 让真错误在第一次
          业务请求时直接抛，避免 3s 启动延迟。
        - session.headers 的 update 用小写 key（与 wire 一致）；curl_cffi
          内部把 dict 转成 HTTP 头时会保留传入的大小写，下层 libcurl
          会按 RFC 7230 normalize。
    """
    if cookie_jar is None:
        raise TypeError(
            "make_session(): cookie_jar is required. "
            "Pass a KuaishouCookieJar instance."
        )
    if not cookie_jar.has_origin_headers():
        raise CookieMetadataMissing(source=cookie_jar.source())

    sess = _Ipv4ForcedSession(impersonate=impersonate)
    # session.headers 是 dict-like。update 后所有出向请求都会带上这些头，
    # 直到被单次 request(headers=...) 显式覆盖（caller 优先级仍然高）。
    sess.headers.update(cookie_jar.get_origin_headers())
    return sess


__all__ = ["make_session", "DEFAULT_IMPERSONATE", "CookieMetadataMissing"]
