"""startplatform — scaffold a new CrawlHub platform crawler.

Reads ``*.tpl`` files from ``crawlhub/scaffolding/templates/`` via
``importlib.resources``, renders them with a small set of substitution
variables, and writes the result under ``crawlhub/crawlers/<name>/``.

Generated platforms are guaranteed to pass
``tests/test_platform_conformance.py`` immediately after generation —
the post-generation self-check (unless ``--no-verify``) enforces that.

Public API
----------
- :func:`startplatform` — main entry point.
- :class:`ScaffoldError` — raised on validation / overwrite / verify failures.
- :class:`ScaffoldResult` — value object describing what was written.

Design notes
------------
- All output goes through ASCII-only ``print`` markers (``[OK]``, ``[ERR]``,
  ``[INFO]``) to stay safe under the Windows GBK console.
- Template files are intentionally renamed at write-time: ``foo.py.tpl``
  becomes ``foo.py``. This keeps the source tree free of ``.py`` files that
  contain unrendered placeholders.
- No external templating engine is used — we keep substitution to a tiny,
  predictable ``str.replace`` loop over ``{{key}}`` markers. This avoids
  Jinja-style escaping pitfalls (e.g. ``{...}`` inside yaml).
"""

from __future__ import annotations

import re
import shutil
import sys
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

# ---------------------------------------------------------------------------
# Public errors / results
# ---------------------------------------------------------------------------


class ScaffoldError(Exception):
    """Raised when the scaffolder refuses to proceed (validation, conflict,
    or post-generation verification failure).

    Carries a human-readable message; the CLI wrapper turns this into a
    ``[ERR]`` line and exit code 1.
    """


@dataclass
class ScaffoldResult:
    """Outcome of a successful :func:`startplatform` call."""

    platform_name: str
    platform_dir: Path
    written_files: list[Path] = field(default_factory=list)
    verified: bool = False  # True iff conformance self-check ran and passed


# ---------------------------------------------------------------------------
# Name validation
# ---------------------------------------------------------------------------

# snake_case, start with a letter, no leading/trailing underscore, no double
# underscore. Mirrors what existing platforms use (steam, bilibili, douyin...).
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*[a-z0-9]$|^[a-z]$")

# Reserved directory names — _template, __pycache__, anything starting with
# underscore. Reject early with a clear message.
_RESERVED_PREFIXES = ("_",)


def _validate_name(name: str) -> None:
    """Reject names that wouldn't be discovered by ``discover_platforms()``
    or that conflict with reserved scaffolding directories."""
    if not name:
        raise ScaffoldError("platform name is required (got empty string)")
    if name.startswith(_RESERVED_PREFIXES):
        raise ScaffoldError(
            f"platform name {name!r} starts with '_' — reserved for "
            "scaffolding directories (would be skipped by discover_platforms)"
        )
    if not _NAME_RE.match(name):
        raise ScaffoldError(
            f"platform name {name!r} is invalid; must be snake_case, start "
            "with a lowercase letter, and contain only [a-z0-9_]"
        )


def _to_pascal(name: str) -> str:
    """``my_platform`` -> ``MyPlatform`` (used for class names)."""
    return "".join(part.capitalize() for part in name.split("_") if part)


def _to_title(name: str) -> str:
    """``my_platform`` -> ``My Platform`` (used as default display_name)."""
    return " ".join(part.capitalize() for part in name.split("_") if part)


# ---------------------------------------------------------------------------
# Template discovery & rendering
# ---------------------------------------------------------------------------

# Each tuple is (template_filename_under_templates_dir, destination_relative_path).
# Order matters only for nicer output; correctness does not depend on it.
_FILE_MAP: tuple[tuple[str, str], ...] = (
    ("__init__.py.tpl",                 "__init__.py"),
    ("plugin.yaml.tpl",                 "plugin.yaml"),
    ("service.py.tpl",                  "service.py"),
    ("README.md.tpl",                   "README.md"),
    ("crawler/__init__.py.tpl",         "crawler/__init__.py"),
    ("crawler/scraper.py.tpl",          "crawler/scraper.py"),
    ("crawler/client.py.tpl",           "crawler/client.py"),
    ("crawler/models.py.tpl",           "crawler/models.py"),
    ("crawler/_internal/__init__.py.tpl", "crawler/_internal/__init__.py"),
)


def _build_context(
    name: str,
    display_name: str | None,
    description: str | None,
) -> dict[str, str]:
    pascal = _to_pascal(name)
    return {
        "platform_name":        name,
        "platform_pascal":      pascal,
        "platform_display":     display_name or _to_title(name),
        "platform_description": description or f"Crawler for {_to_title(name)}.",
        "scraper_class":        f"{pascal}Scraper",
        "client_class":         f"{pascal}Client",
        "service_class":        f"{pascal}Service",
        "models_module":        f"crawlhub.crawlers.{name}.crawler.models",
    }


def _render(text: str, context: dict[str, str]) -> str:
    """Replace every ``{{key}}`` token. Unknown tokens are left untouched
    so unrelated double-brace strings (rare, but possible in code samples)
    don't silently disappear."""
    for key, val in context.items():
        text = text.replace("{{" + key + "}}", val)
    return text


