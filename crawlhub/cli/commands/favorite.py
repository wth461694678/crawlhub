"""Favorite (task template) management commands.

Subcommands:
    favorite list      [--platform] [--search]
    favorite get       <fav_id>
    favorite create    --platform --action [-d JSON | -f FILE | stdin] [--note]
    favorite delete    <fav_id>... [--platform] [--search] [-y] [--dry-run]
    favorite use       <fav_id>
    favorite save-from <task_id> [--note]
    favorite note      <fav_id> [--set TEXT | --clear]
"""

import json
import sys
import time
from typing import Optional

import click
import httpx

from crawlhub.cli._utils import ensure_daemon, get_base_url
from crawlhub.cli.commands._bulk import (
    BulkOutcome,
    BulkResult,
    BulkSpec,
    bulk_options,
    run_bulk,
)


# ── Helpers ───────────────────────────────────────────────────────────

def _fmt_time(ts):
    if not ts:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def _print_favorite_detail(f: dict) -> None:
    """Pretty-print a single favorite dict."""
    click.echo(f"  favorite_id    : {f.get('favorite_id', '-')}")
    click.echo(f"  platform       : {f.get('platform', '-')}")
    click.echo(f"  action         : {f.get('task_type', '-')}")
    click.echo(f"  note           : {f.get('note') or '-'}")
    click.echo(f"  source_task_id : {f.get('source_task_id') or '-'}")
    click.echo(f"  created_at     : {_fmt_time(f.get('created_at'))}")
    lp = f.get("logic_param")
    if lp:
        lp_str = json.dumps(lp, ensure_ascii=False, indent=2)
        click.echo(f"  logic_param    :")
        for line in lp_str.splitlines():
            click.echo(f"    {line}")
    else:
        click.echo(f"  logic_param    : (empty)")


def _read_params_input(data: Optional[str], input_file: Optional[str]) -> dict:
    """Parse --data / --file / stdin into a params dict.

    Priority: --data > --file > stdin. Same precedence as `task submit single`.
    Exits with [ERR] on conflict or invalid JSON.
    """
    if data and input_file:
        click.echo("[ERR] --data and --file are mutually exclusive.", err=True)
        raise SystemExit(2)

    if data:
        try:
            parsed = json.loads(data)
        except json.JSONDecodeError as e:
            click.echo(f"[ERR] Invalid JSON in --data: {e}", err=True)
            raise SystemExit(1)
    elif input_file:
        try:
            with open(input_file, "r", encoding="utf-8") as f:
                parsed = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            click.echo(f"[ERR] Cannot read --file: {e}", err=True)
            raise SystemExit(1)
    elif not sys.stdin.isatty():
        try:
            parsed = json.loads(sys.stdin.read())
        except json.JSONDecodeError as e:
            click.echo(f"[ERR] Invalid JSON from stdin: {e}", err=True)
            raise SystemExit(1)
    else:
        parsed = {}

    if not isinstance(parsed, dict):
        click.echo("[ERR] params must be a JSON object (not list / scalar).", err=True)
        raise SystemExit(1)
    return parsed


# ── favorite group ────────────────────────────────────────────────────

@click.group()
def favorite():
    """Favorite (task template) management commands."""
    pass


# ── favorite list ─────────────────────────────────────────────────────

@favorite.command("list")
@click.option("--platform", default=None, help="Filter by platform.")
@click.option("--search", "-s", default=None,
              help="Keyword search across note / platform / action.")
