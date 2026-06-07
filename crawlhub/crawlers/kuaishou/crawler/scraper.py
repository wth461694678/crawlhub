"""
快手爬虫核心模块
================
功能：
  - 登录 / 登录状态检查
  - 搜索关键词 → 获取视频 ID 清单
  - 获取视频基础信息
  - 批量并发爬取评论（限制 N 条 / 全量翻页）
  - 结果保存为 JSONL + 汇总 JSON

推荐保守值（比 Selenium 快，不触发风控）：
  workers=3, page_delay=1.0s, video_delay=0.5s
"""

from __future__ import annotations

import json
import sys
import time
import threading

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Optional

from crawlhub.core.cookie_resolver import (
    CookieResolverMixin,
    CookieNotReady,
)
from crawlhub.core.task_context import TaskContext
from crawlhub.core.platform import get_current_runtime


from .client import KuaishouSDK
from .utils import parse_video_id, load_ids_from_file
from .rate_limiter import GlobalRateLimiter
from .models import (
    VideoInfo, Comment, SearchResult, VideoResult,
    LiveCategoryItem, LiveCategorySearchResult, LiveRoomSearchResult,
    LiveRoomInfoRecord, LiveEventRecord,
)




# ════════════════════════════════════════════════════════════
#  核心爬虫
# ════════════════════════════════════════════════════════════

