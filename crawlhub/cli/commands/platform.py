"""Platform commands: list registered crawler platforms; scaffold new ones;
inspect (input + output) schema with v2 label/description rendering.

Note: ``crawlhub task schema`` ALSO shows action parameters but is geared
toward task-submission (with an example payload). ``crawlhub platform
schema`` is geared toward "what data does this action produce" — it shows
input AND output schemas side by side with v2 Chinese labels for both
humans (table) and machines (``--json`` dumps the v2 form).
"""

import json

import click
import httpx

from crawlhub.cli._utils import ensure_daemon, get_base_url


@click.group()
def platform():
    """Platform registry commands."""
    pass


def _fetch_platforms() -> list[dict]:
    """GET /api/platforms and return the platforms list (or exit on error)."""
    ensure_daemon()
    base_url = get_base_url()
    try:
        resp = httpx.get(f"{base_url}/api/platforms", timeout=10)
    except httpx.ConnectError:
        click.echo("[ERR] Daemon is not running.", err=True)
        raise SystemExit(1)
    if resp.status_code != 200:
        click.echo(f"[ERR] {resp.status_code}: {resp.text}", err=True)
        raise SystemExit(1)
    return resp.json().get("platforms", [])


@platform.command("list")
@click.pass_context
def platform_list(ctx):
    """List all registered platforms and their supported actions."""
    platforms = _fetch_platforms()

    if ctx.obj and ctx.obj.get("json_output"):
        click.echo(json.dumps({"platforms": platforms}, indent=2, ensure_ascii=False))
        return

    if not platforms:
        click.echo("[INFO] No platforms registered.")
        return

    name_width = max(len(p.get("platform", "")) for p in platforms)
    click.echo(f"{'PLATFORM'.ljust(name_width)}  ACTIONS")
    for p in platforms:
        name = p.get("platform", "")
        actions = p.get("actions", []) or []
        act_names = ", ".join(act.get("name", "") for act in actions) or "-"
        click.echo(f"{name.ljust(name_width)}  {act_names}")

    click.echo(f"\n[OK] {len(platforms)} platform(s).")


# ---------------------------------------------------------------------------
# `crawlhub platform new <name>` — scaffold a new platform crawler.
# ---------------------------------------------------------------------------

@platform.command("new")
@click.argument("name", metavar="NAME")
@click.option(
    "--display",
    "display_name",
    default=None,
    metavar="TEXT",
    help="Human-facing display name shown in the UI / API responses "
         "(default: title-cased <NAME>). Chinese is fine.",
)
@click.option(
    "--description",
    default=None,
    metavar="TEXT",
    help="Plugin description string for plugin.yaml.",
)
def platform_new(
    name: str,
    display_name: str | None,
    description: str | None,
):
    """Scaffold a new platform crawler under crawlhub/crawlers/<NAME>/.

    \b
    NAME    snake_case platform identifier (required), e.g. 'myplatform'
            or 'foo_bar'. Must match ^[a-z][a-z0-9_]*$ and not start
            with '_'. This becomes both the directory name and the
            `name:` field in plugin.yaml.

    The destination is always crawlhub/crawlers/ (the daemon's discovery
    root); no other location is supported. The generated scaffold is
    always verified against tests/test_platform_conformance.py before
    the command returns -- a template-induced failure is a bug in this
    package.
    """
    # Lazy import: keep CLI startup cheap and avoid pulling in importlib.resources
    # for unrelated commands.
    from crawlhub.scaffolding import ScaffoldError, startplatform

    try:
        result = startplatform(
            name,
            display_name=display_name,
            description=description,
        )
    except ScaffoldError as exc:
        click.echo(f"[ERR] {exc}", err=True)
        raise SystemExit(1)

    click.echo(f"[OK] Created platform '{result.platform_name}' at {result.platform_dir}")
    for f in result.written_files:
        try:
            rel = f.relative_to(result.platform_dir.parent)
        except ValueError:
            rel = f
        click.echo(f"       {rel}")

    click.echo("")
    click.echo(f"[OK]  {result.platform_name} - all conformance checks passed")

    click.echo("")
    click.echo("Next steps:")
    click.echo("  1. Edit plugin.yaml -- declare actions, schemas, runtime, transport")
    click.echo("  2. Implement crawler/client.py (HTTP/WSS protocol layer)")
    click.echo("  3. Implement crawler/models.py (@dataclass + to_dict)")
    click.echo("  4. Implement crawler/scraper.py (orchestration)")
    click.echo(
        f"  5. Run: pytest tests/test_platform_conformance.py -k {result.platform_name}"
    )


# ---------------------------------------------------------------------------
# `crawlhub platform schema <platform> <action>` -- inspect input + output
# ---------------------------------------------------------------------------

def _truncate(s: str, n: int) -> str:
    """Trim to n chars with single-line ellipsis (terminal-friendly)."""
    if not s:
        return ""
    s = s.replace("\n", " ").replace("\r", " ").strip()
    if len(s) <= n:
        return s
    return s[: max(0, n - 1)] + "..."


