"""
================================================================================
 R7 Observability — Daemon-Level Transport Interceptor
================================================================================

业务零感知地观测 daemon 内所有出向请求/响应/WSS 帧。

铁律（spec §0）：
  1. install_all() 必须在所有 daemon 入口的第 1 行（cli/__init__.py + __main__.py
     + cli/mcp_server.py），早于任何业务模块 import。
  2. 任何 patch/recorder 失败都不向上传播 —— 观测层崩了不能拖垮业务。
  3. 默认敏感数据脱敏（cookie/token/msToken/a_bogus），原文需 ENV opt-in。

接口：
  install_all()              —— 装所有传输层 patch（idempotent）
  is_installed() -> bool     —— 判断是否已装
  attach_cdp(page, ctx)      —— 给一个 PageHandle 挂 CDP recorder（Phase 2）
================================================================================
"""

from __future__ import annotations

from crawlhub.core.observability.install import install_all, is_installed

__all__ = ["install_all", "is_installed"]
