"""Build a release wheel where ALL crawlhub .py files are Nuitka-compiled.

Strategy: full-package compilation (try-it-first approach).
  - Walk every `.py` under `crawlhub/` (excluding `__main__.py` and
    tests/scripts) and compile each one with `nuitka --module` into a
    platform+ABI specific `.pyd`/`.so` placed alongside the source.
  - Preserve all non-Python resources verbatim: `plugin.yaml`,
    `stealth.min.js`, `scaffolding/templates/*.tpl`, `frontend/*.html`,
    `*.png`, etc.
  - When building the wheel, swap the package tree so the wheel only
    contains the compiled binaries + resources, NOT the original .py.
  - The resulting wheel is platform+ABI specific, e.g.
    `crawlhub-1.1.0-cp312-cp312-win_amd64.whl`.

Why we try this approach first:
  - User explicitly asked to "compile the whole project and see if it
    runs" (per project-release skill, this is the most thorough
    source-protection option).
  - If anything breaks (likely candidates: registry plugin discovery,
    dataclass field reflection, Click command registration), the
    failure is loud and fixable.

Excluded from compilation (intentionally kept as .py):
  - `crawlhub/__main__.py`     -- bootstrap, must stay importable as script.
  - `crawlhub/_version.py`     -- read by setuptools/hatchling at build time.
  - any `__pycache__/`         -- never compile bytecode caches.
  - test files / scripts/      -- not in the distributed package anyway.

Usage:
    pip install nuitka ordered-set build
    python build_pyd.py            # full pipeline: compile + wheel
    python build_pyd.py --compile  # only run Nuitka
    python build_pyd.py --wheel    # only build wheel (assumes .pyd exists)
    python build_pyd.py --clean    # remove all generated artifacts
    python build_pyd.py --only crawlhub/core/telemetry.py  # single-file test

CI drives this script across {Windows, macOS} x {Python 3.11, 3.12};
see `.github/workflows/build.yml`.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# ──────────────────────────────────────────────────────────────────────
#  Paths & exclusions
# ──────────────────────────────────────────────────────────────────────

REPO_ROOT = Path(__file__).resolve().parent
PKG_DIR = REPO_ROOT / "crawlhub"
DIST_DIR = REPO_ROOT / "dist"
BUILD_DIR = REPO_ROOT / "build_nuitka"

# Files we explicitly do NOT compile.
EXCLUDE_FILES = {
    # Bootstrap entry — must be a real .py so `python -m crawlhub` works.
    PKG_DIR / "__main__.py",
    # Version is read as plain text by build backends.
    PKG_DIR / "_version.py",
}

# Filenames (basename) that are always excluded.
# `__init__.py` cannot be compiled by `nuitka --module` (it would need
# `--mode=package` which compiles the whole package as one unit, defeating
# our per-file approach). They usually contain only docstrings / imports /
# re-exports, so leaving them as .py is acceptable — the *real* source
# code in sibling modules is still protected.
EXCLUDE_BASENAMES = {"__init__.py"}

# Directory names that are skipped during the walk (anywhere in the tree).
EXCLUDE_DIR_NAMES = {"__pycache__", "_archive"}


# ──────────────────────────────────────────────────────────────────────
#  Discovery
# ──────────────────────────────────────────────────────────────────────

def discover_py_files() -> list[Path]:
    """Return every .py inside crawlhub/ that should be compiled."""
    result: list[Path] = []
    for path in PKG_DIR.rglob("*.py"):
        if any(part in EXCLUDE_DIR_NAMES for part in path.parts):
            continue
        if path in EXCLUDE_FILES:
            continue
        if path.name in EXCLUDE_BASENAMES:
            continue
        result.append(path)
    return sorted(result)


# ──────────────────────────────────────────────────────────────────────
#  Compilation
# ──────────────────────────────────────────────────────────────────────

def _clean_stale_binaries() -> None:
    """Remove previously generated .pyd/.so/.pyi so a failed run doesn't
    leave stale artifacts that mask new compile errors."""
    n = 0
    for ext in ("*.pyd", "*.so", "*.pyi"):
        for stale in PKG_DIR.rglob(ext):
            try:
                stale.unlink()
                n += 1
            except OSError:
                pass
    if n:
        print(f"  cleaned {n} stale binaries/stubs")


def compile_one(src: Path) -> tuple[Path, bool, str]:
    """Compile a single .py to .pyd/.so in place.

    Returns (src, success, message).
    """
    out_dir = src.parent
    cmd = [
        sys.executable, "-m", "nuitka",
        "--module",
        "--remove-output",
        "--quiet",
        "--assume-yes-for-downloads",
        f"--output-dir={out_dir}",
        str(src),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "").strip().splitlines()
        tail = "\n      ".join(msg[-6:]) if msg else "(no output)"
        return src, False, tail
    return src, True, "ok"


def step_compile_all(jobs: int = 1) -> list[Path]:
    """Compile every discovered .py.  Returns list of successfully compiled sources."""
    files = discover_py_files()
    print(f"[1/3] Found {len(files)} .py files to compile under crawlhub/")
    _clean_stale_binaries()

    successes: list[Path] = []
    failures: list[tuple[Path, str]] = []

    # Serial is the safest default; Nuitka itself uses gcc subprocesses, so
    # too much parallelism just thrashes the disk.
    if jobs <= 1:
        for i, src in enumerate(files, 1):
            rel = src.relative_to(REPO_ROOT)
            print(f"  [{i:3d}/{len(files)}] {rel} ...", end="", flush=True)
            _, ok, msg = compile_one(src)
            if ok:
                print(" ok")
                successes.append(src)
            else:
                print(" FAIL")
                print(f"      {msg}")
                failures.append((src, msg))
    else:
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            futures = {pool.submit(compile_one, src): src for src in files}
            done = 0
            for fut in as_completed(futures):
                done += 1
                src, ok, msg = fut.result()
                rel = src.relative_to(REPO_ROOT)
                if ok:
                    print(f"  [{done:3d}/{len(files)}] {rel} ok")
                    successes.append(src)
                else:
                    print(f"  [{done:3d}/{len(files)}] {rel} FAIL\n      {msg}")
                    failures.append((src, msg))

    print(f"      compiled: {len(successes)} ok, {len(failures)} failed")
    if failures:
        print()
        print("FAILURES:")
        for src, msg in failures:
            print(f"  - {src.relative_to(REPO_ROOT)}")
            print(f"      {msg}")
        raise SystemExit("Compilation failed; fix the above and re-run.")
    return successes


# ──────────────────────────────────────────────────────────────────────
#  Verification
# ──────────────────────────────────────────────────────────────────────

def step_verify() -> None:
    """Smoke-test the compiled package by importing it in a fresh subprocess.

    Done by temporarily hiding all the .py source files and seeing whether
    `crawlhub` (and several key sub-modules) still import.
    """
    print("[2/3] Verifying compiled package can be imported (with .py hidden)...")
    sources = [p for p in discover_py_files() if p.exists()]
    backups: list[tuple[Path, Path]] = []

    try:
        for src in sources:
            bak = src.with_suffix(".py.bak")
            src.rename(bak)
            backups.append((src, bak))

        check_script = r"""
