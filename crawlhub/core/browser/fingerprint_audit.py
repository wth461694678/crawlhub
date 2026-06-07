"""Fingerprint audit runner — let crawlhub run probe.js automatically.

设计目标：
    在 BBA session 启动后、业务请求开始前，自动跑一次 probe.js 取回结果，
    跟 baseline diff，发现 Tier A leak 直接 fail 任务（指纹断言层）。

为什么必须自动化：
    1. 生产是 headless：用户没法手动 F12 粘 console
    2. 跨机器分发：Win10/Win11/Mac 同事跑出来 leak 不一样，需要客观采样
    3. 持续可观测：每个 BBA session 都跑一次，留下趋势

API：
    audit_page(page) -> dict
        在 page 上执行 probe，返回结果 dict（同手动 console 跑出来的结构）

    diff_with_baseline(result, baseline_path) -> AuditDiff
        跟 baseline JSON 对比，按 tier 分类返回 leak 列表

    save_run(result, output_dir) -> Path
        把 result 落盘到 runs/ 目录，文件名带时间戳

用法（生产路径）：
    result = await audit_page(page)
    diff = diff_with_baseline(result, BASELINE_PATH)
    if diff.tier_a_leaks:
        raise FingerprintAuditFailed(diff.tier_a_leaks)

用法（开发调试）：
    result = await audit_page(page)
    save_run(result, DEFAULT_RUNS_DIR)  # 落盘存档
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# probe.js 文件路径：放在项目工具目录
PROBE_JS_PATH = (
    Path(__file__).resolve().parent.parent.parent.parent
    / "tools" / "fingerprint_audit" / "probe.js"
)
DEFAULT_BASELINES_DIR = (
    Path(__file__).resolve().parent.parent.parent.parent
    / "tools" / "fingerprint_audit" / "baselines"
)
DEFAULT_RUNS_DIR = (
    Path(__file__).resolve().parent.parent.parent.parent
    / "tools" / "fingerprint_audit" / "runs"
)


class FingerprintAuditFailed(RuntimeError):
    """Tier A leak detected — task should not proceed."""

    def __init__(self, leaks: list[dict[str, Any]]) -> None:
        self.leaks = leaks
        msg = f"Fingerprint audit failed with {len(leaks)} Tier A leak(s):\n" + \
              "\n".join(f"  - {leak['path']}: {leak['baseline']} → {leak['target']}"
                        for leak in leaks)
        super().__init__(msg)


@dataclass
class AuditDiff:
    """Diff result between current run and baseline."""
    tier_a_leaks: list[dict[str, Any]] = field(default_factory=list)
    tier_b_leaks: list[dict[str, Any]] = field(default_factory=list)
    tier_c_leaks: list[dict[str, Any]] = field(default_factory=list)
    expected_diffs: list[dict[str, Any]] = field(default_factory=list)

    @property
    def has_critical_leaks(self) -> bool:
        return bool(self.tier_a_leaks)

    def summary(self) -> str:
        return (
            f"Tier A: {len(self.tier_a_leaks)} leak(s) | "
            f"Tier B: {len(self.tier_b_leaks)} leak(s) | "
            f"Tier C: {len(self.tier_c_leaks)} leak(s) | "
            f"expected: {len(self.expected_diffs)}"
        )


# 跟 baseline_diff.py 一致的白名单：跨机器/版本天然差异
EXPECTED_DIFF_PATTERNS = [
    "_meta.ts",
    "_meta.url",
    # crawlhub 自己注入的 stealth marker：baseline（真 Chrome）没有这个字段，
    # target（crawlhub 跑的）一定有。整个子树都是 expected diff。
    "tier_a.crawlhub_stealth_marker",
    "tier_a.error_stack_check.stack_preview",  # probe 在 console 跑 vs page.evaluate 跑栈格式必然不同
    # cdp_console_trick 在敏感站点会触发风控，默认 opt-in（probe.js 里默认不跑）
    # 跑出来是 "[SKIPPED:opt_in_required]"，跟 baseline 不一致是预期行为
    "tier_a.cdp_console_trick",
    "tier_b.canvas_hash",
    "tier_b.audio_fingerprint",
    "tier_b.webgl_fingerprint.unmasked_renderer",
    "tier_b.webgl_fingerprint.unmasked_vendor",
    "tier_b.fonts_check_basic",
    "tier_b.date_str",
    "tier_b.devicePixelRatio",
    "tier_b.screen.width",
    "tier_b.screen.height",
    "tier_b.screen.availWidth",
    "tier_b.screen.availHeight",
    # navigator.languages 数组里某一项的取值跨用户系统天然不同
    # （baseline 用户系统装了法语、德语等，普通用户没有；强制对齐反而怪）
    # 我们仍然要保证 length=4（前面 [P0-2] 已 patch 强制 4 项），
    # 但具体哪一项是 fr/en-US/de 不强制
    "tier_b.navigator.languages[",
    # worker 内 navigator.webdriver 的存在性跟 probe.js worker code 的执行
    # 时序有关（部分 probe run 没读到 = MISSING，部分读到 = None），
    # 是 probe 的非确定性而非真实 leak
    "tier_b.worker_context_check.webdriver",
    # worker 内 languages 同上：length=4 已强制，具体某一项不强制
    "tier_b.worker_context_check.languages[",
    "tier_c.performance_navigation",
    "tier_c.battery",
    "tier_c.connection",
    "tier_c.hardware.hardwareConcurrency",
    "tier_c.hardware.deviceMemory",
    "tier_c.document_props.cookie_length",
    "tier_c.document_props.referrer",
    "tier_c.document_props.title",
    "tier_c.document_props.has_focus",
    "tier_c.document_props.visibility",
    "tier_c.window_top_keys",  # baidu 加载状态差异
    # ── 抖音/快手等平台 SDK 注入到 navigator 的字段：
    # 这些是平台 SDK 自己注入的（如 SDKNativeWebApi 是抖音 webcastSDK 的桥），
    # 是"平台对我们的判断"而非"我们的 leak"。强行 polyfill 反而会触发 honeypot
    # 检测（典型如 navigator.pemrissions 故意拼错的反爬陷阱）。
    # 这类字段出现差异 = 平台 SDK 给 crawlhub 跑了不同路径，是诊断信号但不是修复目标。
    "tier_c.navigator_all_props.SDKNativeWebApi",
    "tier_c.navigator_all_props.pemrissions",  # honeypot 故意拼错
    "tier_c.navigator_all_props.vendorSubs",
    # 通用兜底：抖音 / 快手 SDK 注入的全部字段（保守起见，未来出现新注入物时也安全）
    # 若以后想精细管控，可移除这条改为白名单具体字段
]


def _is_expected(path: str) -> bool:
    return any(path.startswith(p) for p in EXPECTED_DIFF_PATTERNS)


# ════════════════════════════════════════════════════════════════════
#  在 page 上跑 probe.js
# ════════════════════════════════════════════════════════════════════


async def audit_page(page: Any, *, probe_js: str | None = None) -> dict[str, Any]:
    """在指定 page 上跑 probe.js 并返回结果 dict。

    page: playwright/patchright Page 对象（不是 PlaywrightPageWrapper）
    probe_js: 可选，自定义 probe 脚本内容（默认从 PROBE_JS_PATH 读）

    返回: probe.js 输出的完整 result dict
    """
    if probe_js is None:
        if not PROBE_JS_PATH.exists():
            raise FileNotFoundError(f"probe.js not found at {PROBE_JS_PATH}")
        probe_js = PROBE_JS_PATH.read_text(encoding="utf-8")

    # probe.js 是 IIFE：(async function(){...})()
    # page.evaluate 接受 expression 形式：直接传 IIFE 字符串就能拿返回值。
    # 注意：probe.js 末尾有"return result;"，IIFE 自身就是 expression，
    # await IIFE 拿到的就是 result dict。
    logger.info("[audit] running probe.js on page (length=%d)", len(probe_js))
    result = await page.evaluate(probe_js)
    if not isinstance(result, dict):
        raise RuntimeError(
            f"probe.js returned unexpected type {type(result).__name__}: "
            f"{str(result)[:200]}"
        )
    logger.info(
        "[audit] probe done. tier_a keys=%d, tier_b=%d, tier_c=%d",
        len(result.get("tier_a", {})),
        len(result.get("tier_b", {})),
        len(result.get("tier_c", {})),
    )
    return result


# ════════════════════════════════════════════════════════════════════
#  Diff
# ════════════════════════════════════════════════════════════════════


def _walk_diff(a: Any, b: Any, prefix: str = "") -> list[tuple[str, Any, Any]]:
    """跟 baseline_diff.py 同款的 deep diff，返回叶节点差异列表。"""
    diffs: list[tuple[str, Any, Any]] = []

    if type(a) != type(b):
        diffs.append((prefix, a, b))
        return diffs

    if isinstance(a, dict):
        keys = set(a) | set(b)
        for k in sorted(keys):
            sub = f"{prefix}.{k}" if prefix else k
            if k not in a:
                diffs.append((sub, "<MISSING>", b[k]))
            elif k not in b:
                diffs.append((sub, a[k], "<MISSING>"))
            else:
                diffs.extend(_walk_diff(a[k], b[k], sub))
        return diffs

    if isinstance(a, list):
        if len(a) != len(b):
            diffs.append((prefix + ".length", len(a), len(b)))
            return diffs
        for i, (x, y) in enumerate(zip(a, b)):
            diffs.extend(_walk_diff(x, y, f"{prefix}[{i}]"))
        return diffs

    if a != b:
        diffs.append((prefix, a, b))
    return diffs


def diff_with_baseline(
    result: dict[str, Any],
    baseline_path: Path | str,
) -> AuditDiff:
    """比对 result 跟 baseline 文件，返回分类后的 leak 列表。"""
    baseline_path = Path(baseline_path)
    if not baseline_path.exists():
        raise FileNotFoundError(f"baseline not found: {baseline_path}")
    with open(baseline_path, encoding="utf-8") as f:
        baseline = json.load(f)

    diff = AuditDiff()
    for tier_name, target_field in [
        ("tier_a", "tier_a_leaks"),
        ("tier_b", "tier_b_leaks"),
        ("tier_c", "tier_c_leaks"),
    ]:
        if tier_name not in baseline or tier_name not in result:
            continue
        for path, bv, tv in _walk_diff(baseline[tier_name], result[tier_name], tier_name):
            entry = {"path": path, "baseline": bv, "target": tv}
            if _is_expected(path):
                diff.expected_diffs.append(entry)
            else:
                getattr(diff, target_field).append(entry)
    return diff


# ════════════════════════════════════════════════════════════════════
#  落盘
# ════════════════════════════════════════════════════════════════════


def save_run(
    result: dict[str, Any],
    output_dir: Path | str = DEFAULT_RUNS_DIR,
    *,
    name_hint: str = "",
) -> Path:
    """把 audit 结果落盘到 runs/ 目录，返回文件路径。

    name_hint: 可选标签，例如 "after_fix" / "douyin"
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = f"_{name_hint}" if name_hint else ""
    path = output_dir / f"crawlhub_{ts}{suffix}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    logger.info("[audit] saved run to %s", path)
    return path


