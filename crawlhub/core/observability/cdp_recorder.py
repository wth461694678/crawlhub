"""
================================================================================
 R7 Observability — CDP Recorder（Phase 2）
================================================================================

挂载点（spec §3.3）：
  BrowserSessionProvider.hold() 拿到 raw_page 之后，主线程 ContextVar 已 set,
  调 cdp_recorder.attach(raw_page, ctx) — ctx 闭包到所有 callback。

CDP 事件（spec §3.3 + ExtraInfo 升级）：
  ─ HTTP 主流（fetch 阶段视图）─
  - Network.requestWillBeSent     → source=browser_network phase=request
  - Network.responseReceived      → source=browser_network phase=response
  - (异步) Network.getResponseBody → phase=response_body  仅 content-type 白名单
  ─ HTTP ExtraInfo（浏览器最终发送视图）─
  - Network.requestWillBeSentExtraInfo  → source=browser_network phase=request_extra
       带浏览器最终注入的全部 headers（cookie / sec-fetch-* / accept-encoding）
  - Network.responseReceivedExtraInfo   → source=browser_network phase=response_extra
       带服务器原始返回的全部 headers（含 set-cookie）
  ─ WSS ─
  - Network.webSocketCreated      → 关联 (requestId, url) 进 ws_url_map
  - Network.webSocketFrameSent    → source=browser_ws phase=ws_send
  - Network.webSocketFrameReceived → source=browser_ws phase=ws_recv

铁律：
  1. Idempotent — 同一 page 重复 attach 直接 return（修复 R7-R1 I4）
  2. patchright stealth 兼容 — new_cdp_session 抛异常 → log warn + 标记 fail，
     绝不 raise 影响业务
  3. 全 callback try/except 自吞 — 一条事件失败不影响其他事件
  4. content-type 黑名单（video/audio/image/octet-stream）跳过 getResponseBody
  5. getResponseBody timeout=2s（避免 streaming 卡死）
  6. ws frame payloadData 双解码：先试 base64，失败回退 text
  7. ExtraInfo 与主事件顺序不保证 — 不做 merge，两条 record 分别落库，靠
     extra.request_id 关联（消费侧 join）。ExtraInfo 可能不来（缓存命中等），
     缺失视为正常。

================================================================================
"""

from __future__ import annotations

import asyncio
import base64
import logging
from typing import Any

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
#  常量
# ─────────────────────────────────────────────────────────────────────────────

_BODY_FETCH_TIMEOUT_SEC = 2.0
_BODY_PREVIEW_MAX = 4096
_WS_FRAME_PREVIEW_MAX = 4096

_BODY_FETCH_DENY_PREFIXES = (
    "video/", "audio/", "image/", "application/octet-stream",
)

_BODY_FETCH_ALLOW_PREFIXES = (
    "application/json",
    "application/x-www-form-urlencoded",
    "application/xml",
    "text/",
)


# ─────────────────────────────────────────────────────────────────────────────
#  Cookie block reason 分类（CDP BlockedCookieWithReason）
# ─────────────────────────────────────────────────────────────────────────────
#
# 关键事实：CDP `associatedCookies[*].blockedReasons` 是个 list[str]，但
# 它的语义混合了两个层级：
#
#   (A) warning-only：Chromium devtools 给前端的提示，cookie 其实仍随请求发送
#       e.g. SameSiteUnspecifiedTreatedAsLax 是 SameSite 缺失被当 Lax 处理，
#       浏览器为了未来收紧策略提前 warn，但当下行为依然把 cookie 发出去
#
#   (B) truly-blocked：cookie 确实没被附到请求上
#       e.g. NotOnPath / DomainMismatch / SecureOnly
#
# 加上 `exemptionReason`（同事件，cookie 级字段）：即便有 truly-blocked reason，
# exemptionReason 非空也会让 cookie 实际发送（用户级例外、企业策略豁免等）。
#
# 历史 bug（2026-06-02）：用 `if c.get("blockedReasons")` 一刀切把 warning
# 也计为 blocked，对成功的请求误报 49% 阻断率，差点引发反爬深水炸弹的误判。
#
# 修正：分三个 count，命名区分语义层级，让读数据的人不需要猜：
#   - associated_cookies_count   : CDP 上报的关联 cookie 总数（信息字段）
#   - cookies_with_warning_count : 有 warning-only reason 的（仅提示，不影响发送）
#   - cookies_truly_blocked_count: 真的没发出去的（扣除 exemption 豁免）
#
# 注：reason 字符串是 Chromium 内部 enum，会随版本演进。这里只列已知 warning，
# 未列入的 reason 默认归为 truly-blocked（保守）。

