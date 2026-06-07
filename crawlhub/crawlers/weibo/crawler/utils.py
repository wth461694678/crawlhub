"""
Public utility functions for the weibo crawler.

Pure helpers — HTML stripping and timestamp normalisation. Lives at the
``crawler/`` level (not under ``_internal/``) because both ``scraper.py``
and ``parsers.py`` legitimately depend on these primitives, and the
R3/R4 segregation rules forbid scraper.py from reaching into
``_internal/``.
"""

from __future__ import annotations

from datetime import datetime
from html.parser import HTMLParser


# ============================================================
# HTML 文本清理
# ============================================================
class _HTMLStripper(HTMLParser):
    """Strip HTML tags, keep raw text only."""

    def __init__(self):
        super().__init__()
        self.pieces: list[str] = []

    def handle_data(self, d):
        self.pieces.append(d)

    def get_text(self):
        return "".join(self.pieces).strip()


def strip_html(html_text: str) -> str:
    """Remove HTML tags and return plain text."""
    if not html_text:
        return ""
    s = _HTMLStripper()
    s.feed(html_text)
    return s.get_text()


def parse_weibo_time(time_str: str) -> str:
    """Convert weibo API time format (e.g. ``Sun Mar 05 14:30:00 +0800 2026``)
    to ``YYYY-MM-DD HH:MM:SS``.

    Returns the input unchanged when parsing fails.
    """
    if not time_str:
        return ""
    try:
        dt = datetime.strptime(time_str, "%a %b %d %H:%M:%S %z %Y")
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return time_str.strip()
