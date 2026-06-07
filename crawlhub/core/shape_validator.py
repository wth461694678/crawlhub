"""Crawler directory shape validator (R1 + R2 + R3 + R6 statics).

Validates that ``crawlers/<platform>/`` follows the contract documented in
``crawlers/_template/README.md``:

  R1 (shape):
    - crawler/__init__.py     (required)
    - crawler/scraper.py      (required)
    - crawler/client.py       (required)
    - crawler/models.py       (required)

  R2 (entry class):
    - crawler/__init__.py must re-export ``<Platform>Scraper`` (PascalCase
      derived from the platform manifest's ``name`` field).

  R3 (deep-import isolation, static check):
    - service.py / bridge.py must NOT contain
      ``from crawlhub.crawlers.<platform>.crawler._internal`` or
      ``from .crawler._internal`` style imports.
      _internal/* belongs to the scraper layer only.

  R6 (write-root isolation, static check):
    - ``crawlers/<platform>/`` and ``crawlers/<platform>/crawler/`` MUST NOT
      contain any of the forbidden subdirectory names: ``output``, ``data``,
      ``logs``, ``cache``, ``downloads``.  Per-task data MUST be written to
      ``ctx.output_dir`` (~/.crawlhub/output/<date>/<task>); shared data
      MUST live under ``get_data_root()`` (~/.crawlhub/cookies, /tmp, ...).
      A platform that owns its own ``output/`` or ``data/`` directory is a
      *very* strong signal that some scraper code is bypassing the daemon's
      write contract (typical leftover from CLI-era code).

The checks are all *static* (filesystem + regex on source); no module is
imported.  This keeps ``discover_platforms()`` free of side effects.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from crawlhub.core.plugin_manifest import PluginManifest


# ---------------------------------------------------------------------------
# Custom exception
# ---------------------------------------------------------------------------

class CrawlerShapeError(ValueError):
    """Raised when a crawler directory violates the shape contract.

    Attributes
    ----------
    platform : str
        Platform name (from manifest, or the directory name when the
        manifest is unavailable).
    violations : list[str]
        Human-readable violation lines (each ends with a ``-> fix:`` hint).
    """

    def __init__(self, platform: str, violations: list[str]) -> None:
        self.platform = platform
        self.violations = list(violations)
        msg_lines = [f"Crawler '{platform}' violates the shape contract:"]
        msg_lines.extend(f"  - {v}" for v in self.violations)
        super().__init__("\n".join(msg_lines))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_REQUIRED_FILES = (
    "__init__.py",
    "service.py",
    "plugin.yaml",
    "crawler/__init__.py",
    "crawler/scraper.py",
    "crawler/client.py",
    "crawler/models.py",
)


def to_pascal_case(snake: str) -> str:
    """Convert ``example_platform`` -> ``ExamplePlatform``.

    Used to derive the expected scraper class name from
    ``manifest.name``.  Non-alnum chars are treated as separators.
    """
    parts = re.split(r"[^0-9a-zA-Z]+", snake)
    return "".join(p[:1].upper() + p[1:] for p in parts if p)


def expected_scraper_class(manifest_name: str) -> str:
    """Return ``<Platform>Scraper`` (R2 contract)."""
    return f"{to_pascal_case(manifest_name)}Scraper"


# Static deep-import patterns we forbid in service.py / bridge.py.
# Both relative (`from .crawler._internal`) and absolute (
# `from crawlhub.crawlers.<x>.crawler._internal`) styles are caught.
_DEEP_IMPORT_PATTERNS = (
    re.compile(r"from\s+\.crawler\._internal"),
    re.compile(r"from\s+\.\.crawler\._internal"),
    re.compile(r"from\s+crawlhub\.crawlers\.[\w_]+\.crawler\._internal"),
    re.compile(r"import\s+crawlhub\.crawlers\.[\w_]+\.crawler\._internal"),
)


# R6: directories that MUST NOT exist under crawlers/<platform>/ or
# crawlers/<platform>/crawler/.  Their presence is a strong signal that
# some scraper code is bypassing the ctx.output_dir contract.
_FORBIDDEN_DIRS = ("output", "data", "logs", "cache", "downloads")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

@dataclass
class ShapeReport:
    """Result of a single crawler shape inspection."""

    platform: str
    platform_dir: Path
    violations: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations


def validate_crawler_shape(
    platform_dir: Path | str,
    manifest: PluginManifest,
) -> ShapeReport:
    """Run all R1+R2+R3 static checks against *platform_dir*.

    Returns a :class:`ShapeReport`; never raises.  Callers that want
    fail-fast behaviour can do::

        report = validate_crawler_shape(...)
        if not report.ok:
            raise CrawlerShapeError(report.platform, report.violations)
    """
    platform_dir = Path(platform_dir)
    report = ShapeReport(platform=manifest.name, platform_dir=platform_dir)

    if not platform_dir.is_dir():
        report.violations.append(
            f"platform directory does not exist: {platform_dir} "
            f"-> fix: ensure manifest's location matches its on-disk path"
        )
        return report

    # ---- R1: required files -----------------------------------------------
    # In a source checkout we look for `service.py`; in a Nuitka-compiled
    # wheel deployment the same module exists only as
    # `service.<abi>-<plat>.pyd` (Windows) or `service.<abi>-<plat>.so`
    # (Linux/macOS). Accept either form so the contract still validates
    # against installed wheels.
    for rel in _REQUIRED_FILES:
        rel_path = platform_dir / rel
        if rel_path.is_file():
            continue
        # `service.py` -> look for `service.*.pyd` or `service.*.so`
        stem = rel_path.stem
        parent = rel_path.parent
        if parent.is_dir() and (
            any(parent.glob(f"{stem}.*.pyd"))
            or any(parent.glob(f"{stem}.*.so"))
        ):
            continue
        report.violations.append(
            f"missing required file '{rel}' (R1) "
            f"-> fix: create it; copy from crawlers/_template/{rel}"
        )

    # ---- R2: __init__.py re-exports <Platform>Scraper ---------------------
    init_py = platform_dir / "crawler" / "__init__.py"
    expected_cls = expected_scraper_class(manifest.name)
    if init_py.is_file():
        text = init_py.read_text(encoding="utf-8", errors="replace")
        # Two acceptable shapes:
        #   from .scraper import <Cls>
        #   from .scraper import <Cls> as <Alias>     (unusual but legal)
        pattern = re.compile(
            rf"from\s+\.scraper\s+import\s+(?:[\w,\s]+,\s*)?{expected_cls}\b"
        )
        if not pattern.search(text):
            report.violations.append(
                f"crawler/__init__.py must re-export '{expected_cls}' "
                f"(R2) -> fix: add `from .scraper import {expected_cls}`"
            )

    # ---- R3 (static): service.py / bridge.py forbidden imports ------------
    for filename in ("service.py", "bridge.py"):
        path = platform_dir / filename
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for pat in _DEEP_IMPORT_PATTERNS:
            if pat.search(text):
                report.violations.append(
                    f"{filename} imports from 'crawler._internal.*' "
                    f"(R3, deep-import) -> fix: expose what you need via "
                    f"crawler/__init__.py or move the helper to scraper.py"
                )
                break  # one violation per file is enough

    # ---- R6: forbidden write-root directories -----------------------------
    # Per-task data must go through ctx.output_dir; global data via
    # get_data_root() (cookies/, tmp/).  Any of these subdirs at the
    # platform root or under crawler/ means some module is doing
    # ``Path(__file__)/../<name>`` and writing local files.
    for parent_rel in ("", "crawler"):
        parent = platform_dir / parent_rel if parent_rel else platform_dir
        if not parent.is_dir():
            continue
        for forbidden in _FORBIDDEN_DIRS:
            offender = parent / forbidden
            if offender.is_dir():
                rel_display = (
                    f"{parent_rel}/{forbidden}/" if parent_rel else f"{forbidden}/"
                )
                report.violations.append(
                    f"forbidden directory '{rel_display}' exists (R6) "
                    f"-> fix: route per-task writes through ctx.output_dir; "
                    f"shared state must live under get_data_root() (~/.crawlhub/)"
                )

    return report


def assert_crawler_shape(
    platform_dir: Path | str,
    manifest: PluginManifest,
) -> None:
    """Fail-fast wrapper: raise :class:`CrawlerShapeError` on any violation."""
    report = validate_crawler_shape(platform_dir, manifest)
    if not report.ok:
        raise CrawlerShapeError(report.platform, report.violations)