_WARNING_ONLY_BLOCK_REASONS: frozenset[str] = frozenset({
    # SameSite 政策警告（Chromium 76+ 起逐步收紧，过渡期内 warning-only）
    "SameSiteUnspecifiedTreatedAsLax",
    "WarnSameSiteUnspecifiedCrossSiteContext",
    "WarnSameSiteUnspecifiedLaxAllowUnsafe",
    "WarnSameSiteNoneInsecure",
    "WarnSameSiteStrictLaxDowngradeStrict",
    "WarnSameSiteStrictCrossDowngradeStrict",
    "WarnSameSiteStrictCrossDowngradeLax",
    "WarnSameSiteLaxCrossDowngradeStrict",
    "WarnSameSiteLaxCrossDowngradeLax",
    # 第三方 cookie 阶段性弃用警告（实际是否阻断看 exemptionReason）
    "WarnThirdPartyPhaseout",
})


def _classify_associated_cookies(
    associated: list[dict],
) -> tuple[int, int]:
    """归类 associatedCookies → (warning_count, truly_blocked_count).

    判定：
      - reasons 全为空                                → 正常发送，不计
      - reasons 全在 _WARNING_ONLY_BLOCK_REASONS 内   → warning-only
      - 否则                                          → 检查 exemptionReason:
          * exemptionReason 非空且非 "None"           → 豁免发送，不计
          * 其它                                      → truly_blocked
    """
    warn_n = 0
    block_n = 0
    for c in associated:
        reasons = c.get("blockedReasons") or []
        if not reasons:
            continue
        # 全是 warning-only？
        if all(r in _WARNING_ONLY_BLOCK_REASONS for r in reasons):
            warn_n += 1
            continue
        # 含真阻断 reason；看 exemption 是否豁免
        exemption = c.get("exemptionReason") or ""
        if exemption and exemption != "None":
            # 豁免发送，info 上仍归 warning（提示性信息）
            warn_n += 1
            continue
        block_n += 1
    return warn_n, block_n


# ─────────────────────────────────────────────────────────────────────────────
#  attach 主入口（spec §3.3）
# ─────────────────────────────────────────────────────────────────────────────

