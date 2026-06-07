"""
Steam API Client - SteamClient class only.

Composed via mixins:
  * SearchMixin           -> _internal/search.py        (storesearch JSON)
  * GameInfoMixin         -> _internal/game_info.py     (store-page HTML: icon, user tags)
  * ReviewsMixin          -> _internal/reviews.py       (appreviews JSON)
  * GameDetailMixin       -> _internal/game_detail.py   (appdetails / CCU / achievements / news / review histogram / language breakdown)

This file owns: __init__, cookie management, the top-selling HTML
scraper, and the shared low-level helpers (_extract_text,
_bypass_age_check, _get_json) that several mixins depend on via 'self.*'.

Dataclasses (ReviewItem, GameDetail, etc.) live in models.py.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

import requests
from bs4 import BeautifulSoup

from crawlhub.core.config import get_data_root
from crawlhub.core.platform import BaseHttpClient, CookieJar, FileCookieJar, ProbeResult
from .models import TopSellingItem, TopSellingResult
from ._internal.game_detail import GameDetailMixin
from ._internal.game_info import GameInfoMixin
from ._internal.reviews import ReviewsMixin
from ._internal.search import SearchMixin

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════
#  Steam Client
# ══════════════════════════════════════════════════════════


class SteamClient(
    SearchMixin,
    GameInfoMixin,
    ReviewsMixin,
    GameDetailMixin,
    BaseHttpClient,
):
    """
    Steam web scraper client.

    Supports:
      - Game info extraction from store pages (HTML)
      - Review crawling via official appreviews JSON API
      - Age verification bypass
      - Mature content handling
      - Cookie-based authentication
      - Top-selling chart scraping (HTML + Protobuf)
      - Game detail / current players / achievements / news (JSON APIs)
    """

    DEFAULT_HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;"
            "q=0.9,image/avif,image/webp,*/*;q=0.8"
        ),
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Sec-Ch-Ua-Platform": '"Windows"',
    }

    AJAX_HEADERS = {
        "X-Prototype-Version": "1.7",
        "X-Requested-With": "XMLHttpRequest",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    }

    def __init__(
        self,
        cookie_path: str = "",
        max_retries: int = 5,
        retry_delay: float = 5.0,
        output_dir: str = "./output",
        cookie_jar: CookieJar | None = None,
    ):
        # Resolve cookie_path early — needed both for FileCookieJar default
        # and for legacy save_cookies()/_load_cookies() helpers.
        if cookie_path:
            self.cookie_path = Path(cookie_path)
        else:
            self.cookie_path = get_data_root() / "cookies" / "steam.json"

        # Knobs used by retry helpers in mixins — must be set BEFORE
        # super().__init__ triggers _setup_sessions / cookie loading.
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # If caller did not pre-build a CookieJar, fall back to the file one
        # backed by self.cookie_path. This keeps SteamClient() zero-arg usable.
        if cookie_jar is None:
            cookie_jar = FileCookieJar(self.cookie_path)

        # BaseHttpClient.__init__ stores the jar and calls _setup_sessions().
        super().__init__(cookie_jar=cookie_jar)

    # ── BaseHttpClient contract ───────────────────────────

    def _setup_sessions(self) -> None:
        """Allocate the single requests.Session and seed it from the cookie jar."""
        self.session = requests.Session()
        self.session.headers.update(self.DEFAULT_HEADERS)

        # Seed cookies from whichever jar was attached.
        jar = self._cookie_jar
        if jar is not None:
            cookies = jar.as_dict()
            if cookies:
                self.session.cookies.update(cookies)
                logger.info("Cookies loaded via %s (%d items)", type(jar).__name__, len(cookies))

    def probe(self, task_type: str = "default") -> ProbeResult:
        """Cheap public-API ping: GetNumberOfCurrentPlayers for app=440 (TF2).

        Endpoint requires no login, so a non-zero ``result`` field means
        connectivity is healthy. We treat ``ok=True`` as "scraping for this
        platform should work" — Steam's read-only APIs are largely
        login-free, so this is a sufficient gate for the task scheduler.
        """
        url = "https://api.steampowered.com/ISteamUserStats/GetNumberOfCurrentPlayers/v1/"
        params = {"appid": "440"}
        start = time.time()
        try:
            resp = self.session.get(url, params=params, timeout=10)
            latency_ms = int((time.time() - start) * 1000)
            resp.raise_for_status()
            payload = resp.json() or {}
            body = payload.get("response") or {}
            ok = int(body.get("result", 0) or 0) == 1
            return ProbeResult(
                ok=ok,
                api="ISteamUserStats/GetNumberOfCurrentPlayers",
                latency_ms=latency_ms,
                error=None if ok else f"unexpected response payload: {payload!r}",
                extras={"task_type": task_type, "logged_in": self.is_logged_in},
            )
        except (requests.RequestException, ValueError) as e:
            latency_ms = int((time.time() - start) * 1000)
            return ProbeResult(
                ok=False,
                api="ISteamUserStats/GetNumberOfCurrentPlayers",
                latency_ms=latency_ms,
                error=str(e),
                extras={"task_type": task_type, "logged_in": self.is_logged_in},
            )

    # ── Cookie Management ─────────────────────────────────

    def save_cookies(self, cookies: dict):
        """Save cookies to JSON file (and seed live session)."""
        if not self.cookie_path:
            return
        self.cookie_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.cookie_path, "w", encoding="utf-8") as f:
            json.dump(cookies, f, ensure_ascii=False, indent=2)
        self.session.cookies.update(cookies)
        logger.info("Cookies saved to %s", self.cookie_path)

    def check_cookie_status(self) -> dict:
        """Check if cookies are loaded and valid."""
        has_cookies = bool(dict(self.session.cookies))
        has_login = bool(self.session.cookies.get("steamLoginSecure"))
        return {
            "has_cookies": has_cookies,
            "has_login_token": has_login,
            "cookie_count": len(dict(self.session.cookies)),
            "message": (
                "Logged in (steamLoginSecure present)"
                if has_login
                else "No login cookie. Some features may be limited."
            ),
        }

    # ── Top Selling Chart (HTML scraper — co-exists with protobuf scraper) ─

    def get_top_selling_chart(self, region: str = "US", date: str = None) -> TopSellingResult:
        """Get Steam sales chart via HTML scraping."""
        start_time = time.time()
        result = TopSellingResult(region=region, date=date or datetime.now().strftime("%Y-%m-%d"))

        try:
            if date:
                url = f"https://store.steampowered.com/charts/topsellers/{region}/{date}"
            else:
                url = f"https://store.steampowered.com/charts/topsellers/{region}"

            logger.info("Fetching top selling chart for region=%s, date=%s", region, date)
            response = self.session.get(url, headers=self.DEFAULT_HEADERS)
            response.raise_for_status()

            soup = BeautifulSoup(response.text, 'html.parser')
            chart_container = soup.find('div', class_='weeklytopsellers_ChartContainer')
            if not chart_container:
                chart_container = soup.find('div', class_='topsellers_ChartContainer')

            if not chart_container:
                result.status = "error"
                result.error = "Chart container not found"
                return result

            game_items = chart_container.find_all('div', class_=['weeklytopsellers_TableRow', 'topsellers_TableRow'])

            for i, item in enumerate(game_items, 1):
                try:
                    game_item = self._parse_top_selling_item(item)
                    game_item.rank = i
                    result.games.append(game_item)
                except Exception as e:
                    logger.warning("Failed to parse game item: %s", e)
                    continue

            logger.info("Got %d games from sales chart", len(result.games))

        except requests.RequestException as e:
            result.status = "error"
            result.error = f"Request failed: {e}"
            logger.error("Failed to fetch sales chart: %s", e)
        except Exception as e:
            result.status = "error"
            result.error = f"Parse failed: {e}"
            logger.error("Failed to parse sales chart: %s", e)

        result.elapsed_s = time.time() - start_time
        return result

    def _parse_top_selling_item(self, item) -> TopSellingItem:
        """Parse a single sales chart game entry (rank + app_id + game_name only)."""
        game_item = TopSellingItem()

        link = item.find('a', href=True)
        if link:
            href = link.get('href', '')
            match = re.search(r'/app/(\d+)/', href)
            if match:
                game_item.app_id = match.group(1)

        name_el = item.find('div', class_=['weeklytopsellers_GameName', 'topsellers_GameName'])
        if name_el:
            game_item.game_name = name_el.get_text(strip=True)

        return game_item

    def get_top_selling_by_week(self, region: str = "US", weeks: int = 4) -> List[TopSellingResult]:
        """Get sales chart for recent weeks."""
        results = []
        for i in range(weeks):
            target_date = (datetime.now() - timedelta(weeks=i)).strftime("%Y-%m-%d")
            result = self.get_top_selling_chart(region, target_date)
            results.append(result)
            if i < weeks - 1:
                time.sleep(1)
        return results

    # ── Top Sellers via Protobuf (preferred over HTML when it works) ────

    def get_topsellers_protobuf(
        self,
        country: str = "US",
        language: str = "schinese",
        date_str: Optional[str] = None,
        count: int = 20,
    ) -> List[dict]:
        """Fetch weekly top sellers via the official Protobuf endpoint.

        Façade in front of the internal SteamTopSellersScraper so that
        scraper.py never imports from _internal/ directly (CRWL C16).

        Returns a list of dicts (each row already includes rich details
        merged from the appdetails API). Raises on transport / parse error.
        """
        from ._internal.topsellers.scraper import SteamTopSellersScraper

        scraper = SteamTopSellersScraper(
            country=country,
            language=language,
            session=self.session,
        )
        games = scraper.get_topsellers_with_details(date_str=date_str, count=count)
        return [g.to_dict() for g in games[:count]]

    # ══════════════════════════════════════════════════════════
    #  Shared low-level helpers (used by multiple mixins)
    # ══════════════════════════════════════════════════════════

    def _extract_text(self, soup: BeautifulSoup, selectors: list) -> str:
        """Try multiple CSS selectors, return first match text."""
        for sel in selectors:
            el = soup.select_one(sel)
            if el:
                return el.get_text(strip=True)
        return ""

    def _bypass_age_check(self, app_id: str):
        """Bypass age verification via cookies + form POST fallback."""
        try:
            self.session.cookies.set("birthtime", "631152000", domain="store.steampowered.com")
            self.session.cookies.set("lastagecheckage", "1-0-1990", domain="store.steampowered.com")

            url = f"https://store.steampowered.com/agecheck/app/{app_id}/"
            data = {
                "snr": "1_agecheck_agecheck__age-gate",
                "ageDay": "1",
                "ageMonth": "January",
                "ageYear": "1990",
            }
            self.session.post(url, data=data, timeout=30)
            time.sleep(1)
        except Exception as e:
            logger.warning("Age check bypass failed: %s", e)

    def _get_json(self, url: str, params: Optional[dict] = None,
                  timeout: int = 30, label: str = "") -> Optional[dict]:
        """Shared GET-and-parse-JSON helper with retry."""
        for attempt in range(self.max_retries):
            try:
                if attempt > 0:
                    time.sleep(self.retry_delay)
                resp = self.session.get(url, params=params, timeout=timeout)
                resp.raise_for_status()
                return resp.json()
            except (requests.RequestException, ValueError) as e:
                logger.warning("%s GET %s failed (attempt %d): %s",
                               label or "_get_json", url, attempt + 1, e)
        return None
