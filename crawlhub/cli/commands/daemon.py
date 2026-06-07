"""Daemon management commands: start / stop."""

import json
import subprocess
import sys
import time

import click


@click.group()
def daemon():
    """Daemon management commands."""
    pass


@daemon.command()
@click.option("--host", default=None,
              help="Bind host (default: read from ~/.crawlhub/config.yaml, fallback 127.0.0.1)")
@click.option("--port", default=None, type=int,
              help="Bind port (default: read from ~/.crawlhub/config.yaml, fallback 8787)")
def start(host: str | None, port: int | None):
    """Start the CrawlHub Daemon server."""
    from crawlhub.core.daemon import start_daemon

    # 单一信源：未显式传 --host/--port 时，回退到 config.yaml；config 也没有时再用硬默认值
    if host is None or port is None:
        try:
            from crawlhub.core.config import get_config
            cfg = get_config()
            if host is None:
                host = cfg.host
            if port is None:
                port = cfg.port
        except Exception:
            pass
    if host is None:
        host = "127.0.0.1"
    if port is None:
        port = 8787

    start_daemon(host=host, port=port)


@daemon.command()
@click.option("--force", is_flag=True, default=False, help="Force kill the daemon process immediately.")
@click.option("-y", "--yes", is_flag=True, default=False, help="Confirm destructive action (required with --force).")
def stop(force: bool, yes: bool):
    """Stop the running CrawlHub Daemon."""
    import httpx

    from crawlhub.core.ensure_daemon import get_daemon_address

    if force:
        if not yes:
            click.echo(
                "[ERR] --force performs a hard kill (taskkill /F or SIGKILL) which "
                "may leave SQLite/workers in an inconsistent state; pass -y/--yes to confirm.",
                err=True,
            )
            sys.exit(2)
        _force_kill_daemon()
        return

    host, port = get_daemon_address()
    base_url = f"http://{host}:{port}"
    try:
        resp = httpx.post(f"{base_url}/api/shutdown", timeout=10)
        if resp.status_code == 200:
            click.echo("[OK] Daemon shutdown initiated.")
        else:
            click.echo(f"[ERR] Unexpected response: {resp.status_code}")
    except httpx.ConnectError:
        click.echo("[WARN] Daemon is not running.")


def _force_kill_daemon():
    """Force kill the daemon process by PID file or process scanning.

    On Windows, SIGTERM is intercepted by Python's signal handler and triggers
    graceful_shutdown() — which can hang if SQLite is locked or workers stuck.
    --force is meant to be a hard kill, so we use taskkill /F (Windows) or
    SIGKILL (POSIX) which the process cannot trap.
    """
    import os
    import signal

    from crawlhub.core.config import get_data_root

    is_windows = sys.platform == "win32"

    def _hard_kill(pid: int) -> bool:
        try:
            if is_windows:
                result = subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    capture_output=True, text=True, timeout=10,
                )
                return result.returncode == 0
            else:
                os.kill(pid, signal.SIGKILL)
                return True
        except (ProcessLookupError, PermissionError):
            return False
        except Exception as e:
            click.echo(f"[WARN] Hard kill of PID {pid} failed: {e}")
            return False

    data_root = get_data_root()
    pid_path = data_root / "daemon.pid"
    killed = False

    if pid_path.exists():
        try:
            with open(pid_path, "r") as f:
                pid_data = json.load(f)
            pid = pid_data.get("pid") if isinstance(pid_data, dict) else int(pid_data)
            if pid and _hard_kill(pid):
                click.echo(f"[OK] Force killed daemon (PID {pid}) via PID file.")
                killed = True
            elif pid:
                click.echo(f"[WARN] PID {pid} from PID file not running.")
        except Exception as e:
            click.echo(f"[WARN] Failed to read PID file: {e}")
        pid_path.unlink(missing_ok=True)

    if not killed:
        try:
            result = subprocess.run(
                ["wmic", "process", "where",
                 "commandline like '%crawlhub%serve%'",
                 "get", "processid"],
                capture_output=True, text=True, timeout=10
            )
            pids_found = []
            for line in result.stdout.strip().splitlines():
                line = line.strip()
                if line.isdigit():
                    pids_found.append(int(line))

            if pids_found:
                for pid in pids_found:
                    if _hard_kill(pid):
                        click.echo(f"[OK] Force killed daemon process (PID {pid}).")
                        killed = True
            else:
                click.echo("[WARN] No daemon process found.")
        except Exception as e:
            click.echo(f"[ERR] Process scan failed: {e}")

    if killed:
        exit_marker = data_root / "exit_marker.json"
        try:
            with open(exit_marker, "w", encoding="utf-8") as f:
                json.dump({
                    "clean": True,
                    "exited_at": time.time(),
                    "reason": "force_killed_via_cli",
                }, f)
        except OSError:
            pass