async def attach(page, ctx, *, ref_id_header: str = "x-crawlhub-refid") -> None:
    """Attach CDP recorder to a Playwright Page.

    Args:
        page: Playwright Page or CrawlHub PlaywrightPageWrapper（自动解包）
        ctx: TaskContext，闭包到所有 callback（修复 ContextVar 跨 loop 陷阱）
        ref_id_header: 用于 grep 关联 py_http 端的 header 名（小写）

    Idempotent: 重复 attach 直接 return。
    Fail-safe: stealth 模式拒绝 CDP → log warn + 标记 attach_failed。
    """
    # CrawlHub 的 PlaywrightPageWrapper 把原生 page 放在 ._page；解包以拿 .context。
    raw = getattr(page, "_page", None)
    if raw is None:
        raw = page  # 已是原生 page

    if getattr(page, "_crawlhub_cdp_attached", False):
        return
    page._crawlhub_cdp_attached = True

    # ── 1. 创 CDP session ────────────────────────────────────────────────────
    try:
        client = await raw.context.new_cdp_session(raw)
    except Exception as exc:
        logger.warning("[obs.cdp] new_cdp_session failed (stealth?): %s", exc)
        page._crawlhub_cdp_attach_failed = True
        return
    logger.debug("[obs.cdp] attach: cdp session created for page=%s", id(page))

    # ── 2. 启用 Network domain ────────────────────────────────────────────
    try:
        await client.send("Network.enable", {"maxResourceBufferSize": 5 * 1024 * 1024})
    except Exception as exc:
        logger.warning("[obs.cdp] Network.enable failed: %s", exc)
        page._crawlhub_cdp_attach_failed = True
        try:
            await client.detach()
        except Exception:
            pass
        return

    # ── 3. 状态映射 ───────────────────────────────────────────────────────
    #   ws_url_map        : ws requestId → url（ws frame 反查）
    #   request_info_map  : http requestId → {method, url}（response / ExtraInfo 反查）
    # 注意：ExtraInfo 事件本身只带 requestId，不带 url。必须靠主事件先填这个 map，
    # ExtraInfo 才有 url 可写。两类事件顺序不保证 → ExtraInfo 比主事件先到时，
    # url 字段会为空（不影响 requestId 关联）。
    ws_url_map: dict[str, str] = {}
    request_info_map: dict[str, dict[str, str]] = {}

    # ── 4. callback 定义 ─────────────────────────────────────────────────────
    def _on_request(evt: dict) -> None:
        try:
            request_id = evt.get("requestId", "")
            request = evt.get("request") or {}
            method = request.get("method", "GET")
            url = request.get("url", "")
            headers = request.get("headers") or {}
            post_data = request.get("postData")

            if request_id:
                request_info_map[request_id] = {"method": method, "url": url}

            ref_id = _extract_ref_id(headers, ref_id_header)

            _record(ctx, {
                "source": "browser_network",
                "phase": "request",
                "method": method,
                "url": url,
                "request_headers": dict(headers),
                "request_body": post_data,
                "transport_library": "cdp",
                "is_async": True,
                "ref_id": ref_id or "",
                "extra": {
                    "request_id": request_id,
                    "type": evt.get("type"),
                    "frame_id": (evt.get("frameId") or ""),
                },
            })
        except Exception:
            logger.exception("[obs.cdp] _on_request failed")

    def _on_response(evt: dict) -> None:
        try:
            request_id = evt.get("requestId", "")
            response = evt.get("response") or {}
            url = response.get("url", "")
            status = response.get("status")
            resp_headers = dict(response.get("headers") or {})
            method = (request_info_map.get(request_id) or {}).get("method", "?")

            content_type = (
                resp_headers.get("content-type")
                or resp_headers.get("Content-Type")
                or ""
            ).lower()

            timing = response.get("timing") or {}
            rt_total = None
            rt_first_byte = None
            try:
                if timing:
                    rt_first_byte = timing.get("receiveHeadersEnd")  # ms (req start 起算)
            except Exception:
                pass

            _record(ctx, {
                "source": "browser_network",
                "phase": "response",
                "method": method,
                "url": url,
                "response_status": status,
                "response_headers": resp_headers,
                "response_content_type": content_type,
                "rt_first_byte_ms": rt_first_byte,
                "rt_total_ms": rt_total,
                "transport_library": "cdp",
                "is_async": True,
                "extra": {
                    "request_id": request_id,
                    "content_type": content_type,
                    "remote_ip": response.get("remoteIPAddress"),
                    "from_cache": response.get("fromDiskCache"),
                    "protocol": response.get("protocol"),
                },
            })

            # 异步取 body（content-type 白名单）
            if request_id and _should_fetch_body(content_type):
                _schedule_body_fetch(client, ctx, request_id, url, method)
        except Exception:
            logger.exception("[obs.cdp] _on_response failed")

    def _on_request_extra_info(evt: dict) -> None:
        """Network.requestWillBeSentExtraInfo —— 浏览器最终发送视图.

        与 _on_request 的差异：
          - 此事件包含浏览器在 fetch 之后注入的全部 headers：
              * cookie（来自 cookie jar）
              * sec-fetch-dest / sec-fetch-mode / sec-fetch-site（W3C Fetch metadata）
              * accept-encoding（gzip/br/zstd 协商）
              * 其它 UA-CH 派生头（如 sec-ch-ua-platform-version 等）
          - associatedCookies 字段含每个 cookie 的 blockedReasons（可见反爬卡点）
          - 无 url 字段，靠 request_info_map[requestId] 反查
          - 顺序与 _on_request 不保证，可能先到或后到

        缺这条事件不报错 — 缓存命中、被取消的请求可能不发 ExtraInfo。
        """
        try:
            request_id = evt.get("requestId", "")
            headers = evt.get("headers") or {}
            info = request_info_map.get(request_id) or {}
            url = info.get("url", "")
            method = info.get("method", "?")

            associated = evt.get("associatedCookies") or []
            warn_n, block_n = _classify_associated_cookies(associated)

            _record(ctx, {
                "source": "browser_network",
                "phase": "request_extra",
                "method": method,
                "url": url,
                "request_headers": dict(headers),
                "transport_library": "cdp",
                "is_async": True,
                "extra": {
                    "request_id": request_id,
                    "associated_cookies_count": len(associated),
                    # warning-only：Chromium 给前端的过渡期提示，cookie 仍随请求发送
                    "cookies_with_warning_count": warn_n,
                    # truly-blocked：cookie 真的没附到请求上（扣除 exemption 豁免）
                    "cookies_truly_blocked_count": block_n,
                },
            })
        except Exception:
            logger.exception("[obs.cdp] _on_request_extra_info failed")

    def _on_response_extra_info(evt: dict) -> None:
        """Network.responseReceivedExtraInfo —— 服务器原始响应视图.

        与 _on_response 的差异：
          - 此事件包含服务器返回的原始 headers（含全部 set-cookie 行）
          - blockedCookies 字段含被浏览器拒绝写入 jar 的 cookie 及原因
          - statusCode 在此事件优先（HTTP/2/3 抢跑场景下比主事件早到）
          - 无 url 字段，靠 request_info_map[requestId] 反查
        """
        try:
            request_id = evt.get("requestId", "")
            headers = evt.get("headers") or {}
            status = evt.get("statusCode")
            info = request_info_map.get(request_id) or {}
            url = info.get("url", "")
            method = info.get("method", "?")

            blocked = evt.get("blockedCookies") or []

            _record(ctx, {
                "source": "browser_network",
                "phase": "response_extra",
                "method": method,
                "url": url,
                "response_status": status,
                "response_headers": dict(headers),
                "transport_library": "cdp",
                "is_async": True,
                "extra": {
                    "request_id": request_id,
                    # 服务器返回的 set-cookie 被浏览器拒绝写入 jar 的数量。
                    # 注意语义：与 request_extra 的 cookies_truly_blocked_count 是两件事——
                    #   request_extra : 已存在 jar 里的 cookie 没被附到出向请求
                    #   response_extra: 入向 set-cookie 没被写入 jar
                    # 两者都可能含 SameSite/Secure 类 warning，是否真阻断需结合 reason
                    # 字段判断（CDP `blockedCookies[*].blockedReasons`）。
                    "set_cookies_blocked_count": len(blocked),
                    "resource_ip_address_space": evt.get("resourceIPAddressSpace"),
                },
            })
        except Exception:
            logger.exception("[obs.cdp] _on_response_extra_info failed")

    def _on_ws_created(evt: dict) -> None:
        try:
            request_id = evt.get("requestId", "")
            url = evt.get("url", "")
            if request_id:
                ws_url_map[request_id] = url
            _record(ctx, {
                "source": "browser_ws",
                "phase": "ws_open",
                "url": url,
                "transport_library": "cdp",
                "is_async": True,
                "extra": {"request_id": request_id, "initiator": evt.get("initiator")},
            })
        except Exception:
            logger.exception("[obs.cdp] _on_ws_created failed")

    def _on_ws_frame_sent(evt: dict) -> None:
        _on_ws_frame(evt, "ws_send", ws_url_map, ctx)

    def _on_ws_frame_received(evt: dict) -> None:
        _on_ws_frame(evt, "ws_recv", ws_url_map, ctx)

    def _on_ws_closed(evt: dict) -> None:
        try:
            request_id = evt.get("requestId", "")
            url = ws_url_map.pop(request_id, "")
            _record(ctx, {
                "source": "browser_ws",
                "phase": "ws_close",
                "url": url,
                "transport_library": "cdp",
                "is_async": True,
                "extra": {"request_id": request_id},
            })
        except Exception:
            logger.exception("[obs.cdp] _on_ws_closed failed")

    # ── 5. 注册 ─────────────────────────────────────────────────────────────
    client.on("Network.requestWillBeSent", _on_request)
    client.on("Network.responseReceived", _on_response)
    # ExtraInfo：浏览器最终发送 / 服务器原始返回视图（cookie / sec-fetch-* / set-cookie）
    client.on("Network.requestWillBeSentExtraInfo", _on_request_extra_info)
    client.on("Network.responseReceivedExtraInfo", _on_response_extra_info)
    client.on("Network.webSocketCreated", _on_ws_created)
    client.on("Network.webSocketFrameSent", _on_ws_frame_sent)
    client.on("Network.webSocketFrameReceived", _on_ws_frame_received)
    client.on("Network.webSocketClosed", _on_ws_closed)

    # ── 6. page 关闭 → detach（best-effort）────────────────────────────────
    def _on_page_close(_):
        try:
            asyncio.get_event_loop().create_task(_detach(client))
        except Exception:
            pass

    try:
        raw.once("close", _on_page_close)
    except Exception:
        # 极少数情况下 page 已关，忽略
        pass

    page._crawlhub_cdp_client = client  # 调试用，retain ref
    logger.debug("[obs.cdp] attached to page %s", id(page))


