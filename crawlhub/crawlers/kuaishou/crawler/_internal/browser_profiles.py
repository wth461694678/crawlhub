"""
浏览器指纹配置 + JSONL 请求日志系统
===================================
公共模块，供 ks_danmu_scraper.py 和 ks_cli.py 使用。

功能：
  1. BrowserProfile: 统一管理 Chrome 版本号、UA、sec-ch-ua 等指纹信息
  2. RequestLogger: JSONL 格式记录每次 HTTP 请求，用于反爬复盘分析

用法：
  from browser_profiles import BROWSER_PROFILES, pick_profile, RequestLogger
"""

import json
import os
import random
import time
from datetime import datetime
from pathlib import Path


# ======================== 浏览器指纹配置 ========================
# 每个 profile 包含一套完整的、内部一致的浏览器指纹信息
# 切换 profile 时所有相关字段一起变，避免版本号矛盾暴露爬虫身份

BROWSER_PROFILES = [
    {
        "name": "chrome136_mac",
        "impersonate": "chrome136",
        "chrome_version": "136",
        "chrome_full_version": "136.0.7103.93",
        "ua": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/136.0.7103.93 Safari/537.36"
        ),
        "sec_ch_ua": '"Chromium";v="136", "Google Chrome";v="136", "Not.A/Brand";v="99"',
        "sec_ch_ua_full_version_list": (
            '"Chromium";v="136.0.7103.93", "Google Chrome";v="136.0.7103.93", '
            '"Not.A/Brand";v="99.0.0.0"'
        ),
        "sec_ch_ua_platform": '"macOS"',
        "sec_ch_ua_mobile": "?0",
    },
    {
        "name": "chrome133_mac",
        "impersonate": "chrome133a",
        "chrome_version": "133",
        "chrome_full_version": "133.0.6943.126",
        "ua": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/133.0.6943.126 Safari/537.36"
        ),
        "sec_ch_ua": '"Chromium";v="133", "Google Chrome";v="133", "Not?A_Brand";v="24"',
        "sec_ch_ua_full_version_list": (
            '"Chromium";v="133.0.6943.126", "Google Chrome";v="133.0.6943.126", '
            '"Not?A_Brand";v="24.0.0.0"'
        ),
        "sec_ch_ua_platform": '"macOS"',
        "sec_ch_ua_mobile": "?0",
    },
    {
        "name": "chrome131_mac",
        "impersonate": "chrome131",
        "chrome_version": "131",
        "chrome_full_version": "131.0.6778.204",
        "ua": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.6778.204 Safari/537.36"
        ),
        "sec_ch_ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
        "sec_ch_ua_full_version_list": (
            '"Google Chrome";v="131.0.6778.204", "Chromium";v="131.0.6778.204", '
            '"Not_A Brand";v="24.0.0.0"'
        ),
        "sec_ch_ua_platform": '"macOS"',
        "sec_ch_ua_mobile": "?0",
    },
    {
        "name": "chrome136_win",
        "impersonate": "chrome136",
        "chrome_version": "136",
        "chrome_full_version": "136.0.7103.93",
        "ua": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/136.0.7103.93 Safari/537.36"
        ),
        "sec_ch_ua": '"Chromium";v="136", "Google Chrome";v="136", "Not.A/Brand";v="99"',
        "sec_ch_ua_full_version_list": (
            '"Chromium";v="136.0.7103.93", "Google Chrome";v="136.0.7103.93", '
            '"Not.A/Brand";v="99.0.0.0"'
        ),
        "sec_ch_ua_platform": '"Windows"',
        "sec_ch_ua_mobile": "?0",
    },
    {
        "name": "chrome133_win",
        "impersonate": "chrome133a",
        "chrome_version": "133",
        "chrome_full_version": "133.0.6943.126",
        "ua": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/133.0.6943.126 Safari/537.36"
        ),
        "sec_ch_ua": '"Chromium";v="133", "Google Chrome";v="133", "Not?A_Brand";v="24"',
        "sec_ch_ua_full_version_list": (
            '"Chromium";v="133.0.6943.126", "Google Chrome";v="133.0.6943.126", '
            '"Not?A_Brand";v="24.0.0.0"'
        ),
        "sec_ch_ua_platform": '"Windows"',
        "sec_ch_ua_mobile": "?0",
    },
]


