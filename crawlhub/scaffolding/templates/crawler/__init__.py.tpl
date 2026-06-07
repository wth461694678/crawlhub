"""Public entry point for the `{{platform_name}}` crawler.

R2 — ONLY this file may be imported by `service.py`. Everything else
(client, models, _internal/) is an implementation detail.

The single re-export below is the one stable contract:

    from .crawler import {{scraper_class}}

If you need to expose more public types later (e.g. a typed enum), add
them here — do NOT have callers reach into submodules.
"""

from .scraper import {{scraper_class}}

__all__ = ["{{scraper_class}}"]