import importlib, sys
checks = [
    'crawlhub',
    'crawlhub.core.config',
    'crawlhub.core.telemetry',
    'crawlhub.core.registry',
    'crawlhub.core.daemon',
    'crawlhub.cli.main',
    'crawlhub.cli.commands.init',
    'crawlhub.api.app',
    'crawlhub.crawlers.douyin.service',
    'crawlhub.crawlers.steam.service',
]
errors = []
for mod in checks:
    try:
        m = importlib.import_module(mod)
        loader = type(getattr(m, '__spec__', None).loader).__name__ if getattr(m, '__spec__', None) else '?'
        print(f'  ok  {mod:40s}  loader={loader}')
    except Exception as e:
        errors.append((mod, repr(e)))
        print(f'  FAIL {mod}: {e!r}')
if errors:
    sys.exit(1)
print('  -> all imports OK; plugin discovery test...')
from crawlhub.core.registry import discover_platforms, get_registry
discover_platforms()
plats = sorted(get_registry().keys())
print(f'  -> platforms: {plats}')
import platform as _pf
_min_plats = 5 if _pf.system() == 'Darwin' else 6
if len(plats) < _min_plats:
    print(f'  FAIL: expected >={_min_plats} platforms, got {len(plats)}')
    sys.exit(2)
