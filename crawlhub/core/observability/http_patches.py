"""
================================================================================
 R7 Observability — HTTP / WS Transport Patches
================================================================================

每个 patch 函数：
  - 接受 _wrap_method（避免循环 import）
  - 失败 silent + log（不阻塞 daemon）
  - 库未安装 → ImportError 自然抛 → 由 install.py 的 _safe_call 兜底

每个 recorder：
  - signature: (self, args, kwargs, result_or_exc, ref_id) -> None
  - 从 args/kwargs 提 url/method/headers/body
  - 从 result 提 status/headers
  - 调 _try_record(ctx, ...) 落 jsonl

================================================================================
"""

from __future__ import annotations

import contextvars
import logging
import os
import time
from typing import Optional

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
#  Response body capture config
# ─────────────────────────────────────────────────────────────────────────────

_BODY_CAPTURE_DENY_PREFIXES = (
    "video/", "audio/", "image/", "application/octet-stream",
)

_BODY_PREVIEW_LIMIT = 4096  # Max bytes to capture from response body


def _should_capture_body(content_type: str | None) -> bool:
    """Check if response body should be captured based on content-type.

    Returns False for binary content types (video, audio, image, octet-stream).
    Returns True for everything else (including unknown content-type).
    """
    if not content_type:
        return True
    ct = content_type.lower()
    return not any(ct.startswith(p) for p in _BODY_CAPTURE_DENY_PREFIXES)


# ─────────────────────────────────────────────────────────────────────────────
#  Capturing stream wrappers (for httpx where body is streamed)
# ─────────────────────────────────────────────────────────────────────────────

def _make_capturing_iter(original_iter, on_body_captured, preview_limit=_BODY_PREVIEW_LIMIT):
    """Wrap a sync stream iterable to capture body preview as it flows through.

    When the stream is fully consumed (or closed), ``on_body_captured`` is
    called with ``(captured_bytes, total_size)``.
    """
    buf = bytearray()
    total = [0]

    def _gen():
        try:
            for chunk in original_iter:
                total[0] += len(chunk)
                if len(buf) < preview_limit:
                    remaining = preview_limit - len(buf)
                    buf.extend(chunk[:remaining])
                yield chunk
        finally:
            try:
                on_body_captured(bytes(buf), total[0])
            except Exception:
                logger.exception("[obs] capturing iter callback failed")

    return _gen()


def _make_capturing_aiter(original_aiter, on_body_captured, preview_limit=_BODY_PREVIEW_LIMIT):
    """Wrap an async stream iterable to capture body preview as it flows through."""
    buf = bytearray()
    total = [0]

    async def _agen():
        try:
            async for chunk in original_aiter:
                total[0] += len(chunk)
                if len(buf) < preview_limit:
                    remaining = preview_limit - len(buf)
                    buf.extend(chunk[:remaining])
                yield chunk
        finally:
            try:
                on_body_captured(bytes(buf), total[0])
            except Exception:
                logger.exception("[obs] capturing aiter callback failed")

    return _agen()


