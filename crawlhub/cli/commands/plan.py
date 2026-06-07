"""Scheduled-plan management commands.

Two sub-domains, mirroring `task` (group / single):

    plan group list
    plan group get      <group_id>
    plan group create   --name TEXT [--note TEXT]
    plan group rename   <group_id> <new_name>
    plan group delete   <group_id>... [-y] [--dry-run]

    plan job list       [--group GID]
    plan job get        <plan_id>
    plan job create     -f SPEC.json
    plan job update     <plan_id> -f SPEC.json
    plan job enable     <plan_id>
    plan job disable    <plan_id>
    plan job delete     <plan_id>... [-y] [--dry-run]
    plan job run        <plan_id> [--time ISO]
    plan job preview    <plan_id> [--time ISO]
    plan job runs       <plan_id> [--status X] [--limit N] [--offset M]

`plan job create / update` only accept a JSON spec via `-f` to keep
non-trivial trigger / step structures sane. Inline flags would invite users
to half-specify cron exprs and step refs on the command line.
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


# ── Generic helpers ───────────────────────────────────────────────────


def _fmt_time(ts):
    if not ts:
        return "-"
    if isinstance(ts, str):
        return ts
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
    except Exception:
        return str(ts)


def _truncate(s: Optional[str], n: int) -> str:
    if not s:
        return "-"
    return s if len(s) <= n else s[: n - 3] + "..."


def _read_spec_file(path: str) -> dict:
    """Load a JSON spec file into a dict. Exits with [ERR] on bad input."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            spec = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        click.echo(f"[ERR] Cannot read --file: {e}", err=True)
        raise SystemExit(1)
    if not isinstance(spec, dict):
        click.echo("[ERR] Plan spec must be a JSON object.", err=True)
        raise SystemExit(1)
    return spec


def _http_json(method: str, url: str, **kwargs) -> dict:
    """Thin wrapper that uniformly maps network/HTTP errors to [ERR] + exit 1.

    Returns parsed JSON on 2xx; raises SystemExit otherwise.
    """
    try:
        resp = httpx.request(method, url, timeout=kwargs.pop("timeout", 15), **kwargs)
    except httpx.RequestError as e:
        click.echo(f"[ERR] network: {e}", err=True)
        raise SystemExit(1)
    if resp.status_code == 404:
        click.echo(f"[ERR] not found: {url}", err=True)
        raise SystemExit(1)
    if resp.status_code >= 400:
        click.echo(f"[ERR] {resp.status_code}: {resp.text}", err=True)
        raise SystemExit(1)
    if not resp.text:
        return {}
    try:
        return resp.json()
    except json.JSONDecodeError:
        return {"_raw": resp.text}


# ── plan top-level group ──────────────────────────────────────────────


@click.group()
def plan():
    """Scheduled-plan management."""
    pass


# ─────────────────────────────────────────────────────────────────────
#                          plan group sub-commands
# ─────────────────────────────────────────────────────────────────────


@plan.group("group")
def plan_group():
    """Plan-group CRUD."""
    pass


