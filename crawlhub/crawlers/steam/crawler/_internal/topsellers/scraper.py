"""
Steam Top Sellers Scraper
============================

Fetches Steam weekly top sellers via Protobuf API:
  - IStoreTopSellersService/GetWeeklyTopSellers/v1
  - IStoreBrowseService/GetItems/v1

Uses pure-Python Protobuf wire-format encoding (no protobuf dependency).
"""

from __future__ import annotations

import base64
import logging
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

import requests

from .models import (
    TopSellerItem,
    build_get_items_request,
    build_get_weekly_top_sellers_request,
    date_to_tuesday_timestamp,
)

logger = logging.getLogger(__name__)

# Steam API endpoints
TOP_SELLERS_URL = (
    "https://api.steampowered.com"
    "/IStoreTopSellersService/GetWeeklyTopSellers/v1"
)
GET_ITEMS_URL = (
    "https://api.steampowered.com"
    "/IStoreBrowseService/GetItems/v1"
)

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": "https://store.steampowered.com/",
}


@dataclass
class TopSellerGame:
    """Game info from top sellers (rank + app_id + name only)."""
    rank: int = 0
    app_id: int = 0
    name: str = ""

    def to_dict(self) -> dict:
        return {
            "rank": self.rank,
            "app_id": str(self.app_id),
            "game_name": self.name,
        }


class SteamTopSellersScraper:
    """Scrape Steam weekly top sellers via Protobuf APIs.

    Usage::
        scraper = SteamTopSellersScraper(country="US", language="schinese")
        games = scraper.get_topsellers_with_details(
            date_str="2026-04-28", count=20
        )
        for game in games:
            print(game.to_dict())
    """

    def __init__(
        self,
        country: str = "US",
        language: str = "schinese",
        session: Optional[requests.Session] = None,
    ):
        self.country = country
        self.language = language
        self.session = session or requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)

    # ---- Public API -------------------------------------------------

    def get_topsellers(
        self,
        date_str: Optional[str] = None,
        count: int = 20,
    ) -> List[TopSellerItem]:
        """Get weekly top sellers (app IDs + ranks only).

        Args:
            date_str: Date YYYY-MM-DD (any day in target week).
                      None = current week.
            count: Number of results (max 20 per request).

        Returns:
            List of TopSellerItem (no details yet).
        """
        start_date = None
        if date_str:
            start_date = date_to_tuesday_timestamp(date_str)

        req_bytes = build_get_weekly_top_sellers_request(
            country_code=self.country,
            language=self.language,
            start_date=start_date,
            count=count,
        )

        params = {
            "origin": "https://store.steampowered.com",
            "input_protobuf_encoded": base64.b64encode(req_bytes).decode("ascii"),
        }

        resp = self.session.get(TOP_SELLERS_URL, params=params, timeout=30)
        resp.raise_for_status()

        # Response is Protobuf binary
        return parse_top_sellers_response(resp.content)

    def get_topsellers_with_details(
        self,
        date_str: Optional[str] = None,
        count: int = 20,
    ) -> List[TopSellerGame]:
        """Get weekly top sellers + enrich with game details.

        Args:
            date_str: Date YYYY-MM-DD (any day in target week).
                      None = current week.
            count: Number of results.

        Returns:
            List of TopSellerGame with full details.
        """
        # Step 1: Get top sellers (app IDs)
        items = self.get_topsellers(date_str=date_str, count=count)
        if not items:
            logger.warning("[WARN] get_topsellers returned empty, falling back to HTML scrape")
            return self._fallback_html_scrape(date_str, count)

        app_ids = [item.app_id for item in items]
        rank_map = {item.app_id: item.rank for item in items}

        # Step 2: Batch get game details
        details = self.get_items_details(app_ids)

        # Step 3: Merge
        results = []
        for app_id in app_ids:
            game = TopSellerGame(
                rank=rank_map.get(app_id, 0),
                app_id=app_id,
            )
            if app_id in details:
                d = details[app_id]
                game.name = d.get("name", "")
            results.append(game)

        return results

    def get_items_details(self, app_ids: List[int]) -> Dict[int, dict]:
        """Batch fetch game details via GetItems API.

        Args:
            app_ids: List of Steam app IDs.

        Returns:
            Dict mapping app_id -> detail dict.
        """
        req_bytes = build_get_items_request(
            app_ids=app_ids,
            language=self.language,
            country=self.country,
        )

        params = {
            "origin": "https://store.steampowered.com",
            "input_protobuf_encoded": base64.b64encode(req_bytes).decode("ascii"),
        }

        resp = self.session.get(GET_ITEMS_URL, params=params, timeout=30)
        resp.raise_for_status()

        return parse_get_items_response(resp.content)

    # ---- HTML Fallback ----------------------------------------------

    def _fallback_html_scrape(
        self,
        date_str: Optional[str] = None,
        count: int = 20,
    ) -> List[TopSellerGame]:
        """Fallback: scrape top sellers from HTML page."""
        if date_str:
            url = f"https://store.steampowered.com/charts/topsellers/{self.country.lower()}/{date_str}"
        else:
            url = f"https://store.steampowered.com/charts/topsellers/{self.country.lower()}"

        resp = self.session.get(url, timeout=30)
        resp.raise_for_status()

        from bs4 import BeautifulSoup
        soup = BeautifulSoup(resp.text, "html.parser")

        results = []
        rows = soup.find_all("div", class_=re.compile(r"(weekly|top)sellers_TableRow"))
        for i, row in enumerate(rows[:count], 1):
            game = TopSellerGame(rank=i)

            # App ID from link
            link = row.find("a", href=True)
            if link:
                m = re.search(r"/app/(\d+)/", link.get("href", ""))
                if m:
                    game.app_id = int(m.group(1))

            # Game name
            name_el = row.find("div", class_=re.compile(r"(Game|App)Name"))
            if name_el:
                game.name = name_el.get_text(strip=True)

            results.append(game)

        return results


# //// Response Parsers (Protobuf wire format) /////////////////////

def parse_top_sellers_response(data: bytes) -> List[TopSellerItem]:
    """Parse GetWeeklyTopSellers Protobuf response.

    Delegates to models.parse_weekly_top_sellers_response().
    """
    from .models import parse_weekly_top_sellers_response as _parse
    return _parse(data)


def parse_get_items_response(data: bytes) -> Dict[int, dict]:
    """Parse GetItems Protobuf response.

    Delegates to models.parse_get_items_response().
    """
    from .models import parse_get_items_response as _parse
    return _parse(data)
