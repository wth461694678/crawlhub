"""
Steam crawler - Scraper layer.

SteamScraper owns the entire business loop:
  * read params with `params.get("name", default)` or `params["name"]`
  * call client for raw data
  * shape into `models.*` dataclasses
  * write records via `ctx.write_record(model.to_dict())`
  * advance progress via `ctx.set_progress(...)`

R3 contract: every public method has signature `(self, ctx, params) -> None`.
No method may be a generator (no `yield`); see C12-C15 in
`tests/test_platform_conformance.py`.

2026-05-25: v1 HTML search/reviews removed. ``search_games`` and
``scrape_reviews`` now refer to the JSON-API implementations (formerly
``search_games_v2`` / ``scrape_reviews_v2``).

2026-05-25: ``get_game_info`` removed; rich JSON ``get_game_detail`` is
the canonical product-info action. Standalone ``get_user_tags`` covers
the only thing ``get_game_detail`` can't deliver (community-popular tags).
"""

from __future__ import annotations

import logging

from crawlhub.core.task_context import TaskContext

from .client import SteamClient
from .models import (
    AchievementPercentage,
    CurrentPlayers,
    GameDetail,
    NewsItem,
    ReviewHistogramBucket,
    ReviewItem,
    ReviewLanguageScore,
    SearchGameResult,
    TopSellingResult,
    UserTag,
)

logger = logging.getLogger(__name__)


