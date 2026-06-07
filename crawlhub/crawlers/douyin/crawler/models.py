"""
Douyin Crawler - Data Models
================================

Data classes for Douyin video, comment, and search results.

These are pure data containers used by Scraper and Service layers.

All record-shape dataclasses inherit ``BaseRecord`` so ``to_dict()``
is uniform (= ``dataclasses.asdict(self)``). The internal aggregate
``VideoResult`` overrides ``to_dict()`` because it needs custom
None-handling and nested-dataclass dispatch.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from crawlhub.core.platform.base_models import BaseRecord


@dataclass
class VideoInfo(BaseRecord):
    """Basic video information.

    to_dict() (inherited) keys match plugin.yaml get_video_detail.output_schema:
      aweme_id, title, author_name, author_uid, author_sec_uid,
      digg_count, comment_count, share_count, collect_count,
      play_count, duration, create_time, cover_url, fetched_at
    """
    aweme_id: str = ""
    title: str = ""
    author_name: str = ""        # maps from author
    author_uid: str = ""
    author_sec_uid: str = ""
    digg_count: int = 0          # maps from like_count
    comment_count: int = 0
    share_count: int = 0
    collect_count: int = 0
    play_count: int = 0
    duration: int = 0
    create_time: int = 0
    cover_url: str = ""
    fetched_at: str = ""


@dataclass
class Comment(BaseRecord):
    """Single comment.

    to_dict() (inherited) keys match plugin.yaml scrape_comments.output_schema:
      cid, text, aweme_id, create_time, digg_count, reply_comment_total,
      user_id, user_nickname, user_sec_uid, is_author_digged,
      reply_to_reply_id, reply_to_user_id, reply_to_user_nickname,
      is_sub_comment, parent_comment_id
    (synthetic ``_source_video`` is injected by bridge.py)
    """
    cid: str = ""
    text: str = ""
    aweme_id: str = ""
    create_time: int = 0
    digg_count: int = 0
    reply_comment_total: int = 0
    user_id: str = ""
    user_nickname: str = ""     # maps from user_name
    user_sec_uid: str = ""
    is_author_digged: bool = False
    reply_to_reply_id: str = ""
    reply_to_user_id: str = ""
    reply_to_user_nickname: str = ""
    is_sub_comment: bool = False
    parent_comment_id: str = ""


@dataclass
class SearchResult(BaseRecord):
    """Single search result entry.

    to_dict() (inherited) keys match plugin.yaml search_videos.output_schema:
      aweme_id, title, like_count, comment_count, share_count,
      author_uid, author_name, keyword
    """
    aweme_id: str = ""
    title: str = ""
    like_count: int = 0         # maps from statistics.digg_count
    comment_count: int = 0
    share_count: int = 0
    author_uid: str = ""
    author_name: str = ""
    keyword: str = ""


@dataclass
class LiveRoom(BaseRecord):
    """Single live room result (used internally, not by plugin actions)."""
    room_id: str = ""
    user_uid: str = ""
    name: str = ""
    title: str = ""
    online_count: int = 0
    cover_url: str = ""
    keyword: str = ""


@dataclass
class LiveRoomSearchResult(BaseRecord):
    """Record shape for search_live_rooms."""
    web_rid: str = ""
    room_id: str = ""
    title: str = ""
    user_count: int = 0
    status: int = 0
    author_nickname: str = ""
    author_uid: str = ""
    author_sec_uid: str = ""
    cover_url: str = ""
    stream_flv: str = ""
    stream_hls: str = ""
    keyword: str = ""


@dataclass
class LiveRoomInfoRecord(BaseRecord):
    """Record shape for get_live_room_info."""
    web_rid: str = ""
    room_id: str = ""
    status: int = 0
    status_str: str = ""
    title: str = ""
    user_count: int = 0
    user_count_str: str = ""
    like_count: int = 0
    owner_uid: str = ""
    owner_sec_uid: str = ""
    owner_nickname: str = ""
    cover_url: str = ""
    stream_flv_origin: str = ""
    stream_hls_origin: str = ""
    fetched_at: str = ""


@dataclass
class LiveEventRecord(BaseRecord):
    """Record shape for collect_live_events."""
    web_rid: str = ""
    event_type: str = ""
    raw_cmd: str = ""
    uid: str = ""
    nickname: str = ""
    content: str = ""
    online_count: int = 0
    like_count: int = 0
    gift_id: str = ""
    gift_count: int = 0
    ts: float = 0.0
    payload: str = ""


@dataclass
class VideoResult(BaseRecord):
    """Complete scraping result for one video.

    Overrides ``to_dict`` because:
      * absent ``video_info`` must serialize as ``{}`` (not ``None``)
      * ``comments`` may contain dicts OR Comment objects (defensive
        handling — scraper sometimes appends raw dicts)
    """
    aweme_id: str = ""
    input_raw: str = ""
    status: str = "pending"   # pending / ok / error
    error: str = ""
    video_info: Optional[VideoInfo] = None
    comments: list = field(default_factory=list)
    total_fetched: int = 0
    total_pages: int = 0
    elapsed_s: float = 0.0

    def to_dict(self) -> dict:
        """Return dict representation for serialization."""
        return {
            "aweme_id": self.aweme_id,
            "input_raw": self.input_raw,
            "status": self.status,
            "error": self.error,
            "video_info": self.video_info.to_dict() if self.video_info else {},
            "comments": [c.to_dict() if hasattr(c, "to_dict") else c for c in self.comments],
            "total_fetched": self.total_fetched,
            "total_pages": self.total_pages,
            "elapsed_s": self.elapsed_s,
        }
