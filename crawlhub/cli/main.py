"""CrawlHub CLI entry point.

Provides subcommands: platform, cookie, task, plan, favorite, throttle, notify,
                     daemon, status.

Platform subcommands: list.

Daemon subcommands: start, stop.
Task subcommands: submit single, submit batch, schema, get, list, retry, cancel, delete, note,
                  force-complete, force-start, summary, lineage, export.
Favorite subcommands: list, get, create, delete, use, save-from, note.
Cookie subcommands: list, get, add, add-raw, refresh, probe, delete, note, history.
Plan subcommands: group <list|get|create|edit|delete>,
                  job <list|get|create|update|enable|disable|delete|run|preview|runs>.
Notify subcommands: channel <list|add|delete>, rule <list|add|delete>, test.

Run without subcommand to enter interactive REPL mode.
"""

import json
import sys
from typing import Optional

# Force UTF-8 on stdout/stderr so Chinese characters in API responses (e.g.
# action descriptions, error messages) render correctly on Windows consoles
# whose default codec is GBK/cp936. Python 3.7+ guarantees `reconfigure`
# on the underlying TextIOWrapper.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        # Detached/wrapped streams: fall back silently — at worst we get the
        # same GBK behavior as before, never a crash.
        pass

import click

from crawlhub.cli._utils import update_session
from crawlhub.cli.commands.cookie import cookie
from crawlhub.cli.commands.daemon import daemon
from crawlhub.cli.commands.favorite import favorite
from crawlhub.cli.commands.init import init
from crawlhub.cli.commands.log import log
from crawlhub.cli.commands.misc import cleanup, status, test
from crawlhub.cli.commands.notify import notify
from crawlhub.cli.commands.plan import plan
from crawlhub.cli.commands.platform import platform
from crawlhub.cli.commands.stealth import stealth
from crawlhub.cli.commands.task import task
from crawlhub.cli.commands.throttle import throttle


# ── REPL ──────────────────────────────────────────────────────────────

def _start_repl(json_output: bool = False) -> None:
    """Start interactive REPL mode."""
    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
        from prompt_toolkit.formatted_text import HTML
        from prompt_toolkit.history import InMemoryHistory

        session = PromptSession(
            history=InMemoryHistory(),
            auto_suggest=AutoSuggestFromHistory(),
        )
        prompt_text = HTML("<ansigreen><b>crawlhub></b></ansigreen> ")
    except ImportError:
        session = None
        prompt_text = "crawlhub> "

    click.echo("CrawlHub - Interactive CLI")
    click.echo("Type 'help' for available commands, 'exit' to quit.")
    click.echo(f"JSON output: {'ON' if json_output else 'OFF'}")
    click.echo("")

    while True:
        try:
            if session:
                user_input = session.prompt(prompt_text).strip()
            else:
                user_input = input(prompt_text).strip()

            if not user_input:
                continue

            if user_input in ("exit", "quit"):
                click.echo("Goodbye!")
                break
            elif user_input == "help":
                _print_repl_help()
            elif user_input == "json":
                json_output = True
                click.echo("JSON output: ON")
            elif user_input == "no-json":
                json_output = False
                click.echo("JSON output: OFF")
            else:
                # Delegate to Click command parser
                import shlex
                try:
                    args = shlex.split(user_input)
                    # Build ctx obj for json_output
                    standalone_mode = False
                    ctx_obj = {"json_output": json_output}
                    main.main(args=args, obj=ctx_obj, standalone_mode=standalone_mode)
                except SystemExit:
                    pass
                except Exception as e:
                    click.echo(f"[ERR] {e}")

        except KeyboardInterrupt:
            click.echo("")
            continue
        except EOFError:
            click.echo("\nGoodbye!")
            break