def pick_profile(exclude_name: str = None) -> dict:
    """随机选择一个浏览器指纹 profile。
    
    Args:
        exclude_name: 排除指定名称的 profile（用于被 ban 后切换到不同指纹）
    Returns:
        一个 profile dict
    """
    candidates = [p for p in BROWSER_PROFILES if p["name"] != exclude_name]
    if not candidates:
        candidates = BROWSER_PROFILES  # fallback: 全部可选
    return random.choice(candidates)


# ======================== 请求延迟工具 ========================

def human_delay(min_sec: float = 1.0, max_sec: float = 3.0, label: str = ""):
    """模拟人类操作的随机延迟。
    
    Args:
        min_sec: 最小延迟秒数
        max_sec: 最大延迟秒数
        label: 可选的日志标签
    """
    delay = random.uniform(min_sec, max_sec)
    if label:
        print(f"[DELAY] {label}: {delay:.1f}s")
    time.sleep(delay)


def exponential_backoff(attempt: int, base: float = 3.0, max_delay: float = 30.0) -> float:
    """指数退避延迟。
    
    Args:
        attempt: 重试次数（从1开始）
        base: 基础延迟秒数
        max_delay: 最大延迟上限
    Returns:
        实际延迟的秒数
    """
    delay = min(base * (2 ** (attempt - 1)) + random.uniform(0, 2), max_delay)
    print(f"[BACKOFF] 等待 {delay:.1f}s (第 {attempt} 次重试)")
    time.sleep(delay)
    return delay


# ======================== JSONL 请求日志系统 ========================
#
# 2026-05-29 关闭：用户反馈 crawler_t<tid>_<ts>.jsonl 跟 data.jsonl 混在
# 同一个 ctx.output_dir 里干扰数据消费。这一套 HTTP 请求审计日志原本是
# 给逆向期排查 did 黑名单时用的，生产链路（hybrid + 浏览器签名）下基本
# 无价值，按用户要求默认关闭。
#
# 重新开启方式：把环境变量 KUAISHOU_REQUEST_LOG=1（或改本文件常量）。
# 关闭时 RequestLogger 是完全的 no-op（不开文件、不打 `[LOG]` 行、
# 所有方法跳过），调用方零侵入。
import os as _os
_REQUEST_LOG_ENABLED = _os.environ.get("KUAISHOU_REQUEST_LOG", "0").lower() in ("1", "true", "yes", "on")


