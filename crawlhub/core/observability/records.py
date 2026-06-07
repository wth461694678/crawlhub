"""
================================================================================
 R7 Observability — Record Schema
================================================================================

每条 jsonl 记录的 schema 构造（spec §3.2）。

设计原则（Linus 简洁执念）：
  - 不引入 Pydantic：dict 即 schema，validate 留给 viewer 端
  - **不脱敏**：R7 监控存在的全部目的就是 kv 级细粒度可观测性，
    脱敏违背设计目的（2026-06-02 哥拍板）
  - 单人本地工具场景：jsonl 永不出本地磁盘，没有泄露面

历史教训（2026-06-02）：
  spec §7.5 当初定义了 cookie / msToken / a_bogus 等敏感字段脱敏，
  把整串 cookie hash 成 `[redacted len=7053 hash=153bfd0b]` 单行字符串。
  但 R7 监控的存在意义就是要做 kv 级 vs cra trace 的细粒度对比，
  脱敏后只能看长度量级，监控完全失效。
  → 一刀拆掉脱敏层，原文落 jsonl。

================================================================================
"""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import parse_qsl, urlsplit


# ─────────────────────────────────────────────────────────────────────────────
#  body preview
# ─────────────────────────────────────────────────────────────────────────────

_BODY_PREVIEW_MAX = 4096  # 4KB


def body_preview(body: bytes | str | None, content_type: str | None = None) -> tuple[str | None, int]:
    """返回 (preview, total_size)。非文本/二进制 → preview=None。"""
    if body is None:
        return None, 0
    if isinstance(body, bytes):
        size = len(body)
        # 黑名单 content_type 直接 skip preview
        if content_type:
            ct = content_type.lower()
            if any(ct.startswith(p) for p in ("video/", "audio/", "image/", "application/octet-stream")):
                return None, size
        try:
            text = body[:_BODY_PREVIEW_MAX].decode("utf-8", errors="replace")
            return text, size
        except Exception:
            return None, size
    if isinstance(body, str):
        size = len(body.encode("utf-8"))
        return body[:_BODY_PREVIEW_MAX], size
    return None, 0


# ─────────────────────────────────────────────────────────────────────────────
#  record 构造
# ─────────────────────────────────────────────────────────────────────────────

def make_record(
    *,
    task_id: str | None,
    platform: str | None,
    action: str | None,
    source: str,           # py_http | py_ws | browser_network | browser_ws
    phase: str,            # request | response | ws_send | ws_recv | ws_close
    method: str | None = None,
    url: str = "",
    request_headers: dict[str, str] | None = None,
    request_body: bytes | str | None = None,
    response_status: int | None = None,
    response_headers: dict[str, str] | None = None,
    response_body: bytes | str | None = None,
    response_content_type: str | None = None,
    rt_total_ms: float | None = None,
    rt_first_byte_ms: float | None = None,
    transport_library: str = "",
    transport_version: str = "",
    is_async: bool = False,
    http_version: str | None = None,
    tls_version: str | None = None,
    tls_cipher: str | None = None,
    ref_id: str = "",
    in_flight_count: int = 0,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造一条 jsonl 记录（原文，无脱敏）.

    URL / headers / body 全部按调用方传入的原文落库，让 viewer 端可以做
    kv 级细粒度对比（vs cra trace、vs 浏览器实发请求）。
    """
    record: dict[str, Any] = {
        "ts_ms": int(time.time() * 1000),
        "task_id": task_id or "<unknown>",
        "platform": platform,
        "action": action,
        "source": source,
        "phase": phase,
    }
    if method is not None:
        record["method"] = method
    if url:
        record["url"] = url
        try:
            parts = urlsplit(url)
            record["url_path"] = parts.path
            record["url_query_keys"] = [k for k, _ in parse_qsl(parts.query, keep_blank_values=True)]
        except Exception:
            record["url_path"] = ""
            record["url_query_keys"] = []

    if request_headers is not None or request_body is not None:
        req_preview, req_size = body_preview(request_body)
        record["request"] = {
            "headers": dict(request_headers or {}),
            "body_preview": req_preview,
            "body_size": req_size,
        }

    if response_status is not None or response_headers is not None or response_body is not None:
        resp_preview, resp_size = body_preview(response_body, response_content_type)
        record["response"] = {
            "status": response_status,
            "headers": dict(response_headers or {}),
            "body_preview": resp_preview,
            "body_size": resp_size,
            "rt_total_ms": rt_total_ms,
            "rt_first_byte_ms": rt_first_byte_ms,
        }

    record["transport"] = {
        "library": transport_library,
        "version": transport_version,
        "is_async": is_async,
        "http_version": http_version,
        "tls": {"version": tls_version, "cipher": tls_cipher},
    }

    record["correlation"] = {
        "ref_id": ref_id,
        "in_flight_count": in_flight_count,
    }

    if extra:
        record["extra"] = extra

    return record
