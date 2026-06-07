"""Host environment probe — Chrome version / OS version / stealth template params.

设计目标：
    crawlhub 在启动 BBA 浏览器之前，先探测宿主机器真实环境，得到一份
    可注入到 stealth_override.js 的"目标伪装值"。

为什么需要这层：
    stealth 不能写死。同一份 crawlhub 代码在 Win10 / Win11 / Mac / Linux
    上跑，要伪装成跟宿主**真实环境一致**的浏览器，否则 navigator.userAgent
    里声明的 OS 跟 navigator.platform 报的 OS 矛盾 → 反爬一秒识破。

跨平台支持：
    - Windows 10/11：从注册表读 build number → 转 platformVersion
    - macOS：从 platform.mac_ver() 读真实版本
    - Linux：暂只透传 OS 名，不强行伪装 Mac/Win（headless 服务器场景）

输出结构（HostInfo）：
    {
        "os": "Windows" | "macOS" | "Linux",
        "os_version": "11" | "10.15.7" | "Ubuntu 22.04",
        "platform_version_hint": "19.0.0",     # 给 stealth 用的 platformVersion
        "chrome_major": "148",
        "chrome_full": "148.0.7778.181",
        "ua": "Mozilla/5.0 ...",
        "should_patch_platform_version": True,   # 是否需要在 stealth 里改写 PV
    }

无副作用、不抛异常：探测失败一律走 fallback，保证 crawlhub launch 不被阻塞。
"""
from __future__ import annotations

import logging
import os
import platform
import re
from dataclasses import dataclass, field, asdict
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# 全局指纹常量：HTTP Accept-Language 头与 navigator.languages 严格一致
# ─────────────────────────────────────────────────────────────────────────────
# 单独设 launch(locale="zh-CN") 会让 Chrome 把出向 Accept-Language 退化成
# 裸字符串 "zh-CN"，但 stealth_override.js 注入的 navigator.languages 是
# ['zh-CN','zh','en-US','en'] 4 项 —— 两端不一致就是反爬指纹的天然分类器。
#
# 解法：所有 crawlhub 主动发起的 HTTP/WSS 请求都使用同一份带 q-value 衰减
#   的真人格式，与 stealth JS 严格对应。业务代码（douyin/kuaishou）应当
#   import 这个常量，而非各自硬编码 ——
#       playwright_runtime.py：浏览器 extra_http_headers 基线
#       douyin/live_protocol.py：search_live 重放 + webcast WSS 握手
#       kuaishou/live_protocol.py：弹幕 WSS 握手
#
# ENV override：CRAWLHUB_BBA_ACCEPT_LANGUAGE（不同地区/A/B 测试可调）。
REAL_ACCEPT_LANGUAGE: str = os.environ.get(
    "CRAWLHUB_BBA_ACCEPT_LANGUAGE",
    "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
)


@dataclass
class HostInfo:
    """跨平台宿主环境快照，用于驱动 stealth 自适应。"""
    os: str = "Unknown"                    # Windows / macOS / Linux / Unknown
    os_version: str = ""                   # 人类可读：Windows 11 / 10.15.7
    platform_version_hint: str = "10.0"    # 注入给 navigator.userAgentData
    should_patch_platform_version: bool = False
    chrome_major: str = "148"
    chrome_full: str = "148.0.7778.181"
    ua: str = ""
    # ── 屏幕分辨率（2026-06-01 新增，修复 viewport 写死 1920x1080 leak）──
    # 探测到的物理像素尺寸；当宿主真实分辨率 ≠ 1920x1080 时，必须把 patchright
    # 的 viewport 同步成宿主真值，否则 navigator.screen.width/height 会泄露。
    screen_width: int = 1920
    screen_height: int = 1080

    def to_dict(self) -> dict:
        return asdict(self)