def _print_schema_table(
    rows: list[tuple],
    headers: tuple,
) -> None:
    """Pretty-print a variable-width schema table.

    Columns auto-fit to content with sane caps so a wide ``description``
    column doesn't wreck the layout. Description rows wider than the cap
    are truncated with "..." (machines should use ``--json``). The number
    of columns is taken from ``headers`` length; input and output tables
    pick different shapes (input has REQUIRED, output doesn't).
    """
    if not rows:
        click.echo("  (none)")
        return
    n = len(headers)
    # Per-column caps in declaration order: field/type/required/label/desc
    # for input; field/type/label/desc for output. The mapping below picks
    # by column name so reordering / dropping columns stays correct.
    cap_for = {
        "FIELD": 24, "TYPE": 12, "REQUIRED": 9, "LABEL": 24, "DESCRIPTION": 60,
    }
    caps = [cap_for.get(str(h).upper(), 32) for h in headers]
    widths = [len(h) for h in headers]
    for r in rows:
        for i, cell in enumerate(r):
            widths[i] = min(caps[i], max(widths[i], len(str(cell))))

    def _fmt_row(cells):
        return "  ".join(str(cells[i]).ljust(widths[i]) for i in range(n))

    click.echo("  " + _fmt_row(headers))
    click.echo("  " + _fmt_row(tuple("-" * w for w in widths)))
    for r in rows:
        click.echo("  " + _fmt_row(tuple(_truncate(str(c), widths[i]) for i, c in enumerate(r))))


@platform.command("schema")
@click.argument("platform_name", metavar="PLATFORM")
@click.argument("action_name", metavar="ACTION")
@click.pass_context
def platform_schema(ctx, platform_name: str, action_name: str):
    """Show v2 schema (input + output, with Chinese labels) for PLATFORM.ACTION.

    \b
    By default prints two human-readable tables:
      - Input parameters: field / type / required / label / description
      - Output columns:   field / type / label / description
    Use ``--json`` (global flag) to dump the full v2 structure for
    programmatic / AI consumers.

    Examples:
        crawlhub platform schema steam search_games
        crawlhub --json platform schema steam scrape_reviews
    """
    ensure_daemon()
    base_url = get_base_url()
    url = f"{base_url}/api/actions/{platform_name}/{action_name}/schema"
    try:
        resp = httpx.get(url, timeout=10)
    except httpx.ConnectError:
        click.echo("[ERR] Daemon is not running.", err=True)
        raise SystemExit(1)
    if resp.status_code == 404:
        # Helpful disambiguation: list available actions for this platform.
        try:
            plats = httpx.get(f"{base_url}/api/platforms", timeout=5).json()\
                .get("platforms", [])
        except Exception:
            plats = []
        plat = next((p for p in plats if p.get("platform") == platform_name), None)
        if plat is None:
            avail = ", ".join(p.get("platform", "") for p in plats) or "(none)"
            click.echo(
                f"[ERR] Platform '{platform_name}' not found. Available: {avail}",
                err=True,
            )
        else:
            avail = ", ".join(a.get("name", "") for a in plat.get("actions", [])) or "(none)"
            click.echo(
                f"[ERR] Action '{action_name}' not found on platform "
                f"'{platform_name}'. Available: {avail}",
                err=True,
            )
        raise SystemExit(1)
    if resp.status_code != 200:
        click.echo(f"[ERR] {resp.status_code}: {resp.text}", err=True)
        raise SystemExit(1)

    payload = resp.json()

    # JSON dump path: hand back the raw v2 envelope.
    if ctx.obj and ctx.obj.get("json_output"):
        click.echo(json.dumps(payload, indent=2, ensure_ascii=False))
        return

    desc = payload.get("description") or ""
    click.echo(f"Action: {platform_name}.{action_name}")
    if desc:
        click.echo(f"Description: {desc}")
    click.echo("")

    # ---- Input parameters --------------------------------------------------
    in_schema = payload.get("input_schema") or {}
    props: dict = in_schema.get("properties") or {}
    required = set(in_schema.get("required") or [])

    click.echo("Input parameters:")
    in_rows: list[tuple] = []
    for fname, spec in props.items():
        # JSON Schema convention: ``title`` is the human label.
        type_str = spec.get("type") or "any"
        if "default" in spec:
            type_str = f"{type_str} (=> {spec['default']!r})"
        in_rows.append((
            fname,
            type_str,
            "yes" if fname in required else "",
            spec.get("title", "") or "",
            spec.get("description", "") or "",
        ))
    _print_schema_table(
        in_rows,
        headers=("FIELD", "TYPE", "REQUIRED", "LABEL", "DESCRIPTION"),
    )
    click.echo("")

    # ---- Output schema (v2) -----------------------------------------------
    out_v2 = payload.get("output_schema_v2") or {}
    click.echo("Output columns:")
    out_rows: list[tuple] = []
    for fname, fdef in out_v2.items():
        # fdef is {type, label, description} (v2 form from the API). Output
        # rows have no REQUIRED column — every declared column is always
        # populated by the dataclass; required-ness is an input-side concept.
        out_rows.append((
            fname,
            fdef.get("type", "") or "",
            fdef.get("label", "") or "",
            fdef.get("description", "") or "",
        ))
    _print_schema_table(
        out_rows,
        headers=("FIELD", "TYPE", "LABEL", "DESCRIPTION"),
    )
    click.echo("")
    click.echo("Tip: pipe with --json (global flag) for the full v2 structure.")