def _install_urllib3_response_capture(response, on_body_captured, preview_limit=_BODY_PREVIEW_LIMIT):
    """Install per-instance hooks on a urllib3 HTTPResponse to capture body
    preview as it is consumed (read/stream).

    设计原则（2026-06-05 chunked 血泪二修）：
      * 只 patch 这一个实例（不动 class 不动 requests）
      * 透传所有返回值，不破坏下游 requests / raw 调用
      * **stream() 是统一上层入口**：
        - chunked 响应 → urllib3 stream() 内部走 read_chunked()，**不调 self.read()**
        - 普通响应   → urllib3 stream() 内部走 self.read()
        => 必须在 _patched_stream 里 capture 每个 chunk，否则 chunked 完全旁路。
      * 用 `in_stream` flag 避免 stream 内部嵌套 read 时 capture 双倍
      * EOF 触发条件：
        - 直接 read() 路径：read() 返回空 chunk
        - stream() 路径：generator 完整迭代完
      * **不**在 close/release_conn 上挂 finalize ——
        urllib3._raw_read 内部用 _error_catcher contextmanager，
        每次 read 完都会走 release_conn 路径，会让 finalize 在 caller
        拿到 chunk 之前提前触发（2026-06-05 血泪一）
      * finalize 用 done flag 保证至多触发一次

    requests 调用链证据：
      response.content → iter_content → self.raw.stream(decode_content=True)
      → 因此 100% 走 _patched_stream 路径
    """
    buf = bytearray()
    total = [0]
    done = [False]
    in_stream = [False]  # stream() 进行中，read() 不再单独 capture（去重）

    def _capture(chunk):
        if not chunk:
            return
        try:
            n = len(chunk)
        except Exception:
            return
        total[0] += n
        if len(buf) < preview_limit:
            remaining = preview_limit - len(buf)
            buf.extend(chunk[:remaining])

    def _finalize():
        if done[0]:
            return
        done[0] = True
        try:
            on_body_captured(bytes(buf), total[0])
        except Exception:
            logger.exception("[obs] urllib3 body capture callback failed")

    orig_read = response.read
    orig_stream = response.stream

    def _patched_read(*args, **kwargs):
        chunk = orig_read(*args, **kwargs)
        # stream() 进行中时 read 是 stream 的内部实现细节（非 chunked 分支），
        # 由 _patched_stream 统一 capture，避免双倍计数。
        if not in_stream[0]:
            _capture(chunk)
            if not chunk:
                _finalize()
        return chunk

    def _patched_stream(*args, **kwargs):
        in_stream[0] = True
        try:
            for chunk in orig_stream(*args, **kwargs):
                _capture(chunk)
                yield chunk
        finally:
            in_stream[0] = False
            _finalize()

    try:
        response.read = _patched_read
        response.stream = _patched_stream
    except Exception:
        # 某些 HTTPResponse 子类禁止动态属性（极少见）→ 安静放弃
        logger.debug("[obs] urllib3 response capture install failed; skip")


# ─────────────────────────────────────────────────────────────────────────────
#  Wire-level trace 基础设施（仅 curl_cffi 用；ENV opt-in）
# ─────────────────────────────────────────────────────────────────────────────
#
#  动机（2026-06-02 哥的灵魂拷问）：
#    "缺一些参数，我无法判断 crawlhub 的请求 vs 浏览器真实请求的参数差异
#     到底是来自于监控不完整还是 crawlhub action 实现问题"
#
#    => observability 必须是信任根。Python 层 caller-visible 不够，得拿到
#       libcurl 真发出去的 wire bytes（包括 impersonate 注入的 sec-ch-ua-*
#       /UA / Accept-Encoding 等）。
#
#  机制：libcurl CURLOPT_DEBUGFUNCTION + CURLINFO_HEADER_OUT (=2)
#    每次 request 前 setopt(VERBOSE=1, DEBUGFUNCTION=callback)，
#    callback 把 HEADER_OUT 字节追加到 ContextVar 持有的 buffer。
#
#  为什么用 ContextVar 而非 threading.local：
#    crawlhub 同时跑 sync + asyncio + ThreadPoolExecutor，ContextVar 是
#    唯一兼容三种并发模型的隔离原语；executor.submit 已被装 patch 自动 copy。
#
#  为什么默认关：
#    VERBOSE=1 让 libcurl 把每个 byte 都过一遍 callback，性能开销显著
#    (~10-15% 单请求延迟)，仅排查"参数差异归因"时打开。
#
#  CURLINFO_* 常量（libcurl 标准；curl_cffi 顶层未导出，硬编码安全）：
#    0 TEXT  1 HEADER_IN  2 HEADER_OUT  3 DATA_IN  4 DATA_OUT
#    5 SSL_DATA_IN  6 SSL_DATA_OUT
# ─────────────────────────────────────────────────────────────────────────────

_TRACE_WIRE_ENV = "CRAWLHUB_TRACE_WIRE"

_WIRE_BUF: contextvars.ContextVar[Optional[list]] = contextvars.ContextVar(
    "crawlhub_wire_buf", default=None,
)