# ════════════════════════════════════════════════════════════════════
#  Windows
# ────────────────────────────────────────────────────────────────────
#  Win10 build < 22000 → platformVersion 应报 "1.0.0" / "10.0.0" 这类（实测
#  Chrome 在 Win10 上报 "10.0"）
#  Win11 build >= 22000 → platformVersion 应报形如 "15.0.0" / "19.0.0" /
#  "26.0.0" 等，规则是 floor((build - 22000) / 1000) * 1.0 + ... 实际上
#  Chrome 内部是从 Windows API 拿 OS major/minor/build，再做映射。
#
#  我们简化处理（足够 stealth 用）：
#    Win10 → "10.0"          → should_patch=False（patchright 默认就报 10.0）
#    Win11 → "15.0.0"+       → should_patch=True，按 build 算
# ════════════════════════════════════════════════════════════════════


def _detect_windows_version() -> tuple[str, str, bool]:
    """Returns (os_version_human, platform_version_hint, should_patch).

    通过 platform.win32_ver() / sys.getwindowsversion() / 注册表三层取信息。
    """
    try:
        # 方法 1：sys.getwindowsversion 拿 build number（最准）
        import sys
        wv = sys.getwindowsversion()  # type: ignore[attr-defined]
        major = wv.major
        build = wv.build
        if major == 10 and build >= 22000:
            # Win11
            # Chrome 的 platformVersion 映射规则（参考 Chromium 源码 base/win/
            # windows_version.cc 里的 GetVersionNumber）：
            #   Win11 21H2 (22000)  → "15.0.0"
            #   Win11 22H2 (22621)  → "15.0.0"
            #   Win11 23H2 (22631)  → "15.0.0"
            #   Win11 24H2 (26100)  → "19.0.0"  ← 这是当前主流
            # 简化：build < 26000 → 15.0.0；>=26000 → 19.0.0
            pv = "19.0.0" if build >= 26000 else "15.0.0"
            return f"Windows 11 (build {build})", pv, True
        elif major == 10:
            # Win10
            return f"Windows 10 (build {build})", "10.0", False
        else:
            # Win7/8 等老系统：少见，按 Win10 处理
            return f"Windows {major}.{wv.minor}", "10.0", False
    except Exception as exc:
        logger.warning("[host_env] win version detect via sys failed: %s", exc)

    # 方法 2：fallback 用 platform.win32_ver()
    try:
        release, version, csd, ptype = platform.win32_ver()
        if release == "11" or version.startswith("10.0.22") or version.startswith("10.0.26"):
            return f"Windows 11 ({version})", "19.0.0", True
        return f"Windows {release} ({version})", "10.0", False
    except Exception:
        pass

    return "Windows (unknown)", "10.0", False


# ════════════════════════════════════════════════════════════════════
#  macOS
# ────────────────────────────────────────────────────────────────────
#  macOS 上 Chrome 的 navigator.userAgentData.platformVersion 报形如
#  "13.5.0"（macOS 13 Ventura）/"14.0.0"（Sonoma）。这是真实 macOS 版本，
#  patchright 默认会传透真实值，**不需要 patch**。
# ════════════════════════════════════════════════════════════════════


def _detect_macos_version() -> tuple[str, str, bool]:
    try:
        release, _, _ = platform.mac_ver()  # ('14.5', ('', '', ''), 'arm64')
        if release:
            # 转成 "14.5.0" 的形式
            parts = release.split(".")
            while len(parts) < 3:
                parts.append("0")
            pv = ".".join(parts[:3])
            return f"macOS {release}", pv, False
    except Exception:
        pass
    return "macOS (unknown)", "13.0.0", False


# ════════════════════════════════════════════════════════════════════
#  Linux
# ────────────────────────────────────────────────────────────────────
#  Linux 服务器上 Chrome 报的 navigator.platform 是 "Linux x86_64"，
#  userAgentData.platform 是 "Linux"。爬国内站点时，**反爬几乎肯定会**
#  对 "Linux" 平台特别警惕（因为真用户 99% 是 Win/Mac），所以生产服务器
#  跑 crawlhub 时，建议在更上层伪装成 Windows / Mac。
#
#  但伪装 OS 比伪装版本号难得多：UA 字符串、Sec-Ch-Ua-Platform、TLS 都要
#  改，超出 stealth_override.js 能管的范围。所以 Linux 环境下我们**只透传
#  真实信息**，不做伪装；如果你们生产真要在 Linux 上跑，需要单独的"OS 伪装"
#  方案（比如用 patchright 的 storage_state + 一台 Win 模拟器开发态）。
# ════════════════════════════════════════════════════════════════════