@daemon.command("log")
@click.option("--tail", "-n", default=200, type=click.IntRange(1, 5000),
              help="Number of lines from the end (default: 200, max: 5000)")
@click.option("--since", default=None,
              help="Only show lines after this time (ISO: 2026-05-21T12:00:00 or date-only: 2026-05-21)")
@click.option("--json", "json_output", is_flag=True, help="Output raw JSON")
@click.pass_context
def daemon_log(ctx, tail: int, since: str, json_output: bool):
    """Show daemon log tail.

    Examples:

        crawlhub daemon log                        # last 200 lines

        crawlhub daemon log -n 500                 # last 500 lines

        crawlhub daemon log --since 2026-05-20T08:00:00

        crawlhub daemon log --since 2026-05-20     # all lines since midnight
    """
    import httpx

    from crawlhub.cli._utils import ensure_daemon, get_base_url

    ensure_daemon()
    base_url = get_base_url()
    json_out = json_output or (ctx.obj and ctx.obj.get("json_output"))

    params = {"tail": tail}
    if since:
        params["since"] = since

    resp = httpx.get(
        f"{base_url}/api/logs/daemon",
        params=params,
        timeout=10,
    )
    if resp.status_code != 200:
        click.echo(f"[ERR] {resp.status_code}: {resp.text}")
        return

    data = resp.json()
    if json_out:
        click.echo(json.dumps(data, indent=2, ensure_ascii=False))
        return

    lines = data.get("lines", [])
    total = data.get("total_lines", 0)

    if not lines:
        click.echo("[INFO] No daemon logs found.")
        return

    click.echo(f"\n[Daemon Logs] (showing {len(lines)} / {total} total lines)")
    click.echo("-" * 60)
    for line in lines:
        click.echo(line)
    click.echo("")


@daemon.command()
@click.option("--host", default=None,
              help="Bind host for the new daemon (default: read from ~/.crawlhub/config.yaml).")
@click.option("--port", default=None, type=int,
              help="Bind port for the new daemon (default: read from ~/.crawlhub/config.yaml).")
@click.option("--force", is_flag=True, default=False,
              help="Use hard kill (taskkill /F or SIGKILL) instead of graceful shutdown.")
@click.option("-y", "--yes", is_flag=True, default=False,
              help="Confirm --force (required when --force is set).")
@click.option("--wait-stop", default=8.0, type=float,
              help="Seconds to wait for the old daemon to release the port (default 8s).")