@click.option("--json", "json_output", is_flag=True, help="Output raw JSON.")
@click.pass_context
def favorite_list(ctx, platform: Optional[str], search: Optional[str], json_output: bool):
    """List favorites."""
    ensure_daemon()
    base_url = get_base_url()
    params = {}
    if platform:
        params["platform"] = platform
    if search:
        params["search"] = search

    try:
        resp = httpx.get(f"{base_url}/api/favorites", params=params, timeout=10)
    except httpx.RequestError as e:
        click.echo(f"[ERR] network: {e}", err=True)
        raise SystemExit(1)

    if resp.status_code != 200:
        click.echo(f"[ERR] {resp.status_code}: {resp.text}", err=True)
        raise SystemExit(1)

    favorites = resp.json().get("favorites", [])
    json_out = json_output or (ctx.obj and ctx.obj.get("json_output"))
    if json_out:
        click.echo(json.dumps(favorites, indent=2, ensure_ascii=False))
        return

    if not favorites:
        click.echo("[INFO] No favorites.")
        return

    # Compact table.
    headers = ["#", "favorite_id", "platform", "action", "note", "created_at"]
    rows = []
    for i, f in enumerate(favorites, 1):
        note = f.get("note") or "-"
        if len(note) > 30:
            note = note[:27] + "..."
        rows.append([
            str(i),
            f.get("favorite_id", "-"),
            (f.get("platform") or "-")[:12],
            (f.get("task_type") or "-")[:20],
            note,
            _fmt_time(f.get("created_at")),
        ])
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def fmt(cells):
        return "  " + "  ".join(c.ljust(widths[i]) for i, c in enumerate(cells))

    click.echo(fmt(headers))
    click.echo(fmt(["-" * w for w in widths]))
    for row in rows:
        click.echo(fmt(row))
    click.echo(f"\n[INFO] {len(favorites)} favorite(s)")


# ── favorite get ──────────────────────────────────────────────────────

@favorite.command("get")
@click.argument("favorite_id")
@click.option("--json", "json_output", is_flag=True, help="Output raw JSON.")
@click.pass_context
def favorite_get(ctx, favorite_id: str, json_output: bool):
    """Show full detail of a single favorite."""
    ensure_daemon()
    base_url = get_base_url()
    try:
        resp = httpx.get(f"{base_url}/api/favorites/{favorite_id}", timeout=10)
    except httpx.RequestError as e:
        click.echo(f"[ERR] network: {e}", err=True)
        raise SystemExit(1)

    if resp.status_code == 404:
        click.echo(f"[ERR] Favorite {favorite_id} not found.", err=True)
        raise SystemExit(1)
    if resp.status_code != 200:
        click.echo(f"[ERR] {resp.status_code}: {resp.text}", err=True)
        raise SystemExit(1)

    fav = resp.json()
    json_out = json_output or (ctx.obj and ctx.obj.get("json_output"))
    if json_out:
        click.echo(json.dumps(fav, indent=2, ensure_ascii=False))
    else:
        _print_favorite_detail(fav)


# ── favorite create ───────────────────────────────────────────────────

@favorite.command("create")
@click.option("--platform", required=True, help="Platform name.")
@click.option("--action", required=True, help="Task type / action name.")
@click.option("--data", "-d", default=None, help="Inline JSON params string.")
@click.option("--file", "-f", "input_file", default=None, type=click.Path(exists=True),
              help="JSON file with params.")
@click.option("--note", default=None, help="Optional note for the favorite.")
@click.option("--json", "json_output", is_flag=True, help="Output raw JSON.")
@click.pass_context
def favorite_create(ctx, platform: str, action: str, data: Optional[str],
                    input_file: Optional[str], note: Optional[str], json_output: bool):
    """Create a new favorite (task template).

    Params input priority: --data > --file > stdin. May be empty.

    Examples:
        crawlhub favorite create --platform steam --action get_game_detail \\
            -d '{"app_id": "730", "cc": "us", "language": "schinese"}' --note "CS2 weekly"
        crawlhub favorite create --platform bilibili --action search_videos \\
            -f params.json
    """
    params = _read_params_input(data, input_file)

    ensure_daemon()
    base_url = get_base_url()
    body = {
        "platform": platform,
        "task_type": action,
        "logic_param": params,
        "note": note or f"{platform}_{action}",
        "source_task_id": "",
    }
    try:
        resp = httpx.post(f"{base_url}/api/favorites", json=body, timeout=10)
    except httpx.RequestError as e:
        click.echo(f"[ERR] network: {e}", err=True)
        raise SystemExit(1)

    if resp.status_code != 200:
        click.echo(f"[ERR] {resp.status_code}: {resp.text}", err=True)
        raise SystemExit(1)

    fav = resp.json()
    json_out = json_output or (ctx.obj and ctx.obj.get("json_output"))
    if json_out:
        click.echo(json.dumps(fav, indent=2, ensure_ascii=False))
    else:
        click.echo(f"[OK] Favorite created")
        _print_favorite_detail(fav)