def _curl_debug_callback(infotype: int, data: bytes) -> int:
    """libcurl DEBUGFUNCTION callback；只截 HEADER_OUT 字节到 buffer."""
    try:
        if infotype == 2:  # CURLINFO_HEADER_OUT
            buf = _WIRE_BUF.get()
            if buf is not None:
                buf.append(bytes(data))
    except Exception:
        # callback 永不抛 —— 否则 libcurl 会把 request 整个崩掉
        pass
    return 0

# ─────────────────────────────────────────────────────────────────────────────
#  共享：从 ContextVar 拿当前 task_context
# ─────────────────────────────────────────────────────────────────────────────

def _get_ctx():
    """拿当前 TaskContext；拿不到返回 None（不报错）."""
    try:
        from crawlhub.core.platform.base_client import CURRENT_TASK_CONTEXT
        return CURRENT_TASK_CONTEXT.get()
    except Exception:
        return None


def _try_record(record_kwargs: dict, *, _ctx=None) -> None:
    """构造 record + 投递到当前 task 的 writer.

    拿不到 ctx → 落 task_id="<unknown>" 但仍写盘（用于 grep 排查盲区）.
    _ctx: optional pre-captured TaskContext (for callbacks that fire later,
          e.g. httpx stream body capture where the original ctx may have changed).
    """
    try:
        from crawlhub.core.observability.records import make_record
        ctx = _ctx or _get_ctx()
        if ctx is None:
            # 没 ctx 也写，但 task_id="<unknown>" 便于 grep
            record = make_record(task_id=None, platform=None, action=None, **record_kwargs)
            # 没 ctx 时不写盘（无 writer）；丢给 logger
            logger.debug("[obs] record without ctx: %s", record.get("url", "<no url>"))
            return
        platform = getattr(ctx, "_platform", None) or record_kwargs.pop("platform_hint", None)
        action = getattr(ctx, "_action", None) or record_kwargs.pop("action_hint", None)
        record = make_record(
            task_id=ctx.task_id, platform=platform, action=action, **record_kwargs,
        )
        if hasattr(ctx, "record_request"):
            ctx.record_request(record)
    except Exception:
        logger.exception("[obs] _try_record failed")


# ─────────────────────────────────────────────────────────────────────────────
#  urllib3
# ─────────────────────────────────────────────────────────────────────────────

def patch_urllib3(wrap):
    """patch urllib3.connectionpool.HTTPConnectionPool.urlopen (sync).

    signature: urlopen(method, url, body=None, headers=None, ...) -> HTTPResponse
    """
    from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
    import urllib3

    version = urllib3.__version__

    def recorder(self, args, kwargs, result_or_exc, ref_id):
        # 解析 args
        method = args[0] if len(args) > 0 else kwargs.get("method", "GET")
        url_path = args[1] if len(args) > 1 else kwargs.get("url", "")
        body = kwargs.get("body")
        headers = kwargs.get("headers") or {}

        # 完整 url
        scheme = "https" if isinstance(self, HTTPSConnectionPool) else "http"
        port = getattr(self, "port", None)
        host = getattr(self, "host", "")
        if port and ((scheme == "http" and port != 80) or (scheme == "https" and port != 443)):
            full_url = f"{scheme}://{host}:{port}{url_path}"
        else:
            full_url = f"{scheme}://{host}{url_path}"

        # 解析 result
        status = None
        resp_headers = {}
        resp_body = None
        resp_content_type = None
        if not isinstance(result_or_exc, BaseException):
            status = getattr(result_or_exc, "status", None)
            resp_headers = dict(getattr(result_or_exc, "headers", {}) or {})
            resp_content_type = (
                resp_headers.get("content-type")
                or resp_headers.get("Content-Type")
            )
            if _should_capture_body(resp_content_type):
                # ① 优先：preload_content=True 路径，body 已在 ._body
                _cached_body = getattr(result_or_exc, "_body", None)
                if _cached_body is not None:
                    resp_body = _cached_body
                else:
                    # ② streaming 路径（requests 走这条）：装实例钩子，
                    #    consumer 读 body 时透传抓 preview，EOF 补发 response_body 记录
                    _ctx_captured = _get_ctx()
                    if _ctx_captured is not None and hasattr(result_or_exc, "read"):
                        _ct = resp_content_type
                        _method = method
                        _url = full_url
                        _ref_id = ref_id
                        _http_version_label = None  # urllib3 不直接暴露

                        def _on_body_done(body_bytes, total_size):
                            _try_record({
                                "source": "py_http",
                                "phase": "response_body",
                                "method": _method,
                                "url": _url,
                                "response_body": body_bytes,
                                "response_content_type": _ct,
                                "transport_library": "urllib3",
                                "transport_version": version,
                                "is_async": False,
                                "http_version": _http_version_label,
                                "ref_id": _ref_id,
                                "extra": {"body_size": total_size},
                            }, _ctx=_ctx_captured)

                        _install_urllib3_response_capture(result_or_exc, _on_body_done)

        _try_record({
            "source": "py_http",
            "phase": "response" if status is not None else "request",
            "method": method,
            "url": full_url,
            "request_headers": dict(headers) if headers else {},
            "request_body": body,
            "response_status": status,
            "response_headers": resp_headers,
            "response_body": resp_body,
            "response_content_type": resp_content_type,
            "transport_library": "urllib3",
            "transport_version": version,
            "is_async": False,
            "ref_id": ref_id,
        })

    wrap(HTTPConnectionPool, "urlopen", recorder, is_coro=False)
    # HTTPSConnectionPool 继承 HTTPConnectionPool.urlopen，已被覆盖


