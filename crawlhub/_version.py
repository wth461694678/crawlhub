"""Runtime package version helpers for CrawlHub.

The manual version source is ``pyproject.toml`` and must follow PEP 440.
At runtime, read the installed package metadata generated from that source;
do not parse ``pyproject.toml`` directly because it may not exist in wheels or
compiled distributions.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

_DISTRIBUTION_NAME = "crawlhub"
_SOURCE_TREE_VERSION = "0.0.0+local"


def get_version() -> str:
    """Return the installed CrawlHub package version."""
    try:
        return version(_DISTRIBUTION_NAME)
    except PackageNotFoundError:
        return _SOURCE_TREE_VERSION


__version__ = get_version()