@plan_group.command("list")
@click.option("--json", "json_output", is_flag=True, help="Output raw JSON.")
@click.pass_context
def plan_group_list(ctx, json_output: bool):
    """List all plan groups."""
    ensure_daemon()
    base_url = get_base_url()
    groups = _http_json("GET", f"{base_url}/api/plan-groups")
    # API returns a list directly.
    if isinstance(groups, dict):
        groups = groups.get("groups") or groups.get("items") or []

    json_out = json_output or (ctx.obj and ctx.obj.get("json_output"))
    if json_out:
        click.echo(json.dumps(groups, indent=2, ensure_ascii=False))
        return
    if not groups:
        click.echo("[INFO] No plan groups.")
        return

    headers = ["#", "group_id", "name", "note", "created_at"]
    rows = []
    for i, g in enumerate(groups, 1):
        rows.append([
            str(i),
            str(g.get("group_id", "-")),
            _truncate(g.get("name"), 24),
            _truncate(g.get("note"), 30),
            _fmt_time(g.get("created_at")),
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
    click.echo(f"\n[INFO] {len(groups)} group(s).")


@plan_group.command("get")
@click.argument("group_id")
@click.option("--json", "json_output", is_flag=True, help="Output raw JSON.")
@click.pass_context
def plan_group_get(ctx, group_id: str, json_output: bool):
    """Show a single plan group with its plans."""
    ensure_daemon()
    base_url = get_base_url()
    # No dedicated GET-one endpoint; filter from list.
    groups = _http_json("GET", f"{base_url}/api/plan-groups")
    if isinstance(groups, dict):
        groups = groups.get("groups") or []
    match = next((g for g in groups if g.get("group_id") == group_id), None)
    if match is None:
        click.echo(f"[ERR] Group {group_id} not found.", err=True)
        raise SystemExit(1)
    plans = _http_json("GET", f"{base_url}/api/plans", params={"group_id": group_id})

    out = {"group": match, "plans": plans if isinstance(plans, list) else []}
    json_out = json_output or (ctx.obj and ctx.obj.get("json_output"))
    if json_out:
        click.echo(json.dumps(out, indent=2, ensure_ascii=False))
        return
    click.echo(f"  group_id   : {match.get('group_id')}")
    click.echo(f"  name       : {match.get('name')}")
    click.echo(f"  note       : {match.get('note') or '-'}")
    click.echo(f"  created_at : {_fmt_time(match.get('created_at'))}")
    click.echo(f"  jobs ({len(out['plans'])}):")
    for p in out["plans"]:
        click.echo(f"    - {p.get('plan_id')}  enabled={p.get('enabled')}  "
                   f"name={p.get('name')}")


@plan_group.command("create")
@click.option("--name", required=True, help="Group name.")
@click.option("--note", default=None, help="Optional note.")
@click.option("--json", "json_output", is_flag=True, help="Output raw JSON.")
@click.pass_context
def plan_group_create(ctx, name: str, note: Optional[str], json_output: bool):
    """Create a new plan group."""
    ensure_daemon()
    base_url = get_base_url()
    body = {"name": name}
    if note is not None:
        body["note"] = note
    g = _http_json("POST", f"{base_url}/api/plan-groups", json=body)
    json_out = json_output or (ctx.obj and ctx.obj.get("json_output"))
    if json_out:
        click.echo(json.dumps(g, indent=2, ensure_ascii=False))
    else:
        click.echo(f"[OK] Group created: {g.get('group_id')} (name={g.get('name')})")


@plan_group.command("rename")
@click.argument("group_id")
@click.argument("new_name")
@click.option("--json", "json_output", is_flag=True, help="Output raw JSON.")
@click.pass_context
def plan_group_rename(ctx, group_id: str, new_name: str, json_output: bool):
    """Rename a plan group.

    Note: groups carry a `note` field on the backend, but it is intentionally
    not exposed via CLI. The web UI is the only surface that edits notes.
    """
    if not new_name or not new_name.strip():
        click.echo("[ERR] NEW_NAME must be non-empty.", err=True)
        raise SystemExit(2)

    ensure_daemon()
    base_url = get_base_url()
    g = _http_json("PATCH", f"{base_url}/api/plan-groups/{group_id}",
                   json={"name": new_name})
    json_out = json_output or (ctx.obj and ctx.obj.get("json_output"))
    if json_out:
        click.echo(json.dumps(g, indent=2, ensure_ascii=False))
    else:
        click.echo(f"[OK] Group {group_id} renamed to '{new_name}'.")


# --- bulk delete for plan groups ------------------------------------------------


def _group_resolver(_filters: dict) -> list:
    # plan group delete is ID-only by design — no filter mode.
    raise click.ClickException("filter-mode delete is not supported for plan groups.")


def _group_fetch_by_id(gid: str):
    base_url = get_base_url()
    try:
        resp = httpx.get(f"{base_url}/api/plan-groups", timeout=10)
    except httpx.RequestError:
        return None
    if resp.status_code != 200:
        return None
    items = resp.json()
    if isinstance(items, dict):
        items = items.get("groups") or []
    for g in items:
        if g.get("group_id") == gid:
            return g
    return None


_GROUP_BULK_COLUMNS = (
    ("group_id", lambda g: str(g.get("group_id", "?"))),
    ("name", lambda g: _truncate(g.get("name"), 24)),
    ("note", lambda g: _truncate(g.get("note"), 30)),
)


@plan_group.command("delete")
@click.argument("group_ids", nargs=-1, required=True)
@click.option("--cascade", is_flag=True,
              help="Cascade-delete disabled child plans (passes ?confirm=true).")
@bulk_options
@click.pass_context
def plan_group_delete(ctx, group_ids, cascade,
                      yes, dry_run):
    """Delete plan groups.

    The backend rejects deletion if a group has any *enabled* plans (HTTP 422).
    For groups containing only disabled plans, pass --cascade to wipe them.
    """
    if not group_ids:
        click.echo("[ERR] Provide at least one GROUP_ID.", err=True)
        raise SystemExit(2)

    # Single-target hard delete also requires -y per AI-friendly CLI rule.
    if len(group_ids) == 1 and not yes and not dry_run:
        click.echo(
            "[ERR] plan group delete is irreversible; "
            "pass -y/--yes to confirm or --dry-run to preview.",
            err=True,
        )
        raise SystemExit(2)

    base_url = get_base_url()

    def action(g: dict) -> BulkOutcome:
        if g.get("_missing"):
            return BulkOutcome(BulkResult.SKIP, "not found")
        gid = g.get("group_id")
        params = {"confirm": "true"} if cascade else {}
        try:
            r = httpx.delete(f"{base_url}/api/plan-groups/{gid}",
                             params=params, timeout=15)
        except httpx.RequestError as e:
            return BulkOutcome(BulkResult.FAIL, f"network: {e}")
        if r.status_code == 200:
            return BulkOutcome(BulkResult.OK, "deleted")
        if r.status_code == 404:
            return BulkOutcome(BulkResult.SKIP, "not found")
        if r.status_code == 422:
            return BulkOutcome(BulkResult.FAIL, "has enabled plans (disable first)")
        if r.status_code == 409:
            return BulkOutcome(BulkResult.FAIL, "has disabled plans (use --cascade)")
        return BulkOutcome(BulkResult.FAIL, f"{r.status_code} {r.text}")

    spec = BulkSpec(
        resolver=_group_resolver,
        fetch_by_id=_group_fetch_by_id,
        action=action,
        columns=_GROUP_BULK_COLUMNS,
        entity_name="plan group",
        id_field="group_id",
        ensure_ready=ensure_daemon,
    )
    sys.exit(run_bulk(spec, group_ids, {},
                      action_label="DELETE", yes=yes, dry_run=dry_run))


# ─────────────────────────────────────────────────────────────────────
#                          plan job sub-commands
# ─────────────────────────────────────────────────────────────────────


@plan.group("job")
def plan_job():
    """Plan (scheduled job) CRUD + run/preview/runs."""
    pass


def _print_plan_summary(p: dict) -> None:
    triggers = p.get("triggers") or []
    steps = p.get("steps") or []
    click.echo(f"  plan_id    : {p.get('plan_id', '-')}")
    click.echo(f"  group_id   : {p.get('group_id', '-')}")
    click.echo(f"  name       : {p.get('name', '-')}")
    click.echo(f"  enabled    : {bool(p.get('enabled'))}")
    click.echo(f"  timezone   : {p.get('timezone', '-')}")
    click.echo(f"  notify_on_fire_fail : {bool(p.get('notify_on_fire_fail'))}")
    click.echo(f"  note       : {p.get('note') or '-'}")
    click.echo(f"  triggers ({len(triggers)}):")
    for t in triggers:
        click.echo(f"    - [{t.get('kind')}] {t.get('expr')}  enabled={t.get('enabled')}  "
                   f"id={t.get('trigger_id')}")
    click.echo(f"  steps ({len(steps)}):")
    for s in steps:
        click.echo(f"    - {s.get('request_kind')}  {s.get('platform')}/{s.get('task_type')}  "
                   f"id={s.get('step_id')}")


@plan_job.command("list")
@click.option("--group", "group_id", default=None, help="Filter by group_id.")
@click.option("--json", "json_output", is_flag=True, help="Output raw JSON.")
@click.pass_context
def plan_job_list(ctx, group_id: Optional[str], json_output: bool):
    """List plans (optionally filtered by group)."""
    ensure_daemon()
    base_url = get_base_url()
    params = {}
    if group_id:
        params["group_id"] = group_id
    plans = _http_json("GET", f"{base_url}/api/plans", params=params)
    if not isinstance(plans, list):
        plans = []

    json_out = json_output or (ctx.obj and ctx.obj.get("json_output"))
    if json_out:
        click.echo(json.dumps(plans, indent=2, ensure_ascii=False))
        return
    if not plans:
        click.echo("[INFO] No plans.")
        return

    headers = ["#", "plan_id", "group_id", "name", "enabled", "triggers", "steps", "note"]
    rows = []
    for i, p in enumerate(plans, 1):
        triggers = p.get("triggers") or []
        steps = p.get("steps") or []
        rows.append([
            str(i),
            str(p.get("plan_id", "-")),
            str(p.get("group_id", "-")),
            _truncate(p.get("name"), 22),
            "Y" if p.get("enabled") else "N",
            str(len(triggers)),
            str(len(steps)),
            _truncate(p.get("note"), 24),
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
    click.echo(f"\n[INFO] {len(plans)} plan(s).")


@plan_job.command("get")
@click.argument("plan_id")
@click.option("--json", "json_output", is_flag=True, help="Output raw JSON.")
@click.pass_context
def plan_job_get(ctx, plan_id: str, json_output: bool):
    """Show a plan in full (triggers + steps)."""
    ensure_daemon()
    base_url = get_base_url()
    p = _http_json("GET", f"{base_url}/api/plans/{plan_id}")
    json_out = json_output or (ctx.obj and ctx.obj.get("json_output"))
    if json_out:
        click.echo(json.dumps(p, indent=2, ensure_ascii=False))
    else:
        _print_plan_summary(p)


@plan_job.command("create")
@click.option("--file", "-f", "spec_file", required=True, type=click.Path(exists=True),
              help="JSON spec file (PlanWriteRequest shape).")
@click.option("--json", "json_output", is_flag=True, help="Output raw JSON.")
@click.pass_context
def plan_job_create(ctx, spec_file: str, json_output: bool):
    """Create a plan from a JSON spec file.

    \b
    Required spec fields:
      group_id, name, triggers[], steps[]
    Optional:
      enabled (bool, default false), timezone (default "Asia/Shanghai"),
      notify_on_fire_fail (bool), note

    \b
    Example spec.json:
      {
        "group_id": "g_abc123",
        "name": "Daily steam pull",
        "enabled": true,
        "timezone": "Asia/Shanghai",
        "notify_on_fire_fail": true,
        "triggers": [
          {"kind": "cron", "expr": "0 9 * * *", "enabled": true}
        ],
        "steps": [
          {"request_kind": "task", "platform": "steam", "task_type": "get_game_detail",
           "request_payload": {"app_id": "730", "cc": "us", "language": "schinese"}}
        ]
      }
    """
    spec = _read_spec_file(spec_file)
    ensure_daemon()
    base_url = get_base_url()
    p = _http_json("POST", f"{base_url}/api/plans", json=spec, timeout=30)
    json_out = json_output or (ctx.obj and ctx.obj.get("json_output"))
    if json_out:
        click.echo(json.dumps(p, indent=2, ensure_ascii=False))
    else:
        click.echo(f"[OK] Plan created: {p.get('plan_id')}")
        _print_plan_summary(p)


@plan_job.command("update")
@click.argument("plan_id")
@click.option("--file", "-f", "spec_file", required=True, type=click.Path(exists=True),
              help="JSON spec file (PlanWriteRequest shape; full replace).")
@click.option("--json", "json_output", is_flag=True, help="Output raw JSON.")
@click.pass_context
def plan_job_update(ctx, plan_id: str, spec_file: str, json_output: bool):
    """Replace a plan wholesale from a JSON spec.

    NOTE: This is a PUT — the spec must contain the full plan (triggers/steps
    are replaced entirely). To toggle enabled-only, use `enable` / `disable`.
    """
    spec = _read_spec_file(spec_file)
    ensure_daemon()
    base_url = get_base_url()
    p = _http_json("PUT", f"{base_url}/api/plans/{plan_id}", json=spec, timeout=30)
    json_out = json_output or (ctx.obj and ctx.obj.get("json_output"))
    if json_out:
        click.echo(json.dumps(p, indent=2, ensure_ascii=False))
    else:
        click.echo(f"[OK] Plan {plan_id} updated.")
        _print_plan_summary(p)


def _patch_plan_enabled(plan_id: str, enabled: bool) -> dict:
    base_url = get_base_url()
    return _http_json("PATCH", f"{base_url}/api/plans/{plan_id}/enabled",
                      json={"enabled": enabled})


@plan_job.command("enable")
@click.argument("plan_id")
@click.option("--json", "json_output", is_flag=True, help="Output raw JSON.")
@click.pass_context
def plan_job_enable(ctx, plan_id: str, json_output: bool):
    """Enable a plan (scheduler will start firing it)."""
    ensure_daemon()
    p = _patch_plan_enabled(plan_id, True)
    json_out = json_output or (ctx.obj and ctx.obj.get("json_output"))
    if json_out:
        click.echo(json.dumps(p, indent=2, ensure_ascii=False))
    else:
        click.echo(f"[OK] Plan {plan_id} enabled.")


@plan_job.command("disable")
@click.argument("plan_id")
@click.option("--json", "json_output", is_flag=True, help="Output raw JSON.")
@click.pass_context
def plan_job_disable(ctx, plan_id: str, json_output: bool):
    """Disable a plan (scheduler stops firing it)."""
    ensure_daemon()
    p = _patch_plan_enabled(plan_id, False)
    json_out = json_output or (ctx.obj and ctx.obj.get("json_output"))
    if json_out:
        click.echo(json.dumps(p, indent=2, ensure_ascii=False))
    else:
        click.echo(f"[OK] Plan {plan_id} disabled.")


# --- bulk delete for plans -----------------------------------------------------


def _plan_resolver(filters: dict) -> list:
    base_url = get_base_url()
    params = {}
    if filters.get("group"):
        params["group_id"] = filters["group"]
    try:
        resp = httpx.get(f"{base_url}/api/plans", params=params, timeout=10)
    except httpx.RequestError as e:
        raise click.ClickException(f"network: {e}")
    if resp.status_code != 200:
        raise click.ClickException(f"{resp.status_code}: {resp.text}")
    plans = resp.json()
    return plans if isinstance(plans, list) else []


def _plan_fetch_by_id(pid: str):
    base_url = get_base_url()
    try:
        resp = httpx.get(f"{base_url}/api/plans/{pid}", timeout=10)
    except httpx.RequestError:
        return None
    if resp.status_code == 200:
        return resp.json()
    return None


_PLAN_BULK_COLUMNS = (
    ("plan_id", lambda p: str(p.get("plan_id", "?"))),
    ("group_id", lambda p: str(p.get("group_id", "-"))),
    ("name", lambda p: _truncate(p.get("name"), 22)),
    ("enabled", lambda p: "Y" if p.get("enabled") else "N"),
)


@plan_job.command("delete")
@click.argument("plan_ids", nargs=-1)
@click.option("--group", default=None, help="Filter: delete every plan in GROUP_ID.")
@bulk_options
@click.pass_context
def plan_job_delete(ctx, plan_ids, group,
                    yes, dry_run):
    """Delete plans permanently (scheduler jobs are dropped first).

    Single  : `plan job delete p_xxx -y`
    Multi   : `plan job delete p_a p_b p_c -y`
    Filter  : `plan job delete --group g_xxx -y`
    """
    if plan_ids and not yes and not dry_run:
        click.echo(
            "[ERR] plan delete is irreversible; "
            "pass -y/--yes to confirm or --dry-run to preview.",
            err=True,
        )
        raise SystemExit(2)

    base_url = get_base_url()

    def action(p: dict) -> BulkOutcome:
        if p.get("_missing"):
            return BulkOutcome(BulkResult.SKIP, "not found")
        pid = p.get("plan_id")
        try:
            r = httpx.delete(f"{base_url}/api/plans/{pid}", timeout=15)
        except httpx.RequestError as e:
            return BulkOutcome(BulkResult.FAIL, f"network: {e}")
        if r.status_code == 200:
            return BulkOutcome(BulkResult.OK, "deleted")
        if r.status_code == 404:
            return BulkOutcome(BulkResult.SKIP, "not found")
        return BulkOutcome(BulkResult.FAIL, f"{r.status_code} {r.text}")

    spec = BulkSpec(
        resolver=_plan_resolver,
        fetch_by_id=_plan_fetch_by_id,
        action=action,
        columns=_PLAN_BULK_COLUMNS,
        entity_name="plan",
        id_field="plan_id",
        ensure_ready=ensure_daemon,
    )
    filters = {"group": group}
    sys.exit(run_bulk(spec, plan_ids, filters,
                      action_label="DELETE", yes=yes, dry_run=dry_run))


# --- run / preview / runs ------------------------------------------------------


@plan_job.command("run")
@click.argument("plan_id")
@click.option("--time", "instance_time", default=None,
              help="ISO-8601 instance_time (defaults to now in plan tz).")
@click.option("--json", "json_output", is_flag=True, help="Output raw JSON.")
@click.pass_context
def plan_job_run(ctx, plan_id: str, instance_time: Optional[str], json_output: bool):
    """Manually fire a plan now (or as-of a given instance_time)."""
    ensure_daemon()
    base_url = get_base_url()
    body = {}
    if instance_time:
        body["instance_time"] = instance_time
    data = _http_json("POST", f"{base_url}/api/plans/{plan_id}/run",
                      json=body, timeout=30)
    json_out = json_output or (ctx.obj and ctx.obj.get("json_output"))
    if json_out:
        click.echo(json.dumps(data, indent=2, ensure_ascii=False))
        return

    submitted = data.get("submitted")
    if isinstance(submitted, list):
        click.echo(f"[OK] Plan {plan_id} fired; {len(submitted)} task(s) submitted.")
        for item in submitted:
            if isinstance(item, dict):
                click.echo(f"  - step={item.get('step_id', '?')}  "
                           f"task_id={item.get('task_id', '?')}  "
                           f"status={item.get('status', '-')}")
            else:
                click.echo(f"  - {item}")
    else:
        click.echo(f"[OK] Plan {plan_id} fired.")
        click.echo(json.dumps(data, indent=2, ensure_ascii=False))


@plan_job.command("preview")
@click.argument("plan_id")
@click.option("--time", "instance_time", default=None,
              help="ISO-8601 instance_time (defaults to now in plan tz).")
@click.option("--json", "json_output", is_flag=True, help="Output raw JSON.")
@click.pass_context
def plan_job_preview(ctx, plan_id: str, instance_time: Optional[str], json_output: bool):
    """Render every step's templates without submitting any task."""
    ensure_daemon()
    base_url = get_base_url()
    params = {}
    if instance_time:
        params["instance_time"] = instance_time
    data = _http_json("GET", f"{base_url}/api/plans/{plan_id}/preview",
                      params=params, timeout=30)
    # Preview is structural; just dump JSON. The shape isn't strictly fixed.
    click.echo(json.dumps(data, indent=2, ensure_ascii=False))


@plan_job.command("runs")
@click.argument("plan_id")
@click.option("--status", default=None, help="Filter by task status.")
@click.option("--limit", default=200, type=int, show_default=True)
@click.option("--offset", default=0, type=int, show_default=True)
@click.option("--json", "json_output", is_flag=True, help="Output raw JSON.")
@click.pass_context
def plan_job_runs(ctx, plan_id: str, status: Optional[str],
                  limit: int, offset: int, json_output: bool):
    """List tasks fired by this plan (manual + scheduled)."""
    ensure_daemon()
    base_url = get_base_url()
    params = {"limit": limit, "offset": offset}
    if status:
        params["status"] = status
    tasks = _http_json("GET", f"{base_url}/api/plans/{plan_id}/tasks", params=params)
    if not isinstance(tasks, list):
        tasks = tasks.get("tasks", []) if isinstance(tasks, dict) else []

    json_out = json_output or (ctx.obj and ctx.obj.get("json_output"))
    if json_out:
        click.echo(json.dumps(tasks, indent=2, ensure_ascii=False))
        return
    if not tasks:
        click.echo("[INFO] No runs.")
        return

    headers = ["#", "task_id", "platform", "action", "status", "created_at"]
    rows = []
    for i, t in enumerate(tasks, 1):
        rows.append([
            str(i),
            str(t.get("task_id", "-"))[:12],
            _truncate(t.get("platform"), 12),
            _truncate(t.get("task_type"), 22),
            str(t.get("status", "-")),
            _fmt_time(t.get("created_at")),
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
    click.echo(f"\n[INFO] {len(tasks)} run(s) (offset={offset}, limit={limit})")
