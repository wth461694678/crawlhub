"""CrawlHub scaffolding — code generators for new platform crawlers.

Public API
----------
- ``startplatform(name, ...)`` — create a new platform crawler from templates
  that is guaranteed to pass ``tests/test_platform_conformance.py`` out of the
  box.

Templates live under ``crawlhub/scaffolding/templates/`` as ``*.tpl`` files
(packaged via ``importlib.resources``). The ``.tpl`` suffix keeps them away
from ruff / mypy / pytest collection.

CLI entry: ``crawlhub platform new <name>``.
"""

from crawlhub.scaffolding.startplatform import (
    ScaffoldError,
    ScaffoldResult,
    startplatform,
)

__all__ = ["ScaffoldError", "ScaffoldResult", "startplatform"]