def _detect_linux_version() -> tuple[str, str, bool]:
    try:
        # 先试 distro 模块（如果装了）
        try:
            import distro  # type: ignore[import]
            return f"{distro.name()} {distro.version()}", "0.0.0", False
        except ImportError:
            pass
        # fallback 到 /etc/os-release
        os_release = Path("/etc/os-release")
        if os_release.exists():
            text = os_release.read_text()
            m_name = re.search(r'^NAME="?([^"\n]+)"?', text, re.M)
            m_ver = re.search(r'^VERSION_ID="?([^"\n]+)"?', text, re.M)
            name = m_name.group(1) if m_name else "Linux"
            ver = m_ver.group(1) if m_ver else ""
            return f"{name} {ver}".strip(), "0.0.0", False
    except Exception:
        pass
    return "Linux (unknown)", "0.0.0", False


# ════════════════════════════════════════════════════════════════════
#  屏幕分辨率检测（跨平台）
# ────────────────────────────────────────────────────────────────────
#  为什么需要：
#    患处在 viewport=1920x1080 写死。stealth_override.js 没 patch
#    screen.width/height，所以抖音 acrawler.js 在请求里读 navigator.screen
#    拿到的就是 1920x1080。如果宿主是 2K/4K 屏，请求里的 screen_width/
#    screen_height 就会跟"用户真机本应该是的尺寸"系统性偏差 —— 反爬一眼
#    识别"虚拟机/CI 容器/低配机"。
#
#  方案：
#    Windows → ctypes user32.GetSystemMetrics(0/1) 拿物理像素
#    macOS   → system_profiler 解析 Resolution
#    Linux   → xrandr / xdpyinfo（headless 服务器可能拿不到）
#    fallback → 1920x1080（原默认值）
#
#  注：
#    - 高 DPI 下 GetSystemMetrics 返回逻辑像素；先 SetProcessDPIAware()
#      切到物理像素模式再读
#    - 拿不到不抛错，silent fallback —— stealth 体系铁律：探测失败永远
#      不能阻塞 launch
# ════════════════════════════════════════════════════════════════════


