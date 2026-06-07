"""Steam reviews mixin — official appreviews JSON endpoint (cursor pagination)."""
from __future__ import annotations

import logging
import random
import time
from typing import Callable, List, Optional

from ..models import ReviewItem

logger = logging.getLogger(__name__)


class ReviewsMixin:
    """fetch_reviews — paginated cursor-based JSON API."""

    def fetch_reviews(
        self,
        app_id: str,
        max_reviews: int = 100,
        language: str = "all",
        review_type: str = "all",
        purchase_type: str = "all",
        day_range: int = 0,
        filter_: str = "recent",
        on_progress: Optional[Callable[[int, int, str], None]] = None,
    ) -> List[ReviewItem]:
        """Fetch reviews via the official appreviews JSON endpoint.

        Endpoint: GET store.steampowered.com/appreviews/<appid>?json=1
        Pagination via the response 'cursor' field.

        Args:
            app_id: Steam app ID
            max_reviews: Stop after collecting this many (0 = no cap, page until empty)
            language: 'all' / 'schinese' / 'english' / ...
            review_type: 'all' / 'positive' / 'negative'
            purchase_type: 'all' / 'steam' / 'non_steam_purchase'
            day_range: limit recency (0 = no limit; only honoured when filter='all')
            filter_: 'recent' (default) / 'updated' / 'all'
        """
        url = f"https://store.steampowered.com/appreviews/{app_id}"
        out: List[ReviewItem] = []
        cursor = "*"
        page = 0
        seen_cursors: set[str] = set()

        while True:
            page += 1
            params = {
                "json": 1,
                "filter": filter_,
                "language": language,
                "review_type": review_type,
                "purchase_type": purchase_type,
                "num_per_page": 100,
                "cursor": cursor,
            }
            if day_range and filter_ == "all":
                params["day_range"] = day_range

            if on_progress:
                on_progress(page, max(1, (max_reviews or 100) // 100),
                            f"appreviews page {page} (collected {len(out)})")

            data = self._get_json(url, params=params, timeout=30,
                                  label=f"fetch_reviews[{app_id}]")
            if not data or data.get("success") != 1:
                logger.info("appreviews stopped: success != 1 at page %d", page)
                break

            reviews = data.get("reviews") or []
            if not reviews:
                logger.info("appreviews stopped: empty page %d", page)
                break

            for r in reviews:
                author = r.get("author") or {}
                item = ReviewItem(
                    app_id=str(app_id),
                    recommendationid=str(r.get("recommendationid", "")),
                    author_steamid=str(author.get("steamid", "")),
                    author_num_games_owned=int(author.get("num_games_owned", 0) or 0),
                    author_num_reviews=int(author.get("num_reviews", 0) or 0),
                    author_playtime_forever=int(author.get("playtime_forever", 0) or 0),
                    author_playtime_at_review=int(author.get("playtime_at_review", 0) or 0),
                    author_last_played=int(author.get("last_played", 0) or 0),
                    language=str(r.get("language", "")),
                    review=str(r.get("review", "")),
                    timestamp_created=int(r.get("timestamp_created", 0) or 0),
                    timestamp_updated=int(r.get("timestamp_updated", 0) or 0),
                    voted_up=bool(r.get("voted_up", False)),
                    votes_up=int(r.get("votes_up", 0) or 0),
                    votes_funny=int(r.get("votes_funny", 0) or 0),
                    weighted_vote_score=str(r.get("weighted_vote_score", "")),
                    comment_count=int(r.get("comment_count", 0) or 0),
                    steam_purchase=bool(r.get("steam_purchase", False)),
                    received_for_free=bool(r.get("received_for_free", False)),
                    written_during_early_access=bool(r.get("written_during_early_access", False)),
                    primarily_steam_deck=bool(r.get("primarily_steam_deck", False)),
                )
                out.append(item)
                if max_reviews and len(out) >= max_reviews:
                    return out

            next_cursor = data.get("cursor")
            if not next_cursor or next_cursor in seen_cursors:
                logger.info("appreviews stopped: cursor exhausted at page %d", page)
                break
            seen_cursors.add(next_cursor)
            cursor = next_cursor

            time.sleep(random.uniform(0.5, 1.5))

        return out