"""
        result = subprocess.run(
            [sys.executable, "-c", check_script],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        print(result.stdout)
        if result.stderr:
            print(result.stderr)
        if result.returncode != 0:
            raise SystemExit(f"Verification failed (exit={result.returncode}).")
    finally:
        # Always restore — even on success, so dev environment is left clean.
        for src, bak in backups:
            if bak.exists():
                bak.rename(src)
    print("      verification passed.")


# ──────────────────────────────────────────────────────────────────────
#  Wheel build (manual zip; avoids hatchling's auto-include-of-everything)
# ──────────────────────────────────────────────────────────────────────

def step_build_wheel() -> Path:
    """Build a wheel that contains compiled binaries + non-Python resources.

    Approach: use `python -m build --wheel` after temporarily hiding all
    the compiled-away .py files. Hatchling will then see only:
      - excluded .py (e.g. __main__.py, _version.py)
      - compiled .pyd / .so
      - resource files (plugin.yaml, *.tpl, *.html, *.png, stealth.min.js)
    """
    print("[3/3] Building wheel...")
    DIST_DIR.mkdir(exist_ok=True)
    for old in DIST_DIR.glob("crawlhub-*.whl"):
        old.unlink()

    sources = [p for p in discover_py_files() if p.exists()]
    backups: list[tuple[Path, Path]] = []

    try:
        # Hide compiled-away .py
        for src in sources:
            bak = src.with_suffix(".py.bak")
            src.rename(bak)
            backups.append((src, bak))

        # Build
        result = subprocess.run(
            [sys.executable, "-m", "build", "--wheel", "--no-isolation"],
            cwd=REPO_ROOT,
        )
        if result.returncode != 0:
            raise SystemExit(f"`python -m build` failed: {result.returncode}")
    finally:
        for src, bak in backups:
            if bak.exists():
                bak.rename(src)

    wheels = sorted(DIST_DIR.glob("crawlhub-*.whl"))
    if not wheels:
        raise SystemExit("No wheel was produced.")
    wheel = wheels[-1]

    # ── Re-tag the wheel as platform-specific ─────────────────────────
    # Hatchling produces a `py3-none-any` wheel by default. But our wheel
    # contains cp3X-cp3X-<plat> .pyd/.so binaries — installing it on a
    # different Python/OS will crash at import time. Use PyPA's official
    # `wheel tags` tool to relabel it correctly.
    py_tag = f"cp{sys.version_info.major}{sys.version_info.minor}"
    if sys.platform == "win32":
        plat_tag = "win_amd64"
    elif sys.platform == "darwin":
        # macOS tag depends on arch + min deployment target; we use a
        # conservative default that matches what cibuildwheel produces.
        import platform as _platform
        arch = _platform.machine()  # 'arm64' or 'x86_64'
        plat_tag = f"macosx_11_0_{arch}"
    else:  # linux (not currently in the support matrix, but be defensive)
        import platform as _platform
        plat_tag = f"linux_{_platform.machine()}"

    print(f"      re-tagging: py3-none-any -> {py_tag}-{py_tag}-{plat_tag}")
    retag = subprocess.run(
        [sys.executable, "-m", "wheel", "tags",
         f"--python-tag={py_tag}",
         f"--abi-tag={py_tag}",
         f"--platform-tag={plat_tag}",
         "--remove",
         str(wheel)],
        cwd=DIST_DIR, capture_output=True, text=True,
    )
    if retag.returncode != 0:
        print(retag.stdout); print(retag.stderr)
        raise SystemExit("wheel re-tag failed")
    # `wheel tags --remove` deletes the original and prints the new name
    # on stdout. Re-discover the wheel.
    wheels = sorted(DIST_DIR.glob("crawlhub-*.whl"))
    wheel = wheels[-1]
    size_kb = wheel.stat().st_size // 1024
    print(f"      -> {wheel.relative_to(REPO_ROOT)} ({size_kb} KB)")

    # Audit the wheel contents.
    import zipfile
    with zipfile.ZipFile(wheel) as zf:
        names = zf.namelist()

    py_leaked = [n for n in names
                 if n.startswith("crawlhub/") and n.endswith(".py")
                 and n not in {"crawlhub/__main__.py", "crawlhub/_version.py"}
                 # __init__.py files are intentionally left as plain .py
                 # (see EXCLUDE_BASENAMES above); they only contain imports.
                 and not n.endswith("/__init__.py")
                 # Templates under scaffolding/ are .py.tpl, but raw .py
                 # template skeletons (if any) are resources, not source.
                 and "/scaffolding/templates/" not in n]
    binaries = [n for n in names
                if n.startswith("crawlhub/")
                and (n.endswith(".pyd") or n.endswith(".so"))]
    resources = [n for n in names
                 if n.startswith("crawlhub/")
                 and any(n.endswith(ext) for ext in (".yaml", ".tpl", ".html", ".png", ".js", ".md"))]

    print(f"      audit: binaries={len(binaries)}, resources={len(resources)}, "
          f"leaked-.py={len(py_leaked)}")
    if py_leaked:
        print("      [!] These .py leaked into the wheel:")
        for p in py_leaked[:10]:
            print(f"           {p}")
        raise SystemExit("Wheel audit failed — .py source leaked.")

    if not binaries:
        raise SystemExit("Wheel audit failed — no compiled binaries found.")

    print("      wheel contents verified.")
    return wheel


# ──────────────────────────────────────────────────────────────────────
#  Clean
# ──────────────────────────────────────────────────────────────────────

def step_clean() -> None:
    print("Cleaning generated artifacts...")
    for p in (BUILD_DIR, DIST_DIR, REPO_ROOT / "crawlhub.egg-info"):
        if p.exists():
            shutil.rmtree(p, ignore_errors=True)
            print(f"  removed {p.relative_to(REPO_ROOT)}/")
    n_bin = 0
    for ext in ("*.pyd", "*.so", "*.pyi"):
        for stale in PKG_DIR.rglob(ext):
            stale.unlink()
            n_bin += 1
    n_bak = 0
    for bak in PKG_DIR.rglob("*.py.bak"):
        bak.unlink()
        n_bak += 1
    print(f"  removed {n_bin} compiled binaries/stubs, {n_bak} .py.bak files")


# ──────────────────────────────────────────────────────────────────────
#  Single-file mode (for fast iteration / CI debugging)
# ──────────────────────────────────────────────────────────────────────

def step_compile_one_file(path_str: str) -> None:
    src = (REPO_ROOT / path_str).resolve()
    if not src.exists():
        raise SystemExit(f"file not found: {src}")
    if src.suffix != ".py":
        raise SystemExit(f"not a .py file: {src}")
    print(f"Single-file compile: {src.relative_to(REPO_ROOT)}")
    _, ok, msg = compile_one(src)
    if not ok:
        raise SystemExit(f"compile failed:\n  {msg}")
    binaries = list(src.parent.glob(f"{src.stem}.*.pyd")) + \
               list(src.parent.glob(f"{src.stem}.*.so"))
    print(f"  -> {binaries[0].relative_to(REPO_ROOT) if binaries else '(missing!)'}")


# ──────────────────────────────────────────────────────────────────────
#  Main
# ──────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="Build compiled crawlhub wheel")
    parser.add_argument("--compile", action="store_true", help="only run Nuitka on the whole package")
    parser.add_argument("--wheel", action="store_true", help="only build wheel (assumes binaries exist)")
    parser.add_argument("--clean", action="store_true", help="remove all generated artifacts")
    parser.add_argument("--only", metavar="PATH", help="compile a single .py file (relative to repo) and stop")
    parser.add_argument("--jobs", type=int, default=1, help="parallel compile jobs (default: 1)")
    parser.add_argument("--skip-verify", action="store_true", help="skip the post-compile import smoke-test")
    args = parser.parse_args()

    if args.clean:
        step_clean()
        return 0
    if args.only:
        step_compile_one_file(args.only)
        return 0

    print(f"Python: {sys.version.split()[0]} on {sys.platform}")
    print(f"Repo:   {REPO_ROOT}")
    print()

    if args.compile and not args.wheel:
        step_compile_all(jobs=args.jobs)
        if not args.skip_verify:
            step_verify()
        return 0
    if args.wheel and not args.compile:
        step_build_wheel()
        return 0

    # Default: full pipeline.
    step_compile_all(jobs=args.jobs)
    if not args.skip_verify:
        step_verify()
    step_build_wheel()
    print()
    print("[OK] Release wheel ready in dist/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
