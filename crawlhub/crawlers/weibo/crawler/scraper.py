"""
核心爬虫类 WeiboScraper — 封装所有微博数据采集接口

双通道架构:
- WeiboClient.api_session: weibo.com/ajax/ JSON API
- WeiboClient.search_session: s.weibo.com SSR HTML 解析

逆向分析详见 analysis/reverse_engineering.md

重构说明 (CRWL-002):
- 网络层 → crawler/client.py (WeiboClient)
- HTML 解析 → crawler/_internal/parsers.py (纯函数)
- 数据模型 → crawler/models.py (dataclass)
"""

import re
import csv
import json
import time
from pathlib import Path
from datetime import datetime
from urllib.parse import quote
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

import requests

from crawlhub.core.cookie_resolver import (
    CookieResolverMixin,
    CookieNotReady,
)
from crawlhub.core.config import get_data_root
from crawlhub.core.task_context import TaskContext

from .client import (
    WeiboClient,
    COOKIE, XSRF_TOKEN, DEFAULT_GAME, UA,
    MAX_SEARCH_PAGES, MAX_COMMENTS_PER_POST, REQUEST_INTERVAL, MAX_WORKERS,
)
from .models import (
    WeiboPost, WeiboComment, WeiboUser, WeiboTopic, HotSearchEntry,
    WeiboUserBrief, WeiboPostDetail,
)
from .utils import strip_html, parse_weibo_time
from .parsers import (
    parse_post_cards, parse_user_cards, parse_topic_cards,
)


