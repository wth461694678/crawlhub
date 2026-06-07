"""
Douyin Scraper Core
===================
High-level scraper class that orchestrates search, video info,
and comment scraping using the Douyin SDK.

Features:
  - Search videos by keyword
  - Scrape comments (with optional sub-comments)
  - Rate limiting
  - Result saving (JSONL format)
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import sys
import time
import threading

from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlencode

from typing import Optional

from crawlhub.core.cookie_resolver import (
    CookieResolverMixin,
    CookieNotReady,
)
from crawlhub.core.task_context import TaskContext
from crawlhub.core.platform import get_current_runtime


from .client import DouyinSDK
from .models import (
    VideoInfo, Comment, SearchResult, LiveRoom, VideoResult,
    LiveRoomSearchResult, LiveRoomInfoRecord, LiveEventRecord,
)



# ════════════════════════════════════════════════════════════
#  Core Crawler
# ════════════════════════════════════════════════════════════

logger = logging.getLogger(__name__)


class DouyinScraper(CookieResolverMixin):
    """Douyin Scraper.

    Args:
        cookie_path:          Path to cookie JSON file.
        output_dir:           Directory for saving results.
        page_delay:           Delay between comment pagination requests (seconds).
        max_comments:         Max comments to fetch per video (None = all).
        include_sub_comments: Whether to also fetch sub-comments (replies).
    """

    DEFAULT_PAGE_DELAY = 1.0
    PLATFORM_NAME = "douyin"

    def __init__(
        self,
        cookie_path: str = None,
        output_dir: str = None,
        page_delay: float = DEFAULT_PAGE_DELAY,
        max_comments: Optional[int] = None,
        include_sub_comments: bool = False,
        comment_sort: int = 0,
        log_callback=None,
    ):
        # cookie_path is treated as an explicit override only.  When None
        # the scraper lazily resolves the current task's cookie via
        # ``CookieResolverMixin.resolve_cookie_path()`` (which honors the
        # daemon's thread-local override). This matches the R3 contract.
        self._explicit_cookie_path = cookie_path
        # R6: do NOT default output_dir to a module-local path. The legacy
        # CLI used to fall back to ``_base / "output"`` which creates a
        # forbidden directory under crawlers/douyin/ (write-root isolation
        # violation). Daemon mode never writes through self.output_dir
        # anyway — records flow through ctx.write_record. The CLI helpers
        # ``_save_search_results`` / ``save_result`` lazily mkdir below.
        self.output_dir = Path(output_dir) if output_dir else None

        self.page_delay = page_delay
        self.max_comments = max_comments
        self.include_sub_comments = include_sub_comments
        self.comment_sort = comment_sort

        self._print_lock = threading.Lock()
        self._sdk: Optional[DouyinSDK] = None
        self._log_callback = log_callback

        # NOTE: do NOT call self._log() here.  Construction must be free of
        # stdout/stderr side effects because the platform service may be
        # instantiated from non-task threads (FastAPI request handlers,
        # cookie-check probes, etc.).  Writing to sys.stderr in that
        # context can hit a closed task log writer and crash the request.
        logger.debug(
            "DouyinScraper initialized | page_delay=%ss max_comments=%s "
            "include_sub_comments=%s",
            page_delay, max_comments or "all", include_sub_comments,
        )

    def _resolved_cookie_path(self) -> str:
        """Resolve the cookie path the current call MUST use.

        Honors (in order):
        1. Daemon thread-local override (set per task via CookieResolverMixin).
        2. Explicit ``cookie_path=`` constructor override.
        3. Standard ``CookieResolverMixin`` resolution.
        """
        if self._explicit_cookie_path:
            return self._explicit_cookie_path
        return str(self.resolve_cookie_path())

    @property
    def cookie_path(self) -> str:
        """Legacy attribute kept for compatibility with internal helpers
        (browser_bridge, save_cookies_from_browser, etc.)."""
        return self._resolved_cookie_path()

    def check_cookie_valid(self) -> bool:
        """CookieResolverMixin hook -- raises CookieNotReady if no cookie."""
        crawler_path = self.get_crawler_cookie_path()
        if crawler_path.exists():
            return True
        fallback = self.get_cookie_path()
        if fallback.exists():
            return True
        raise CookieNotReady("douyin", "No cookie file found. Please login first.")

    def _get_sdk(self) -> DouyinSDK:
        """Get or create SDK instance.

        Re-resolves cookie path on every call so that thread-local overrides
        (set by daemon per task) take effect even when the scraper is reused
        across tasks (R3 singleton pattern).
        """
        cookie_path = self._resolved_cookie_path()
        if self._sdk is None or getattr(self._sdk, "cookie_path", None) != cookie_path:
            self._sdk = DouyinSDK(
                cookie_path=cookie_path,
                log_prefix="dy_crawler",
            )
        return self._sdk

    # TODO(R3-ghost): suggest rename to `_shutdown` (internal cleanup); awaiting decision
    def shutdown(self):
        """Clean up resources."""
        self._sdk = None

    def _log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        with self._print_lock:
            try:
                print(line, file=sys.stderr)
            except UnicodeEncodeError:
                print(line.encode("utf-8", errors="replace").decode("utf-8"),
                      file=sys.stderr)
        if self._log_callback:
            try:
                self._log_callback(msg)
            except Exception:
                pass

    # ── Login ────────────────────────────────────────────────

    # TODO(R3-ghost): suggest rename to `_check_login` (internal cookie self-check); awaiting decision
    def check_login(self) -> bool:
        """Check if current cookies are valid."""
        sdk = self._get_sdk()
        result = sdk.check_login()
        ok = result.get("is_login", False)
        if ok:
            self._log(f"[OK] Logged in as: {result.get('name', 'unknown')}")
        else:
            self._log("[FAIL] Not logged in")
        return ok

    # TODO(R3-ghost): suggest add to plugin.yaml as action `save_cookies_from_browser` (used by MCP server post-login); awaiting decision
    def save_cookies_from_browser(self, cookie_string: str,
                                   extra_headers: dict = None):
        """Save cookies from browser interception.

        This is called by the MCP server after browser-based SMS login.
        """
        sdk = self._get_sdk()
        sdk.save_cookies_from_browser(cookie_string, extra_headers, self.cookie_path)
        self._log("[OK] Cookies saved from browser")

    # ── Search (BBA) ──────────────────────────────────────────

    _SEARCH_ENDPOINT = "https://www.douyin.com/aweme/v1/web/general/search/single/"
    _SEARCH_PAGE_SIZE = 10

    def _search_videos_bba(
        self,
        browser_session,
        *,
        ctx: TaskContext,
        keyword: str,
        max_results: int,
        sort_type: int,
        publish_time: int,
    ) -> list[SearchResult]:

        """Search videos through daemon-managed BrowserSession."""
        results: list[SearchResult] = []
        seen_ids: set[str] = set()
        search_id = ""
        page = 1
        browser_session.goto(self._build_bba_search_page_url(keyword, sort_type, publish_time))
        # ──────────────────────────────────────────────────────────
        #  搜索发起前的 SDK 环境自检（Fix 3 + Fix 4）
        # ----------------------------------------------------------
        #  goto 返回时只保证 domcontentloaded —— acrawler.js 的 fetch
        #  hook 可能还没装上去；此时直接发 search/single 会拿到没签名
        #  没指纹注入的"裸请求"，反爬秒识破。
        #
        #  这里探测 SDK 的"副作用"作 readiness 信号（不依赖任何内部
        #  全局对象名 —— 那玩意儿随版本会变）：
        #    - localStorage.xmst   ← SDK 写入的 msToken 镜像
        #    - cookie msToken      ← SDK 拉 token 接口后种下
        #  两个都就位 = SDK 已经跑过至少一轮 fetch hook，可以发请求了。
        #
        #  同时把 verifyFp / s_v_web_id / bd_ticket_guard_client_data
        #  的 cookie 状态写进任务日志，便于反爬归因（Fix 4）。
        # ──────────────────────────────────────────────────────────
        self._audit_sdk_environment(browser_session, ctx)
        while len(results) < max_results:

            offset = (page - 1) * self._SEARCH_PAGE_SIZE
            params = self._build_bba_search_params(
                browser_session,
                keyword=keyword,
                offset=offset,
                search_id=search_id,
                sort_type=sort_type,
                publish_time=publish_time,
            )
            data = browser_session.fetch_json(
                self._SEARCH_ENDPOINT,
                params,
                referer=self._build_bba_search_referer(keyword),
                task_context=ctx,
            )

            # ──────────────────────────────────────────────────────────
            #  风控空响应识别 — verify_check / agg_check
            # ----------------------------------------------------------
            #  douyin 在指纹/cookie 被怀疑时会回一个 200 OK + data:[] +
            #  search_nil_info.search_nil_type=verify_check。
            #  这不是"没结果"，是"软封禁"。必须当反爬上报，触发
            #  request_gate 的 ANTI_CRAWL 指数退避（base 翻倍，120s
            #  起步）。否则会被 daemon 当 NATURAL_EMPTY 默默吞掉，
            #  下次任务又拿同一个 cookie + 同一个指纹去送死。
            #
            #  report_anti_crawl() 内部会 raise AntiCrawlDetected，
            #  daemon 错误路径会把它归为 FailureMode.ANTI_CRAWL
            #  并走 retry / cookie escalation 流程。
            # ──────────────────────────────────────────────────────────
            nil_info = data.get("search_nil_info") or {}
            nil_type = str(nil_info.get("search_nil_type") or "")
            if nil_type in ("verify_check", "agg_check"):
                # AntiCrawlDetected is raised from within report_anti_crawl,
                # so this line is unreachable at runtime — but kept explicit
                # for readers: control transfers up to daemon retry layer.
                browser_session.report_anti_crawl(
                    signal=nil_type,
                    platform="douyin",
                    detail=f"keyword={keyword!r} offset={offset}",
                )

            items = data.get("data") or []
            if not items:
                # ──────────────────────────────────────────────────────────
                #  零结果由 daemon 统一处理
                # ----------------------------------------------------------
                #  Daemon's zero-record path calls _log_zero_record_response_dump
                #  unconditionally (both SUCCEEDED-empty and FAILED-empty).
                #  The raw response was already pushed into ctx by
                #  BrowserSession._capture_last_response inside fetch_json.
                #  Nothing to do here — break out and let daemon surface it.
                # ──────────────────────────────────────────────────────────
                break

            search_id = str(data.get("extra", {}).get("logid", "") or search_id)
            for item in items:
                aweme_info = self._extract_bba_aweme_info(item)
                if not aweme_info:
                    continue
                aweme_id = str(aweme_info.get("aweme_id", ""))
                if not aweme_id or aweme_id in seen_ids:
                    continue
                seen_ids.add(aweme_id)
                results.append(self._to_search_result(aweme_info, keyword))
                if len(results) >= max_results:
                    break
            page += 1
        return results

    def _build_bba_search_params(
        self,
        browser_session,
        *,
        keyword: str,
        offset: int,
        search_id: str,
        sort_type: int,
        publish_time: int,
    ) -> dict[str, str | int]:
        # ╔══════════════════════════════════════════════════════════════════╗
        # ║  search/single 请求参数构造 —— 单一真相源原则（2026-06-01 重构）    ║
        # ╠══════════════════════════════════════════════════════════════════╣
        # ║  历史教训：                                                       ║
        # ║    旧版本在这里硬编码了一整套 Mac 指纹（MacIntel / Chrome 125 /    ║
        # ║    Mac OS 10.15.7 / 8 核 / 8GB 等），但实际 jsonl 显示这些值都被  ║
        # ║    抖音 acrawler.js 的 fetch hook 覆盖成宿主真实值（Win / 148 /   ║
        # ║    24核 / 32GB）—— 即"双源真相"，平时靠 SDK 兜底没出事。           ║
        # ║                                                                  ║
        # ║  问题：一旦 SDK 拦截失败（main world race / 未来版本变更），       ║
        # ║    crawlhub 会发出"穿 Mac 西装的 Win 大叔"请求，反爬 1ms 识破。   ║
        # ║                                                                  ║
        # ║  本次修复（"好品味"）：                                          ║
        # ║    1. 把所有指纹字段（browser_*/os_*/cpu_*/device_memory/         ║
        # ║       screen_*/engine_*/platform/cookie_enabled/browser_online/   ║
        # ║       downlink/effective_type/round_trip_time）整体删除            ║
        # ║    2. 这些字段统一由 acrawler.js 在 fetch 拦截时从 navigator/     ║
        # ║       connection 拿真值注入 —— 单一真相源 = 真实浏览器运行时       ║
        # ║    3. crawlhub 只负责传"业务语义"参数（关键词/翻页/筛选）          ║
        # ║                                                                  ║
        # ║  并修正 5 项业务参数与真实浏览器观察值对齐：                        ║
        # ║    search_source       tab_search → normal_search                ║
        # ║    from_group_id       <写死的具体ID> → ""                       ║
        # ║    count               15 → 10                                  ║
        # ║    need_filter_settings 1 → 0                                   ║
        # ║    list_type           multi → single                           ║
        # ╚══════════════════════════════════════════════════════════════════╝
        local_storage = browser_session.local_storage()
        params: dict[str, str | int] = {
            # ── 业务语义参数（必须由 crawlhub 决定）──
            "search_channel": "aweme_general",
            "enable_history": "1",
            "keyword": keyword,
            "search_source": "normal_search",
            "query_correct_type": "1",
            "is_filter_search": "0",
            "from_group_id": "",
            "offset": offset,
            "count": "10",
            "need_filter_settings": "0",
            "list_type": "single",
            "search_id": search_id,
            # ── PC web 应用元信息（acrawler.js 不会改写）──
            "device_platform": "webapp",
            "aid": "6383",
            "channel": "channel_pc_web",
            "version_code": "190600",
            "version_name": "19.6.0",
            "update_version_code": "170400",
            "pc_client_type": "1",
            # ── webid 自生成（每会话独立）──
            "webid": self._generate_bba_webid(),
            # ── msToken：由 cookie 镜像到 localStorage 'xmst'，acrawler 不补 ──
            "msToken": local_storage.get("xmst", ""),
            # 注：以下字段刻意不传，由 acrawler.js fetch hook 从 navigator/
            # connection 真值注入，避免双源真相：
            #   browser_platform / browser_name / browser_version /
            #   browser_language / browser_online / engine_name / engine_version /
            #   os_name / os_version / cpu_core_num / device_memory /
            #   platform / screen_width / screen_height /
            #   cookie_enabled / downlink / effective_type / round_trip_time
        }
        if sort_type != 0 or publish_time != 0:
            params["filter_selected"] = json.dumps({
                "sort_type": str(sort_type),
                "publish_time": str(publish_time),
            })
            params["is_filter_search"] = 1
        return params

    @staticmethod
    def _build_bba_search_page_url(keyword: str, sort_type: int, publish_time: int) -> str:
        url = f"https://www.douyin.com/search/{quote(keyword, safe='')}?type=general"
        filters = []
        if publish_time:
            filters.append(f"publish_time={publish_time}")
        if sort_type:
            filters.append(f"sort_type={sort_type}")
        return url + ("&" + "&".join(filters) if filters else "")

    @staticmethod
    def _build_bba_search_referer(keyword: str) -> str:
        referer = (
            f"https://www.douyin.com/search/{keyword}"
            "?aid=f594bbd9-a0e2-4651-9319-ebe3cb6298c1&type=general"
        )
        return quote(referer, safe=":/")

    # ════════════════════════════════════════════════════════════════
    #  SDK 环境自检 —— 搜索发起前等 acrawler.js 真正接管 fetch
    # ════════════════════════════════════════════════════════════════
    #
    #  设计哲学（"好品味"）：
    #    不去 hardcode SDK 内部全局对象名（byted_acrawler / _gsdk / 任
    #    何叫法），那些会随抖音前端版本悄悄改名 —— 等于把爬虫的命门
    #    交给对方版本号。
    #
    #    转而探测 SDK 的"必然副作用"：
    #      A) localStorage.xmst —— acrawler 在 fetch 拦截时写入的
    #         msToken 镜像；scraper 后续 _build_bba_search_params 也
    #         直接读这个 key（见上方 params['msToken']）。
    #      B) cookie 里 msToken —— SDK 调 mssdk.bytedance.com/token
    #         接口拉到的真 token，请求带 cookie 才有签名。
    #    任一缺失 = SDK 还没初始化完毕 / 被反爬挡了 / 网络异常。
    #
    #    顺便审计反爬强相关 cookie（verifyFp / s_v_web_id /
    #    bd_ticket_guard_client_data）—— 缺了不阻塞，但 ctx.log 写出
    #    来，daemon 端排查反爬时立刻能看到"哦这个会话指纹没起来"。
    #
    #  非阻塞性原则：
    #    8 秒超时上限；超时只 warn 不 raise。让"穿西装的裸请求"自然
    #    撞上 verify_check —— daemon ANTI_CRAWL 路径会处理重试和
    #    cookie 升级，比这里直接抛异常更收敛。
    # ────────────────────────────────────────────────────────────────

    _SDK_AUDIT_SCRIPT = r"""
    async () => {
        const TIMEOUT_MS = 8000;
        const POLL_MS = 100;
        const probe = () => {
            const cookieKV = Object.fromEntries(
                document.cookie.split(';')
                    .map(s => s.trim().split('='))
                    .filter(p => p[0])
            );
            return {
                readyState: document.readyState,
                hasXmst: !!localStorage.getItem('xmst'),
                hasMsToken: 'msToken' in cookieKV,
                hasVerifyFp: 's_v_web_id' in cookieKV || 'verifyFp' in cookieKV,
                hasTicketGuard: 'bd_ticket_guard_client_data' in cookieKV,
                hasOdinTT: 'odin_tt' in cookieKV,
            };
        };
        const start = Date.now();
        while (Date.now() - start < TIMEOUT_MS) {
            const p = probe();
            if (p.readyState === 'complete' && p.hasXmst && p.hasMsToken) {
                return Object.assign({ready: true, elapsed_ms: Date.now() - start}, p);
            }
            await new Promise(r => setTimeout(r, POLL_MS));
        }
        return Object.assign(
            {ready: false, elapsed_ms: Date.now() - start},
            probe()
        );
    }
    """

    def _audit_sdk_environment(self, browser_session, ctx: TaskContext) -> None:
        """SDK readiness + 关键 cookie 完整性自检。

        非阻塞 —— 失败只写 ctx.log，不抛异常。让真实的反爬响应
        （verify_check / agg_check）走 daemon 错误归类路径。
        """
        try:
            info = browser_session.evaluate(self._SDK_AUDIT_SCRIPT)
        except Exception as exc:
            ctx.log(f"[BBA][sdk_audit] evaluate failed: {exc}")
            return
        if not isinstance(info, dict):
            ctx.log(f"[BBA][sdk_audit] unexpected result type: {type(info).__name__}")
            return

        ready = bool(info.get("ready"))
        elapsed = int(info.get("elapsed_ms") or 0)
        readyState = info.get("readyState")
        has_xmst = bool(info.get("hasXmst"))
        has_mstoken = bool(info.get("hasMsToken"))
        has_verifyfp = bool(info.get("hasVerifyFp"))
        has_ticketguard = bool(info.get("hasTicketGuard"))
        has_odin = bool(info.get("hasOdinTT"))

        prefix = "[BBA][sdk_audit]"
        if ready:
            ctx.log(
                f"{prefix} ready in {elapsed}ms "
                f"| verifyFp={has_verifyfp} ticketGuard={has_ticketguard} "
                f"odin_tt={has_odin}"
            )
        else:
            ctx.log(
                f"{prefix} WARN not ready after {elapsed}ms "
                f"| readyState={readyState!r} xmst={has_xmst} "
                f"msToken={has_mstoken} verifyFp={has_verifyfp}"
            )

        # ── verifyFp 缺失独立 warn ──
        # 即便 SDK 已 ready，老会话也可能没 s_v_web_id（首次加载
        # 时机敏感）。打日志单独提一笔，让排查反爬时更直接。
        if not has_verifyfp:
            ctx.log(
                f"{prefix} WARN cookie missing verifyFp/s_v_web_id "
                "— session fingerprint may be incomplete"
            )

    @staticmethod
    def _generate_bba_webid() -> str:

        def repl(token: int | None) -> str:
            if token is not None:
                return str(token ^ (int(16 * random.random()) >> (token // 4)))
            return "".join([str(int(1e7)), "-", str(int(1e3)), "-", str(int(4e3)), "-", str(int(8e3)), "-", str(int(1e11))])
        return "".join(repl(int(ch)) if ch in "018" else ch for ch in repl(None)).replace("-", "")[:19]

    @staticmethod
    def _extract_bba_aweme_info(item: dict) -> dict | None:
        if not isinstance(item, dict):
            return None
        aweme_info = item.get("aweme_info")
        if isinstance(aweme_info, dict):
            return aweme_info
        mix_items = item.get("aweme_mix_info", {}).get("mix_items") if isinstance(item.get("aweme_mix_info"), dict) else None
        if isinstance(mix_items, list) and mix_items and isinstance(mix_items[0], dict):
            return mix_items[0]
        return None

    @staticmethod
    def _to_search_result(aweme_info: dict, keyword: str) -> SearchResult:
        stats = aweme_info.get("statistics", {}) or {}
        author = aweme_info.get("author", {}) or {}
        return SearchResult(
            aweme_id=str(aweme_info.get("aweme_id", "")),
            title=aweme_info.get("desc", ""),
            like_count=stats.get("digg_count", 0),
            comment_count=stats.get("comment_count", 0),
            share_count=stats.get("share_count", 0),
            author_uid=str(author.get("uid", "")),
            author_name=author.get("name", ""),
            keyword=keyword,
        )

    def _save_search_results(self, keyword: str, results: list[SearchResult]):
        """Save search results to txt and JSONL files (CLI helper)."""
        if self.output_dir is None:
            raise ValueError(
                "DouyinScraper.output_dir is not set; pass output_dir=... "
                "to the constructor before calling search_videos(save_to_file=True)."
            )
        self.output_dir.mkdir(parents=True, exist_ok=True)
        safe_kw = re.sub(r'[/\\:*?"<>|]', '_', keyword)[:30]
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")

        # Save ID list (txt)
        txt_path = self.output_dir / f"search_{safe_kw}_{ts}.txt"
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write(f"# Keyword: {keyword}\n")
            f.write(f"# Time: {datetime.now().isoformat()}\n")
            f.write(f"# Total: {len(results)}\n\n")
            for r in results:
                f.write(f"{r.aweme_id}  # {r.author_name}: {r.desc[:50]}\n")

        # Save full info (JSONL)
        jsonl_path = self.output_dir / f"search_{safe_kw}_{ts}.jsonl"
        with open(jsonl_path, "w", encoding="utf-8") as f:
            for r in results:
                f.write(json.dumps(asdict(r), ensure_ascii=False) + "\n")

        self._log(f"  [FILE] Saved: {txt_path.name} / {jsonl_path.name}")

    # ── Live Room Search ──────────────────────────────────────

    # TODO(R3-ghost): suggest add to plugin.yaml as action `search_live_rooms_via_browser` (independent capability, browser path); awaiting decision
    async def search_live_rooms_via_browser(
        self,
        keyword: str,
        max_results: int = 20,
        headless: bool = False,
    ) -> list[LiveRoom]:
        """Search live rooms via Playwright browser.

        Uses Douyin search page with type=live, intercepting API responses.

        Args:
            keyword:     Search keyword.
            max_results: Maximum number of live room results.
            headless:    Whether to run browser in headless mode.

        Returns:
            List of LiveRoom.
        """
        from urllib.parse import quote as url_quote

        try:
            from playwright.async_api import async_playwright
        except ImportError:
            raise RuntimeError(
                "Playwright not installed. Run:\n"
                "  pip install playwright && playwright install chromium"
            )

        self._log(f"[INFO] Browser live search: '{keyword}' (max {max_results})")

        encoded_kw = url_quote(keyword, safe='')
        search_url = f"https://www.douyin.com/search/{encoded_kw}?type=live"

        collected_items: list[dict] = []
        seen_ids: set[str] = set()

        async def _on_response(response):
            url = response.url
            if 'search' not in url or 'live' not in url:
                return
            ct = response.headers.get('content-type', '')
            if 'json' not in ct:
                return
            try:
                body = await response.json()
                items = body.get("data", [])
                if not isinstance(items, list):
                    return
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    lives = item.get("lives") or item.get("live_info") or {}
                    if not lives and item.get("room_id"):
                        lives = item
                    if not lives:
                        continue
                    rid = str(lives.get("room_id", "") or item.get("room_id", ""))
                    if not rid or rid in seen_ids:
                        continue
                    seen_ids.add(rid)
                    collected_items.append({
                        "room_id": rid,
                        "lives": lives,
                        "item": item,
                    })
                if collected_items:
                    self._log(f"  [live intercept] total {len(collected_items)} rooms")
            except Exception:
                pass

        browser = None
        pw = None
        try:
            from crawlhub.core.browser.playwright_runtime import _STEALTH_LAUNCH_ARGS
            pw = await async_playwright().start()
            browser = await pw.chromium.launch(
                headless=headless,
                # 使用统一 stealth args（含按平台分发的 sandbox 处理）
                # 历史教训：曾各自硬编码 ["--no-sandbox", ...] 导致 Windows 上
                # 触发 Chrome unsafe-flag infobar，反爬 SDK 可探测视口偏移。
                args=list(_STEALTH_LAUNCH_ARGS),
            )
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/146.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1536, "height": 864},
                locale="zh-CN",
            )

            # Inject saved cookies
            cookie_path = Path(self.cookie_path)
            if cookie_path.exists():
                with open(cookie_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                cookie_dict = data.get("cookies", {})
                if cookie_dict:
                    pw_cookies = [
                        {"name": k, "value": v, "domain": ".douyin.com", "path": "/"}
                        for k, v in cookie_dict.items()
                    ]
                    await context.add_cookies(pw_cookies)

            page = await context.new_page()
            page.on("response", _on_response)

            self._log(f"  Navigating to live search page...")
            try:
                await page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                self._log(f"  Navigation timeout (continuing): {e}")

            await page.wait_for_timeout(5000)

            # Scroll to load more
            max_scroll = max(1, (max_results // 10) + 3)
            no_new = 0
            for i in range(max_scroll):
                if len(collected_items) >= max_results:
                    break
                prev = len(collected_items)
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await page.wait_for_timeout(3000)
                if len(collected_items) == prev:
                    no_new += 1
                    if no_new >= 3:
                        break
                else:
                    no_new = 0

        except Exception as e:
            self._log(f"[FAIL] Browser live search error: {e}")
            raise
        finally:
            if browser:
                await browser.close()
            if pw:
                await pw.stop()

        # Convert to LiveRoom
        results: list[LiveRoom] = []
        for entry in collected_items[:max_results]:
            lives = entry["lives"]
            item = entry["item"]
            # Extract name from various possible locations
            nick = (lives.get("name", "") or
                    (item.get("author", {}) or {}).get("name", "") or
                    lives.get("user_info", {}).get("name", "") or "")
            results.append(LiveRoom(
                room_id=entry["room_id"],
                user_uid=str(
                    lives.get("user_uid", "") or
                    (item.get("author", {}) or {}).get("uid", "") or
                    lives.get("user_info", {}).get("uid", "") or ""
                ),
                name=nick,
                title=lives.get("title", "") or item.get("desc", "") or "",
                online_count=int(lives.get("user_count", 0) or lives.get("online_count", 0) or 0),
                cover_url=str(
                    lives.get("cover", {}).get("url_list", [""])[0]
                    if isinstance(lives.get("cover"), dict) else ""
                ),
                keyword=keyword,
            ))

        self._log(f"[OK] Browser live search done: '{keyword}' -> {len(results)} rooms")
        return results

    # ── Video Info ───────────────────────────────────────────

    def _fetch_video_info(self, sdk: DouyinSDK, aweme_id: str) -> VideoInfo:
        """Fetch video basic information."""
        try:
            data = sdk.video_detail(aweme_id)
            detail = data.get("aweme_detail", {}) or {}
            statistics = detail.get("statistics", {}) or {}
            author = detail.get("author", {}) or {}

            return VideoInfo(
                aweme_id=aweme_id,
                title=detail.get("desc", ""),
                digg_count=statistics.get("digg_count", 0),
                comment_count=statistics.get("comment_count", 0),
                share_count=statistics.get("share_count", 0),
                collect_count=statistics.get("collect_count", 0),
                author_uid=str(author.get("uid", "")),
                author_name=author.get("name", ""),
                author_sec_uid=author.get("sec_uid", ""),
                duration=detail.get("duration", 0),
                create_time=detail.get("create_time", 0),
                fetched_at=datetime.now().isoformat(),
            )
        except Exception as e:
            self._log(f"  [{aweme_id}] Video info failed: {e}")
            return VideoInfo(aweme_id=aweme_id, fetched_at=datetime.now().isoformat())

    # ── Comment Scraping ─────────────────────────────────────

    def _fetch_comments_all(
        self, sdk: DouyinSDK, aweme_id: str, max_comments: Optional[int]
    ) -> tuple[list[Comment], int]:
        """Fetch all root comments with pagination.

        Returns:
            (comments_list, total_pages)
        """
        all_comments: list[Comment] = []
        cursor = 0
        page_num = 0

        while True:
            page_num += 1
            try:
                data = sdk.comment_list(aweme_id, cursor=cursor, count=20, sort=self.comment_sort)

                comments_raw = data.get("comments") or []
                if not comments_raw:
                    # Log response for debugging when first page returns empty
                    if page_num == 1:
                        data_str = json.dumps(data, ensure_ascii=False)
                        self._log(f"  [{aweme_id}] Page 1 empty, response: {data_str[:500]}")
                    break

                for c in comments_raw:
                    user = c.get("user", {}) or {}
                    all_comments.append(Comment(
                        cid=str(c.get("cid", "")),
                        text=c.get("text", ""),
                        aweme_id=aweme_id,
                        create_time=c.get("create_time", 0),
                        digg_count=c.get("digg_count", 0),
                        reply_comment_total=c.get("reply_comment_total", 0),
                        user_id=str(user.get("uid", "")),
                        user_nickname=user.get("name", ""),
                        user_sec_uid=user.get("sec_uid", ""),
                        is_author_digged=bool(c.get("is_author_digged", False)),
                    ))

                    if max_comments and len(all_comments) >= max_comments:
                        self._log(f"  [{aweme_id}] Reached limit {max_comments}")
                        return all_comments[:max_comments], page_num

                has_more = data.get("has_more", 0)
                new_cursor = data.get("cursor", 0)

                if not has_more or new_cursor == cursor:
                    self._log(f"  [{aweme_id}] Page {page_num}: end of comments")
                    break

                cursor = new_cursor
                self._log(
                    f"  [{aweme_id}] Page {page_num}: +{len(comments_raw)}, "
                    f"total {len(all_comments)}"
                )
                time.sleep(self.page_delay)

            except Exception as e:
                self._log(f"  [{aweme_id}] Page {page_num} failed: {e}")
                break

        return all_comments, page_num

    # NOTE: Sub-comment (reply) fetching is disabled — requires bd-ticket-guard
    # headers that we cannot generate via pure Python. Sub-comment volume is small,
    # so we skip it for now and only collect root-level comments.

    # ── Public API (used by crawlhub bridge) ─────────────────

    # TODO(R3-ghost): renamed from public `get_video_detail` -> internal raw
    # helper used by the R3 public action `get_video_detail(ctx, params)` below.
    def _get_video_detail_raw(self, video_id: str) -> VideoInfo:
        """Fetch only video info for a single video (no comments).

        Public API consumed by ``DouyinBridge.get_video_detail``.
        For full scrape (info + comments) use ``scrape_one``.
        """
        aweme_id = self._parse_aweme_id(video_id)
        sdk = self._get_sdk()
        return self._fetch_video_info(sdk, aweme_id)

    # TODO(R3-ghost): suggest rename to `_fetch_comments` (internal helper used by scrape_comments); awaiting decision
    def fetch_comments(
        self,
        video_id: str,
        max_count: Optional[int] = None,
        sort_type: int = 0,
    ) -> list[Comment]:
        """Fetch root-level comments for a single video.

        Public API consumed by ``DouyinBridge.scrape_comments``.

        Args:
            video_id:  Video ID or URL.
            max_count: Max comments to fetch; None = unlimited.
            sort_type: 0=hot, 1=newest (overrides instance ``comment_sort``).
        """
        aweme_id = self._parse_aweme_id(video_id)
        sdk = self._get_sdk()
        # honor caller-specified sort by temporarily swapping
        original_sort = self.comment_sort
        self.comment_sort = sort_type
        try:
            comments, _pages = self._fetch_comments_all(sdk, aweme_id, max_count)
        finally:
            self.comment_sort = original_sort
        return comments

    # ── Single Video Scrape ──────────────────────────────────

    # TODO(R3-ghost): suggest rename to `_scrape_one` (internal, called by scrape_video action); awaiting decision
    def scrape_one(self, raw_input: str) -> VideoResult:
        """Scrape comments for a single video.

        Args:
            raw_input: Video ID or URL.

        Returns:
            VideoResult with all data.
        """
        aweme_id = self._parse_aweme_id(raw_input)
        result = VideoResult(aweme_id=aweme_id, input_raw=raw_input)
        t0 = time.time()

        self._log(f"[INFO] Starting: {aweme_id}")
        try:
            sdk = self._get_sdk()

            # Fetch video info
            result.video_info = self._fetch_video_info(sdk, aweme_id)

            # Fetch root comments
            comments, total_pages = self._fetch_comments_all(
                sdk, aweme_id, self.max_comments
            )

            result.comments = list(comments)
            result.total_fetched = len(comments)
            result.total_pages = total_pages
            result.status = "ok"
            result.elapsed_s = round(time.time() - t0, 2)

            self._log(
                f"[OK] Done: {aweme_id} | {result.total_fetched} comments "
                f"/ {total_pages} pages | {result.elapsed_s}s"
            )
        except Exception as e:
            result.status = "error"
            result.error = str(e)
            result.elapsed_s = round(time.time() - t0, 2)
            self._log(f"[FAIL] Failed: {aweme_id} | {e}")

        return result

    # ── Save ─────────────────────────────────────────────────

    # TODO(R3-ghost): suggest rename to `_save_result` (CLI-only helper, ctx.write_record preferred in R3); awaiting decision
    def save_result(self, result: VideoResult):
        """Save a single video result to JSONL file (CLI helper)."""
        if self.output_dir is None:
            raise ValueError(
                "DouyinScraper.output_dir is not set; pass output_dir=... "
                "to the constructor before calling save_result()."
            )
        self.output_dir.mkdir(parents=True, exist_ok=True)
        filename = self.output_dir / f"{result.aweme_id}.jsonl"
        with open(filename, "w", encoding="utf-8") as f:
            # Write metadata line
            meta = {
                "type": "video_info",
                "aweme_id": result.aweme_id,
                "input_raw": result.input_raw,
                "status": result.status,
                "error": result.error,
                "total_fetched": result.total_fetched,
                "total_pages": result.total_pages,
                "elapsed_s": result.elapsed_s,
                "scraped_at": datetime.now().isoformat(),
                "video_info": result.video_info.to_dict() if result.video_info else {},
            }
            f.write(json.dumps(meta, ensure_ascii=False) + "\n")
            # Write each comment
            for c in result.comments:
                f.write(json.dumps(c.to_dict() if hasattr(c, 'to_dict') else c, ensure_ascii=False) + "\n")
        self._log(f"  [FILE] Saved: {filename.name} ({result.total_fetched} comments)")

    # ── Helpers ──────────────────────────────────────────────

    @staticmethod
    def _parse_aweme_id(raw_input: str) -> str:
        """Extract video ID from URL or raw string.

        Supports:
          - Pure ID: 7615062955545169202
          - Full URL: https://www.douyin.com/video/7615062955545169202
          - Modal URL: https://www.douyin.com/jingxuan?modal_id=7615062955545169202
          - Short URL: https://v.douyin.com/xxxxx
        """
        raw = raw_input.strip()

        # Pure numeric ID
        if raw.isdigit():
            return raw

        # URL pattern: modal_id query parameter (jingxuan, search, etc.)
        match = re.search(r'[?&]modal_id=(\d+)', raw)
        if match:
            return match.group(1)

        # URL pattern: /video/{id}
        match = re.search(r'/video/(\d+)', raw)
        if match:
            return match.group(1)

        # URL pattern: note or other patterns with numeric ID in path
        match = re.search(r'/(\d{15,})(?:\?|$|/)', raw)
        if match:
            return match.group(1)

        # Fallback: return as-is (might be a short URL, would need redirect)
        return raw

    # ══════════════════════════════════════════════════════════
    #  R3 PUBLIC ACTIONS - signature `(self, ctx, params) -> None`
    #  These are the dispatch targets for service.execute().
    # ══════════════════════════════════════════════════════════

    def scrape_comments(self, ctx: TaskContext, params: dict) -> None:
        """Action: scrape_comments - fetch comments for a single douyin video."""
        video_id = params["video_id"]
        assert isinstance(video_id, str), (
            f"scrape_comments expects single video_id (str), got {type(video_id).__name__}"
        )
        max_comments = params.get("max_comments") or 200
        # comment_sort can come in as int or string from JSON params.
        raw_sort = params.get("comment_sort", 0)
        try:
            sort_type = int(raw_sort)
        except (TypeError, ValueError):
            sort_type = 0

        self.ensure_cookie()
        cookie_path = self._resolved_cookie_path()
        ctx.log(f"cookie: path={cookie_path}, exists={Path(cookie_path).exists()}")

        ctx.check_cancelled()
        ctx.log(f"Scraping douyin video: {video_id}")

        comments = self.fetch_comments(
            video_id=video_id,
            max_count=max_comments,
            sort_type=sort_type,
        )
        ctx.log(f"  result: fetched={len(comments)}")

        written = 0
        for comment in comments:
            ctx.check_cancelled()
            record = comment if isinstance(comment, dict) else comment.to_dict()
            record["_source_video"] = video_id
            ctx.write_record(record)
            written += 1

        ctx.log(f"  [OK] {video_id}: written={written}")
        ctx.set_progress(1.0)

    def search_videos(self, ctx: TaskContext, params: dict) -> None:
        """Action: search_videos - BBA keyword search.

        R7: with runtime.browser.hold() as bba_page —— hold 包整个翻页循环
        （高频借还场景，page 全程独占给本 action）。
        """
        import logging as _logging
        _scraper_log = _logging.getLogger(__name__)
        task_tag = str(getattr(ctx, "task_id", ""))[:12] or "<no_ctx>"
        keyword = params["keyword"]
        max_results = int(params.get("max_results", 20))
        sort_type = int(params.get("sort_type", 0))
        publish_time = int(params.get("publish_time", 0))

        runtime = get_current_runtime()
        if runtime is None or runtime.browser is None:
            raise RuntimeError("search_videos requires browser_backed runtime")

        _scraper_log.info(
            "[BBA] scraper.enter task=%s action=search_videos kw=%r scraper_id=%x",
            task_tag, keyword, id(self),
        )

        ctx.check_cancelled()
        ctx.log(f"Searching douyin with BBA: '{keyword}' (max {max_results})")

        with runtime.browser.hold() as bba_page:
            results = self._search_videos_bba(
                bba_page,
                ctx=ctx,
                keyword=keyword,
                max_results=max_results,
                sort_type=sort_type,
                publish_time=publish_time,
            )

        for r in results:
            ctx.check_cancelled()
            ctx.write_record(r.to_dict())

        ctx.log(f"  [OK] Found {len(results)} results for '{keyword}'")
        ctx.set_progress(1.0)


    def search_live_rooms(self, ctx: TaskContext, params: dict) -> None:
        """Action: search_live_rooms — search Douyin live rooms.

        Params:
          - live_only: bool, drop offline rooms client-side (default false)
          - sort_by: "default" | "user_count" (descending popularity)
        """
        keyword = params["keyword"]
        max_results = int(params.get("max_results", 20) or 20)
        live_only = bool(params.get("live_only", False))
        sort_by = str(params.get("sort_by", "default") or "default")

        runtime = get_current_runtime()
        if runtime is None or runtime.browser is None:
            raise RuntimeError("search_live_rooms requires browser_backed runtime")

        with runtime.browser.hold() as bba_page:
            rows = self._get_sdk().search_live_rooms(
                bba_page, keyword, max_results,
                live_only=live_only, sort_by=sort_by,
            )
        for row in rows:
            ctx.check_cancelled()
            record = LiveRoomSearchResult(
                web_rid=str(row.get("web_rid") or ""),
                room_id=str(row.get("room_id") or ""),
                title=str(row.get("title") or ""),
                user_count=int(row.get("user_count") or 0),
                status=int(row.get("status") or 0),
                author_nickname=str(row.get("author_nickname") or ""),
                author_uid=str(row.get("author_uid") or ""),
                author_sec_uid=str(row.get("author_sec_uid") or ""),
                cover_url=str(row.get("cover_url") or ""),
                stream_flv=str(row.get("stream_flv") or ""),
                stream_hls=str(row.get("stream_hls") or ""),
                keyword=keyword,
            )
            ctx.write_record(record.to_dict())
        ctx.set_progress(1.0)

    def get_live_room_info(self, ctx: TaskContext, params: dict) -> None:
        """Action: get_live_room_info — fetch Douyin live room detail."""
        web_rid = params["web_rid"]
        runtime = get_current_runtime()
        if runtime is None or runtime.browser is None:
            raise RuntimeError("get_live_room_info requires browser_backed runtime")
        with runtime.browser.hold() as bba_page:
            row = self._get_sdk().get_live_room_info(bba_page, web_rid)
        record = LiveRoomInfoRecord(
            web_rid=str(row.get("web_rid") or ""),
            room_id=str(row.get("room_id") or ""),
            status=int(row.get("status") or 0),
            status_str=str(row.get("status_str") or ""),
            title=str(row.get("title") or ""),
            user_count=int(row.get("user_count") or 0),
            user_count_str=str(row.get("user_count_str") or ""),
            like_count=int(row.get("like_count") or 0),
            owner_uid=str(row.get("owner_uid") or ""),
            owner_sec_uid=str(row.get("owner_sec_uid") or ""),
            owner_nickname=str(row.get("owner_nickname") or ""),
            cover_url=str(row.get("cover_url") or ""),
            stream_flv_origin=str(row.get("stream_flv_origin") or ""),
            stream_hls_origin=str(row.get("stream_hls_origin") or ""),
            fetched_at=str(row.get("fetched_at") or ""),
        )
        ctx.write_record(record.to_dict())
        ctx.set_progress(1.0)

    def collect_live_events(self, ctx: TaskContext, params: dict) -> None:
        """Action: collect_live_events — collect Douyin live WSS events.

        R7: scraper 把 browser_provider 传给 SDK，SDK 内 bootstrap 单 hold
        然后释放——剩下 24h 纯 Python WSS 期间不 hold（chrome 自动关闭）。
        wait_until_live 模式由 SDK 内单 hold 包整个 while 循环（避免反复冷启动）。

        Stop conditions are auto (no user-facing toggle):
          1. duration_seconds reached
          2. task cancelled
          3. WSS connection closed
          4. WebcastControlMessage status=3 (live ended)
        """
        runtime = get_current_runtime()
        if runtime is None or runtime.browser is None:
            raise RuntimeError("collect_live_events requires browser_backed runtime")
        web_rid = params["web_rid"]
        duration = float(params.get("duration_seconds") or 0)  # 0 = run until live ends
        wait_until_live = bool(params.get("wait_until_live", False))
        wait_timeout = float(params.get("wait_timeout_seconds", 86400) or 86400)

        # event_filter: list of cmd names from frontend (already raw english names)
        raw_filter = params.get("event_filter")
        event_filter: set[str] | None = None
        if raw_filter and isinstance(raw_filter, list) and len(raw_filter) > 0:
            event_filter = {str(x) for x in raw_filter if x}

        started = time.monotonic()

        def emit(event: dict) -> None:
            ctx.check_cancelled()
            record = LiveEventRecord(
                web_rid=str(event.get("web_rid") or web_rid),
                event_type=str(event.get("event_type") or "raw"),
                raw_cmd=str(event.get("raw_cmd") or ""),
                uid=str(event.get("uid") or ""),
                nickname=str(event.get("nickname") or ""),
                content=str(event.get("content") or ""),
                online_count=int(event.get("online_count") or 0),
                like_count=int(event.get("like_count") or 0),
                gift_id=str(event.get("gift_id") or ""),
                gift_count=int(event.get("gift_count") or 0),
                ts=round(time.monotonic() - started, 3),
                payload=json.dumps(event.get("payload") or {}, ensure_ascii=False),
            )
            ctx.write_record(record.to_dict())
            if duration > 0:
                ctx.set_progress(min(0.99, (time.monotonic() - started) / duration))

        count = self._get_sdk().collect_live_events(
            browser_provider=runtime.browser,
            web_rid=web_rid,
            duration_seconds=duration,
            on_event=emit,
            is_cancelled=lambda: ctx.is_cancelled,
            event_filter=event_filter,
            wait_until_live=wait_until_live,
            wait_timeout_seconds=wait_timeout,
        )
        ctx.log(f"  [OK] web_rid={web_rid}: collected {count} live events")
        ctx.set_progress(1.0)

    def get_video_detail(self, ctx: TaskContext, params: dict) -> None:

        """Action: get_video_detail - fetch VideoInfo for a single douyin video."""
        video_id = params["video_id"]

        self.ensure_cookie()
        ctx.check_cancelled()
        ctx.log(f"[douyin] Fetching video detail: {video_id}")

        try:
            info = self._get_video_detail_raw(video_id)
            record = info if isinstance(info, dict) else info.to_dict()
            ctx.write_record(record)
            ctx.log(f"  [OK] {video_id}: {record.get('title', '')[:50]}")
        except Exception as e:
            ctx.record_error(f"{video_id}: {e}", response=e)
            ctx.log(f"  [ERR] {video_id}: {e}")

        ctx.set_progress(1.0)