# ─────────────────────────────────────────────────────────────────────────────
#  httpx (sync)
# ─────────────────────────────────────────────────────────────────────────────

def patch_httpx_sync(wrap):
    """patch httpcore._sync.connection_pool.ConnectionPool.handle_request.

    httpx.Client 内部最终会调 httpcore，我们 patch 在 httpcore 层覆盖
    所有 httpx.Client 实例（不论是 BaseHttpClient 还是业务 A4 直连）.
    """
    try:
        from httpcore._sync.connection_pool import ConnectionPool
    except ImportError:
        # httpcore 0.x / 老版本路径不同，跳过（spec §8 fallback safe）
        logger.debug("[obs] httpcore.sync.ConnectionPool not found, skipping")
        return

    import httpcore
    version = getattr(httpcore, "__version__", "?")

    def recorder(self, args, kwargs, result_or_exc, ref_id):
        # request 是 httpcore.Request 对象
        request = args[0] if args else kwargs.get("request")
        if request is None:
            return

        try:
            method = request.method.decode() if isinstance(request.method, bytes) else str(request.method)
        except Exception:
            method = "?"
        try:
            url = str(request.url)
        except Exception:
            url = ""

        try:
            req_headers = {
                (k.decode() if isinstance(k, bytes) else k): (v.decode() if isinstance(v, bytes) else v)
                for k, v in request.headers
            }
        except Exception:
            req_headers = {}

        status = None
        resp_headers = {}
        http_version = None
        resp_content_type = None
        if not isinstance(result_or_exc, BaseException):
            status = getattr(result_or_exc, "status", None)
            try:
                resp_headers = {
                    (k.decode() if isinstance(k, bytes) else k): (v.decode() if isinstance(v, bytes) else v)
                    for k, v in (result_or_exc.headers or [])
                }
            except Exception:
                pass
            ext = getattr(result_or_exc, "extensions", {}) or {}
            http_version = ext.get("http_version")
            if isinstance(http_version, bytes):
                http_version = http_version.decode()
            resp_content_type = (
                resp_headers.get("content-type")
                or resp_headers.get("Content-Type")
            )

        # Capture response body via stream wrapper.
        # At httpcore level the body stream hasn't been consumed yet, so we
        # wrap it to capture preview bytes as they flow through the caller.
        # A separate ``phase=response_body`` record is emitted when the stream
        # is fully consumed (matching the CDP pattern).
        if (not isinstance(result_or_exc, BaseException)
                and _should_capture_body(resp_content_type)
                and hasattr(result_or_exc, "stream")):
            ctx = _get_ctx()
            if ctx is not None:
                _ct = resp_content_type
                _method = method
                _url = url
                _ref_id = ref_id

                def _on_body_done(body_bytes, total_size):
                    _try_record({
                        "source": "py_http",
                        "phase": "response_body",
                        "method": _method,
                        "url": _url,
                        "response_body": body_bytes,
                        "response_content_type": _ct,
                        "transport_library": "httpx",
                        "transport_version": version,
                        "is_async": False,
                        "http_version": http_version,
                        "ref_id": _ref_id,
                        "extra": {"body_size": total_size},
                    }, _ctx=ctx)

                result_or_exc.stream = _make_capturing_iter(
                    result_or_exc.stream, _on_body_done,
                )

        _try_record({
            "source": "py_http",
            "phase": "response" if status is not None else "request",
            "method": method,
            "url": url,
            "request_headers": req_headers,
            "response_status": status,
            "response_headers": resp_headers,
            "response_content_type": resp_content_type,
            "transport_library": "httpx",
            "transport_version": version,
            "is_async": False,
            "http_version": http_version,
            "ref_id": ref_id,
        })

    wrap(ConnectionPool, "handle_request", recorder, is_coro=False)


