"""Notify commands: notification channels and rules.

Command tree:
    crawlhub notify channel list
    crawlhub notify channel add <name> <webhook_url> [--disable]
    crawlhub notify channel delete <name>... [-y]
    crawlhub notify rule list
    crawlhub notify rule add <event_type> <channel_name> [--disable]
    crawlhub notify rule delete <rule_id>... [-y]
    crawlhub notify test [--channel default]

Backed by these REST endpoints:
    GET    /api/notifications/channels
    POST   /api/notifications/channels
    DELETE /api/notifications/channels/{name}
    GET    /api/notifications/rules
    POST   /api/notifications/rules
    DELETE /api/notifications/rules/{rule_id}
    POST   /api/notifications/test
"""

import json
import sys
from typing import Any

import click
import httpx

from crawlhub.cli._utils import ensure_daemon, get_base_url


# ── helpers ───────────────────────────────────────────────────────────

def _json_or(ctx, payload: Any) -> bool:
    """If --json is on, dump payload and return True (caller should return)."""
    if ctx.obj and ctx.obj.get("json_output"):
        click.echo(json.dumps(payload, indent=2, ensure_ascii=False))
        return True
    return False


def _err(msg: str, code: int = 1) -> None:
    """Print to stderr and exit with given code."""
    click.echo(msg, err=True)
    raise SystemExit(code)


def _get(path: str) -> Any:
    ensure_daemon()
    base_url = get_base_url()
    try:
        resp = httpx.get(f"{base_url}{path}", timeout=10)
    except httpx.ConnectError:
        _err("[ERR] Daemon is not running.")
    if resp.status_code != 200:
        _err(f"[ERR] {resp.status_code}: {resp.text}")
    return resp.json()


def _post(path: str, body: dict, timeout: float = 15) -> tuple[int, Any]:
    ensure_daemon()
    base_url = get_base_url()
    try:
        resp = httpx.post(f"{base_url}{path}", json=body, timeout=timeout)
    except httpx.ConnectError:
        _err("[ERR] Daemon is not running.")
    try:
        data = resp.json()
    except Exception:
        data = resp.text
    return resp.status_code, data


def _delete(path: str) -> tuple[int, Any]:
    ensure_daemon()
    base_url = get_base_url()
    try:
        resp = httpx.delete(f"{base_url}{path}", timeout=10)
    except httpx.ConnectError:
        _err("[ERR] Daemon is not running.")
    try:
        data = resp.json()
    except Exception:
        data = resp.text
    return resp.status_code, data


# ── notify (root group) ───────────────────────────────────────────────

@click.group()
def notify():
    """Notification channels, rules, and test."""
    pass


# ── notify channel ────────────────────────────────────────────────────

@notify.group("channel")
def notify_channel():
    """Manage notification channels (webhook endpoints)."""
    pass


@notify_channel.command("list")
@click.pass_context
def channel_list(ctx):
    """List all notification channels."""
    data = _get("/api/notifications/channels")
    if _json_or(ctx, data):
        return

    if not data:
        click.echo("[INFO] No channels configured.")
        return

    name_w = max(len("NAME"), *(len(c.get("name", "")) for c in data))
    click.echo(f"{'NAME'.ljust(name_w)}  ENABLED  WEBHOOK_URL")
    for c in data:
        name = c.get("name", "")
        enabled = "yes" if c.get("enabled") else "no"
        url = c.get("webhook_url", "") or ""
        # Truncate long URLs to keep table readable.
        if len(url) > 80:
            url = url[:77] + "..."
        click.echo(f"{name.ljust(name_w)}  {enabled.ljust(7)}  {url}")
    click.echo(f"\n[OK] {len(data)} channel(s).")


@notify_channel.command("add")
@click.argument("name")
@click.argument("webhook_url")
@click.option("--disable", is_flag=True,
              help="Create the channel in disabled state.")
@click.pass_context
def channel_add(ctx, name: str, webhook_url: str, disable: bool):
    """Create or update a notification channel.

    If a channel with the same NAME exists, it will be updated (webhook_url
    overwritten, enabled flag re-applied).
    """
    body = {
        "name": name,
        "webhook_url": webhook_url,
        "enabled": not disable,
    }
    code, data = _post("/api/notifications/channels", body)
    if code != 200:
        _err(f"[ERR] {code}: {data}")
    if _json_or(ctx, data):
        return
    state = "disabled" if disable else "enabled"
    click.echo(f"[OK] Channel '{name}' saved ({state}).")


@notify_channel.command("delete")
@click.argument("names", nargs=-1, required=True)
@click.option("-y", "--yes", is_flag=True,
              help="Confirm deletion (required when deleting >=2 channels).")