class KuaishouScraper(CookieResolverMixin):
    """快手爬虫主类（pure generator/return — 不主动写盘）。

    Persistence is the caller's job: the platform service iterates the
    yielded records and pipes them through ``ctx.write_record()``.  All
    request-level diagnostics are funneled into ``log_dir`` (per-task,
    injected by the service from ``ctx.output_dir``).

    Args:
        cookie_path:  cookie 文件路径（None = SDK 自找；建议由 service 注入）
        log_dir:      请求日志目录（None = 不写文件日志；R3 在每次 action 通过 ctx.output_dir 注入）
        workers:      并发线程数（推荐保守值 3）
        page_delay:   同一视频翻页间隔秒数（推荐 1.0）
        video_delay:  不同视频之间的启动间隔秒数（推荐 0.5）
        max_comments: 每个视频最多爬取评论数，None = 全量
    """

    DEFAULT_WORKERS = 3
    DEFAULT_PAGE_DELAY = 1.0
    DEFAULT_VIDEO_DELAY = 0.5
    PLATFORM_NAME = "kuaishou"

    def __init__(
        self,
        cookie_path: str = None,
        log_dir=None,
        workers: int = DEFAULT_WORKERS,
        page_delay: float = DEFAULT_PAGE_DELAY,
        video_delay: float = DEFAULT_VIDEO_DELAY,
        max_comments: Optional[int] = None,
        include_sub_comments: bool = False,
        log_callback=None,
    ):
        # log_dir is optional in R3 mode: the platform service injects a
        # per-task ctx.output_dir into ``self.log_dir`` right before each
        # action call. Construction must succeed without a task context so
        # that the singleton scraper held by KuaishouService can be built
        # at import time.
        self._explicit_cookie_path = cookie_path
        self.log_dir = Path(log_dir) if log_dir else None

        self.workers = workers
        self.page_delay = page_delay
        self.video_delay = video_delay
        self.max_comments = max_comments
        self.include_sub_comments = include_sub_comments

        self._sdk_local = threading.local()
        self._sdk_local_key = None  # tracks (cookie_path, log_dir) to detect changes
        self._print_lock = threading.Lock()
        self._rate_limiter = GlobalRateLimiter.instance()
        self._log_callback = log_callback

        # NOTE: do NOT call self._log() here.  Construction must be free
        # of stdout/stderr side effects because the platform service may
        # be instantiated from non-task threads (FastAPI handlers, cookie
        # probes, etc.).
        import logging as _logging
        _logging.getLogger(__name__).debug(
            "KuaishouScraper init | workers=%s page_delay=%ss "
            "video_delay=%ss max_comments=%s include_sub_comments=%s",
            workers, page_delay, video_delay,
            max_comments or "all", include_sub_comments,
        )

    # ── 内部工具 ─────────────────────────────────────────────

    def _resolved_cookie_path(self) -> Optional[str]:
        """Resolve cookie path (honors thread-local override / mixin)."""
        if self._explicit_cookie_path:
            return self._explicit_cookie_path
        try:
            return str(self.resolve_cookie_path())
        except Exception:
            return None

    @property
    def cookie_path(self) -> Optional[str]:
        """Legacy attribute kept for compatibility."""
        return self._resolved_cookie_path()

    def check_cookie_valid(self) -> bool:
        """CookieResolverMixin hook -- raises CookieNotReady if no cookie."""
        crawler_path = self.get_crawler_cookie_path()
        if crawler_path.exists():
            return True
        fallback = self.get_cookie_path()
        if fallback.exists():
            return True
        raise CookieNotReady("kuaishou", "No cookie file found. Please login first.")

    def _get_sdk(self) -> KuaishouSDK:
        """获取当前线程的 SDK 实例（懒初始化）。

        Re-creates the SDK if either the resolved cookie path or
        ``self.log_dir`` changed since last call, so that per-task daemon
        thread overrides AND per-task output_dir injection both take effect.
        """
        cookie_path = self._resolved_cookie_path()
        log_dir = self.log_dir
        cur_key = (cookie_path, str(log_dir) if log_dir else None)
        cached_sdk = getattr(self._sdk_local, "sdk", None)
        cached_key = getattr(self._sdk_local, "key", None)
        if cached_sdk is None or cached_key != cur_key:
            self._sdk_local.sdk = KuaishouSDK(
                cookie_path=cookie_path,
                log_prefix=f"crawler_t{threading.get_ident() % 10000}",
                log_dir=log_dir,
            )
            self._sdk_local.key = cur_key
        return self._sdk_local.sdk

    def _log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        with self._print_lock:
            try:
                print(line, file=sys.stderr)
            except UnicodeEncodeError:
                print(line.encode("utf-8", errors="replace").decode("utf-8"), file=sys.stderr)
        if self._log_callback:
            try:
                self._log_callback(msg)
            except Exception:
                pass

    # ── 登录 ────────────────────────────────────────────────

    # TODO(R3-ghost): suggest add to plugin.yaml as action `login` (QR code login is an independent capability); awaiting decision
    def login(self) -> bool:
        """扫码登录（主线程调用，会弹出二维码）。"""
        sdk = KuaishouSDK(
            cookie_path=self.cookie_path, log_prefix="login", log_dir=self.log_dir
        )
        ok = sdk.qr_login()
        if ok:
            data = sdk.verify_login()
            self._log(f"[OK] 登录成功: {data.get('userName', '')} (result={data.get('result')})")
        else:
            self._log("[ERR] 登录失败")
        return ok

    # TODO(R3-ghost): suggest add to plugin.yaml as action `sms_send_code` (paired with sms_verify_code for SMS login flow); awaiting decision
    def sms_send_code(self, phone: str, country_code: str = "+86") -> bool:
        """手机验证码登录第一步：发送短信验证码。

        Args:
            phone:        手机号（11位，不含国家码）
            country_code: 国家码（默认 +86）

        Returns:
            True 发送成功, False 失败
        """
        sdk = KuaishouSDK(
            cookie_path=self.cookie_path, log_prefix="sms_login", log_dir=self.log_dir
        )
        # 将 sdk 实例缓存到主线程 local，供 sms_verify_code 复用同一 session
        self._sms_sdk = sdk
        ok = sdk.sms_send_code(phone, country_code)
        if ok:
            self._log(f"[OK] 验证码已发送至 {country_code}{phone}")
        else:
            self._log(f"[ERR] 发送验证码失败")
        return ok

    # TODO(R3-ghost): suggest add to plugin.yaml as action `sms_verify_code` (paired with sms_send_code for SMS login flow); awaiting decision
    def sms_verify_code(self, phone: str, sms_code: str, country_code: str = "+86") -> bool:
        """手机验证码登录第二步：提交验证码完成登录。

        Args:
            phone:        手机号（与发送时一致）
            sms_code:     收到的短信验证码
            country_code: 国家码（默认 +86）

        Returns:
            True 登录成功, False 失败
        """
        # 优先复用发送验证码时的 sdk 实例（同一 session/cookie jar）
        sdk = getattr(self, "_sms_sdk", None) or KuaishouSDK(
            cookie_path=self.cookie_path, log_prefix="sms_login", log_dir=self.log_dir
        )
        ok = sdk.sms_verify_code(phone, sms_code, country_code)
        if ok:
            data = sdk.verify_login()
            self._log(f"[OK] 登录成功: {data.get('userName', '')} (result={data.get('result')})")
            self._sms_sdk = None  # 清理缓存
        else:
            self._log("[ERR] 验证码登录失败")
        return ok

    # TODO(R3-ghost): suggest rename to `_check_login` (internal cookie self-check); awaiting decision
    def check_login(self) -> bool:
        """检查当前 cookie 是否有效。"""
        try:
            sdk = self._get_sdk()
            data = sdk.verify_login()
            ok = data.get("result") == 1
            user = data.get("userName", "")
            self._log(f"{'[OK]' if ok else '[ERR]'} 登录状态: result={data.get('result')} user={user}")
            return ok
        except Exception as e:
            self._log(f"[ERR] 登录检查失败: {e}")
            return False

    # ── 视频详情 ─────────────────────────────────────────────

    # TODO(R3-ghost): renamed from public `get_video_detail` -> internal raw
    # helper used by the R3 public action `get_video_detail(ctx, params)` below.
    def _get_video_detail_raw(self, video_id: str) -> VideoInfo:
        """Get video detail info (title, author, likes, views, comments, etc).

        Args:
            video_id: photo_id or URL

        Returns:
            VideoInfo dataclass
        """
        photo_id = parse_video_id(video_id)
        if not photo_id:
            raise ValueError(f"Cannot parse video ID from: {video_id}")

        sdk = self._get_sdk()
        self._rate_limiter.acquire()
        data = sdk.video_detail(photo_id)

        vd = (data.get("data") or {}).get("visionVideoDetail") or {}
        photo = vd.get("photo") or {}
        author = vd.get("author") or {}
        tags_raw = vd.get("tags") or []

        if not photo:
            raise ValueError(f"Video not found or deleted: {photo_id}")

        info = VideoInfo(
            photo_id=photo.get("id", photo_id),
            name=photo.get("caption", ""),
            like_count=photo.get("likeCount", 0),
            view_count=photo.get("viewCount", 0),
            comment_count=0,  # not returned by this API
            author_id=author.get("id", ""),
            author_name=author.get("name", ""),
            duration=photo.get("duration", 0),
            tags=[t.get("name", "") for t in tags_raw if t.get("name")],
            fetched_at=datetime.now().isoformat(),
        )
        self._log(f"[OK] Video detail: {info.name[:50]} by {info.author_name}")
        return info

    # ── 搜索 ────────────────────────────────────────────────

    # TODO(R3-ghost): renamed from public `search_videos` -> internal raw
    # helper used by the R3 public action `search_videos(ctx, params)` below.
    def _search_videos_raw(
        self,
        keyword: str,
        max_results: int = 20,
    ) -> list[SearchResult]:
        """搜索关键词，返回视频 ID 清单。

        Args:
            keyword:      搜索关键词
            max_results:  最多返回多少条（自动翻页，默认 20）

        Returns:
            SearchResult 列表
        """
        sdk = self._get_sdk()
        results: list[SearchResult] = []
        pcursor = ""
        page = 0

        self._log(f"[INFO] 搜索: '{keyword}' (最多 {max_results} 条)")

        while len(results) < max_results:
            page += 1
            try:
                self._rate_limiter.acquire()
                data = sdk.search_feed(keyword, pcursor=pcursor)
                feeds = data.get("feeds") or []

                if not feeds:
                    self._log(f"  搜索第 {page} 页无结果，停止")
                    break

                for f in feeds:
                    ph = f.get("photo") or {}
                    au = f.get("author") or {}
                    results.append(SearchResult(
                        photo_id=ph.get("id", ""),
                        name=ph.get("caption", ""),
                        like_count=ph.get("likeCount", 0),
                        view_count=ph.get("viewCount", 0),
                        author_id=au.get("id", ""),
                        author_name=au.get("name", ""),
                        keyword=keyword,
                    ))
                    if len(results) >= max_results:
                        break

                next_pcursor = data.get("pcursor", "")
                if not next_pcursor or next_pcursor == "no_more":
                    self._log(f"  搜索第 {page} 页，已到末页，共 {len(results)} 条")
                    break

                pcursor = next_pcursor
                self._log(f"  搜索第 {page} 页 +{len(feeds)} 条，共 {len(results)} 条，继续...")
                time.sleep(0.5)

            except Exception as e:
                self._log(f"  搜索第 {page} 页失败: {e}")
                break

        results = results[:max_results]
        self._log(f"[OK] 搜索完成: '{keyword}' → {len(results)} 个视频")

        return results

    # ── 视频信息 ─────────────────────────────────────────────

    def _fetch_video_info(self, sdk: KuaishouSDK, photo_id: str) -> VideoInfo:
        """获取视频基础信息。"""
        try:
            self._rate_limiter.acquire()
            data = sdk.video_detail(photo_id)
            detail = (data.get("data") or {}).get("visionVideoDetail") or {}
            photo = detail.get("photo") or {}
            author = detail.get("author") or {}
            tags = [t.get("name", "") for t in (detail.get("tags") or [])]
            return VideoInfo(
                photo_id=photo_id,
                name=photo.get("caption", ""),
                like_count=photo.get("likeCount", 0),
                view_count=photo.get("viewCount", 0),
                comment_count=0,
                author_id=author.get("id", ""),
                author_name=author.get("name", ""),
                duration=photo.get("duration", 0),
                tags=tags,
                fetched_at=datetime.now().isoformat(),
            )
        except Exception as e:
            self._log(f"  [{photo_id}] 视频信息获取失败: {e}")
            return VideoInfo(photo_id=photo_id, fetched_at=datetime.now().isoformat())

    # ── 评论爬取 ─────────────────────────────────────────────

    def _fetch_comments_all(
        self, sdk: KuaishouSDK, photo_id: str, max_comments: Optional[int]
    ) -> tuple[list[Comment], int, int]:
        """翻页爬取评论，返回 (comments, total_fetched, total_pages)。"""
        all_comments: list[Comment] = []
        pcursor = ""
        page_num = 0

        while True:
            page_num += 1
            try:
                self._rate_limiter.acquire()
                data = sdk.comment_list(photo_id, pcursor=pcursor)
                cl = (data.get("data") or {}).get("visionCommentList") or {}
                raw_comments = cl.get("rootCommentsV2") or []

                if not raw_comments:
                    # Log response for debugging when first page returns empty
                    if page_num == 1:
                        import json as _json
                        data_str = _json.dumps(data, ensure_ascii=False)
                        self._log(f"  [{photo_id}] Page 1 empty, response: {data_str[:500]}")
                    break

                for c in raw_comments:
                    all_comments.append(Comment(
                        comment_id=c.get("commentId", ""),
                        author_id=c.get("authorId", ""),
                        author_name=c.get("authorName", ""),
                        content=c.get("content", ""),
                        timestamp=c.get("timestamp", 0),
                        like_count=c.get("likedCount", 0),
                        has_sub_comments=c.get("hasSubComments", False),
                        photo_id=photo_id,
                    ))
                    if max_comments and len(all_comments) >= max_comments:
                        self._log(f"  [{photo_id}] 已达限制 {max_comments} 条，停止翻页")
                        return all_comments[:max_comments], len(all_comments[:max_comments]), page_num

                next_pcursor = cl.get("pcursorV2") or cl.get("pcursor") or ""
                if not next_pcursor or next_pcursor == "no_more":
                    self._log(f"  [{photo_id}] 第 {page_num} 页，已到末页")
                    break

                pcursor = next_pcursor
                self._log(
                    f"  [{photo_id}] 第 {page_num} 页 +{len(raw_comments)} 条，"
                    f"共 {len(all_comments)} 条，继续翻页..."
                )
                time.sleep(self.page_delay)

            except Exception as e:
                self._log(f"  [{photo_id}] 第 {page_num} 页请求失败: {e}")
                break

        return all_comments, len(all_comments), page_num

    def _fetch_sub_comments(self, sdk: KuaishouSDK, photo_id: str,
                            root_comment_id: str) -> list[Comment]:
        """Fetch all sub-comments (replies) for a root comment."""
        sub_comments: list[Comment] = []
        pcursor = ""

        while True:
            try:
                self._rate_limiter.acquire()
                data = sdk.sub_comment_list(photo_id, root_comment_id, pcursor=pcursor)
                sl = (data.get("data") or {}).get("visionSubCommentList") or {}
                raw_subs = sl.get("subCommentsV2") or []

                if not raw_subs:
                    break

                for s in raw_subs:
                    sub_comments.append(Comment(
                        comment_id=s.get("commentId", ""),
                        author_id=s.get("authorId", ""),
                        author_name=s.get("authorName", ""),
                        content=s.get("content", ""),
                        timestamp=s.get("timestamp", 0),
                        like_count=s.get("likedCount", 0),
                        has_sub_comments=s.get("hasSubComments", False),
                        photo_id=photo_id,
                        reply_to_user_name=s.get("replyToUserName", ""),
                        reply_to=s.get("replyTo", ""),
                        is_sub_comment=True,
                        parent_comment_id=root_comment_id,
                    ))

                next_pcursor = sl.get("pcursorV2") or sl.get("pcursor") or ""
                if not next_pcursor or next_pcursor == "no_more":
                    break

                pcursor = next_pcursor
                time.sleep(self.page_delay * 0.5)  # sub-comment pages can be faster

            except Exception as e:
                self._log(f"  [{photo_id}] sub-comments for {root_comment_id} failed: {e}")
                break

        return sub_comments

    # TODO(R3-ghost): suggest rename to `_scrape_one` (internal helper used by scrape/scrape_video action); awaiting decision
    def scrape_one(self, raw_input: str) -> VideoResult:
        """Scrape comments for a single video."""
        photo_id = parse_video_id(raw_input)
        result = VideoResult(photo_id=photo_id, input_raw=raw_input)
        t0 = time.time()

        self._log(f"▶ 开始爬取: {photo_id}")
        try:
            sdk = self._get_sdk()
            result.video_info = self._fetch_video_info(sdk, photo_id)
            comments, total_fetched, total_pages = self._fetch_comments_all(
                sdk, photo_id, self.max_comments
            )

            # Fetch sub-comments for root comments that have replies
            all_comments = list(comments)
            root_with_subs = [c for c in comments if c.has_sub_comments]
            if root_with_subs and self.include_sub_comments:
                self._log(f"  [{photo_id}] Fetching sub-comments for {len(root_with_subs)} root comments...")
                sub_total = 0
                for rc in root_with_subs:
                    subs = self._fetch_sub_comments(sdk, photo_id, rc.comment_id)
                    all_comments.extend(subs)
                    sub_total += len(subs)
                    time.sleep(self.page_delay * 0.3)
                self._log(f"  [{photo_id}] Sub-comments fetched: {sub_total} replies")

            result.comments = all_comments
            result.total_fetched = len(all_comments)
            result.total_pages = total_pages
            if result.video_info:
                result.video_info.comment_count = len(all_comments)
            result.status = "ok"
            result.elapsed_s = round(time.time() - t0, 2)
            root_cnt = len([c for c in result.comments if not c.is_sub_comment])
            sub_cnt = len([c for c in result.comments if c.is_sub_comment])
            self._log(
                f"[OK] 完成: {photo_id} | 评论 {result.total_fetched} 条 "
                f"(一级 {root_cnt} + 回复 {sub_cnt}) / {total_pages} 页 "
                f"| 耗时 {result.elapsed_s}s"
            )
        except Exception as e:
            result.status = "error"
            result.error = str(e)
            result.elapsed_s = round(time.time() - t0, 2)
            self._log(f"[ERR] 失败: {photo_id} | {e}")

        return result

    # ── 批量入口 ─────────────────────────────────────────────

    # TODO(R3-ghost): suggest delete (CLI-only batch entry; R3 uses ctx + per-record write); awaiting decision
    def scrape(self, inputs: list[str]) -> list[VideoResult]:
        """批量并发爬取评论。

        Args:
            inputs: 视频 ID 或 URL 列表

        Returns:
            VideoResult 列表
        """
        if not inputs:
            self._log("[WARN] 输入列表为空")
            return []

        # 去重保序
        seen, deduped = set(), []
        for x in inputs:
            if x not in seen:
                seen.add(x)
                deduped.append(x)
        if len(deduped) < len(inputs):
            self._log(f"  [INFO] 去重: {len(inputs)} -> {len(deduped)} 个视频")

        self._log(f"[START] 开始批量爬取 {len(deduped)} 个视频 | workers={self.workers}")
        results: list[VideoResult] = []
        results_lock = threading.Lock()

        def task(idx: int, raw: str) -> VideoResult:
            time.sleep(idx * self.video_delay)
            r = self.scrape_one(raw)
            with results_lock:
                results.append(r)
            return r

        with ThreadPoolExecutor(max_workers=self.workers) as executor:
            futures = {executor.submit(task, i, raw): raw for i, raw in enumerate(deduped)}
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as e:
                    self._log(f"[ERR] 任务异常 [{futures[future]}]: {e}")

        ok_cnt = sum(1 for r in results if r.status == "ok")
        total_comments = sum(r.total_fetched for r in results)
        self._log(
            f"[OK] 完成！{ok_cnt}/{len(results)} 个视频成功，共 {total_comments} 条评论"
        )
        return results

    # TODO(R3-ghost): suggest delete (CLI-only file-loader entry; R3 receives inputs via params); awaiting decision
    def scrape_from_file(self, filepath: str) -> list[VideoResult]:
        """从 txt 文件批量爬取。"""
        inputs = load_ids_from_file(filepath)
        self._log(f"📂 从文件加载 {len(inputs)} 个视频: {filepath}")
        return self.scrape(inputs)

    # TODO(R3-ghost): suggest add to plugin.yaml as action `search_and_scrape` (combines search + scrape, useful one-shot); awaiting decision
    def search_and_scrape(
        self,
        keyword: str,
        max_search: int = 20,
        max_comments: Optional[int] = None,
    ) -> tuple[list[SearchResult], list[VideoResult]]:
        """搜索关键词后直接爬取评论（一步到位）。

        Args:
            keyword:      搜索关键词
            max_search:   搜索结果数量上限
            max_comments: 每个视频评论数量上限（None = 全量）

        Returns:
            (search_results, video_results)
        """
        old_max = self.max_comments
        self.max_comments = max_comments
        try:
            search_results = self._search_videos_raw(keyword, max_results=max_search)
            ids = [r.photo_id for r in search_results if r.photo_id]
            video_results = self.scrape(ids)
        finally:
            self.max_comments = old_max
        return search_results, video_results

    # ══════════════════════════════════════════════════════════
    #  R3 PUBLIC ACTIONS - signature `(self, ctx, params) -> None`
    #  These are the dispatch targets for service.execute().
    # ══════════════════════════════════════════════════════════

    def _bind_task(self, ctx: TaskContext) -> None:
        """Wire per-task state onto the singleton scraper.

        log_dir / log_callback change every task, so we mutate them on the
        instance before each action. ``_get_sdk`` detects the change via
        its (cookie_path, log_dir) key and rebuilds the SDK transparently.
        """
        self.log_dir = Path(ctx.output_dir) if ctx.output_dir else None
        self._log_callback = ctx.log

    def scrape_comments(self, ctx: TaskContext, params: dict) -> None:
        """Action: scrape_comments - fetch comments for a single kuaishou video."""
        video_id = params["video_id"]
        assert isinstance(video_id, str), (
            f"scrape_comments expects single video_id (str), got {type(video_id).__name__}"
        )
        max_comments = params.get("max_comments")
        include_sub_comments = params.get("include_sub_comments", False)

        self.ensure_cookie()
        self._bind_task(ctx)
        self.max_comments = max_comments
        self.include_sub_comments = include_sub_comments

        cookie_path = self._resolved_cookie_path() or ""
        ctx.log(f"cookie: path={cookie_path}, exists={Path(cookie_path).exists() if cookie_path else False}")

        ctx.check_cancelled()
        ctx.log(f"Scraping kuaishou video: {video_id}")

        result = self.scrape_one(video_id)
        ctx.log(
            f"  result: status={result.status}, in_memory={len(result.comments)}, "
            f"total_fetched={result.total_fetched}"
        )

        if result.status == "ok":
            written = 0
            for comment in result.comments:
                ctx.check_cancelled()
                record = comment.to_dict()
                record["_source_video"] = result.photo_id
                ctx.write_record(record)
                written += 1
            ctx.log(f"  [OK] {result.photo_id}: written={written}")
            if written == 0:
                ctx.log("  [WARN] API returned ok but 0 comments in memory", level="WARN")
            if written != len(result.comments):
                ctx.log(
                    f"  [WARN] count mismatch: expected={len(result.comments)}, written={written}",
                    level="WARN",
                )
        else:
            ctx.log(f"  [ERR] {result.photo_id or video_id}: {result.error}")
            raise RuntimeError(f"{result.photo_id or video_id}: {result.error}")

        ctx.set_progress(1.0)

    def search_videos(self, ctx: TaskContext, params: dict) -> None:
        """Action: search_videos - keyword search returning SearchResult records."""
        keyword = params["keyword"]
        max_results = params.get("max_results", 20)

        self.ensure_cookie()
        self._bind_task(ctx)

        ctx.check_cancelled()
        ctx.log(f"Searching kuaishou: '{keyword}' (max {max_results})")

        results = self._search_videos_raw(keyword=keyword, max_results=max_results)

        for r in results:
            ctx.check_cancelled()
            ctx.write_record(r.to_dict())

        ctx.log(f"  [OK] Found {len(results)} results for '{keyword}'")
        ctx.set_progress(1.0)

    def get_video_detail(self, ctx: TaskContext, params: dict) -> None:
        """Action: get_video_detail - fetch VideoInfo for a single kuaishou video."""
        video_id = params["video_id"]

        self.ensure_cookie()
        self._bind_task(ctx)

        ctx.check_cancelled()
        ctx.log(f"[kuaishou] Fetching video detail: {video_id}")

        try:
            info = self._get_video_detail_raw(video_id)
            record = info.to_dict()
            ctx.write_record(record)
            ctx.log(f"  [OK] {video_id}: {record.get('name', '')[:50]}")
        except Exception as e:
            ctx.record_error(f"{video_id}: {e}", response=e)
            ctx.log(f"  [ERR] {video_id}: {e}")

        ctx.set_progress(1.0)


    # ══════════════════════════════════════════════════════════
    #  LIVE ACTIONS (Hybrid: browser bootstrap → Python WS)
    # ══════════════════════════════════════════════════════════

    def list_live_categories(self, ctx: TaskContext, params: dict) -> None:
        """Action: list_live_categories — paged crawl of /live_api/category/data.

        runtime=browser_backed, transport=http, throttle_scope=request.
        Single browser handshake -> harvest signature -> httpx replays
        page=1..N until ``hasMore=False`` or no new (non-duplicate) items.
        Output: every (de-duped) category.
        """
        runtime = get_current_runtime()
        if runtime is None or runtime.browser is None:
            raise RuntimeError("list_live_categories requires browser_backed runtime")

        self._bind_task(ctx)
        ctx.check_cancelled()
        ctx.log("[kuaishou] list_live_categories (paging until no new content)")

        with runtime.browser.hold() as bba_page:
            rows = self._get_sdk().list_live_categories_hybrid(
                bba_page,
                is_cancelled=lambda: ctx.is_cancelled,
            )
        for row in rows:
            ctx.check_cancelled()
            record = LiveCategoryItem(
                category_id=str(row.get("category_id") or ""),
                category_name=str(row.get("category_name") or ""),
                icon_url=str(row.get("icon_url") or ""),
                category_type=int(row.get("category_type") or 0),
                top_rooms_count=int(row.get("top_rooms_count") or 0),
            )
            ctx.write_record(record.to_dict())
        ctx.log(f"  [OK] collected {len(rows)} categories")
        ctx.set_progress(1.0)

    def search_live_categories(self, ctx: TaskContext, params: dict) -> None:
        """Action: search_live_categories — keyword search over live categories.

        runtime=browser_backed, transport=http, throttle_scope=request.
        Single-shot replay of /live_api/category/search?keyword=... after
        capturing signature in browser.
        """
        keyword = params["keyword"]
        runtime = get_current_runtime()
        if runtime is None or runtime.browser is None:
            raise RuntimeError("search_live_categories requires browser_backed runtime")

        self._bind_task(ctx)
        ctx.check_cancelled()
        ctx.log(f"[kuaishou] search_live_categories keyword={keyword!r}")

        with runtime.browser.hold() as bba_page:
            rows = self._get_sdk().search_live_categories_hybrid(bba_page, keyword)
        for row in rows:
            ctx.check_cancelled()
            record = LiveCategorySearchResult(
                category_id=str(row.get("category_id") or ""),
                category_name=str(row.get("category_name") or ""),
                icon_url=str(row.get("icon_url") or ""),
                category_type=int(row.get("category_type") or 0),
                keyword=keyword,
            )
            ctx.write_record(record.to_dict())
        ctx.log(f"  [OK] keyword={keyword!r} matched {len(rows)} categories")
        ctx.set_progress(1.0)

    def list_category_live_rooms(self, ctx: TaskContext, params: dict) -> None:
        """Action: list_category_live_rooms — list rooms inside a live category.

        runtime=browser_backed, transport=http, throttle_scope=request.
        Browser is borrowed once to harvest ``__NS_hxfalcon`` (query) and
        ``kww`` (header) by triggering a real /gameboard/list call;
        Python then httpx-replays the same endpoint with our chosen
        ``category_id`` and walks pages.
        """
        category_id = str(params["category_id"]).strip()
        if not category_id:
            raise ValueError("category_id is required")
        max_results = int(params.get("max_results", 100) or 100)
        page_size = int(params.get("page_size", 20) or 20)

        runtime = get_current_runtime()
        if runtime is None or runtime.browser is None:
            raise RuntimeError("list_category_live_rooms requires browser_backed runtime")

        self._bind_task(ctx)
        ctx.check_cancelled()
        ctx.log(
            f"[kuaishou] list_category_live_rooms category_id={category_id} "
            f"max_results={max_results} page_size={page_size}"
        )

        with runtime.browser.hold() as bba_page:
            rows = self._get_sdk().list_category_live_rooms_hybrid(
                bba_page, category_id,
                max_results=max_results, page_size=page_size,
                is_cancelled=lambda: ctx.is_cancelled,
            )
        for row in rows:
            ctx.check_cancelled()
            record = LiveRoomSearchResult(
                live_stream_id=str(row.get("live_stream_id") or ""),
                principal_id=str(row.get("principal_id") or ""),
                author_name=str(row.get("author_name") or ""),
                author_avatar=str(row.get("author_avatar") or ""),
                title=str(row.get("title") or ""),
                cover_url=str(row.get("cover_url") or ""),
                watching_count=int(row.get("watching_count") or 0),
                like_count=int(row.get("like_count") or 0),
                category_id=str(row.get("category_id") or category_id),
                category_name=str(row.get("category_name") or ""),
                stream_flv=str(row.get("stream_flv") or ""),
                start_time=int(row.get("start_time") or 0),
            )
            ctx.write_record(record.to_dict())
        ctx.log(f"  [OK] category_id={category_id}: collected {len(rows)} live rooms")
        ctx.set_progress(1.0)


    def get_live_room_info(self, ctx: TaskContext, params: dict) -> None:
        """Action: get_live_room_info — first-screen live room snapshot.

        runtime=browser_backed, transport=http, throttle_scope=request.
        Receives ``self.browser_session`` (BrowserSessionHandle) from daemon.
        """
        principal_id = params["principal_id"]
        runtime = get_current_runtime()
        if runtime is None or runtime.browser is None:
            raise RuntimeError("get_live_room_info requires browser_backed runtime")

        self._bind_task(ctx)
        ctx.check_cancelled()
        ctx.log(f"[kuaishou] get_live_room_info principal_id={principal_id}")

        sdk = self._get_sdk()
        with runtime.browser.hold() as bba_page:
            row = sdk.get_live_room_info_hybrid(bba_page, principal_id)

        record = LiveRoomInfoRecord(
            principal_id=str(row.get("principal_id") or ""),
            live_stream_id=str(row.get("live_stream_id") or ""),
            title=str(row.get("title") or ""),
            author_id=str(row.get("author_id") or ""),
            author_name=str(row.get("author_name") or ""),
            is_live=bool(row.get("is_live") or False),
            fetched_at=str(row.get("fetched_at") or ""),
            source_url=str(row.get("source_url") or ""),
        )
        ctx.write_record(record.to_dict())
        ctx.set_progress(1.0)

    def collect_live_events(self, ctx: TaskContext, params: dict) -> None:
        """Action: collect_live_events — collect Kuaishou live WSS events.

        runtime=browser_backed, transport=websocket, lease_policy=manual.
        Browser is leased ONLY for bootstrap (capture token + ws_urls + cookie),
        then released; the rest of ``duration_seconds`` is pure Python WS.

        Stop conditions are auto (no user toggle):
          1. duration_seconds reached
          2. task cancelled
          3. WSS connection closed
          4. SC_ERROR with code=60200 (broadcaster ended live; emits a
             synthetic SC_LIVE_END event then returns)

        Params:
          - event_filter: list[str] of raw_cmd values to keep (e.g.
            ["SC_FEED_PUSH_COMMENT", "SC_FEED_PUSH_GIFT", "SC_LIVE_END"]).
            Empty/missing = keep all events.
          - wait_until_live: bool, poll get_live_room_info until is_live=True
            before connecting. Default false.
          - wait_timeout_seconds: int, default 86400 (24h) wait cap.
        """
        runtime = get_current_runtime()
        if runtime is None or runtime.browser is None:
            raise RuntimeError("collect_live_events requires browser_backed runtime")

        principal_id = params["principal_id"]
        duration = float(params.get("duration_seconds") or 0)  # 0 = run until live ends
        wait_until_live = bool(params.get("wait_until_live", False))
        wait_timeout = float(params.get("wait_timeout_seconds", 86400) or 86400)

        # event_filter: list of raw_cmd from frontend (already raw english names)
        raw_filter = params.get("event_filter")
        event_filter: set[str] | None = None
        if raw_filter and isinstance(raw_filter, list) and len(raw_filter) > 0:
            event_filter = {str(x) for x in raw_filter if x}

        self._bind_task(ctx)
        ctx.check_cancelled()
        ctx.log(
            f"[kuaishou] collect_live_events principal_id={principal_id} "
            f"duration={duration}s wait_until_live={wait_until_live} "
            f"event_filter={'all' if event_filter is None else sorted(event_filter)}"
        )

        started = time.monotonic()

        def emit(event: dict) -> None:
            ctx.check_cancelled()
            record = LiveEventRecord(
                principal_id=str(event.get("principal_id") or principal_id),
                live_stream_id=str(event.get("live_stream_id") or ""),
                event_type=str(event.get("event_type") or "raw"),
                raw_cmd=str(event.get("raw_cmd") or ""),
                uid=str(event.get("uid") or ""),
                nickname=str(event.get("nickname") or ""),
                content=str(event.get("content") or ""),
                online_count=int(event.get("online_count") or 0),
                online_count_str=str(event.get("online_count_str") or ""),
                like_count_str=str(event.get("like_count_str") or ""),
                gift_id=str(event.get("gift_id") or ""),
                gift_count=int(event.get("gift_count") or 0),
                error_code=int(event.get("error_code") or 0),
                ts=float(event.get("ts") or 0.0),
                payload=json.dumps(event.get("payload") or {}, ensure_ascii=False),
            )
            ctx.write_record(record.to_dict())
            if duration > 0:
                ctx.set_progress(min(0.99, (time.monotonic() - started) / duration))

        sdk = self._get_sdk()
        count = sdk.collect_live_events_hybrid(
            browser_provider=runtime.browser,
            principal_id=principal_id,
            duration_seconds=duration,
            on_event=emit,
            is_cancelled=lambda: ctx.is_cancelled,
            event_filter=event_filter,
            wait_until_live=wait_until_live,
            wait_timeout_seconds=wait_timeout,
        )
        ctx.log(f"  [OK] principal_id={principal_id}: collected {count} live events")
        ctx.set_progress(1.0)

