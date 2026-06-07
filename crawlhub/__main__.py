"""Allow running crawlhub as a module: python -m crawlhub."""

# ════════════════════════════════════════════════════════════════════════════
#  R7 Observability — MUST be first; this entrypoint does NOT go through
#  crawlhub/cli/__init__.py (it imports crawlhub.cli.main directly), so we
#  must install patches here too. Idempotent.
# ════════════════════════════════════════════════════════════════════════════
from crawlhub.core.observability import install_all as _r7_install_all
_r7_install_all()

from crawlhub.cli.main import main  # noqa: E402

if __name__ == "__main__":
    main()
