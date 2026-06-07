"""
================================================================================
 R7 Observability — Install Center
================================================================================

总入口：install_all() 在所有 daemon 入口的第 1 行调用。

铁律：
  1. idempotent：重复 install 安全
  2. silent on missing：库未安装 → skip，不报错
  3. fallback safe：单个 patch 失败不影响其他 patch
  4. 永不基于版本号切片：用 try/except + ImportError 决定是否能装

================================================================================
"""

from __future__ import annotations

import functools
import inspect
import logging
import secrets
import threading

logger = logging.getLogger(__name__)

_INSTALL_LOCK = threading.Lock()
_INSTALLED = False


def is_installed() -> bool:
    return _INSTALLED


def install_all() -> None:
    """装所有 transport patch。Idempotent。

    无 kill switch：所有 daemon 进程一律装 patch，确保 transport 监控全覆盖。
    是否把事件落到 `requests.jsonl` 由 config.observability.record_requests 控制
    （默认 false）。
    """
    global _INSTALLED
    with _INSTALL_LOCK:
        if _INSTALLED:
            return

        # 顺序无关（每个 patch 内部保护自己），但按依赖关系排：
        # executor 先装（让其他 patch 装后立即生效；其实顺序无关，纯偏好）
        _safe_call(_patch_executor_submit, name="executor.submit")
        _safe_call(_patch_urllib3, name="urllib3")
        _safe_call(_patch_httpx_sync, name="httpx.sync")
        _safe_call(_patch_httpx_async, name="httpx.async")
        _safe_call(_patch_curl_cffi, name="curl_cffi")
        _safe_call(_patch_websockets_dual, name="websockets")

        _INSTALLED = True
        logger.info("[obs] install_all() done")


def _safe_call(fn, *, name: str) -> None:
    try:
        fn()
        logger.debug("[obs] patched %s", name)
    except Exception:
        logger.exception("[obs] patching %s failed; obs partial", name)


# ─────────────────────────────────────────────────────────────────────────────
#  通用 wrapper（spec §8.1）
# ─────────────────────────────────────────────────────────────────────────────

def _wrap_method(cls, method_name: str, recorder, *, is_coro: bool):
    """通用 method wrapper（async/sync 自适应）.

    Args:
        cls: 要 patch 的类
        method_name: method 名
        recorder: callable(self, args, kwargs, result_or_exc, ref_id) -> None
                  仅做记录，错误自吞
        is_coro: True 当且仅当原方法是 `async def`
    """
    sentinel = f"_crawlhub_wrapped_{method_name}"
    if getattr(cls, sentinel, False):
        return
    orig = getattr(cls, method_name, None)
    if orig is None:
        return

    if is_coro:
        if not inspect.iscoroutinefunction(orig):
            logger.warning(
                "[obs] %s.%s expected coroutine but got %r; skipping",
                cls.__name__, method_name, orig,
            )
            return

        @functools.wraps(orig)
        async def async_wrapper(self, *args, **kwargs):
            ref_id = f"rq_{secrets.token_hex(4)}"
            try:
                result = await orig(self, *args, **kwargs)
            except BaseException as exc:
                _safe_record(recorder, self, args, kwargs, exc, ref_id)
                raise
            else:
                _safe_record(recorder, self, args, kwargs, result, ref_id)
                return result

        new_method = async_wrapper
    else:
        @functools.wraps(orig)
        def sync_wrapper(self, *args, **kwargs):
            ref_id = f"rq_{secrets.token_hex(4)}"
            try:
                result = orig(self, *args, **kwargs)
            except BaseException as exc:
                _safe_record(recorder, self, args, kwargs, exc, ref_id)
                raise
            else:
                _safe_record(recorder, self, args, kwargs, result, ref_id)
                return result

        new_method = sync_wrapper

    setattr(cls, sentinel, True)
    setattr(cls, method_name, new_method)


def _safe_record(recorder, self_obj, args, kwargs, result, ref_id) -> None:
    try:
        recorder(self_obj, args, kwargs, result, ref_id)
    except BaseException:
        logger.exception("[obs] recorder failed silently for ref_id=%s", ref_id)


# ─────────────────────────────────────────────────────────────────────────────
#  executor.submit ContextVar 兜底（spec §8 _patch_executor_submit）
# ─────────────────────────────────────────────────────────────────────────────

def _patch_executor_submit() -> None:
    """修复 R7-R2 C2：weibo/kuaishou scraper 用 ThreadPoolExecutor.submit
    派生子线程发请求。Python 默认不传播 ContextVar，导致子线程 ctx=None。

    monkey-patch submit，自动 contextvars.copy_context() 包裹 fn。
    """
    import concurrent.futures
    import contextvars

    cls = concurrent.futures.ThreadPoolExecutor
    if getattr(cls.submit, "_crawlhub_ctx_wrapped", False):
        return

    orig_submit = cls.submit

    def patched_submit(self, fn, /, *args, **kwargs):
        ctx = contextvars.copy_context()
        return orig_submit(self, ctx.run, fn, *args, **kwargs)

    patched_submit._crawlhub_ctx_wrapped = True  # type: ignore[attr-defined]
    cls.submit = patched_submit  # type: ignore[assignment]


# ─────────────────────────────────────────────────────────────────────────────
#  HTTP / WebSocket patches —— 实装在 http_patches.py
# ─────────────────────────────────────────────────────────────────────────────

def _patch_urllib3() -> None:
    from crawlhub.core.observability.http_patches import patch_urllib3
    patch_urllib3(_wrap_method)


def _patch_httpx_sync() -> None:
    from crawlhub.core.observability.http_patches import patch_httpx_sync
    patch_httpx_sync(_wrap_method)


def _patch_httpx_async() -> None:
    from crawlhub.core.observability.http_patches import patch_httpx_async
    patch_httpx_async(_wrap_method)


def _patch_curl_cffi() -> None:
    from crawlhub.core.observability.http_patches import patch_curl_cffi
    patch_curl_cffi(_wrap_method)


def _patch_websockets_dual() -> None:
    from crawlhub.core.observability.http_patches import patch_websockets_dual
    patch_websockets_dual(_wrap_method)
