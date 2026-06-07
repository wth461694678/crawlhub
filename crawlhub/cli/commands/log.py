"""Log viewing commands (DEPRECATED — use `daemon log` and `task output log`)."""

import json

import click


@click.group("log", hidden=True)
def log():
    """View logs (task / daemon). DEPRECATED: use `daemon log` and `task output log`."""
    pass


# ── log task (DEPRECATED) ──────────────────────────────────────────

@log.command("task", hidden=True)
@click.argument("task_id")
@click.option("--tail", "-n", default=200, type=click.IntRange(1, 5000),
              help="Number of lines from the end (default: 200, max: 5000)")
@click.option("--since", default=None,
              help="Only show lines after this time (ISO: 2026-05-21T12:00:00 or date-only: 2026-05-21)")
@click.option("--json", "json_output", is_flag=True, help="Output raw JSON")
@click.pass_context
def log_task(ctx, task_id: str, tail: int, since: str, json_output: bool):
    """[DEPRECATED] Use `crawlhub task output log <task_id>` instead."""
    click.echo(
        "[WARN] `log task` is deprecated; use `crawlhub task output log <task_id>` instead.",
        err=True,
    )
    from crawlhub.cli.commands.task import task_output_group
    argv = ["log", task_id]
    if tail != 200:
        argv += ["-n", str(tail)]
    if since:
        argv += ["--since", since]
    if json_output:
        argv.append("--json")
    ctx_obj = ctx.ensure_object(dict)
    try:
        task_output_group(args=argv, obj=ctx_obj, standalone_mode=False)
    except SystemExit:
        pass


# ── log daemon (DEPRECATED) ────────────────────────────────────────

@log.command("daemon", hidden=True)
@click.option("--tail", "-n", default=200, type=click.IntRange(1, 5000),
              help="Number of lines from the end (default: 200, max: 5000)")
@click.option("--since", default=None,
              help="Only show lines after this time (ISO: 2026-05-21T12:00:00 or date-only: 2026-05-21)")
@click.option("--json", "json_output", is_flag=True, help="Output raw JSON")
@click.pass_context
def log_daemon(ctx, tail: int, since: str, json_output: bool):
    """[DEPRECATED] Use `crawlhub daemon log` instead."""
    click.echo(
        "[WARN] `log daemon` is deprecated; use `crawlhub daemon log` instead.",
        err=True,
    )
    from crawlhub.cli.commands.daemon import daemon
    argv = ["log"]
    if tail != 200:
        argv += ["-n", str(tail)]
    if since:
        argv += ["--since", since]
    if json_output:
        argv.append("--json")
    ctx_obj = ctx.ensure_object(dict)
    try:
        daemon(args=argv, obj=ctx_obj, standalone_mode=False)
    except SystemExit:
        pass
