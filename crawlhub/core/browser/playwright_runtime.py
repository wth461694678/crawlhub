"""Real Playwright-backed browser session factory.

══════════════════════════════════════════════════════════════════════
  反爬加固设计（2026-05-28 更新）
══════════════════════════════════════════════════════════════════════

Stealth 分层：

  1. **User-Agent 覆盖**  ── headless=True 时 Chromium 默认 UA 含
                             "HeadlessChrome"，是最大的暴露面。必须显式
                             传 user_agent= 覆盖为真实 Chrome UA。
                             （2026-05-28 修复，根因：抖音 SDK 检测
                             HeadlessChrome 后降级到 frontier 通用 IM）
  2. **navigator.webdriver** ── JS 层 override（_WEBDRIVER_OVERRIDE_JS），
                             无论 skip_stealth 都注入。channel="chrome" 用
                             系统 Chrome，没有 patchright 二进制补丁，
                             不能依赖 --disable-blink-features=AutomationControlled
                             命令行 flag（会触发 infobar → 视口偏移 → 被反爬检测）。
  3. **ignore_default_args** ── 排除 --enable-automation（Chrome 自动化模式）
                             和 --disable-blink-features=AutomationControlled
                             （patchright 默认加，Chrome 对其报 infobar）。
                             navigator.webdriver 由 #2 的 JS override 兜底。
  4. **Persistent context** ── 用 user_data_dir 持久化 IndexedDB /
                             Service Worker / cache / history，让
                             平台前端 SDK 看到的"指纹连续性"接近真实用户
  5. **stealth_override.js** ── 自研精确 patch（languages / platformVersion /
                             screen 等），skip_stealth=True 时不注入（登录模式）
  6. **locale + timezone** ── zh-CN / Asia/Shanghai，国内平台 SDK
                             用 navigator.language + Intl.DateTimeFormat
                             做路由判断
  7. **viewport** ── 宿主真实逻辑分辨率（动态探测），规避 Playwright 默认
                     1280×720 和写死 1920×1080 的偏差
  8. **chromium_sandbox** ── Windows/macOS 上 =True，阻止 patchright 自动
                             注入 --no-sandbox（会触发 infobar）

  已知不覆盖的检测层（当前不需要，备忘）：
  - CDP Runtime.Enable 泄露（需 rebrowser-patches，目前停更于 PW 1.52）
  - chrome-headless-shell vs chrome 二进制差异（PW 1.53+ 默认新 headless）
  - WebGL/Canvas 指纹一致性（需更重量级的 fingerprint randomization）

══════════════════════════════════════════════════════════════════════
  R4-P14 Phase 2 资源模型重构（spec §5.5）
══════════════════════════════════════════════════════════════════════

Phase 2 把"playwright / browser / context / page"四件套拆为两个独立
职责单元：

  - **PlaywrightContextHandle**：持有 playwright 和 context；负责
    stealth.js 注入、cookie 灌种、context.close + playwright.stop 的固定
    回收顺序。Context 级别**全局唯一**，不参与 page 池。BrowserSession
    通过 `_context_handle` 字段持有它，`BrowserSession.close()` 末尾
    统一调用 `self._context_handle.close()` 把这两层一起关。
  - **PlaywrightPageWrapper**：持有单个 page；提供 evaluate / goto /
    fetch_json / local_storage / is_closed 接口；page 池里借/还的就是
    它（包装在 PageHandle 内）。

容器化的好处：N=5 时启动 1 个 context + 5 个 page；close 时按
"先关 N 个 page 再关 context 再 stop playwright" 的固定顺序回收，
不会再有"page 漏关 / playwright 没 stop"的边界 bug。
══════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlencode, urlparse


from crawlhub.core.browser.cookie_injection import load_storage_state
from crawlhub.core.browser.session import BrowserSession
from crawlhub.core.browser.session_key import SessionKey
from crawlhub.core.config import get_data_root
from crawlhub.core.plugin_manifest import BrowserConfig

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────
#  反爬资源 & 启动参数
# ────────────────────────────────────────────────────────────────────

_STEALTH_JS_PATH = Path(__file__).parent / "_stealth" / "stealth.min.js"
# 2026-05-29 起停用 stealth.min.js（puppeteer-extra-stealth 黑盒），
# 改用我们自己写的精确 patch。详见 stealth_override.js 文件头注释。
_STEALTH_OVERRIDE_PATH = Path(__file__).parent / "_stealth" / "stealth_override.js"

# ── navigator.webdriver JS override ──────────────────────────────────
# channel="chrome" 使用系统 Chrome（非 patchright 的打补丁 Chromium），
# 所以 navigator.webdriver 防护不能依赖 patchright 二进制补丁。
# 以前依赖 --disable-blink-features=AutomationControlled 命令行 flag，
# 但 Chrome 对其报"不受支持的命令行标记" infobar → 视口偏移 → 被反爬检测。
# 现改为 JS 层 override：无 infobar、无命令行标记暴露。
#
# 反检测策略：
#   1. Object.defineProperty 在原型链上覆盖，page JS 读到的是 undefined
#   2. getter 的 toString 伪装成 native code（反 Object.getOwnPropertyDescriptor
#      检查 .toString() 的探测）
#   3. 整段代码在 add_init_script 阶段注入，早于任何 page script
#
# 与 stealth_override.js 的关系：
#   stealth_override.js 注释说"不 patch navigator.webdriver（launch args 已 disable）"，
#   那是因为以前 --disable-blink-features=AutomationControlled 够用。
#   现在不再传该 flag，改由本 JS override 兜底。两段代码不冲突：
#   本 JS 只做 webdriver 这一项，stealth_override.js 做其他 leak。
_WEBDRIVER_OVERRIDE_JS = """\
// ── crawlhub: navigator.webdriver override ──
// 必须在任何 page script 之前运行（add_init_script 保证）。
// 幂等：重复 defineProperty 只覆盖 getter，不产生副作用。
//
// 2026-06-03 修复（BBA refresh cookie 实测 webdriver=true 暴露的 bug）：
//   旧实现 if (!desc) return；early return 假设 webdriver 一定挂在
//   Navigator.prototype 上，但 channel="chrome" + CDP attach 路径下，
//   Chrome 把 webdriver=true 直接塞在 navigator 实例的 own property
//   上（不走 prototype getter），导致 getOwnPropertyDescriptor(proto)
//   返回 undefined → early return → patch 完全没生效。
//
//   新实现三管齐下：
//     1. 删 navigator 实例上可能存在的 own property
//     2. 在 Navigator.prototype 上强行 defineProperty（不依赖原 desc）
//     3. 同时在 navigator 实例上 defineProperty 兜底
//
//   反检测：getter.toString() 伪装 native code，通过 descriptor 探测。
(() => {
  const nav = navigator;
  const proto = Navigator.prototype;

  // 构造 native-like getter
  const getter = function() { return undefined; };
  getter.toString = function() {
    return 'function get webdriver() { [native code] }';
  };
  getter.toString.toString = function() {
    return 'function toString() { [native code] }';
  };

  // (1) 删 navigator 实例 own property（CDP attach 直接塞的 true）
  try { delete nav.webdriver; } catch (e) {}

  // (2) Navigator.prototype 上强行 defineProperty
  try {
    Object.defineProperty(proto, 'webdriver', {
      get: getter,
      set: undefined,
      enumerable: true,
      configurable: true,
    });
  } catch (e) {}

  // (3) navigator 实例上再覆盖一次（兜底，防 Chrome 内部 slot 直返）
  try {
    Object.defineProperty(nav, 'webdriver', {
      get: getter,
      set: undefined,
      enumerable: true,
      configurable: true,
    });
  } catch (e) {}
})();
"""

# Launch args — 2026 最优反检测参数集
# 来源：puppeteer-extra-stealth + MediaCrawler + nodriver + 实测验证
#
# 分层说明：
#   [核心] 必须有，去掉会被一线反爬立刻检测到
#   [加固] 降低指纹"机器味"，减少启发式检测风险
#   [性能] headless 场景不需要的 UI 渲染/后台节流
# ───────────────────────────────────────────────────────────────────
#  跨平台铁律（2026-06-01 修复，由 BBA headful 任务自报警告暴露）：
#
#  Chrome 命令行参数有平台属性 + "unsafe flag 黑名单"两个维度：
#
#    1) 平台不识别（如 Linux 专属）→ unsupported-flag infobar
#    2) Chrome 内部 bad_flags_prompt 黑名单 → "您使用的是不受支持的命令行
#       标记" infobar（即使平台识别也会报，如 --no-sandbox）
#
#  任一 infobar 都会占 ~40px 视口 → window.outerHeight - innerHeight
#  异常 → 反爬 SDK 可探测。
#
#  所以"sandbox 关停"这件事是 **Linux 容器/CI 专属需求**：
#    - Linux 容器：非 root 用户、缺 user namespace → 必须 --no-sandbox
#      + --disable-setuid-sandbox 才能启动 Chrome
#    - Linux 桌面：sandbox 工作正常，不需要关，但也容忍 --no-sandbox
#    - Windows / macOS：Chrome 用 Job Objects / seatbelt sandbox，
#      原生工作良好，**完全不需要任何 sandbox 相关 flag**
#
#  历史 case：
#    2026-06-01 抖音搜索任务在 Windows 自报先 setuid 后 --no-sandbox 两条
#    警告，根因是把 sandbox 相关 flag 当作"全平台 stealth 标配"加进来。
#
#  解决：args 按平台分发 + 用 env var 给极少数 Win Docker 等异常环境留
#  override 钩子。
# ───────────────────────────────────────────────────────────────────
_STEALTH_LAUNCH_ARGS_COMMON = [
    # ── [核心] 反自动化检测 ──
    # ⚠️ --disable-blink-features=AutomationControlled 已从命令行参数移除！
    # 原因：Chrome 会对其报"不受支持的命令行标记" infobar（占 40px 视口），
    # 反爬 SDK 可探测 outerHeight-innerHeight 异常。
    # navigator.webdriver=false 现由 _WEBDRIVER_OVERRIDE_JS（JS 层 override）
    # 实现，无论 skip_stealth 都注入。ignore_default_args 也排除该 flag
    # （patchright 内部 chromiumSwitches.js L87 默认加），避免双重来源。
    "--disable-features=IsolateOrigins,site-per-process,AutomationControlled",

    # ── [加固] 去除 automation 可见 UI 痕迹 ──
    "--disable-infobars",                      # 去掉"Chrome 正在受自动化控制"横幅
    "--disable-dev-shm-usage",                 # 共享内存不足时防崩溃（Docker 必须，Win 安全忽略）
    "--disable-ipc-flooding-protection",       # 去掉 IPC 频率限制（自动化会触发）
    "--disable-default-apps",                  # 不加载默认应用（减少指纹差异）
    "--disable-extensions",                    # 不加载扩展
    "--disable-component-extensions-with-background-pages",
    "--disable-hang-monitor",                  # 去掉"页面未响应"检测
    # ── [加固] 模拟真实浏览器行为 ──
    "--enable-features=NetworkService,NetworkServiceInProcess",
    "--disable-client-side-phishing-detection",
    "--disable-sync",                          # 不走 Google 同步
    "--metrics-recording-only",                # 不上传遥测但保留 metrics API
    "--no-first-run",
    "--no-default-browser-check",
    "--password-store=basic",                  # 避免系统密钥环弹窗
    "--use-mock-keychain",                     # macOS: mock keychain（Win/Linux 安全忽略）
    # ── [性能] headless 不需要的后台调度 ──
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-popup-blocking",
    "--disable-prompt-on-repost",
    "--disable-window-activation",
    "--disable-focus-on-load",
    "--no-startup-window",
    # ── [视窗] ──
    "--window-position=0,0",
    "--window-size=1920,1080",
]

# Linux 专属：sandbox 关停参数。
#   --no-sandbox            禁用整个 Chromium sandbox 层
#   --disable-setuid-sandbox 禁用 Linux 用户命名空间 sandbox
# 桌面 Linux 也能容忍（不会报 infobar），容器/CI 必需。
# Windows / macOS 上加这两个会触发"unsupported flag" / "unsafe flag" infobar。
_LINUX_ONLY_LAUNCH_ARGS = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
]

if sys.platform.startswith("linux"):
    _STEALTH_LAUNCH_ARGS = _STEALTH_LAUNCH_ARGS_COMMON + _LINUX_ONLY_LAUNCH_ARGS
else:
    _STEALTH_LAUNCH_ARGS = list(_STEALTH_LAUNCH_ARGS_COMMON)

# BBA 登录场景专用：最小化启动参数。
# 登录时用户直接操作浏览器，不需要反检测；那些 --disable-extensions /
# --disable-features=IsolateOrigins 等"反检测"参数反而被 websig4 等 SDK
# 识别为自动化指纹，导致 QR 登录失败（channelType='UNKNOWN'）。
_BBA_LOGIN_LAUNCH_ARGS = [
    # ⚠️ 不含 --disable-blink-features=AutomationControlled（同上，会触发 infobar）
    # navigator.webdriver=false 由 _WEBDRIVER_OVERRIDE_JS（JS 层 override）实现
    # 防弹窗/首次运行向导
    "--no-first-run",
    "--no-default-browser-check",
    # 防系统密钥环弹窗
    "--password-store=basic",
    "--use-mock-keychain",
    # Docker / 低内存环境安全兜底
    "--disable-dev-shm-usage",
]

# 极少数异常环境（Win Docker / 受限 macOS Runner 等）启动失败时的逃生通道：
# 设 CRAWLHUB_DISABLE_SANDBOX=1 强制加 --no-sandbox。
# 注意：这会触发 unsafe-flag infobar，仅作为"先能跑起来"的临时方案。
if os.environ.get("CRAWLHUB_DISABLE_SANDBOX", "").strip() in ("1", "true", "yes"):
    if "--no-sandbox" not in _STEALTH_LAUNCH_ARGS:
        _STEALTH_LAUNCH_ARGS.append("--no-sandbox")
        logger.warning(
            "[stealth] CRAWLHUB_DISABLE_SANDBOX=1 -> 加 --no-sandbox，"
            "会触发 Chrome unsafe-flag infobar，反爬 SDK 可能因此识破。"
            "仅在容器启动失败时使用。"
        )

# Viewport - 主流真实分辨率；Playwright 默认 1280x720 是自动化老黑名单尺寸。
# ⚠️ 这是 fallback 值；运行时实际 viewport 由 detect_host_environment() 探测的
# 宿主真实逻辑分辨率覆盖（见 create_playwright_browser_session 内 launch_kwargs
# 的 viewport 字段）。当宿主是 2K/4K + DPI 缩放时，写死 1920x1080 会让
# navigator.screen 跟 cra.json 真机系统性偏差。
_REAL_VIEWPORT_FALLBACK = {"width": 1920, "height": 1080}

# User-Agent / Stealth 配置：跨平台自适应
# ───────────────────────────────────────────────────────────────────
#  反爬铁律（2026-05-29 修复，由 fingerprint_audit/probe.js diff 暴露）：
#    navigator.userAgent       声明的 Chrome 版本
#    navigator.userAgentData   返回的 uaFullVersion
#    Sec-Ch-Ua HTTP 头里的版本
#    TLS 握手时浏览器二进制的真实版本
#  这四者**必须一致**。任何一项漂移 → 抖音 SDK 直接判定伪造。
#
#  历史教训：
#    2026-05-29 之前硬编码 Chrome/147.0.0.0，但本机 Chrome 已自动升级到
#    148.0.7778.181，导致 UA 字符串声明 147、Client Hints 暴露 148，
#    内部矛盾，被 probe diff 抓到。彻底修：每次启动动态读本机版本。
#
#  跨平台：detect_host_environment() 处理 Windows/macOS/Linux 三个 OS，
#  返回 HostInfo 包含 UA、platformVersion 等所有 stealth 需要的字段。
#  用同一份 crawlhub 代码可以发给 Win10/Win11/Mac 同事跑，无需各自调整。
# ───────────────────────────────────────────────────────────────────

from crawlhub.core.browser.host_environment import detect_host_environment

# Locale & Timezone: 国内平台 SDK（抖音/B站）会通过 navigator.language 和
# Intl.DateTimeFormat().resolvedOptions().timeZone 做辅助路由判断。
# 不设会用操作系统默认值，在海外服务器或 CI 上可能是 en-US / Etc/Unknown。
_REAL_LOCALE = "zh-CN"
_REAL_TIMEZONE_ID = "Asia/Shanghai"

# ── User-Agent：动态读本机真实 Chrome 版本 ──
# 反爬铁律：navigator.userAgent 声明的版本必须跟 navigator.userAgentData
# 返回的 uaFullVersion 以及 TLS 握手的真实二进制版本一致，否则被秒杀。
# 不能硬编码——Chrome 会自动更新，硬编码版本号会跟真机矛盾。
# 所以在模块加载时调用 detect_host_environment() 获取真实 UA。
_host_info = detect_host_environment()
_REAL_USER_AGENT = _host_info.ua

# ─────────────────────────────────────────────────────────────────────────────
# Accept-Language 头与 navigator.languages 一致性约束
# ─────────────────────────────────────────────────────────────────────────────
# 全局唯一定义已搬家到 host_environment.REAL_ACCEPT_LANGUAGE（业务代码也复用）。
# 痛点（2026-06-02 R7 ExtraInfo 验证暴露）：
#   单独设 launch(locale="zh-CN") 会让 Chrome 把出向请求的 HTTP
#   `Accept-Language` 头直接退化成裸字符串 "zh-CN"。
#   但 stealth_override.js 把 navigator.languages 注入成
#   ['zh-CN', 'zh', 'en-US', 'en']（4 项）—— 两端不一致就是反爬指纹的天然
#   分类器：HTTP 头说"我只懂 zh-CN"，JS 说"我懂 4 种"，正常人不会这样。
#
# 解法：launch 时显式 extra_http_headers 覆盖，与 stealth JS 的 4 项严格对应，
#   带 q-value 衰减是真人浏览器的标准行为。
from .host_environment import REAL_ACCEPT_LANGUAGE  # noqa: E402  (常量复用)

# SDK 初始化等待：MediaCrawler 实测下限 1.5s。
# N>1 时这是串行 cost 的主因（详见 create_playwright_browser_session）。
_SDK_INIT_WAIT_SECONDS = 1.5


@dataclass
class BrowserFetchResponse:
    """Small response snapshot compatible with TaskContext.set_last_response."""

    status_code: int
    text: str
    url: str = ""


# ════════════════════════════════════════════════════════════════════
#  PlaywrightPageWrapper —— 单个 page 的薄封装
# --------------------------------------------------------------------
#  对外暴露 evaluate / goto / fetch_json / local_storage / is_closed
#  这五个方法。**不负责** playwright/browser/context 生命周期。
#
#  fetch_json Phase 2 元组返回：把 (data, response) 一起返回，让
#  BrowserSession._acquire_page 块内显式拆元组写入 handle.last_response
#  —— 多 page 并发时 response 不会互相覆盖（spec §3.2 H4 修复）。
# ════════════════════════════════════════════════════════════════════


class PlaywrightPageWrapper:
    """Owns a single Playwright Page; no playwright/browser/context lifecycle."""

    def __init__(self, page: Any) -> None:
        self._page = page
        # Phase 1 桥接：BrowserSession 仍读 last_response 属性。
        # Phase 2 走 fetch_json 元组返回路径后此字段不再被外部读，
        # 仅保留 for backwards compat。
        self.last_response: BrowserFetchResponse | None = None

    @property
    def page(self) -> Any:
        return self._page

    def is_closed(self) -> bool:
        """页面是否已关闭（Phase 2 release 时 health check 用）."""
        check = getattr(self._page, "is_closed", None)
        if check is None:
            return False
        try:
            return bool(check())
        except Exception:
            # 拿不到状态就当它还活着，让上层操作自己抛错暴露问题
            return False

    async def evaluate(self, script: str, arg: object | None = None) -> Any:
        return await self._page.evaluate(script, arg)

    async def goto(self, url: str) -> None:
        await self._page.goto(url, wait_until="domcontentloaded")

    async def fetch_json(
        self,
        url: str,
        params: dict[str, Any],
        *,
        referer: str | None = None,
    ) -> dict[str, Any]:
        """Phase 1 兼容签名：返回 dict，把 response 写到 self.last_response.

        Phase 2 BrowserSession._sync_handle_response 会读 self.last_response
        同步到 PageHandle.last_response —— 此为单 page 时无污染的桥接。
        N>1 时由 BrowserSession 在 _acquire_page 块内独占 handle，
        多 page 并发也不会互相覆盖（每个 wrapper 实例独立持有 last_response）。

        ─────────────────────────────────────────────────────────────
        R4-P14 观测增强：JS 端自测 RT + 总条数
        -------------------------------------------------------------
        外层 BrowserSession.fetch_json 已经把这一段记成 http_ms（一段大
        黑盒）。把 RT 拆得更细需要在 JS 里测 —— 因为：
          - fetch 真正 await 的网络耗时（rt_network_ms）
          - resp.text() 解 body 的耗时（rt_body_ms）
          - 整段 JS 总耗时（rt_total_ms）
        这三个值能区分"douyin 服务端慢"vs"大 body 拖慢"vs"playwright bridge
        来回开销"。bridge 开销 = http_ms - rt_total_ms，一减就出。
        ─────────────────────────────────────────────────────────────
        """
        import time as _t  # 局部 import，避免污染模块顶层
        full_url = f"{url}?{urlencode(params)}" if params else url
        t_eval0 = _t.perf_counter()
        payload = await self._page.evaluate(
            """
            async ({ url, referer }) => {
              const t0 = performance.now();
              const resp = await fetch(url, {
                method: 'GET',
                credentials: 'include',
                referrer: referer || undefined,
                headers: { accept: 'application/json, text/plain, */*' }
              });
              const t1 = performance.now();
              const text = await resp.text();
              const t2 = performance.now();
              return {
                ok: resp.ok,
                status: resp.status,
                url: resp.url,
                text: text,
                rt_network_ms: t1 - t0,
                rt_body_ms: t2 - t1,
                rt_total_ms: t2 - t0,
                body_bytes: text.length
              };
            }
            """,
            {"url": full_url, "referer": referer},
        )
        eval_ms = (_t.perf_counter() - t_eval0) * 1000
        text = str(payload.get("text") or "")
        self.last_response = BrowserFetchResponse(
            status_code=int(payload.get("status") or 0),
            text=text,
            url=str(payload.get("url") or full_url),
        )
        # 单行带齐：bridge 开销 = eval_ms - rt_total_ms（playwright IPC + JSON 序列化）
        logger.info(
            "[BBA] http.rt status=%s body_bytes=%d eval_ms=%.1f "
            "rt_network_ms=%.1f rt_body_ms=%.1f rt_total_ms=%.1f bridge_ms=%.1f",
            payload.get("status"),
            int(payload.get("body_bytes") or 0),
            eval_ms,
            float(payload.get("rt_network_ms") or 0.0),
            float(payload.get("rt_body_ms") or 0.0),
            float(payload.get("rt_total_ms") or 0.0),
            eval_ms - float(payload.get("rt_total_ms") or 0.0),
        )
        if not payload.get("ok"):
            raise RuntimeError(f"Browser fetch failed: HTTP {payload.get('status')} {text[:500]}")
        if not text or text == "blocked":
            raise RuntimeError(f"Browser fetch returned invalid body: {text!r}")
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Browser fetch returned non-JSON body: {text[:500]}") from exc

    async def local_storage(self) -> dict[str, str]:
        data = await self._page.evaluate("() => Object.assign({}, window.localStorage)")
        return {str(k): str(v) for k, v in (data or {}).items()}

    async def capture_websocket(
        self,
        url_substring: str,
        *,
        trigger_url: str | None = None,
        timeout_seconds: float = 15.0,
    ) -> str:
        """Capture the first websocket URL containing ``url_substring``.

        This is a bootstrap helper only: it captures the WSS URL opened by
        the page, not frame payloads.
        """
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[str] = loop.create_future()

        def on_ws(ws: Any) -> None:
            url = str(getattr(ws, "url", "") or "")
            if url_substring in url and not fut.done():
                fut.set_result(url)

        self._page.on("websocket", on_ws)
        try:
            if trigger_url:
                await self._page.goto(trigger_url, wait_until="domcontentloaded")
            return await asyncio.wait_for(fut, timeout=timeout_seconds)
        finally:
            remover = getattr(self._page, "remove_listener", None)
            if callable(remover):
                try:
                    remover("websocket", on_ws)
                except Exception:
                    pass

    async def close(self) -> None:
        await _safe_close(self._page)


# ════════════════════════════════════════════════════════════════════
#  PlaywrightContextHandle —— playwright/browser/context 三件套
# --------------------------------------------------------------------
#  唯一职责：管理 context 级别的资源生命周期，以及 page 工厂方法
#  （new_page）—— 让 BrowserSession.page_factory 调一下就能补位。
#
#  close 顺序固定：context.close() → playwright.stop() → sleep 0.1s
#  （sleep 是 playwright 实测的"资源真正回收前的尾巴时间"，去掉容易
#  在测试里看到 "asyncio: subprocess transport was not closed" warning）
# ════════════════════════════════════════════════════════════════════


class PlaywrightContextHandle:
    """Owns playwright + (browser=None for persistent) + context. Hot factory for pages."""

    def __init__(self, playwright: Any, context: Any) -> None:
        self._playwright = playwright
        self._context = context

    @property
    def context(self) -> Any:
        return self._context

    async def new_page_wrapper(self) -> PlaywrightPageWrapper:
        """Create a new Page from this context and wrap it.

        给 BrowserSession.page_factory 闭包调用 —— supervise 时
        page 死了用这个补位。
        """
        page = await self._context.new_page()
        return PlaywrightPageWrapper(page)

    async def close(self) -> None:
        # 按 spec §5.5 固定顺序回收：context → playwright.stop → sleep
        await _safe_close(self._context)
        stop = getattr(self._playwright, "stop", None)
        if stop is not None:
            result = stop()
            if hasattr(result, "__await__"):
                await result
        # 给 subprocess transport 回收的尾巴时间，避免 unclosed transport warning
        await asyncio.sleep(0.1)



# ════════════════════════════════════════════════════════════════════
#  create_playwright_browser_session —— 工厂入口
# ════════════════════════════════════════════════════════════════════


async def create_playwright_browser_session(
    session_key: SessionKey,
    config: BrowserConfig,
    *,
    request_gate: Any,
    on_cookie_expired: Any,
    on_origin_headers_captured: Callable[[dict[str, str]], None] | None = None,
    skip_stealth: bool = False,
    force_headful: bool | None = None,
) -> BrowserSession:
    """Create a BrowserSession backed by a Playwright page with anti-detection.

    Anti-detection stack (in order of importance):
      1. launch_persistent_context with per-cookie user_data_dir
         → 持久化 IndexedDB / Service Worker / cache / history，
           让抖音前端 SDK 看到的指纹连续性接近真实用户
      2. _WEBDRIVER_OVERRIDE_JS（JS 层 override navigator.webdriver）
         → 无论 skip_stealth 都注入，让 navigator.webdriver 返回 undefined
         → 不依赖 --disable-blink-features=AutomationControlled 命令行 flag
      3. ignore_default_args 排除 --enable-automation +
         --disable-blink-features=AutomationControlled
         → 不进入 automation 模式 + 不传触发 infobar 的 flag
      4. chromium_sandbox=True（Windows/macOS）
         → 阻止 patchright 自动注入 --no-sandbox（也会触发 infobar）
      5. add_init_script(stealth_override.js)（skip_stealth=False 时）
         → 自研精确 patch（languages / platformVersion / screen 等）
      4. viewport 1920x1080
         → 主流真实分辨率
      5. cookie 注入 + storage_state 同步
         → 复用已登录态

    R4-P14 Phase 2：根据 config.page_pool_size 创建 N 个 page（默认 1，
    向后兼容；N>1 时启用真正的多 page 并发，每个 page 借/还独立）。
    N>1 时串行 launch，每个 page 还要等 1.5s SDK 初始化 —— 启动总耗时
    ≈ N × 1.5s + 浏览器 boot(~5s)。N=5 约 12-22s，注意 manager 层
    timeout 必须 > 30s 才能避开 cold-start 误超时。

    Args:
        on_origin_headers_captured (R7 P5):
            可选 callback。BBA 浏览器首次发出针对 platform 主域 (kuaishou.com /
            douyin.com) 的 document 请求时被调用一次，参数是 wire 上的关键身份
            头 dict (lower-case keys: ``user-agent`` / ``sec-ch-ua`` /
            ``sec-ch-ua-mobile`` / ``sec-ch-ua-platform`` / ``accept-language``)。
            调用方应在 callback 内把 headers 持久化到 cookie_jar.metadata，
            供 cffi 第二阶段做身份对齐。callback 抛异常被吞（不影响 BBA），
            但会 logger.warning。
            ⚠️ 一次性事件：listener 捕获后立即 self-unregister，避免后续请求
            重复触发 callback。
        skip_stealth: True 时跳过 stealth_override.js 注入。BBA 登录场景下
            用户直接操作浏览器，不需要反检测补丁；且补丁会干扰快手 websig4
            等 SDK 导致二维码登录失败。默认 False（数据抓取场景需要 stealth）。
    """
    try:
        # patchright = drop-in undetected fork of playwright. Identical API,
        # 只需 import 路径不同。要点：
        #   - patchright 修了 Runtime.Enable / Console.Enable / launch flags
        #     等 CDP 协议层泄露（Cloudflare/Datadome/Akamai 检测的同款）
        #   - patchright 不修 UA 字符串里的 "HeadlessChrome"（这是 Chromium
        #     headless=old 模式遗留）。我们用 channel='chrome' + --headless=new
        #     + 显式 user_agent 三件套绕掉这个问题（详见下方 launch_kwargs）。
        # 2026-05-29 引入：抖音 SDK 检测 HeadlessChrome 后把 webcast WSS
        # 路由降级到 frontier-im（不可用），头是关键所在。
        from patchright.async_api import async_playwright
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise RuntimeError(
            "patchright is required for browser_backed actions. "
            "Install: pip install patchright && patchright install chrome"
        ) from exc

    playwright = await async_playwright().start()

    user_data_dir = _resolve_user_data_dir(session_key)
    storage_state = _load_storage_state_if_present(session_key)

    # ── 1. Persistent context: 用户数据目录决定指纹连续性 ──
    #   storage_state 仅在首次 / 用户数据目录被清理时用于填种 cookie；
    #   后续浏览器会从 user_data_dir 自身的 cookie store / IndexedDB
    #   恢复，比纯 storage_state 模式真实得多。
    #
    # ── 反检测三件套（2026-05-29 落地，解决抖音 frontier-im 降级）──
    # 现象：headless=True 时 Chromium 二进制 UA 含 "HeadlessChrome"，
    # navigator.userAgentData.brands 也含 HeadlessChrome；抖音 SDK 一眼
    # 识破后把 /webcast/im/push/v2/ WSS 降级到 /frontier-im/ws/v2 通用
    # IM（不带弹幕推送），导致 collect_live_events 抓不到数据。
    #
    # 配方：
    #   ① channel="chrome"        — 用真实 Google Chrome 二进制（brands
    #                                自动是 "Google Chrome" 而非 Chromium）
    #   ② headless=False          — 关键：让 patchright/playwright 不
    #                                自动注入 --headless=old (chrome-headless-shell)
    #   ③ args=["--headless=new"] — 自己注入 Chrome 112+ 的新 headless
    #                                模式（浏览器跑在不可见窗口，但完整
    #                                复用真实 Chrome 代码路径）
    #   ④ user_agent=_REAL_...    — 显式覆盖 UA 字符串去掉残留的
    #                                "HeadlessChrome"（Chromium 历史遗留 bug，
    #                                new headless 没修这一项）
    # 实测：抖音直播间 1 秒拿到 webcast/im/push/v2/，全套指纹干净。
    #
    # ── BBA headless 开关 ──
    # 来源：~/.crawlhub/config.yaml -> browser.bba_headful（默认 false）
    # 单一信源：不再读 ENV。调用方需要单次强制 headful（例如 BBA 登录流程）
    # 通过函数参数 force_headful=True 传入。
    #
    # 注：headful 模式下必须移除 --no-startup-window（这个 arg 会让 Chrome
    # 启动后不开任何窗口 → 没 page → CDP 连不上 → 180s timeout）。
    #
    # 2026-05-29 修复：headless 模式同样需要去掉 --no-startup-window。
    #   原以为只是 headful 问题，实际 headless=new 模式下 Chrome 启动后
    #   依然需要至少一个 page 让 CDP 接管。--no-startup-window 在两种模式
    #   下都会破坏 CDP 连接（症状：launch_persistent_context 抛
    #   "Connection closed while reading from the driver"）。
    if force_headful is not None:
        bba_headful = bool(force_headful)
    else:
        try:
            from crawlhub.core.config import get_config
            bba_headful = bool(get_config().browser.bba_headful)
        except Exception:
            bba_headful = False

    if skip_stealth:
        # BBA 登录模式：最小化启动参数，避免 websig4 等 SDK 检测到
        # --disable-extensions / --disable-features=IsolateOrigins 等自动化指纹
        chromium_args = list(_BBA_LOGIN_LAUNCH_ARGS)
        if not bba_headful:
            chromium_args.append("--headless=new")
        logger.info("[BBA] login mode: minimal launch args (%d)", len(chromium_args))
    else:
        # 数据抓取模式：完整 stealth 参数
        _base_args = [a for a in _STEALTH_LAUNCH_ARGS if a != "--no-startup-window"]
        if bba_headful:
            chromium_args = _base_args
            logger.info("[BBA] headful mode (config.browser.bba_headful=true or force_headful=True)")
        else:
            chromium_args = _base_args + ["--headless=new"]
            logger.info("[BBA] headless mode (--headless=new)")

    # 探测宿主环境，得到 UA / platformVersion 等 stealth 配置
    _host_info = detect_host_environment()

    launch_kwargs: dict[str, Any] = {
        # channel="chrome"：启动本机 Google Chrome 二进制。
        # ★ 安全说明：这只是"借用 chrome.exe 这个文件"，配合独立的
        #   user_data_dir 跑一个全新 profile —— 绝不读写用户主 Chrome 的
        #   cookie / history / 标签页 / session。
        #   daemon 代码里也没有 taskkill 逻辑。
        # ★ 为何必须 channel="chrome"：patchright 自带的 Chromium 145 在
        #   headless=False + --headless=new 模式下有 CDP pipe 连接 bug
        #   （180s timeout），而真实 Chrome 148 没有此问题。
        "channel": "chrome",
        # headless=False + --headless=new 组合：
        #   headless=False 防止 patchright 自动注入 --headless=old
        #   --headless=new 是 Chrome 112+ 的新 headless 模式：窗口不可见，
        #   但走真实 Chrome 代码路径（不是 chrome-headless-shell fork）。
        #   效果：navigator.userAgentData.brands 不含 HeadlessChrome。
        "headless": False,
        "args": chromium_args,
        # ⚠️ viewport 必须 = 宿主真实逻辑分辨率（见 _detect_screen_size 注释）
        # 老版本写死 1920x1080，但宿主是 2K (2560x1440) / 4K (3840x2160) 时，
        # navigator.screen.width/height 会被抖音 acrawler.js 读出来注入到
        # search/single 等接口的 query string 里 → 系统性 leak。
        "viewport": {
            "width": _host_info.screen_width,
            "height": _host_info.screen_height,
        },
        # ── ignore_default_args：从 patchright 默认 args 中排除触发 infobar 的项 ──
        # 1. --enable-automation：Chrome 默认加，导致 navigator.webdriver=true +
        #    "Chrome 正在受自动化控制" infobar。排除后 Chrome 不进入 automation
        #    模式，navigator.webdriver 保持 false。
        # 2. --disable-blink-features=AutomationControlled：patchright 内部
        #    (chromiumSwitches.js L87) 作为默认 arg 加入，Chrome 对其报
        #    "不受支持的命令行标记" infobar（占 40px 视口），反爬 SDK 可探测
        #    outerHeight-innerHeight 异常。排除后由 #1 的机制同等保护。
        "ignore_default_args": [
            "--enable-automation",
            "--disable-blink-features=AutomationControlled",
        ],
        "locale": _REAL_LOCALE,
        "timezone_id": _REAL_TIMEZONE_ID,
    }

    # ── chromium_sandbox：抑制 patchright 自动注入 --no-sandbox ──
    # patchright (chromium.js L290-291) 默认加 --no-sandbox：
    #   if (options.chromiumSandbox !== true)
    #       chromeArguments.push("--no-sandbox");
    # Windows/macOS 上 Chrome sandbox 原生工作良好，--no-sandbox 会触发
    # "您使用的是不受支持的命令行标记" infobar（占 40px 视口），
    # 被快手 websig4 / 抖音 SDK 等反爬检测为自动化。
    # Linux 容器/CI 需要 --no-sandbox 才能启动，不设 chromium_sandbox。
    if not sys.platform.startswith("linux"):
        launch_kwargs["chromium_sandbox"] = True

    if skip_stealth:
        # 登录模式：不覆盖 UA / accept-language，让 Chrome 用自身默认值
        # 真实 Chrome (channel="chrome") 的默认 UA 和 headers 本身就是"正常"的
        pass
    else:
        # 数据抓取模式：需要精确控制 UA 和 accept-language 与 stealth JS 对齐
        # UA 字符串仍残留 HeadlessChrome（Chromium 上游 bug），显式覆盖。
        # 必须**动态构造**：版本号读本机真实 Chrome，不能硬编码——否则
        # navigator.userAgent (你声明的版本) 会跟 navigator.userAgentData.uaFullVersion
        # (真 Chrome 版本) 矛盾，被反爬一秒识破。
        launch_kwargs["user_agent"] = _host_info.ua
        # ── Accept-Language 与 stealth JS 严格对齐 ────────────────────────
        # 见文件头 REAL_ACCEPT_LANGUAGE 注释：locale=zh-CN 单独设会让
        # 出向 HTTP 头退化成裸 "zh-CN"，与 navigator.languages 的 4 项不一致。
        # 这里显式 override，让 ExtraInfo 阶段抓到的 accept-language 是
        # "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7" —— 与 stealth_override.js
        # 第 200~211 行 navigator.languages = ['zh-CN','zh','en-US','en'] 一致。
        # ENV override 已内置于 host_environment.REAL_ACCEPT_LANGUAGE 常量自身。
        launch_kwargs["extra_http_headers"] = {
            "accept-language": REAL_ACCEPT_LANGUAGE,
        }
    logger.info(
        "[stealth] host=%s os=%s pv=%s patch_pv=%s screen=%dx%d ua=%s",
        _host_info.os, _host_info.os_version, _host_info.platform_version_hint,
        _host_info.should_patch_platform_version,
        _host_info.screen_width, _host_info.screen_height,
        _host_info.ua[:80],
    )
    context = await playwright.chromium.launch_persistent_context(
        user_data_dir=str(user_data_dir),
        **launch_kwargs,
    )
    context_handle = PlaywrightContextHandle(playwright, context)

    # ════════════════════════════════════════════════════════════════════
    #  R7 P5：Origin headers capture —— BBA 实抓 wire headers 给 cffi 用
    # ────────────────────────────────────────────────────────────────────
    #  问题背景：
    #    crawlhub 用 BBA Chrome 登录拿 cookie，再切到 curl_cffi 用同一份
    #    cookie 出网。两阶段必须身份对齐——同 cookie 不同 UA / 不同
    #    sec-ch-ua-platform 是反爬的天然分类信号。
    #
    #  抓什么：
    #    BBA 第一次对 platform 主域（kuaishou.com / douyin.com）发出的
    #    document 请求 wire headers。这是浏览器的"开场白"——所有静态身份
    #    头（UA / Client Hints 三件套 / Accept-Language）此时已稳定下来。
    #
    #  抓哪个：
    #    context.on("request") 是 context-level event，不依赖具体 page。
    #    listener 第一次命中后立刻 self-unregister，避免后续请求噪音 +
    #    listener 长存导致的内存累积。
    #
    #  抓到怎么办：
    #    通过 caller 传入的 on_origin_headers_captured callback 推上去，
    #    由 caller（daemon factory）写入 cookie_jar.metadata 持久化。
    #    callback 必须便宜——listener 在 patchright 事件循环内同步调用。
    # ════════════════════════════════════════════════════════════════════
    if on_origin_headers_captured is not None:
        _platform_host_suffix = _platform_host_suffix_for(session_key.platform)
        # 用 list 包 bool 避开 Python 闭包的 nonlocal 麻烦——
        # listener 只读写这一个 cell，无需 asyncio.Lock。
        _captured_flag = [False]
        # 白名单：只持久化身份层 headers。
        # 不抓 accept / accept-encoding（transport 层，与 ja3 强绑定，让
        # cffi impersonate 自己生成才不会双方矛盾）。
        _ORIGIN_KEYS = (
            "user-agent",
            "sec-ch-ua",
            "sec-ch-ua-mobile",
            "sec-ch-ua-platform",
            "accept-language",
        )

        def _on_request(req: Any) -> None:
            if _captured_flag[0]:
                return
            # 只抓主文档 navigation；过滤掉 acrawler.js / 第三方 CDN /
            # fetch / xhr 这些子资源——它们的 headers 是子集且可能被
            # acrawler 改写，不能代表浏览器的"开场白"。
            try:
                if req.resource_type != "document":
                    return
            except Exception:
                return
            try:
                host = (urlparse(req.url).hostname or "").lower()
            except Exception:
                return
            if not host.endswith(_platform_host_suffix):
                return

            try:
                headers_raw = req.headers  # patchright 已 lower-case keys
            except Exception as exc:
                logger.warning("[origin] read request.headers failed: %s", exc)
                return

            extracted: dict[str, str] = {}
            for key in _ORIGIN_KEYS:
                v = headers_raw.get(key)
                if v:
                    extracted[key] = str(v)

            # 最小完备子集校验：UA + sec-ch-ua-platform 是身份的两个支点
            if "user-agent" not in extracted or "sec-ch-ua-platform" not in extracted:
                logger.warning(
                    "[origin] capture skipped — missing UA or platform "
                    "(host=%s keys=%s)", host, list(extracted.keys()),
                )
                return

            # 标记 + self-unregister 必须在调 callback 之前——避免 callback
            # 慢 / 抛异常时事件队列里其它 request 又跑一次 listener
            _captured_flag[0] = True
            try:
                context.remove_listener("request", _on_request)
            except Exception as exc:
                logger.debug("[origin] remove_listener swallow: %s", exc)

            try:
                on_origin_headers_captured(extracted)
            except Exception as exc:
                logger.warning(
                    "[origin] callback raised: %s (extracted ua=%s platform=%s)",
                    exc, extracted.get("user-agent", "")[:60],
                    extracted.get("sec-ch-ua-platform", ""),
                )
                return

            logger.info(
                "[origin] captured wire headers from %s: ua=%s platform=%s",
                host,
                extracted.get("user-agent", "")[:60],
                extracted.get("sec-ch-ua-platform", ""),
            )

        try:
            context.on("request", _on_request)
        except Exception as exc:
            logger.warning(
                "[origin] failed to attach context.on('request'): %s", exc,
            )

    # ⚠️ 关键修复（2026-05-29 by fingerprint_audit 诊断暴露）：
    #   launch_persistent_context 启动时会自动创建一个空白 page (context.pages[0])。
    #   这个 page **早于** add_init_script 注入，意味着我们后面 add_init_script 注入
    #   的 stealth_override.js 在这个 page 上**永远不会跑**。
    #   page.goto() 同 origin / about: 跳转也不会重新触发 init script。
    #
    #   ⚠️ 不能直接关掉这个空白 page —— headful 模式下这个 page 就是浏览器进程的
    #   主窗口，关掉等于关掉整个 Chrome 进程（实测 TargetClosedError）。
    #
    #   ★ 正确做法：保留这个 page，但**显式在它上面 evaluate 一次** stealth 代码，
    #   把 add_init_script 没生效的部分手动补上。后续 new_page 创建的 page 走
    #   add_init_script 路径，无需手动补。
    _stale_pages = list(context.pages)

    # ── storage_state 注入（仅在 profile 全新时执行）─────────────
    #   ⚠️ 2026-06-04 修：旧注释说"注入也无害"——错的，是反向 bug。
    #
    #   场景重建：cookie 文件是 N 秒前的快照，SQLite 是浏览器实时
    #   更新的。reuse/refresh 拉起 chrome 时：
    #     1. launch_persistent_context 从 SQLite 加载活 cookie
    #        → 此时浏览器已经是登录态
    #     2. add_cookies(storage_state.cookies) 用文件里**旧的**
    #        web_st / passToken **覆盖** SQLite 里的**活的**值
    #     3. 后续 navigate 主域 → 服务端看到过期 token →
    #        判定异常 → 异步级联作废整个 SSO 域 → 用户掉登录
    #
    #   症状（2026-06-04 排查 R7 P6 复现）：
    #     - 独立脚本打开 profile 有登录态（不调 add_cookies）
    #     - BBA 拉起同一 profile 立即掉登录（probe result=109 expired）
    #
    #   修复策略：只在 profile 是全新的（无 SQLite Cookies 文件）
    #   时才注入 storage_state 填种。已有持久化 cookie 时**绝对不
    #   能注入**，让 chrome 完全信任自己的 SQLite。
    if storage_state and storage_state.get("cookies"):
        # Chrome 持久化 cookie 在两个位置之一（版本相关）
        _cookies_db_legacy = user_data_dir / "Default" / "Cookies"
        _cookies_db_modern = user_data_dir / "Default" / "Network" / "Cookies"
        _profile_has_cookies = (
            _cookies_db_legacy.is_file() or _cookies_db_modern.is_file()
        )
        if _profile_has_cookies:
            logger.info(
                "[cookie_inject] %s: profile already has SQLite cookies "
                "(%s), SKIP storage_state injection to avoid overwriting "
                "live cookies with stale snapshot",
                session_key.platform,
                _cookies_db_modern if _cookies_db_modern.is_file()
                else _cookies_db_legacy,
            )
        else:
            logger.info(
                "[cookie_inject] %s: fresh profile, seeding %d cookie(s) "
                "from storage_state file",
                session_key.platform,
                len(storage_state["cookies"]),
            )
            try:
                await context.add_cookies(storage_state["cookies"])
            except Exception as exc:
                logger.warning(
                    "[cookie_inject] %s: add_cookies failed: %s",
                    session_key.platform, exc,
                )

    # ── 2. stealth_override.js: 必须在任何页面 navigate 之前注入 ──
    # 2026-05-29: 替换 puppeteer-extra-stealth (stealth.min.js)
    #   原 stealth.min.js 是 176KB 黑盒混淆代码，做的事既不少又不全：
    #     - 主动注入假 chrome.runtime → 在普通页面被反向识别为伪装
    #     - 注入固定 plugins polyfill → 反成"stealth 库指纹"
    #     - 没修 platformVersion / languages 等真正的 leak
    #   改用自己的 stealth_override.js（~200 行可读代码）：
    #     - 只 patch probe.js diff 出来的真实 leak
    #     - 跨平台自适应：从 __CRAWLHUB_STEALTH_CONFIG__ 读宿主信息决定 patch
    #     - 不主动添加真 Chrome 没有的字段
    #   旧 stealth.min.js 文件保留在 _stealth/ 目录下做备查，但代码不引用。
    #
    # 两阶段注入：
    #   Step 1: 注入 __CRAWLHUB_STEALTH_CONFIG__ 配置（host_info dict）
    #   Step 2: 注入 stealth_override.js（读 step 1 的配置做 patch）
    # add_init_script 是 context 级，对该 context 下任何 page navigate 时
    # 自动按注册顺序执行（包括 Phase 2 后续创建的 page，无需额外处理）。
    # Worker 上下文不会自动跑 init script——stealth_override.js 内部通过
    # 劫持 Worker 构造器把 patch 拼到 worker code 里。
    _stealth_config_json = json.dumps(_host_info.to_dict(), ensure_ascii=False)
    _config_inject_script = (
        f"globalThis.__CRAWLHUB_STEALTH_CONFIG__ = {_stealth_config_json};"
    )
    _stealth_override_code: str | None = None

    # ── navigator.webdriver override：无论 skip_stealth 都注入 ──
    # channel="chrome" 用系统 Chrome，没有 patchright 二进制补丁，
    # navigator.webdriver 会是 true。必须 JS 层覆盖。
    # 详见 _WEBDRIVER_OVERRIDE_JS 常量的注释。
    await context.add_init_script(script=_WEBDRIVER_OVERRIDE_JS)
    logger.info("[stealth] injected navigator.webdriver override (always-on)")

    if not skip_stealth:
        await context.add_init_script(script=_config_inject_script)
        if _STEALTH_OVERRIDE_PATH.exists():
            _stealth_override_code = _STEALTH_OVERRIDE_PATH.read_text(encoding="utf-8")
            await context.add_init_script(script=_stealth_override_code)
            logger.info("[stealth] injected stealth_override.js (config=%s)", _host_info.os)
        else:
            logger.warning(
                "[stealth] stealth_override.js not found at %s — running unprotected",
                _STEALTH_OVERRIDE_PATH,
            )
    else:
        logger.info("[stealth] skip_stealth=True — not injecting stealth patches (login mode)")

    # ★ 给 launch 时自带的空白 page 手动补跑一次：add_init_script 只对**之后**
    #   navigate 的 page 生效，自带空白 page 的注入时机已过。
    #   evaluate 是同步在当前 page 的 main world 跑，效果跟 init script 等价。
    #
    # ⚠️ 2026-05-29 second fix（fingerprint_audit auto-run 暴露）：
    #   patchright/playwright 的 add_init_script 是按"page lifetime"绑定的——
    #   `pages[0]` 在 add_init_script 之前就被 launch_persistent_context 创建，
    #   它**永远拿不到**这两份 init script，无论后续 navigate 多少次。
    #   只在启动那一刻 evaluate 一次的话：about:blank → baidu.com 跨 origin
    #   navigation 后 main world 重置，所有 patch 蒸发，languages/PV 又变回
    #   未 patch 状态。
    #   修复：给 stale_page 注册 framenavigated 监听器，每次 page navigate
    #   后都重新 evaluate 一遍 stealth 代码（stealth_override.js 已改成
    #   幂等：configurable getter 重复 defineProperty 会覆盖前一次）。
    #
    # navigator.webdriver override 也需要同样的 stale page + framenavigated
    # 补跑机制，且不依赖 skip_stealth（无论什么模式都要覆盖 webdriver）。
    for stale_page in _stale_pages:
        # ── webdriver override: always ──
        try:
            await stale_page.evaluate(_WEBDRIVER_OVERRIDE_JS)
        except Exception as exc:
            logger.warning(
                "[stealth] failed to back-fill webdriver override on stale page: %s", exc,
            )

        # ── stealth_override.js: only when skip_stealth=False ──
        if not skip_stealth:
            try:
                await stale_page.evaluate(_config_inject_script)
                if _stealth_override_code is not None:
                    # 注意：stealth_override.js 是 IIFE，evaluate 可以直接吃 IIFE 字符串
                    await stale_page.evaluate(_stealth_override_code)
            except Exception as exc:
                # 单 page 补跑失败不致命：后续 new_page 仍能正常跑 init script
                logger.warning(
                    "[stealth] failed to back-fill stealth on stale page: %s", exc,
                )

        # ── framenavigated 兜底（所有模式：webdriver always + stealth conditional）──
        # 给 stale_page 注册 framenavigated 监听器，每次 page navigate 后重新
        # evaluate 代码（幂等：configurable getter 重复 defineProperty 会覆盖前一次）。
        # 把 evaluate 闭包绑到这一个具体的 stale_page；用 _captured 闭包
        # 避免 Python 的 late-binding 陷阱（多 page 时所有 listener 都会
        # 引用同一个 stale_page）。
        def _make_listener(target_page):
            async def _on_framenavigated(frame):
                # 只处理 main frame，不管 iframe
                try:
                    if frame is not target_page.main_frame:
                        return
                except Exception:
                    return
                # webdriver override: always
                try:
                    await target_page.evaluate(_WEBDRIVER_OVERRIDE_JS)
                except Exception:
                    pass
                # stealth_override.js: only when skip_stealth=False
                if not skip_stealth and _stealth_override_code is not None:
                    try:
                        await target_page.evaluate(_config_inject_script)
                        await target_page.evaluate(_stealth_override_code)
                    except Exception as exc:
                        logger.info(
                            "[stealth] framenavigated re-eval skipped: %s",
                            type(exc).__name__,
                        )
            return _on_framenavigated

        try:
            stale_page.on("framenavigated", _make_listener(stale_page))
            logger.info("[stealth] framenavigated listener attached to stale page")
        except Exception as exc:
            logger.warning(
                "[stealth] failed to attach framenavigated listener: %s", exc,
            )

    # ── 3. R7: chrome 启动只对 stale_page 触发 SDK init ──
    # stale_page 是 launch_persistent_context 自带的空白 page，必须保留
    # （headful 模式下关掉会挂整个 chrome；stealth_override 通过 framenavigated
    # 持续 patch 它）。它**不**进入 BrowserSession._owned_pages 业务集合，
    # 只作为 chrome 进程的隐形 anchor + 首次 SDK init 触发器。
    #
    # 后续业务 page 由 BrowserSession.new_owned_page → context_handle.new_page_wrapper
    # lazy 创建。每个新业务 page 也要走 home + SDK init 才能稳定干活——
    # 这个逻辑由 context_handle.new_page_wrapper 包装，对调用方透明。
    home_url = _home_url(session_key.platform)
    for stale_page in _stale_pages:
        # stale_page 触发 SDK init（首次加载平台 home）
        try:
            await stale_page.goto(home_url, wait_until="domcontentloaded")
            await asyncio.sleep(_SDK_INIT_WAIT_SECONDS)
        except Exception as exc:
            logger.warning("[stealth] stale_page initial goto failed: %s", exc)

    # R7：让 context_handle.new_page_wrapper 自带 home goto + SDK init
    # 这样 BrowserSession.new_owned_page 拿到的就是已 init 好的 page
    _orig_new_page_wrapper = context_handle.new_page_wrapper
    async def _new_page_wrapper_with_init() -> PlaywrightPageWrapper:
        wrapper = await _orig_new_page_wrapper()
        await wrapper.goto(home_url)
        await asyncio.sleep(_SDK_INIT_WAIT_SECONDS)
        return wrapper
    context_handle.new_page_wrapper = _new_page_wrapper_with_init  # type: ignore[method-assign]

    # ── 4. 构造 BrowserSession（R7：无 pool，按需 lazy 开 page）──
    session = BrowserSession(
        context_handle=context_handle,
        request_gate=request_gate,
        on_cookie_expired=on_cookie_expired,
    )
    return session


# ════════════════════════════════════════════════════════════════════
#  BBA Login Session —— 全平台统一登录/刷新入口
# ════════════════════════════════════════════════════════════════════
#
#  设计哲学（R7 P5 统一改造，2026-06-03）：
#
#    "一条唯一的浏览器启动路径，所有场景走同一条路。"
#
#    登录/刷新/抓数据 统一走 create_playwright_browser_session，
#    差异通过参数表达，不通过代码分叉表达。这意味着：
#
#      1. patchright + stealth_override.js（不是 vanilla playwright）
#      2. launch_persistent_context + per-cookie user_data_dir
#         （不是临时 browser.new_context）
#      3. on_origin_headers_captured callback（不是"抓不到就不抓"）
#      4. IndexedDB / SW cache 跨会话存活（不会触发 SSO 失效）
#
#    历史血泪：
#      旧 _run_playwright_login 用 vanilla playwright + 临时 context，
#      造出来的 cookie 叫"流浪 cookie"——有 cookie file 但没有
#      user_data_dir 也没有 origin metadata。后续 BBA 抓数据时
#      打开的是全新的空白 user_data_dir → web SDK 检测到 IndexedDB
#      / localStorage device 凭证缺失 → 上报后端"新设备" →
#      快手 SSO 域 invalidation → 手机端跟着掉登录。
#
#  同步包装：
#    bba_login_session() 是同步入口（供 routes.py threading.Thread 调），
#    内部 asyncio.run() 起独立 event loop。不能复用 daemon 的 loop——
#    那个 loop 可能在跑别的 task。
# ════════════════════════════════════════════════════════════════════

import time as _time_mod


# ── 平台 → 登录 URL 映射 ──────────────────────────────────────────
#    bilibili: 开主页 www.bilibili.com，用户在页内弹窗扫码登录，
#    BROWSER_LOGIN_CHECK_JS 检测 .header-login-entry 消失即已登录。
#    不再跳 passport.bilibili.com/login（旧方案导致页面闪烁 +
#    二次导航覆盖 Factory 已加载的首页）。
#
#    weibo (2026-06-05 改造)：同 bilibili，开主页 weibo.com。
#      * 已有有效 cookie → 注入后 SSR 直接渲染登录态，stage1 立即 PASS
#      * 无 cookie → 主页右上角"立即登录"弹窗扫码，登录后页内 SPA
#        刷新 SSR HTML，BROWSER_LOGIN_CHECK_JS 通过 fetch('/') 检测到
#        非零 uid，stage1 PASS
#      旧方案 passport.weibo.com/sso/signin 的问题：
#        - "刷新已有 cookie"场景：用户期望直接落主页验证登录态，结果
#          被强制带到登录页，已登录态被忽略；
#        - 登录后会跳到 weibo.com/newlogin?...，SPA 不重新 SSR，
#          window.$CONFIG 永远是登录前的旧快照（v1 stage1 永远 FAIL）。
_LOGIN_URLS: dict[str, str] = {
    "bilibili": "https://www.bilibili.com",
    "douyin":   "https://www.douyin.com",
    "kuaishou": "https://www.kuaishou.com",
    "weibo":    "https://weibo.com",
    "qimai":    "https://www.qimai.cn/account/signin",
}

# ── 平台 → 主页 URL 映射（BBA 登录检测用）────────────────────────
#    登录页 != 主页 的平台，在轮询时需要判断是否已离开登录页。
#
#    ⚠️ 必须与 _LOGIN_URLS 保持完全一致（含/不含 www 都要对齐），
#    否则 stage1 polling 的"is current_url 还在 login page"判断
#    （line ~1485）会因为字符串前缀不匹配而误判为"仍在登录页",
#    永远跳过 JS 检测 → stage1 永远 FAIL。
#    weibo 取 ``https://weibo.com`` (no www)，与生产 probe URL 保持
#    同 origin（HTTP probe 走的也是 ``weibo.com/``）。
_HOME_URLS: dict[str, str] = {
    "kuaishou": "https://www.kuaishou.com",
    "douyin":   "https://www.douyin.com",
    "bilibili": "https://www.bilibili.com",
    "weibo":    "https://weibo.com",
    "qimai":    "https://www.qimai.cn",
}


def _get_browser_login_js(platform: str) -> str | None:
    """Lazy-load the ``BROWSER_LOGIN_CHECK_JS`` snippet for *platform*.

    Returns ``None`` for unknown platforms (caller falls back to HTTP probe).
    Lazy import avoids a hard core → crawlers dependency at module level.

    The JS snippet runs in the browser page via ``page.evaluate()`` and
    returns ``{ ok: bool, reason: str, extras: {} }`` — the same semantics
    as ``check_login_from_html()`` but without the expensive ``page.content()``
    full-DOM serialization that causes visible page flickering on heavy SPAs.
    """
    if platform == "douyin":
        from crawlhub.crawlers.douyin.crawler.client import DouyinSDK
        return DouyinSDK.BROWSER_LOGIN_CHECK_JS
    if platform == "kuaishou":
        from crawlhub.crawlers.kuaishou.crawler.client import KuaishouSDK
        return KuaishouSDK.BROWSER_LOGIN_CHECK_JS
    if platform == "bilibili":
        from crawlhub.crawlers.bilibili.crawler.client import BilibiliClient
        return BilibiliClient.BROWSER_LOGIN_CHECK_JS
    if platform == "weibo":
        from crawlhub.crawlers.weibo.crawler.client import WeiboClient
        return WeiboClient.BROWSER_LOGIN_CHECK_JS
    if platform == "qimai":
        from crawlhub.crawlers.qimai.crawler.client import QimaiClient
        return QimaiClient.BROWSER_LOGIN_CHECK_JS
    return None


def bba_login_session(
    platform: str,
    label: str | None = None,
    *,
    timeout: int = 300,
    skip_stealth: bool = False,
) -> str:
    """Launch a BBA browser for login/refresh. Blocking call — run from thread.

    Uses the SAME factory as data-crawling (patchright + persistent_context
    + stealth_override.js + origin headers capture), ensuring:

    1. user_data_dir is consistent across login and subsequent crawls
    2. wire headers are captured and persisted to cookie metadata
    3. IndexedDB / SW cache survive across sessions (no SSO invalidation)

    Args:
        platform: Target platform name.
        label: If provided, update this specific cookie label;
               otherwise create new or update first existing.
        timeout: Max seconds to wait for login.
        skip_stealth: True 时跳过 stealth_override.js 注入 + 使用最小化
            启动参数。快手等平台的 websig4 SDK 会检测 --disable-extensions
            等自动化指纹导致 QR 登录失败，需要跳过。默认 False（保留完整
            stealth）。由各平台 Service.bba_skip_stealth 属性决定。

    Returns:
        One of "completed", "cancelled", "timeout".

    Raises:
        RuntimeError: If BBA factory fails to start browser.
    """
    return asyncio.run(
        _bba_login_session_impl(platform, label, timeout, skip_stealth)
    )


async def _bba_login_session_impl(
    platform: str,
    label: str | None,
    timeout: int,
    skip_stealth: bool,
) -> str:
    """Async implementation of bba_login_session."""
    from crawlhub.core.cookies import get_cookie_store
    from crawlhub.core.cookie_converters import convert_storage_state

    store = get_cookie_store()

    # ── 1. 定位 cookie → 构建 SessionKey ──────────────────────
    #    新增账号（label=None）：全新 profile，不注入旧 cookie
    #    刷新账号（label 有值）：复用旧 cookie 路径 + user_data_dir
    cookie_path = ""
    cookie_id: str

    if label:
        # 刷新指定 cookie → 复用其路径和 user_data_dir
        cp = store.get_cookie_path(platform, label)
        if cp.exists():
            cookie_path = str(cp)
        # cookie_id 必须与 daemon 数据抓取一致（含 platform 前缀），
        # 否则 _resolve_user_data_dir 算出的目录名不同 →
        # 数据抓取开空 profile → SSO invalidation → 掉登录。
        cookie_id = f"{platform}:{label}"
    else:
        # 新增账号 → 全新 profile，不注入旧 cookie
        # cookie_id 用时间戳占位，save_cookie 时会根据
        # account_id 自动重命名
        cookie_id = f"_new_{_time_mod.strftime('%Y%m%d_%H%M%S')}"

    session_key = SessionKey(
        platform=platform,
        cookie_id=cookie_id,
        cookie_path=cookie_path,
    )

    # ── 提前 resolve user_data_dir ────────────────────────────
    #    R7 P6：首次 _save_login_cookie 时把它的相对路径写进
    #    cookie metadata.profile_dir，让 daemon 后续打开同一
    #    profile，不再依赖目录 rename。这里早算一次，整个 BBA
    #    生命周期复用同一个 Path 对象。
    user_data_dir_path = _resolve_user_data_dir(session_key)

    # ── 2. Origin metadata persist callback ───────────────────
    #    BBA Factory 在第一次 navigate 主域时 fire callback，
    #    我们把 wire headers 写入 cookie_jar.metadata。
    #    目前只有 kuaishou 有 KuaishouCookieJar 的完整实现，
    #    其他平台 callback fire 了但 metadata 只记在 captured
    #    dict 里——等后续平台逐步接入。
    _captured: list[dict[str, str] | None] = [None]

    def _persist_origin(headers: dict[str, str]) -> None:
        _captured[0] = headers
        # 快手：仅内存捕获，不再立即 jar.save()。
        # 原因：浏览器刚 navigate 到主域时用户尚未登录，
        # jar.save() 会把 metadata 写入 cookie 文件（创建/覆盖），
        # 即使 cookie 无效。现在 probe 通过后才 _save_login_cookie，
        # 其中统一写 metadata + profile_dir。
        logger.info(
            "[bba_login] origin headers captured (platform=%s, ua=%s)",
            platform,
            headers.get("user-agent", "")[:60],
        )

    # ── 3. 强制 headful（登录必须可见）────────────────────────
    # 通过 force_headful=True 透传到 create_playwright_browser_session，
    # 不再依赖 ENV (config 是单一信源)。

    config = BrowserConfig()

    session: BrowserSession | None = None
    # saved_label: 提到 try 外，让 finally 块能在任意失败路径
    # 安全访问；用于 flush 后绑定 metadata.profile_dir
    # （见 _bind_profile_dir_after_flush）。
    saved_label: str | None = None
    try:
        session = await create_playwright_browser_session(
            session_key,
            config,
            request_gate=None,
            on_cookie_expired=None,
            on_origin_headers_captured=_persist_origin,
            skip_stealth=skip_stealth,
            force_headful=True,
        )

        # ── 4. Navigate stale_page 到登录 URL ────────────────
        #    BBA Factory 启动时 stale_page 已 goto home_url，
        #    现在导航到登录页。
        login_url = _LOGIN_URLS.get(platform, _home_url(platform))
        ctx = session._context_handle.context
        pages = ctx.pages

        if not pages:
            # 极端情况：persistent context 没有 page
            page = await ctx.new_page()
        else:
            page = pages[0]

        # ── 窗口前置 ──────────────────────────────────────────
        #    BBA 浏览器启动后用户需要看到窗口才能扫码。
        await _bring_bba_to_front(page, platform)

        # ── 导航到登录 URL ────────────────────────────────────
        #    Factory 启动时 stale_page 已 goto home_url。对于
        #    login_url == home_url 的平台（bilibili/douyin/kuaishou），
        #    再次 goto 同一 URL 会触发整页 reload，造成可见闪烁。
        #    若当前页面已在目标 origin+path 下则跳过导航。
        _login_base = login_url.split("?")[0].rstrip("/")
        _current_base = page.url.split("?")[0].rstrip("/")
        if _current_base == _login_base or _current_base.startswith(_login_base + "/"):
            logger.debug(
                "[bba_login] %s already on %s, skip goto", platform, _login_base,
            )
        else:
            await page.goto(login_url, wait_until="domcontentloaded")

        # ── 5. 确保 platform registry 已初始化 ────────────────────
        #    create_platform_service 依赖 discover_platforms 填充 registry，
        #    在 bba_login 场景下 daemon 可能还没跑过 discover。
        try:
            from crawlhub.core.registry import discover_platforms, create_platform_service
            if not create_platform_service(platform):
                discover_platforms()
        except Exception as exc:
            logger.warning("[bba_login] discover_platforms failed: %s", exc)

        # ── 6. Poll: 每 1s 检测浏览器页面登录状态 ────────────────
        #    新方案（R7 P6）：不再发独立 HTTP probe，而是直接
        #    检测浏览器页面 DOM 中是否还包含登录按钮。
        #    登录按钮消失 → 说明已登录 → 保存 cookie → "completed"
        #
        #    检测逻辑复用各平台 client 的 check_login_from_html()，
        #    与 HTTP probe 路径完全一致，不重复写 HTML 解析。
        #
        #    对于有独立登录页的平台（bilibili/weibo/qimai），
        #    额外判断 URL 是否已离开登录页。
        #
        #    三种退出路径：
        #      a) 页面检测登录成功 → 保存 cookie → "completed"
        #      b) 用户关闭浏览器 → 最后一次 HTTP probe fallback → 通过/取消
        #      c) 超时 → "timeout"
        start = _time_mod.time()
        browser_login_js = _get_browser_login_js(platform)
        login_url_prefix = _LOGIN_URLS.get(platform, "")
        home_url = _HOME_URLS.get(platform, "")

        def _probe_cookie(saved_label: str) -> bool:
            """Fallback: probe saved cookie via platform service. Returns True if valid."""
            try:
                from crawlhub.core.registry import create_platform_service
                from crawlhub.core.cookie_override import (
                    set_thread_cookie_override,
                    clear_thread_cookie_override,
                )
                cookie_path_str = str(store.get_cookie_path(platform, saved_label))
                set_thread_cookie_override(cookie_path_str)
                try:
                    svc = create_platform_service(platform)
                    if svc is None:
                        logger.warning("[bba_login] no service for %s, skip probe", platform)
                        return True  # 无法 probe 时降级为通过
                    result = svc.check_cookie()
                    is_valid = result.status == "valid"
                    if not is_valid:
                        logger.info(
                            "[bba_login] %s fallback probe result: %s (%s)",
                            platform, result.status, getattr(result, "message", ""),
                        )
                    return is_valid
                finally:
                    clear_thread_cookie_override()
            except Exception as exc:
                logger.warning("[bba_login] %s fallback probe failed: %s", platform, exc)
                return False

        # saved_label 在函数顶层（try 外）声明，这里不再重复
        # 声明，避免遮蔽 finally 的可见性。第一次 save 后锁定，
        # 后续同 label 覆盖写入。

        # ── Stage 1 (page check) 日志状态机 ─────────────────────
        #    每秒一次 polling，全打 info 会刷屏；纯 debug 又看不到进度。
        #    策略：状态翻转时打 info；同状态持续打 debug 采样（每 10 轮一次）。
        #    用户在 daemon log 里能看到 "stage1 page check: not-logged-in
        #    (loop=N)" 这种节奏，知道轮询活着 + 当前是 stage 1 fail。
        _stage1_loop_idx = 0
        _stage1_last_state: bool | None = None  # None = 尚未首次 evaluate

        def _log_stage1(is_logged_in: bool, *, source: str = "") -> None:
            """记录 stage 1 (page DOM check) 结果，状态翻转或周期采样。"""
            nonlocal _stage1_last_state
            tag = f" [{source}]" if source else ""
            if _stage1_last_state is None or _stage1_last_state != is_logged_in:
                logger.info(
                    "[bba_login] %s stage1 page check %s%s "
                    "(browser DOM evaluation)",
                    platform,
                    "PASSED — proceeding to stage2 probe" if is_logged_in
                        else "not-logged-in",
                    tag,
                )
                _stage1_last_state = is_logged_in
            else:
                # 同状态持续：每 10 轮打一次 debug 采样
                if _stage1_loop_idx % 10 == 0:
                    logger.debug(
                        "[bba_login] %s stage1 page check %s "
                        "(persistent, loop=%d)%s",
                        platform,
                        "PASSED" if is_logged_in else "not-logged-in",
                        _stage1_loop_idx,
                        tag,
                    )

        while _time_mod.time() - start < timeout:
            browser_closed = await _async_sleep_or_close(page, ctx, 1.0)
            _stage1_loop_idx += 1

            try:
                # ── 方案 A: 浏览器页面 DOM 检测（主路径）───────────
                #    用 page.evaluate(JS) 定向查询登录指示器，
                #    而非 page.content() 全量序列化 DOM。
                #    后者对抖音等重 SPA 会每秒一次阻塞主线程序列化
                #    整棵 DOM 树，导致可见闪烁。
                #
                #    ⚠️ JS 检测到登录后，不立即保存 cookie。
                #    先 probe 验证 cookie 有效性（写临时文件 → HTTP
                #    probe → 清理），probe 通过才保存。这防止了
                #    "页面显示已登录但 cookie 无效"时覆盖旧文件。
                if not browser_closed and browser_login_js is not None:
                    try:
                        current_url = page.url

                        # 对于有独立登录页的平台，如果还在登录页
                        # → 用户尚未登录成功，跳过检测
                        # （登录页没有主页的登录按钮指示器，会误判为"已登录"）
                        if login_url_prefix and home_url:
                            # 去掉 query string 比较
                            login_base = login_url_prefix.split("?")[0]
                            if current_url.startswith(login_base) and login_base != home_url.split("?")[0]:
                                # 还在登录页，跳过检测
                                pass
                            else:
                                # 已离开登录页 → 执行定向 JS 查询
                                result = await page.evaluate(browser_login_js)
                                is_logged_in = bool(result.get("ok"))
                                _log_stage1(is_logged_in, source="post-login-page")
                                if is_logged_in:
                                    # 先 probe，再 save
                                    native_cookie, probe_ok = await _extract_and_probe_cookie(
                                        ctx, platform, store, convert_storage_state,
                                        captured_origin=_captured[0],
                                    )
                                    if probe_ok and native_cookie is not None:
                                        try:
                                            saved = await _save_login_cookie(
                                                ctx, platform, store, saved_label or label,
                                                convert_storage_state,
                                                captured_origin=_captured[0],
                                                user_data_dir=user_data_dir_path,
                                                native_cookie=native_cookie,
                                            )
                                            if saved_label is None:
                                                saved_label = saved.label
                                        except Exception as save_exc:
                                            logger.warning("[bba_login] save cookie error: %s", save_exc)
                                        return "completed"
                                    else:
                                        logger.warning(
                                            "[bba_login] %s stage1 PASSED but stage2 FAILED — "
                                            "NOT saving (防止覆盖旧有效文件), continue polling",
                                            platform,
                                        )
                        else:
                            # 登录页即主页（douyin/kuaishou）→ 直接执行 JS
                            result = await page.evaluate(browser_login_js)
                            is_logged_in = bool(result.get("ok"))
                            _log_stage1(is_logged_in, source="homepage")
                            if is_logged_in:
                                native_cookie, probe_ok = await _extract_and_probe_cookie(
                                    ctx, platform, store, convert_storage_state,
                                    captured_origin=_captured[0],
                                )
                                if probe_ok and native_cookie is not None:
                                    try:
                                        saved = await _save_login_cookie(
                                            ctx, platform, store, saved_label or label,
                                            convert_storage_state,
                                            captured_origin=_captured[0],
                                            user_data_dir=user_data_dir_path,
                                            native_cookie=native_cookie,
                                        )
                                        if saved_label is None:
                                            saved_label = saved.label
                                    except Exception as save_exc:
                                        logger.warning("[bba_login] save cookie error: %s", save_exc)
                                    return "completed"
                                else:
                                    logger.warning(
                                        "[bba_login] %s stage1 PASSED but stage2 FAILED — "
                                        "NOT saving (防止覆盖旧有效文件), continue polling",
                                        platform,
                                    )
                    except Exception as page_exc:
                        # 页面还没加载完 / DOM 不可读 → 忽略，下轮再试
                        logger.debug("[bba_login] page check skip: %s", page_exc)

                # ── 方案 B: 浏览器已关闭 → probe fallback ────────────
                #    先 probe，后 save。probe 不过不保存 cookie，
                #    防止覆盖旧有效文件。
                if browser_closed:
                    logger.info(
                        "[bba_login] %s browser closed — skipping stage1, "
                        "running stage2 probe directly as fallback",
                        platform,
                    )
                    native_cookie, probe_ok = await _extract_and_probe_cookie(
                        ctx, platform, store, convert_storage_state,
                        captured_origin=_captured[0],
                    )
                    if probe_ok and native_cookie is not None:
                        try:
                            saved = await _save_login_cookie(
                                ctx, platform, store, label,
                                convert_storage_state,
                                captured_origin=_captured[0],
                                user_data_dir=user_data_dir_path,
                                native_cookie=native_cookie,
                            )
                            saved_label = saved.label
                        except Exception as save_exc:
                            logger.warning("[bba_login] save cookie error: %s", save_exc)
                    # 用 HTTP probe 最终确认
                    if saved_label and _probe_cookie(saved_label):
                        logger.info("[bba_login] %s fallback probe passed after close — completed", platform)
                        return "completed"
                    logger.info("[bba_login] %s browser closed, probe not passed — cancelled", platform)
                    return "cancelled"

            except Exception as exc:
                if _is_target_closed(exc):
                    # 浏览器崩溃/被杀 → fallback HTTP probe
                    if saved_label and _probe_cookie(saved_label):
                        logger.info("[bba_login] %s fallback probe passed after close — completed", platform)
                        return "completed"
                    logger.info("[bba_login] %s browser closed, probe not passed — cancelled", platform)
                    return "cancelled"
                logger.warning("[bba_login] poll error: %s", exc)

        # Timeout
        logger.warning("[bba_login] timed out after %ds", timeout)
        return "timeout"

    finally:
        # ── 关闭 + 等 cookie 落盘（2026-06-04 R7 P6 第二轮改造）──
        #
        # 历史：旧逻辑是"close 前 sleep 30s"——猜的，没信号
        # 验证刷盘是否完成。
        #
        # 现在：先 close（发 graceful shutdown 信号），再
        # watch SQLite 落盘稳定。原理：chrome 关闭时会调
        # CookieMonster::FlushStore 同步刷盘，刷完后
        #   - Default/Network/Cookies mtime 不再变化
        #   - Default/Network/Cookies-journal（WAL）合并清空
        # 这两个状态联合达成 = chrome 已退出且数据落盘。
        #
        # 上限 30s 硬兜底，由 ENV CRAWLHUB_LOGIN_FLUSH_SLEEP
        # 可调（兼容旧 ENV 名）。日常情况 1-3s 就 return，
        # 用户感知近乎瞬间关闭。
        _flush_env_raw = os.environ.get("CRAWLHUB_LOGIN_FLUSH_SLEEP")
        try:
            _flush_max = float((_flush_env_raw or "30").strip() or "30")
        except ValueError:
            _flush_max = 30.0

        logger.info(
            "[bba_login] finally: flush_max=%.1fs env_raw=%r "
            "(strategy: close → watch SQLite stable)",
            _flush_max,
            _flush_env_raw,
        )

        # 先发关闭信号，让 chrome 进入 graceful shutdown
        if session is not None:
            try:
                await session.close()
            except Exception as exc:
                logger.debug(
                    "[bba_login] session close swallow: %s", exc,
                )

        # 等 cookie SQLite 落盘稳定（上限 _flush_max）
        if _flush_max > 0:
            elapsed = await _wait_chrome_cookies_flushed(
                user_data_dir_path,
                max_wait=_flush_max,
                stable_for=2.0,
                poll_interval=0.5,
            )
            logger.info(
                "[bba_login] chrome cookies flushed in %.1fs (max %.1fs)",
                elapsed,
                _flush_max,
            )

        # ── 绑定 profile_dir 到 cookie 文件（flush 完成后）────────
        #
        # 时序铁律：必须在 _wait_chrome_cookies_flushed 之后执行。
        # 见 _bind_profile_dir_after_flush 的 docstring。
        #
        # saved_label 的来源：BBA 轮询里第一次成功 _save_login_cookie
        # 时设置；如果本次 session 用户中途关闭浏览器或超时未登录，
        # saved_label 为 None，下面跳过——此时 cookie 文件根本就没
        # 写出，绑了也没意义。
        if saved_label:
            try:
                _saved_cookie_path = store.get_cookie_path(platform, saved_label)
                _bind_profile_dir_after_flush(
                    platform,
                    Path(_saved_cookie_path),
                    user_data_dir_path,
                )
            except Exception as exc:
                logger.warning(
                    "[bba_login] post-flush profile_dir bind dispatch failed: %s",
                    exc,
                )

        # 给 Chrome 进程多 1s 释放文件锁（Windows SharedDB /
        # IndexedDB 锁残留），避免 daemon 紧接着拉起新浏览器撞锁
        await asyncio.sleep(1.0)


# ── 内部辅助 ─────────────────────────────────────────────────────


async def _bring_bba_to_front(page: Any, platform: str) -> None:
    """Retitle + bring the BBA login window to the foreground."""
    # 给窗口打标（与旧 _focus_login_window 同理）
    title = f"CrawlHub Login {platform} {_uuid.uuid4().hex[:12]}"
    try:
        await page.evaluate("t => { document.title = t; }", title)
    except Exception:
        pass
    try:
        await page.bring_to_front()
    except Exception:
        pass
    # OS-level 前置（Windows 用 ctypes，macOS 用 applescript）
    try:
        import platform as _pf
        _os = _pf.system()
        if _os == "Windows":
            _win32_bring_to_front(title)
        elif _os == "Darwin":
            _macos_bring_to_front(title)
    except Exception:
        pass


def _win32_bring_to_front(title_hint: str) -> None:
    """Windows: 用 EnumWindows 找窗口 + SetForegroundWindow 前置."""
    try:
        import ctypes
        user32 = ctypes.windll.user32
        EnumWindows = user32.EnumWindows
        EnumWindowsProc = ctypes.WINFUNCTYPE(
            ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p,
        )
        GetWindowText = user32.GetWindowTextW
        GetWindowTextLength = user32.GetWindowTextLengthW
        IsWindowVisible = user32.IsWindowVisible
        SetForegroundWindow = user32.SetForegroundWindow
        ShowWindow = user32.ShowWindow
        SW_RESTORE = 9

        target_hwnd = [None]  # 用 list 避免闭包 nonlocal 问题

        def _cb(hwnd, _):
            if not IsWindowVisible(hwnd):
                return True
            length = GetWindowTextLength(hwnd)
            if length <= 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            GetWindowText(hwnd, buf, length + 1)
            if title_hint in buf.value:
                target_hwnd[0] = hwnd
                return False
            return True

        EnumWindows(EnumWindowsProc(_cb), 0)
        if target_hwnd[0] is not None:
            ShowWindow(target_hwnd[0], SW_RESTORE)
            SetForegroundWindow(target_hwnd[0])
    except Exception:
        pass


def _macos_bring_to_front(title_hint: str) -> None:
    """macOS: 用 applescript 激活窗口."""
    try:
        import subprocess
        subprocess.run(
            ["osascript", "-e",
             f'tell application "System Events" to set frontmost of '
             f'(every process whose windows contains title contains '
             f'"{title_hint}") to true'],
            capture_output=True, timeout=3,
        )
    except Exception:
        pass


async def _async_sleep_or_close(
    page: Any, ctx: Any, seconds: float, tick: float = 0.3,
) -> bool:
    """Sleep up to `seconds`; return True if browser closed early.

    Checks page.is_closed() and browser connectivity each tick.
    """
    deadline = _time_mod.time() + seconds
    while _time_mod.time() < deadline:
        try:
            if page.is_closed():
                return True
        except Exception:
            return True
        try:
            # context.pages 在 close 后抛异常
            _ = ctx.pages
        except Exception:
            return True
        await asyncio.sleep(min(tick, max(deadline - _time_mod.time(), 0)))
    return False


def _is_target_closed(exc: Exception) -> bool:
    """Heuristic: did the browser close (TargetClosedError / similar)?"""
    name = type(exc).__name__.lower()
    msg = str(exc).lower()
    return "target" in name or "closed" in msg or "disconnected" in msg


async def _extract_and_probe_cookie(
    ctx: Any,
    platform: str,
    store: Any,
    convert_fn: Any,
    captured_origin: dict[str, str] | None = None,
) -> tuple[dict[str, Any] | None, bool]:
    """Extract cookies from browser context and probe WITHOUT saving.

    Writes native cookie to a temp file, probes via platform service,
    then deletes the temp file.  Returns ``(native_cookie, is_valid)``.
    ``native_cookie`` is ``None`` when extraction fails (e.g. browser
    already closed).

    Args:
        captured_origin: BBA 实抓的 wire-format identity headers
            （``_persist_origin`` callback 写入的 ``_captured[0]``）。
            非空时会以 ``metadata`` block 写入 tmp cookie 文件——这是
            快手 cffi probe（``http_factory.make_session``）的硬要求：
            tmp 文件被加载为 KuaishouCookieJar 时若 metadata 缺失，
            ``has_origin_headers`` 返回 False → 直接 raise
            ``CookieMetadataMissing``，probe 永远过不了。

    实现注意（2026-06-05 R7 P6 闪烁修复）：
        本函数被 BBA polling 每秒调用一次（登录成功后），曾经用
        ``ctx.storage_state()`` 拿 cookie。但 storage_state 在 patchright
        的 persistent context 下会枚举所有 origin 的 localStorage /
        IndexedDB / Service Worker 元数据，每秒一次会导致目标页面
        可见闪烁（用户实测 bilibili 主页登录后每 1s 闪一次）。

        ``ctx.cookies()`` 走纯 CDP ``Network.getCookies``，完全不接触
        页面渲染层 / 主世界，无任何副作用。我们包成 storage_state
        形状（``origins=[]``）喂给 ``convert_fn``——converters 只关心
        cookie 列表，origins 字段未使用，behavior 等价。

        localStorage / IndexedDB 不需要在此处 dump：BBA profile 已经
        通过 ``user_data_dir`` 持久化到磁盘，下次 daemon 拉起浏览器
        直接 load 同一目录即可恢复。
    """
    try:
        cookies_list = await ctx.cookies()
    except Exception as exc:
        logger.warning("[bba_login] ctx.cookies() failed: %s", exc)
        return None, False

    storage_state = {"cookies": cookies_list, "origins": []}
    native_cookie = convert_fn(platform, storage_state)

    # ── Origin metadata 注入 tmp 文件 ───────────────────────────
    # 历史血泪（2026-06-05）：老版 probe 回退（POST /rest/v/feed/liked
    # via curl_cffi）后才暴露——cffi probe 走 http_factory.make_session，
    # 它读 KuaishouCookieJar.has_origin_headers()，没 metadata 直接抛
    # CookieMetadataMissing。stage1 PASSED → stage2 FAILED 的死循环
    # 根因就在 tmp 文件没带 metadata。
    #
    # 实现：把 _captured[0]（wire-format dict, e.g. {"user-agent": ...}）
    # 按 KuaishouCookieJar.update_origin_headers 的同款规则映射成
    # ``origin_*`` 字段，连同 source/captured_at 写入 metadata block。
    # 其他平台（FileCookieJar 系）会通过 _RESERVED_KEYS 过滤掉
    # metadata，不会污染 cookie 数据，所以无平台路由也安全。
    payload_to_write: dict[str, Any] = native_cookie
    if captured_origin and isinstance(native_cookie, dict):
        metadata_block: dict[str, Any] = {}
        for wire_key, value in captured_origin.items():
            if not value:
                continue
            field = "origin_" + wire_key.lower().replace("-", "_")
            metadata_block[field] = str(value)
        if metadata_block:
            metadata_block["origin_source"] = "captured"
            metadata_block["origin_captured_at"] = _time_mod.time()
            payload_to_write = {**native_cookie, "metadata": metadata_block}

    # Write to temp file for probing
    platform_dir = store._root / platform
    platform_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = platform_dir / "_bba_probe_tmp.json"

    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload_to_write, f, ensure_ascii=False, indent=2)

        is_valid = False
        try:
            from crawlhub.core.registry import create_platform_service
            from crawlhub.core.cookie_override import (
                set_thread_cookie_override,
                clear_thread_cookie_override,
            )
            set_thread_cookie_override(str(tmp_path))
            try:
                svc = create_platform_service(platform)
                if svc is None:
                    # No service → degrade to pass (can't validate)
                    is_valid = True
                    logger.debug(
                        "[bba_login] %s no probe service, degrading to pass",
                        platform,
                    )
                else:
                    result = svc.check_cookie()
                    is_valid = result.status == "valid"
                    if is_valid:
                        logger.info(
                            "[bba_login] %s stage2 probe PASSED "
                            "(independent HTTP probe via cffi)",
                            platform,
                        )
                    else:
                        logger.warning(
                            "[bba_login] %s stage2 probe FAILED: "
                            "status=%s message=%s "
                            "(page DOM said logged-in but cffi HTTP probe "
                            "rejected — cookie/identity mismatch?)",
                            platform,
                            result.status,
                            getattr(result, "message", ""),
                        )
            finally:
                clear_thread_cookie_override()
        except Exception as exc:
            logger.warning("[bba_login] %s pre-save probe error: %s", platform, exc)
            is_valid = False

        return native_cookie, is_valid
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass


async def _save_login_cookie(
    ctx: Any,
    platform: str,
    store: Any,
    label: str | None,
    convert_fn: Any,
    captured_origin: dict[str, str] | None = None,
    user_data_dir: Path | None = None,
    native_cookie: dict[str, Any] | None = None,
) -> Any:
    """Dump context.storage_state → convert → save_cookie + reset probe.

    Args:
        captured_origin: BBA 实抓的 wire identity headers（来自 origin
            capture callback）。``save_cookie`` 会覆盖整个文件，所以
            保存后需要把 metadata 重新写入 cookie jar。传 None 则跳过。
        user_data_dir: 当前 BBA session 用的 user_data_dir 绝对路径。
            R7 P6 起把它的相对路径写入 cookie metadata.profile_dir，
            让后续 daemon 数据抓取直接打开同一个 profile，不再依赖
            目录 rename。传 None 则跳过 metadata 绑定（旧行为）。
        native_cookie: If provided, skip ``ctx.storage_state()`` extraction
            and use this pre-computed native cookie dict directly.  Used
            when the caller has already extracted + probed the cookie via
            ``_extract_and_probe_cookie()``.

    Returns the CookieInfo of the saved cookie.
    """
    if native_cookie is None:
        storage_state = await ctx.storage_state()
        native_cookie = convert_fn(platform, storage_state)
    saved = store.save_cookie(platform, native_cookie, label=label)
    logger.info(
        "[bba_login] %s cookie saved (label=%s, %d cookies)",
        platform, saved.label, saved.cookie_count,
    )

    # ── 重新写入 origin metadata + 绑定 profile_dir ──────────────
    #    save_cookie 会 json.dump 覆盖整个文件，把 _persist_origin
    #    之前写入的 metadata 冲掉。每次 save 后重新写入即可。
    #    R7 P6：同一时机把 user_data_dir 相对路径写进 metadata，
    #    后续 daemon 通过 _resolve_user_data_dir 直接读它，不再
    #    依赖 rename 目录。
    # ── Origin headers metadata（platform-specific, fast）──────────
    #    与 profile_dir 绑定不同：origin headers 不依赖浏览器 flush，
    #    在轮询期间立即写盘没问题（kuaishou 专用 schema）。
    #
    # ⚠️ profile_dir 绑定**不在这里**写。它需要等浏览器 SQLite 落盘
    # 完成才能确保"绑的目录真的有 cookies"——见 BBA finally 块的
    # ``_bind_profile_dir_after_flush()`` 调用。
    if platform == "kuaishou" and captured_origin:
        try:
            from crawlhub.crawlers.kuaishou.crawler._internal.cookie_jar import (
                KuaishouCookieJar,
            )
            jar = KuaishouCookieJar(str(saved.path))
            jar.update_origin_headers(captured_origin, source="captured")
            jar.save()
            logger.debug(
                "[bba_login] kuaishou origin headers re-applied",
            )
        except Exception as exc:
            logger.warning(
                "[bba_login] kuaishou origin re-apply failed: %s", exc,
            )

    # Reset probe status to green — 避免登录完还是红色
    try:
        from crawlhub.core.config import get_data_root
        from crawlhub.core.sqlite_store import SqliteStateStore
        db_path = get_data_root() / "crawlhub.db"
        SqliteStateStore(db_path).reset_probe_status(platform, saved.label)
    except Exception as exc:
        logger.warning("[bba_login] probe reset failed: %s", exc)
    return saved


def _profile_dir_to_relative(p: Path) -> str:
    """Convert an absolute user_data_dir path to a data_root-relative POSIX string.

    Returns empty string if the path is outside data_root（异常场景，
    例如用户改了 CRAWLHUB_DATA_ROOT 但 cookie 还指向老路径），
    调用方应跳过绑定保持向后兼容。
    """
    try:
        root = get_data_root().resolve()
        candidate = p.resolve()
        rel = candidate.relative_to(root)
        return rel.as_posix()
    except (ValueError, OSError):
        return ""


def _write_profile_dir_metadata(cookie_path: Path, profile_dir_rel: str) -> None:
    """Read-modify-write ``metadata.profile_dir`` into a flat-dict cookie file.

    Used for non-kuaishou platforms whose cookie file is a flat
    ``{name: value}`` JSON dict.  Embeds a ``metadata`` sub-dict alongside
    the cookies so ``_resolve_user_data_dir`` can rediscover the bound
    user_data_dir on subsequent BBA opens.

    The ``metadata`` key is filtered out by ``FileCookieJar.as_dict`` (see
    its ``_RESERVED_KEYS``) so it never gets injected into HTTP requests as
    a fake cookie.

    Args:
        cookie_path: Absolute path to the cookie JSON file.
        profile_dir_rel: data_root-relative POSIX path of the user_data_dir.

    Raises:
        OSError, json.JSONDecodeError on read/write failure (caller catches).
    """
    raw = json.loads(cookie_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        # List-shape cookie file (browser export) — wrap into dict form so
        # we can attach metadata.  Cookies live under ``__cookies`` to keep
        # the original shape recoverable; but in practice save_cookie always
        # writes flat dict, so this branch is paranoia-only.
        raw = {"__cookies": raw}

    meta = raw.get("metadata")
    if not isinstance(meta, dict):
        meta = {}
    meta["profile_dir"] = profile_dir_rel
    raw["metadata"] = meta

    tmp = cookie_path.with_suffix(cookie_path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(raw, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(cookie_path)


def _bind_profile_dir_after_flush(
    platform: str,
    cookie_path: Path,
    user_data_dir: Path,
) -> None:
    """Bind ``metadata.profile_dir`` to ``user_data_dir`` AFTER chrome flush.

    ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
     时序契约（R7 P6 第二轮，2026-06-05）—— 修 "更新" 空 profile bug
    ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    问题（事故）：
      第一轮 R7 P6 把 metadata.profile_dir 写在 ``_save_login_cookie``
      里（轮询期间，浏览器还活着）。但 chrome 的 CookieMonster
      是异步批写——此时 ``user_data_dir/Default/Network/Cookies``
      SQLite 里很可能**还没有 SESSDATA**。

      JSON 落盘时 metadata.profile_dir 已经指向那个 dir，但 dir 本身
      要等 BBA finally 块的 ``_wait_chrome_cookies_flushed`` 完成
      才有 cookies。如果 daemon / "更新" 按钮在窗口期内打开这个
      profile，看到的是空的——表现就是用户那条"更新打开浏览器
      没 SESSDATA"的 bug。

    修复（本函数）：
      把 metadata.profile_dir 的写入从 ``_save_login_cookie`` 移出，
      改成 BBA finally 块里 ``_wait_chrome_cookies_flushed`` 之后
      再调用——这样写盘时 dir 已经是 "guaranteed has cookies" 状态。

    幂等：函数被设计成可以**反复**调用（finally 重入也无副作用）。
    路径不在 data_root 内：跳过（不报错）。
    cookie 文件不存在：跳过（登录失败的正常路径）。

    Args:
        platform: 平台名，用于决定 cookie 文件 schema：
            - ``kuaishou``: 双站 ``{"main":..., "live":..., "metadata":...}``
              schema，用 ``KuaishouCookieJar.set_profile_dir + save``。
            - 其他: 扁平 dict ``{cookie_name: value, "metadata": {...}}``
              schema，用 ``_write_profile_dir_metadata``。
        cookie_path: cookie 文件绝对路径。
        user_data_dir: BBA session 真实使用的 user_data_dir 绝对路径。
    """
    if not cookie_path.exists():
        logger.debug(
            "[bba_login] %s skip profile_dir bind: cookie file missing (%s)",
            platform, cookie_path,
        )
        return
    rel = _profile_dir_to_relative(user_data_dir)
    if not rel:
        logger.warning(
            "[bba_login] %s skip profile_dir bind: %s outside data_root",
            platform, user_data_dir,
        )
        return
    try:
        if platform == "kuaishou":
            from crawlhub.crawlers.kuaishou.crawler._internal.cookie_jar import (
                KuaishouCookieJar,
            )
            jar = KuaishouCookieJar(str(cookie_path))
            jar.set_profile_dir(rel)
            jar.save()
        else:
            _write_profile_dir_metadata(cookie_path, rel)
        logger.info(
            "[bba_login] %s metadata.profile_dir bound to %s (after flush)",
            platform, rel,
        )
    except Exception as exc:
        logger.error(
            "[bba_login] %s profile_dir bind failed: %s", platform, exc,
            exc_info=True,
        )


# uuid import for window title stamping
import uuid as _uuid
from pathlib import Path as _Path_mod


# ════════════════════════════════════════════════════════════════════
#  内部工具
# ════════════════════════════════════════════════════════════════════


def _resolve_user_data_dir(session_key: SessionKey) -> Path:
    """Per-cookie persistent user data dir.

    每个 cookie 独立一个 user_data_dir，避免多 cookie 共用一个浏览器
    profile 时身份串味。

    解析优先级（R7 P6, 2026-06-03）：
      1. cookie 文件的 ``metadata.profile_dir`` 显式绑定 —— 由 BBA
         登录 session 在第一次保存 cookie 时写入。如果存在且能解析
         成合法路径就直接用，**不创建新目录覆盖**。
      2. Fallback：按 ``<data_root>/browser_profiles/<platform>/
         <cookie_id_safe>/`` 计算 —— 旧逻辑，兼容未升级的旧 cookie
         以及未登录的临时 SessionKey。

    为什么 metadata 旁路是首选：
      Windows 上把 ``_new_xxx`` 目录 ``Path.rename`` 成最终 label
      命名的目录有偶发 ``WinError 2`` 失败 —— Chrome 关闭后文件锁
      释放有竞争。一旦 rename 失败，daemon 后续打开的就是空 profile
      → IndexedDB / Service Worker / Chrome stable_token 全丢
      → kuaishou 服务端判异常 → 服务器主动作废 passToken
      → 一边 5s 轮询保存 cookie 一边把好 cookie 覆盖成废的。
      把绑定写进 cookie metadata 后，登录目录原地不动，daemon 直接
      去 ``_new_xxx`` 找，整条链路天然对齐。
    """
    # ── 优先级 1：读 cookie metadata.profile_dir ──────────────
    if session_key.cookie_path:
        cookie_p = Path(session_key.cookie_path)
        if cookie_p.is_file():
            try:
                raw = json.loads(cookie_p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                raw = None
            if isinstance(raw, dict):
                meta = raw.get("metadata")
                if isinstance(meta, dict):
                    pd = meta.get("profile_dir")
                    if isinstance(pd, str) and pd.strip():
                        candidate = Path(pd)
                        if not candidate.is_absolute():
                            candidate = get_data_root() / candidate
                        try:
                            candidate.mkdir(parents=True, exist_ok=True)
                            return candidate
                        except OSError as exc:
                            # 路径非法（比如包含非法字符 / 权限不足）
                            # → 退回 fallback，不让 daemon 起不来
                            logger.warning(
                                "_resolve_user_data_dir: metadata.profile_dir "
                                "%r unusable (%s), falling back to cookie_id",
                                pd, exc,
                            )

    # ── 优先级 2：cookie_id 推算（fallback / 旧 cookie / 无 cookie）──
    safe_cookie_id = (session_key.cookie_id or "default").replace(":", "_").replace("/", "_")
    base = get_data_root() / "browser_profiles" / session_key.platform / safe_cookie_id
    base.mkdir(parents=True, exist_ok=True)
    return base


def _load_storage_state_if_present(session_key: SessionKey) -> dict[str, Any] | None:
    if not session_key.cookie_path:
        return None
    path = Path(session_key.cookie_path)
    if not path.is_file():
        return None
    return load_storage_state(session_key.platform, path)


def _home_url(platform: str) -> str:
    if platform == "douyin":
        return "https://www.douyin.com"
    if platform == "kuaishou":
        return "https://www.kuaishou.com/"
    return "about:blank"


def _platform_host_suffix_for(platform: str) -> str:
    """主域后缀，用于 origin headers capture 的 host 过滤.

    返回小写字符串，调用方用 ``host.endswith(...)`` 匹配——之所以用后缀
    而非精确域，是为了在 BBA 跳到 live.kuaishou.com / live.douyin.com 等
    子域时也能命中（同一站点统一身份头）。

    未知 platform 返回 ``""``——使所有 endswith 检查失败，等同于禁用
    capture（这是安全的 fallback：无关域名不会被误抓）。
    """
    if platform == "douyin":
        return "douyin.com"
    if platform == "kuaishou":
        return "kuaishou.com"
    return ""



async def _safe_close(obj: Any) -> None:
    close = getattr(obj, "close", None)
    if close is None:
        return
    try:
        result = close()
        if hasattr(result, "__await__"):
            await result
    except Exception as exc:
        # close 失败不致命，但要让人看见
        logger.debug("safe_close swallow: %s", exc)


async def _wait_chrome_cookies_flushed(
    user_data_dir: Path,
    *,
    max_wait: float = 30.0,
    stable_for: float = 2.0,
    poll_interval: float = 0.5,
) -> float:
    """等 Chrome cookie SQLite 落盘稳定（chrome 已优雅退出的等价信号）。

    原理：chrome graceful shutdown 时 CookieMonster::FlushStore 会
    同步把内存 cookie 写入 SQLite。完成后：
      - Default/Network/Cookies (主 DB) 的 mtime 不再变化
      - Default/Network/Cookies-journal (WAL) 合并清空 / 不存在

    联合达成 = chrome 退出且 cookie 落盘完成。

    返回实际等待秒数。达到 max_wait 仍未稳定时返回 max_wait（兜底）。
    """
    cookies_db_modern = user_data_dir / "Default" / "Network" / "Cookies"
    cookies_db_legacy = user_data_dir / "Default" / "Cookies"
    journal_modern = user_data_dir / "Default" / "Network" / "Cookies-journal"
    journal_legacy = user_data_dir / "Default" / "Cookies-journal"

    started = _time_mod.monotonic()
    deadline = started + max_wait
    last_mtime = -1.0
    last_change = started

    def _stat_mtime(p: Path) -> float:
        try:
            return p.stat().st_mtime if p.is_file() else -1.0
        except OSError:
            return -1.0

    def _journal_clean(p1: Path, p2: Path) -> bool:
        for p in (p1, p2):
            try:
                if p.is_file() and p.stat().st_size > 0:
                    return False
            except OSError:
                pass
        return True

    while True:
        now = _time_mod.monotonic()
        if now >= deadline:
            return max_wait  # 兜底

        # 任一路径存在的 cookie db mtime
        cur_mtime = max(
            _stat_mtime(cookies_db_modern),
            _stat_mtime(cookies_db_legacy),
        )
        if cur_mtime != last_mtime:
            last_mtime = cur_mtime
            last_change = now

        if (
            last_mtime > 0
            and (now - last_change) >= stable_for
            and _journal_clean(journal_modern, journal_legacy)
        ):
            return now - started

        await asyncio.sleep(poll_interval)