# ─────────────────────────────────────────────────────────────────────────────
#  辅助
# ─────────────────────────────────────────────────────────────────────────────

def _on_ws_frame(evt: dict, phase: str, ws_url_map: dict[str, str], ctx) -> None:
    """处理 webSocketFrameSent / webSocketFrameReceived。

    CDP frame 结构：
      {
        "requestId": str,
        "timestamp": float,
        "response": {"opcode": int, "mask": bool, "payloadData": str}
      }

    opcode 1 = text，opcode 2 = binary（base64 编码的 payloadData）。
    """
    try:
        request_id = evt.get("requestId", "")
        url = ws_url_map.get(request_id, "")
        response = evt.get("response") or {}
        opcode = response.get("opcode", 1)
        payload_data = response.get("payloadData", "") or ""

        preview, size = _decode_ws_payload(payload_data, opcode)

        kwargs: dict[str, Any] = {
            "source": "browser_ws",
            "phase": phase,
            "url": url,
            "transport_library": "cdp",
            "is_async": True,
            "extra": {
                "request_id": request_id,
                "opcode": opcode,
                "frame_size": size,
                "mask": response.get("mask"),
            },
        }
        if phase == "ws_send":
            kwargs["request_body"] = preview
        else:
            kwargs["response_body"] = preview

        _record(ctx, kwargs)
    except Exception:
        logger.exception("[obs.cdp] _on_ws_frame failed")


