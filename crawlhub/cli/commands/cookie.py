"""Cookie multi-account management commands.

Subcommands:
    cookie list        [--platform]
    cookie get         <platform> <label>
    cookie add         <platform> [--label TEXT]                 # browser login (blocking)
    cookie add-raw     <platform> [-r STR | -f FILE] [--label] [--format auto|raw_string|netscape|json]
    cookie refresh     <platform> --label LABEL                  # single cookie only
    cookie cancel-login <platform> -y                            # clear stuck login state
    cookie probe       [PLATFORM]... [--all] [--task-type X] [-y] [--dry-run]
    cookie delete      <platform> <label>... [-y] [--dry-run]
    cookie note        <platform> <label> [--set TEXT | --clear]
    cookie history     <platform> <label>                        # probe history
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


_REFRESH_POLL_TIMEOUT = 310  # seconds


def _list_platforms() -> list[str]:
    """Discover platforms dynamically from the daemon. Falls back to a known list."""
    base_url = get_base_url()
    try:
        resp = httpx.get(f"{base_url}/api/platforms", timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            # /api/platforms returns either {"platforms": [...]} or a flat list
            if isinstance(data, dict):
                items = data.get("platforms") or data.get("items") or []
            else:
                items = data
            names = []
            for it in items:
                if isinstance(it, str):
                    names.append(it)
                elif isinstance(it, dict):
                    name = it.get("name") or it.get("platform")
                    if name:
                        names.append(name)
            if names:
                return names
    except httpx.RequestError:
        pass
    # Fallback. Keep this list narrow — it only matters when /api/platforms is unreachable.
    return ["bilibili", "douyin", "kuaishou", "weibo", "qimai", "steam"]


def _fetch_cookies(platform: str, search: Optional[str] = None):
    """Return (status_code, cookies_list) for a platform."""
    base_url = get_base_url()
    params = {}
    if search:
        params["search"] = search
    try:
        resp = httpx.get(f"{base_url}/api/cookies/{platform}", params=params, timeout=10)
    except httpx.RequestError as e:
        raise click.ClickException(f"network: {e}")
    if resp.status_code == 200:
        return 200, resp.json().get("cookies", [])
    return resp.status_code, []


def _print_cookie_table(platform: str, cookies: list) -> None:
    """Render a small fixed-width table of cookies for one platform."""
    if not cookies:
        click.echo(f"\n  [{platform}] No cookies.")
        return
    headers = ["status", "label", "account_id", "n", "modified", "note"]
    rows = []
    for c in cookies:
        status_icon = {"green": "[OK]", "red": "[EXPIRED]", "gray": "[?]"}.get(
            c.get("status_light", "gray"), "[?]"
        )
        note = (c.get("note") or "").strip()
        if len(note) > 30:
            note = note[:27] + "..."
        rows.append([
            status_icon,
            str(c.get("label") or "-"),
            str(c.get("account_id") or "-")[:18],
            str(c.get("cookie_count") or 0),
            str(c.get("last_modified") or "-"),
            note or "-",
        ])
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def fmt(cells):
        return "    " + "  ".join(c.ljust(widths[i]) for i, c in enumerate(cells))

    click.echo(f"\n  [{platform}] ({len(cookies)} account(s))")
    click.echo(fmt(headers))
    click.echo(fmt(["-" * w for w in widths]))
    for row in rows:
        click.echo(fmt(row))


def _poll_refresh_status(base_url: str, platform: str, action: str = "refresh"):
    """Poll refresh-status until done (blocking)."""
    start = time.time()
    msgs = {
        "completed": {"refresh": "[OK] Cookie refreshed.", "add": "[OK] New account cookie saved."},
        "cancelled": {"refresh": "[INFO] Browser was closed by user.",
                      "add": "[INFO] Browser was closed by user."},
        "timeout":   {"refresh": "[WARN] Refresh timed out.", "add": "[WARN] Login timed out."},
    }
    while time.time() - start < _REFRESH_POLL_TIMEOUT:
        # Poll quickly so a server-side close-event update reaches the user
        # promptly. Was 3s before, but with event-driven close detection on
        # the server side, 1s tightens the visible feedback loop.
        time.sleep(1)
        try:
            resp = httpx.get(f"{base_url}/api/cookies/{platform}/refresh-status", timeout=10)
            if resp.status_code == 200:
                state = resp.json()
                if not state.get("is_refreshing"):
                    result = state.get("result", "unknown")
                    click.echo(msgs.get(result, {}).get(action, f"[INFO] {action} ended: {result}"))
                    return
        except Exception:
            pass
    click.echo("[WARN] Polling timed out.")


# ── cookie group ──────────────────────────────────────────────────────

@click.group()
def cookie():
    """Cookie multi-account management commands."""
    pass


# ── cookie list ───────────────────────────────────────────────────────

@cookie.command("list")
@click.option("--platform", "-p", default=None, help="Filter by platform. Omit to scan all.")
@click.option("--search", "-s", default=None,
              help="Keyword search across label / account_id / note.")
@click.option("--json", "json_output", is_flag=True, help="Output raw JSON.")
@click.pass_context
def cookie_list(ctx, platform: Optional[str], search: Optional[str], json_output: bool):
    """List cookies for a platform (or every known platform)."""
    ensure_daemon()
    platforms = [platform] if platform else _list_platforms()

    aggregate = {}
    total = 0
    for p in platforms:
        code, cookies = _fetch_cookies(p, search)
        if code == 404:
            if platform:
                click.echo(f"[ERR] Platform '{p}' not found.", err=True)
                raise SystemExit(1)
            continue  # skip unknown platforms in scan-all mode
        if code != 200:
            click.echo(f"[WARN] {p}: HTTP {code}")
            continue
        aggregate[p] = cookies
        total += len(cookies)

    json_out = json_output or (ctx.obj and ctx.obj.get("json_output"))
    if json_out:
        click.echo(json.dumps(aggregate, indent=2, ensure_ascii=False))
        return

    if total == 0:
        click.echo("[INFO] No cookies.")
        return

    for p, cookies in aggregate.items():
        _print_cookie_table(p, cookies)
    click.echo(f"\n[INFO] {total} cookie(s) across {len(aggregate)} platform(s).")


# ── cookie get ────────────────────────────────────────────────────────

@cookie.command("get")
@click.argument("platform")
@click.argument("label")
@click.option("--json", "json_output", is_flag=True, help="Output raw JSON.")
@click.pass_context
def cookie_get(ctx, platform: str, label: str, json_output: bool):
    """Show full detail of one cookie (label/account_id/note/last_probe)."""
    ensure_daemon()
    code, cookies = _fetch_cookies(platform)
    if code == 404:
        click.echo(f"[ERR] Platform '{platform}' not found.", err=True)
        raise SystemExit(1)
    if code != 200:
        click.echo(f"[ERR] HTTP {code}", err=True)
        raise SystemExit(1)

    match = next((c for c in cookies if c.get("label") == label), None)
    if not match:
        click.echo(f"[ERR] Cookie {platform}/{label} not found.", err=True)
        raise SystemExit(1)

    json_out = json_output or (ctx.obj and ctx.obj.get("json_output"))
    if json_out:
        click.echo(json.dumps(match, indent=2, ensure_ascii=False))
        return
    click.echo(f"  platform        : {platform}")
    click.echo(f"  label           : {match.get('label')}")
    click.echo(f"  account_id      : {match.get('account_id') or '-'}")
    click.echo(f"  cookie_count    : {match.get('cookie_count') or 0}")
    click.echo(f"  status_light    : {match.get('status_light') or '-'}")
    click.echo(f"  last_modified   : {match.get('last_modified') or '-'}")
    click.echo(f"  last_probe_time : {match.get('last_probe_time') or '-'}")
    click.echo(f"  note            : {match.get('note') or '-'}")


# ── cookie add (browser-assisted) ─────────────────────────────────────

@cookie.command("add")
@click.argument("platform")
@click.option("--label", "-l", default=None,
              help="(Reserved) Optional label hint for the new account. "
                   "The backend may ignore this and assign one based on login.")
def cookie_add(platform: str, label: Optional[str]):
    """Open a browser window to log in and add a new cookie account.

    Blocks until the browser closes or the login completes (max ~5 min).
    """
    ensure_daemon()
    base_url = get_base_url()
    try:
        resp = httpx.post(f"{base_url}/api/cookies/{platform}/add", timeout=10)
    except httpx.RequestError as e:
        click.echo(f"[ERR] network: {e}", err=True)
        raise SystemExit(1)

    if resp.status_code == 409:
        click.echo("[ERR] A login flow is already in progress for this platform.", err=True)
        raise SystemExit(1)
    if resp.status_code != 200:
        click.echo(f"[ERR] {resp.status_code}: {resp.text}", err=True)
        raise SystemExit(1)

    click.echo("[INFO] Browser opened. Please complete login...")
    _poll_refresh_status(base_url, platform, action="add")


# ── cookie add-raw ────────────────────────────────────────────────────

@cookie.command("add-raw")
@click.argument("platform")
@click.option("--raw", "-r", default=None, help="Raw cookie string.")
@click.option("--file", "-f", "cookie_file", default=None, type=click.Path(exists=True),
              help="File containing cookie data.")
@click.option("--label", "-l", default=None, help="Optional label for the cookie.")
@click.option("--format", "fmt", default="auto",
              type=click.Choice(["auto", "raw_string", "netscape", "json"]),
              help="Cookie format (default: auto).")
def cookie_add_raw(platform: str, raw: Optional[str], cookie_file: Optional[str],
                   label: Optional[str], fmt: str):
    """Add a cookie by pasting a raw string or supplying a file.

    Examples:
        crawlhub cookie add-raw bilibili --raw "SESSDATA=xxx; bili_jct=yyy"
        crawlhub cookie add-raw bilibili --file cookies.txt --format netscape
    """
    if not raw and not cookie_file:
        click.echo("[ERR] Must provide --raw or --file.", err=True)
        raise SystemExit(2)
    if raw and cookie_file:
        click.echo("[ERR] --raw and --file are mutually exclusive.", err=True)
        raise SystemExit(2)

    if cookie_file:
        try:
            with open(cookie_file, "r", encoding="utf-8") as f:
                raw = f.read()
        except OSError as e:
            click.echo(f"[ERR] Cannot read --file: {e}", err=True)
            raise SystemExit(1)

    ensure_daemon()
    base_url = get_base_url()
    body = {"raw_cookie": raw, "format": fmt}
    if label:
        body["label"] = label

    try:
        resp = httpx.post(f"{base_url}/api/cookies/{platform}/add-raw", json=body, timeout=15)
    except httpx.RequestError as e:
        click.echo(f"[ERR] network: {e}", err=True)
        raise SystemExit(1)

    if resp.status_code != 200:
        click.echo(f"[ERR] {resp.status_code}: {resp.text}", err=True)
        raise SystemExit(1)

    data = resp.json()
    click.echo(
        f"[OK] Cookie saved: label={data.get('label')}, "
        f"account_id={data.get('account_id', 'unknown')}, "
        f"cookies={data.get('cookie_count', 0)}, "
        f"format={data.get('format_detected')}"
    )


# ── cookie refresh (single account only) ──────────────────────────────

@cookie.command("refresh")
@click.argument("platform")
@click.option("--label", "-l", required=True,
              help="Cookie label to refresh. Required — refresh is single-target only.")
def cookie_refresh(platform: str, label: str):
    """Re-login a specific cookie account via browser (blocking).

    Refresh always targets exactly one (platform, label). To add a brand-new
    account, use `cookie add`.
    """
    ensure_daemon()
    base_url = get_base_url()
    try:
        resp = httpx.post(f"{base_url}/api/cookies/{platform}/refresh/{label}", timeout=10)
    except httpx.RequestError as e:
        click.echo(f"[ERR] network: {e}", err=True)
        raise SystemExit(1)

    if resp.status_code == 404:
        click.echo(f"[ERR] Cookie {platform}/{label} not found.", err=True)
        raise SystemExit(1)
    if resp.status_code == 409:
        click.echo("[ERR] A login flow is already in progress for this platform.", err=True)
        raise SystemExit(1)
    if resp.status_code != 200:
        click.echo(f"[ERR] {resp.status_code}: {resp.text}", err=True)
        raise SystemExit(1)

    click.echo(f"[INFO] Browser opened to refresh {platform}/{label}. Please complete login...")
    _poll_refresh_status(base_url, platform, action="refresh")


@cookie.command("cancel-login")
@click.argument("platform")
@click.option("-y", "--yes", "yes", is_flag=True,
              help="Confirm clearing the in-progress login flag.")
def cookie_cancel_login(platform: str, yes: bool):
    """Force-clear a stuck login flow for PLATFORM.

    Use this if `cookie add` / `cookie refresh` reports
    \"A login flow is already in progress\" but no browser is actually open
    (e.g., Ctrl+C'd the CLI while the daemon was still parked on a closed
    browser).

    This does NOT touch any saved cookie — it only flips the daemon-side
    in-memory \"is_refreshing\" flag back to False so a fresh login attempt
    can start.
    """
    if not yes:
        click.echo(
            "[ERR] cookie cancel-login forces the daemon's login state to clean; "
            "pass -y/--yes to confirm.",
            err=True,
        )
        raise SystemExit(2)

    ensure_daemon()
    base_url = get_base_url()
    try:
        resp = httpx.post(
            f"{base_url}/api/cookies/{platform}/cancel-login",
            timeout=10,
        )
    except httpx.RequestError as e:
        click.echo(f"[ERR] network: {e}", err=True)
        raise SystemExit(1)
    if resp.status_code != 200:
        click.echo(f"[ERR] {resp.status_code}: {resp.text}", err=True)
        raise SystemExit(1)
    body = resp.json()
    if body.get("was_refreshing"):
        click.echo(f"[OK] Cleared in-progress login for {platform}.")
    else:
        click.echo(f"[INFO] No login was in progress for {platform}; nothing to do.")


# ── cookie probe (bulk-aware) ─────────────────────────────────────────


def _probe_one_platform(platform: str, task_type: str) -> dict:
    """POST /api/cookies/{p}/probe and return the parsed body (or an error stub)."""
    base_url = get_base_url()
    try:
        resp = httpx.post(
            f"{base_url}/api/cookies/{platform}/probe",
            json={"task_type": task_type},
            timeout=60,
        )
    except httpx.RequestError as e:
        return {"platform": platform, "error": f"network: {e}", "results": []}
    if resp.status_code != 200:
        return {"platform": platform, "error": f"HTTP {resp.status_code}: {resp.text}", "results": []}
    return resp.json()


def _print_probe_block(platform: str, payload: dict) -> None:
    if payload.get("error"):
        click.echo(f"\n  [{platform}] ERROR: {payload['error']}")
        return
    results = payload.get("results", [])
    if not results:
        click.echo(f"\n  [{platform}] no cookies probed.")
        return
    click.echo(f"\n  [{platform}] task_type={payload.get('task_type', '?')}")
    for r in results:
        icon = {"valid": "[OK]", "expired": "[EXPIRED]", "error": "[ERR]"}.get(
            r.get("status", ""), "[?]"
        )
        click.echo(f"    {icon} {r.get('label', 'unknown'):20s} {r.get('message', '')}")


@cookie.command("probe")
@click.argument("platforms", nargs=-1)
@click.option("--all", "probe_all", is_flag=True, help="Probe every platform.")
@click.option("--task-type", "-t", default="search_videos",
              help="Task type to probe with (default: search_videos). Ignored when --all.")
@click.option("-y", "--yes", is_flag=True,
              help="Required when probing multiple platforms (>=2) or --all.")
@click.option("--dry-run", is_flag=True, help="Preview which platforms would be probed.")
@click.option("--json", "json_output", is_flag=True, help="Output raw JSON.")
@click.pass_context
def cookie_probe(ctx, platforms, probe_all: bool, task_type: str,
                 yes: bool, dry_run: bool, json_output: bool):
    """Probe cookie validity.

    Single platform : `cookie probe bilibili`
    Multi-platform  : `cookie probe bilibili douyin -y`
    Every platform  : `cookie probe --all -y`   (uses backend probe-all,
                                                 task_type per registered probe)
    """
    if probe_all and platforms:
        click.echo("[ERR] --all and explicit PLATFORMS are mutually exclusive.", err=True)
        raise SystemExit(2)
    if not probe_all and not platforms:
        click.echo("[ERR] Provide at least one PLATFORM or --all.", err=True)
        raise SystemExit(2)

    ensure_daemon()
    base_url = get_base_url()
    json_out = json_output or (ctx.obj and ctx.obj.get("json_output"))

    # ----- --all branch: uses backend probe-all (cheaper, also broadcasts WS).
    if probe_all:
        if not yes and not dry_run:
            click.echo(
                "[ERR] `cookie probe --all` hits every platform; "
                "pass -y/--yes to confirm or --dry-run to preview.",
                err=True,
            )
            raise SystemExit(2)
        if dry_run:
            discovered = _list_platforms()
            click.echo(f"[DRY-RUN] Would probe all platforms: {', '.join(discovered)}")
            return
        try:
            resp = httpx.post(f"{base_url}/api/cookies/probe-all", timeout=180)
        except httpx.RequestError as e:
            click.echo(f"[ERR] network: {e}", err=True)
            raise SystemExit(1)
        if resp.status_code != 200:
            click.echo(f"[ERR] {resp.status_code}: {resp.text}", err=True)
            raise SystemExit(1)
        data = resp.json().get("results", {})
        if json_out:
            click.echo(json.dumps(data, indent=2, ensure_ascii=False))
            return
        for p, payload in data.items():
            # probe-all returns a flat list of result dicts per platform
            block = {"task_type": "probe", "results": payload if isinstance(payload, list) else []}
            _print_probe_block(p, block)
        return

    # ----- explicit PLATFORMS branch
    n = len(platforms)
    if n >= 2 and not yes and not dry_run:
        click.echo(f"About to probe {n} platforms: {', '.join(platforms)}", err=True)
        click.echo(
            "[ERR] Multi-platform probe is rate-sensitive; "
            "pass -y/--yes to confirm or --dry-run to preview.",
            err=True,
        )
        raise SystemExit(2)

    if dry_run:
        click.echo(f"[DRY-RUN] Would probe {n} platform(s) with task_type={task_type}: "
                   f"{', '.join(platforms)}")
        return

    aggregate = {}
    fail = 0
    for p in platforms:
        payload = _probe_one_platform(p, task_type)
        aggregate[p] = payload
        if payload.get("error"):
            fail += 1

    if json_out:
        click.echo(json.dumps(aggregate, indent=2, ensure_ascii=False))
    else:
        for p, payload in aggregate.items():
            _print_probe_block(p, payload)

    if fail:
        raise SystemExit(1)


# ── cookie history ────────────────────────────────────────────────────

@cookie.command("history")
@click.argument("platform")
@click.argument("label")
@click.option("--json", "json_output", is_flag=True, help="Output raw JSON.")
@click.pass_context
def cookie_history(ctx, platform: str, label: str, json_output: bool):
    """Show probe history of one cookie (most recent 20 entries)."""
    ensure_daemon()
    base_url = get_base_url()
    try:
        resp = httpx.get(f"{base_url}/api/cookies/{platform}/{label}/probes", timeout=10)
    except httpx.RequestError as e:
        click.echo(f"[ERR] network: {e}", err=True)
        raise SystemExit(1)
    if resp.status_code != 200:
        click.echo(f"[ERR] {resp.status_code}: {resp.text}", err=True)
        raise SystemExit(1)

    data = resp.json()
    json_out = json_output or (ctx.obj and ctx.obj.get("json_output"))
    if json_out:
        click.echo(json.dumps(data, indent=2, ensure_ascii=False))
        return

    probes = data.get("probes", [])
    if not probes:
        click.echo(f"[INFO] No probe history for {platform}/{label}.")
        return
    click.echo(f"\n  [{platform}/{label}] {len(probes)} probe(s):")
    for p in probes:
        ts = p.get("probe_time") or "-"
        result = p.get("result") or "?"
        tt = p.get("task_type") or "-"
        msg = p.get("error_message") or ""
        if len(msg) > 60:
            msg = msg[:57] + "..."
        click.echo(f"    {ts}  {result:8s}  task_type={tt:18s}  {msg}")


# ── cookie delete (bulk) ──────────────────────────────────────────────

# A "cookie target" inside _bulk is the dict {"platform": ..., "label": ...}.
# We synthesize a unique id for accounting purposes.

def _ck_id(c: dict) -> str:
    return f"{c.get('platform','?')}/{c.get('label','?')}"


def _cookie_resolver(_filters: dict) -> list:
    # cookie delete is ID-only by design; no filter mode.
    raise click.ClickException(
        "filter-mode delete is not supported for cookies; "
        "spell out the labels you mean to delete."
    )


def _cookie_fetch_by_id(_id: str):
    """ID = 'platform/label'. Look up via list_platform_cookies."""
    if "/" not in _id:
        return None
    platform, label = _id.split("/", 1)
    code, cookies = _fetch_cookies(platform)
    if code != 200:
        return None
    for c in cookies:
        if c.get("label") == label:
            return {
                "platform": platform,
                "label": label,
                "account_id": c.get("account_id"),
                "note": c.get("note"),
                "_id": _id,
            }
    return None


_COOKIE_BULK_COLUMNS = (
    ("platform", lambda c: str(c.get("platform") or "-")),
    ("label", lambda c: str(c.get("label") or "-")),
    ("account_id", lambda c: str(c.get("account_id") or "-")[:18]),
    ("note", lambda c: ((c.get("note") or "-")[:30])),
)


@cookie.command("delete")
@click.argument("platform")
@click.argument("labels", nargs=-1, required=True)
@bulk_options
@click.pass_context
def cookie_delete(ctx, platform, labels,
                  yes, dry_run):
    """Delete cookies permanently (no recycle bin).

    Single  : `cookie delete bilibili mylabel -y`
    Multi   : `cookie delete bilibili label1 label2 label3 -y`

    Wholesale platform wipe is intentionally not supported — spell out the
    labels you mean to delete, or use `cookie list` first to inspect them.
    """
    # Hard-delete is irreversible. Single-target also requires -y.
    if not yes and not dry_run:
        click.echo(
            "[ERR] cookie delete is irreversible (no recycle bin); "
            "pass -y/--yes to confirm or --dry-run to preview.",
            err=True,
        )
        raise SystemExit(2)

    base_url = get_base_url()

    def action(c: dict) -> BulkOutcome:
        if c.get("_missing"):
            return BulkOutcome(BulkResult.SKIP, "not found")
        p, lab = c.get("platform"), c.get("label")
        try:
            r = httpx.delete(f"{base_url}/api/cookies/{p}/{lab}", timeout=10)
        except httpx.RequestError as e:
            return BulkOutcome(BulkResult.FAIL, f"network: {e}")
        if r.status_code == 200:
            return BulkOutcome(BulkResult.OK, "deleted")
        if r.status_code == 404:
            return BulkOutcome(BulkResult.SKIP, "not found")
        return BulkOutcome(BulkResult.FAIL, f"{r.status_code} {r.text}")

    spec = BulkSpec(
        resolver=_cookie_resolver,
        fetch_by_id=_cookie_fetch_by_id,
        action=action,
        columns=_COOKIE_BULK_COLUMNS,
        entity_name="cookie",
        id_field="_id",
        ensure_ready=ensure_daemon,
    )

    ids = tuple(_ck_id({"platform": platform, "label": lab}) for lab in labels)
    sys.exit(run_bulk(spec, ids, {},
                      action_label="DELETE", yes=yes, dry_run=dry_run))


# ── cookie note ───────────────────────────────────────────────────────

@cookie.command("note")
@click.argument("platform")
@click.argument("label")
@click.option("--set", "set_note", default=None, help="Set note text.")
@click.option("--clear", is_flag=True, help="Clear note (set to empty).")
@click.option("--json", "json_output", is_flag=True, help="Output raw JSON.")
@click.pass_context
def cookie_note(ctx, platform: str, label: str, set_note: Optional[str],
                clear: bool, json_output: bool):
    """View or update a cookie's note.

    No options    : show current note.
    --set <text>  : set note to <text>.
    --clear       : clear note (sets it to empty string).

    --set and --clear are mutually exclusive. --set "" is rejected (use --clear).
    """
    if set_note is not None and clear:
        click.echo("[ERR] --set and --clear are mutually exclusive.", err=True)
        raise SystemExit(2)
    if set_note is not None and set_note == "":
        click.echo("[ERR] --set with empty string is not allowed; use --clear instead.", err=True)
        raise SystemExit(2)

    ensure_daemon()
    base_url = get_base_url()
    json_out = json_output or (ctx.obj and ctx.obj.get("json_output"))

    if set_note is not None or clear:
        new_value = "" if clear else set_note
        try:
            resp = httpx.put(f"{base_url}/api/cookies/{platform}/{label}/note",
                             json={"note": new_value}, timeout=10)
        except httpx.RequestError as e:
            click.echo(f"[ERR] network: {e}", err=True)
            raise SystemExit(1)
        if resp.status_code == 404:
            click.echo(f"[ERR] Cookie {platform}/{label} not found.", err=True)
            raise SystemExit(1)
        if resp.status_code != 200:
            click.echo(f"[ERR] {resp.status_code}: {resp.text}", err=True)
            raise SystemExit(1)
        if json_out:
            click.echo(json.dumps(resp.json(), indent=2, ensure_ascii=False))
        else:
            verb = "cleared" if clear else "updated"
            click.echo(f"[OK] Note {verb}: {platform}/{label}")
        return

    # No flags -> show current note.
    try:
        resp = httpx.get(f"{base_url}/api/cookies/{platform}/{label}/note", timeout=10)
    except httpx.RequestError as e:
        click.echo(f"[ERR] network: {e}", err=True)
        raise SystemExit(1)
    if resp.status_code == 404:
        click.echo(f"[ERR] Cookie {platform}/{label} not found.", err=True)
        raise SystemExit(1)
    if resp.status_code != 200:
        click.echo(f"[ERR] {resp.status_code}: {resp.text}", err=True)
        raise SystemExit(1)

    note = resp.json().get("note")
    if json_out:
        click.echo(json.dumps({"platform": platform, "label": label, "note": note},
                              indent=2, ensure_ascii=False))
    else:
        click.echo(f"  note: {note or '(empty)'}")