@click.pass_context
def channel_delete(ctx, names: tuple[str, ...], yes: bool):
    """Delete one or more notification channels by NAME.

    Deleting >=2 at once requires -y/--yes (AI-friendly fail-fast).
    """
    # AI-friendly fail-fast: bulk delete must be explicitly confirmed.
    if len(names) >= 2 and not yes:
        click.echo(
            f"[ERR] Refusing to delete {len(names)} channels without confirmation; "
            f"pass -y/--yes to proceed.",
            err=True,
        )
        click.echo("Targets:", err=True)
        for n in names:
            click.echo(f"  - {n}", err=True)
        sys.exit(2)

    results = []
    failed = 0
    for n in names:
        code, data = _delete(f"/api/notifications/channels/{n}")
        ok = code == 200
        if not ok:
            failed += 1
        results.append({"name": n, "ok": ok, "status": code, "detail": data})

    if _json_or(ctx, {"results": results, "failed": failed}):
        sys.exit(1 if failed else 0)

    for r in results:
        if r["ok"]:
            click.echo(f"[OK] Deleted channel '{r['name']}'.")
        else:
            click.echo(f"[ERR] {r['name']}: {r['status']} {r['detail']}", err=True)
    if failed:
        sys.exit(1)


# ── notify rule ───────────────────────────────────────────────────────

@notify.group("rule")
def notify_rule():
    """Manage notification rules (event_type -> channel mapping)."""
    pass


@notify_rule.command("list")
@click.pass_context
def rule_list(ctx):
    """List all notification rules."""
    data = _get("/api/notifications/rules")
    if _json_or(ctx, data):
        return

    if not data:
        click.echo("[INFO] No rules configured.")
        return

    rid_w = max(len("RULE_ID"), *(len(r.get("rule_id", "")) for r in data))
    evt_w = max(len("EVENT_TYPE"), *(len(r.get("event_type", "")) for r in data))
    chn_w = max(len("CHANNEL"), *(len(r.get("channel_name", "")) for r in data))

    click.echo(f"{'RULE_ID'.ljust(rid_w)}  {'EVENT_TYPE'.ljust(evt_w)}  "
               f"{'CHANNEL'.ljust(chn_w)}  ENABLED")
    for r in data:
        rid = r.get("rule_id", "")
        evt = r.get("event_type", "")
        chn = r.get("channel_name", "")
        en = "yes" if r.get("enabled") else "no"
        click.echo(f"{rid.ljust(rid_w)}  {evt.ljust(evt_w)}  "
                   f"{chn.ljust(chn_w)}  {en}")
    click.echo(f"\n[OK] {len(data)} rule(s).")


@notify_rule.command("add")
@click.argument("event_type")
@click.argument("channel_name")
@click.option("--disable", is_flag=True,
              help="Create the rule in disabled state.")
@click.pass_context
def rule_add(ctx, event_type: str, channel_name: str, disable: bool):
    """Create a notification rule: EVENT_TYPE -> CHANNEL_NAME.

    The server assigns a rule_id (12-char hex). To update an existing rule,
    use --json output to capture the rule_id and re-POST with it (advanced).
    """
    body = {
        "event_type": event_type,
        "channel_name": channel_name,
        "enabled": not disable,
    }
    code, data = _post("/api/notifications/rules", body)
    if code != 200:
        _err(f"[ERR] {code}: {data}")
    if _json_or(ctx, data):
        return
    rid = (data or {}).get("rule_id", "?") if isinstance(data, dict) else "?"
    state = "disabled" if disable else "enabled"
    click.echo(f"[OK] Rule '{rid}' saved: {event_type} -> {channel_name} ({state}).")


@notify_rule.command("delete")
@click.argument("rule_ids", nargs=-1, required=True)
@click.option("-y", "--yes", is_flag=True,
              help="Confirm deletion (required when deleting >=2 rules).")
@click.pass_context
def rule_delete(ctx, rule_ids: tuple[str, ...], yes: bool):
    """Delete one or more notification rules by RULE_ID.

    Deleting >=2 at once requires -y/--yes (AI-friendly fail-fast).
    """
    if len(rule_ids) >= 2 and not yes:
        click.echo(
            f"[ERR] Refusing to delete {len(rule_ids)} rules without confirmation; "
            f"pass -y/--yes to proceed.",
            err=True,
        )
        click.echo("Targets:", err=True)
        for r in rule_ids:
            click.echo(f"  - {r}", err=True)
        sys.exit(2)

    results = []
    failed = 0
    for rid in rule_ids:
        code, data = _delete(f"/api/notifications/rules/{rid}")
        ok = code == 200
        if not ok:
            failed += 1
        results.append({"rule_id": rid, "ok": ok, "status": code, "detail": data})

    if _json_or(ctx, {"results": results, "failed": failed}):
        sys.exit(1 if failed else 0)

    for r in results:
        if r["ok"]:
            click.echo(f"[OK] Deleted rule '{r['rule_id']}'.")
        else:
            click.echo(f"[ERR] {r['rule_id']}: {r['status']} {r['detail']}", err=True)
    if failed:
        sys.exit(1)


# ── notify test ───────────────────────────────────────────────────────

@notify.command("test")
@click.option("--channel", default="default",
              help="Channel name to send test message to (default: 'default').")
@click.pass_context
def notify_test(ctx, channel: str):
    """Send a test notification to verify the channel webhook works."""
    code, data = _post(
        "/api/notifications/test",
        {"channel": channel},
        timeout=15,
    )
    if code == 200:
        if _json_or(ctx, data):
            return
        click.echo(f"[OK] Test message sent to channel '{channel}'.")
    else:
        _err(f"[ERR] {code}: {data}")