def _decode_ws_payload(payload_data: str, opcode: int) -> tuple[str | None, int]:
    """ws frame payload 解码 → (preview, total_size)。

    opcode=2 binary：尝试 base64 解码 → utf-8 (errors=replace)。
    opcode=1 text：直接截断。
    其他 opcode（ping/pong/close）：原样截断。
    """
    if not payload_data:
        return None, 0

    if opcode == 2:
        try:
            raw = base64.b64decode(payload_data, validate=False)
            preview = raw[:_WS_FRAME_PREVIEW_MAX].decode("utf-8", errors="replace")
            return preview, len(raw)
        except Exception:
            # 解码失败回退 text
            return payload_data[:_WS_FRAME_PREVIEW_MAX], len(payload_data.encode("utf-8"))

    # text / control frame
    return payload_data[:_WS_FRAME_PREVIEW_MAX], len(payload_data.encode("utf-8"))


def _schedule_body_fetch(client, ctx, request_id: str, url: str, method: str) -> None:
    """异步调 Network.getResponseBody，timeout=2s。失败 silent。"""
    async def _fetch():
        try:
            result = await asyncio.wait_for(
                client.send("Network.getResponseBody", {"requestId": request_id}),
                timeout=_BODY_FETCH_TIMEOUT_SEC,
            )
            body = result.get("body", "") or ""
            is_b64 = bool(result.get("base64Encoded", False))

            if is_b64:
                try:
                    body_bytes = base64.b64decode(body, validate=False)
                    preview = body_bytes[:_BODY_PREVIEW_MAX].decode("utf-8", errors="replace")
                    size = len(body_bytes)
                except Exception:
                    preview = None
                    size = len(body)
            else:
                preview = body[:_BODY_PREVIEW_MAX]
                size = len(body.encode("utf-8"))

            _record(ctx, {
                "source": "browser_network",
                "phase": "response_body",
                "method": method,
                "url": url,
                "response_body": preview,
                "transport_library": "cdp",
                "is_async": True,
                "extra": {"request_id": request_id, "body_size": size, "base64": is_b64},
            })
        except asyncio.TimeoutError:
            logger.debug("[obs.cdp] getResponseBody timeout for %s", url)
        except Exception as exc:
            logger.debug("[obs.cdp] getResponseBody failed for %s: %s", url, exc)

    try:
        loop = asyncio.get_event_loop()
        loop.create_task(_fetch())
    except RuntimeError:
        # 没有 running loop（不应该发生 — CDP callback 总在 loop 里）
        logger.debug("[obs.cdp] no running loop, skip body fetch")