def _detect_screen_size() -> tuple[int, int]:
    """Returns (width, height) in **logical pixels** that Chrome will report.

    ⚠️ 这里要的是"Chrome 看到的 navigator.screen.width/height"，不是物理像素。
       高 DPI 屏（4K + 150% 缩放）下：
         物理像素     = 3840 x 2160
         逻辑像素     = 2560 x 1440  ← Chrome navigator.screen 报这个
       两者相差一个 DPI 缩放因子。

    实现要点：
      Windows: 故意不调 SetProcessDPIAware()。让 Python 进程保持
               DPI-unaware 模式，此时 GetSystemMetrics(0/1) 由系统自动
               按缩放因子返回"虚拟逻辑分辨率"——刚好等于 Chrome 在
               DPI-per-monitor-v2 下报的 navigator.screen.* 值。
      macOS:   system_profiler 报的就是逻辑像素（Retina 已折算）。
      Linux:   xrandr current 返回的也是逻辑像素。
      fallback: 1920x1080（探测失败永不阻塞 launch）。
    """
    sysname = platform.system()

    # ── Windows ──
    if sysname == "Windows":
        try:
            import ctypes
            user32 = ctypes.windll.user32  # type: ignore[attr-defined]
            # ⚠️ 故意不调 SetProcessDPIAware：保持 DPI-unaware 模式让 GSM
            # 返回缩放后的逻辑分辨率（= Chrome 报告值）
            w = int(user32.GetSystemMetrics(0))  # SM_CXSCREEN（逻辑像素）
            h = int(user32.GetSystemMetrics(1))  # SM_CYSCREEN（逻辑像素）
            if w > 0 and h > 0:
                return w, h
        except Exception as exc:
            logger.warning("[host_env] win screen detect failed: %s", exc)

    # ── macOS ──
    elif sysname == "Darwin":
        # 优先用 AppKit 取逻辑像素（Retina 已自动折算），且
        # visibleFrame 已排除菜单栏和 Dock，避免窗口顶出屏幕。
        try:
            import AppKit
            screen = AppKit.NSScreen.mainScreen()
            if screen is not None:
                vf = screen.visibleFrame()
                w = int(vf.size.width)
                h = int(vf.size.height)
                if w > 0 and h > 0:
                    return w, h
        except Exception as exc:
            logger.warning("[host_env] mac screen detect (AppKit) failed: %s", exc)

        # fallback: system_profiler 取的是物理像素，必须除以
        # backingScaleFactor 才是逻辑像素；同时无法排除菜单栏/Dock。
        try:
            import subprocess
            out = subprocess.check_output(
                ["system_profiler", "SPDisplaysDataType"],
                timeout=5, encoding="utf-8", errors="ignore",
            )
            m = re.search(r"Resolution:\s+(\d+)\s*x\s*(\d+)", out)
            if m:
                physical_w = int(m.group(1))
                physical_h = int(m.group(2))
                # 尝试读取 backingScaleFactor；取不到则默认 2.0
                try:
                    scale = AppKit.NSScreen.mainScreen().backingScaleFactor()
                except Exception:
                    scale = 2.0
                w = max(1, int(physical_w / scale))
                h = max(1, int(physical_h / scale))
                return w, h
        except Exception as exc:
            logger.warning("[host_env] mac screen detect (fallback) failed: %s", exc)

    # ── Linux ──
    elif sysname == "Linux":
        try:
            import subprocess
            # xrandr 在有 X server 时可用；headless 服务器一般失败 → fallback
            out = subprocess.check_output(
                ["xrandr", "--current"], timeout=5,
                encoding="utf-8", errors="ignore",
            )
            m = re.search(r"current\s+(\d+)\s+x\s+(\d+)", out)
            if m:
                return int(m.group(1)), int(m.group(2))
        except Exception:
            # headless 服务器没 X，是预期路径，不告警
            pass

    return 1920, 1080


# ════════════════════════════════════════════════════════════════════
#  Chrome 版本检测（之前已有，这里抽过来集中管理）
# ════════════════════════════════════════════════════════════════════


