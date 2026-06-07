"""Configuration management for CrawlHub.

Handles loading/creating config.yaml and defining the data root directory.
"""

from __future__ import annotations

import math
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


def get_data_root() -> Path:
    """Return the CrawlHub data root directory (~/.crawlhub/)."""
    return Path.home() / ".crawlhub"


# --- Default throttle intervals per platform ---

DEFAULT_THROTTLE_INTERVALS: dict[str, float] = {
    "qimai": 0.5,
    "steam": 0.5,
    "bilibili": 1.0,
    "weibo": 1.0,
    "douyin": 1.0,
    "kuaishou": 1.0,
}

DEFAULT_BACKOFF_BASE_SECONDS = 60.0
DEFAULT_MAX_BACKOFF_EXPONENT = 4  # max 2^4 * base = 16 * 60 = 960s

# ──────────────────────────────────────────────────────────────────────
#  指数分布尾部截断：百分位 → 倍数转换
# ──────────────────────────────────────────────────────────────────────
#  指数分布 CDF: F(x) = 1 - e^(-x/μ)
#  分位数函数:   Q(p) = -μ * ln(1 - p)
#  → 截断在 p 分位数 == 截断在 -ln(1-p) × μ 处
#
#  常用映射（μ = 期望间隔）：
#    p=0.90  → cap = 2.30μ   （砍掉 10% 长尾）
#    p=0.95  → cap = 3.00μ   （砍掉 5% 长尾，默认值）
#    p=0.99  → cap = 4.61μ   （砍掉 1% 长尾）
#    p=1.00  → cap = ∞       （等价于关闭截断）
# ──────────────────────────────────────────────────────────────────────

# 默认截断分位数：砍掉 top 5% 长尾。
# 设为 None 或 >= 1.0 时关闭截断，回退到原始 expovariate 行为。
DEFAULT_TRUNCATE_PERCENTILE = 0.95


@dataclass
class ThrottleConfig:
    """Per-platform throttle configuration."""

    expected_interval: float = 1.0  # Expected interval in seconds (exponential distribution mean)
    min_floor: float | None = None  # Minimum interval floor; defaults to expected * 0.3
    backoff_base_seconds: float = DEFAULT_BACKOFF_BASE_SECONDS
    max_backoff_exponent: int = DEFAULT_MAX_BACKOFF_EXPONENT
    # ──────────────────────────────────────────────────────────────
    #  R4-P15 长尾截断：超过 p 分位数的取样直接钳位（clamp），
    #  保留指数分布的真人节奏感，同时消除 ≥ 9μ 的极端长尾。
    #  None 或 >= 1.0 表示关闭截断。
    # ──────────────────────────────────────────────────────────────
    truncate_percentile: float | None = DEFAULT_TRUNCATE_PERCENTILE

    @property
    def effective_min_floor(self) -> float:
        """Get effective min_floor (computed default if not explicitly set)."""
        if self.min_floor is not None:
            return self.min_floor
        return self.expected_interval * 0.3

    @property
    def effective_truncate_cap(self) -> float | None:
        """计算截断阈值（绝对秒数）；None 表示不截断。

        对于有效百分位 p ∈ (0, 1)，cap = -ln(1-p) × expected_interval。
        """
        p = self.truncate_percentile
        if p is None or p <= 0.0 or p >= 1.0:
            return None
        if self.expected_interval <= 0:
            return None
        return -math.log(1.0 - p) * self.expected_interval

    def to_dict(self) -> dict[str, Any]:
        return {
            "expected_interval": self.expected_interval,
            "min_floor": self.min_floor,
            "backoff_base_seconds": self.backoff_base_seconds,
            "max_backoff_exponent": self.max_backoff_exponent,
            "truncate_percentile": self.truncate_percentile,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ThrottleConfig:
        # truncate_percentile 显式缺失 → 沿用默认 0.95；显式为 None → 关闭截断
        if "truncate_percentile" in data:
            tp = data["truncate_percentile"]
            tp_val = float(tp) if tp is not None else None
        else:
            tp_val = DEFAULT_TRUNCATE_PERCENTILE
        return cls(
            expected_interval=data.get("expected_interval", 1.0),
            min_floor=data.get("min_floor"),
            backoff_base_seconds=data.get("backoff_base_seconds", DEFAULT_BACKOFF_BASE_SECONDS),
            max_backoff_exponent=data.get("max_backoff_exponent", DEFAULT_MAX_BACKOFF_EXPONENT),
            truncate_percentile=tp_val,
        )


@dataclass
class ObservabilityConfig:
    """R7 observability configuration.

    Fields:
        record_requests: Whether to write `requests.jsonl` per-task.
            Default False (off). Set to true to capture every transport-level
            HTTP/WSS event for debugging. The patch layer is always installed
            regardless — this only toggles the jsonl writer.
    """

    record_requests: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"record_requests": self.record_requests}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ObservabilityConfig:
        return cls(record_requests=bool(data.get("record_requests", False)))


