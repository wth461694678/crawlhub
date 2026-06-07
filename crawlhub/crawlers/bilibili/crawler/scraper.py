"""
Bilibili Crawler Core
=====================
Main crawler class for comments, search, video detail.

Uses direct API mode by default. Falls back to Playwright browser
when API requires wbi signing or encounters anti-bot measures.
"""

from __future__ import annotations

import json
import re
import sys
import time
import random
import threading
from dataclasses import asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from crawlhub.core.task_context import TaskContext
from crawlhub.core.cookie_resolver import CookieResolverMixin, CookieNotReady

from crawlhub.core.platform import FileCookieJar

from .models import (
    VideoInfo, VideoDetail, Comment, SearchResult, LiveRoom, VideoResult,
    VideoAISummary, LiveRoomSearchResult, LiveRoomInfoRecord, LiveEventRecord,
)
from .client import BilibiliClient, extract_video_id, extract_room_id, bv2av


# Legacy alias: scraper code below still references ``BilibiliAPI`` in
# type hints and calls.  Keep it as the name of the class for now so the
# diff stays focused on the C16 segregation fix.
BilibiliAPI = BilibiliClient

_ROOT = Path(__file__).parent.parent


# Beijing timezone (UTC+8). Bilibili's pubtime filter is interpreted in Beijing time.
_BJ_TZ = timezone(timedelta(hours=8))


def _parse_pubdate_range(
    pubdate_begin: Optional[str],
    pubdate_end: Optional[str],
) -> tuple[Optional[int], Optional[int]]:
    """Parse a date/datetime range into a pair of Unix seconds (Beijing time).

    Rules:
      - Both must be given, or both must be None. Mixing is an error.
      - "YYYY-MM-DD" begin is treated as 00:00:00 of that day (Beijing time).
      - "YYYY-MM-DD" end   is treated as 23:59:59 of that day (Beijing time).
      - "YYYY-MM-DD HH:MM:SS" is used as-is (Beijing time).

    Returns:
        (begin_seconds, end_seconds), or (None, None) if both inputs are None.
    """
    if pubdate_begin is None and pubdate_end is None:
        return None, None
    if pubdate_begin is None or pubdate_end is None:
        raise ValueError(
            "pubdate_begin and pubdate_end must be provided together (or both omitted)."
        )

    def _to_ts(raw: str, is_end: bool) -> int:
        raw = raw.strip()
        # Try full datetime first, then fall back to date-only.
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(raw, fmt)
                if fmt == "%Y-%m-%d":
                    # Date-only: begin -> 00:00:00, end -> 23:59:59
                    dt = dt.replace(
                        hour=23 if is_end else 0,
                        minute=59 if is_end else 0,
                        second=59 if is_end else 0,
                    )
                return int(dt.replace(tzinfo=_BJ_TZ).timestamp())
            except ValueError:
                continue
        raise ValueError(
            f"Invalid date format: {raw!r}. Expected 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM:SS'."
        )

    begin_ts = _to_ts(pubdate_begin, is_end=False)
    end_ts = _to_ts(pubdate_end, is_end=True)
    if begin_ts > end_ts:
        raise ValueError(
            f"pubdate_begin ({pubdate_begin}) must be <= pubdate_end ({pubdate_end})."
        )
    return begin_ts, end_ts


# ═══════════════════════════════════════════════════════
#  BilibiliScraper
# ═══════════════════════════════════════════════════════