def _detect_chrome_version() -> tuple[str, str]:
    """检测本机 Chrome 版本，返回 (major, full)。

    跨平台容错：
      - Windows: 注册表 + 文件系统枚举
      - macOS: 读 /Applications/Google Chrome.app/Contents/Info.plist
      - Linux: google-chrome --version
      - 兜底: ("148", "148.0.7778.181")
    """
    sysname = platform.system()

    # ── Windows ──
    if sysname == "Windows":
        try:
            import winreg  # type: ignore[import]
            candidates = [
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Google Chrome"),
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\Google Chrome"),
                (winreg.HKEY_CURRENT_USER, r"Software\Google\Chrome\BLBeacon"),
            ]
            for hive, regpath in candidates:
                try:
                    with winreg.OpenKey(hive, regpath) as key:
                        for value_name in ("DisplayVersion", "version"):
                            try:
                                v, _ = winreg.QueryValueEx(key, value_name)
                                m = str(v).split(".")[0]
                                if m.isdigit():
                                    return m, str(v)
                            except OSError:
                                continue
                except OSError:
                    continue
        except (ImportError, OSError):
            pass
        # 文件系统枚举
        for chrome_dir in [
            Path(r"C:\Program Files\Google\Chrome\Application"),
            Path(r"C:\Program Files (x86)\Google\Chrome\Application"),
        ]:
            if not chrome_dir.is_dir():
                continue
            try:
                for sub in chrome_dir.iterdir():
                    if sub.is_dir() and re.match(r"^\d+\.\d+\.\d+\.\d+$", sub.name):
                        return sub.name.split(".")[0], sub.name
            except OSError:
                continue

    # ── macOS ──
    elif sysname == "Darwin":
        plist = Path("/Applications/Google Chrome.app/Contents/Info.plist")
        if plist.exists():
            try:
                text = plist.read_text(encoding="utf-8", errors="ignore")
                m = re.search(r"<key>CFBundleShortVersionString</key>\s*<string>([^<]+)</string>", text)
                if m:
                    full = m.group(1)
                    return full.split(".")[0], full
            except Exception:
                pass
        # 也试一下 chrome --version
        try:
            import subprocess
            for chrome_bin in [
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                "/usr/bin/google-chrome",
            ]:
                if Path(chrome_bin).exists():
                    out = subprocess.check_output(
                        [chrome_bin, "--version"], timeout=5,
                        encoding="utf-8", errors="ignore",
                    )
                    m = re.search(r"(\d+)\.(\d+\.\d+\.\d+)", out)
                    if m:
                        return m.group(1), m.group(0)
        except Exception:
            pass

    # ── Linux ──
    elif sysname == "Linux":
        try:
            import subprocess
            for chrome_bin in ["google-chrome", "google-chrome-stable", "chromium-browser", "chromium"]:
                try:
                    out = subprocess.check_output(
                        [chrome_bin, "--version"], timeout=5,
                        encoding="utf-8", errors="ignore",
                    )
                    m = re.search(r"(\d+)\.(\d+\.\d+\.\d+)", out)
                    if m:
                        return m.group(1), m.group(0)
                except (FileNotFoundError, subprocess.SubprocessError):
                    continue
        except Exception:
            pass

    logger.warning("[host_env] could not detect Chrome version, using fallback 148")
    return "148", "148.0.7778.181"


# ════════════════════════════════════════════════════════════════════
#  主入口
# ════════════════════════════════════════════════════════════════════


@lru_cache(maxsize=1)
def detect_host_environment() -> HostInfo:
    """Detect host environment for stealth template rendering.

    返回 HostInfo dataclass。lru_cache 保证多次调用零成本。
    """
    sysname = platform.system()
    chrome_major, chrome_full = _detect_chrome_version()
    screen_w, screen_h = _detect_screen_size()

    if sysname == "Windows":
        os_version, pv, should_patch = _detect_windows_version()
        os_name = "Windows"
        # UA 模板：Win10/Win11 都用 "Windows NT 10.0"（Microsoft 故意保持兼容）
        ua = (
            f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            f"AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{chrome_major}.0.0.0 Safari/537.36"
        )
    elif sysname == "Darwin":
        os_version, pv, should_patch = _detect_macos_version()
        os_name = "macOS"
        # macOS UA: 用 "Macintosh; Intel Mac OS X 10_15_7"（Chrome 在 macOS
        # 11+ 也保持这个旧字符串以最大兼容）
        ua = (
            f"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            f"AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{chrome_major}.0.0.0 Safari/537.36"
        )
    elif sysname == "Linux":
        os_version, pv, should_patch = _detect_linux_version()
        os_name = "Linux"
        ua = (
            f"Mozilla/5.0 (X11; Linux x86_64) "
            f"AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{chrome_major}.0.0.0 Safari/537.36"
        )
    else:
        os_version, pv, should_patch = "Unknown", "10.0", False
        os_name = "Unknown"
        ua = (
            f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            f"AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{chrome_major}.0.0.0 Safari/537.36"
        )

    info = HostInfo(
        os=os_name,
        os_version=os_version,
        platform_version_hint=pv,
        should_patch_platform_version=should_patch,
        chrome_major=chrome_major,
        chrome_full=chrome_full,
        ua=ua,
        screen_width=screen_w,
        screen_height=screen_h,
    )
    logger.info(
        "[host_env] detected: os=%s ver=%s pv=%s patch=%s chrome=%s screen=%dx%d",
        info.os, info.os_version, info.platform_version_hint,
        info.should_patch_platform_version, info.chrome_full,
        info.screen_width, info.screen_height,
    )
    return info