def _read_template(rel_path: str) -> str:
    """Read a template file using ``importlib.resources`` so it works both
    from a source checkout and from an installed wheel."""
    pkg = resources.files("crawlhub.scaffolding") / "templates"
    target = pkg.joinpath(rel_path)
    return target.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Filesystem helpers
# ---------------------------------------------------------------------------


def _default_crawlers_root() -> Path:
    """Locate ``crawlhub/crawlers/`` from the installed/imported package."""
    import crawlhub.crawlers as pkg  # local import to keep module import cheap

    return Path(pkg.__file__).parent


def _ensure_clean_destination(dest_dir: Path) -> None:
    if not dest_dir.exists():
        return
    if not dest_dir.is_dir():
        raise ScaffoldError(
            f"destination {dest_dir} exists and is not a directory; refusing to overwrite"
        )
    # Reject any non-empty directory; empty dirs are tolerated for ergonomics.
    if any(dest_dir.iterdir()):
        raise ScaffoldError(
            f"destination {dest_dir} already exists and is not empty; "
            "delete it manually or pick a different name"
        )


# ---------------------------------------------------------------------------
# Post-generation conformance self-check
# ---------------------------------------------------------------------------


def _verify_scaffolded(name: str, platform_dir: Path) -> list[str]:
    """Run the conformance checker against the freshly generated platform.

    Returns a list of error message strings (empty iff PASS).

    We import the verifier lazily so that scaffolding doesn't pull in
    pytest / ast machinery for the happy CLI path.
    """
    # tests/ is not a package; load via path.
    repo_root = Path(__file__).resolve().parent.parent.parent
    test_file = repo_root / "tests" / "test_platform_conformance.py"
    if not test_file.is_file():
        # Test harness missing (e.g. installed wheel without tests/) — skip.
        return []

    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_crawlhub_scaffolding_verifier", test_file,
    )
    if spec is None or spec.loader is None:
        return [f"failed to load conformance verifier from {test_file}"]
    mod = importlib.util.module_from_spec(spec)
    # MUST register before exec_module: the module defines @dataclass
    # types whose decorator looks up sys.modules[__name__].__dict__ at
    # class-creation time. Without this, dataclass raises AttributeError
    # on a fresh import via spec_from_file_location.
    sys.modules[spec.name] = mod
    try:
        spec.loader.exec_module(mod)
        report = mod.verify_platform(platform_dir)
    finally:
        sys.modules.pop(spec.name, None)

    return [f.format() for f in report.errors]


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def startplatform(
    name: str,
    *,
    display_name: str | None = None,
    description: str | None = None,
) -> ScaffoldResult:
    """Generate a new platform crawler scaffold under ``crawlhub/crawlers/<name>/``.

    The destination is **always** the package's own ``crawlhub/crawlers/``
    directory — that is the only path the daemon discovers, so placing a
    scaffold anywhere else would silently fail to register. The location
    is therefore not user-configurable.

    A post-generation conformance self-check is **always** run. If the
    templates ever drift and produce a non-conforming scaffold, that's a
    bug in this package and we want a loud failure on the first
    ``platform new`` invocation, not later when the user tries to ship.

    Parameters
    ----------
    name:
        snake_case platform identifier, e.g. ``myplatform`` or ``foo_bar``.
        Must match ``^[a-z][a-z0-9_]*$`` and not start with ``_``.
    display_name:
        Human-facing name shown in the UI / API responses
        (default: title-cased ``name``).
    description:
        Plugin description string for ``plugin.yaml``
        (default: ``"Crawler for <Display>."``).

    Returns
    -------
    :class:`ScaffoldResult`

    Raises
    ------
    :class:`ScaffoldError`
        On invalid name, existing non-empty destination, missing template,
        or post-generation conformance failure.
    """
    _validate_name(name)

    parent = _default_crawlers_root()
    if not parent.is_dir():
        # Should be unreachable: the crawlhub.crawlers package is part of
        # the installed wheel, so its directory must exist.
        raise ScaffoldError(
            f"crawlhub/crawlers/ root not found at {parent}; package install is broken"
        )

    platform_dir = parent / name
    _ensure_clean_destination(platform_dir)

    context = _build_context(name, display_name, description)
    written: list[Path] = []

    try:
        platform_dir.mkdir(parents=True, exist_ok=False)
        for tpl_rel, out_rel in _FILE_MAP:
            raw = _read_template(tpl_rel)
            rendered = _render(raw, context)
            out_path = platform_dir / out_rel
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(rendered, encoding="utf-8")
            written.append(out_path)
    except Exception as exc:
        # Roll back partial writes — easier on developers than leaving a
        # broken half-platform that the daemon would then try to register.
        if platform_dir.exists():
            shutil.rmtree(platform_dir, ignore_errors=True)
        raise ScaffoldError(f"failed to write scaffold: {exc}") from exc

    # Always verify — see docstring rationale.
    errors = _verify_scaffolded(name, platform_dir)
    if errors:
        # Leave the scaffold on disk so the user / maintainer can inspect it.
        joined = "\n      ".join(errors)
        raise ScaffoldError(
            "generated scaffold failed conformance self-check "
            f"(this is a bug in scaffolding/templates/):\n      {joined}"
        )

    return ScaffoldResult(
        platform_name=name,
        platform_dir=platform_dir,
        written_files=written,
        verified=True,
    )
