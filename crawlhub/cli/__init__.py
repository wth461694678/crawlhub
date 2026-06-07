"""CLI package."""

# ════════════════════════════════════════════════════════════════════════════
#  R7 Observability — MUST be the first executable line of the cli package.
#  Covers both `crawlhub` and `crawlhub-mcp` console_script entrypoints,
#  which both implicitly import `crawlhub.cli` (this __init__.py runs first).
#  Idempotent — safe even if also called from __main__.py.
# ════════════════════════════════════════════════════════════════════════════
from crawlhub.core.observability import install_all as _r7_install_all
_r7_install_all()

from crawlhub.cli.main import main  # noqa: E402

__all__ = ["main"]
