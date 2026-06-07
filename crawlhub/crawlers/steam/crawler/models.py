"""
Steam crawler - Data Models

Canonical dataclasses for Steam crawler.
R7 contract: each record-shape dataclass field set MUST match the
corresponding plugin.yaml output_schema exactly.

All dataclasses inherit ``BaseRecord`` so ``to_dict()`` is uniform
(= ``dataclasses.asdict(self)``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

from crawlhub.core.platform.base_models import BaseRecord


# ═══════════════════════════════════════════════════════
#  Top Selling Items — get_weekly_topsellers.output_schema
# ═══════════════════════════════════════════════════════

@dataclass
class TopSellingItem(BaseRecord):
    """Sales chart game entry.

    Field set MUST match plugin.yaml -> actions.get_weekly_topsellers.output_schema:
      rank, app_id, game_name
    """
    rank: int = 0
    app_id: str = ""
    game_name: str = ""


@dataclass
class TopSellingResult(BaseRecord):
    """Sales chart scrape result (internal-only)."""
    region: str = ""
    date: str = ""
    games: List[TopSellingItem] = field(default_factory=list)
    status: str = "ok"
    error: str = ""
    elapsed_s: float = 0.0


# ═══════════════════════════════════════════════════════════════════
#  JSON-API MODELS — backed by Steam official JSON / Protobuf endpoints
#  Each dataclass field set MUST match the corresponding plugin.yaml
#  output_schema EXACTLY (R7 contract).
# ═══════════════════════════════════════════════════════════════════


# ── scrape_reviews (appreviews JSON API) ───────────────────────────

@dataclass
class ReviewItem(BaseRecord):
    """A single Steam review returned by the appreviews JSON endpoint.

    Field set MUST match plugin.yaml -> actions.scrape_reviews.output_schema.
    Author sub-object is flattened with author_* prefix.
    """
    app_id: str = ""
    recommendationid: str = ""
    # Author (flattened from response['author'])
    author_steamid: str = ""
    author_num_games_owned: int = 0
    author_num_reviews: int = 0
    author_playtime_forever: int = 0
    author_playtime_at_review: int = 0
    author_last_played: int = 0
    # Review body
    language: str = ""
    review: str = ""
    timestamp_created: int = 0
    timestamp_updated: int = 0
    voted_up: bool = False
    votes_up: int = 0
    votes_funny: int = 0
    weighted_vote_score: str = ""   # API returns as string ("0.523809...")
    comment_count: int = 0
    steam_purchase: bool = False
    received_for_free: bool = False
    written_during_early_access: bool = False
    primarily_steam_deck: bool = False


# ── get_game_detail (appdetails JSON API) ──────────────────────────

@dataclass
class GameDetail(BaseRecord):
    """Rich game profile from store.steampowered.com/api/appdetails.

    Field set MUST match plugin.yaml -> actions.get_game_detail.output_schema.
    """
    app_id: str = ""
    name: str = ""
    type: str = ""               # "game", "dlc", "demo", ...
    is_free: bool = False
    detailed_description: str = ""
    about_the_game: str = ""
    short_description: str = ""
    supported_languages: str = ""
    header_image: str = ""
    capsule_image: str = ""
    website: str = ""
    developers: list = field(default_factory=list)
    publishers: list = field(default_factory=list)
    price_overview: dict = field(default_factory=dict)   # currency / initial / final / discount_percent / final_formatted
    platforms: dict = field(default_factory=dict)        # {windows, mac, linux}
    categories: list = field(default_factory=list)       # [{id, description}, ...]
    genres: list = field(default_factory=list)           # [{id, description}, ...]
    screenshots: list = field(default_factory=list)
    movies: list = field(default_factory=list)
    recommendations_total: int = 0
    achievements_total: int = 0
    release_date: dict = field(default_factory=dict)     # {coming_soon, date}
    metacritic_score: int = 0
    metacritic_url: str = ""
    required_age: int = 0
    controller_support: str = ""
    crawl_time: str = ""


# ── get_current_players (ISteamUserStats/GetNumberOfCurrentPlayers) ─

@dataclass
class CurrentPlayers(BaseRecord):
    """Real-time concurrent-player count for a game.

    Field set MUST match plugin.yaml -> actions.get_current_players.output_schema.
    """
    app_id: str = ""
    player_count: int = 0
    result: int = 0       # 1 = success per Steam API convention
    crawl_time: str = ""


# ── get_achievement_percentages (one record per achievement) ───────

@dataclass
class AchievementPercentage(BaseRecord):
    """Global achievement-unlock percentage for one achievement of a game.

    One record per achievement. Sourced from the public stats HTML page
    (steamcommunity.com/stats/{app_id}/achievements), so only the
    fields that page exposes are available — internal API name ('name')
    is intentionally absent; use display_name + description + icon_url
    as the human-facing identity.

    Field set MUST match plugin.yaml -> actions.get_achievement_percentages.output_schema.
    """
    app_id: str = ""
    display_name: str = ""         # Localized achievement title (h3)
    description: str = ""          # Localized achievement description (h5)
    icon_url: str = ""             # Lit icon URL (only this state is exposed publicly)
    percent: float = 0.0           # 0.0 ~ 100.0
    crawl_time: str = ""


# ── get_user_tags (one record per tag) ─────────────────────────────

@dataclass
class UserTag(BaseRecord):
    """A single Steam community user-tag attached to a game.

    Sourced from the store-page HTML (.glance_tags.popular_tags).
    One record per tag; ordering follows page order, which roughly
    reflects popularity. Localization controlled by `language`.

    Field set MUST match plugin.yaml -> actions.get_user_tags.output_schema.
    """
    app_id: str = ""
    tag: str = ""                  # Localized tag text
    rank: int = 0                  # 1-based position on the page
    crawl_time: str = ""


# ── search_games (storesearch JSON, rich fields) ───────────────────

@dataclass
class SearchGameResult(BaseRecord):
    """Storesearch result row.

    Field set MUST match plugin.yaml -> actions.search_games.output_schema.
    """
    app_id: str = ""
    name: str = ""
    type: str = ""                 # "app" / "bundle" / "sub"
    tiny_image: str = ""
    small_capsule: str = ""
    price: dict = field(default_factory=dict)
    platforms: dict = field(default_factory=dict)
    streamingvideo: bool = False
    controller_support: str = ""
    metascore: str = ""
    store_url: str = ""


# ── get_news (ISteamNews/GetNewsForApp) ────────────────────────────

@dataclass
class NewsItem(BaseRecord):
    """One news/announcement entry for a game.

    Field set MUST match plugin.yaml -> actions.get_news.output_schema.
    """
    app_id: str = ""
    gid: str = ""
    title: str = ""
    url: str = ""
    is_external_url: bool = False
    author: str = ""
    contents: str = ""             # full BBCode/HTML, not truncated
    feedlabel: str = ""
    feedname: str = ""
    feed_type: int = 0
    date: int = 0                  # unix timestamp
    tags: list = field(default_factory=list)


# ── get_review_histogram (appreviewhistogram JSON) ─────────────────

@dataclass
class ReviewHistogramBucket(BaseRecord):
    """One bucket of the Steam review-count time histogram.

    Source: ``GET store.steampowered.com/appreviewhistogram/{appid}``.

    The endpoint returns two parallel time-series concatenated into one
    chart on the store page:

      * ``rollups``  — full history at coarse granularity (``rollup_type``
        is decided by Steam server-side: ``month`` for old/popular games,
        ``week`` for newer/smaller ones). The client cannot override.
      * ``recent``   — last 30 days at ``day`` granularity, ALWAYS.

    Both series are flattened into the same shape; ``bucket_type``
    distinguishes them so downstream charting can render them as one
    timeline (rollups on the left, recent on the right).

    ``is_review_bombing`` flags buckets whose ``date_unix`` falls inside
    one of the response's ``past_events`` ranges (review-bombing windows
    Steam paints in grey on the chart).

    Field set MUST match plugin.yaml -> actions.get_review_histogram.output_schema.
    """
    app_id: str = ""
    bucket_type: str = ""             # "rollup" | "recent"
    rollup_type: str = ""             # "month" | "week" | "day" (mirrored from response;
                                       # "day" for recent buckets)
    date_unix: int = 0                # Unix seconds (UTC); start of the bucket
    date_iso: str = ""                # YYYY-MM-DD (UTC)
    recommendations_up: int = 0
    recommendations_down: int = 0
    total_reviews: int = 0            # = up + down
    positive_pct: float = 0.0         # 0.0 ~ 100.0
    is_review_bombing: bool = False   # bucket sits inside a past_events range
    filter_offtopic_activity: int = 1 # echoes the request param (1 = off-topic filtered)
    crawl_time: str = ""


# ── get_review_language_breakdown (viewlanguagereviewscores HTML) ──

@dataclass
class ReviewLanguageScore(BaseRecord):
    """One row of the Steam review language-breakdown panel.

    Source: ``GET store.steampowered.com/viewlanguagereviewscores/{appid}``
    (returned as an HTML fragment; we always request ``l=english`` so the
    English regex tooltip parser is reliable — none of the request params
    affect the underlying numbers, only the wording).

    Three buckets are emitted:

      * ``your_language``  — the single language matched by the UI lang
        request (one row per game).
      * ``other``          — all languages with enough reviews to score
        independently (zero or many rows).
      * ``all_languages``  — the all-languages aggregate (one row per
        game). Only this row carries ``total_languages_count``.

    Field set MUST match plugin.yaml -> actions.get_review_language_breakdown.output_schema.
    """
    app_id: str = ""
    bucket: str = ""                  # "your_language" | "other" | "all_languages"
    language: str = ""                # span text; "" for the aggregate row
    rating_text: str = ""             # "Very Positive" / "Mostly Positive" / ...; may be ""
    positive_pct: int = 0             # 0~100, integer (Steam rounds in tooltip)
    total_reviews: int = 0
    has_review_bombing_filter: bool = False  # tooltip mentions off-topic activity
    total_languages_count: int = 0    # only set on the all_languages row
    crawl_time: str = ""
