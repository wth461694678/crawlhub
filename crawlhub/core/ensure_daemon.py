"""Daemon auto-spawn: detect, launch, and wait for CrawlHub Daemon.

This module provides the core logic for automatically starting the Daemon
when it is not running. It is used by both the MCP server and CLI commands.
"""

from __future__ import annotations

import logging
import os
import platform
import subprocess
import sys
import time
from pathlib import Path

import httpx

from crawlhub.core.config import get_data_root, load_config

logger = logging.getLogger(__name__)

# Constants
_HEALTH_TIMEOUT = 0.5  # seconds for health check request (localhost, fast fail)
_SPAWN_WAIT_TIMEOUT = 15.0  # max seconds to wait for daemon startup
_SPAWN_POLL_INTERVAL = 0.3  # seconds between health polls
_LOCK_TIMEOUT = 20.0  # seconds to wait for file lock acquisition


def get_daemon_address() -> tuple[str, int]:
    """Get daemon host and port from environment or config.

    Priority:
      1. Environment variables CRAWLHUB_HOST / CRAWLHUB_PORT
      2. ~/.crawlhub/config.yaml
      3. Defaults: 127.0.0.1:8787
    """
    host = os.environ.get("CRAWLHUB_HOST")
    port_str = os.environ.get("CRAWLHUB_PORT")

    if host and port_str:
        try:
            return host, int(port_str)
        except ValueError:
            pass

    # Fall back to config
    config = load_config()
    final_host = host or config.host
    final_port = int(port_str) if port_str else config.port
    return final_host, final_port


def is_daemon_alive(host: str, port: int) -> bool:
    """Check if the Daemon is alive by hitting /health endpoint.

    Returns True if HTTP 200, False on ConnectError/ConnectTimeout.
    """
    url = f"http://{host}:{port}/health"
    try:
        resp = httpx.get(url, timeout=_HEALTH_TIMEOUT)
        return resp.status_code == 200
    except (httpx.ConnectError, httpx.ConnectTimeout):
        return False
    except Exception:
        return False


def spawn_daemon(host: str, port: int) -> None:
    """Start the Daemon as a fully detached background process.

    stdout/stderr are redirected to ~/.crawlhub/logs/daemon.log (append mode).
    The spawned process is completely decoupled from the caller.
    """
    # Ensure log directory exists
    log_dir = get_data_root() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "daemon.log"

    # Build command
    # Use sys.executable to find the Python interpreter, then call crawlhub daemon start
    # This works whether crawlhub is installed as a script or run via python -m
    cmd = [sys.executable, "-m", "crawlhub", "daemon", "start", "--host", host, "--port", str(port)]

    # Open log file in append mode
    log_handle = open(log_file, "a", encoding="utf-8")

    # Platform-specific detach
    kwargs: dict = {
        "stdout": log_handle,
        "stderr": log_handle,
        "stdin": subprocess.DEVNULL,
    }

    if platform.system() == "Windows":
        # On Windows, use CREATE_NEW_PROCESS_GROUP + DETACHED_PROCESS
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        DETACHED_PROCESS = 0x00000008
        kwargs["creationflags"] = CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS
    else:
        # On POSIX, start_new_session detaches from parent
        kwargs["start_new_session"] = True

    logger.info("Spawning daemon: %s", " ".join(cmd))
    subprocess.Popen(cmd, **kwargs)
    # Note: we intentionally do NOT close log_handle here;
    # the child process inherits it and keeps writing.


def wait_for_daemon(host: str, port: int, timeout: float = _SPAWN_WAIT_TIMEOUT, interval: float = _SPAWN_POLL_INTERVAL) -> bool:
    """Poll /health until the Daemon is ready or timeout is reached.

    Returns True if daemon became ready, False on timeout.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_daemon_alive(host, port):
            return True
        time.sleep(interval)
    return False


def ensure_daemon_running() -> None:
    """Main entry point: ensure the Daemon is running.

    Logic:
      1. Quick health check - if alive, return immediately.
      2. Acquire file lock (~/.crawlhub/daemon.lock).
         - If lock acquired: double-check health, spawn if needed, wait.
         - If lock not acquired (timeout): just wait for health (another process is spawning).
      3. Raise RuntimeError if daemon fails to start within timeout.
    """
    t0 = time.monotonic()
    host, port = get_daemon_address()
    logger.info("[ensure_daemon] target=%s:%d", host, port)

    # Fast path: daemon already running
    if is_daemon_alive(host, port):
        logger.info("[ensure_daemon] daemon already alive (%.1fs)", time.monotonic() - t0)
        return

    logger.info("[ensure_daemon] daemon not alive, attempting spawn... (%.1fs)", time.monotonic() - t0)

    # Need to spawn - use file lock for concurrency safety
    lock_path = get_data_root() / "daemon.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    lock_acquired = False
    lock_fd = None

    try:
        lock_fd = _try_acquire_lock(lock_path)
        lock_acquired = lock_fd is not None
        logger.info("[ensure_daemon] lock acquired=%s (%.1fs)", lock_acquired, time.monotonic() - t0)

        if lock_acquired:
            # Double-check after acquiring lock (another process may have spawned)
            if is_daemon_alive(host, port):
                logger.info("[ensure_daemon] daemon came alive during lock wait (%.1fs)", time.monotonic() - t0)
                return

            # Spawn daemon
            logger.info("[ensure_daemon] spawning daemon process... (%.1fs)", time.monotonic() - t0)
            spawn_daemon(host, port)
            logger.info("[ensure_daemon] spawn_daemon() returned (%.1fs)", time.monotonic() - t0)

        # Wait for daemon to become ready (whether we spawned or another process did)
        logger.info("[ensure_daemon] waiting for daemon health (timeout=%.0fs)... (%.1fs)", _SPAWN_WAIT_TIMEOUT, time.monotonic() - t0)
        if not wait_for_daemon(host, port):
            elapsed = time.monotonic() - t0
            log_path = get_data_root() / 'logs' / 'daemon.log'
            logger.error(
                "[ensure_daemon] FAILED - daemon did not become healthy within %.0fs (%.1fs total). "
                "Check: %s",
                _SPAWN_WAIT_TIMEOUT, elapsed, log_path,
            )
            raise RuntimeError(
                f"Failed to start CrawlHub Daemon within {_SPAWN_WAIT_TIMEOUT:.0f}s. "
                f"Check logs at: {log_path}"
            )

        logger.info("[ensure_daemon] SUCCESS - daemon is healthy (%.1fs total)", time.monotonic() - t0)

    finally:
        if lock_acquired and lock_fd is not None:
            _release_lock(lock_fd, lock_path)


def _try_acquire_lock(lock_path: Path) -> object | None:
    """Try to acquire a file lock. Returns the lock fd/handle or None if failed.

    Uses OS-level file locking (fcntl on POSIX, msvcrt on Windows).
    Non-blocking: returns None immediately if lock is held by another process.
    """
    try:
        if platform.system() == "Windows":
            import msvcrt
            fd = open(lock_path, "w")
            msvcrt.locking(fd.fileno(), msvcrt.LK_NBLCK, 1)
            return fd
        else:
            import fcntl
            fd = open(lock_path, "w")
            fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
    except (OSError, IOError):
        return None


def _release_lock(fd: object, lock_path: Path) -> None:
    """Release the file lock."""
    try:
        if platform.system() == "Windows":
            import msvcrt
            msvcrt.locking(fd.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
        fd.close()
    except Exception:
        pass
