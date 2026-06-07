"""Data models for qimai crawler.

Layer-2 (data contract) responsibility ONLY:
  * Define dataclass-shaped records that scraper.py emits
  * to_dict() is inherited from ``BaseRecord`` and equals
    ``dataclasses.asdict(self)``; field names MUST match the
    corresponding output_schema in plugin.yaml exactly.

Why this layer exists:
  * Keeps client.py free of business mapping
  * Keeps scraper.py free of inline dict-construction noise
  * Makes the contract between crawler and crawlhub explicit.

Allowed in this file:
  * @dataclass declarations (each extends ``BaseRecord``)
  * from_raw() classmethod that maps raw client payload -> dataclass

Forbidden:
  * Network calls
  * Anything that depends on TaskContext
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from crawlhub.core.platform.base_models import BaseRecord


# ---------------------------------------------------------------------------
# search_app  (output_schema: app_id, app_name, subtitle, icon)
# ---------------------------------------------------------------------------

@dataclass
class SearchAppRecord(BaseRecord):
    """One row returned by QimaiScraper.search_app().

    Field set MUST match plugin.yaml -> actions.search_app.output_schema:
      app_id   VARCHAR
      app_name VARCHAR
      subtitle VARCHAR
      icon     VARCHAR
    """

    app_id: str
    app_name: str
    subtitle: str
    icon: str

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> "SearchAppRecord":
        """Map raw client payload entry -> SearchAppRecord."""
        return cls(
            app_id=str(raw.get("app_id", "")),
            app_name=str(raw.get("app_name", "")),
            subtitle=str(raw.get("subtitle", "")),
            icon=str(raw.get("icon", "")),
        )


# ---------------------------------------------------------------------------
# get_global_rank  (output_schema: country, country_code, apps)
# ---------------------------------------------------------------------------

@dataclass
class GlobalRankApp(BaseRecord):
    """One app entry inside a global rank country group."""

    rank: int
    app_id: str
    app_name: str
    icon: str

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> "GlobalRankApp":
        return cls(
            rank=int(raw.get("times", 0)),
            app_id=str(raw.get("app_id", "")),
            app_name=str(raw.get("app_name", "")),
            icon=str(raw.get("icon", "")),
        )


@dataclass
class GlobalRankCountry(BaseRecord):
    """One country group in global rank results.

    Field set MUST match plugin.yaml -> actions.get_global_rank.output_schema:
      country       VARCHAR
      country_code  VARCHAR
      apps          JSON
    """

    country: str
    country_code: str
    apps: list[dict[str, Any]]


# ---------------------------------------------------------------------------
# get_rank_daily / get_rank_hourly
# (output_schema: app_id, sdate, edate, country, brand, data)
# ---------------------------------------------------------------------------

@dataclass
class RankRecord(BaseRecord):
    """One row for daily/hourly rank results.

    Field set MUST match plugin.yaml -> actions.get_rank_daily.output_schema
    and actions.get_rank_hourly.output_schema:
      app_id  VARCHAR
      sdate   VARCHAR
      edate   VARCHAR
      country VARCHAR
      brand   VARCHAR
      data    JSON
    """

    app_id: str
    sdate: str
    edate: str
    country: str
    brand: str
    data: dict[str, Any]