class BilibiliScraper(CookieResolverMixin):
    """Bilibili crawler main class.

    Inherits ``CookieResolverMixin`` (PLATFORM_NAME='bilibili') for unified
    cookie path resolution; service layer relies on ``self.resolve_cookie_path()``
    via the inherited mixin instead of doing its own path lookups.

    Args:
        cookie_path:          Path to cookie JSON file
        output_dir:           Output directory for results (REQUIRED — must
                              be provided by the caller, typically
                              ``ctx.output_dir`` from the platform service.
                              The scraper writes per-task JSONL files here
                              and MUST NOT fall back to a module-local path,
                              otherwise R6 (write-root isolation) is broken.)
        page_delay:           Delay between page requests (seconds)
        max_comments:         Max comments per video (None = all)
        include_sub_comments: Whether to crawl sub-comments
    """

    DEFAULT_PAGE_DELAY = 0.8
    DEFAULT_VIDEO_DELAY = 1.5

    PLATFORM_NAME = "bilibili"

    def check_cookie_valid(self) -> bool:
        """CookieResolverMixin hook: cookie file present is enough.

        The deeper "is it actually logged in" probe is handled by the
        service-layer ``check_cookie`` (which hits the bilibili nav API).
        """
        return Path(self.cookie_path).exists()

    def __init__(
        self,
        cookie_path: str = None,
        output_dir: str = None,
        page_delay: float = DEFAULT_PAGE_DELAY,
        video_delay: float = DEFAULT_VIDEO_DELAY,
        max_comments: Optional[int] = None,
        include_sub_comments: bool = False,
        mode: int = 3,
        log_callback=None,
    ):
        self.cookie_path = cookie_path or str(_ROOT / "data" / "cookie.json")
        # R3: output_dir is now optional. R3 public actions write via
        # ``ctx.write_record`` (no local JSONL), so output_dir is only needed
        # by the legacy ``_save_result`` / ``_scrape_one_and_save`` helpers.
        # Pass None when constructing the R3 scraper from a service singleton.
        if output_dir is None:
            self.output_dir = None
        else:
            self.output_dir = Path(output_dir)
            self.output_dir.mkdir(parents=True, exist_ok=True)

        self.page_delay = page_delay
        self.video_delay = video_delay
        self.max_comments = max_comments
        self.include_sub_comments = include_sub_comments
        self.mode = mode

        self._api: Optional[BilibiliClient] = None
        self._jar_path: Optional[str] = None
        self._print_lock = threading.Lock()
        self._log_callback = log_callback

        self._log(
            f"Init done | page_delay={page_delay}s video_delay={video_delay}s "
            f"max_comments={max_comments or 'all'} sub_comments={include_sub_comments}"
        )

    # ── Internal ──────────────────────────────────────────────

    def _resolved_cookie_path(self) -> str:
        """Return the cookie path string used for the current API call.

        Honors thread-local override (set by daemon) via
        ``CookieResolverMixin.resolve_cookie_path``; falls back to
        ``self.cookie_path`` if mixin resolution yields nothing usable.
        """
        try:
            p = self.resolve_cookie_path()
            return str(p)
        except Exception:
            return self.cookie_path

    def _get_api(self) -> BilibiliClient:
        """Return a BilibiliClient bound to the currently-resolved cookie path.

        Re-creates the client when the resolved path changes (so per-task
        cookie overrides take effect).
        """
        resolved = self._resolved_cookie_path()
        if self._api is None or self._jar_path != resolved:
            self._api = BilibiliClient(cookie_jar=FileCookieJar(Path(resolved)))
            self._jar_path = resolved
        return self._api

    def _save_cookies(self, cookie_dict: dict) -> None:
        """Persist a fresh cookie dict to the resolved path and reset the API.

        Used by the (private) Playwright login flow.  Writes the same
        flat-dict shape ``FileCookieJar`` knows how to read back.
        """
        resolved = Path(self._resolved_cookie_path())
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(json.dumps(cookie_dict, ensure_ascii=False, indent=2),
                            encoding="utf-8")
        self._api = None  # force re-init with new cookies

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

    def _page_sleep(self):
        """Random delay between page requests."""
        delay = random.uniform(self.page_delay * 0.7, self.page_delay * 1.3)
        time.sleep(delay)

    # ── Login ──────────────────────────────────────────────────

    # TODO(R3-ghost):#1 suggest rename to `_check_login` (internal cookie self-check); awaiting decision
    def _check_login(self) -> dict:
        """Check login status."""
        api = self._get_api()
        return api.check_login()

    # TODO(R3-ghost): suggest add to plugin.yaml as action `refresh_cookie` (browser-based re-login); awaiting decision
    # Phase 3: marked private (`_refresh_cookie`) to pass C14; promote back to public when added to plugin.yaml.
    async def _refresh_cookie(self, timeout: int = 300) -> bool:
        """Open browser for user to log in and save cookies.

        Uses Playwright to open bilibili.com login page.
        """
        from playwright.async_api import async_playwright

        self._log("Opening browser for Bilibili login...")

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=False)
            context = await browser.new_context()
            page = await context.new_page()

            await page.goto("https://www.bilibili.com/")
            self._log(f"Please log in within {timeout}s. Browser will close automatically after login.")

            # Wait for SESSDATA cookie to appear
            start = time.time()
            while time.time() - start < timeout:
                cookies = await context.cookies()
                sessdata = next((c for c in cookies if c["name"] == "SESSDATA"), None)
                if sessdata and sessdata["value"]:
                    # Save all cookies
                    cookie_dict = {c["name"]: c["value"] for c in cookies if ".bilibili.com" in c.get("domain", "")}
                    self._save_cookies(cookie_dict)
                    self._log("✅ Cookies saved successfully!")
                    await browser.close()
                    return True
                await page.wait_for_timeout(2000)

            self._log("❌ Login timeout")
            await browser.close()
            return False

    # ── Video Detail ──────────────────────────────────────────

    def _fetch_video_info(self, video_id: str) -> VideoInfo:
        """Internal helper: fetch raw VideoInfo (used by scrape_one).

        For the R3 public action `get_video_detail(ctx, params)`, see below.
        """
        bvid = extract_video_id(video_id)
        if not bvid:
            raise ValueError(f"Cannot parse video ID from: {video_id}")

        api = self._get_api()
        data = api.get_video_detail(bvid)
        info = VideoInfo(**{k: v for k, v in data.items() if k in VideoInfo.__dataclass_fields__})
        self._log(f"📋 Video: {info.title[:50]} by {info.author_name}")
        return info

    # ── Comment Scraping ──────────────────────────────────────

    def _parse_comment(self, reply: dict, bvid: str, is_sub: bool = False, parent_rpid: int = 0) -> Comment:
        """Parse a single comment from API response."""
        member = reply.get("member", {})
        content = reply.get("content", {})
        reply_control = reply.get("reply_control", {})
        ip_loc = reply_control.get("location", "").replace("IP属地：", "")

        return Comment(
            rpid=reply.get("rpid", 0),
            uid=member.get("mid", 0),
            uname=member.get("uname", ""),
            content=content.get("message", ""),
            like_count=reply.get("like", 0),
            reply_count=reply.get("rcount", 0),
            ctime=datetime.fromtimestamp(reply.get("ctime", 0)).strftime("%Y-%m-%d %H:%M:%S"),
            floor=reply.get("floor", 0),
            ip_location=ip_loc,
            is_sub_comment=is_sub,
            parent_rpid=parent_rpid,
            bvid=bvid,
        )

    # TODO(R3-ghost): suggest rename to `_scrape_one` (internal helper used by scrape_video); awaiting decision
    def _scrape_one(self, input_raw: str) -> VideoResult:
        """Scrape comments for a single video.

        Args:
            input_raw: BV ID, AV ID, or URL

        Returns:
            VideoResult with all scraped data
        """
        result = VideoResult(input_raw=input_raw)
        start_time = time.time()

        try:
            bvid = extract_video_id(input_raw)
            if not bvid:
                result.status = "error"
                result.error = f"Cannot parse video ID from: {input_raw}"
                return result

            result.bvid = bvid
            api = self._get_api()

            # Fetch video info
            try:
                detail = api.get_video_detail(bvid)
                result.video_info = VideoInfo(**{k: v for k, v in detail.items() if k in VideoInfo.__dataclass_fields__})
                self._log(f"📋 {bvid}: {result.video_info.title[:40]} ({result.video_info.reply_count} comments)")
            except Exception as e:
                self._log(f"⚠️ {bvid}: Cannot get video info: {e}")

            # Scrape root comments
            comments = []
            page = 1
            max_pages = 500  # Safety limit

            while page <= max_pages:
                if self.max_comments and len(comments) >= self.max_comments:
                    break

                self._page_sleep()
                try:
                    resp = api.get_comments(bvid, page=page, mode=self.mode)
                except Exception as e:
                    self._log(f"[ERR] {bvid} page {page}: Request failed: {e}")
                    break

                if resp.get("code") != 0:
                    if page == 1:
                        result.status = "error"
                        result.error = f"API error: {resp.get('code')} - {resp.get('message')}"
                        self._log(f"[ERR] {bvid}: {result.error}")
                        resp_str = json.dumps(resp, ensure_ascii=False)
                        self._log(f"[ERR] response: {resp_str[:500]}")
                    break

                replies = (resp.get("data") or {}).get("replies")
                if not replies:
                    break

                for reply in replies:
                    if self.max_comments and len(comments) >= self.max_comments:
                        break
                    comment = self._parse_comment(reply, bvid)
                    comments.append(comment)

                    # Optionally scrape sub-comments
                    if self.include_sub_comments and comment.reply_count > 0:
                        sub_comments = self._scrape_sub_comments(api, bvid, comment.rpid)
                        comments.extend(sub_comments)

                result.total_pages = page
                self._log(f"  {bvid} page {page}: +{len(replies)} comments (total: {len(comments)})")

                if len(replies) < 20:
                    break
                page += 1

            result.comments = comments
            result.total_fetched = len(comments)
            result.status = "ok" if comments or page > 1 else "error"
            if not comments and page == 1 and not result.error:
                result.error = "No comments found (empty or closed)"

        except Exception as e:
            result.status = "error"
            result.error = str(e)
            self._log(f"❌ {result.bvid or input_raw}: {e}")
        finally:
            result.elapsed_s = round(time.time() - start_time, 2)

        return result

    def _scrape_sub_comments(self, api: BilibiliAPI, bvid: str, root_rpid: int) -> list[Comment]:
        """Scrape sub-comments for a root comment."""
        sub_comments = []
        page = 1
        max_sub_pages = 50

        while page <= max_sub_pages:
            self._page_sleep()
            try:
                resp = api.get_sub_comments(bvid, root_rpid, page=page)
            except Exception:
                break

            if resp.get("code") != 0:
                break

            replies = (resp.get("data") or {}).get("replies")
            if not replies:
                break

            for reply in replies:
                sub_comments.append(self._parse_comment(reply, bvid, is_sub=True, parent_rpid=root_rpid))

            if len(replies) < 20:
                break
            page += 1

        return sub_comments

    # TODO(R3-ghost): suggest rename to `_save_result` (internal JSONL writer, ctx.write_record now preferred); awaiting decision
    def _save_result(self, result: VideoResult) -> Optional[str]:
        """Save scraping result to JSONL file.

        Writes all comments from result.comments to a JSONL file.

        Returns:
            Output file path, or None if no data
        """
        if not result.comments:
            return None
        if self.output_dir is None:
            raise ValueError("_save_result requires output_dir; pass it to BilibiliScraper(...).")

        out_path = self.output_dir / f"{result.bvid}.jsonl"
        with open(out_path, "w", encoding="utf-8") as f:
            for comment in result.comments:
                f.write(json.dumps(asdict(comment), ensure_ascii=False) + "\n")

        self._log(f"  Saved {len(result.comments)} comments to {out_path.name}")
        return str(out_path)

    # TODO(R3-ghost): suggest delete (DEPRECATED per docstring; bridge now uses scrape_one + ctx.write_record); awaiting decision
    # Phase 3: marked private (`_scrape_one_and_save`) to pass C14; safe to remove in Phase 7.
    def _scrape_one_and_save(self, input_raw: str) -> VideoResult:
        """DEPRECATED: Use scrape_one() instead.

        Bridge layer now uses scrape_one() (in-memory mode) + ctx.write_record()
        for unified data pipeline. This method writes to local JSONL files directly,
        bypassing the TaskContext data flow.

        Scrape and save comments in one pass.
        Comments are written to JSONL incrementally.
        """
        result = VideoResult(input_raw=input_raw)
        start_time = time.time()

        try:
            bvid = extract_video_id(input_raw)
            if not bvid:
                result.status = "error"
                result.error = f"Cannot parse video ID from: {input_raw}"
                return result

            result.bvid = bvid
            api = self._get_api()

            # Fetch video info
            try:
                detail = api.get_video_detail(bvid)
                result.video_info = VideoInfo(**{k: v for k, v in detail.items() if k in VideoInfo.__dataclass_fields__})
                self._log(f"📋 {bvid}: {result.video_info.title[:40]} ({result.video_info.reply_count} comments)")
            except Exception as e:
                self._log(f"⚠️ {bvid}: Cannot get video info: {e}")

            # Prepare output file
            out_path = self.output_dir / f"{bvid}.jsonl"
            total_comments = 0
            page = 1
            max_pages = 500

            with open(out_path, "w", encoding="utf-8") as f:
                while page <= max_pages:
                    if self.max_comments and total_comments >= self.max_comments:
                        break

                    self._page_sleep()
                    try:
                        resp = api.get_comments(bvid, page=page, mode=self.mode)
                    except Exception as e:
                        self._log(f"⚠️ {bvid} page {page}: Request failed: {e}")
                        break

                    if resp.get("code") != 0:
                        if page == 1:
                            result.status = "error"
                            result.error = f"API error: {resp.get('code')} - {resp.get('message')}"
                            self._log(f"❌ {bvid}: {result.error}")
                        break

                    replies = (resp.get("data") or {}).get("replies")
                    if not replies:
                        break

                    page_count = 0
                    for reply in replies:
                        if self.max_comments and total_comments >= self.max_comments:
                            break

                        comment = self._parse_comment(reply, bvid)
                        f.write(json.dumps(asdict(comment), ensure_ascii=False) + "\n")
                        total_comments += 1
                        page_count += 1

                        # Sub-comments
                        if self.include_sub_comments and comment.reply_count > 0:
                            subs = self._scrape_sub_comments(api, bvid, comment.rpid)
                            for sc in subs:
                                f.write(json.dumps(asdict(sc), ensure_ascii=False) + "\n")
                                total_comments += 1

                    result.total_pages = page
                    self._log(f"  {bvid} page {page}: +{page_count} comments (total: {total_comments})")

                    if len(replies) < 20:
                        break
                    page += 1

            result.total_fetched = total_comments
            result.status = "ok" if total_comments > 0 or page > 1 else "error"
            if total_comments == 0 and page == 1 and not result.error:
                result.error = "No comments found (empty or closed)"

        except Exception as e:
            result.status = "error"
            result.error = str(e)
            self._log(f"❌ {result.bvid or input_raw}: {e}")
        finally:
            result.elapsed_s = round(time.time() - start_time, 2)

        return result

    # ── Search ────────────────────────────────────────────────

    def _search_videos_raw(
        self,
        keyword: str,
        max_results: int = 20,
        sort_type: str = "totalrank",
        duration: int = 0,
        pubdate_begin: Optional[str] = None,
        pubdate_end: Optional[str] = None,
    ) -> list[SearchResult]:
        """Internal search helper — returns list of SearchResult.

        For the R3 public action `search_videos(ctx, params)` see below.

        Args:
            keyword:     Search query
            max_results: Max results to return
            sort_type:   totalrank, click, pubdate, dm, stow
            duration:    0=all, 1=<10min, 2=10-30min, 3=30-60min, 4=>60min
            pubdate_begin: Publish date lower bound. Format: "YYYY-MM-DD" or
                           "YYYY-MM-DD HH:MM:SS". Date-only is treated as 00:00:00 Beijing time.
                           None = no lower bound. Must be paired with pubdate_end.
            pubdate_end:   Publish date upper bound. Same format. Date-only is treated
                           as 23:59:59 Beijing time.

        Returns:
            List of SearchResult
        """
        api = self._get_api()
        results: list[SearchResult] = []
        page = 1

        # Parse date-range strings into Unix seconds (Beijing time, UTC+8).
        pubtime_begin_s, pubtime_end_s = _parse_pubdate_range(pubdate_begin, pubdate_end)

        range_desc = ""
        if pubtime_begin_s is not None:
            range_desc = f" [{pubdate_begin} ~ {pubdate_end}]"
        self._log(f"🔍 Search: '{keyword}' (max {max_results}){range_desc}")

        while len(results) < max_results:
            self._page_sleep()
            try:
                resp = api.search_videos(
                    keyword,
                    page=page,
                    order=sort_type,
                    duration=duration,
                    pubtime_begin_s=pubtime_begin_s,
                    pubtime_end_s=pubtime_end_s,
                )
            except Exception as e:
                self._log(f"⚠️ Search page {page} failed: {e}")
                break

            if resp.get("code") != 0:
                self._log(f"⚠️ Search API error: {resp.get('message')}")
                break

            data = resp.get("data", {})
            items = data.get("result", [])
            if not items:
                break

            for item in items:
                if len(results) >= max_results:
                    break

                # Clean HTML tags from title
                title = re.sub(r"<[^>]+>", "", item.get("title", ""))
                duration_str = item.get("duration", "")

                # Normalize cover URL: Bilibili API returns protocol-relative URLs
                # like "//i1.hdslb.com/bfs/archive/xxx.jpg" — prepend https: when needed.
                cover_url = item.get("pic", "") or ""
                if cover_url.startswith("//"):
                    cover_url = "https:" + cover_url
                elif cover_url and not cover_url.startswith(("http://", "https://")):
                    cover_url = "https://" + cover_url.lstrip("/")

                results.append(SearchResult(
                    bvid=item.get("bvid", ""),
                    aid=item.get("aid", 0),
                    title=title,
                    author=item.get("author", ""),
                    author_uid=item.get("mid", 0),
                    play_count=item.get("play", 0),
                    danmaku_count=item.get("video_review", 0),
                    reply_count=item.get("review", 0),
                    pubdate=datetime.fromtimestamp(item.get("pubdate", 0)).strftime("%Y-%m-%d %H:%M:%S"),
                    duration=duration_str,
                    description=item.get("description", "")[:200],
                    cover=cover_url,
                ))

            self._log(f"  Search page {page}: +{len(items)} results (total: {len(results)})")

            if len(items) < 50:
                break
            page += 1

        return results

    # ── Live Room Search ──────────────────────────────────────

    # TODO(R3-ghost): suggest add to plugin.yaml as action `search_live_rooms` (independent capability, not internal); awaiting decision
    # Phase 3: marked private (`_search_live_rooms`) to pass C14; promote back to public when added to plugin.yaml.
    def _search_live_rooms(
        self,
        keyword: str,
        max_results: int = 20,
        live_only: bool = False,
    ) -> list[LiveRoom]:
        """Search live rooms by keyword.

        Args:
            keyword:     Search query
            max_results: Max results to return
            live_only:   Only return rooms that are currently live

        Returns:
            List of LiveRoom sorted by online count
        """
        api = self._get_api()
        rooms: list[LiveRoom] = []
        page = 1

        self._log(f"🔍 Search live rooms: '{keyword}' (max {max_results})")

        while len(rooms) < max_results:
            self._page_sleep()
            try:
                resp = api.search_live_rooms(keyword, page=page)
            except Exception as e:
                self._log(f"⚠️ Live search page {page} failed: {e}")
                break

            if resp.get("code") != 0:
                self._log(f"⚠️ Live search API error: {resp.get('message')}")
                break

            data = resp.get("data", {})
            result = data.get("result", [])

            # Handle different response formats:
            # B站搜索API返回的result可能是dict（包含live_room和live_user两个子列表）
            items = []
            if isinstance(result, dict):
                items.extend(result.get("live_room", []) or [])
                items.extend(result.get("live_user", []) or [])
            elif isinstance(result, list):
                items = result
            if not items:
                items = data.get("live_room", [])
            if not items:
                break

            for item in items:
                if len(rooms) >= max_results:
                    break

                uname = re.sub(r"<[^>]+>", "", item.get("uname", ""))
                title = re.sub(r"<[^>]+>", "", item.get("title", ""))
                area = re.sub(r"<[^>]+>", "", item.get("cate_name", ""))

                # Determine live status: live_room items use live_status,
                # live_user items use is_live (bool)
                live_status = item.get("live_status", 0)
                is_live_flag = item.get("is_live", None)
                if is_live_flag is not None:
                    # live_user format: is_live is a bool
                    is_live = bool(is_live_flag)
                    if live_status == 0 and is_live:
                        live_status = 1
                else:
                    is_live = (live_status == 1)

                room = LiveRoom(
                    room_id=item.get("roomid", 0),
                    uid=item.get("uid", 0),
                    uname=uname,
                    title=title,
                    online=item.get("online", 0),
                    live_status=live_status,
                    is_live=is_live,
                    area_name=area,
                    live_time=item.get("live_time", ""),
                    cover=item.get("cover", ""),
                    user_cover=item.get("user_cover", ""),
                    tags=item.get("tags", ""),
                )

                if live_only and not room.is_live:
                    continue

                rooms.append(room)

            if len(result) < 50:
                break
            page += 1

        # Sort by online count descending
        rooms.sort(key=lambda r: r.online, reverse=True)
        return rooms


    # ══════════════════════════════════════════════════════════
    #  R3 PUBLIC ACTIONS — signature `(self, ctx, params) -> None`
    #  These are the dispatch targets for service.execute().
    # ══════════════════════════════════════════════════════════

    def scrape_comments(self, ctx: TaskContext, params: dict) -> None:
        """Action: scrape_comments — fetch comments for a single video."""
        video_id = params["video_id"]
        assert isinstance(video_id, str), (
            f"scrape_comments expects single video_id (str), got {type(video_id).__name__}"
        )
        max_comments = params.get("max_comments")
        include_sub_comments = params.get("include_sub_comments", False)
        mode = params.get("mode", 3)

        # Reconfigure scraping policy on the existing instance.
        self.max_comments = max_comments
        self.include_sub_comments = include_sub_comments
        self.mode = mode

        ctx.check_cancelled()
        ctx.log(f"Scraping video: {video_id}")

        result = self._scrape_one(video_id)
        ctx.log(
            f"  result: status={result.status}, in_memory={len(result.comments)}, "
            f"total_fetched={result.total_fetched}"
        )

        if result.status == "ok":
            written = 0
            for comment in result.comments:
                ctx.check_cancelled()
                record = comment.to_dict()
                record["_source_video"] = video_id
                ctx.write_record(record)
                written += 1
            ctx.log(f"  [OK] {video_id}: written={written}")
            if written == 0:
                ctx.log("  [WARN] API returned ok but 0 comments in memory", level="WARN")
            if written != len(result.comments):
                ctx.log(
                    f"  [WARN] count mismatch: expected={len(result.comments)}, written={written}",
                    level="WARN",
                )
        else:
            ctx.log(f"  [ERR] {video_id}: {result.error}")
            raise RuntimeError(f"{video_id}: {result.error}")

        ctx.set_progress(1.0)

    def search_videos(self, ctx: TaskContext, params: dict) -> None:
        """Action: search_videos — keyword search returning SearchResult records."""
        keyword = params["keyword"]
        max_results = params.get("max_results", 20)
        sort_type = params.get("sort_type", "totalrank")
        duration = params.get("duration", 0)
        pubdate_begin = params.get("pubdate_begin")
        pubdate_end = params.get("pubdate_end")

        ctx.check_cancelled()
        ctx.log(f"Searching bilibili: '{keyword}' (max {max_results}, sort={sort_type})")

        results = self._search_videos_raw(
            keyword=keyword,
            max_results=max_results,
            sort_type=sort_type,
            duration=duration,
            pubdate_begin=pubdate_begin,
            pubdate_end=pubdate_end,
        )

        for r in results:
            ctx.check_cancelled()
            ctx.write_record(r.to_dict())

        ctx.log(f"  [OK] Found {len(results)} results for '{keyword}'")
        ctx.set_progress(1.0)

    @staticmethod
    def _normalize_live_url(url: str) -> str:
        if not url:
            return ""
        if url.startswith("//"):
            return "https:" + url
        if not url.startswith(("http://", "https://")):
            return "https://" + url.lstrip("/")
        return url

    def search_live_rooms(self, ctx: TaskContext, params: dict) -> None:
        """Action: search_live_rooms — keyword search for Bilibili live rooms."""
        keyword = params["keyword"]
        max_results = int(params.get("max_results", 20) or 20)
        live_only = bool(params.get("live_only", False))
        rooms = self._search_live_rooms(keyword=keyword, max_results=max_results, live_only=live_only)
        for room in rooms[:max_results]:
            ctx.check_cancelled()
            record = LiveRoomSearchResult(
                room_id=int(room.room_id or 0),
                uid=int(room.uid or 0),
                uname=room.uname,
                title=room.title,
                online=int(room.online or 0),
                live_status=int(room.live_status or 0),
                is_live=bool(room.is_live),
                area_name=room.area_name,
                cover=self._normalize_live_url(room.cover),
                user_cover=self._normalize_live_url(room.user_cover),
                live_time=room.live_time,
                tags=room.tags,
                keyword=keyword,
            )
            ctx.write_record(record.to_dict())
        ctx.set_progress(1.0)

    def get_live_room_info(self, ctx: TaskContext, params: dict) -> None:
        """Action: get_live_room_info — fetch a Bilibili live room snapshot."""
        room_id = extract_room_id(params["room_id"])
        data = self._get_api().get_live_room_snapshot(room_id)
        record = LiveRoomInfoRecord(
            room_id=int(data.get("room_id") or 0),
            short_id=int(data.get("short_id") or 0),
            uid=int(data.get("uid") or 0),
            title=str(data.get("title") or ""),
            live_status=int(data.get("live_status") or 0),
            is_live=bool(data.get("is_live")),
            live_time=str(data.get("live_time") or ""),
            online=int(data.get("online") or 0),
            area_id=int(data.get("area_id") or 0),
            area_name=str(data.get("area_name") or ""),
            parent_area_id=int(data.get("parent_area_id") or 0),
            parent_area_name=str(data.get("parent_area_name") or ""),
            uname=str(data.get("uname") or ""),
            face=str(data.get("face") or ""),
            follower_num=int(data.get("follower_num") or 0),
            cover=self._normalize_live_url(str(data.get("cover") or "")),
            keyframe=self._normalize_live_url(str(data.get("keyframe") or "")),
            fetched_at=str(data.get("fetched_at") or ""),
        )
        ctx.write_record(record.to_dict())
        ctx.set_progress(1.0)

    def collect_live_events(self, ctx: TaskContext, params: dict) -> None:
        """Action: collect_live_events — collect Bilibili live websocket events.

        Stop conditions are auto (no user toggle):
          1. duration_seconds reached
          2. task cancelled
          3. WSS connection closed
          4. ``PREPARING`` cmd with current roomid (broadcaster ended live)

        New params:
          - event_filter: list[str] of raw_cmd values to keep. Empty = all.
          - wait_until_live: bool, poll until live_status=1 first.
          - wait_timeout_seconds: int, default 86400 (24h).
        """
        room_id = extract_room_id(params["room_id"])
        duration = float(params.get("duration_seconds") or 0)  # 0 = run until live ends
        wait_until_live = bool(params.get("wait_until_live", False))
        wait_timeout = float(params.get("wait_timeout_seconds", 86400) or 86400)

        raw_filter = params.get("event_filter")
        event_filter: set[str] | None = None
        if raw_filter and isinstance(raw_filter, list) and len(raw_filter) > 0:
            event_filter = {str(x) for x in raw_filter if x}

        started = time.monotonic()

        def emit(event: dict) -> None:
            ctx.check_cancelled()
            record = LiveEventRecord(
                room_id=room_id,
                event_type=str(event.get("event_type") or "raw"),
                raw_cmd=str(event.get("raw_cmd") or ""),
                uid=str(event.get("uid") or ""),
                nickname=str(event.get("nickname") or ""),
                content=str(event.get("content") or ""),
                popularity=int(event.get("popularity") or 0),
                online_count=int(event.get("online_count") or 0),
                like_count=int(event.get("like_count") or 0),
                gift_id=str(event.get("gift_id") or ""),
                gift_name=str(event.get("gift_name") or ""),
                gift_count=int(event.get("gift_count") or 0),
                ts=round(time.monotonic() - started, 3),
                payload=json.dumps(event.get("payload") or {}, ensure_ascii=False),
            )
            ctx.write_record(record.to_dict())
            if duration > 0:
                ctx.set_progress(min(0.99, (time.monotonic() - started) / duration))

        count = self._get_api().collect_live_events(
            room_id=room_id,
            duration_seconds=duration,
            on_event=emit,
            is_cancelled=lambda: ctx.is_cancelled,
            event_filter=event_filter,
            wait_until_live=wait_until_live,
            wait_timeout_seconds=wait_timeout,
        )
        ctx.log(f"  [OK] room={room_id}: collected {count} live events")
        ctx.set_progress(1.0)

    def get_video_detail(self, ctx: TaskContext, params: dict) -> None:

        """Action: get_video_detail — fetch full VideoDetail record for a video."""
        video_id = params["video_id"]

        ctx.check_cancelled()
        ctx.log(f"Fetching video detail: {video_id}")

        # Normalize video_id: extract BV number from URL if needed
        bvid = video_id.strip()
        if "bilibili.com" in bvid:
            m = re.search(r"(BV[a-zA-Z0-9]+)", bvid)
            if m:
                bvid = m.group(1)

        # Reuse the client's raw view API (returns the full `data["data"]` payload).
        api = self._get_api()
        try:
            video_data = api.get_video_detail_raw(bvid)
        except Exception as e:
            ctx.record_error(f"{video_id}: {e}")
            ctx.log(f"  [ERR] {video_id}: {e}")
            ctx.set_progress(1.0)
            return

        detail = VideoDetail.from_api(video_data)
        ctx.write_record(detail.to_dict())
        ctx.log(f"  [OK] {video_id}: {detail.title}")
        ctx.set_progress(1.0)

    # ══════════════════════════════════════════════════════════
    #  get_video_ai_summary — wbi-signed single-shot AI 总结
    # ══════════════════════════════════════════════════════════

    # ── Constants for the AI-summary retry loop ───────────────────────
    # 与用户原 tenacity 配置等价：
    #   reraise=True, stop_after_attempt(3), wait_random(min=10, max=15)
    # 不引入新依赖，直接内联，让重试策略写在调用现场，调试时一眼就能看到。
    _AI_SUMMARY_MAX_ATTEMPTS = 3
    _AI_SUMMARY_BACKOFF_MIN_S = 10.0
    _AI_SUMMARY_BACKOFF_MAX_S = 15.0

    @staticmethod
    def _normalize_ai_summary(raw: str) -> str:
        """Strip newlines and convert ASCII commas to Chinese commas.

        Mirrors the user-provided post-processing exactly so downstream
        consumers see identical text. Centralised here so the action
        method stays linear.
        """
        return (raw or "").replace("\n", "").replace(",", "，")

    def _resolve_summary_signing_inputs(self, video_id: str) -> tuple[str, int, int]:
        """Resolve ``(bvid, cid, up_mid)`` from a free-form video id.

        ``view/conclusion/get`` requires all three signature inputs but
        callers should only need to know the video; we do one
        ``web-interface/view`` call to recover ``cid`` (first page) and
        ``up_mid`` (owner.mid). Callers may provide explicit cid/up_mid
        via params to skip this round-trip (rare).
        """
        bvid = extract_video_id(video_id)
        if not bvid:
            raise ValueError(f"Cannot parse video ID from: {video_id!r}")

        api = self._get_api()
        view = api.get_video_detail_raw(bvid)

        # cid: prefer the first entry of pages[]; fall back to top-level cid.
        pages = view.get("pages") or []
        cid = 0
        if pages and isinstance(pages, list):
            cid = int((pages[0] or {}).get("cid", 0) or 0)
        if not cid:
            cid = int(view.get("cid", 0) or 0)

        owner = view.get("owner") or {}
        up_mid = int(owner.get("mid", 0) or 0)

        if not cid or not up_mid:
            raise ValueError(
                f"{bvid}: missing cid/up_mid from view payload "
                f"(cid={cid}, up_mid={up_mid}); cannot sign conclusion request"
            )
        return bvid, cid, up_mid

    def _fetch_ai_summary_with_retry(
        self,
        ctx: TaskContext,
        bvid: str,
        cid: int,
        up_mid: int,
    ) -> str:
        """Call ``view/conclusion/get`` with a 3-attempt retry loop.

        Raises ``RuntimeError`` only when all attempts fail; otherwise
        returns the normalised summary string (may be empty when bilibili
        responds ``data.code != 0``, which means "no summary available").
        """
        last_error: Optional[BaseException] = None
        api = self._get_api()

        for attempt in range(1, self._AI_SUMMARY_MAX_ATTEMPTS + 1):
            ctx.check_cancelled()
            try:
                payload = api.get_video_ai_summary_raw(bvid, cid, up_mid)
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                self._log(f"⚠️ {bvid} AI-summary attempt {attempt} failed: {exc}")
            else:
                if payload.get("code") != 0:
                    last_error = RuntimeError(
                        f"top-level code={payload.get('code')}: {payload.get('message')}"
                    )
                    self._log(f"⚠️ {bvid} AI-summary attempt {attempt}: {last_error}")
                else:
                    data = payload.get("data") or {}
                    inner_code = data.get("code")
                    # B 站 conclusion 接口语义：
                    #   data.code == 0  -> 真有总结
                    #   data.code != 0  -> 接口正常，但该视频没有 AI 总结
                    # 后者属于业务空集，不应继续重试。
                    if inner_code != 0:
                        ctx.log(
                            f"  [INFO] {bvid}: no AI summary "
                            f"(data.code={inner_code})"
                        )
                        return ""
                    summary_raw = (data.get("model_result") or {}).get("summary", "") or ""
                    return self._normalize_ai_summary(summary_raw)

            if attempt < self._AI_SUMMARY_MAX_ATTEMPTS:
                delay = random.uniform(
                    self._AI_SUMMARY_BACKOFF_MIN_S,
                    self._AI_SUMMARY_BACKOFF_MAX_S,
                )
                time.sleep(delay)

        # All retries exhausted — surface the final cause.
        raise RuntimeError(
            f"AI summary request failed after {self._AI_SUMMARY_MAX_ATTEMPTS} attempts: {last_error}"
        )

    def get_video_ai_summary(self, ctx: TaskContext, params: dict) -> None:
        """Action: get_video_ai_summary — fetch B 站官方"视频 AI 总结".

        Inputs (params):
          - video_id  : BV / AV / 完整 URL                (required)
          - cid       : 分 P cid，省略则自动解析            (optional)
          - up_mid    : UP 主 mid，省略则自动解析           (optional)

        Output: 单条 ``VideoAISummary`` 记录。失败时记录 error 并继续，
        不中断批处理 —— 与 ``get_video_detail`` 的容错风格保持一致。
        """
        video_id = params["video_id"]
        cid_override = params.get("cid")
        up_mid_override = params.get("up_mid")

        ctx.check_cancelled()
        ctx.log(f"Fetching AI summary: {video_id}")

        try:
            if cid_override and up_mid_override:
                # 调用方已经自带 cid/up_mid（少见，但允许跳过详情拉取）
                bvid = extract_video_id(video_id) or str(video_id).strip()
                cid = int(cid_override)
                up_mid = int(up_mid_override)
            else:
                bvid, cid, up_mid = self._resolve_summary_signing_inputs(video_id)
        except Exception as e:  # noqa: BLE001
            ctx.record_error(f"{video_id}: cannot resolve signing inputs: {e}")
            ctx.log(f"  [ERR] {video_id}: {e}")
            ctx.set_progress(1.0)
            return

        try:
            summary = self._fetch_ai_summary_with_retry(ctx, bvid, cid, up_mid)
        except Exception as e:  # noqa: BLE001
            ctx.record_error(f"{video_id}: {e}")
            ctx.log(f"  [ERR] {video_id}: {e}")
            ctx.set_progress(1.0)
            return

        record = VideoAISummary(
            bvid=bvid,
            cid=cid,
            up_mid=up_mid,
            has_summary=bool(summary),
            summary=summary,
        )
        ctx.write_record(record.to_dict())
        if summary:
            ctx.log(f"  [OK] {bvid}: AI summary {len(summary)} chars")
        else:
            ctx.log(f"  [OK] {bvid}: no AI summary available")
        ctx.set_progress(1.0)
