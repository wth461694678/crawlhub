"""
Weibo data models
==================
Dataclasses for Weibo records (R7 contract).

Each record-shape dataclass:
  * field set MUST equal plugin.yaml output_schema for the action
    (minus the synthetic ``_source_*`` keys injected by bridge.py)
  * inherits ``BaseRecord`` to get ``to_dict()`` (= ``asdict(self)``).

Note: previously these classes also exposed ``from_record(dict)`` so that
bridge.py could ingest scraper output via a dataclass round-trip. That
became an anti-pattern — it silently dropped unknown keys, hiding scraper
bugs. Now the scraper is the single source of truth: it MUST return dicts
whose key set equals the dataclass field set, period. Bridge writes those
dicts straight through (plus synthetic ``_source_*`` injection).
"""

from __future__ import annotations

from dataclasses import dataclass

from crawlhub.core.platform.base_models import BaseRecord


@dataclass
class WeiboPost(BaseRecord):
    """Search / user-posts record shape.

    Field set MUST match plugin.yaml -> actions.search_posts.output_schema
    AND actions.scrape_user_posts.output_schema.
    """
    source_type: str = ""    # search dimension or "user_posts"
    mid: str = ""
    user_name: str = ""
    user_uid: str = ""
    verified: str = "否"      # "是" / "否"
    content: str = ""
    pub_time: str = ""
    source: str = ""
    repost_count: int = 0
    comment_count: int = 0
    like_count: int = 0
    topic_tags: str = ""     # "|" joined
    url: str = ""


@dataclass
class WeiboPostDetail(BaseRecord):
    """get_post_detail record shape.

    Field set MUST match plugin.yaml -> actions.get_post_detail.output_schema.
    Richer than WeiboPost: includes region, pic count, is_long_text, etc.
    """
    mid: str = ""
    mblogid: str = ""
    user_name: str = ""
    user_uid: str = ""
    verified: str = "否"
    content: str = ""
    pub_time: str = ""
    source: str = ""
    region_name: str = ""
    repost_count: int = 0
    comment_count: int = 0
    like_count: int = 0
    pic_num: int = 0
    is_long_text: bool = False
    text_length: int = 0
    topic_tags: str = ""
    url: str = ""


@dataclass
class WeiboComment(BaseRecord):
    """scrape_comments record shape (sans synthetic ``_source_post``)."""
    source_mid: str = ""
    comment_id: str = ""
    user_name: str = ""
    user_uid: str = ""
    content: str = ""
    pub_time: str = ""
    source: str = ""
    like_count: int = 0
    floor: int = 0


@dataclass
class WeiboUserBrief(BaseRecord):
    """get_user_info record shape.

    Field set MUST match plugin.yaml -> actions.get_user_info.output_schema.
    """
    uid: int = 0
    screen_name: str = ""
    description: str = ""
    verified: bool = False
    verified_reason: str = ""
    comment_cnt: int = 0
    repost_cnt: int = 0
    like_cnt: int = 0
    total_cnt: int = 0
    followers_count: int = 0
    friends_count: int = 0
    statuses_count: int = 0
    gender: str = ""
    location: str = ""


# ---------------------------------------------------------------------------
# Internal-only models — NOT bound to yaml, kept for legacy in-memory use
# ---------------------------------------------------------------------------

@dataclass
class WeiboUser(BaseRecord):
    """Full Weibo user profile (internal-only, NOT a record shape)."""
    uid: str = ""
    user_name: str = ""
    followers_count: int = 0
    followers_str: str = ""
    friends_count: int = 0
    statuses_count: int = 0
    verified: str = "否"
    verified_type_code: int = -1
    verified_type: str = "未认证"
    verified_reason: str = ""
    description: str = ""
    location: str = ""
    gender: str = "未知"
    avatar: str = ""
    profile_url: str = ""
    svip: int = 0
    total_counter: str = ""
    ip_location: str = ""
    created_at: str = ""
    sunshine_credit: str = ""
    birthday: str = ""
    labels: str = ""
    user_type: str = ""
    official_score: int = 0


@dataclass
class WeiboTopic(BaseRecord):
    """Internal-only."""
    topic_name: str = ""
    description: str = ""
    discuss_count: str = ""
    read_count: str = ""
    stats_raw: str = ""
    url: str = ""


@dataclass
class HotSearchEntry(BaseRecord):
    """Internal-only."""
    rank: str = ""
    word: str = ""
    heat: str = ""
    icon_desc: str = ""