def restart(host: str | None, port: int | None, force: bool, yes: bool, wait_stop: float):
    """Stop the running daemon (if any) and start a fresh one.

    Why this exists
    ---------------
    Daemon code (anything under crawlhub/api/, crawlhub/core/, etc.) does NOT
    auto-reload — uvicorn is launched without ``--reload``. So after editing
    server-side code, you must restart the daemon for the change to take
    effect. Doing it as ``stop`` + ``start`` manually is error-prone
    (race on the port, forget to wait, accidentally double-start).

    Behavior
    --------
    1. POST /api/shutdown for graceful exit; falls back to hard kill if
       --force is given (requires -y).
    2. Wait up to --wait-stop seconds for the port to be released.
    3. Spawn a *detached* daemon process (so it survives this CLI exit).
    """
    import socket

    import httpx

    from crawlhub.core.ensure_daemon import get_daemon_address

    # 单一信源：未显式传 --host/--port 时，回退到 config.yaml；config 也没有时再用硬默认值
    if host is None or port is None:
        try:
            from crawlhub.core.config import get_config
            cfg = get_config()
            if host is None:
                host = cfg.host
            if port is None:
                port = cfg.port
        except Exception:
            pass
    if host is None:
        host = "127.0.0.1"
    if port is None:
        port = 8787

    if force and not yes:
        click.echo(
            "[ERR] --force performs a hard kill (taskkill /F or SIGKILL) which "
            "may leave SQLite/workers in an inconsistent state; pass -y/--yes to confirm.",
            err=True,
        )
        sys.exit(2)

    # -- Step 1: stop the running daemon (if any) ----------------------
    cur_host, cur_port = get_daemon_address()
    base_url = f"http://{cur_host}:{cur_port}"
    daemon_was_running = False

    if force:
        # Hard kill path: skip the HTTP shutdown, go straight to taskkill / SIGKILL.
        click.echo("[INFO] Force-killing daemon...")
        _force_kill_daemon()
        daemon_was_running = True
    else:
        try:
            resp = httpx.post(f"{base_url}/api/shutdown", timeout=10)
            if resp.status_code == 200:
                click.echo("[OK] Old daemon shutdown initiated.")
                daemon_was_running = True
            else:
                click.echo(f"[WARN] Unexpected shutdown response: {resp.status_code}")
                daemon_was_running = True
        except httpx.ConnectError:
            click.echo("[INFO] No daemon running; starting fresh.")
        except Exception as e:
            click.echo(f"[WARN] Shutdown call failed: {e}; trying anyway.")
            daemon_was_running = True

    # -- Step 2: wait for the target port to be free -------------------
    if daemon_was_running:
        deadline = time.time() + wait_stop
        port_free = False
        while time.time() < deadline:
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.settimeout(0.5)
                    if s.connect_ex((host, port)) != 0:
                        port_free = True
                        break
            except OSError:
                port_free = True
                break
            time.sleep(0.3)

        if not port_free:
            click.echo(
                f"[ERR] Port {host}:{port} is still in use after {wait_stop:.0f}s. "
                "Try: crawlhub daemon stop --force -y, then crawlhub daemon start.",
                err=True,
            )
            sys.exit(1)

    # -- Step 3: spawn a detached new daemon ---------------------------
    # We deliberately do NOT call start_daemon() inline: that would block
    # this CLI process. Instead spawn `crawlhub daemon start` detached.
    #
    # Spawn strategy:
    # - Prefer the installed `crawlhub` (or `crawlhub.exe`) entry point so
    #   we don't depend on `-m crawlhub.cli` being importable in whatever
    #   working directory we were invoked from.
    # - Fall back to `python -m crawlhub.cli` if the entry point is missing.
    import shutil

    crawlhub_exe = shutil.which("crawlhub")
    if crawlhub_exe:
        cmd = [crawlhub_exe, "daemon", "start", "--host", host, "--port", str(port)]
    else:
        cmd = [sys.executable, "-m", "crawlhub.cli", "daemon", "start",
               "--host", host, "--port", str(port)]

    creationflags = 0
    start_new_session = False
    if sys.platform == "win32":
        # CREATE_NO_WINDOW: no console window flashes up.
        # CREATE_NEW_PROCESS_GROUP: detach from this CLI's Ctrl+C signals.
        # (Avoid DETACHED_PROCESS — it interacts badly with miniforge/venv
        # launchers on some Windows setups and can cause silent spawn fails.)
        creationflags = 0x08000000 | 0x00000200
    else:
        start_new_session = True

    try:
        subprocess.Popen(
            cmd,
            creationflags=creationflags,
            start_new_session=start_new_session,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=False if sys.platform == "win32" else True,
        )
    except Exception as e:
        click.echo(f"[ERR] Failed to spawn new daemon: {e}", err=True)
        sys.exit(1)

    # -- Step 4: wait for the new daemon to come up --------------------
    # Daemon import/init can take 10-25s on a cold start (loading SQLite,
    # registering 50+ routes, importing all platform service modules).
    deadline = time.time() + 30.0
    while time.time() < deadline:
        try:
            r = httpx.get(f"http://{host}:{port}/health", timeout=2)
            if r.status_code == 200:
                click.echo(f"[OK] Daemon restarted on {host}:{port}.")
                return
        except Exception:
            pass
        time.sleep(0.5)

    click.echo(
        f"[WARN] New daemon was spawned but did not respond on {host}:{port} within 30s. "
        f"Run `crawlhub daemon start --host {host} --port {port}` manually to see startup errors.",
        err=True,
    )
    sys.exit(1)
