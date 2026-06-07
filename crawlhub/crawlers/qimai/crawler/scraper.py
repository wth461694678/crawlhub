"""QimaiScraper — business orchestration layer.

Layer-3 (orchestration) responsibility:
  * Compose calls to ``client.py`` (network) and ``models.py`` (mapping)
  * Loop / paginate / merge multi-step flows
  * Write records directly via ``ctx.write_record(...)``
  * Manage cookie health via ``CookieResolverMixin``

Strict rules (enforced by tests/test_platform_conformance.py):
  * Class name MUST be ``QimaiScraper`` in PascalCase.
  * Class MUST be re-exported by ``crawler/__init__.py``.
  * Each public method name MUST equal a key in plugin.yaml.actions (C14).
  * Public action methods MUST be plain (non-generator) functions with
    signature ``(self, ctx, params)`` (C14, C15).
  * Methods drive ``ctx.write_record(...)`` themselves; service.py only
    dispatches.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from crawlhub.core.cookie_resolver import CookieResolverMixin
from crawlhub.core.registry import CookieStatus

from .client import QimaiClient
from .models import GlobalRankApp, GlobalRankCountry, RankRecord, SearchAppRecord


logger = logging.getLogger(__name__)


class QimaiScraper(CookieResolverMixin):
    """Orchestrates QimaiClient + models for the qimai platform."""

    PLATFORM_NAME = "qimai"

    def __init__(self, cookie_path: str | None = None) -> None:
        self._cookie_path = cookie_path
        self._client = self._build_client(cookie_path)

    @property
    def client(self) -> QimaiClient:
        """Lazy-loaded QimaiClient (BaseService.check_cookie expects this)."""
        return self._client

    # ------------------------------------------------------------------
    # Client construction
    # ------------------------------------------------------------------

    @staticmethod
    def _build_client(cookie_path: str | None) -> QimaiClient:
        """QimaiClient does not accept cookie_path directly; we load the
        cookie file ourselves and pass cookie_string / username+password.
        """
        if not cookie_path:
            return QimaiClient()
        try:
            with open(cookie_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return QimaiClient()
        cookie_string = data.get("cookie_string", "")
        username = data.get("username")
        password = data.get("password")
        if cookie_string:
            return QimaiClient(cookie_string=cookie_string)
        if username and password:
            return QimaiClient(username=username, password=password)
        return QimaiClient()

    # ------------------------------------------------------------------
    # CookieResolverMixin hook
    # ------------------------------------------------------------------

    def check_cookie_valid(self) -> bool:
        """Lightweight probe used by ``ensure_cookie()``.

        Returns True if the underlying client believes it is logged in.
        """
        try:
            return bool(self._client.is_logged_in)
        except Exception:  # noqa: BLE001
            return False

    def check_cookie(self) -> CookieStatus:
        """CookieStatus probe used by ``QimaiService.check_cookie``.

        Mirrors the previous service-layer implementation so behaviour
        is unchanged after the R3 refactor.
        """
        cookie_path = Path(self._cookie_path) if self._cookie_path else None
        if cookie_path is None or not cookie_path.exists():
            try:
                cookie_path = self.resolve_cookie_path()
            except Exception:  # noqa: BLE001
                cookie_path = None
        if cookie_path is None or not cookie_path.exists():
            return CookieStatus(status="missing", message="No qimai cookie file found.")

        try:
            if self._client.is_logged_in:
                uname = ""
                if self._client.userinfo:
                    uname = self._client.userinfo.get("username", "")
                msg = f"Logged in: {uname}" if uname else "Logged in."
                return CookieStatus(status="valid", message=msg)
            return CookieStatus(status="expired", message="Cookie exists but session expired.")
        except Exception:  # noqa: BLE001
            try:
                with open(cookie_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if data.get("username") or data.get("cookie_string"):
                    return CookieStatus(status="valid", message="Cookie file exists (unverified).")
            except Exception:  # noqa: BLE001
                pass
            return CookieStatus(status="expired", message="Cookie file is corrupted.")

    # ------------------------------------------------------------------
    # action: search_app
    # ------------------------------------------------------------------

    def search_app(self, ctx, params: dict[str, Any]) -> None:

        """Search apps by keyword.

        Yields records matching plugin.yaml.actions.search_app.output_schema:
            app_id, app_name, subtitle, icon
        """
        keyword = params["keyword"]
        country = params.get("country", "cn")

        apps = self._client.search_app_simple(keyword, country)
        for app in apps:
            ctx.write_record(SearchAppRecord.from_raw(app).to_dict())
        ctx.set_progress(1.0)

    # ------------------------------------------------------------------
    # action: get_rank_daily
    # ------------------------------------------------------------------

    def get_rank_daily(self, ctx, params: dict[str, Any]) -> None:
        """Get daily rank time-series for an app.

        Yields a single record matching plugin.yaml.actions.get_rank_daily.output_schema:
            app_id, sdate, edate, country, brand, data

        When the API returns code=20000 (no ranking data for this app),
        we still emit a record with ``data={}`` — this is a normal result,
        not an error.
        """
        app_id = params["app_id"]
        sdate = params["sdate"]
        edate = params["edate"]
        country = params.get("country", "cn")
        brand = params.get("brand", "free")

        data = self._client.get_rank_daily(app_id, sdate, edate, country, brand)
        rank_data = data.get("data", {}) if data.get("code") == 20000 else data
        ctx.write_record(
            RankRecord(
                app_id=app_id,
                sdate=sdate,
                edate=edate,
                country=country,
                brand=brand,
                data=rank_data,
            ).to_dict()
        )
        ctx.set_progress(1.0)

    # ------------------------------------------------------------------
    # action: get_global_rank
    # ------------------------------------------------------------------

    def get_global_rank(self, ctx, params: dict[str, Any]) -> None:
        """Get global rank overview for a date.

        Yields one record per country, matching
        plugin.yaml.actions.get_global_rank.output_schema:
            country, country_code, apps
        """
        date = params["date"]
        genre = params.get("genre", "6014")
        brand = params.get("brand", "grossing")
        device = params.get("device", "iphone")

        country_data = self._client.get_global_rank_apps(
            date, genre, device, "0", brand=brand,
        )
        total = max(len(country_data), 1)
        for idx, group in enumerate(country_data, 1):
            apps_list = [
                GlobalRankApp.from_raw(app).to_dict()
                for app in group.get("apps", [])
            ]
            ctx.write_record(
                GlobalRankCountry(
                    country=group.get("country", ""),
                    country_code=group.get("country_code", ""),
                    apps=apps_list,
                ).to_dict()
            )
            ctx.set_progress(idx / total)

    # ------------------------------------------------------------------
    # action: get_rank_hourly
    # ------------------------------------------------------------------

    def get_rank_hourly(self, ctx, params: dict[str, Any]) -> None:
        """Get hourly rank time-series for an app.

        Yields a single record matching plugin.yaml.actions.get_rank_hourly.output_schema:
            app_id, sdate, edate, country, brand, data

        When the API returns code=20000 (no ranking data for this app),
        we still emit a record with ``data={}`` — this is a normal result,
        not an error.
        """
        app_id = params["app_id"]
        sdate = params["sdate"]
        edate = params["edate"]
        country = params.get("country", "cn")
        brand = params.get("brand", "free")

        data = self._client.get_rank_hourly(app_id, sdate, edate, country, brand)
        rank_data = data.get("data", {}) if data.get("code") == 20000 else data
        ctx.write_record(
            RankRecord(
                app_id=app_id,
                sdate=sdate,
                edate=edate,
                country=country,
                brand=brand,
                data=rank_data,
            ).to_dict()
        )
        ctx.set_progress(1.0)
