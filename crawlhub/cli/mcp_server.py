"""CrawlHub MCP Server.

Exposes a single MCP tool `crawlhub_command` that accepts CLI arguments
as a JSON array (list of strings), bypassing shell quoting/escaping entirely.

This avoids all shell-specific issues (PowerShell vs bash, JSON string escaping)
by passing argv directly to Click without any intermediate string parsing.

Uses the official `mcp` Python SDK with stdio transport.

Concurrency model
-----------------
The MCP tool handler is `async` and dispatches each command to a fresh
``python -m crawlhub`` subprocess. This is critical because:

1. **Real concurrency over a single MCP session.**
   FastMCP runs handlers on an anyio event loop. A *sync* handler that does
   blocking I/O (e.g. the old in-process Click invocation that took 2-4s) would
   freeze the loop and serialize every concurrent ``mcp_call_tool`` request,
   causing later parallel calls to time out at the MCP client layer.
   ``async + create_subprocess_exec`` lets N parallel handlers truly run in
   parallel — the OS schedules the children, the event loop only awaits.

2. **Process isolation.**
   ``contextlib.redirect_stdout`` mutates the global ``sys.stdout``; running
   multiple Click commands concurrently in the same process produces interleaved
   garbage. A subprocess gets its own stdio, full stop.

3. **Per-call timeout under the MCP wire timeout.**
   We enforce 25s per command, well below typical 30s MCP client timeouts, so
   a single slow command can never silently exhaust the wire budget.

Cost: ~300ms cold-start per call (Python import). For commands that already
take ~1-3s end-to-end this is acceptable; for sub-second hot paths it is the
biggest single overhead, but parallelism wins overwhelmingly compensate.

Usage (in any MCP client config)::

    {
      "mcpServers": {
        "crawlhub": {
          "command": "crawlhub-mcp"
        }
      }
    }

Or manually:

    python -m crawlhub.cli.mcp_server
"""

# ════════════════════════════════════════════════════════════════════════════
#  R7 Observability — redundant safety net. cli/__init__.py is loaded by
#  Python import system before this file (since this file lives inside
#  crawlhub.cli package), so install_all() already ran. Re-call is idempotent.
#  Kept here in case someone runs this file directly (`python crawlhub/cli/mcp_server.py`).
# ════════════════════════════════════════════════════════════════════════════
from crawlhub.core.observability import install_all as _r7_install_all
_r7_install_all()

import asyncio
import os
import sys

from mcp.server.fastmcp import FastMCP

# ── MCP server instance ──────────────────────────────────────────────

mcp = FastMCP("CrawlHub")

# ── Constants ───────────────────────────────────────────────────────

# Per-command wall-clock timeout. MCP clients (CodeBuddy IDE, Claude Desktop)
# typically use ~30s; staying under that lets us return a structured timeout
# error instead of letting the wire-level timeout fire.
_COMMAND_TIMEOUT_SECONDS = 25.0

# ── Tool definition ─────────────────────────────────────────────────

@mcp.tool()
async def crawlhub_command(args: list[str]) -> dict:
    """Execute a CrawlHub CLI command by passing argv directly to Click.

    Each call spawns an isolated ``python -m crawlhub`` subprocess, so
    concurrent invocations from a single MCP client run in true parallel
    (no shared stdout, no event-loop blocking, no Click global-state
    interference).

    No shell involvement at any step — args are passed straight to the
    child process's argv, so quoting/escaping issues (PowerShell vs bash,
    JSON strings, etc.) are eliminated.

    Args:
        args: CLI arguments as a list of strings, **without** the leading
              "crawlhub". Example:
              ``["task", "submit", "batch", "steam", "get_game_detail",
                 "--item-key", "app_id"]``

    Returns:
        A dict with keys:
        - success (bool): True if the command exited with code 0.
        - exit_code (int): Process exit code (0 = success, 2 = usage error,
                           -1 = timed out, -2 = failed to spawn).
        - stdout (str): Captured stdout (UTF-8 decoded with replacement).
        - stderr (str): Captured stderr (UTF-8 decoded with replacement).
    """
    # Force the child to use UTF-8 on stdio regardless of host console codec.
    # crawlhub.cli.main already calls reconfigure(), but PYTHONIOENCODING is
    # belt-and-braces in case any sub-import prints before that runs.
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"

    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "crawlhub",
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
    except (OSError, ValueError) as exc:
        return {
            "success": False,
            "exit_code": -2,
            "stdout": "",
            "stderr": f"[ERR] Failed to spawn subprocess: {exc}\n",
        }

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=_COMMAND_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        # Best-effort cleanup so we don't leak a runaway child.
        try:
            proc.kill()
            await proc.wait()
        except ProcessLookupError:
            pass
        return {
            "success": False,
            "exit_code": -1,
            "stdout": "",
            "stderr": (
                f"[ERR] Command timed out after {_COMMAND_TIMEOUT_SECONDS:.0f}s. "
                f"Split into smaller batches or increase server-side timeout.\n"
            ),
        }

    exit_code = proc.returncode if proc.returncode is not None else 1
    return {
        "success": exit_code == 0,
        "exit_code": exit_code,
        "stdout": stdout_bytes.decode("utf-8", errors="replace"),
        "stderr": stderr_bytes.decode("utf-8", errors="replace"),
    }


# ── Entry point ──────────────────────────────────────────────────────

def main():
    """Run the MCP server over stdio."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
