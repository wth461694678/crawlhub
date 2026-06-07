"""
Kuaishou crawler data models.

Layer-2 (data contract) responsibility:
  * Each dataclass field name MUST equal the corresponding yaml
    output_schema key. R7 startup check enforces strict equality.
  * to_dict() (inherited from BaseRecord) emits the dict shape
    declared in plugin.yaml.

Models
------
- VideoInfo:    plugin.yaml -> actions.get_video_detail.output_schema
- Comment:      plugin.yaml -> actions.scrape_comments.output_schema
                (synthetic ``_source_video`` is injected by bridge.py,
                not by the dataclass.)
- SearchResult: plugin.yaml -> actions.search_videos.output_schema
- VideoResult:  internal scrape bundle (NOT a record shape)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from crawlhub.core.platform.base_models import BaseRecord


@dataclass
class VideoInfo(BaseRecord):
    """Video detail info — matches get_video_detail.output_schema."""

    photo_id: str = ""
    name: str = ""           # video title/caption (was "caption" pre-R7)
    duration: int = 0
    like_count: int = 0
    view_count: int = 0
    comment_count: int = 0
    author_id: str = ""
    author_name: str = ""
    tags: list = None  # type: ignore
    fetched_at: str = ""

    def __post_init__(self):
        if self.tags is None:
            self.tags = []


@dataclass
class Comment(BaseRecord):
    """Single comment — matches scrape_comments.output_schema (minus
    the synthetic ``_source_video`` key, injected by bridge.py)."""

    comment_id: str = ""
    author_id: str = ""
    author_name: str = ""
    content: str = ""
    timestamp: int = 0
    like_count: int = 0
    has_sub_comments: bool = False
    photo_id: str = ""
    reply_to_user_name: str = ""
    reply_to: str = ""
    is_sub_comment: bool = False
    parent_comment_id: str = ""


@dataclass
class SearchResult(BaseRecord):
    """Search result item — matches search_videos.output_schema."""

    photo_id: str = ""
    name: str = ""           # video caption/title (was "caption" pre-R7)
    like_count: int = 0
    view_count: int = 0
    author_id: str = ""
    author_name: str = ""
    keyword: str = ""


@dataclass
class VideoResult(BaseRecord):
    """Full scrape result for a single video.

    Internal-only — not a record shape, never persisted as a row.
    """

    photo_id: str = ""
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


# ════════════════════════════════════════════════════════════
#  Live room records (Hybrid: browser bootstrap → Python WS)
# ════════════════════════════════════════════════════════════


@dataclass
class LiveCategoryItem(BaseRecord):
    """One category from /live_api/category/data — list_live_categories output."""

    category_id: str = ""
    category_name: str = ""
    icon_url: str = ""
    category_type: int = 0
    top_rooms_count: int = 0


@dataclass
class LiveCategorySearchResult(BaseRecord):
    """One match from /live_api/category/search — search_live_categories output."""

    category_id: str = ""
    category_name: str = ""
    icon_url: str = ""
    category_type: int = 0
    keyword: str = ""


@dataclass
class LiveRoomSearchResult(BaseRecord):
    """One live room from /live_api/gameboard/list — list_category_live_rooms output."""

    live_stream_id: str = ""
    principal_id: str = ""
    author_name: str = ""
    author_avatar: str = ""
    title: str = ""
    cover_url: str = ""
    watching_count: int = 0
    like_count: int = 0
    category_id: str = ""
    category_name: str = ""
    stream_flv: str = ""
    start_time: int = 0


@dataclass
class LiveRoomInfoRecord(BaseRecord):
    """First-screen live room snapshot — matches get_live_room_info.output_schema."""

    principal_id: str = ""
    live_stream_id: str = ""
    title: str = ""
    author_id: str = ""
    author_name: str = ""
    is_live: bool = False
    fetched_at: str = ""
    source_url: str = ""


@dataclass
class LiveEventRecord(BaseRecord):
    """One emitted live event — matches collect_live_events.output_schema."""

    principal_id: str = ""
    live_stream_id: str = ""
    event_type: str = ""    # chat / gift / like / system_notice / room_stats / live_end / error / raw
    raw_cmd: str = ""       # 原生 protocol 名（SC_FEED_PUSH_COMMENT / SC_LIVE_WATCHING_LIST / SC_LIVE_END / ...）
    uid: str = ""
    nickname: str = ""
    content: str = ""
    online_count: int = 0
    online_count_str: str = ""
    like_count_str: str = ""
    gift_id: str = ""
    gift_count: int = 0
    error_code: int = 0
    ts: float = 0.0
    payload: str = ""