# ─────────────────────────────────────────────────────────────────────────────
#  httpx (async)
# ─────────────────────────────────────────────────────────────────────────────

def patch_httpx_async(wrap):
    try:
        from httpcore._async.connection_pool import AsyncConnectionPool
    except ImportError:
        logger.debug("[obs] httpcore.async.AsyncConnectionPool not found, skipping")
        return

    import httpcore
    version = getattr(httpcore, "__version__", "?")

    def recorder(self, args, kwargs, result_or_exc, ref_id):
        request = args[0] if args else kwargs.get("request")
        if request is None:
            return

        try:
            method = request.method.decode() if isinstance(request.method, bytes) else str(request.method)
        except Exception:
            method = "?"
        try:
            url = str(request.url)
        except Exception:
            url = ""

        try:
            req_headers = {
                (k.decode() if isinstance(k, bytes) else k): (v.decode() if isinstance(v, bytes) else v)
                for k, v in request.headers
            }
        except Exception:
            req_headers = {}

        status = None
        resp_headers = {}
        http_version = None
        resp_content_type = None
        if not isinstance(result_or_exc, BaseException):
            status = getattr(result_or_exc, "status", None)
            try:
                resp_headers = {
                    (k.decode() if isinstance(k, bytes) else k): (v.decode() if isinstance(v, bytes) else v)
                    for k, v in (result_or_exc.headers or [])
                }
            except Exception:
                pass
            ext = getattr(result_or_exc, "extensions", {}) or {}
            http_version = ext.get("http_version")
            if isinstance(http_version, bytes):
                http_version = http_version.decode()
            resp_content_type = (
                resp_headers.get("content-type")
                or resp_headers.get("Content-Type")
            )

        # Capture response body via async stream wrapper (same pattern as sync).
        if (not isinstance(result_or_exc, BaseException)
                and _should_capture_body(resp_content_type)
                and hasattr(result_or_exc, "stream")):
            ctx = _get_ctx()
            if ctx is not None:
                _ct = resp_content_type
                _method = method
                _url = url
                _ref_id = ref_id

                def _on_body_done(body_bytes, total_size):
                    _try_record({
                        "source": "py_http",
                        "phase": "response_body",
                        "method": _method,
                        "url": _url,
                        "response_body": body_bytes,
                        "response_content_type": _ct,
                        "transport_library": "httpx",
                        "transport_version": version,
                        "is_async": True,
                        "http_version": http_version,
                        "ref_id": _ref_id,
                        "extra": {"body_size": total_size},
                    }, _ctx=ctx)

                result_or_exc.stream = _make_capturing_aiter(
                    result_or_exc.stream, _on_body_done,
                )

        _try_record({
            "source": "py_http",
            "phase": "response" if status is not None else "request",
            "method": method,
            "url": url,
            "request_headers": req_headers,
            "response_status": status,
            "response_headers": resp_headers,
            "response_content_type": resp_content_type,
            "transport_library": "httpx",
            "transport_version": version,
            "is_async": True,
            "http_version": http_version,
            "ref_id": ref_id,
        })

    wrap(AsyncConnectionPool, "handle_async_request", recorder, is_coro=True)