@dataclass
class BrowserGlobalConfig:
    """Global browser launch configuration.

    Distinct from `crawlhub.core.plugin_manifest.BrowserConfig` (per-plugin
    schema). This holds runtime/global toggles.

    Fields:
        bba_headful: When True, BBA browsers (data-collection mode) launch
            with a visible window. Default False (headless via --headless=new).
            The login flow always forces headful regardless of this setting,
            via an explicit `force_headful=True` argument.
    """

    bba_headful: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"bba_headful": self.bba_headful}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BrowserGlobalConfig:
        return cls(bba_headful=bool(data.get("bba_headful", False)))


@dataclass
class CrawlHubConfig:
    """Global configuration loaded from ~/.crawlhub/config.yaml."""

    host: str = "127.0.0.1"
    port: int = 8787

    # Per-platform concurrency
    concurrency: dict[str, int] = field(default_factory=lambda: {"default": 3})

    # Per-platform throttle configuration
    throttle: dict[str, ThrottleConfig] = field(default_factory=dict)

    # Recycle bin: how long an archived task is kept before auto-purge.
    # When `archived_at` is older than now - archived_purge_days, the
    # scheduler permanently deletes the task and its on-disk artifacts.
    archived_purge_days: int = 30
    cleanup_cron: str = "0 3 * * *"  # daily at 03:00

    # Logging
    log_max_size_mb: int = 50
    log_backup_count: int = 3

    # VACUUM
    vacuum_interval_hours: int = 24

    # Disk threshold
    disk_low_threshold_mb: int = 500

    # Webhook (notification channels stored in DB, this is just defaults)
    default_webhook_url: str = ""

    # R7 Observability — controls task-level requests.jsonl writer
    observability: ObservabilityConfig = field(default_factory=ObservabilityConfig)

    # Browser launch toggles (BBA headful mode, etc.)
    browser: BrowserGlobalConfig = field(default_factory=BrowserGlobalConfig)

    def get_concurrency(self, platform: str) -> int:
        """Get max concurrent workers for a platform."""
        return self.concurrency.get(platform, self.concurrency.get("default", 3))

    def get_throttle_config(self, platform: str) -> ThrottleConfig:
        """Get throttle config for a platform (with defaults)."""
        if platform in self.throttle:
            return self.throttle[platform]
        # Return default based on platform
        default_interval = DEFAULT_THROTTLE_INTERVALS.get(platform, 1.0)
        return ThrottleConfig(expected_interval=default_interval)

    def update_throttle_config(self, platform: str, params: dict[str, Any]) -> ThrottleConfig:
        """Update throttle config for a platform (hot-reload).

        Merges params into existing config, persists to disk, and returns updated config.
        """
        current = self.get_throttle_config(platform)
        if "expected_interval" in params:
            current.expected_interval = float(params["expected_interval"])
        if "min_floor" in params:
            current.min_floor = float(params["min_floor"]) if params["min_floor"] is not None else None
        if "backoff_base_seconds" in params:
            current.backoff_base_seconds = float(params["backoff_base_seconds"])
        if "max_backoff_exponent" in params:
            current.max_backoff_exponent = int(params["max_backoff_exponent"])
        if "truncate_percentile" in params:
            tp = params["truncate_percentile"]
            current.truncate_percentile = float(tp) if tp is not None else None

        self.throttle[platform] = current
        # Persist to disk
        config_path = get_data_root() / "config.yaml"
        _write_config(config_path, self)
        return current


# --- Singleton config instance for hot-reload ---

_config_instance: CrawlHubConfig | None = None


def get_config() -> CrawlHubConfig:
    """Get the global config singleton (supports hot-reload)."""
    global _config_instance
    if _config_instance is None:
        _config_instance = load_config()
    return _config_instance


