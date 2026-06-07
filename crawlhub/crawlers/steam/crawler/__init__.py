# Steam Crawler Package
# Migrated from crawlhub.crawlers.steam as part of CRWL-002 (2026-05-20)
# R4 P5 (2026-05-24): SteamTopSellersScraper removed from public surface —
# external callers go through SteamClient.get_topsellers_protobuf().
# 2026-05-25: v1 search/reviews removed; JSON-API actions promoted to
# canonical names (search_games / scrape_reviews).
# 2026-05-25: GameInfo / get_game_info removed; get_game_detail covers
# the same surface with richer JSON fields.

from .client import SteamClient
from .models import ReviewItem
from .scraper import SteamScraper

__all__ = [
    "SteamClient",
    "ReviewItem",
    "SteamScraper",
]