class WeiboScraper(CookieResolverMixin):
    """
    微博通用爬虫 —— 支持搜索帖子、搜索用户、用户信息获取、官号识别等通用功能，
    同时保留一键游戏舆情监控模式。

    用法示例:
        scraper = WeiboScraper()

        # 通用搜索帖子
        posts = scraper._search_posts_raw("逆战·未来", sort="hot", max_pages=2)

        # 通用搜索用户
        users = scraper.search_users("逆战·未来", user_type="org_vip")

        # 获取用户详细信息
        info = scraper._get_user_info_raw(7780320531)

        # 智能识别官号
        officials = scraper.identify_official_accounts("逆战·未来")

        # 一键舆情监控（自动识别官号）
        scraper.run_game_monitor("逆战·未来")
    """

    PLATFORM_NAME = "weibo"

    def __init__(self, cookie: str = "", xsrf_token: str = ""):
        # cookie / xsrf_token are now optional. When omitted, the scraper
        # lazily loads the resolved cookie file (honoring the daemon's
        # thread-local override) on first use via ``_ensure_client_loaded``.
        self._explicit_cookie = cookie
        self._explicit_xsrf = xsrf_token
        self._client: Optional[WeiboClient] = None
        self._client_cookie_path: Optional[str] = None  # path the loaded client was built from

        # Public attrs kept for backward compatibility (populated lazily).
        self.cookie = cookie
        self.xsrf_token = xsrf_token

        # Eager init when caller passed a cookie string directly (legacy path).
        if cookie:
            self._client = WeiboClient(cookie=cookie, xsrf_token=xsrf_token)
            self.api_session = self._client.api_session
            self.search_session = self._client.search_session
            self.xsrf_token = self._client.xsrf_token

    # ------------------------------------------------------------------
    # CookieResolverMixin overrides
    # ------------------------------------------------------------------

    def get_crawler_cookie_path(self) -> Path:
        """Weibo uses a non-standard path: output/.weibo_cookie.json."""
        return get_data_root() / "crawlers" / "weibo" / "output" / ".weibo_cookie.json"

    def check_cookie_valid(self) -> bool:
        crawler_path = self.get_crawler_cookie_path()
        if crawler_path.exists():
            return True
        fallback = self.get_cookie_path()
        if fallback.exists():
            return True
        raise CookieNotReady("weibo", "No cookie file found. Please login first.")

    # ------------------------------------------------------------------
    # Cookie file parsing + lazy WeiboClient (re)build
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_weibo_cookie_file(cookie_data: dict) -> tuple[str, str]:
        """Parse cookie string and xsrf_token from various cookie file formats."""
        cookie_str = ""
        xsrf_token = ""

        if isinstance(cookie_data, dict):
            if "cookie" in cookie_data and isinstance(cookie_data["cookie"], str):
                cookie_str = cookie_data["cookie"]
                xsrf_token = cookie_data.get("xsrf_token", "")
            elif "cookies" in cookie_data and isinstance(cookie_data["cookies"], list):
                parts = []
                for c in cookie_data["cookies"]:
                    if isinstance(c, dict) and "name" in c and "value" in c:
                        parts.append(f"{c['name']}={c['value']}")
                        if c["name"] == "XSRF-TOKEN":
                            xsrf_token = c["value"]
                cookie_str = "; ".join(parts)
            elif "cookies" not in cookie_data and "cookie" not in cookie_data:
                parts = [f"{k}={v}" for k, v in cookie_data.items()
                         if k not in ("timestamp", "domain", "xsrf_token")]
                cookie_str = "; ".join(parts)
                xsrf_token = cookie_data.get("xsrf_token", "")

        return cookie_str, xsrf_token

    def _ensure_client_loaded(self) -> WeiboClient:
        """Lazily (re)build WeiboClient from the currently resolved cookie path.

        Re-creates the client whenever the resolved cookie path differs from
        the path used to build the cached client (so daemon thread-local
        cookie overrides take effect across tasks).
        """
        # If caller passed an explicit cookie string at construction, keep it.
        if self._explicit_cookie and self._client is not None:
            return self._client

        cookie_path = str(self.resolve_cookie_path())
        if self._client is not None and self._client_cookie_path == cookie_path:
            return self._client

        with open(cookie_path, "r", encoding="utf-8") as f:
            cookie_data = json.load(f)
        cookie_str, xsrf_token = self._parse_weibo_cookie_file(cookie_data)

        self._client = WeiboClient(cookie=cookie_str, xsrf_token=xsrf_token)
        self._client_cookie_path = cookie_path
        # Refresh public mirrors
        self.api_session = self._client.api_session
        self.search_session = self._client.search_session
        self.cookie = cookie_str
        self.xsrf_token = self._client.xsrf_token

        return self._client


    def _bind_task(self, ctx: TaskContext) -> None:
        """Ensure client matches the current resolved cookie path."""
        self._ensure_client_loaded()


    @property
    def client(self) -> WeiboClient:
        """Lazy-loaded WeiboClient (R3 bridge-less code paths use this)."""
        return self._ensure_client_loaded()

    # ============================================================
    # A. 通用搜索 —— 搜帖子
    # 接口: GET https://s.weibo.com/weibo?q={keyword}&page={page}
    # 数据格式: SSR HTML，需正则解析 card-wrap 卡片
    # ============================================================
    # R3: renamed from public ``search_posts`` -> internal raw helper.
    # The R3 plugin action ``search_posts(ctx, params)`` lives at the bottom
    # of this file and wraps this helper to honor the (ctx, params) signature.
    def _search_posts_raw(self, keyword: str, sort: str = "default",
                          time_start: str = "", time_end: str = "",
                          only_original: bool = False, only_verified: bool = False,
                          has_video: bool = False, has_pic: bool = False,
                          region: str = "",
                          max_pages: int = MAX_SEARCH_PAGES,
                          verbose: bool = True) -> list[dict]:
        """
        通用帖子搜索，暴露所有搜索参数。

        参数:
            keyword:        搜索关键词
            sort:           排序方式 "default"(综合), "time"(最新), "hot"(热门)
            time_start:     起始时间 "YYYY-MM-DD" (可选)
            time_end:       结束时间 "YYYY-MM-DD" (可选)
            only_original:  仅看原创
            only_verified:  仅看认证用户
            has_video:      仅含视频
            has_pic:        仅含图片
            region:         地区代码 (如 "11:1000" 表示北京)
            max_pages:      最大翻页数

        返回: [{ mid, 用户名, 用户UID, 认证用户, 正文, 发布时间, ... }, ...]
        """
        params = [f"q={quote(keyword)}"]

        if sort == "time":
            params.append("xsort=time")
            params.append("suball=1")
        elif sort == "hot":
            params.append("xsort=hot")
            params.append("suball=1")

        if time_start or time_end:
            ts = time_start.replace("-", "") if time_start else ""
            te = time_end.replace("-", "") if time_end else ""
            scope = f"custom:{ts}-0:{te}-0" if ts or te else ""
            if scope:
                params.append(f"timescope={scope}")

        if only_original:
            params.append("auth=ori")
        if only_verified:
            params.append("auth=vip")
        if has_video:
            params.append("hasvideo=1")
        if has_pic:
            params.append("haspic=1")
        if region:
            params.append(f"region=custom:{region}")

        sort_label = {"default": "综合", "time": "最新", "hot": "热门"}.get(sort, sort)
        if verbose:
            print(f"\n  [SEARCH] 搜索帖子【{sort_label}】: {keyword}")

        all_items: list[dict] = []
        seen_mids = set()

        for page in range(1, max_pages + 1):
            url = f"https://s.weibo.com/weibo?{'&'.join(params)}&page={page}"
            if verbose:
                print(f"    第 {page}/{max_pages} 页...", end=" ")

            resp = self._client.get_search(url)
            if resp.status_code == 302 or "passport.weibo.com" in resp.url:
                print("[ERR] Cookie 已失效")
                break

            dict_items = parse_post_cards(resp.text, sort_label)
            new_count = 0
            for d in dict_items:
                mid = d.get("mid", "")
                if mid and mid not in seen_mids:
                    seen_mids.add(mid)
                    all_items.append(d)
                    new_count += 1

            if verbose:
                print(f"[OK] 解析 {len(dict_items)} 条，新增 {new_count} 条")
            if not dict_items:
                if verbose:
                    print(f"    [WARN] 无更多结果")
                break
            if page < max_pages:
                self._client.sleep()

        return all_items

    # ============================================================
    # B. 通用搜索 —— 搜用户
    # 接口: GET https://s.weibo.com/user?q={keyword}&page={page}
    # 数据格式: SSR HTML，需正则解析 card-user-b 卡片
    # ============================================================
    # TODO(R3-ghost): suggest add to plugin.yaml as action `search_users` (independent capability used by identify_official_accounts and standalone); awaiting decision
    def search_users(self, keyword: str, user_type: str = "all",
                     flag: str = "all", gender: str = "all",
                     age: str = "all",
                     max_pages: int = MAX_SEARCH_PAGES,
                     fetch_detail: bool = True,
                     verbose: bool = True) -> list[dict]:
        """
        通用用户搜索，暴露所有搜索参数。

        参数:
            keyword:      搜索关键词
            user_type:    用户类型 "all", "org_vip"(机构认证), "per_vip"(个人认证), "ord"(普通)
            flag:         搜索维度 "all", "nickname", "tag", "school", "work"
            gender:       性别 "all", "man", "women"
            age:          年龄 "all", "18y", "22y", "29y", "39y", "40y"
            max_pages:    最大翻页数
            fetch_detail: 是否对每个用户调用 profile/info API 获取详细信息

        返回: [{ UID, 用户名, 粉丝数, 认证类型, 简介, ... }, ...]
        """
        type_label = {"all": "全部", "org_vip": "机构认证", "per_vip": "个人认证", "ord": "普通用户"}
        if verbose:
            print(f"\n  [SEARCH] 搜索用户【{type_label.get(user_type, user_type)}】: {keyword}")

        all_users = []
        seen_uids = set()

        for page in range(1, max_pages + 1):
            params = [f"q={quote(keyword)}", f"page={page}"]
            if user_type != "all":
                params.append(f"sort={user_type}")
            if flag != "all":
                params.append(f"flag={flag}")
            if gender != "all":
                params.append(f"gender={gender}")
            if age != "all":
                params.append(f"age={age}")

            url = f"https://s.weibo.com/user?{'&'.join(params)}"
            if verbose:
                print(f"    第 {page}/{max_pages} 页...", end=" ")

            resp = self._client.get_search(url)
            if resp.status_code == 302 or "passport.weibo.com" in resp.url:
                print("[ERR] Cookie 已失效")
                break

            users = parse_user_cards(resp.text)
            new_count = 0
            for user in users:
                uid = user.get("UID", "")
                if uid and uid not in seen_uids:
                    seen_uids.add(uid)
                    all_users.append(user)
                    new_count += 1

            if verbose:
                print(f"[OK] 解析 {len(users)} 条，新增 {new_count} 条")
            if not users:
                break
            if page < max_pages:
                self._client.sleep()

        # 并发获取详细用户信息
        if fetch_detail and all_users:
            total = len(all_users)
            if verbose:
                print(f"    [INFO] 并发获取 {total} 个用户的详细信息...")
            t_start = time.time()

            def _fetch_one(idx_user):
                idx, u = idx_user
                uid = u.get("UID", "")
                if not uid:
                    return idx, None
                detail = self._get_user_info_raw(int(uid), verbose=False)
                return idx, detail

            with ThreadPoolExecutor(max_workers=min(total, MAX_WORKERS)) as pool:
                futures = {pool.submit(_fetch_one, (i, u)): i for i, u in enumerate(all_users)}
                done_count = 0
                for future in as_completed(futures):
                    idx, detail = future.result()
                    done_count += 1
                    if detail:
                        all_users[idx].update(detail)
                    if verbose:
                        uid = all_users[idx].get("UID", "?")
                        uname = (detail.get("用户名", all_users[idx].get("用户名", "?"))
                                 if detail else all_users[idx].get("用户名", "?"))
                        print(f"      [{done_count}/{total}] {uname} (UID:{uid})")

            if verbose:
                print(f"    [OK] 详细信息获取完成 (并发耗时 {time.time() - t_start:.1f}s)")

        return all_users

    # ============================================================
    # C. 通用搜索 —— 搜话题
    # 接口: GET https://s.weibo.com/topic?q={keyword}&pagetype=topic&topic=1&page={page}
    # ============================================================
    # TODO(R3-ghost): suggest add to plugin.yaml as action `search_topics` (independent capability); awaiting decision
    def search_topics(self, keyword: str, max_pages: int = 1,
                      verbose: bool = True) -> list[dict]:
        """搜索话题。返回: [{ 话题名, 描述, 讨论数, 阅读数, 链接 }, ...]"""
        if verbose:
            print(f"\n  [SEARCH] 搜索话题: {keyword}")
        all_topics = []

        for page in range(1, max_pages + 1):
            url = f"https://s.weibo.com/topic?q={quote(keyword)}&pagetype=topic&topic=1&page={page}"
            if verbose:
                print(f"    第 {page}/{max_pages} 页...", end=" ")

            resp = self._client.get_search(url)
            if resp.status_code == 302 or "passport.weibo.com" in resp.url:
                print("[ERR] Cookie 已失效")
                break

            topics = parse_topic_cards(resp.text)
            all_topics.extend(topics)
            if verbose:
                print(f"[OK] {len(topics)} 个话题")

            if not topics:
                break
            if page < max_pages:
                self._client.sleep()

        return all_topics

    # ============================================================
    # D. 用户信息 API
    # 接口: GET https://weibo.com/ajax/profile/info?uid={uid}
    #       GET https://weibo.com/ajax/profile/detail?uid={uid}
    # ============================================================
    # R3: renamed from public ``get_user_info`` -> internal raw helper.
    # The R3 plugin action ``get_user_info(ctx, params)`` at the bottom of
    # this file uses ``WeiboUserBrief`` shape directly via self.client; this
    # helper still exists for ``search_users``/``identify_official_accounts``
    # which need the richer (Chinese-keyed) fields.
    def _get_user_info_raw(self, uid: int, verbose: bool = True) -> dict:
        """获取用户详细信息 (合并 profile/info + profile/detail)"""
        if verbose:
            print(f"\n  [INFO] 获取用户信息: UID={uid}")

        info = {}

        # 1) profile/info — 基础信息
        try:
            resp = self._client.get_api(
                f"https://weibo.com/ajax/profile/info?uid={uid}", timeout=15
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("ok") == 1:
                user = data.get("data", {}).get("user", {})
                vtype = user.get("verified_type", -1)
                info = {
                    "uid": str(user.get("id", uid)),
                    "user_name": user.get("screen_name", ""),
                    "followers_count": user.get("followers_count", 0),
                    "followers_str": user.get("followers_count_str", ""),
                    "friends_count": user.get("friends_count", 0),
                    "statuses_count": user.get("statuses_count", 0),
                    "verified": "是" if user.get("verified") else "否",
                    "verified_type_code": vtype,
                    "verified_type": self._verified_type_label(vtype),
                    "verified_reason": user.get("verified_reason", ""),
                    "description": user.get("description", ""),
                    "location": user.get("location", ""),
                    "gender": {"m": "男", "f": "女"}.get(user.get("gender", ""), "未知"),
                    "avatar": user.get("avatar_hd", ""),
                    "profile_url": f"https://weibo.com/u/{user.get('id', uid)}",
                    "svip": user.get("svip", 0),
                    "total_counter": user.get("status_total_counter", {}).get("total_cnt_format", ""),
                }
        except Exception as e:
            if verbose:
                print(f"    [WARN] profile/info 请求失败: {e}")

        # 2) profile/detail — 补充字段 (IP属地, 注册时间等)
        self._client.sleep(0.5)
        try:
            resp = self._client.get_api(
                f"https://weibo.com/ajax/profile/detail?uid={uid}", timeout=15
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("ok") == 1:
                detail = data.get("data", {})
                info["ip_location"] = detail.get("ip_location", "").replace("IP属地：", "")
                info["created_at"] = detail.get("created_at", "")
                info["sunshine_credit"] = detail.get("sunshine_credit", {}).get("level", "")
                info["birthday"] = detail.get("birthday", "")
                labels = detail.get("label_desc", [])
                info["labels"] = " | ".join(l.get("name", "") for l in labels) if labels else ""
        except Exception as e:
            if verbose:
                print(f"    [WARN] profile/detail 请求失败: {e}")

        info["user_type"] = self._classify_user_type(info)

        if verbose and info:
            print(f"    [OK] {info.get('user_name', 'N/A')} | {info.get('verified_type', '未认证')} | "
                  f"粉丝 {info.get('followers_str', info.get('followers_count', 0))}")

        return info

    # ============================================================
    # E. 智能官号识别
    # ============================================================
    # TODO(R3-ghost): suggest add to plugin.yaml as action `identify_official_accounts` (high-value composite capability); awaiting decision
    def identify_official_accounts(self, keyword: str,
                                   max_search_pages: int = 2,
                                   verbose: bool = True) -> list[dict]:
        """
        智能识别与关键词相关的官方账号。
        策略: 搜索用户 → API 详情 → 多维度评分 (认证+关键词+粉丝)
        """
        if verbose:
            print(f"\n  🔍 智能识别官方账号: {keyword}")

        kw_variants = self._build_keyword_variants(keyword)
        if verbose:
            print(f"    关键词变体: {kw_variants}")

        users = self.search_users(keyword, user_type="all",
                                  max_pages=max_search_pages, fetch_detail=True,
                                  verbose=verbose)

        scored = []
        for user in users:
            score = self._score_official(user, kw_variants)
            user["official_score"] = score
            scored.append(user)

        scored.sort(key=lambda x: x["official_score"], reverse=True)
        officials = [u for u in scored if u["official_score"] >= 50]

        if verbose:
            print(f"\n    [OK] 识别结果: 共 {len(officials)} 个可能官号")
            for u in officials:
                print(f"      [{u['official_score']}分] {u.get('user_name', 'N/A')} "
                      f"(UID: {u.get('uid', 'N/A')}) — {u.get('verified_type', '未认证')} — "
                      f"粉丝: {u.get('followers_str', u.get('followers_count', 0))}")

        return officials

    # ============================================================
    # F. 评论接口
    # 接口: GET https://weibo.com/ajax/statuses/buildComments
    # 翻页: max_id 游标 (max_id=0 表示无更多数据)
    # ============================================================
    # TODO(R3-ghost): suggest rename to `_fetch_comments` (internal helper; plugin action `scrape_comments` will wrap it); awaiting decision
    def fetch_comments(self, mid: str, uid: str = "",
                       max_count: int = MAX_COMMENTS_PER_POST,
                       verbose: bool = True) -> list[dict]:
        """获取指定微博的评论"""
        if verbose:
            print(f"    💬 获取 mid={mid} 的评论...", end=" ")
        all_comments = []
        max_id = 0

        while len(all_comments) < max_count:
            params = {
                "is_reload": 1, "id": mid, "is_show_bulletin": 2,
                "is_mix": 0, "count": min(max_count, 20),
                "uid": uid, "fetch_level": 0, "locale": "zh-CN",
            }
            if max_id:
                params["max_id"] = max_id

            try:
                resp = self._client.get_api(
                    "https://weibo.com/ajax/statuses/buildComments",
                    params=params, timeout=30
                )
                resp.raise_for_status()
                data = resp.json()
            except (requests.RequestException, json.JSONDecodeError) as e:
                print(f"[ERR] {e}")
                break

            if data.get("ok") != 1:
                break

            comment_list = data.get("data", [])
            if not comment_list:
                break

            for cmt in comment_list:
                user = cmt.get("user", {})
                all_comments.append({
                    "source_mid": mid,
                    "comment_id": str(cmt.get("id", "")),
                    "user_name": user.get("screen_name", ""),
                    "user_uid": str(user.get("id", "")),
                    "content": strip_html(cmt.get("text_raw", cmt.get("text", "")))[:500],
                    "pub_time": parse_weibo_time(cmt.get("created_at", "")),
                    "source": cmt.get("source", ""),
                    "like_count": cmt.get("like_counts", 0),
                    "floor": cmt.get("floor_number", 0),
                })

            max_id = data.get("max_id", 0)
            if not max_id:
                break
            self._client.sleep(1)

        if verbose:
            print(f"[OK] {len(all_comments)} 条评论")
        return all_comments

    # ============================================================
    # G. 用户微博列表
    # 接口: GET https://weibo.com/ajax/statuses/mymblog?uid={uid}&page={page}&feature=0
    # ============================================================
    # TODO(R3-ghost): suggest rename to `_fetch_user_posts` (internal helper; plugin action `scrape_user_posts` will wrap it; see baseline confirmed-decision §3); awaiting decision
    def fetch_user_posts(self, uid: int, max_pages: int = 2,
                         verbose: bool = True) -> list[dict]:
        """获取指定用户的最新微博"""
        if verbose:
            print(f"\n  [INFO] 获取用户 {uid} 的微博动态")
        all_posts = []

        for page in range(1, max_pages + 1):
            url = f"https://weibo.com/ajax/statuses/mymblog?uid={uid}&page={page}&feature=0"
            if verbose:
                print(f"    第 {page}/{max_pages} 页...", end=" ")

            try:
                resp = self._client.get_api(url, timeout=30)
                resp.raise_for_status()
                data = resp.json()
            except (requests.RequestException, json.JSONDecodeError) as e:
                print(f"[ERR] {e}")
                break

            if data.get("ok") != 1:
                break

            post_list = data.get("data", {}).get("list", [])
            if not post_list:
                if verbose:
                    print("[WARN] 无更多数据")
                break

            for post in post_list:
                user = post.get("user", {})
                raw_text = strip_html(post.get("text_raw", post.get("text", "")))
                topics = re.findall(r'#([^#]+)#', raw_text)
                all_posts.append({
                    "source_type": "user_posts",
                    "mid": post.get("mid", ""),
                    "user_name": user.get("screen_name", ""),
                    "user_uid": str(user.get("id", "")),
                    "verified": "是" if user.get("verified") else "否",
                    "content": raw_text[:500],
                    "pub_time": parse_weibo_time(post.get("created_at", "")),
                    "source": post.get("source", ""),
                    "repost_count": post.get("reposts_count", 0),
                    "comment_count": post.get("comments_count", 0),
                    "like_count": post.get("attitudes_count", 0),
                    "topic_tags": "|".join(topics) if topics else "",
                    "url": f"https://weibo.com/{user.get('id', '')}/{post.get('mblogid', '')}",
                })

            if verbose:
                print(f"[OK] {len(post_list)} 条")
            if page < max_pages:
                self._client.sleep(REQUEST_INTERVAL)

        return all_posts

    # ============================================================
    # H. 话题接口
    # 接口: GET https://weibo.com/ajax/statuses/topic_band?topic={topic_name}
    # ============================================================
    # TODO(R3-ghost): suggest add to plugin.yaml as action `fetch_topic_band` (independent capability); awaiting decision
    def fetch_topic_band(self, topic_name: str, verbose: bool = True) -> dict:
        """获取话题相关微博和热度数据"""
        if verbose:
            print(f"\n  [INFO] 获取话题 #{topic_name}# 数据")

        try:
            resp = self._client.get_api(
                "https://weibo.com/ajax/statuses/topic_band",
                params={"topic": topic_name}, timeout=30
            )
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, json.JSONDecodeError) as e:
            if verbose:
                print(f"    [ERR] {e}")
            return {}

        if data.get("ok") != 1:
            return {}

        statuses = data.get("data", {}).get("statuses", [])
        result = {
            "topic_name": f"#{topic_name}#",
            "query_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "related_post_count": len(statuses),
            "hot_post_summary": [],
        }
        for s in statuses[:10]:
            result["hot_post_summary"].append({
                "topic": s.get("topic", ""),
                "read_count": s.get("read", 0),
                "mention_count": s.get("mention", 0),
                "summary": s.get("summary", ""),
            })

        if verbose:
            print(f"    [OK] {len(statuses)} 条话题相关微博")
        return result

    # ============================================================
    # I. 热搜接口
    # 接口: GET https://weibo.com/ajax/side/hotSearch (无需参数)
    # ============================================================
    # TODO(R3-ghost): suggest add to plugin.yaml as action `fetch_hot_search` (independent capability, useful for monitoring); awaiting decision
    def fetch_hot_search(self, check_keywords: list[str] | None = None,
                         verbose: bool = True) -> dict:
        """获取微博热搜榜，可选检查某些关键词是否上榜"""
        if verbose:
            print(f"\n  [INFO] 获取微博热搜榜")

        try:
            resp = self._client.get_api(
                "https://weibo.com/ajax/side/hotSearch", timeout=15
            )
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, json.JSONDecodeError) as e:
            if verbose:
                print(f"    [ERR] {e}")
            return {}

        if data.get("ok") != 1:
            return {}

        realtime = data.get("data", {}).get("realtime", [])

        matched = []
        if check_keywords:
            for item in realtime:
                word = item.get("word", "")
                for kw in check_keywords:
                    if kw in word:
                        matched.append({
                            "word": word,
                            "rank": item.get("rank", "N/A"),
                            "heat": item.get("description", ""),
                            "icon_desc": item.get("icon_desc", ""),
                        })
                        break

        result = {
            "query_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "hot_search_total": len(realtime),
            "keyword_hit_count": len(matched),
            "keyword_hits": matched,
            "top10": [
                {"rank": i.get("rank", ""), "word": i.get("word", ""),
                 "heat": i.get("description", ""), "icon_desc": i.get("icon_desc", "")}
                for i in realtime[:10]
            ],
        }

        if verbose:
            if matched:
                print(f"    [OK] 上榜 {len(matched)} 个词条")
            else:
                print(f"    [OK] 热搜总数: {len(realtime)}" +
                      (f"，关键词未上榜" if check_keywords else ""))
        return result

    # ============================================================
    # J. 一键游戏舆情监控
    # ============================================================
    # TODO(R3-ghost): suggest delete (CLI-only mega-orchestrator using save_csv; R3 prefers narrower actions composed by upstream); awaiting decision
    def run_game_monitor(self, game_name: str = DEFAULT_GAME,
                         official_uids: list[int] | None = None,
                         max_search_pages: int = MAX_SEARCH_PAGES):
        """执行完整的游戏舆情监控流程"""
        print("=" * 60)
        print(f"微博游戏舆情监控 - 目标: {game_name}")
        print(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print("=" * 60)

        output_dir = Path(__file__).parent.parent / "output" / "data"
        output_dir.mkdir(parents=True, exist_ok=True)

        # 并行第一波
        print("\n⏳ 并行采集中 (综合/实时/热门搜索 + 话题搜索 + 官号识别) ...")
        results = {}

        task_map = {
            "search_comp": lambda: self._search_posts_raw(game_name, sort="default", max_pages=max_search_pages, verbose=False),
            "search_rt": lambda: self._search_posts_raw(game_name, sort="time", max_pages=max_search_pages, verbose=False),
            "search_hot": lambda: self._search_posts_raw(game_name, sort="hot", max_pages=max_search_pages, verbose=False),
            "topic_search": lambda: self.search_topics(game_name, max_pages=1, verbose=False),
            "identify_officials": lambda: (None if official_uids is not None
                                           else self.identify_official_accounts(game_name, max_search_pages=2, verbose=False)),
        }

        task_labels = {
            "search_comp": "综合搜索", "search_rt": "实时搜索", "search_hot": "热门搜索",
            "topic_search": "话题搜索", "identify_officials": "官号识别",
        }

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {pool.submit(fn): key for key, fn in task_map.items()}
            for fut in as_completed(futures):
                key = futures[fut]
                try:
                    res = fut.result()
                    results[key] = res
                    count = len(res) if isinstance(res, (list, dict)) else 0
                    print(f"  [OK] {task_labels[key]} 完成 ({count} 条)")
                except Exception as e:
                    results[key] = []
                    print(f"  [WARN] {task_labels[key]} 失败: {e}")

        search_comp = results.get("search_comp", [])
        search_rt = results.get("search_rt", [])
        search_hot = results.get("search_hot", [])
        topic_search = results.get("topic_search", [])

        officials_result = results.get("identify_officials")
        if official_uids is None:
            officials = officials_result or []
            official_uids = [int(u["uid"]) for u in officials if u.get("uid")]
            if officials:
                self.save_csv(officials, output_dir / "identified_officials.csv",
                              ["uid", "user_name", "followers_count", "followers_str", "friends_count", "statuses_count",
                               "verified", "verified_type", "verified_reason", "description", "location", "gender",
                               "ip_location", "created_at", "user_type", "official_score"])
                print(f"  [OK] 官号识别结果 -> identified_officials.csv ({len(officials)} 个)")

        # 并行第二波
        print("\n⏳ 并行采集中 (官号动态 + 热门帖子评论) ...")
        top_posts = sorted(search_comp + search_hot, key=lambda x: x.get("comment_count", 0), reverse=True)[:5]
        official_posts = []
        all_comments = []

        wave2_tasks = []
        for uid in (official_uids or []):
            wave2_tasks.append(("official", uid, lambda u: self.fetch_user_posts(u, verbose=False), uid))
        for post in top_posts:
            mid = post.get("mid", "")
            uid_str = post.get("user_uid", "")
            wave2_tasks.append(("comment", mid, lambda p: self.fetch_comments(p.get("mid", ""), p.get("user_uid", ""), verbose=False), post))

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {}
            for tag, label, fn, arg in wave2_tasks:
                futures[pool.submit(fn, arg)] = (tag, label)
            for fut in as_completed(futures):
                tag, label = futures[fut]
                try:
                    data = fut.result()
                    if tag == "official":
                        official_posts.extend(data)
                    else:
                        all_comments.extend(data)
                except Exception as e:
                    print(f"  [WARN] {tag}({label}) 失败: {e}")

        print(f"  [OK] 官号动态 {len(official_posts)} 条, 评论 {len(all_comments)} 条")

        # 保存
        print("\n\n💾 保存数据...")
        post_fields = ["source_type", "mid", "user_name", "user_uid", "verified", "content",
                        "pub_time", "source", "repost_count", "comment_count", "like_count", "topic_tags", "url"]

        for data, label, fname in [
            (search_comp, "综合", "search_comprehensive.csv"),
            (search_rt, "实时", "search_realtime.csv"),
            (search_hot, "热门", "search_hot.csv"),
        ]:
            if data:
                self.save_csv(data, output_dir / fname, post_fields)
                print(f"  [OK] {label}搜索 -> {fname} ({len(data)} 条)")

        if official_posts:
            self.save_csv(official_posts, output_dir / "official_posts.csv", post_fields)
            print(f"  [OK] 官方动态 -> official_posts.csv ({len(official_posts)} 条)")

        if all_comments:
            self.save_csv(all_comments, output_dir / "comments.csv",
                          ["source_mid", "comment_id", "user_name", "user_uid",
                           "content", "pub_time", "source", "like_count", "floor"])
            print(f"  [OK] 评论数据 -> comments.csv ({len(all_comments)} 条)")

        if topic_search:
            self.save_csv(topic_search, output_dir / "topic_search.csv",
                          ["topic_name", "description", "discuss_count", "read_count", "url"])
            print(f"  [OK] 话题搜索 -> topic_search.csv ({len(topic_search)} 个)")

        print(f"\n{'=' * 60}")
        print("[INFO] 采集汇总:")
        print(f"  综合搜索: {len(search_comp)} 条")
        print(f"  实时搜索: {len(search_rt)} 条")
        print(f"  热门帖子: {len(search_hot)} 条")
        print(f"  官方动态: {len(official_posts)} 条 (官号: {len(official_uids)} 个 — 自动识别)")
        print(f"  评论数据: {len(all_comments)} 条")
        print(f"  话题搜索: {len(topic_search)} 个话题")
        print(f"{'=' * 60}")

    # ============================================================
    # 内部方法 —— 用户分类和评分
    # ============================================================
    @staticmethod
    def _verified_type_label(vtype: int) -> str:
        mapping = {
            -1: "未认证", 0: "个人认证(黄V)", 1: "政府机构",
            2: "企业官方(蓝V)", 3: "媒体", 4: "校园",
            5: "其他机构", 7: "企业官方(蓝V)", 8: "其他认证",
        }
        return mapping.get(vtype, f"其他({vtype})")

    @staticmethod
    def _classify_user_type(info: dict) -> str:
        vtype = info.get("verified_type_code", -1)
        fans = info.get("followers_count", 0)
        if vtype in (1, 2, 3, 5, 7):
            return "官方/机构"
        elif vtype == 0 and fans >= 50000:
            return "KOL/大V"
        elif vtype == 0:
            return "个人认证"
        elif fans >= 10000:
            return "活跃用户"
        else:
            return "普通用户"

    @staticmethod
    def _build_keyword_variants(keyword: str) -> list[str]:
        variants = [keyword]
        clean = keyword.replace("·", "").replace("：", "").replace(":", "")
        if clean != keyword:
            variants.append(clean)
        v2 = keyword.replace("·", ":").replace("·", "：")
        if v2 not in variants:
            variants.append(v2)
        parts = re.split(r'[·：:\s]+', keyword)
        if len(parts) > 1:
            variants.extend(parts)
        return list(set(variants))

    @staticmethod
    def _score_official(user: dict, kw_variants: list[str]) -> int:
        """
        官号置信度打分 (0-100)
        核心: 认证(40) + 关键词匹配(30) = 70 分基础
        """
        score = 0
        name = user.get("user_name", "")
        desc = user.get("description", "")
        reason = user.get("verified_reason", "")
        vtype = user.get("verified_type_code", -1)
        fans = user.get("followers_count", 0)
        is_verified = vtype >= 0

        if vtype in (1, 2, 3, 5, 7):
            score += 40
        elif vtype == 0:
            score += 15

        long_kws = [kw for kw in kw_variants if len(kw) >= 3]
        for kw in long_kws:
            if kw and kw in reason:
                score += 30
                break

        for kw in long_kws:
            if kw and kw.lower() in name.lower():
                score += 15 if is_verified else 5
                break

        for kw in long_kws:
            if kw and kw in desc:
                score += 10
                break

        for tag in ["官方", "官博", "官微", "官网", "official"]:
            if tag in name.lower():
                score += 15
                break

        if fans >= 100000:
            score += 10
        elif fans >= 10000:
            score += 5

        if not is_verified and fans < 1000:
            score = min(score, 25)

        return min(score, 100)

    # ============================================================
    # 数据保存
    # ============================================================
    # TODO(R3-ghost): suggest rename to `_save_csv` (CLI-only helper, R3 uses ctx.write_record); awaiting decision
    @staticmethod
    def save_csv(data: list[dict], filepath: Path, fieldnames: list[str]):
        with open(filepath, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(data)

    # TODO(R3-ghost): suggest rename to `_save_json` (CLI-only helper, R3 uses ctx.write_record); awaiting decision
    @staticmethod
    def save_json(data, filepath: Path):
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    # ============================================================
    # R3 plugin actions — signature: (self, ctx: TaskContext, params: dict) -> None
    # These wrap internal helpers, write records via ctx, and report progress.
    # ============================================================

    def search_posts(self, ctx: TaskContext, params: dict) -> None:
        """R3 action: search weibo posts by keyword and stream to ctx."""
        self._bind_task(ctx)

        keyword = params["keyword"]
        sort = params.get("sort", "default")
        max_pages = int(params.get("max_pages", MAX_SEARCH_PAGES))
        time_start = params.get("time_start") or ""
        time_end = params.get("time_end") or ""
        only_original = bool(params.get("only_original", False))

        ctx.log(f"Searching weibo: '{keyword}' (sort={sort}, pages={max_pages})")

        try:
            results = self._search_posts_raw(
                keyword=keyword,
                sort=sort,
                max_pages=max_pages,
                time_start=time_start,
                time_end=time_end,
                only_original=only_original,
                verbose=False,
            )
            for r in results:
                ctx.write_record(r)
            ctx.log(f"  [OK] Found {len(results)} posts for '{keyword}'")
        except Exception as e:
            ctx.record_error(f"Search failed: {e}", response=e)
            ctx.log(f"  [ERR] Search failed: {e}")

        ctx.set_progress(1.0)

    def get_user_info(self, ctx: TaskContext, params: dict) -> None:
        """R3 action: fetch a single user's profile, emit one WeiboUserBrief record."""
        self._bind_task(ctx)

        uid = int(params["uid"])
        ctx.log(f"[weibo] Fetching user info for uid={uid}")

        def _parse_int_with_commas(val) -> int:
            """Parse '38,012' or 38012 or '' -> int."""
            if isinstance(val, int):
                return val
            if isinstance(val, str):
                return int(val.replace(",", "")) if val.strip() else 0
            return 0

        resp = None
        try:
            url = f"https://weibo.com/ajax/profile/info?uid={uid}"
            resp = self.client.get_api(url, timeout=15)
            resp.raise_for_status()
            data = resp.json()

            if data.get("ok") != 1:
                ctx.record_error(f"uid={uid}: API returned error: {data}", response=resp)
                ctx.log(f"  [ERR] uid={uid}: API error")
                ctx.set_progress(1.0)
                return

            user = data.get("data", {}).get("user", {})

            # Interaction counters from status_total_counter
            counter = user.get("status_total_counter", {})
            comment_cnt = _parse_int_with_commas(counter.get("comment_cnt", 0))
            repost_cnt = _parse_int_with_commas(counter.get("repost_cnt", 0))
            like_cnt = _parse_int_with_commas(counter.get("like_cnt", 0))
            total_cnt = _parse_int_with_commas(counter.get("total_cnt", 0))

            # Gender: m -> 男, f -> 女
            gender_raw = user.get("gender", "")
            gender = {"m": "男", "f": "女"}.get(gender_raw, "")

            record = WeiboUserBrief(
                uid=int(uid),
                screen_name=user.get("screen_name", ""),
                description=user.get("description", ""),
                verified=bool(user.get("verified", False)),
                verified_reason=user.get("verified_reason", ""),
                comment_cnt=comment_cnt,
                repost_cnt=repost_cnt,
                like_cnt=like_cnt,
                total_cnt=total_cnt,
                followers_count=int(user.get("followers_count", 0) or 0),
                friends_count=int(user.get("friends_count", 0) or 0),
                statuses_count=int(user.get("statuses_count", 0) or 0),
                gender=gender,
                location=user.get("location", ""),
            ).to_dict()
            ctx.write_record(record)
            ctx.log(
                f"  [OK] uid={uid}: {record['screen_name']} "
                f"({record['followers_count']} followers, total_cnt={total_cnt})"
            )
        except Exception as e:
            ctx.record_error(f"uid={uid}: {e}", response=resp)
            ctx.log(f"  [ERR] uid={uid}: {e}")

        ctx.set_progress(1.0)

    def scrape_comments(self, ctx: TaskContext, params: dict) -> None:
        """R3 action: scrape comments for a single weibo post."""
        self._bind_task(ctx)

        mid = params["mid"]
        assert isinstance(mid, str), (
            f"scrape_comments expects single mid (str), got {type(mid).__name__}"
        )
        max_count = int(params.get("max_comments_per_post", MAX_COMMENTS_PER_POST))

        cookie_path = self.resolve_cookie_path()
        ctx.log(f"cookie: path={cookie_path}, exists={cookie_path.exists()}")
        ctx.check_cancelled()
        ctx.log(f"Scraping post: {mid}")

        comments = self.fetch_comments(mid, max_count=max_count, verbose=False)
        ctx.log(f"  result: fetched={len(comments)}")

        # scraper output is already R7-shaped; just stamp synthetic key.
        written = 0
        for raw in comments:
            record = dict(raw)
            record["_source_post"] = mid
            ctx.write_record(record)
            written += 1

        ctx.log(f"  [OK] {mid}: written={written}")
        if written == 0:
            ctx.log("  [WARN] fetch returned ok but 0 comments", level="WARN")
        if written != len(comments):
            ctx.log(
                f"  [WARN] count mismatch: expected={len(comments)}, written={written}",
                level="WARN",
            )

        ctx.set_progress(1.0)

    def scrape_user_posts(self, ctx: TaskContext, params: dict) -> None:
        """R3 action: scrape posts from a single weibo user's timeline."""
        self._bind_task(ctx)

        uid = int(params["uid"])
        max_post_pages = int(params.get("max_post_pages", 3))

        ctx.check_cancelled()
        ctx.log(f"Scraping user: {uid}")

        posts = self.fetch_user_posts(uid, max_pages=max_post_pages, verbose=False)
        for raw in posts:
            ctx.write_record(raw)

        ctx.log(f"  [OK] uid={uid}: {len(posts)} posts")
        ctx.set_progress(1.0)

    def get_post_detail(self, ctx: TaskContext, params: dict) -> None:
        """R3 action: fetch a single weibo post's detail by mid or URL."""
        self._bind_task(ctx)

        raw_id = params["id"]
        # Support both pure mid and full URL (https://weibo.com/123/abc)
        mid = raw_id.strip()
        if "/" in mid:
            # Extract mid from URL like https://weibo.com/7780320531/R2nje2Hl7
            parts = mid.rstrip("/").split("/")
            mid = parts[-1]

        ctx.log(f"[weibo] Fetching post detail: id={mid}")

        resp = None
        try:
            url = f"https://weibo.com/ajax/statuses/show?id={mid}&locale=zh-CN&isGetLongText=true"
            resp = self.client.get_api(url, timeout=15)
            resp.raise_for_status()
            data = resp.json()

            if data.get("ok") == 0 and "message" in data:
                ctx.record_error(f"mid={mid}: API error: {data.get('message', data)}", response=resp)
                ctx.log(f"  [ERR] mid={mid}: API error")
                ctx.set_progress(1.0)
                return

            # API returns post fields at top level (ok=1 is absent for success)
            user = data.get("user", {})
            raw_text = strip_html(data.get("text_raw", data.get("text", "")))
            topics = re.findall(r'#([^#]+)#', raw_text)

            # Normalize pub_time (API returns "Fri Jun 05 20:30:07 +0800 2026")
            pub_time = parse_weibo_time(data.get("created_at", ""))

            # region_name: "发布于 河南" -> "河南"
            region_name = data.get("region_name", "")
            if region_name.startswith("发布于 "):
                region_name = region_name[4:]

            record = WeiboPostDetail(
                mid=str(data.get("mid", mid)),
                mblogid=data.get("mblogid", ""),
                user_name=user.get("screen_name", ""),
                user_uid=str(user.get("id", "")),
                verified="是" if user.get("verified") else "否",
                content=raw_text[:500],
                pub_time=pub_time,
                source=data.get("source", ""),
                region_name=region_name,
                repost_count=int(data.get("reposts_count", 0) or 0),
                comment_count=int(data.get("comments_count", 0) or 0),
                like_count=int(data.get("attitudes_count", 0) or 0),
                pic_num=int(data.get("pic_num", 0) or 0),
                is_long_text=bool(data.get("isLongText", False)),
                text_length=int(data.get("textLength", 0) or 0),
                topic_tags="|".join(topics) if topics else "",
                url=f"https://weibo.com/{user.get('id', '')}/{data.get('mblogid', '')}",
            ).to_dict()
            ctx.write_record(record)
            ctx.log(
                f"  [OK] mid={mid}: {record['user_name']} "
                f"repost={record['repost_count']} comment={record['comment_count']} like={record['like_count']}"
            )
        except Exception as e:
            ctx.record_error(f"mid={mid}: {e}", response=resp)
            ctx.log(f"  [ERR] mid={mid}: {e}")

        ctx.set_progress(1.0)
