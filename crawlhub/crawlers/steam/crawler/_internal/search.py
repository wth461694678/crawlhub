"""Steam search mixin — storesearch JSON endpoint with rich per-row fields."""
from __future__ import annotations

import logging
from typing import List

from ..models import SearchGameResult

logger = logging.getLogger(__name__)


class SearchMixin:
    """search_games — storesearch JSON (rich fields, region-aware)."""

    def search_games(
        self,
        keyword: str,
        max_results: int = 10,
        cc: str = "US",
        language: str = "schinese",
    ) -> List[SearchGameResult]:
        """Storesearch returning rich per-row fields.

        Args:
            keyword: search term
            max_results: cap on rows returned
            cc: ISO-3166 alpha-2 region code (affects pricing currency)
            language: Steam language code (e.g. ``schinese`` / ``english``)
        """
        url = "https://store.steampowered.com/api/storesearch/"
        params = {"term": keyword, "l": language, "cc": cc}
        data = self._get_json(url, params=params, timeout=20,
                              label=f"search_games[{keyword}]")
        items = (data or {}).get("items") or []
        out: List[SearchGameResult] = []
        for it in items[:max_results]:
            app_id = str(it.get("id", ""))
            out.append(SearchGameResult(
                app_id=app_id,
                name=str(it.get("name", "") or ""),
                type=str(it.get("type", "") or ""),
                tiny_image=str(it.get("tiny_image", "") or ""),
                small_capsule=str(it.get("small_capsule", "") or ""),
                price=dict(it.get("price") or {}),
                platforms=dict(it.get("platforms") or {}),
                streamingvideo=bool(it.get("streamingvideo", False)),
                controller_support=str(it.get("controller_support", "") or ""),
                metascore=str(it.get("metascore", "") or ""),
                store_url=f"https://store.steampowered.com/app/{app_id}",
            ))
        return out