# ── favorite delete (bulk) ────────────────────────────────────────────

def _favorite_resolver(filters: dict) -> list:
    """Resolve filter flags -> list of favorite dicts."""
    base_url = get_base_url()
    params = {}
    if filters.get("platform"):
        params["platform"] = filters["platform"]
    if filters.get("search"):
        params["search"] = filters["search"]
    try:
        resp = httpx.get(f"{base_url}/api/favorites", params=params, timeout=10)
    except httpx.RequestError as e:
        raise click.ClickException(f"network: {e}")
    if resp.status_code != 200:
        raise click.ClickException(f"{resp.status_code}: {resp.text}")
    return resp.json().get("favorites", [])


def _favorite_fetch_by_id(fav_id: str):
    base_url = get_base_url()
    try:
        resp = httpx.get(f"{base_url}/api/favorites/{fav_id}", timeout=10)
    except httpx.RequestError:
        return None
    if resp.status_code == 200:
        return resp.json()
    return None


_FAV_BULK_COLUMNS = (
    ("favorite_id", lambda f: f.get("favorite_id", "?")),
    ("platform", lambda f: (f.get("platform") or "-")[:12]),
    ("action", lambda f: (f.get("task_type") or "-")[:20]),
    ("note", lambda f: ((f.get("note") or "-")[:30])),
)


@favorite.command("delete")
@click.argument("favorite_ids", nargs=-1)
@click.option("--platform", default=None, help="Filter: platform.")
@click.option("--search", "-s", default=None, help="Filter: keyword across note/platform/action.")
@bulk_options
@click.pass_context
def favorite_delete(ctx, favorite_ids, platform, search,
                    yes, dry_run):
    """Delete favorites permanently.

    Pass one or more FAVORITE_IDs, or use filters to select multiple. Favorites
    have no recycle bin — deletion is immediate.
    """
    # Favorites have no recycle bin; every delete is destructive. AI-friendly
    # CLI never prompts on stdin, so we require -y even for a single-ID call.
    # (The bulk runner enforces -y for >=2 targets via filters separately.)
    if favorite_ids and not yes and not dry_run:
        click.echo(
            "[ERR] favorite delete is irreversible (no recycle bin); "
            "pass -y/--yes to confirm or --dry-run to preview.",
            err=True,
        )
        sys.exit(2)

    base_url = get_base_url()

    def action(f: dict) -> BulkOutcome:
        fid = f.get("favorite_id")
        if f.get("_missing"):
            return BulkOutcome(BulkResult.SKIP, "not found")
        try:
            r = httpx.delete(f"{base_url}/api/favorites/{fid}", timeout=10)
        except httpx.RequestError as e:
            return BulkOutcome(BulkResult.FAIL, f"network: {e}")
        if r.status_code == 200:
            return BulkOutcome(BulkResult.OK, "deleted")
        if r.status_code == 404:
            return BulkOutcome(BulkResult.SKIP, "not found")
        return BulkOutcome(BulkResult.FAIL, f"{r.status_code} {r.text}")

    spec = BulkSpec(
        resolver=_favorite_resolver,
        fetch_by_id=_favorite_fetch_by_id,
        action=action,
        columns=_FAV_BULK_COLUMNS,
        entity_name="favorite",
        id_field="favorite_id",
        ensure_ready=ensure_daemon,
    )
    filters = {"platform": platform, "search": search}
    sys.exit(run_bulk(spec, favorite_ids, filters,
                      action_label="DELETE", yes=yes, dry_run=dry_run))


