"""Stealth fingerprint management — manual ops only.

设计原则：
    daemon / 业务路径**永远不自动跑 audit**。这套命令只在开发/排查时手动用。
    告警只 print 给跑命令的人，不发通知通道。

Subcommands:
    stealth audit       手动跑一次指纹 audit，输出 leak 报告
    stealth baseline    起浏览器停在指定页面，手动用 console 跑 probe.js 建 baseline
    stealth list-runs   列出 runs/ 目录下历史 audit 结果
    stealth list-baselines  列出 baselines/ 目录下已建的 baseline

典型用法：
    # 用 baidu 当默认 baseline 验一下 stealth
    crawlhub stealth audit

    # 用抖音特定 baseline 验
    crawlhub stealth audit --url "https://live.douyin.com/<room_id>" \\
                          --baseline real_chrome_douyin_live.json

    # 起浏览器停页面，手动建 baseline
    crawlhub stealth baseline --url "https://live.douyin.com/<room_id>"

    # 看历史
    crawlhub stealth list-runs
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Optional

import click


@click.group()
def stealth():
    """Stealth fingerprint management (manual operations only)."""


# ── stealth audit ─────────────────────────────────────────────────────


@stealth.command("audit")
@click.option(
    "--url",
    default="https://www.baidu.com",
    help="目标 URL（baseline 必须在同一 URL 上建过）",
)
@click.option(
    "--baseline",
    default=None,
    help="baseline 文件名（在 tools/fingerprint_audit/baselines/ 下）"
         "或绝对路径。默认根据 URL 推断（baidu→real_chrome_baidu.json）",
)
@click.option(
    "--headless/--headful",
    default=False,
    help="跑 headless 还是 headful（默认 headful，看得到浏览器窗口）",
)
@click.option(
    "--cookie-path",
    default=None,
    help="可选：注入的 cookie 文件路径（playwright storage_state 格式）",
)
@click.option(
    "--wait-selector",
    default=None,
    help="可选：等某个 CSS selector 出现再跑 probe（用于等 SDK 加载完）"
         "例：--wait-selector '.chat-room' 等抖音直播间弹幕区"
)
@click.option(
    "--wait-seconds",
    type=float,
    default=3.0,
    help="等待 SDK 初始化的额外秒数（默认 3）",
)
@click.option(
    "--name-hint",
    default="",
    help="落盘文件名后缀，便于区分多次 run（如 douyin_live）",
)
def audit_cmd(url, baseline, headless, cookie_path, wait_selector, wait_seconds, name_hint):
    """手动跑一次 fingerprint audit。

    退出码：
        0 = Tier A 全绿
        1 = Tier A 有 leak
        2 = baseline 不存在 / 启动失败
    """
    from crawlhub.core.browser.fingerprint_audit import (
        DEFAULT_BASELINES_DIR, audit_page, diff_with_baseline, save_run,
    )
    from crawlhub.core.browser.playwright_runtime import (
        create_playwright_browser_session,
    )
    from crawlhub.core.browser.session_key import SessionKey
    from crawlhub.core.plugin_manifest import BrowserConfig

    # 解析 baseline 路径
    if baseline:
        bp = Path(baseline)
        if not bp.is_absolute():
            bp = DEFAULT_BASELINES_DIR / baseline
    else:
        # 根据 URL 推断
        if "baidu" in url:
            bp = DEFAULT_BASELINES_DIR / "real_chrome_baidu.json"
        elif "douyin" in url and "live" in url:
            bp = DEFAULT_BASELINES_DIR / "real_chrome_douyin_live.json"
        elif "kuaishou" in url and "live" in url:
            bp = DEFAULT_BASELINES_DIR / "real_chrome_kuaishou_live.json"
        else:
            bp = DEFAULT_BASELINES_DIR / "real_chrome_baidu.json"

    if not bp.exists():
        click.echo(f"[ERR] baseline not found: {bp}", err=True)
        click.echo(
            f"[HINT] 建议先用 'crawlhub stealth baseline --url {url}' 起浏览器,"
            f"\n       手动用 console 跑 probe.js 建 baseline,"
            f"\n       存到 {bp}",
            err=True,
        )
        sys.exit(2)

    # headful/headless 通过 force_headful 参数显式传给浏览器入口
    # （--headless 默认 False → force_headful=True；指定 --headless → force_headful=False）
    _force_headful = not headless

    async def _run():
        click.echo(f"[stealth] starting BBA browser session "
                   f"(mode={'headless' if headless else 'headful'})...")

        class _NoopGate:
            async def acquire_async(self): pass
            def report_success(self): pass
            def report_failure(self, *a, **kw): return False

        # 选 platform（影响 user_data_dir 隔离）
        if "douyin" in url:
            platform = "douyin"
        elif "kuaishou" in url:
            platform = "kuaishou"
        else:
            platform = "probe"

        key = SessionKey(
            platform=platform,
            cookie_id=f"audit_{platform}",
            cookie_path=cookie_path,
        )
        # R7: BrowserConfig 极简（只剩 session_scope）
        cfg = BrowserConfig()
        session = await create_playwright_browser_session(
            key, cfg, request_gate=_NoopGate(),
            on_cookie_expired=lambda: None,
            force_headful=_force_headful,
        )
        try:
            # R7: chrome 启动后 _owned_pages 为空（lazy 模式），手动创建一个 page
            page_wrapper = await session.new_owned_page()
            page = page_wrapper.page

            click.echo(f"[stealth] navigating to {url} ...")
            await page.goto(url, wait_until="domcontentloaded")

            if wait_selector:
                click.echo(f"[stealth] waiting for selector {wait_selector!r} ...")
                try:
                    await page.wait_for_selector(wait_selector, timeout=15_000)
                    click.echo("[stealth] selector found, SDK ready")
                except Exception as exc:
                    click.echo(f"[stealth] selector wait timeout: {exc}", err=True)

            if wait_seconds > 0:
                click.echo(f"[stealth] waiting {wait_seconds}s for SDK init ...")
                await asyncio.sleep(wait_seconds)

            click.echo("[stealth] running probe.js ...")
            result = await audit_page(page)
            run_path = save_run(result, name_hint=name_hint or platform)
            click.echo(f"[stealth] saved run to {run_path}")

            diff = diff_with_baseline(result, bp)

            # 报告
            click.echo("")
            click.echo("=" * 70)
            click.echo(f"  AUDIT REPORT  ({diff.summary()})")
            click.echo(f"  baseline: {bp.name}")
            click.echo(f"  url:      {url}")
            click.echo("=" * 70)

            if diff.tier_a_leaks:
                click.echo("")
                click.secho("  [FAIL] TIER A LEAKS (must fix):", fg="red", bold=True)
                for leak in diff.tier_a_leaks:
                    click.echo(f"     {leak['path']}")
                    click.echo(f"        baseline: {str(leak['baseline'])[:80]}")
                    click.echo(f"        target:   {str(leak['target'])[:80]}")
            else:
                click.echo("")
                click.secho("  [OK] TIER A: identical -- no critical leaks", fg="green", bold=True)

            if diff.tier_b_leaks:
                click.echo("")
                click.secho(f"  [WARN] TIER B differences ({len(diff.tier_b_leaks)}):", fg="yellow")
                for leak in diff.tier_b_leaks[:10]:
                    click.echo(f"     - {leak['path']}: "
                               f"{str(leak['baseline'])[:40]} -> {str(leak['target'])[:40]}")
                if len(diff.tier_b_leaks) > 10:
                    click.echo(f"     ... and {len(diff.tier_b_leaks) - 10} more")

            if diff.expected_diffs:
                click.echo("")
                click.echo(f"  - Expected diffs (whitelisted): "
                           f"{len(diff.expected_diffs)} (hardware/timestamp/site, harmless)")
            click.echo("")

            return 0 if not diff.tier_a_leaks else 1
        finally:
            await session.close()

    try:
        rc = asyncio.run(_run())
        sys.exit(rc)
    except KeyboardInterrupt:
        click.echo("[stealth] interrupted")
        sys.exit(130)


# ── stealth baseline ──────────────────────────────────────────────────


@stealth.command("baseline")
@click.option("--url", required=True, help="目标 URL (不能省)")
@click.option("--wait-selector", default=None, help="等 CSS selector 出现")
def baseline_cmd(url, wait_selector):
    """起浏览器停在指定页面，等手动 console 跑 probe.js 建 baseline.

    用法：
        1. 跑 'crawlhub stealth baseline --url "..."'
        2. 浏览器窗口弹出，导航到目标页
        3. 在那个浏览器里 F12 → Console
        4. 粘 tools/fingerprint_audit/probe.js 全文，回车
        5. console 输入 copy(window.__probe_result__)
        6. 粘到 tools/fingerprint_audit/baselines/<some_name>.json
        7. 回此终端 Ctrl+C 退出

    ⚠️ 必须用**真 Chrome** 不是 crawlhub 启的浏览器。这条命令是给你**搭一个跟
    crawlhub 一致的环境** —— 但前提是你已经在真 Chrome 跑过了，存了 baseline 文件。

    实际上更简单的姿势是直接用你日常 Chrome 在目标 URL 上跑 probe.js，
    完全不用这条命令。这条命令保留是为了未来扩展（比如自动化 baseline 生成）。
    """
    click.echo(
        "[hint] 这条命令只是辅助 —— 实际建 baseline 推荐直接在你日常 Chrome 上：\n"
        f"  1. 真 Chrome 打开 {url}\n"
        "  2. F12 → Console → 粘 tools/fingerprint_audit/probe.js\n"
        "  3. copy(window.__probe_result__) → 存到 baselines/ 目录\n"
        ""
    )


# ── stealth list-runs ─────────────────────────────────────────────────


@stealth.command("list-runs")
@click.option("--limit", type=int, default=20, help="显示最近 N 条")
def list_runs_cmd(limit):
    """列出 runs/ 目录下历史 audit 结果."""
    from crawlhub.core.browser.fingerprint_audit import DEFAULT_RUNS_DIR

    if not DEFAULT_RUNS_DIR.exists():
        click.echo(f"[INFO] runs dir empty: {DEFAULT_RUNS_DIR}")
        return

    files = sorted(DEFAULT_RUNS_DIR.glob("*.json"), reverse=True)[:limit]
    if not files:
        click.echo(f"[INFO] no runs in {DEFAULT_RUNS_DIR}")
        return

    click.echo(f"Last {len(files)} run(s) in {DEFAULT_RUNS_DIR}:")
    for f in files:
        try:
            with open(f, encoding="utf-8") as fh:
                data = json.load(fh)
            ts = data.get("_meta", {}).get("ts", "?")
            url = data.get("_meta", {}).get("url", "?")
            click.echo(f"  {f.name:50s}  ts={ts}  url={url}")
        except Exception as exc:
            click.echo(f"  {f.name:50s}  [ERR: {exc}]")


# ── stealth list-baselines ────────────────────────────────────────────


@stealth.command("list-baselines")
def list_baselines_cmd():
    """列出 baselines/ 目录下已建的 baseline."""
    from crawlhub.core.browser.fingerprint_audit import DEFAULT_BASELINES_DIR

    if not DEFAULT_BASELINES_DIR.exists():
        click.echo(f"[INFO] baselines dir not found: {DEFAULT_BASELINES_DIR}")
        return

    files = sorted(DEFAULT_BASELINES_DIR.glob("*.json"))
    if not files:
        click.echo(f"[INFO] no baselines yet in {DEFAULT_BASELINES_DIR}")
        click.echo("[HINT] 用日常 Chrome 跑 probe.js 后存到此目录")
        return

    click.echo(f"Baselines in {DEFAULT_BASELINES_DIR}:")
    for f in files:
        try:
            with open(f, encoding="utf-8") as fh:
                data = json.load(fh)
            ts = data.get("_meta", {}).get("ts", "?")
            url = data.get("_meta", {}).get("url", "?")
            click.echo(f"  {f.name:50s}  built_at={ts}  for_url={url}")
        except Exception as exc:
            click.echo(f"  {f.name:50s}  [ERR: {exc}]")
