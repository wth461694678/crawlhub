"""`crawlhub init` — one-shot environment bootstrap.

Goal: take a user who just ran ``pip install crawlhub-x.y.z.whl`` and get
them to a "ready to submit tasks" state with a single command.

Steps performed (in order):
  1. Create ``~/.crawlhub/`` directory tree (data root + subdirs).
  2. Generate ``config.yaml`` with user-chosen host/port (or defaults).
  3. Install Playwright Chromium (~150MB) — unless ``--skip-chromium``.
  4. Touch SQLite state store so the file is created with the right schema.
  5. Discover platform plugins and print a short summary.

The command is *idempotent*: re-running it on an already-initialized
machine is safe — existing config/data is preserved unless ``--force``
is passed.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import click


# ──────────────────────────────────────────────────────────────────────
#  CLI entry
# ──────────────────────────────────────────────────────────────────────

@click.command(name="init")
@click.option(
    "--host",
    default=None,
    help="Daemon bind host written to config.yaml (default: 127.0.0.1, "
         "or keep existing value if config already exists).",
)
@click.option(
    "--port",
    default=None,
    type=int,
    help="Daemon bind port written to config.yaml (default: 8787, "
         "or keep existing value if config already exists).",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Overwrite existing config.yaml with the new host/port. "
         "Without --force, an existing config is preserved.",
)
@click.option(
    "--skip-chromium",
    is_flag=True,
    default=False,
    help="Skip the Playwright Chromium download (~150MB). "
         "Useful in air-gapped environments or when chromium is already installed.",
)
def init(host: str | None, port: int | None, force: bool, skip_chromium: bool) -> None:
    """Initialize CrawlHub environment (one-shot, idempotent).

    Run this once after ``pip install``. It creates ``~/.crawlhub/``,
    generates ``config.yaml``, installs the Chromium browser used by
    Playwright, and verifies that all platform plugins load.

    Examples:

      crawlhub init                       # use defaults
      crawlhub init --port 9999           # bind daemon to port 9999
      crawlhub init --port 9999 --force   # overwrite existing config
      crawlhub init --skip-chromium       # skip browser download
    """
    click.echo("CrawlHub environment bootstrap")
    click.echo("=" * 50)

    total_steps = 7
    failures: list[str] = []

    # -- Step 1: data directories ------------------------------------
    step = 1
    click.echo(f"[{step}/{total_steps}] Create data directories ...", nl=False)
    try:
        from crawlhub.core.config import ensure_directories, get_data_root
        ensure_directories()
        click.echo(f" OK ({get_data_root()})")
    except Exception as exc:  # noqa: BLE001
        click.echo(f" FAIL ({exc})")
        failures.append(f"Step {step}: {exc}")

    # -- Step 2: config.yaml -----------------------------------------
    step = 2
    click.echo(f"[{step}/{total_steps}] Generate config.yaml ...", nl=False)
    try:
        msg = _setup_config(host=host, port=port, force=force)
        click.echo(f" OK ({msg})")
    except Exception as exc:  # noqa: BLE001
        click.echo(f" FAIL ({exc})")
        failures.append(f"Step {step}: {exc}")

    # -- Step 3: Playwright Chromium ---------------------------------
    step = 3
    if skip_chromium:
        click.echo(f"[{step}/{total_steps}] Install Playwright Chromium ... SKIPPED (--skip-chromium)")
    else:
        click.echo(f"[{step}/{total_steps}] Install Playwright Chromium (~150MB, first run only) ...")
        ok, detail = _install_chromium()
        if ok:
            click.echo(f"        -> OK ({detail})")
        else:
            click.echo(f"        -> FAIL ({detail})")
            failures.append(
                f"Step {step}: chromium install failed. "
                f"Run manually: {sys.executable} -m playwright install chromium"
            )

    # -- Step 4: Browser stealth assets ------------------------------
    step = 4
    click.echo(f"[{step}/{total_steps}] Verify browser stealth assets ...", nl=False)
    try:
        stealth_msg = _verify_stealth_assets()
        click.echo(f" OK ({stealth_msg})")
    except Exception as exc:  # noqa: BLE001
        click.echo(f" WARN ({exc})")
        # Not a hard failure — the daemon can still start, but stealth is degraded.
        click.echo(f"        [!] Stealth may be degraded. Fix: check crawlhub/core/browser/_stealth/")

    # -- Step 5: SQLite state store ----------------------------------
    step = 5
    click.echo(f"[{step}/{total_steps}] Initialize SQLite state store ...", nl=False)
    try:
        store_path = _init_sqlite_store()
        click.echo(f" OK ({store_path.name})")
    except Exception as exc:  # noqa: BLE001
        click.echo(f" FAIL ({exc})")
        failures.append(f"Step {step}: {exc}")

    # -- Step 6: platform discovery ----------------------------------
    step = 6
    click.echo(f"[{step}/{total_steps}] Discover platform plugins ...", nl=False)
    try:
        platforms = _discover_platforms()
        click.echo(f" OK ({len(platforms)} platforms: {', '.join(platforms)})")
    except Exception as exc:  # noqa: BLE001
        click.echo(f" FAIL ({exc})")
        failures.append(f"Step {step}: {exc}")

    # -- Step 7: write version stamp ----------------------------------
    step = 7
    total_steps = 7
    click.echo(f"[{step}/{total_steps}] Record version stamp ...", nl=False)
    try:
        _write_version_stamp()
        from crawlhub._version import __version__
        click.echo(f" OK (v{__version__})")
    except Exception as exc:  # noqa: BLE001
        click.echo(f" WARN ({exc})")  # non-critical

    # -- Summary -----------------------------------------------------
    click.echo("=" * 50)
    if failures:
        click.echo(f"[!] init completed with {len(failures)} issue(s):")
        for f in failures:
            click.echo(f"    - {f}")
        click.echo("")
        click.echo("Fix the above issues, then re-run: crawlhub init")
        sys.exit(1)

    # Read back actual port so we show the user what's configured.
    try:
        from crawlhub.core.config import load_config
        # Force a re-read (load_config caches the singleton).
        import crawlhub.core.config as _cfg_mod
        _cfg_mod._config_instance = None
        cfg = load_config()
        bind_addr = f"{cfg.host}:{cfg.port}"
    except Exception:  # noqa: BLE001
        bind_addr = "127.0.0.1:8787"

    click.echo("[OK] CrawlHub is ready.")
    click.echo("")
    click.echo("Next steps:")
    click.echo(f"  crawlhub daemon start          # bind to {bind_addr}")
    click.echo("  crawlhub cookie add douyin     # add an account (browser will open)")
    click.echo("  crawlhub platform list         # see all platforms + actions")
    click.echo("")
    click.echo("Need to change host/port later? Edit ~/.crawlhub/config.yaml, "
               "or re-run: crawlhub init --port <NEW_PORT> --force")


# ──────────────────────────────────────────────────────────────────────
#  Internals
# ──────────────────────────────────────────────────────────────────────


def _write_version_stamp() -> None:
    """Write current version to ~/.crawlhub/last_version.json.

    This allows the daemon to detect version upgrades on next start.
    """
    import json as _json
    import time as _time
    from crawlhub._version import __version__
    from crawlhub.core.config import get_data_root

    version_file = get_data_root() / "last_version.json"
    version_file.write_text(
        _json.dumps({
            "version": __version__,
            "updated_at": _time.strftime("%Y-%m-%dT%H:%M:%S"),
        }, indent=2),
        encoding="utf-8",
    )

def _setup_config(host: str | None, port: int | None, force: bool) -> str:
    """Create or update config.yaml.

    - If config doesn't exist: write defaults (or user-supplied values).
    - If config exists and --force: overwrite host/port, preserve everything else.
    - If config exists and not --force: leave untouched.

    Returns a short status message describing what happened.
    """
    from crawlhub.core.config import (
        CrawlHubConfig,
        get_data_root,
        load_config,
        save_config,
    )

    config_path = get_data_root() / "config.yaml"

    if not config_path.exists():
        # Fresh install — write a brand new config.
        cfg = CrawlHubConfig()
        if host is not None:
            cfg.host = host
        if port is not None:
            cfg.port = port
        save_config(cfg)
        return f"created, bind={cfg.host}:{cfg.port}"

    # Config exists — only touch it if --force.
    existing = load_config()
    if not force:
        if host is not None or port is not None:
            return (
                f"already exists at {existing.host}:{existing.port} "
                f"(use --force to overwrite)"
            )
        return f"already exists, bind={existing.host}:{existing.port}"

    # --force path: update host/port, preserve other fields.
    if host is not None:
        existing.host = host
    if port is not None:
        existing.port = port
    save_config(existing)
    # Reset singleton so subsequent load_config() picks up the new values.
    import crawlhub.core.config as _cfg_mod
    _cfg_mod._config_instance = None
    return f"updated, bind={existing.host}:{existing.port}"


def _install_chromium() -> tuple[bool, str]:
    """Run ``python -m playwright install chromium``.

    Returns (success, detail_message). We stream output live so the user
    can see download progress instead of staring at a frozen terminal
    for 30+ seconds.
    """
    # First check: does Playwright import at all?
    try:
        import playwright  # noqa: F401
    except ImportError:
        return False, "playwright package not installed — `pip install playwright` first"

    cmd = [sys.executable, "-m", "playwright", "install", "chromium"]
    try:
        # We use Popen + live forwarding rather than capture_output so the
        # user sees the download progress bar.
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert proc.stdout is not None  # for type checker
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                click.echo(f"        {line}")
        rc = proc.wait()
        if rc != 0:
            return False, f"playwright install exited with code {rc}"
        return True, "chromium installed"
    except FileNotFoundError:
        return False, f"could not run {sys.executable} -m playwright"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def _init_sqlite_store() -> Path:
    """Touch the SQLite state store to verify schema can be created.

    SqliteStateStore lazily creates the file on first connection, so we
    instantiate then call ``initialize()`` which is the public schema
    setup method declared on the ``StateStore`` ABC.
    """
    from crawlhub.core.config import get_data_root
    from crawlhub.core.sqlite_store import SqliteStateStore

    db_path = get_data_root() / "state.db"
    store = SqliteStateStore(str(db_path))
    store.initialize()
    return db_path


def _discover_platforms() -> list[str]:
    """Run the same discovery the daemon does at startup."""
    from crawlhub.core.registry import discover_platforms, get_registry

    discover_platforms()
    reg = get_registry()
    # `get_registry()` returns a dict-like mapping platform name -> service class.
    return sorted(reg.keys())


def _verify_stealth_assets() -> str:
    """Verify that browser stealth assets are present and usable.

    Checks:
      1. stealth.min.js exists and is non-empty
      2. UA constant is not "HeadlessChrome"
      3. Launch args contain --disable-blink-features=AutomationControlled

    Returns a short status message.
    """
    from crawlhub.core.browser.playwright_runtime import (
        _STEALTH_JS_PATH,
        _STEALTH_LAUNCH_ARGS,
        _REAL_USER_AGENT,
    )

    issues: list[str] = []

    # 1. stealth.min.js
    if not _STEALTH_JS_PATH.exists():
        issues.append("stealth.min.js missing")
    else:
        size = _STEALTH_JS_PATH.stat().st_size
        if size < 1000:
            issues.append(f"stealth.min.js too small ({size}B)")

    # 2. UA must not contain HeadlessChrome
    if "HeadlessChrome" in _REAL_USER_AGENT:
        issues.append("user_agent still contains HeadlessChrome!")

    # 3. Automation flag disabled via ignore_default_args (not command-line flag)
    # --disable-blink-features=AutomationControlled 会触发 Chrome infobar，
    # 已从 _STEALTH_LAUNCH_ARGS 移除，改用 ignore_default_args 排除
    # --enable-automation 来实现同等效果。
    if "--disable-blink-features=AutomationControlled" in _STEALTH_LAUNCH_ARGS:
        issues.append(
            "--disable-blink-features=AutomationControlled still in launch args "
            "(will trigger Chrome infobar, should be in ignore_default_args instead)"
        )

    if issues:
        raise RuntimeError("; ".join(issues))

    size_kb = _STEALTH_JS_PATH.stat().st_size // 1024
    return f"stealth.min.js={size_kb}KB, UA=Chrome/147, anti-automation=on"
