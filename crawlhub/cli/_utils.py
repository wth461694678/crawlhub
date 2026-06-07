"""Shared utilities for CLI commands.

Provides session state management and common helpers used across all command modules.
"""

import click

# ── Session State ─────────────────────────────────────────────────────
# Single source of truth for daemon connection info.
# Defaults are loaded from ~/.crawlhub/config.yaml (lazy on first read).
# Overridden by main group options (--host / --port) before any subcommand runs.


def _load_defaults() -> dict:
    """Read host/port defaults from ~/.crawlhub/config.yaml. Falls back to 127.0.0.1:8787 if unavailable."""
    try:
        from crawlhub.core.config import get_config
        cfg = get_config()
        return {"daemon_host": cfg.host, "daemon_port": cfg.port}
    except Exception:
        return {"daemon_host": "127.0.0.1", "daemon_port": 8787}


_session_state: dict = {
    **_load_defaults(),
    "verbose": False,
}


def get_base_url() -> str:
    """Return the daemon base URL from current session state."""
    return f"http://{_session_state['daemon_host']}:{_session_state['daemon_port']}"


def update_session(host: str | None = None, port: int | None = None, verbose: bool = False) -> None:
    """Update session state from CLI global options."""
    if host:
        _session_state["daemon_host"] = host
    if port:
        _session_state["daemon_port"] = port
    _session_state["verbose"] = verbose


def ensure_daemon() -> None:
    """Ensure daemon is running before commands that need it."""
    from crawlhub.core.ensure_daemon import ensure_daemon_running

    try:
        ensure_daemon_running()
    except RuntimeError as e:
        click.echo(f"[ERR] {e}")
        raise SystemExit(1)
