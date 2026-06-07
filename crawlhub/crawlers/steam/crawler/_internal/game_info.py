"""Steam game info mixin — HTML scraping of store page.

Originally extracted from client.py during R4 P5. Owns the small
store-page HTML routines that don't have a clean JSON-API equivalent:

  * ``get_game_icon``    — header image probe (used by other tools)
  * ``get_user_tags``    — community popular_tags (one record per tag)

The legacy ``get_game_info`` (whole-page scrape into ``GameInfo``) was
removed 2026-05-25 — ``get_game_detail`` (appdetails JSON) supersedes
it for everything except community tags, which now have their own
``get_user_tags`` action.
"""
from __future__ import annotations

import logging
import random
import time
from datetime import datetime
from typing import List

import requests
from bs4 import BeautifulSoup

from ..models import UserTag

logger = logging.getLogger(__name__)


class GameInfoMixin:
    """get_game_icon + get_user_tags — store-page HTML scraping."""

    def get_game_icon(self, app_id: str) -> dict:
        """Extract game icon (header image) from Steam store page."""
        url = f"https://store.steampowered.com/app/{app_id}?l=schinese"

        for attempt in range(self.max_retries):
            try:
                time.sleep(random.uniform(1, 2))
                logger.info("Fetching game icon for app_id=%s (attempt %d)", app_id, attempt + 1)

                resp = self.session.get(url, timeout=60, allow_redirects=True)
                resp.raise_for_status()
                resp.encoding = "utf-8"

                if "agecheck" in resp.url:
                    self._bypass_age_check(app_id)
                    resp = self.session.get(url, timeout=60, allow_redirects=True)
                    resp.raise_for_status()
                    resp.encoding = "utf-8"

                soup = BeautifulSoup(resp.text, "html.parser")
                icon_url = ""
                img_el = soup.select_one("#gameHeaderImageCtn > img")
                if img_el:
                    icon_url = img_el.get("src", "")

                game_name = self._extract_text(soup, [
                    ".apphub_AppName",
                    "#appHubAppName_responsive",
                ])

                return {
                    "app_id": app_id,
                    "icon_url": icon_url,
                    "game_name": game_name,
                }

            except requests.exceptions.RequestException as e:
                logger.warning("Request error (attempt %d): %s", attempt + 1, e)
                if attempt < self.max_retries - 1:
                    time.sleep(self.retry_delay)
                continue
            except Exception as e:
                logger.error("Unexpected error extracting game icon: %s", e)
                break

        return {"app_id": app_id, "icon_url": "", "game_name": ""}

    # ── user tags (popular_tags on the store page) ─────────────

    def get_user_tags(self, app_id: str, language: str = "schinese") -> List[UserTag]:
        """Return Steam community user-tags for a game (one record per tag).

        Source: store.steampowered.com/app/{id}?l={language}, parsing the
        ``.glance_tags.popular_tags`` block. Tag text is localized by Steam
        according to ``language`` (schinese / english / japanese / ...).

        Args:
            app_id:    Steam app id (numeric string).
            language:  Steam language code; defaults to 'schinese'.

        Returns:
            List of UserTag, in page order (rank 1..N). Empty list on
            persistent failure or if the tag block is absent.
        """
        url = f"https://store.steampowered.com/app/{app_id}?l={language}"
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        for attempt in range(self.max_retries):
            try:
                time.sleep(random.uniform(1, 3))
                logger.info(
                    "Fetching user tags for app_id=%s lang=%s (attempt %d)",
                    app_id, language, attempt + 1,
                )

                resp = self.session.get(url, timeout=60, allow_redirects=True)
                resp.raise_for_status()
                resp.encoding = "utf-8"

                if "agecheck" in resp.url:
                    logger.info("Age verification detected, bypassing...")
                    self._bypass_age_check(app_id)
                    resp = self.session.get(url, timeout=60, allow_redirects=True)
                    resp.raise_for_status()
                    resp.encoding = "utf-8"

                soup = BeautifulSoup(resp.text, "html.parser")
                container = soup.select_one(".glance_tags.popular_tags")
                if not container:
                    logger.warning("popular_tags block not found for app_id=%s", app_id)
                    return []

                out: List[UserTag] = []
                for i, a in enumerate(container.select("a.app_tag"), 1):
                    text = a.get_text(strip=True)
                    if not text:
                        continue
                    out.append(UserTag(
                        app_id=str(app_id),
                        tag=text,
                        rank=i,
                        crawl_time=ts,
                    ))
                return out

            except requests.exceptions.RequestException as e:
                logger.warning("Request error (attempt %d): %s", attempt + 1, e)
                if attempt < self.max_retries - 1:
                    time.sleep(self.retry_delay)
                continue
            except Exception as e:
                logger.error("Unexpected error extracting user tags: %s", e)
                break

        return []