class SteamScraper:
    """High-level Steam scraper — all public methods are R3 actions.

    Every public method:
      * signature: ``(self, ctx: TaskContext, params: dict) -> None``
      * uses ``params.get(...)`` to read business fields
      * calls ``ctx.write_record``, ``ctx.set_progress``, ``ctx.check_cancelled``
      * NEVER yields (must return None)
    """

    def __init__(self, client: SteamClient | None = None):
        self.client = client or SteamClient()

    # ──────────────────────────────────────────────────────────
    #  Action method signature contract (applies to ALL actions)
    # ──────────────────────────────────────────────────────────
    # All action methods take the SAME shape:
    #     def <action>(self, ctx: TaskContext, params: dict) -> None
    #
    # Inside the method, read business fields with `params.get("name", default)`
    # or `params["name"]` (for required keys).
    #
    # DO NOT use `**params` to unpack into keyword arguments. The platform
    # injects control flags (e.g. `treat_empty_as_success`) into params that
    # are NOT part of any action's business signature, and `**params` would
    # crash with `TypeError: got an unexpected keyword argument ...`.
    # ──────────────────────────────────────────────────────────

    # ══════════════════════════════════════════════════════════
    #  HTML actions (store-page scraping)
    # ══════════════════════════════════════════════════════════

    def get_user_tags(self, ctx: TaskContext, params: dict) -> None:
        """Action: get_user_tags — community user tags from store-page HTML.

        One record per tag. Localization is controlled by ``language``.
        """
        app_id = str(params["app_id"])
        language = params.get("language", "schinese")

        ctx.log(f"get_user_tags app_id={app_id} lang={language}")
        ctx.check_cancelled()
        items: list[UserTag] = self.client.get_user_tags(app_id, language=language)
        total = len(items)
        for i, item in enumerate(items, 1):
            ctx.check_cancelled()
            ctx.write_record(item.to_dict())
            if total:
                ctx.set_progress(min(1.0, i / total))
        ctx.set_progress(1.0)
        ctx.log(f"get_user_tags done: app_id={app_id} tags={total}")

    def get_weekly_topsellers(self, ctx: TaskContext, params: dict) -> None:
        """Action: get_weekly_topsellers — Protobuf topsellers API with HTML fallback."""
        date_str = params.get("date") or None
        # Accept both 'country' (preferred) and 'region' (legacy) param names.
        country = params.get("country") or params.get("region") or "US"
        language = params.get("language", "schinese")
        count = params.get("count", 20)

        ctx.log(
            f"Fetching Steam weekly top sellers: date={date_str or 'current'} "
            f"country={country} count={count}"
        )
        ctx.check_cancelled()

        # Preferred: Protobuf API (via client facade — scraper never imports _internal)
        try:
            games = self.client.get_topsellers_protobuf(
                country=country, language=language, date_str=date_str, count=count,
            )
            written = 0
            for d in games:
                ctx.check_cancelled()
                d["app_id"] = str(d.get("app_id", ""))
                ctx.write_record(d)
                written += 1
            ctx.set_progress(1.0)
            ctx.log(f"[OK] topsellers via protobuf: {written} games")
            return
        except Exception as e:
            ctx.log(
                f"[WARN] Protobuf topsellers API failed ({e}); falling back to HTML scrape",
                level="WARN",
            )

        # Fallback: HTML scrape via SteamClient
        result: TopSellingResult = self.client.get_top_selling_chart(region=country, date=date_str)
        if result.status != "ok" or not result.games:
            ctx.log("[WARN] No results from top sellers HTML scrape", level="WARN")
            ctx.set_progress(1.0)
            return
        written = 0
        for game in result.games[:count]:
            ctx.check_cancelled()
            ctx.write_record(game.to_dict())
            written += 1
        ctx.set_progress(1.0)
        ctx.log(f"[OK] topsellers via HTML fallback: {written} games")

    # ══════════════════════════════════════════════════════════
    #  JSON-API actions (Steam official endpoints)
    # ══════════════════════════════════════════════════════════

    def search_games(self, ctx: TaskContext, params: dict) -> None:
        """Action: search_games — storesearch JSON (rich fields)."""
        keyword = params["keyword"]
        max_results = params.get("max_results", 10)
        cc = params.get("cc", "US")
        language = params.get("language", "schinese")

        ctx.log(f"search_games keyword={keyword!r} max={max_results} cc={cc} l={language}")
        ctx.check_cancelled()
        items: list[SearchGameResult] = self.client.search_games(
            keyword=keyword, max_results=max_results, cc=cc, language=language,
        )
        for item in items:
            ctx.check_cancelled()
            ctx.write_record(item.to_dict())
        ctx.set_progress(1.0)
        ctx.log(f"search_games done: {len(items)} results")

    def scrape_reviews(self, ctx: TaskContext, params: dict) -> None:
        """Action: scrape_reviews — appreviews JSON API."""
        app_id = str(params["app_id"])
        max_reviews = params.get("max_reviews", 100)
        language = params.get("language", "all")
        review_type = params.get("review_type", "all")
        purchase_type = params.get("purchase_type", "all")
        day_range = params.get("day_range", 0)
        filter_ = params.get("filter", "recent")

        ctx.log(f"scrape_reviews app_id={app_id} max={max_reviews} "
                f"lang={language} type={review_type} filter={filter_}")
        ctx.check_cancelled()

        items: list[ReviewItem] = self.client.fetch_reviews(
            app_id=app_id,
            max_reviews=max_reviews,
            language=language,
            review_type=review_type,
            purchase_type=purchase_type,
            day_range=day_range,
            filter_=filter_,
        )

        total = len(items)
        for i, item in enumerate(items, 1):
            ctx.check_cancelled()
            ctx.write_record(item.to_dict())
            if total:
                ctx.set_progress(min(1.0, i / total))
        ctx.set_progress(1.0)
        ctx.log(f"scrape_reviews done: app_id={app_id} written={total}")

    def get_game_detail(self, ctx: TaskContext, params: dict) -> None:
        """Action: get_game_detail — appdetails JSON API."""
        app_id = str(params["app_id"])
        cc = params.get("cc", "us")
        language = params.get("language", "schinese")

        ctx.log(f"get_game_detail app_id={app_id} cc={cc} l={language}")
        ctx.check_cancelled()
        detail: GameDetail | None = self.client.fetch_game_detail(app_id, cc=cc, language=language)
        if detail is None:
            ctx.log(f"[WARN] appdetails returned no data for app_id={app_id}", level="WARN")
            ctx.set_progress(1.0)
            return
        ctx.write_record(detail.to_dict())
        ctx.set_progress(1.0)
        ctx.log(f"get_game_detail done: {detail.name or app_id}")

    def get_current_players(self, ctx: TaskContext, params: dict) -> None:
        """Action: get_current_players — ISteamUserStats CCU."""
        app_id = str(params["app_id"])

        ctx.log(f"get_current_players app_id={app_id}")
        ctx.check_cancelled()
        record: CurrentPlayers = self.client.fetch_current_players(app_id)
        ctx.write_record(record.to_dict())
        ctx.set_progress(1.0)
        ctx.log(f"get_current_players done: app_id={app_id} ccu={record.player_count}")

    def get_achievement_percentages(self, ctx: TaskContext, params: dict) -> None:
        """Action: get_achievement_percentages — one record per achievement.

        Sourced from the public stats HTML page, so each row carries
        localized display name + description + lit icon URL + percent.
        """
        app_id = str(params["app_id"])
        language = params.get("language", "schinese")

        ctx.log(f"get_achievement_percentages app_id={app_id} lang={language}")
        ctx.check_cancelled()
        items: list[AchievementPercentage] = self.client.fetch_achievement_percentages(
            app_id, language=language,
        )
        total = len(items)
        for i, item in enumerate(items, 1):
            ctx.check_cancelled()
            ctx.write_record(item.to_dict())
            if total:
                ctx.set_progress(min(1.0, i / total))
        ctx.set_progress(1.0)
        ctx.log(f"get_achievement_percentages done: app_id={app_id} rows={total}")

    def get_news(self, ctx: TaskContext, params: dict) -> None:
        """Action: get_news — ISteamNews/GetNewsForApp. contents NOT truncated."""
        app_id = str(params["app_id"])
        count = params.get("count", 20)

        ctx.log(f"get_news app_id={app_id} count={count}")
        ctx.check_cancelled()
        items: list[NewsItem] = self.client.fetch_news(
            app_id=app_id, count=count,
        )
        for item in items:
            ctx.check_cancelled()
            ctx.write_record(item.to_dict())
        ctx.set_progress(1.0)
        ctx.log(f"get_news done: app_id={app_id} items={len(items)}")

    def get_review_histogram(self, ctx: TaskContext, params: dict) -> None:
        """Action: get_review_histogram — review-count timeline buckets.

        Flattens ``rollups`` (full-history coarse) and ``recent`` (last 30
        days, daily) into a single record stream. Each row is one bucket;
        rows that fall inside a Steam-flagged review-bombing window have
        ``is_review_bombing=True``.

        Only ``filter_offtopic_activity`` (0/1) actually moves the
        numbers — other params on the underlying endpoint are cosmetic
        and intentionally NOT exposed here.
        """
        app_id = str(params["app_id"])
        filter_offtopic_activity = int(params.get("filter_offtopic_activity", 1))

        ctx.log(
            f"get_review_histogram app_id={app_id} "
            f"filter_offtopic_activity={filter_offtopic_activity}"
        )
        ctx.check_cancelled()
        items: list[ReviewHistogramBucket] = self.client.fetch_review_histogram(
            app_id, filter_offtopic_activity=filter_offtopic_activity,
        )
        total = len(items)
        for i, item in enumerate(items, 1):
            ctx.check_cancelled()
            ctx.write_record(item.to_dict())
            if total:
                ctx.set_progress(min(1.0, i / total))
        ctx.set_progress(1.0)
        ctx.log(f"get_review_histogram done: app_id={app_id} buckets={total}")

    def get_review_language_breakdown(self, ctx: TaskContext, params: dict) -> None:
        """Action: get_review_language_breakdown — per-language review scores.

        Server-side language is pinned to ``english`` so the tooltip
        regex is deterministic; the returned scores themselves are
        UI-language-independent. Three bucket types are emitted:
        ``your_language`` (≤1) / ``other`` (0..N) / ``all_languages`` (≤1).
        """
        app_id = str(params["app_id"])

        ctx.log(f"get_review_language_breakdown app_id={app_id}")
        ctx.check_cancelled()
        items: list[ReviewLanguageScore] = self.client.fetch_review_language_breakdown(app_id)
        total = len(items)
        for i, item in enumerate(items, 1):
            ctx.check_cancelled()
            ctx.write_record(item.to_dict())
            if total:
                ctx.set_progress(min(1.0, i / total))
        ctx.set_progress(1.0)
        ctx.log(f"get_review_language_breakdown done: app_id={app_id} rows={total}")
