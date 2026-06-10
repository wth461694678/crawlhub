#!/usr/bin/env python3
"""
Pre-build version consistency check.

Usage:
    # Check pyproject.toml version vs GITHUB_REF_TAG (CI)
    python scripts/check_version.py

    # Check vs explicit tag
    python scripts/check_version.py v1.2.3

    # Only check pyproject.toml format (no tag comparison)
    python scripts/check_version.py --no-tag

Exit code: 0=pass, 1=fail
"""
import re
import sys
import os
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
PYPROJECT = PROJECT_ROOT / "pyproject.toml"


def get_pyproject_version() -> str:
    """Extract version from pyproject.toml."""
    if not PYPROJECT.exists():
        print(f"[ERR] pyproject.toml not found at {PYPROJECT}")
        sys.exit(1)

    content = PYPROJECT.read_text(encoding="utf-8")
    # Match: version = "x.y.z"  (PEP 621 format)
    match = re.search(r'^version\s*=\s*"([^"]+)"', content, re.MULTILINE)
    if not match:
        print("[ERR] Could not find 'version = \"...\"' in pyproject.toml")
        sys.exit(1)

    version = match.group(1)
    print(f"[OK] pyproject.toml version: {version}")
    return version


def get_tag_version() -> str | None:
    """Get version from GITHUB_REF_TAG env var, or CLI arg."""
    # CLI arg takes priority
    if len(sys.argv) > 1 and sys.argv[1].startswith("v"):
        tag = sys.argv[1]
    else:
        tag = os.environ.get("GITHUB_REF_NAME", "")

    if not tag:
        return None

    # Normalize: "v1.2.3" -> "1.2.3"
    version = tag.lstrip("v")
    print(f"[INFO] Tag/ref version: {version} (from {tag})")
    return version


def validate_pep440(version: str) -> bool:
    """Check version looks like PEP 440 (basic check)."""
    if not re.match(r'^\d+\.\d+\.\d+(\.\d+)?$', version):
        print(f"[WARN] Version '{version}' does not match PEP 440 pattern (x.y.z)")
        return False
    return True


def main():
    no_tag = "--no-tag" in sys.argv

    # Always check pyproject.toml exists and parse version
    py_version = get_pyproject_version()
    validate_pep440(py_version)

    if no_tag:
        print("[OK] --no-tag: skipping tag comparison")
        sys.exit(0)

    tag_version = get_tag_version()
    if tag_version is None:
        print("[WARN] No tag found (not in CI or no CLI arg). Skipping comparison.")
        print("[INFO] To compare: python scripts/check_version.py v1.2.3")
        sys.exit(0)

    # Compare
    if py_version == tag_version:
        print(f"[PASS] Version match: pyproject.toml={py_version}, tag={tag_version}")
        sys.exit(0)
    else:
        print(f"[FAIL] Version mismatch!")
        print(f"  pyproject.toml: {py_version}")
        print(f"  tag/ref:        {tag_version}")
        print(f"\n[FIX] Update pyproject.toml version to '{tag_version}' before building.")
        sys.exit(1)


if __name__ == "__main__":
    main()