def _should_fetch_body(content_type: str) -> bool:
    """content-type 黑白名单。空 content-type 当 unknown 处理（试一下）。"""
    if not content_type:
        return False  # 修复：未知 content-type 不试，避免对 video/audio 流误调
    ct = content_type.lower()
    for deny in _BODY_FETCH_DENY_PREFIXES:
        if ct.startswith(deny):
            return False
    for allow in _BODY_FETCH_ALLOW_PREFIXES:
        if ct.startswith(allow):
            return True
    return False


def _extract_ref_id(headers: dict[str, str] | None, header_name: str) -> str | None:
    """从 headers 里 grep ref_id（大小写不敏感）."""
    if not headers:
        return None
    target = header_name.lower()
    for k, v in headers.items():
        if k.lower() == target:
            return v
    return None


def _record(ctx, kwargs: dict[str, Any]) -> None:
    """构造 record + 落 jsonl，闭包 ctx 防跨线程丢失."""
    try:
        from crawlhub.core.observability.records import make_record
        platform = getattr(ctx, "_platform", None)
        action = getattr(ctx, "_action", None)
        record = make_record(
            task_id=getattr(ctx, "task_id", None),
            platform=platform,
            action=action,
            **kwargs,
        )
        if hasattr(ctx, "record_request"):
            ctx.record_request(record)
    except Exception:
        logger.exception("[obs.cdp] _record failed")


async def _detach(client) -> None:
    """page 关闭时 best-effort detach。CDP session 通常随 context 自动 cleanup。"""
    try:
        await client.detach()
    except Exception:
        pass