# ─────────────────────────────────────────────────────────────────────────────
#  websockets dual patch（asyncio + legacy 全装，try/except 决定）
# ─────────────────────────────────────────────────────────────────────────────

def patch_websockets_dual(wrap):
    import websockets
    version = getattr(websockets, "__version__", "?")

    def make_send_recorder(path_label):
        def recorder(self, args, kwargs, result_or_exc, ref_id):
            payload = args[0] if args else None
            preview, size = _ws_preview(payload)
            _try_record({
                "source": "py_ws",
                "phase": "ws_send",
                "url": getattr(self, "uri", "") or "",
                "request_body": preview,
                "transport_library": f"websockets.{path_label}",
                "transport_version": version,
                "is_async": True,
                "ref_id": ref_id,
                "extra": {"frame_size": size},
            })
        return recorder

    def make_recv_recorder(path_label):
        def recorder(self, args, kwargs, result_or_exc, ref_id):
            if isinstance(result_or_exc, BaseException):
                return
            preview, size = _ws_preview(result_or_exc)
            _try_record({
                "source": "py_ws",
                "phase": "ws_recv",
                "url": getattr(self, "uri", "") or "",
                "response_body": preview,
                "transport_library": f"websockets.{path_label}",
                "transport_version": version,
                "is_async": True,
                "ref_id": ref_id,
                "extra": {"frame_size": size},
            })
        return recorder

    asyncio_ok = False
    try:
        from websockets.asyncio.client import ClientConnection
        wrap(ClientConnection, "send", make_send_recorder("asyncio"), is_coro=True)
        wrap(ClientConnection, "recv", make_recv_recorder("asyncio"), is_coro=True)
        asyncio_ok = True
    except ImportError:
        pass

    legacy_ok = False
    try:
        from websockets.legacy.client import WebSocketClientProtocol
        wrap(WebSocketClientProtocol, "send", make_send_recorder("legacy"), is_coro=True)
        wrap(WebSocketClientProtocol, "recv", make_recv_recorder("legacy"), is_coro=True)
        legacy_ok = True
    except ImportError:
        pass

    if not asyncio_ok and not legacy_ok:
        logger.warning("[obs] websockets %s patchable neither asyncio nor legacy", version)


# ─────────────────────────────────────────────────────────────────────────────
#  curl_cffi (sync + async) — libcurl C 层旁路腿
# ─────────────────────────────────────────────────────────────────────────────
#
#  为什么必须有这条 patch：
#  curl_cffi 通过 CFFI 直调 libcurl，**完全绕过** urllib3 / httpcore 这套
#  Python transport 栈。urllib3 patch 看不见它，httpx patch 也看不见它。
#  没有这条 patch，所有走 curl_cffi 的请求 = 黑盒。
#
#  历史血泪（2026-06-02 cfa680006e4f）：
#  kuaishou 包把 _http_get_json 从 httpx 切到 curl_cffi 修风控之后，
#  R7 jsonl 里这 5 条带 SSO Cookie 的请求直接消失。"失明"比"风控"更糟 ——
#  它让我们以为问题没了，其实问题只是看不见。
#
#  切点选择：``Session.request`` 高层 API
#    * 优点：所有 get/post/put 等便捷方法都委托到 request，一处覆盖全部
#    * 优点：参数已结构化（method/url/headers/cookies/data/json/params）
#    * 局限：拿不到 libcurl 真实发出去的 final headers（CFFI 黑盒）
#    * 解药：CRAWLHUB_TRACE_WIRE=1 → 装 CURLOPT_DEBUGFUNCTION，每条
#            record 的 extra.wire_headers 携带 libcurl 真发的 wire bytes
#  当前保真度：
#    * 默认模式（trace 关）：caller-visible 信息全留，对"我发了什么"够用
#    * trace 模式（trace 开）：100% 真 wire headers，含 impersonate 注入
# ─────────────────────────────────────────────────────────────────────────────