# ── favorite use ──────────────────────────────────────────────────────

@favorite.command("use")
@click.argument("favorite_id")
@click.option("--json", "json_output", is_flag=True, help="Output raw JSON.")
@click.pass_context
def favorite_use(ctx, favorite_id: str, json_output: bool):
    """Submit a task using a favorite as-is.

    The favorite is preserved for reuse. To submit with different params,
    create a new favorite. (No --override-params by design.)
    """
    ensure_daemon()
    base_url = get_base_url()

    # Fetch favorite
    try:
        resp = httpx.get(f"{base_url}/api/favorites/{favorite_id}", timeout=10)
    except httpx.RequestError as e:
        click.echo(f"[ERR] network: {e}", err=True)
        raise SystemExit(1)
    if resp.status_code == 404:
        click.echo(f"[ERR] Favorite {favorite_id} not found.", err=True)
        raise SystemExit(1)
    if resp.status_code != 200:
        click.echo(f"[ERR] {resp.status_code}: {resp.text}", err=True)
        raise SystemExit(1)
    fav = resp.json()

    # Submit task verbatim from favorite
    body = {
        "platform": fav["platform"],
        "task_type": fav["task_type"],
        "logic_param": fav.get("logic_param", {}),
    }
    try:
        sub = httpx.post(f"{base_url}/api/tasks", json=body, timeout=10)
    except httpx.RequestError as e:
        click.echo(f"[ERR] network: {e}", err=True)
        raise SystemExit(1)

    if sub.status_code != 200:
        click.echo(f"[ERR] submit failed: {sub.status_code}: {sub.text}", err=True)
        raise SystemExit(1)

    result = sub.json()
    json_out = json_output or (ctx.obj and ctx.obj.get("json_output"))
    if json_out:
        click.echo(json.dumps(result, indent=2, ensure_ascii=False))
        return

    tid = result.get("task_id", "?")
    click.echo(f"[OK] Task submitted from favorite {favorite_id}")
    click.echo(f"  task_id   : {tid}")
    click.echo(f"  platform  : {result.get('platform', '-')}")
    click.echo(f"  action    : {result.get('task_type', '-')}")
    click.echo(f"  status    : {result.get('status', '-')}")
    click.echo(f"  created_at: {_fmt_time(result.get('created_at'))}")
    click.echo(f"\n  Next: crawlhub task get {tid}")


# ── favorite save-from ────────────────────────────────────────────────

@favorite.command("save-from")
@click.argument("task_id")
@click.option("--note", default=None, help="Optional note (defaults to platform_action).")
@click.option("--json", "json_output", is_flag=True, help="Output raw JSON.")
@click.pass_context
def favorite_save_from(ctx, task_id: str, note: Optional[str], json_output: bool):
    """Save an existing task's params as a new favorite.

    Copies platform / task_type / logic_param verbatim from the task. The
    resulting favorite can be re-submitted via `favorite use <fav_id>`.
    """
    ensure_daemon()
    base_url = get_base_url()

    # Fetch task
    try:
        resp = httpx.get(f"{base_url}/api/tasks/{task_id}", timeout=10)
    except httpx.RequestError as e:
        click.echo(f"[ERR] network: {e}", err=True)
        raise SystemExit(1)
    if resp.status_code == 404:
        click.echo(f"[ERR] Task {task_id} not found.", err=True)
        raise SystemExit(1)
    if resp.status_code != 200:
        click.echo(f"[ERR] {resp.status_code}: {resp.text}", err=True)
        raise SystemExit(1)
    t = resp.json()

    platform = t.get("platform", "")
    action = t.get("task_type", "")
    if not platform or not action:
        click.echo(f"[ERR] Task {task_id} missing platform/task_type, cannot save.", err=True)
        raise SystemExit(1)

    body = {
        "platform": platform,
        "task_type": action,
        # Save the user's original POST body (logic_param) verbatim as the
        # favorite's logic_param -- that's what "save this task as a
        # template" should mean. snapshot_param has expanded items / filled
        # defaults, which would be misleading to re-submit verbatim later.
        "logic_param": t.get("logic_param") or {},
        "note": note or f"{platform}_{action}",
        "source_task_id": task_id,
    }
    try:
        cr = httpx.post(f"{base_url}/api/favorites", json=body, timeout=10)
    except httpx.RequestError as e:
        click.echo(f"[ERR] network: {e}", err=True)
        raise SystemExit(1)

    if cr.status_code != 200:
        click.echo(f"[ERR] {cr.status_code}: {cr.text}", err=True)
        raise SystemExit(1)

    fav = cr.json()
    json_out = json_output or (ctx.obj and ctx.obj.get("json_output"))
    if json_out:
        click.echo(json.dumps(fav, indent=2, ensure_ascii=False))
    else:
        click.echo(f"[OK] Favorite saved from task {task_id}")
        _print_favorite_detail(fav)