# ════════════════════════════════════════════════════════════════════
#  Origin Headers 合成（E2 兜底路径）
# ────────────────────────────────────────────────────────────────────
#  设计目标（2026-06-02 R7 protect）：
#    cffi 第二阶段必须服从 BBA 第一阶段的真实身份。优先走 E1（实抓
#    page.on('request') wire headers），抓失败才兜底走 E2（合成）。
#
#  为什么只合成 4 件套（UA / sec-ch-ua×3 / Accept-Language）：
#    - UA + Client Hints：身份层，跨请求稳定，可放心合成
#    - Accept-Language：与 navigator.languages 严格对齐（参见
#      REAL_ACCEPT_LANGUAGE 注释）
#    - accept / accept-encoding：transport 层，与 ja3 / HTTP2 settings
#      指纹强绑定。交给 curl_cffi impersonate 自己生成，避免双方不一致
#      导致 inter-protocol fingerprint 自相矛盾
#
#  为什么 sec-ch-ua brand 顺序写死有风险：
#    Chrome 不同版本 GREASE brand 顺序会变（C137 vs C148 实测不同）。
#    E2 合成只是兜底，主路径必须 E1 实抓拿真实顺序。
# ════════════════════════════════════════════════════════════════════


def _platform_to_sec_ch_ua_platform(os_name: str) -> str:
    """Map HostInfo.os to sec-ch-ua-platform value (含外层引号)."""
    mapping = {
        "Windows": '"Windows"',
        "macOS":   '"macOS"',
        "Linux":   '"Linux"',
    }
    return mapping.get(os_name, '"Windows"')


def _synthesize_sec_ch_ua(chrome_major: str) -> str:
    """合成 sec-ch-ua header value（Chrome 138+ GREASE 格式）.

    实测 Chrome 148 wire 顺序：Chromium → Not.A/Brand → Google Chrome
    GREASE brand="Not.A/Brand" 是 Chrome 138+ 标准（旧版是 "Not/A)Brand"）。

    ⚠️ 顺序是反爬指纹分类器，但只能写一个固定顺序——这就是为什么
    生产必须优先走 E1 实抓而非依赖此函数。
    """
    m = chrome_major or "148"
    return (
        f'"Chromium";v="{m}", '
        f'"Not.A/Brand";v="24", '
        f'"Google Chrome";v="{m}"'
    )


def synthesize_origin_headers(host_info: HostInfo) -> dict[str, str]:
    """E2 兜底：根据宿主环境合成关键身份头.

    返回 4 类（key 全小写，与 Chrome wire 格式一致）：
      - user-agent          : 来自 HostInfo.ua（已 Reduced UA）
      - sec-ch-ua           : 合成的 brand 列表
      - sec-ch-ua-mobile    : 固定 "?0"（PC）
      - sec-ch-ua-platform  : 由 HostInfo.os 派生
      - accept-language     : REAL_ACCEPT_LANGUAGE 常量

    使用方（http_factory）应给 cookie_jar.metadata 标记 origin_source=
    "synthesized"，便于 R7 观测时识别 E1/E2 来源。
    """
    return {
        "user-agent": host_info.ua,
        "sec-ch-ua": _synthesize_sec_ch_ua(host_info.chrome_major),
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": _platform_to_sec_ch_ua_platform(host_info.os),
        "accept-language": REAL_ACCEPT_LANGUAGE,
    }


# ════════════════════════════════════════════════════════════════════
#  CLI（方便手动验证）
# ════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import json
    info = detect_host_environment()
    print("─── HostInfo ───")
    print(json.dumps(info.to_dict(), indent=2, ensure_ascii=False))
    print("\n─── Synthesized origin headers (E2 fallback) ───")
    print(json.dumps(synthesize_origin_headers(info), indent=2, ensure_ascii=False))