class RequestLogger:
    """JSONL 格式的 HTTP 请求日志记录器。
    
    每次运行生成一个 .jsonl 文件，每行记录一次 HTTP 请求的完整信息。
    用于反爬复盘分析：请求密度、400002 触发时机、did 生命周期等。
    
    日志字段：
      ts           - ISO 时间戳（精确到毫秒）
      seq          - 本次运行的请求序号
      method       - HTTP 方法 (GET/POST)
      url          - 请求 URL（不含 query string）
      status_code  - HTTP 响应状态码
      result       - 业务层 result 字段值
      did          - 当前使用的 did
      profile      - 当前使用的浏览器指纹名称
      elapsed_ms   - 请求耗时（毫秒）
      since_last_ms- 距离上一次请求的间隔（毫秒）
      response_summary - 响应摘要
      anti_crawl   - 是否触发反爬标记
      cookies_sent - 发送的 cookie key 列表
      error        - 异常信息（如有）
    """
    
    def __init__(self, log_dir, prefix: str = "requests"):
        # log_dir is REQUIRED. Per the platform contract (R6), crawler code
        # MUST NOT pick its own write root via __file__-relative paths;
        # the caller (Service/Scraper) injects ctx.output_dir so all
        # request logs land under ~/.crawlhub/output/<date>/<task_id>_*/.
        #
        # 2026-05-29: enabled flag — when False the logger is a complete
        # no-op (no file open, no `[LOG]` print, no _write). This keeps the
        # constructor signature stable while letting callers disable the
        # per-task crawler_t<tid>_<ts>.jsonl spam without touching every
        # call site.
        self.enabled = bool(log_dir is not None and _REQUEST_LOG_ENABLED)
        self._seq = 0
        self._last_request_time = None
        self._did = ""
        self._profile_name = ""
        if not self.enabled:
            self.log_dir = None
            self.log_path = None
            return

        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)

        # 每次运行生成一个新的日志文件
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.log_path = self.log_dir / f"{prefix}_{ts}.jsonl"

        # 写入启动头信息
        self._write({
            "event": "session_start",
            "ts": datetime.now().isoformat(timespec="milliseconds"),
            "profile": self._profile_name,
            "did": self._did,
        })

        print(f"[LOG] 请求日志: {self.log_path}")
    
    def set_context(self, did: str = None, profile_name: str = None):
        """更新日志上下文（did 或 profile 变更时调用）。"""
        if did is not None:
            old = self._did
            self._did = did
            if old and old != did:
                self._write({
                    "event": "did_changed",
                    "ts": datetime.now().isoformat(timespec="milliseconds"),
                    "old_did": old,
                    "new_did": did,
                    "seq": self._seq,
                })
        if profile_name is not None:
            old = self._profile_name
            self._profile_name = profile_name
            if old and old != profile_name:
                self._write({
                    "event": "profile_changed",
                    "ts": datetime.now().isoformat(timespec="milliseconds"),
                    "old_profile": old,
                    "new_profile": profile_name,
                    "seq": self._seq,
                })
    
    def log_request(
        self,
        method: str,
        url: str,
        status_code: int = None,
        result: int = None,
        elapsed_ms: float = None,
        response_summary: str = "",
        anti_crawl: bool = False,
        cookies_sent: list = None,
        error: str = None,
    ):
        """记录一次 HTTP 请求。"""
        now = time.time()
        self._seq += 1
        
        since_last_ms = None
        if self._last_request_time is not None:
            since_last_ms = round((now - self._last_request_time) * 1000)
        self._last_request_time = now
        
        # 去掉 URL 中的 query string 以减少日志体积
        url_clean = url.split("?")[0] if url else url
        
        record = {
            "ts": datetime.now().isoformat(timespec="milliseconds"),
            "seq": self._seq,
            "method": method,
            "url": url_clean,
            "status_code": status_code,
            "result": result,
            "did": self._did,
            "profile": self._profile_name,
            "elapsed_ms": elapsed_ms,
            "since_last_ms": since_last_ms,
            "response_summary": response_summary,
            "anti_crawl": anti_crawl,
            "cookies_sent": cookies_sent,
            "error": error,
        }
        
        self._write(record)
        
        # 如果触发反爬，额外打印醒目日志
        if anti_crawl:
            print(f"[LOG] ⚠️ 反爬触发! seq={self._seq}, url={url_clean}, result={result}")
    
    def log_event(self, event: str, **kwargs):
        """记录自定义事件（非请求事件）。"""
        record = {
            "event": event,
            "ts": datetime.now().isoformat(timespec="milliseconds"),
            "seq": self._seq,
            "did": self._did,
            "profile": self._profile_name,
            **kwargs,
        }
        self._write(record)
    
    def _write(self, record: dict):
        """写入一行 JSONL。"""
        if not self.enabled or not self.log_path:
            return
        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as e:
            print(f"[LOG] 写入失败: {e}")
    
    def summary(self) -> dict:
        """返回本次运行的统计摘要。"""
        return {
            "total_requests": self._seq,
            "log_file": str(self.log_path),
            "did": self._did,
            "profile": self._profile_name,
        }