# ── favorite note ─────────────────────────────────────────────────────

@favorite.command("note")
@click.argument("favorite_id")
@click.option("--set", "set_note", default=None, help="Set note text.")
@click.option("--clear", is_flag=True, help="Clear note (set to empty).")
@click.option("--json", "json_output", is_flag=True, help="Output raw JSON.")
@click.pass_context
def favorite_note(ctx, favorite_id: str, set_note: Optional[str],
                  clear: bool, json_output: bool):
    """View or update a favorite's note.

    No options    : show current note.
    --set <text>  : set note to <text>.
    --clear       : clear note (sets it to empty string).

    --set and --clear are mutually exclusive. --set "" is rejected (use --clear).
    """
    # Strict mutual exclusion: report and exit before any network call.
    if set_note is not None and clear:
        click.echo("[ERR] --set and --clear are mutually exclusive.", err=True)
        raise SystemExit(2)
    if set_note is not None and set_note == "":
        click.echo("[ERR] --set with empty string is not allowed; use --clear instead.",
                   err=True)
        raise SystemExit(2)

    ensure_daemon()
    base_url = get_base_url()
    json_out = json_output or (ctx.obj and ctx.obj.get("json_output"))

    if set_note is not None or clear:
        new_value = "" if clear else set_note
        try:
            resp = httpx.put(f"{base_url}/api/favorites/{favorite_id}",
                             json={"note": new_value}, timeout=10)
        except httpx.RequestError as e:
            click.echo(f"[ERR] network: {e}", err=True)
            raise SystemExit(1)
        if resp.status_code == 404:
            click.echo(f"[ERR] Favorite {favorite_id} not found.", err=True)
            raise SystemExit(1)
        if resp.status_code != 200:
            click.echo(f"[ERR] {resp.status_code}: {resp.text}", err=True)
            raise SystemExit(1)
        data = resp.json()
        if json_out:
            click.echo(json.dumps(data, indent=2, ensure_ascii=False))
        else:
            verb = "cleared" if clear else "updated"
            click.echo(f"[OK] Note {verb}: {favorite_id}")
        return

    # No flags -> show current note.
    try:
        resp = httpx.get(f"{base_url}/api/favorites/{favorite_id}", timeout=10)
    except httpx.RequestError as e:
        click.echo(f"[ERR] network: {e}", err=True)
        raise SystemExit(1)
    if resp.status_code == 404:
        click.echo(f"[ERR] Favorite {favorite_id} not found.", err=True)
        raise SystemExit(1)
    if resp.status_code != 200:
        click.echo(f"[ERR] {resp.status_code}: {resp.text}", err=True)
        raise SystemExit(1)
    fav = resp.json()
    note = fav.get("note")
    if json_out:
        click.echo(json.dumps({"favorite_id": favorite_id, "note": note},
                              indent=2, ensure_ascii=False))
    else:
        click.echo(f"  note: {note or '(empty)'}")
