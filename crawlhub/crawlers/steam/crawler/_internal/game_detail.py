"""Steam game-detail mixin — appdetails / current_players / achievements / news / review histogram / language breakdown.

Extracted from client.py during R4 P5 — pure mechanical move, no logic change.
Bundles the read-only JSON / HTML-fragment endpoints because they share the
same shape (single-shot GET + small dataclass mapping).

2026-05-25: ``fetch_achievement_percentages`` rewired from the
``ISteamUserStats/GetGlobalAchievementPercentagesForApp`` JSON endpoint
(which only exposes opaque internal names + percent) to the public
community stats HTML page, which gives us localized display name +
description + icon + percent in one shot.

2026-05-25: ``fetch_review_histogram`` (``appreviewhistogram``) and
``fetch_review_language_breakdown`` (``viewlanguagereviewscores``)
added to back the two new review-shape actions.
"""
from __future__ import annotations

import logging
import random
import re
import time
from datetime import datetime, timezone
from typing import List

import requests
from bs4 import BeautifulSoup

from ..models import (
    AchievementPercentage,
    CurrentPlayers,
    GameDetail,
    NewsItem,
    ReviewHistogramBucket,
    ReviewLanguageScore,
)

logger = logging.getLogger(__name__)


class GameDetailMixin:
    """fetch_game_detail + fetch_current_players + fetch_achievement_percentages
    + fetch_news + fetch_review_histogram + fetch_review_language_breakdown."""

    # ── appdetails ────────────────────────────────────────────

    def fetch_game_detail(
        self,
        app_id: str,
        cc: str = "us",
        language: str = "schinese",
    ) -> GameDetail | None:
        """Fetch rich game profile via the appdetails endpoint.

        Endpoint: GET store.steampowered.com/api/appdetails?appids=<appid>
        Returns None if Steam reports success=false (e.g. invalid/removed app).
        """
        url = "https://store.steampowered.com/api/appdetails"
        params = {"appids": str(app_id), "cc": cc, "l": language}
        data = self._get_json(url, params=params, timeout=30,
                              label=f"fetch_game_detail[{app_id}]")
        if not data:
            return None
        block = data.get(str(app_id)) or {}
        if not block.get("success"):
            logger.warning("appdetails reports success=false for app_id=%s", app_id)
            return None
        d = block.get("data") or {}

        # Defensive extraction (some fields are missing for DLC/demos/free games)
        recs_total = 0
        if isinstance(d.get("recommendations"), dict):
            recs_total = int(d["recommendations"].get("total", 0) or 0)
        achv_total = 0
        if isinstance(d.get("achievements"), dict):
            achv_total = int(d["achievements"].get("total", 0) or 0)
        meta = d.get("metacritic") or {}

        return GameDetail(
            app_id=str(app_id),
            name=str(d.get("name", "") or ""),
            type=str(d.get("type", "") or ""),
            is_free=bool(d.get("is_free", False)),
            detailed_description=str(d.get("detailed_description", "") or ""),
            about_the_game=str(d.get("about_the_game", "") or ""),
            short_description=str(d.get("short_description", "") or ""),
            supported_languages=str(d.get("supported_languages", "") or ""),
            header_image=str(d.get("header_image", "") or ""),
            capsule_image=str(d.get("capsule_image", "") or ""),
            website=str(d.get("website", "") or ""),
            developers=list(d.get("developers") or []),
            publishers=list(d.get("publishers") or []),
            price_overview=dict(d.get("price_overview") or {}),
            platforms=dict(d.get("platforms") or {}),
            categories=list(d.get("categories") or []),
            genres=list(d.get("genres") or []),
            screenshots=list(d.get("screenshots") or []),
            movies=list(d.get("movies") or []),
            recommendations_total=recs_total,
            achievements_total=achv_total,
            release_date=dict(d.get("release_date") or {}),
            metacritic_score=int(meta.get("score", 0) or 0),
            metacritic_url=str(meta.get("url", "") or ""),
            required_age=int(d.get("required_age", 0) or 0),
            controller_support=str(d.get("controller_support", "") or ""),
            crawl_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )

    # ── current players ───────────────────────────────────────

    def fetch_current_players(self, app_id: str) -> CurrentPlayers:
        """Real-time CCU via official ISteamUserStats endpoint."""
        url = "https://api.steampowered.com/ISteamUserStats/GetNumberOfCurrentPlayers/v1/"
        params = {"appid": str(app_id)}
        data = self._get_json(url, params=params, timeout=15,
                              label=f"fetch_current_players[{app_id}]")
        body = (data or {}).get("response") or {}
        return CurrentPlayers(
            app_id=str(app_id),
            player_count=int(body.get("player_count", 0) or 0),
            result=int(body.get("result", 0) or 0),
            crawl_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )

    # ── achievement percentages ───────────────────────────────

    def fetch_achievement_percentages(
        self,
        app_id: str,
        language: str = "schinese",
    ) -> List[AchievementPercentage]:
        """Per-achievement global unlock % + localized identity, via stats HTML.

        Endpoint: ``https://steamcommunity.com/stats/{app_id}/achievements/?l={language}``

        Why HTML and not the official API?
        ``ISteamUserStats/GetGlobalAchievementPercentagesForApp`` only
        returns opaque internal names + percent. The public community
        stats page renders, for the same set of achievements:

            * h3   -> localized display name
            * h5   -> localized description
            * img  -> public lit icon URL
            * .achievePercent -> "12.3%"

        which is what downstream consumers actually want. This HTML view
        does **not** expose the API-internal name, so ``achievement_name``
        is intentionally absent from the dataclass; use display_name +
        description as the human identity instead.

        Args:
            app_id:    Steam app id (numeric string).
            language:  Steam language code; defaults to 'schinese'.

        Returns:
            List of AchievementPercentage in page order. Empty on
            persistent failure or if the stats page isn't fully rendered
            for very large catalogs (Steam truncates extremely large
            achievement sets — see CS2/Dota2). Each row carries
            ``crawl_time``.
        """
        url = (
            f"https://steamcommunity.com/stats/{app_id}/achievements/"
            f"?l={language}"
        )
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        for attempt in range(self.max_retries):
            try:
                if attempt > 0:
                    time.sleep(self.retry_delay)
                else:
                    time.sleep(random.uniform(0.8, 1.8))

                logger.info(
                    "fetch_achievement_percentages app_id=%s lang=%s (attempt %d)",
                    app_id, language, attempt + 1,
                )
                resp = self.session.get(url, timeout=30, allow_redirects=True)
                resp.raise_for_status()
                resp.encoding = "utf-8"

                soup = BeautifulSoup(resp.text, "html.parser")
                rows = soup.select(".achieveRow")
                if not rows:
                    logger.warning(
                        "no .achieveRow on stats page for app_id=%s "
                        "(login-gated, no achievements, or huge catalog truncated)",
                        app_id,
                    )
                    return []

                out: List[AchievementPercentage] = []
                for row in rows:
                    img = row.select_one(".achieveImgHolder img")
                    txt = row.select_one(".achieveTxt")
                    h3 = txt.select_one("h3") if txt else None
                    h5 = txt.select_one("h5") if txt else None
                    pct_el = row.select_one(".achievePercent")

                    icon_url = (img.get("src") or "") if img else ""
                    display_name = h3.get_text(strip=True) if h3 else ""
                    description = h5.get_text(strip=True) if h5 else ""

                    pct_value = 0.0
                    if pct_el:
                        m = re.search(r"([\d.]+)", pct_el.get_text())
                        if m:
                            try:
                                pct_value = float(m.group(1))
                            except ValueError:
                                pct_value = 0.0

                    out.append(AchievementPercentage(
                        app_id=str(app_id),
                        display_name=display_name,
                        description=description,
                        icon_url=icon_url,
                        percent=pct_value,
                        crawl_time=ts,
                    ))
                return out

            except requests.exceptions.RequestException as e:
                logger.warning(
                    "fetch_achievement_percentages request error (attempt %d): %s",
                    attempt + 1, e,
                )
                continue
            except Exception as e:
                logger.error(
                    "fetch_achievement_percentages unexpected error: %s", e,
                )
                break

        return []

    # ── news feed ─────────────────────────────────────────────

    def fetch_news(
        self,
        app_id: str,
        count: int = 20,
        maxlength: int = 0,   # 0 = no truncation (always)
        feeds: str = "",      # empty = all feeds (always)
    ) -> List[NewsItem]:
        """Official news/announcement feed for a game.

        maxlength=0 and feeds="" are hardcoded — no truncation, no feed filtering.
        """
        url = "https://api.steampowered.com/ISteamNews/GetNewsForApp/v0002/"
        params = {
            "appid": str(app_id),
            "count": count,
            "maxlength": maxlength,
            "format": "json",
        }
        if feeds:
            params["feeds"] = feeds
        data = self._get_json(url, params=params, timeout=20,
                              label=f"fetch_news[{app_id}]")
        body = (data or {}).get("appnews") or {}
        newsitems = body.get("newsitems") or []
        out: List[NewsItem] = []
        for n in newsitems:
            out.append(NewsItem(
                app_id=str(app_id),
                gid=str(n.get("gid", "") or ""),
                title=str(n.get("title", "") or ""),
                url=str(n.get("url", "") or ""),
                is_external_url=bool(n.get("is_external_url", False)),
                author=str(n.get("author", "") or ""),
                contents=str(n.get("contents", "") or ""),
                feedlabel=str(n.get("feedlabel", "") or ""),
                feedname=str(n.get("feedname", "") or ""),
                feed_type=int(n.get("feed_type", 0) or 0),
                date=int(n.get("date", 0) or 0),
                tags=list(n.get("tags") or []),
            ))
        return out

    # ── review histogram (timeline bar chart) ─────────────────

    def fetch_review_histogram(
        self,
        app_id: str,
        filter_offtopic_activity: int = 1,
    ) -> List[ReviewHistogramBucket]:
        """Time-series of review counts powering the store-page chart.

        Endpoint: ``GET store.steampowered.com/appreviewhistogram/{app_id}``.

        Effective parameters:
          * ``filter_offtopic_activity`` (0 | 1) — the ONLY param that
            actually changes the numbers. ``1`` (default) excludes
            Steam-flagged review-bombing windows from the totals.

        Cosmetic parameters (server ignores or only changes wording):
          * ``l``                       — tooltip language
          * ``review_score_preference`` — UI hint, no effect on payload
          * ``rollup_type``             — server decides (month/week/day);
                                          client cannot override

        Output:
          * Two parallel series flattened into the same record shape:
              - ``rollups``  → ``bucket_type='rollup'``, full history at
                Steam-decided granularity (rollup_type = month or week)
              - ``recent``   → ``bucket_type='recent'``, last 30 days,
                always day-granularity (rollup_type = 'day')
          * Each row carries ``is_review_bombing=True`` if its
            ``date_unix`` falls inside any range in ``past_events``.

        Returns an empty list on persistent failure or success=0.
        """
        url = f"https://store.steampowered.com/appreviewhistogram/{app_id}"
        params = {
            "l": "english",
            "review_score_preference": 0,
            "filter_offtopic_activity": int(filter_offtopic_activity),
        }
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        data = self._get_json(
            url, params=params, timeout=20,
            label=f"fetch_review_histogram[{app_id}]",
        )
        if not data or not data.get("success"):
            logger.warning(
                "appreviewhistogram returned empty / success=0 for app_id=%s",
                app_id,
            )
            return []

        results = data.get("results") or {}
        server_rollup_type = str(results.get("rollup_type") or "")  # "month" | "week"
        past_events = data.get("past_events") or []

        def _is_bombed(date_unix: int) -> bool:
            for ev in past_events:
                try:
                    s = int(ev.get("start_date", 0) or 0)
                    e = int(ev.get("end_date", 0) or 0)
                except (TypeError, ValueError):
                    continue
                if s and e and s <= date_unix <= e:
                    return True
            return False

        out: List[ReviewHistogramBucket] = []

        def _emit(bucket_type: str, rollup_type: str, items: list) -> None:
            for item in items or []:
                up = int(item.get("recommendations_up", 0) or 0)
                down = int(item.get("recommendations_down", 0) or 0)
                total = up + down
                date_unix = int(item.get("date", 0) or 0)
                date_iso = (
                    datetime.fromtimestamp(date_unix, tz=timezone.utc).strftime("%Y-%m-%d")
                    if date_unix else ""
                )
                pct = round(up * 100 / total, 2) if total > 0 else 0.0
                out.append(ReviewHistogramBucket(
                    app_id=str(app_id),
                    bucket_type=bucket_type,
                    rollup_type=rollup_type,
                    date_unix=date_unix,
                    date_iso=date_iso,
                    recommendations_up=up,
                    recommendations_down=down,
                    total_reviews=total,
                    positive_pct=pct,
                    is_review_bombing=_is_bombed(date_unix),
                    filter_offtopic_activity=int(filter_offtopic_activity),
                    crawl_time=ts,
                ))

        _emit("rollup", server_rollup_type, results.get("rollups") or [])
        _emit("recent", "day", results.get("recent") or [])
        return out

    # ── language-breakdown table (HTML fragment) ──────────────

    # Tooltip patterns we encounter (l=english):
    #   "Very Positive - 86% of the 2,542,561 user reviews in this language are positive."
    #   "86% of the 9,622,652 user reviews for this game are positive."
    # Possibly trailing: "<br><br>This product has had off-topic review activity..."
    _LANG_TOOLTIP_RE = re.compile(
        r"(?:(?P<rating>[A-Za-z][A-Za-z\s]+?)\s*-\s*)?"
        r"(?P<positive_pct>\d+)%\s*of\s*the\s*(?P<total>[\d,]+)\s*user\s*reviews",
        re.IGNORECASE,
    )

    def fetch_review_language_breakdown(
        self,
        app_id: str,
    ) -> List[ReviewLanguageScore]:
        """Per-language review-score panel from the store page.

        Endpoint: ``GET store.steampowered.com/viewlanguagereviewscores/{app_id}``.

        We always request ``l=english`` because none of the request params
        change the numbers — they only change wording. Pinning to English
        keeps the tooltip parser regex robust across regions.

        DOM classification rules (the response wraps three ``review_language_outliers_group``
        siblings; using ``find_parent`` is unsafe because the outer group
        nests an inner group with the same class name):

          * ``span`` inside a ``.score`` container, whose enclosing group
            DOES have ``.all_languages_total`` sibling   → all_languages
          * ``span`` inside a ``.score`` container, whose enclosing group
            does NOT have ``.all_languages_total``       → your_language
          * ``span`` inside a ``.languages`` container   → other

        Returns: rows in the order ``your_language`` (≤1) ->
        ``other`` (0..N) -> ``all_languages`` (≤1). Empty list on failure.
        """
        url = f"https://store.steampowered.com/viewlanguagereviewscores/{app_id}"
        # Server-pinned english tooltip — see docstring.
        params = {"l": "english"}
        headers = {
            "Accept": "text/html, */*; q=0.01",
            "Accept-Language": "en-US,en;q=0.9",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"https://store.steampowered.com/app/{app_id}/",
        }
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        text = ""
        for attempt in range(self.max_retries):
            try:
                if attempt > 0:
                    time.sleep(self.retry_delay)
                else:
                    time.sleep(random.uniform(0.3, 0.8))
                resp = self.session.get(url, params=params, headers=headers, timeout=20)
                resp.raise_for_status()
                resp.encoding = "utf-8"
                text = resp.text
                break
            except requests.exceptions.RequestException as e:
                logger.warning(
                    "fetch_review_language_breakdown request error (attempt %d): %s",
                    attempt + 1, e,
                )
        if not text:
            return []

        soup = BeautifulSoup(text, "html.parser")
        out: List[ReviewLanguageScore] = []
        total_languages_count = 0

        # First pass: scan the all_languages_total marker once for the
        # total-language count text ("Total: All 30 languages").
        all_total_div = soup.select_one(".all_languages_total")
        if all_total_div is not None:
            m = re.search(r"(\d+)", all_total_div.get_text())
            if m:
                try:
                    total_languages_count = int(m.group(1))
                except ValueError:
                    total_languages_count = 0

        # Pull the "Your Language" block's header label (e.g. "English
        # Review Score") so the your_language row carries a real language
        # name. The corresponding span text inside .score is the rating
        # ("Very Positive"), not a language.
        your_lang_label = ""
        your_lang_group = soup.find("div", class_="review_language_outliers_your_language_group")
        if your_lang_group is not None:
            # Walk back to the preceding header sibling — the header lives
            # ABOVE the group div, not inside it.
            container = your_lang_group.find_parent(
                "div", class_="review_language_outliers_group"
            )
            if container is not None:
                header = container.find_previous_sibling(
                    "div", class_="review_language_outliers_group_header"
                )
                if header is not None:
                    cat = header.select_one(".review_language_outliers_group_category")
                    if cat is not None:
                        raw = cat.get_text(strip=True)
                        # Strip the localized " Review Score" suffix when
                        # present (we always request l=english, so this
                        # keeps it as 'English' / 'Simplified Chinese' / ...).
                        your_lang_label = re.sub(
                            r"\s*Review\s*Score\s*$", "", raw, flags=re.IGNORECASE,
                        ).strip() or raw

        # Group the spans by sibling classification (your_language / other / all_languages).
        your_rows: List[ReviewLanguageScore] = []
        other_rows: List[ReviewLanguageScore] = []
        all_rows: List[ReviewLanguageScore] = []

        for span in soup.select("span.game_review_summary"):
            tooltip = (span.get("data-tooltip-html") or "").strip()
            tooltip_clean = tooltip.replace("&lt;br&gt;", " | ").replace("<br>", " | ")
            m = self._LANG_TOOLTIP_RE.search(tooltip_clean)
            if not m:
                continue
            try:
                positive_pct = int(m.group("positive_pct"))
                total_reviews = int(m.group("total").replace(",", ""))
            except (TypeError, ValueError):
                continue
            rating_from_tooltip = (m.group("rating") or "").strip()
            # The "your_language" span's tooltip omits the leading "<rating> -",
            # but the visible span text holds it (e.g. "Very Positive"). Fall back.
            rating = rating_from_tooltip or span.get_text(strip=True)
            has_bomb = "off-topic" in tooltip_clean.lower()

            # Classify by DOM container — robust against nested outer/inner
            # ``review_language_outliers_group`` divs.
            score_parent = span.find_parent(class_="score")
            languages_parent = span.find_parent(class_="languages")

            if score_parent is not None:
                # Either your_language or all_languages — disambiguate by
                # whether there's an .all_languages_total in the same
                # ``.review_language_outliers_group_languages`` container.
                container = score_parent.parent  # .review_language_outliers_group_languages
                in_all = (
                    container is not None
                    and container.find("div", class_="all_languages_total") is not None
                )
                if in_all:
                    all_rows.append(ReviewLanguageScore(
                        app_id=str(app_id),
                        bucket="all_languages",
                        language="",
                        rating_text=rating,
                        positive_pct=positive_pct,
                        total_reviews=total_reviews,
                        has_review_bombing_filter=has_bomb,
                        total_languages_count=total_languages_count,
                        crawl_time=ts,
                    ))
                else:
                    your_rows.append(ReviewLanguageScore(
                        app_id=str(app_id),
                        bucket="your_language",
                        language=your_lang_label or span.get_text(strip=True),
                        rating_text=rating,
                        positive_pct=positive_pct,
                        total_reviews=total_reviews,
                        has_review_bombing_filter=has_bomb,
                        total_languages_count=0,
                        crawl_time=ts,
                    ))
            elif languages_parent is not None:
                other_rows.append(ReviewLanguageScore(
                    app_id=str(app_id),
                    bucket="other",
                    language=span.get_text(strip=True),
                    rating_text=rating,
                    positive_pct=positive_pct,
                    total_reviews=total_reviews,
                    has_review_bombing_filter=has_bomb,
                    total_languages_count=0,
                    crawl_time=ts,
                ))
            # If neither container matched, drop the span — defensive.

        out.extend(your_rows)
        out.extend(other_rows)
        out.extend(all_rows)
        return out
