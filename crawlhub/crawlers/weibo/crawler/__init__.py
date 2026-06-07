"""
Weibo crawler package.

R4 P7 (2026-05-24): public helpers ``strip_html`` and ``parse_weibo_time``
now live in ``crawler/utils.py`` (not ``_internal/``) so external callers
and the scraper can both import them without violating the C16
segregation rule.
"""

from .scraper import WeiboScraper
from .utils import strip_html, parse_weibo_time

__all__ = ["WeiboScraper", "strip_html", "parse_weibo_time"]