# ════════════════════════════════════════════════════════════════════
#  一站式：跑 + diff + 落盘 + 抛错（生产路径用）
# ════════════════════════════════════════════════════════════════════


async def audit_and_assert(
    page: Any,
    *,
    baseline_path: Path | str,
    save_to: Path | str | None = DEFAULT_RUNS_DIR,
    name_hint: str = "",
    raise_on_tier_a: bool = True,
) -> AuditDiff:
    """一站式审计：跑 probe → 落盘 → diff → Tier A leak 时抛错。

    生产 BBA session 启动后调用：

        await audit_and_assert(
            page,
            baseline_path=DEFAULT_BASELINES_DIR / "real_chrome_baidu.json",
            name_hint="bba_startup",
        )
    """
    result = await audit_page(page)
    if save_to is not None:
        save_run(result, save_to, name_hint=name_hint)
    diff = diff_with_baseline(result, baseline_path)
    logger.info("[audit] diff summary: %s", diff.summary())
    if diff.tier_a_leaks:
        for leak in diff.tier_a_leaks:
            logger.warning(
                "[audit] TIER_A LEAK: %s baseline=%r target=%r",
                leak["path"], leak["baseline"], leak["target"],
            )
        if raise_on_tier_a:
            raise FingerprintAuditFailed(diff.tier_a_leaks)
    if diff.tier_b_leaks:
        for leak in diff.tier_b_leaks:
            logger.info(
                "[audit] tier_b leak: %s baseline=%r target=%r",
                leak["path"], leak["baseline"], leak["target"],
            )
    return diff
