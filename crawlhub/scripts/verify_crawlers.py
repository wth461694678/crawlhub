"""verify_crawlers — fail-fast CI gate for crawler shape & schema.

Usage::

    python -m crawlhub.scripts.verify_crawlers          # check every platform
    python -m crawlhub.scripts.verify_crawlers --only bilibili,douyin
    python -m crawlhub.scripts.verify_crawlers --root path/to/crawlers

Exit codes
----------
* ``0`` -- all crawlers pass every check (R1 + R2 + R3-static + R4)
* ``1`` -- one or more crawlers violate the shape or schema contract

Output uses pure ASCII status markers (``[OK]`` / ``[ERR]`` / ``[WARN]``)
to stay safe under the Windows GBK console.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from crawlhub.core.plugin_manifest import ManifestError, load_manifest
from crawlhub.core.shape_validator import validate_crawler_shape


def _default_crawlers_root() -> Path:
    """Locate ``crawlhub/crawlers/`` from the installed package."""
    try:
        import crawlhub.crawlers as pkg
        return Path(pkg.__file__).parent
    except (ImportError, AttributeError):
        return Path(__file__).resolve().parent.parent / "crawlers"


def _iter_platform_dirs(root: Path) -> list[Path]:
    """Return direct sub-dirs of *root* containing a plugin.yaml."""
    out: list[Path] = []
    if not root.is_dir():
        return out
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        if entry.name.startswith("_") or entry.name == "__pycache__":
            continue
        if (entry / "plugin.yaml").is_file():
            out.append(entry)
    return out


def verify_one(platform_dir: Path) -> tuple[str, list[str]]:
    """Verify a single crawler dir.

    Returns
    -------
    (platform_name, violations)
        ``violations`` is empty iff the crawler passes every check.
    """
    name = platform_dir.name  # tentative; overwritten by manifest.name on success
    violations: list[str] = []

    # ---- Schema check (R4) via load_manifest --------------------------------
    manifest_path = platform_dir / "plugin.yaml"
    try:
        manifest = load_manifest(manifest_path)
        name = manifest.name
    except ManifestError as exc:
        prefix = exc.field or "<root>"
        violations.append(
            f"plugin.yaml schema violation at '{prefix}': {exc} "
            f"-> fix: see crawlers/_template/plugin.yaml"
        )
        return name, violations

    # ---- Shape check (R1 + R2 + R3-static) ----------------------------------
    report = validate_crawler_shape(platform_dir, manifest)
    violations.extend(report.violations)

    return name, violations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify all crawlers obey the CrawlHub shape & schema contract.",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="Crawlers directory (default: crawlhub/crawlers/)",
    )
    parser.add_argument(
        "--only",
        type=str,
        default="",
        help="Comma-separated platform names to check; default = all.",
    )
    args = parser.parse_args(argv)

    root = args.root or _default_crawlers_root()
    only = {s.strip() for s in args.only.split(",") if s.strip()}

    all_dirs = _iter_platform_dirs(root)
    if only:
        all_dirs = [d for d in all_dirs if d.name in only]
    if not all_dirs:
        print(f"[ERR] no crawlers found under {root}", file=sys.stderr)
        return 1

    print(f"[INFO] verifying {len(all_dirs)} crawler(s) under {root}")
    fail_count = 0
    for d in all_dirs:
        name, violations = verify_one(d)
        if violations:
            fail_count += 1
            print(f"[ERR] {name}:")
            for v in violations:
                print(f"      - {v}")
        else:
            print(f"[OK]  {name}")

    if fail_count:
        print(
            f"\n[ERR] {fail_count}/{len(all_dirs)} crawler(s) violated the contract.",
            file=sys.stderr,
        )
        return 1

    print(f"\n[OK] {len(all_dirs)} crawlers verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