def patch_curl_cffi(wrap):
    """patch curl_cffi.requests.session.{Session,AsyncSession}.request."""
    try:
        from curl_cffi.requests.session import Session as _CurlSession
        from curl_cffi.requests.session import AsyncSession as _CurlAsyncSession
    except ImportError:
        logger.debug("[obs] curl_cffi not installed, skipping")
        return

    import curl_cffi
    version = getattr(curl_cffi, "__version__", "?")

    # ── trace mode 检测（ENV opt-in）──
    trace_wire = os.environ.get(_TRACE_WIRE_ENV, "0") == "1"
    _CurlOpt = None
    if trace_wire:
        try:
            from curl_cffi.const import CurlOpt as _CurlOpt  # noqa: F811
        except ImportError:
            logger.warning(
                "[obs] curl_cffi.const.CurlOpt unavailable; CRAWLHUB_TRACE_WIRE disabled"
            )
            trace_wire = False

    def _normalise_cookie_kwarg(cookies) -> str:
        """把 cookies kwarg（dict / CookieJar / iterable）转成 'k=v; k=v' 字符串。

        失败返回空串（patch 永不抛）。
        """
        if not cookies:
            return ""
        try:
            if isinstance(cookies, dict):
                items = cookies.items()
            elif hasattr(cookies, "items"):
                items = cookies.items()
            else:
                items = []
                for c in cookies:
                    name = getattr(c, "name", None)
                    val = getattr(c, "value", None)
                    if name:
                        items.append((name, val or ""))
            return "; ".join(f"{k}={v}" for k, v in items if v is not None)
        except Exception:
            return ""

    def _make_recorder(*, is_async: bool):
        def recorder(self, args, kwargs, result_or_exc, ref_id):
            try:
                method = args[0] if len(args) > 0 else kwargs.get("method", "GET")
                url = args[1] if len(args) > 1 else kwargs.get("url", "")
                headers = kwargs.get("headers") or {}
                cookies_kw = kwargs.get("cookies")
                data = kwargs.get("data")
                json_body = kwargs.get("json")
                params = kwargs.get("params")
                impersonate = kwargs.get("impersonate") or getattr(self, "impersonate", None)

                # ── 还原带 params 的完整 URL（方便 grep msToken / a_bogus / __NS_hxfalcon）──
                url_str = str(url) if url else ""
                if params and url_str:
                    try:
                        from urllib.parse import urlencode
                        sep = "&" if "?" in url_str else "?"
                        url_str = f"{url_str}{sep}{urlencode(params, doseq=True)}"
                    except Exception:
                        pass

                # ── 合并 headers + cookies kwarg → 与"实际发出去的 cookie"对齐 ──
                req_headers = dict(headers) if headers else {}
                cookie_str = _normalise_cookie_kwarg(cookies_kw)
                if cookie_str:
                    existing = req_headers.get("cookie") or req_headers.get("Cookie") or ""
                    req_headers["cookie"] = (
                        f"{existing}; {cookie_str}" if existing else cookie_str
                    )

                # body preview：json 优先（结构化），否则 raw data
                body = json_body if json_body is not None else data

                # ── 解析 response ──
                status = None
                resp_headers: dict = {}
                http_version = None
                resp_body = None
                resp_content_type = None
                if not isinstance(result_or_exc, BaseException):
                    status = getattr(result_or_exc, "status_code", None)
                    try:
                        resp_headers = dict(getattr(result_or_exc, "headers", {}) or {})
                    except Exception:
                        resp_headers = {}
                    hv = getattr(result_or_exc, "http_version", None)
                    if hv is not None:
                        http_version = str(hv)
                    resp_content_type = (
                        resp_headers.get("content-type")
                        or resp_headers.get("Content-Type")
                    )
                    # Response body is already materialized in curl_cffi Response.content
                    if _should_capture_body(resp_content_type):
                        resp_body = getattr(result_or_exc, "content", None)

                method_str = str(method).upper() if method else "?"

                # ── extra: impersonate（ja3 配方）+ wire_headers（trace mode）──
                extra: dict = {
                    "impersonate": str(impersonate) if impersonate else None,
                }
                # trace mode 下读 ContextVar buffer；is_async=True 时 buf=None（async 暂不支持 trace）
                if trace_wire:
                    buf = _WIRE_BUF.get()
                    if buf:
                        try:
                            extra["wire_headers"] = b"".join(buf).decode(
                                "utf-8", errors="replace"
                            )
                        except Exception:
                            pass

                _try_record({
                    "source": "py_http",
                    "phase": "response" if status is not None else "request",
                    "method": method_str,
                    "url": url_str,
                    "request_headers": req_headers,
                    "request_body": body,
                    "response_status": status,
                    "response_headers": resp_headers,
                    "response_body": resp_body,
                    "response_content_type": resp_content_type,
                    "transport_library": "curl_cffi",
                    "transport_version": version,
                    "is_async": is_async,
                    "http_version": http_version,
                    "ref_id": ref_id,
                    "extra": extra,
                })
            except Exception:
                # 任何解析失败都不能让业务请求被影响
                logger.exception("[obs] curl_cffi recorder failed silently")

        return recorder

    wrap(_CurlSession, "request", _make_recorder(is_async=False), is_coro=False)
    wrap(_CurlAsyncSession, "request", _make_recorder(is_async=True), is_coro=True)

    # ─────────────────────────────────────────────────────────────────────────
    #  trace_wrapper —— 仅当 CRAWLHUB_TRACE_WIRE=1 时生效
    # ─────────────────────────────────────────────────────────────────────────
    #  时序拓扑：
    #    trace_wrapper(set buf + setopt DEBUGFUNCTION)
    #      └─> sync_wrapper(wrap 包出来的)
    #            └─> orig curl_cffi.request   ← libcurl 在这里写 buf
    #            └─> recorder(读 buf 写 extra.wire_headers)
    #
    #  为什么 sync only：
    #    AsyncSession 的实现细节里每次 request 用一个独立的 curl handle，
    #    self.curl 不稳定；要装 trace 需在 transport.acurl_cls 层 hook，
    #    复杂度大幅上升。当前 P0 是 sync 路径（kuaishou _http_get_json /
    #    SDK.probe 都是 sync），先解决 80% 价值的部分，async 留 P2。
    #
    #  为什么每次 request 都 setopt：
    #    curl_easy_reset() 在 Session.request 内被调用（这是 IPv4 强制方案
    #    踩过的同一个坑）—— 它会擦掉所有 setopt。所以必须前置钩子。
    # ─────────────────────────────────────────────────────────────────────────
    if not trace_wire:
        return

    sentinel = "_crawlhub_wire_traced"
    if getattr(_CurlSession, sentinel, False):
        return

    _wrapped_sync_request = _CurlSession.request  # 此时已是 wrap 后的版本

    def _traced_sync_request(self, *args, **kwargs):
        token = _WIRE_BUF.set([])
        try:
            try:
                # 每次都重设：curl_easy_reset 会擦
                self.curl.setopt(_CurlOpt.VERBOSE, 1)
                self.curl.setopt(_CurlOpt.DEBUGFUNCTION, _curl_debug_callback)
            except Exception:
                # setopt 失败不阻塞业务请求 —— trace 退化为"那条 record 没 wire_headers"
                logger.exception("[obs] curl_cffi setopt DEBUGFUNCTION failed")
            return _wrapped_sync_request(self, *args, **kwargs)
        finally:
            _WIRE_BUF.reset(token)

    setattr(_CurlSession, sentinel, True)
    setattr(_CurlSession, "request", _traced_sync_request)
    logger.info(
        "[obs] curl_cffi wire trace ENABLED (sync only; CRAWLHUB_TRACE_WIRE=1)"
    )



def _ws_preview(payload) -> tuple[str | None, int]:
    """ws frame payload preview (≤ 4KB)."""
    if payload is None:
        return None, 0
    if isinstance(payload, bytes):
        return payload[:4096].decode("utf-8", errors="replace"), len(payload)
    if isinstance(payload, str):
        return payload[:4096], len(payload.encode("utf-8"))
    return None, 0
