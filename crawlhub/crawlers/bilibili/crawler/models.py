"""
Bilibili data models
====================
Dataclasses for Bilibili video info, comments, search results, live rooms.

R7 contract: every dataclass that backs a plugin.yaml action MUST have a
field set strictly equal to that action's output_schema.

  * VideoInfo  -- internal scraper bundle (NOT a record shape)
  * VideoDetail -- get_video_detail.output_schema
  * SearchResult -- search_videos.output_schema
  * Comment    -- scrape_comments.output_schema (minus synthetic _source_video)

All dataclasses inherit ``BaseRecord`` to get a uniform ``to_dict()``
(= ``dataclasses.asdict(self)``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from crawlhub.core.platform.base_models import BaseRecord


@dataclass
class VideoInfo(BaseRecord):
    """Internal scraper bundle, NOT a record shape.

    Used by BilibiliScraper to track per-video progress in memory.
    Do NOT bind to plugin.yaml — fields here are scraper-internal.
    """
    bvid: str = ""
    aid: int = 0
    title: str = ""
    desc: str = ""
    author_name: str = ""
    author_uid: int = 0
    duration: int = 0
    pubdate: str = ""
    view_count: int = 0
    like_count: int = 0
    coin_count: int = 0
    favorite_count: int = 0
    share_count: int = 0
    danmaku_count: int = 0
    reply_count: int = 0
    tname: str = ""
    pages: list = None  # type: ignore

    def __post_init__(self):
        if self.pages is None:
            self.pages = []


@dataclass
class VideoDetail(BaseRecord):
    """Record shape for get_video_detail.

    Field set MUST match plugin.yaml -> actions.get_video_detail.output_schema:
      bvid, aid, title, description, duration, owner_name, owner_mid,
      view_count, like_count, coin_count, share_count, reply_count,
      danmaku_count, favorite_count, pub_date, tname, pic
    """
    bvid: str = ""
    aid: str = ""
    title: str = ""
    description: str = ""
    duration: int = 0
    owner_name: str = ""
    owner_mid: str = ""
    view_count: int = 0
    like_count: int = 0
    coin_count: int = 0
    share_count: int = 0
    reply_count: int = 0
    danmaku_count: int = 0
    favorite_count: int = 0
    pub_date: int = 0
    tname: str = ""
    pic: str = ""

    @classmethod
    def from_api(cls, video_data: dict[str, Any]) -> "VideoDetail":
        """Map Bilibili web-interface/view payload -> VideoDetail."""
        owner = video_data.get("owner", {}) or {}
        stat = video_data.get("stat", {}) or {}
        return cls(
            bvid=str(video_data.get("bvid", "")),
            aid=str(video_data.get("aid", "")),
            title=str(video_data.get("title", "")),
            description=str(video_data.get("desc", "")),
            duration=int(video_data.get("duration", 0) or 0),
            owner_name=str(owner.get("name", "")),
            owner_mid=str(owner.get("mid", "")),
            view_count=int(stat.get("view", 0) or 0),
            like_count=int(stat.get("like", 0) or 0),
            coin_count=int(stat.get("coin", 0) or 0),
            share_count=int(stat.get("share", 0) or 0),
            reply_count=int(stat.get("reply", 0) or 0),
            danmaku_count=int(stat.get("danmaku", 0) or 0),
            favorite_count=int(stat.get("favorite", 0) or 0),
            pub_date=int(video_data.get("pubdate", 0) or 0),
            tname=str(video_data.get("tname", "")),
            pic=str(video_data.get("pic", "")),
        )


@dataclass
class Comment(BaseRecord):
    """Single comment — matches scrape_comments.output_schema (minus
    the synthetic ``_source_video`` key, injected by bridge.py)."""
    rpid: int = 0
    uid: int = 0
    uname: str = ""
    content: str = ""
    like_count: int = 0
    reply_count: int = 0
    ctime: str = ""
    floor: int = 0
    ip_location: str = ""
    is_sub_comment: bool = False
    parent_rpid: int = 0
    bvid: str = ""


@dataclass
class SearchResult(BaseRecord):
    """Single search result — matches search_videos.output_schema."""
    bvid: str = ""
    aid: int = 0
    title: str = ""
    author: str = ""
    author_uid: int = 0
    play_count: int = 0
    danmaku_count: int = 0
    reply_count: int = 0
    pubdate: str = ""
    duration: str = ""
    description: str = ""
    cover: str = ""  # Video cover image URL (absolute https URL)


@dataclass
class LiveRoomSearchResult(BaseRecord):
    """Record shape for search_live_rooms."""
    room_id: int = 0
    uid: int = 0
    uname: str = ""
    title: str = ""
    online: int = 0
    live_status: int = 0
    is_live: bool = False
    area_name: str = ""
    cover: str = ""
    user_cover: str = ""
    live_time: str = ""
    tags: str = ""
    keyword: str = ""


@dataclass
class LiveRoomInfoRecord(BaseRecord):
    """Record shape for get_live_room_info."""
    room_id: int = 0
    short_id: int = 0
    uid: int = 0
    title: str = ""
    live_status: int = 0
    is_live: bool = False
    live_time: str = ""
    online: int = 0
    area_id: int = 0
    area_name: str = ""
    parent_area_id: int = 0
    parent_area_name: str = ""
    uname: str = ""
    face: str = ""
    follower_num: int = 0
    cover: str = ""
    keyframe: str = ""
    fetched_at: str = ""


@dataclass
class LiveEventRecord(BaseRecord):
    """Record shape for collect_live_events."""
    room_id: int = 0
    event_type: str = ""
    raw_cmd: str = ""
    uid: str = ""
    nickname: str = ""
    content: str = ""
    popularity: int = 0
    online_count: int = 0
    like_count: int = 0
    gift_id: str = ""
    gift_name: str = ""
    gift_count: int = 0
    ts: float = 0.0
    payload: str = ""


@dataclass
class VideoAISummary(BaseRecord):
    """Record shape for ``get_video_ai_summary``.

    ┌──────────────────────────────────────────────────────────────────────┐
    │ B 站官方"视频 AI 总结"接口 (view/conclusion/get) 返回的精简表头。     │
    │ 字段集必须与 plugin.yaml -> actions.get_video_ai_summary.output_schema│
    │ 严格相等 (R7 / C3)。                                                 │
    └──────────────────────────────────────────────────────────────────────┘
    """
    bvid: str = ""           # BV 号 (调用方传入或解析得到的标识)
    cid: int = 0             # 分 P 的 cid (签名素材，原样回写便于复核)
    up_mid: int = 0          # UP 主 mid (签名素材)
    has_summary: bool = False  # 是否成功拿到 summary (False 时 summary 为空串)
    summary: str = ""        # 已规整过的总结文本 (去换行、英文逗号 -> 中文逗号)


@dataclass
class LiveRoom(BaseRecord):
    """Single live room result. Internal-only."""
    room_id: int = 0
    uid: int = 0
    uname: str = ""
    title: str = ""
    online: int = 0
    live_status: int = 0
    is_live: bool = False
    area_name: str = ""
    live_time: str = ""
    cover: str = ""
    user_cover: str = ""
    tags: str = ""


@dataclass
class VideoResult(BaseRecord):
    """Full scraping result for one video. Internal-only."""
    bvid: str = ""
    input_raw: str = ""
    status: str = "pending"  # pending / ok / error
    error: str = ""
    video_info: Optional[VideoInfo] = None
    comments: list = None  # type: ignore
    total_fetched: int = 0
    total_pages: int = 0
    elapsed_s: float = 0.0

    def __post_init__(self):
        if self.comments is None:
            self.comments = []