def _print_repl_help() -> None:
    click.echo("""
Available Commands:
  help                                    Show this help
  init [--port N] [--host H] [--force]    One-shot environment bootstrap
  platform list                           List platforms and their actions
  cookie list [-p platform] [-s]          List cookies (all or one platform)
  cookie get <platform> <label>           Show cookie detail
  cookie add <platform>                   Add new account via browser
  cookie add-raw <platform> [-r|-f]       Add cookie from string/file
  cookie refresh <platform> -l <label>    Re-login a single cookie
  cookie probe [PLATFORMS...|--all] [-y]  Probe cookie validity
  cookie delete <p> <label>... [-y]       Delete cookies (no recycle bin)
  cookie note <p> <label> [--set|--clear] View/update note
  cookie history <p> <label>              Show probe history
  task list [-p] [-s] [-q] [--parent]     List tasks
  task submit single <platform> <action>  Submit single task
  task submit batch <platform> <action>   Submit batch task
  task schema <platform> <action>         Show JSON schema (params) for an action
  task get <id> [--with-parent/children/lineage]  Task details
  task retry <id> [--all|--failed]        Retry task
  task cancel <id>                        Cancel task
  task delete <id> [--force]              Delete task
  task note <id> [--set|--clear]          View/update note
  task force-complete <id>                Force complete
  task force-start <id>                   Force start
  task summary <id>                       Batch progress
  task lineage <id>                       Dependency tree
  task export <id> [-f] [-o]              Export results
  plan group list                         List plan groups
  plan group get <gid>                    Show group + plans
  plan group create --name X [--note Y]   Create plan group
  plan group edit <gid> [--name|--note]   Update group
  plan group delete <gid>... [-y]         Delete groups
  plan job list [--group GID]             List plans
  plan job get <plan_id>                  Show plan detail
  plan job create -f SPEC.json            Create from JSON spec
  plan job update <pid> -f SPEC.json      Replace plan (PUT)
  plan job enable <pid>                   Enable plan
  plan job disable <pid>                  Disable plan
  plan job delete <pid>... [-y]           Delete plans
  plan job run <pid> [--time ISO]         Manual fire
  plan job preview <pid> [--time ISO]     Preview rendered steps
  plan job runs <pid> [--status]          List tasks fired by plan
  favorite list [--platform] [-s]         List favorites
  favorite get <fav_id>                   Show favorite detail
  favorite create --platform --action     Create favorite (-d / -f for params)
  favorite delete <fav_id>... [filters]   Delete favorites (no recycle bin)
  favorite use <fav_id>                   Submit a task from favorite
  favorite save-from <task_id>            Save task params as a new favorite
  favorite note <fav_id> [--set|--clear]  View/update favorite note
  throttle list                            List throttle configs (all platforms)
  throttle get <platform>                  Show throttle config + cookie states
  throttle update <platform> [opts]       Hot-update throttle config
  notify channel list                     List notification channels
  notify channel add <name> <url>         Add/update a channel webhook
  notify channel delete <name>... [-y]    Delete channels
  notify rule list                        List notification rules
  notify rule add <event> <channel>       Add a rule (event -> channel)
  notify rule delete <rule_id>... [-y]    Delete rules
  notify test [--channel]                 Send test message to channel
  task output log <id> [-l] [--export]      View task log (from ~/.crawlhub/logs/tasks/)
  task output data <id> [-l] [--export]     View task data (data.jsonl)
  task output requests <id> [-l] [--export] View task requests (requests.jsonl)
  task output summary <id> [-l] [--export]  View task summary (summary.json)
  task output error <id> [-l] [--export]    View task error (error.json, if any)
  daemon log [-n] [--since]                 Show daemon log (with time filter)
  daemon start [--host] [--port]          Start daemon
  daemon stop [--force]                   Stop daemon
  status                                  Check daemon health
  json                                    Enable JSON output
  no-json                                 Disable JSON output
  exit / quit                             Exit REPL
""")


# ── Main CLI Group ────────────────────────────────────────────────────

@click.group(invoke_without_command=True)
@click.version_option(package_name="crawlhub")
@click.option("--host", default=None, help="Daemon host (default: 127.0.0.1)")
@click.option("--port", default=None, type=int, help="Daemon port (default: 8787)")
@click.option("--json", "json_output", is_flag=True, help="Output in JSON format")
@click.option("-v", "--verbose", is_flag=True, help="Verbose output")
@click.pass_context
def main(ctx, host: Optional[str], port: Optional[int], json_output: bool, verbose: bool):
    """CrawlHub - Unified Crawler Platform CLI.

    Run without subcommand to enter interactive REPL mode.
    """
    # Update shared session state so all subcommands use the right daemon address
    update_session(host=host, port=port, verbose=verbose)

    ctx.ensure_object(dict)
    ctx.obj["json_output"] = json_output

    if ctx.invoked_subcommand is None:
        _start_repl(json_output)


# ── Custom help order ────────────────────────────────────────────────

# Desired order for `crawlhub --help` Commands section.
# Commands not listed here are appended alphabetically at the end.
_HELP_ORDER = [
    "init", "platform", "cookie", "task", "plan",
    "favorite", "throttle", "notify", "daemon", "status",
]


def _format_commands(self, ctx, formatter):
    """Override help output to respect _HELP_ORDER instead of alphabetical."""
    from click import Command

    commands = []
    for name in _HELP_ORDER:
        cmd = self.get_command(ctx, name)
        if cmd and not getattr(cmd, "hidden", False):
            commands.append((name, cmd))

    # Append any remaining (non-hidden) commands not in _HELP_ORDER
    for name, cmd in self.commands.items():
        if name not in _HELP_ORDER and not getattr(cmd, "hidden", False):
            commands.append((name, cmd))

    if not commands:
        return

    with formatter.section("Commands"):
        rows = []
        for name, cmd in commands:
            if isinstance(cmd, Command):
                # short_help may be None if help has no double-newline split
                help_text = cmd.short_help or ""
                if not help_text and cmd.help:
                    # Fallback: first paragraph of cmd.help
                    help_text = cmd.help.split("\n\n")[0].split("\n")[0][:80]
                rows.append((name, help_text))
            else:
                rows.append((name, ""))
        formatter.write_dl(rows)


main.format_commands = _format_commands.__get__(main, type(main))


# ── Register subcommands ──────────────────────────────────────────────

main.add_command(init)
main.add_command(platform)
main.add_command(cookie)
main.add_command(task)
main.add_command(plan)
main.add_command(favorite)
main.add_command(throttle)
main.add_command(stealth)
main.add_command(notify)
main.add_command(log)
main.add_command(daemon)
main.add_command(status)


if __name__ == "__main__":
    main()