def load_config() -> CrawlHubConfig:
    """Load config from ~/.crawlhub/config.yaml.

    - If file doesn't exist: create with defaults (permissions 600).
    - If YAML parse fails: print error line to stderr and exit(1).
    """
    global _config_instance
    data_root = get_data_root()
    config_path = data_root / "config.yaml"

    if not config_path.exists():
        # Create default config
        data_root.mkdir(parents=True, exist_ok=True)
        default_config = CrawlHubConfig()
        _write_config(config_path, default_config)
        _config_instance = default_config
        return default_config

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)
    except yaml.YAMLError as e:
        # Print specific error info
        if hasattr(e, "problem_mark"):
            mark = e.problem_mark
            print(
                f"[ERR] config.yaml parse error at line {mark.line + 1}, column {mark.column + 1}: {e.problem}",
                file=sys.stderr,
            )
        else:
            print(f"[ERR] config.yaml parse error: {e}", file=sys.stderr)
        sys.exit(1)

    if raw is None:
        config = CrawlHubConfig()
        _config_instance = config
        return config

    # Parse throttle config
    throttle_raw = raw.get("throttle", {})
    throttle: dict[str, ThrottleConfig] = {}
    if isinstance(throttle_raw, dict):
        for platform, tc_data in throttle_raw.items():
            if isinstance(tc_data, dict):
                throttle[platform] = ThrottleConfig.from_dict(tc_data)

    # Parse observability/browser sections (back-compat: missing → defaults)
    obs_raw = raw.get("observability", {})
    observability = (
        ObservabilityConfig.from_dict(obs_raw)
        if isinstance(obs_raw, dict)
        else ObservabilityConfig()
    )
    browser_raw = raw.get("browser", {})
    browser = (
        BrowserGlobalConfig.from_dict(browser_raw)
        if isinstance(browser_raw, dict)
        else BrowserGlobalConfig()
    )

    config = CrawlHubConfig(
        host=raw.get("host", "127.0.0.1"),
        port=raw.get("port", 8787),
        concurrency=raw.get("concurrency", {"default": 3}),
        throttle=throttle,
        archived_purge_days=raw.get("archived_purge_days", 30),
        cleanup_cron=raw.get("cleanup_cron", "0 3 * * *"),
        log_max_size_mb=raw.get("log_max_size_mb", 50),
        log_backup_count=raw.get("log_backup_count", 3),
        vacuum_interval_hours=raw.get("vacuum_interval_hours", 24),
        disk_low_threshold_mb=raw.get("disk_low_threshold_mb", 500),
        default_webhook_url=raw.get("default_webhook_url", ""),
        observability=observability,
        browser=browser,
    )
    _config_instance = config
    return config


def _write_config(path: Path, config: CrawlHubConfig) -> None:
    """Write config to YAML file with restricted permissions."""
    # Serialize throttle configs
    throttle_data: dict[str, Any] = {}
    for platform, tc in config.throttle.items():
        throttle_data[platform] = tc.to_dict()

    data: dict[str, Any] = {
        "host": config.host,
        "port": config.port,
        "concurrency": config.concurrency,
        "throttle": throttle_data,
        "archived_purge_days": config.archived_purge_days,
        "cleanup_cron": config.cleanup_cron,
        "log_max_size_mb": config.log_max_size_mb,
        "log_backup_count": config.log_backup_count,
        "vacuum_interval_hours": config.vacuum_interval_hours,
        "disk_low_threshold_mb": config.disk_low_threshold_mb,
        "default_webhook_url": config.default_webhook_url,
        "observability": config.observability.to_dict(),
        "browser": config.browser.to_dict(),
    }

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True)

    # Set permissions to 600 (owner read/write only) on POSIX
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass  # Windows doesn't support POSIX permissions natively


def ensure_directories() -> None:
    """Create all required subdirectories under ~/.crawlhub/."""
    root = get_data_root()
    dirs = [
        root / "cookies",
        root / "output",
        root / "logs",
        root / "logs" / "tasks",
        root / "tmp",
        root / "trash",
    ]
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)


def save_config(config: CrawlHubConfig) -> None:
    """Save config to disk (public API for hot-reload)."""
    config_path = get_data_root() / "config.yaml"
    _write_config(config_path, config)
