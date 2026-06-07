"""Miscellaneous commands: status, cleanup, test."""

import click

from crawlhub.cli._utils import ensure_daemon, get_base_url


# ── status ────────────────────────────────────────────────────────────

@click.command()
@click.pass_context
def status(ctx):
    """Check Daemon health status."""
    import httpx

    ensure_daemon()
    base_url = get_base_url()
    try:
        resp = httpx.get(f"{base_url}/health", timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            if ctx.obj and ctx.obj.get("json_output"):
                import json
                click.echo(json.dumps(data, indent=2, ensure_ascii=False))
            else:
                click.echo(f"[OK] Daemon running - uptime={data.get('uptime')}s, "
                           f"tasks_running={data.get('running_tasks')}, "
                           f"tasks_queued={data.get('queued_tasks')}")
        else:
            click.echo(f"[WARN] Unexpected: {resp.status_code}")
    except httpx.ConnectError:
        click.echo("[ERR] Daemon is not running.")


# ── cleanup ───────────────────────────────────────────────────────────

@click.command(hidden=True)
def cleanup():
    """Trigger manual retention cleanup."""
    import httpx

    ensure_daemon()
    base_url = get_base_url()
    resp = httpx.post(f"{base_url}/api/cleanup", timeout=60)
    if resp.status_code == 200:
        click.echo(f"[OK] Cleanup completed: {resp.json()}")
    else:
        click.echo(f"[ERR] {resp.status_code}: {resp.text}")


# ── test ──────────────────────────────────────────────────────────────

@click.group(hidden=True)
def test():
    """Run automated test suite against a running daemon."""
    pass


@test.command("run")
@click.option("--report", "report_path", default=None, type=click.Path(),
              help="Write Markdown test report to this path (e.g. ./test_report.md)")
@click.option("-s", "--sections", default=None,
              help="Comma-separated section numbers to run (e.g. '0,1,2' or '3-5,8'). "
                   "Run 'crawlhub test run --list-sections' to see all section numbers.")
def test_run(report_path: str | None, sections: str | None):
    """Run integration test suite.

    By default runs ALL sections. Use -s to run only selected sections.

    Examples:
        crawlhub test run
        crawlhub test run -s 0,1,2
        crawlhub test run -s 3-5,8,11
        crawlhub test run --report ./report.md -s 12
    """
    import sys

    from crawlhub.cli.test_runner import run_tests

    exit_code = run_tests(report_path=report_path, sections=sections)
    sys.exit(exit_code)


@test.command("list-sections")
def test_list_sections():
    """Print all test section numbers and titles."""
    from crawlhub.cli.test_runner import SECTIONS_INFO

    click.echo("  Available test sections:\n")
    for num, title in sorted(SECTIONS_INFO.items()):
        click.echo(f"    {num:>2d}: {title}")
    click.echo("\n  Usage: crawlhub test run -s <nums>")
    click.echo("  Example: crawlhub test run -s 0,1,2")
    click.echo("  Range: crawlhub test run -s 3-5,8")
