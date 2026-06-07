"""Task management commands."""

import csv
import json
import sys
import time
from datetime import datetime, timezone
from typing import Any, Optional

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


def _print_task_detail(t: dict) -> None:
    """Pretty-print a single task dict."""
    click.echo(f"  task_id    : {t.get('task_id', '-')}")
    click.echo(f"  platform   : {t.get('platform', '-')}")
    click.echo(f"  action     : {t.get('task_type', '-')}")
    click.echo(f"  status     : {t.get('status', '-')}")
    click.echo(f"  progress   : {t.get('progress', 0) * 100:.1f}%")
    click.echo(f"  records    : {t.get('record_count', 0)}")
    click.echo(f"  created_at : {_fmt_time(t.get('created_at'))}")
    click.echo(f"  started_at : {_fmt_time(t.get('started_at'))}")
    click.echo(f"  finished_at: {_fmt_time(t.get('finished_at'))}")
    note = t.get("note") or "-"
    click.echo(f"  note       : {note}")

    # Show both param flavors:
    #   * logic_param    -- the original POST body (with items_from kept)
    #   * snapshot_param -- the executable view (defaults filled, items
    #                       resolved). retry uses this one.
    for label, key in (("logic_param   ", "logic_param"), ("snapshot_param", "snapshot_param")):
        val = t.get(key)
        if val:
            val_str = json.dumps(val, ensure_ascii=False)
            if len(val_str) > 200:
                val_str = val_str[:200] + "..."
            click.echo(f"  {label}: {val_str}")


def _parse_items_file(path: str) -> list:
    """Parse items file: JSONL / JSON array / CSV (first column)."""
    with open(path, "r", encoding="utf-8") as f:
        content = f.read().strip()

    # Try JSON array
    if content.startswith("["):
        try:
            data = json.loads(content)
            if isinstance(data, list):
                return [str(x) for x in data]
        except json.JSONDecodeError:
            pass

    # Try JSONL
    lines = [l.strip() for l in content.splitlines() if l.strip()]
    try:
        parsed = [json.loads(l) for l in lines]
        # If each line is a scalar, use as-is; if dict, take first value
        result = []
        for p in parsed:
            if isinstance(p, (str, int, float)):
                result.append(str(p))
            elif isinstance(p, dict):
                result.append(str(next(iter(p.values()))))
            else:
                result.append(str(p))
        return result
    except json.JSONDecodeError:
        pass

    # Try CSV (first column)
    try:
        reader = csv.reader(content.splitlines())
        rows = list(reader)
        if rows:
            # Skip header if first cell looks like a header (non-numeric)
            start = 0
            if rows and not rows[0][0].strip().lstrip("-").isdigit():
                start = 1
            return [row[0].strip() for row in rows[start:] if row and row[0].strip()]
    except Exception:
        pass

    # Fallback: one item per line
    return lines


# ── task group ────────────────────────────────────────────────────────

@click.group()
def task():
    """Task management commands."""
    pass


# ── submit group ──────────────────────────────────────────────────────

@task.group("submit")
def task_submit():
    """Submit tasks (single or batch)."""
    pass


# ── Dynamic help + param validation for submit single ────────────────

def _fetch_platform_info(platform_name: str) -> dict | None:
    """Fetch a single platform's metadata from daemon. Returns None on failure."""
    try:
        base_url = get_base_url()
        resp = httpx.get(f"{base_url}/api/platforms", timeout=10)
        if resp.status_code != 200:
            return None
        platforms = resp.json().get("platforms", [])
        return next((p for p in platforms if p.get("platform") == platform_name), None)
    except Exception:
        return None


def _show_platform_actions(platform_name: str) -> None:
    """Print available actions for a platform (used by dynamic --help)."""
    plat = _fetch_platform_info(platform_name)
    if plat is None:
        click.echo(f"[ERR] Cannot fetch platform info for '{platform_name}'. Is the daemon running?")
        return

    display_name = plat.get("display_name", platform_name)
    click.echo(f"\nPlatform: {display_name} ({platform_name})")
    desc = plat.get("description", "")
    if desc:
        click.echo(f"  {desc}")
    click.echo("")

    actions = plat.get("actions", [])
    if not actions:
        click.echo("  (no actions available)")
        return

    click.echo("Available actions:")
    for a in actions:
        name = a.get("name", "")
        disp = a.get("display_name", "")
        adesc = a.get("description", "")
        label = f"{disp}" if disp and disp != name else ""
        click.echo(f"  {name:30s}  {label}")
        if adesc:
            click.echo(f"  {'':30s}  {adesc[:80]}")

    click.echo(f"\nShow action parameters:")
    click.echo(f"  crawlhub task submit single {platform_name} <action> --help")


def _show_action_schema(platform_name: str, action_name: str) -> None:
    """Print detailed parameter schema for a (platform, action) (used by dynamic --help)."""
    plat = _fetch_platform_info(platform_name)
    if plat is None:
        click.echo(f"[ERR] Cannot fetch platform info for '{platform_name}'. Is the daemon running?")
        return

    display_name = plat.get("display_name", platform_name)
    action = next((a for a in plat.get("actions", []) if a.get("name") == action_name), None)
    if action is None:
        avail = ", ".join(a.get("name", "") for a in plat.get("actions", [])) or "(none)"
        click.echo(f"[ERR] Action '{action_name}' not found on {platform_name}.")
        click.echo(f"  Available: {avail}")
        return

    adesc = action.get("description", "")
    click.echo(f"\nAction: {display_name}.{action_name}")
    if adesc:
        click.echo(f"  {adesc}")
    click.echo("")

    schema = action.get("schema", {}) or {}
    props: dict = schema.get("properties", {}) or {}
    required: list = schema.get("required", []) or []
    required_set = set(required)

    if not props:
        click.echo("  (no parameters required)")
        click.echo(f"\nSubmit: crawlhub task submit single {platform_name} {action_name}")
        return

    req_keys = [k for k in props.keys() if k in required_set]
    opt_keys = [k for k in props.keys() if k not in required_set]

    def _print_param(key: str, spec: dict) -> None:
        type_str = _format_type(spec)
        req_mark = " *required*" if key in required_set else ""
        line = f"  {key}  ({type_str}){req_mark}"
        if "default" in spec:
            line += f"  [default: {spec['default']!r}]"
        click.echo(line)
        d = (spec.get("description") or spec.get("title") or "").strip()
        if d:
            click.echo(f"      {d}")
        labels = spec.get("enum_labels")
        if labels and "enum" in spec:
            pairs = ", ".join(f"{v}={lbl}" for v, lbl
                              in zip(spec["enum"], labels))
            click.echo(f"      enum: {pairs}")

    if req_keys:
        click.echo("Required parameters:")
        for k in req_keys:
            _print_param(k, props[k])
        click.echo("")

    if opt_keys:
        click.echo("Optional parameters:")
        for k in opt_keys:
            _print_param(k, props[k])
        click.echo("")

    example = _example_payload(schema)
    payload = json.dumps(example, ensure_ascii=False)
    click.echo("Example:")
    click.echo(f"  crawlhub task submit single {platform_name} {action_name} -d '{payload}'")


class SubmitSingleCommand(click.Command):
    """Custom command class that provides dynamic --help based on partial arguments.

    - ``crawlhub task submit single bilibili --help``       -> show bilibili actions
    - ``crawlhub task submit single bilibili action --help`` -> show action schema
    - ``crawlhub task submit single --help``                 -> show default help
    """

    def parse_args(self, ctx, args):
        # Detect --help before Click's default handling
        has_help = "--help" in args or "-h" in args
        if not has_help:
            return super().parse_args(ctx, args)

        # Extract positional args from the raw arg list (skip flags)
        positional = [a for a in args if not a.startswith("-")]
        platform_val = positional[0] if len(positional) >= 1 else None
        action_val = positional[1] if len(positional) >= 2 else None

        if platform_val and action_val:
            _show_action_schema(platform_val, action_val)
            ctx.exit()
        elif platform_val:
            _show_platform_actions(platform_val)
            ctx.exit()
        else:
            # No platform given, fall through to default help
            return super().parse_args(ctx, args)


def _validate_params_against_schema(platform: str, action: str, params: dict,
                                     exclude_keys: set[str] | None = None) -> list[str]:
    """Validate user params against the action's input_schema.

    Returns a list of error messages. Empty list = valid.
    Checks: required fields present, enum values in range.
    exclude_keys: keys to skip during required-check (e.g. item_key in batch mode).
    """
    errors: list[str] = []
    _exclude = exclude_keys or set()

    try:
        base_url = get_base_url()
        resp = httpx.get(
            f"{base_url}/api/actions/{platform}/{action}/schema",
            timeout=10,
        )
        if resp.status_code != 200:
            # Fallback: try the platforms list endpoint
            plat = _fetch_platform_info(platform)
            if plat is None:
                return errors  # can't validate without schema
            act = next((a for a in plat.get("actions", []) if a.get("name") == action), None)
            if act is None:
                return errors
            schema = act.get("schema", {}) or {}
        else:
            data = resp.json()
            schema = data.get("input_schema", {}) or {}
    except Exception:
        return errors  # network error, skip validation

    props: dict = schema.get("properties", {}) or {}
    required: list = schema.get("required", []) or []

    # 1. Check required fields (skip those with defaults — daemon will fill them)
    missing = [k for k in required
               if k not in params and k not in _exclude
               and "default" not in (props.get(k) or {})]
    if missing:
        for k in missing:
            spec = props.get(k, {})
            desc = spec.get("description") or spec.get("title") or ""
            type_str = _format_type(spec)
            errors.append(f"Missing required parameter: {k} ({type_str})" +
                          (f" -- {desc}" if desc else ""))

    # 2. Check enum values
    for key, value in params.items():
        spec = props.get(key)
        if spec is None:
            continue  # unknown param, let daemon decide
        if "enum" in spec and value not in spec["enum"]:
            allowed = spec["enum"]
            labels = spec.get("enum_labels")
            if labels:
                pairs = ", ".join(f"{v}={lbl}" for v, lbl in zip(allowed, labels))
                errors.append(
                    f"Invalid value for '{key}': {value!r}. "
                    f"Allowed: {pairs}"
                )
            else:
                errors.append(
                    f"Invalid value for '{key}': {value!r}. "
                    f"Allowed: {allowed}"
                )

    return errors


@task_submit.command("single", cls=SubmitSingleCommand)
@click.argument("platform", required=False, default=None)
@click.argument("action", required=False, default=None)
@click.option("--data", "-d", default=None, help="Inline JSON params, e.g. '{\"key\":\"val\"}'")
@click.option("--params-file", "--file", "-f", "input_file", default=None, type=click.Path(exists=True),
              help="JSON file with task params (alias: --file).")
@click.option("--json", "json_output", is_flag=True, help="Output raw JSON")
@click.pass_context
def submit_single(ctx, platform: str, action: str, data: str, input_file: str, json_output: bool):
    """Submit a single crawl task.

    Pass params as JSON via --data (-d), --file (-f), or stdin.

    \b
    Quick help (no submission):
      crawlhub task submit single bilibili --help
      crawlhub task submit single bilibili scrape_reviews --help

    \b
    Submit with params:
      crawlhub task submit single steam scrape_reviews -d '{"app_id":"730"}'
      crawlhub task submit single steam scrape_reviews --file params.json
    """
    # Validate required positional args (now optional for --help flow)
    if not platform:
        click.echo("[ERR] PLATFORM is required. Usage: crawlhub task submit single <platform> <action> -d '<json>'")
        click.echo("  Show available platforms: crawlhub platform list")
        raise SystemExit(2)
    if not action:
        _show_platform_actions(platform)
        raise SystemExit(2)

    if data and input_file:
        click.echo("[ERR] --data and --file are mutually exclusive.")
        raise SystemExit(1)

    params: dict = {}
    if data:
        # Windows cmd.exe does not strip single quotes:
        #   --data '{"a":1}'  arrives as the literal string '{"a":1}' (with quotes),
        #   which is not valid JSON. Strip them.
        if data.startswith("'") and data.endswith("'"):
            data = data[1:-1]
        try:
            params = json.loads(data)
        except json.JSONDecodeError as e:
            click.echo(f"[ERR] Invalid JSON in --data: {e}")
            click.echo(f"  Received: {data[:120]}")
            click.echo(f"  Hint: use --file to avoid shell quoting issues.")
            raise SystemExit(1)
    elif input_file:
        try:
            with open(input_file, "r", encoding="utf-8") as f:
                params = json.load(f)
        except UnicodeDecodeError:
            # Windows cmd.exe `echo` outputs in GBK; try GBK as fallback.
            try:
                with open(input_file, "r", encoding="gbk") as f:
                    params = json.load(f)
            except (json.JSONDecodeError, OSError) as e:
                click.echo(f"[ERR] Cannot read --file: {e}")
                raise SystemExit(1)
        except (json.JSONDecodeError, OSError) as e:
            click.echo(f"[ERR] Cannot read --file: {e}")
            raise SystemExit(1)
    elif not sys.stdin.isatty():
        try:
            params = json.loads(sys.stdin.read())
        except json.JSONDecodeError as e:
            click.echo(f"[ERR] Invalid JSON from stdin: {e}")
            raise SystemExit(1)

    # ── Validate params against action schema ──
    # This catches: missing required fields, enum values out of range.
    # Done before daemon submission so the user gets clear, immediate feedback.
    if params:
        validation_errors = _validate_params_against_schema(platform, action, params)
        if validation_errors:
            click.echo(f"[ERR] Parameter validation failed for {platform}.{action}:")
            for err in validation_errors:
                click.echo(f"  - {err}")
            click.echo(f"\n  Check schema: crawlhub task submit single {platform} {action} --help")
            raise SystemExit(1)

    # Block submission with no params if the action requires any input.
    # (If params is empty and the action has required fields, we can't submit.)
    if not params:
        try:
            base_url = get_base_url()
            resp = httpx.get(
                f"{base_url}/api/actions/{platform}/{action}/schema", timeout=10)
            if resp.status_code == 200:
                schema = resp.json().get("input_schema", {}) or {}
            else:
                # Fallback to platforms list
                plat = _fetch_platform_info(platform)
                act = None
                if plat:
                    act = next((a for a in plat.get("actions", [])
                                if a.get("name") == action), None)
                schema = (act or {}).get("schema", {}) or {}
            required = schema.get("required", []) or []
            if required:
                example = _example_payload(schema)
                payload = json.dumps(example, ensure_ascii=False)
                click.echo(
                    f"[ERR] {platform} {action} requires parameters: "
                    f"{', '.join(required)}"
                )
                click.echo(f"  Example: crawlhub task submit single {platform} {action} -d '{payload}'")
                click.echo(f"  Schema:  crawlhub task submit single {platform} {action} --help")
                raise SystemExit(1)
        except SystemExit:
            raise
        except Exception:
            pass  # If we can't fetch schema, let it through

    ensure_daemon()
    base_url = get_base_url()
    resp = httpx.post(f"{base_url}/api/tasks", json={
        "platform": platform,
        "task_type": action,
        "logic_param": params,
    }, timeout=10)

    if resp.status_code == 200:
        result = resp.json()
        json_out = json_output or (ctx.obj and ctx.obj.get("json_output"))
        if json_out:
            click.echo(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            tid = result.get("task_id", "?")
            click.echo(f"[OK] Task submitted")
            click.echo(f"  task_id   : {tid}")
            click.echo(f"  platform  : {result.get('platform', platform)}")
            click.echo(f"  action    : {result.get('task_type', action)}")
            click.echo(f"  status    : {result.get('status', '-')}")
            click.echo(f"  created_at: {_fmt_time(result.get('created_at'))}")
            click.echo(f"\n  Next:")
            click.echo(f"    crawlhub task get {tid}")
            click.echo(f"    crawlhub task output data {tid}")
    else:
        click.echo(f"[ERR] {resp.status_code}: {resp.text}")


class SubmitBatchCommand(click.Command):
    """Custom command class that provides dynamic --help for batch.

    - ``crawlhub task submit batch bilibili --help``       -> show bilibili actions
    - ``crawlhub task submit batch bilibili action --help`` -> show action schema + batch examples
    - ``crawlhub task submit batch --help``                 -> show default help
    """

    def parse_args(self, ctx, args):
        has_help = "--help" in args or "-h" in args
        if not has_help:
            return super().parse_args(ctx, args)

        positional = [a for a in args if not a.startswith("-")]
        platform_val = positional[0] if len(positional) >= 1 else None
        action_val = positional[1] if len(positional) >= 2 else None

        if platform_val and action_val:
            _show_batch_action_help(platform_val, action_val)
            ctx.exit()
        elif platform_val:
            _show_platform_actions(platform_val)
            ctx.exit()
        else:
            return super().parse_args(ctx, args)


def _show_batch_action_help(platform_name: str, action_name: str) -> None:
    """Show action schema + batch-specific examples for dynamic --help."""
    # Reuse the schema display from single, then append batch examples
    _show_action_schema(platform_name, action_name)

    # Append batch-specific usage examples
    click.echo("\n" + "-" * 60)
    click.echo("Batch usage for this action:")
    click.echo("")

    # Build a realistic example from the schema
    plat = _fetch_platform_info(platform_name)
    if plat is None:
        return
    action = next((a for a in plat.get("actions", []) if a.get("name") == action_name), None)
    if action is None:
        return

    schema = action.get("schema", {}) or {}
    props: dict = schema.get("properties", {}) or {}
    required: list = schema.get("required", []) or []

    # Pick an item_key: prefer the first required string field, else first string field
    item_key_hint = None
    for k in required:
        if props.get(k, {}).get("type") == "string":
            item_key_hint = k
            break
    if not item_key_hint:
        for k, v in props.items():
            if v.get("type") == "string":
                item_key_hint = k
                break
    if not item_key_hint:
        item_key_hint = "id"

    # Build shared-params example (all required except item_key)
    shared = {}
    for k in required:
        if k == item_key_hint:
            continue
        spec = props.get(k, {})
        if "default" in spec:
            shared[k] = spec["default"]
        elif "enum" in spec and spec["enum"]:
            shared[k] = spec["enum"][0]
        elif spec.get("type") == "string":
            shared[k] = "..."
        elif spec.get("type") in ("integer", "number"):
            shared[k] = 0
        elif spec.get("type") == "boolean":
            shared[k] = False

    shared_json = json.dumps(shared, ensure_ascii=False) if shared else ""

    items_json = json.dumps(["item1", "item2", "item3"], ensure_ascii=False)

    # Mode 1: --items + --data
    line1 = f"  crawlhub task submit batch {platform_name} {action_name}"
    line1 += f" --items '{items_json}'"
    line1 += f" --item-key {item_key_hint}"
    if shared_json:
        line1 += f" --data '{shared_json}'"
    click.echo("Mode 1: Pass items list directly")
    click.echo(line1)
    click.echo("")

    # Mode 2: --items-from (upstream task)
    line2 = f"  crawlhub task submit batch {platform_name} {action_name}"
    line2 += f" --items-from '{{\"sources\":{{\"t1\":{{\"run_id\":\"<task_id>\"}}}},\"sql\":\"SELECT {item_key_hint} FROM t1\",\"field\":\"{item_key_hint}\"}}'"
    line2 += f" --item-key {item_key_hint}"
    if shared_json:
        line2 += f" --data '{shared_json}'"
    click.echo("Mode 2: Use items from upstream task (auto-dependency)")
    click.echo("  No need to wait for upstream! Submit immediately -- CrawlHub auto-waits for upstream to finish before starting this batch.")
    click.echo(line2)


@task_submit.command("batch", cls=SubmitBatchCommand)
@click.argument("platform", required=False, default=None)
@click.argument("action", required=False, default=None)
# ── Items source (mutually exclusive) ──
@click.option("--items", default=None,
              help="JSON array of item values, e.g. '[\"730\",\"570\"]'")
@click.option("--items-from", "items_from", default=None,
              help="JSON object for dynamic items from upstream task. "
                   "Format: {\"sources\":{\"t1\":{\"run_id\":\"<task_id>\"}},\"sql\":\"SELECT ...\",\"field\":\"col\"}")
@click.option("--item-key", default=None, help="Parameter key name for each item (e.g. app_id)")
# ── Common (shared) parameters for every child ──
@click.option("--data", "-d", default=None,
              help="Shared params as JSON for ALL children, e.g. '{\"country\":\"cn\"}'")
# ── Dependency ──
@click.option("--depends-on", "depends_on", multiple=True, default=None,
              help="Task ID(s) this batch depends on. Accepts multiple.")
# ── Execution control ──
@click.option("--concurrency", default=1, type=int, help="Max concurrent tasks (default: 1)")
@click.option("--fail-strategy", default="continue",
              type=click.Choice(["continue", "abort"]),
              help="Strategy on failure: continue (default) or abort")
@click.option("--json", "json_output", is_flag=True, help="Output raw JSON")
@click.pass_context
def submit_batch(ctx, platform, action, items, items_from, item_key,
                 data, depends_on, concurrency,
                 fail_strategy, json_output):
    """Submit a batch task: run ACTION on multiple items for PLATFORM.

    Each child task runs ACTION with one item value plugged into --item-key;
    other parameters are shared via --data.

    \b
    Two modes (pick one):
      Mode 1: --items      Pass a JSON array of item values
      Mode 2: --items-from Pull items from upstream task output via SQL

    \b
    Quick help (no submission):
      crawlhub task submit batch bilibili --help
      crawlhub task submit batch bilibili search_videos --help
    """
    import httpx

    # Validate required positional args
    if not platform:
        click.echo("[ERR] PLATFORM is required. Usage: crawlhub task submit batch <platform> <action> --items '<json>' --item-key <key>")
        click.echo("  Show available platforms: crawlhub platform list")
        raise SystemExit(2)
    if not action:
        _show_platform_actions(platform)
        raise SystemExit(2)

    # ── Validate: --items and --items-from are mutually exclusive ──
    if items and items_from:
        click.echo("[ERR] --items and --items-from are mutually exclusive. Pick one.")
        raise SystemExit(1)
    if not items and not items_from:
        click.echo("[ERR] Must provide either --items (JSON array) or --items-from (upstream task spec).")
        click.echo("  Hint: crawlhub task submit batch --help")
        raise SystemExit(1)

    # ── Resolve --items (JSON array) ──
    items_list: list[str] | None = None
    if items:
        raw = items.strip()
        if raw.startswith("'") and raw.endswith("'"):
            raw = raw[1:-1]
        try:
            parsed = json.loads(raw)
            if not isinstance(parsed, list):
                click.echo("[ERR] --items must be a JSON array, e.g. '[\"730\",\"570\"]'")
                raise SystemExit(1)
            items_list = [str(x) for x in parsed]
        except json.JSONDecodeError as e:
            click.echo(f"[ERR] Invalid JSON in --items: {e}")
            click.echo(f"  Received: {raw[:120]}")
            raise SystemExit(1)
        if not items_list:
            click.echo("[ERR] --items array is empty.")
            raise SystemExit(1)

    # ── Resolve --items-from (JSON object) ──
    items_from_dict: dict[str, Any] | None = None
    if items_from:
        raw = items_from.strip()
        if raw.startswith("'") and raw.endswith("'"):
            raw = raw[1:-1]
        try:
            items_from_dict = json.loads(raw)
        except json.JSONDecodeError as e:
            click.echo(f"[ERR] Invalid JSON in --items-from: {e}")
            click.echo(f"  Received: {raw[:120]}")
            raise SystemExit(1)

    # ── Resolve --data (shared params) ──
    common_params: dict[str, Any] = {}
    if data:
        raw = data.strip()
        if raw.startswith("'") and raw.endswith("'"):
            raw = raw[1:-1]
        try:
            common_params = json.loads(raw)
        except json.JSONDecodeError as e:
            click.echo(f"[ERR] Invalid JSON in --data: {e}")
            click.echo(f"  Received: {raw[:120]}")
            raise SystemExit(1)

    # ── Auto-detect item_key if not provided ──
    if not item_key:
        # Try to infer from action schema: pick first required string field
        try:
            base_url = get_base_url()
            resp = httpx.get(f"{base_url}/api/actions/{platform}/{action}/schema", timeout=10)
            if resp.status_code == 200:
                schema = resp.json().get("input_schema", {}) or {}
            else:
                plat = _fetch_platform_info(platform)
                act = None
                if plat:
                    act = next((a for a in plat.get("actions", [])
                                if a.get("name") == action), None)
                schema = (act or {}).get("schema", {}) or {}
            props = schema.get("properties", {}) or {}
            required = schema.get("required", []) or []
            for k in required:
                if props.get(k, {}).get("type") == "string":
                    item_key = k
                    break
            if not item_key:
                for k, v in props.items():
                    if v.get("type") == "string":
                        item_key = k
                        break
        except Exception:
            pass
        if not item_key:
            click.echo("[ERR] --item-key is required. Specify the parameter key each item maps to, e.g. --item-key app_id")
            raise SystemExit(1)
        click.echo(f"[INFO] Auto-detected --item-key: {item_key}")

    # ── Validate shared params against action schema ──
    # item_key is provided via --items, so exclude it from required-check
    if common_params:
        validation_errors = _validate_params_against_schema(
            platform, action, common_params, exclude_keys={item_key})
        if validation_errors:
            click.echo(f"[ERR] Shared params validation failed for {platform}.{action}:")
            for err in validation_errors:
                click.echo(f"  - {err}")
            click.echo(f"\n  Check schema: crawlhub task submit single {platform} {action} --help")
            raise SystemExit(1)

    # ── Build request body ──
    body: dict = {
        "platform": platform,
        "action": action,
        "item_key": item_key,
        "common_params": common_params,
        "concurrency": concurrency,
        "fail_strategy": fail_strategy,
    }
    if items_list is not None:
        body["items"] = items_list
    if items_from_dict is not None:
        body["items_from"] = items_from_dict
    if depends_on:
        body["depends_on_task_ids"] = list(depends_on)

    ensure_daemon()
    base_url = get_base_url()
    try:
        resp = httpx.post(f"{base_url}/api/batch", json=body, timeout=30)
        if resp.status_code == 200:
            result = resp.json()
            json_out = json_output or (ctx.obj and ctx.obj.get("json_output"))
            if json_out:
                click.echo(json.dumps(result, indent=2, ensure_ascii=False))
            else:
                tid = result.get("task_id", "?")
                click.echo(f"[OK] Batch submitted")
                click.echo(f"  task_id   : {tid}")
                click.echo(f"  platform  : {platform}")
                click.echo(f"  action    : {action}")
                click.echo(f"  children  : {result.get('child_count', len(items_list) if items_list else 0)}")
                click.echo(f"  status    : {result.get('status', '-')}")
                click.echo(f"\n  Next:")
                click.echo(f"    crawlhub task summary {tid}")
                click.echo(f"    crawlhub task output data {tid}")
        else:
            click.echo(f"[ERR] {resp.status_code}: {resp.text}")
    except httpx.ConnectError:
        click.echo("[ERR] Cannot connect to daemon. Is it running?")


# ── task get ──────────────────────────────────────────────────────────

@task.command("get")
@click.argument("task_id")
@click.option("--with-parent", is_flag=True, help="Also show parent task info")
@click.option("--with-children", is_flag=True, help="Also show children summary + first 10")
@click.option("--with-lineage", is_flag=True, help="Also show lineage tree")
@click.option("--json", "json_output", is_flag=True, help="Output raw JSON")
@click.pass_context
def task_get(ctx, task_id: str, with_parent: bool, with_children: bool,
             with_lineage: bool, json_output: bool):
    """Get task details, optionally with parent / children / lineage."""
    import httpx

    ensure_daemon()
    base_url = get_base_url()
    json_out = json_output or (ctx.obj and ctx.obj.get("json_output"))

    # Fetch main task
    resp = httpx.get(f"{base_url}/api/tasks/{task_id}", timeout=10)
    if resp.status_code == 404:
        click.echo(f"[ERR] Task {task_id} not found.")
        click.echo("  Hint: crawlhub task list")
        return
    if resp.status_code != 200:
        click.echo(f"[ERR] {resp.status_code}: {resp.text}")
        return

    t = resp.json()
    parent_data = None
    children_data = None
    lineage_data = None

    if with_parent and t.get("parent_id"):
        pr = httpx.get(f"{base_url}/api/tasks/{t['parent_id']}", timeout=10)
        if pr.status_code == 200:
            parent_data = pr.json()

    if with_children:
        cr = httpx.get(f"{base_url}/api/tasks", params={
            "parent_id": task_id, "limit": 10, "offset": 0
        }, timeout=10)
        if cr.status_code == 200:
            children_data = cr.json()

    if with_lineage:
        lr = httpx.get(f"{base_url}/api/tasks/{task_id}/lineage", timeout=10)
        if lr.status_code == 200:
            lineage_data = lr.json()

    if json_out:
        click.echo(json.dumps({
            "task": t,
            "parent": parent_data,
            "children": children_data,
            "lineage": lineage_data,
        }, indent=2, ensure_ascii=False))
        return

    # Human-readable output
    click.echo(f"\n[Task] {task_id}")
    click.echo("-" * 60)
    _print_task_detail(t)

    if with_parent:
        click.echo(f"\n[Parent Task]")
        click.echo("-" * 60)
        if parent_data:
            _print_task_detail(parent_data)
        elif t.get("parent_id"):
            click.echo(f"  (could not fetch parent {t['parent_id']})")
        else:
            click.echo("  (no parent)")

    if with_children and children_data is not None:
        items = children_data if isinstance(children_data, list) else children_data.get("tasks", [])
        total = len(items)
        click.echo(f"\n[Children] (showing up to 10 of {total})")
        click.echo("-" * 60)
        status_counts: dict = {}
        for c in items:
            s = c.get("status", "unknown")
            status_counts[s] = status_counts.get(s, 0) + 1
        for s, cnt in status_counts.items():
            click.echo(f"  {s}: {cnt}")
        click.echo("")
        for c in items[:10]:
            tid_short = (c.get("task_id") or "")[:20]
            cstatus = (c.get("status") or "")[:12]
            click.echo(f"  {tid_short:20s}  {cstatus}")

    if with_lineage and lineage_data is not None:
        click.echo(f"\n[Lineage]")
        click.echo("-" * 60)
        _print_lineage_tree(lineage_data)


def _print_lineage_tree(node, indent=0):
    """Recursively print lineage tree."""
    if not node or not node.get("task_id"):
        return
    prefix = "  " * indent
    tid = node.get("task_id", "")[:20]
    status = node.get("status") or "-"
    action = node.get("task_type") or "-"
    click.echo(f"{prefix}- {tid}  [{status}]  {action}")
    for child in node.get("children", []):
        _print_lineage_tree(child, indent + 1)


# ── task list ─────────────────────────────────────────────────────────

@task.command("list")
@click.option("--platform", "-p", default=None, help="Filter by platform")
@click.option("--status", "-s", "status_filter", default=None, help="Filter by status")
@click.option("--search", "-q", "-k", default=None, help="Search keyword (task_id/note/logic_param/snapshot_param)")
@click.option("--parent", default=None, help="Filter by parent task ID")
@click.option("--archived", is_flag=True, help="Show archived (recycle bin) tasks only")
@click.option("--limit", "-l", default=100, type=int, help="Max results (default: 100)")
@click.option("--offset", "-o", default=0, type=int, help="Skip N tasks (default: 0)")
@click.option("--sort-by", "sort_by", default="created_at",
              type=click.Choice(["created_at", "started_at", "finished_at",
                                 "status", "platform", "task_type", "progress", "record_count"]),
              help="Sort field (default: created_at)")
@click.option("--sort-order", "sort_order", default="DESC",
              type=click.Choice(["ASC", "DESC"]),
              help="Sort order (default: DESC)")
@click.option("--json", "json_output", is_flag=True, help="Output raw JSON")
@click.pass_context
def task_list(ctx, platform, status_filter, search, parent, archived,
              limit, offset, sort_by, sort_order, json_output):
    """List tasks."""
    import httpx

    ensure_daemon()
    base_url = get_base_url()
    params = {"limit": limit, "offset": offset, "sort_by": sort_by, "sort_order": sort_order}
    if platform:
        params["platform"] = platform
    if status_filter:
        params["status"] = status_filter
    if search:
        params["search"] = search
    if parent:
        params["parent_id"] = parent
    if archived:
        # Backend query param is `only_archived` (recycle-bin view).
        # `archived` is NOT a valid backend param — FastAPI silently drops it.
        params["only_archived"] = "true"

    resp = httpx.get(f"{base_url}/api/tasks", params=params, timeout=10)
    if resp.status_code != 200:
        click.echo(f"[ERR] {resp.status_code}: {resp.text}")
        return

    tasks = resp.json()
    json_out = json_output or (ctx.obj and ctx.obj.get("json_output"))
    if json_out:
        click.echo(json.dumps(tasks, indent=2, ensure_ascii=False))
        return

    if not tasks:
        click.echo("[INFO] No tasks found.")
        return

    click.echo(f"  {'#':>3s}  {'task_id':20s}  {'platform':10s}  {'action':20s}  "
               f"{'status':12s}  {'progress':8s}  {'records':>7s}  {'created_at':19s}  note")
    click.echo("  " + "-" * 115)
    for i, t in enumerate(tasks):
        tid = (t.get("task_id") or "")[:20]
        plat = (t.get("platform") or "")[:10]
        action = (t.get("task_type") or "")[:20]
        st = (t.get("status") or "")[:12]
        prog = t.get("progress", 0) or 0
        prog_str = f"{prog * 100:5.1f}%" if prog else "    -"
        rec = t.get("record_count", 0) or 0
        created = _fmt_time(t.get("created_at"))
        note = (t.get("note") or "")
        note_disp = (note[:25] + "...") if len(note) > 25 else note
        click.echo(f"  {i+1:>3d}  {tid:20s}  {plat:10s}  {action:20s}  "
                   f"{st:12s}  {prog_str:8s}  {rec:>7d}  {created:19s}  {note_disp}")

    click.echo(f"\n[INFO] {len(tasks)} tasks (offset={offset}, limit={limit})")


# ── Bulk plumbing for task commands ──────────────────────────────────

_TASK_BULK_COLUMNS = [
    ("task_id", lambda t: (t.get("task_id") or "")[:20]),
    ("platform", lambda t: (t.get("platform") or "-")[:10]),
    ("action", lambda t: (t.get("task_type") or "-")[:20]),
    ("status", lambda t: (t.get("status") or "-")[:14]),
    ("created", lambda t: _fmt_time(t.get("created_at"))),
]


def _parse_iso_to_epoch(s: str) -> Optional[float]:
    """Parse YYYY-MM-DD or full ISO timestamp into a unix epoch float.

    Date-only inputs are treated as local-midnight.
    Returns None if the string cannot be parsed.
    """
    if not s:
        return None
    s = s.strip()
    fmts = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d")
    for fmt in fmts:
        try:
            dt = datetime.strptime(s, fmt)
            return dt.timestamp()
        except ValueError:
            continue
    # Fallback: fromisoformat handles offsets like +08:00.
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            return dt.timestamp()
        return dt.astimezone(timezone.utc).timestamp()
    except ValueError:
        return None


def _task_resolver(filters: dict) -> list:
    """Pull tasks from /api/tasks and apply client-side date filter."""
    base_url = get_base_url()
    params = {"limit": 500, "offset": 0,
              "sort_by": "created_at", "sort_order": "DESC"}
    if filters.get("platform"):
        params["platform"] = filters["platform"]
    if filters.get("status"):
        params["status"] = filters["status"]
    if filters.get("endpoint"):
        # Backend doesn't have a dedicated task_type filter, but `search`
        # hits task_type / id / note / logic_param / snapshot_param —
        # narrow client-side below.
        params["search"] = filters["endpoint"]
    if filters.get("archived"):
        # Backend query param is `only_archived` (recycle-bin view).
        # `archived` is NOT a valid backend param — FastAPI silently drops it.
        params["only_archived"] = "true"

    try:
        resp = httpx.get(f"{base_url}/api/tasks", params=params, timeout=15)
    except httpx.RequestError as e:
        raise click.ClickException(f"Cannot reach daemon: {e}")
    if resp.status_code != 200:
        raise click.ClickException(f"List failed: {resp.status_code} {resp.text}")

    tasks = resp.json() or []

    # Client-side: exact endpoint match (search is fuzzy).
    if filters.get("endpoint"):
        ep = filters["endpoint"]
        tasks = [t for t in tasks if t.get("task_type") == ep]

    # Client-side date filter.
    before = _parse_iso_to_epoch(filters.get("created_before")) if filters.get("created_before") else None
    after = _parse_iso_to_epoch(filters.get("created_after")) if filters.get("created_after") else None
    if before is not None:
        tasks = [t for t in tasks if (t.get("created_at") or 0) < before]
    if after is not None:
        tasks = [t for t in tasks if (t.get("created_at") or 0) >= after]

    return tasks


def _task_fetch_by_id(task_id: str) -> Optional[dict]:
    base_url = get_base_url()
    try:
        r = httpx.get(f"{base_url}/api/tasks/{task_id}", timeout=10)
    except httpx.RequestError:
        return None
    if r.status_code == 200:
        return r.json()
    return None


def _filter_kwargs(status, platform, endpoint, created_before, created_after,
                   archived: bool = False) -> dict:
    return {
        "status": status,
        "platform": platform,
        "endpoint": endpoint,
        "created_before": created_before,
        "created_after": created_after,
        "archived": archived,
    }


def task_filter_options(*, include_archived: bool = False):
    """Decorator factory for the standard task-bulk filter flags."""
    def deco(f):
        f = click.option("--created-after", default=None,
                         help="Only tasks created on/after this time (YYYY-MM-DD or ISO).")(f)
        f = click.option("--created-before", default=None,
                         help="Only tasks created strictly before this time.")(f)
        f = click.option("--endpoint", default=None,
                         help="Filter by task_type / action.")(f)
        f = click.option("--platform", default=None, help="Filter by platform.")(f)
        f = click.option("--status", default=None,
                         help="Filter by status (running / failed / succeeded ...).")(f)
        if include_archived:
            f = click.option("--archived", is_flag=True,
                             help="Operate on archived (recycle bin) tasks.")(f)
        return f
    return deco


# ── task cancel ───────────────────────────────────────────────────────

@task.command("cancel")
@click.argument("task_ids", nargs=-1)
@task_filter_options()
@bulk_options
@click.pass_context
def task_cancel(ctx, task_ids, status, platform, endpoint, created_before,
                created_after, yes, dry_run):
    """Cancel running tasks.

    Pass one or more TASK_IDs, or use filters to select multiple tasks.
    """
    base_url = get_base_url()

    def action(t: dict) -> BulkOutcome:
        tid = t.get("task_id")
        if t.get("_missing"):
            return BulkOutcome(BulkResult.SKIP, "not found")
        try:
            r = httpx.post(f"{base_url}/api/tasks/{tid}/cancel", timeout=10)
        except httpx.RequestError as e:
            return BulkOutcome(BulkResult.FAIL, f"network: {e}")
        if r.status_code == 200:
            return BulkOutcome(BulkResult.OK, "cancelled")
        if r.status_code == 404:
            return BulkOutcome(BulkResult.SKIP, "not found")
        if r.status_code == 409:
            try:
                detail = r.json().get("detail", "")
            except Exception:
                detail = r.text
            return BulkOutcome(BulkResult.SKIP, f"not cancellable ({detail})")
        return BulkOutcome(BulkResult.FAIL, f"{r.status_code} {r.text}")

    spec = BulkSpec(
        resolver=_task_resolver,
        fetch_by_id=_task_fetch_by_id,
        action=action,
        columns=_TASK_BULK_COLUMNS,
        entity_name="task",
        id_field="task_id",
        ensure_ready=ensure_daemon,
    )
    filters = _filter_kwargs(status, platform, endpoint, created_before, created_after)
    sys.exit(run_bulk(spec, task_ids, filters,
                      action_label="CANCEL", yes=yes, dry_run=dry_run))


# ── task retry ──────────────────────────────────────────────────
@task.command("retry")
@click.argument("task_ids", nargs=-1)
@task_filter_options()
@click.option("--all", "retry_all", is_flag=True,
              help="For batch parents: retry all children.")
@click.option("--failed", "retry_failed", is_flag=True,
              help="For batch parents: retry only failed/cancelled children.")
@bulk_options
@click.pass_context
def task_retry(ctx, task_ids, status, platform, endpoint, created_before,
               created_after, retry_all, retry_failed,
               yes, dry_run):
    """Retry tasks.

    Default: retry each target itself.
    --failed / --all apply per-task to batch-parent targets.
    """
    if retry_all and retry_failed:
        click.echo("[ERR] --all and --failed are mutually exclusive.", err=True)
        sys.exit(2)

    base_url = get_base_url()

    def action(t: dict) -> BulkOutcome:
        tid = t.get("task_id")
        if t.get("_missing"):
            return BulkOutcome(BulkResult.SKIP, "not found")
        is_batch = bool(t.get("parent_id") is None and t.get("child_count", 0) > 0)
        if retry_failed:
            url = (f"{base_url}/api/tasks/{tid}/retry-failed"
                   if is_batch else f"{base_url}/api/tasks/{tid}/retry")
            if not is_batch and t.get("status") != "failed":
                return BulkOutcome(BulkResult.SKIP, f"status={t.get('status')}")
        elif retry_all:
            url = (f"{base_url}/api/tasks/{tid}/retry-all"
                   if is_batch else f"{base_url}/api/tasks/{tid}/retry")
        else:
            url = f"{base_url}/api/tasks/{tid}/retry"
        try:
            r = httpx.post(url, json={}, timeout=10)
        except httpx.RequestError as e:
            return BulkOutcome(BulkResult.FAIL, f"network: {e}")
        if r.status_code == 200:
            return BulkOutcome(BulkResult.OK, "retried")
        if r.status_code == 409:
            try:
                err = r.json().get("error") or r.json().get("detail", "")
            except Exception:
                err = r.text
            return BulkOutcome(BulkResult.SKIP, f"not retryable ({err})")
        if r.status_code == 404:
            return BulkOutcome(BulkResult.SKIP, "not found")
        return BulkOutcome(BulkResult.FAIL, f"{r.status_code} {r.text}")

    spec = BulkSpec(
        resolver=_task_resolver,
        fetch_by_id=_task_fetch_by_id,
        action=action,
        columns=_TASK_BULK_COLUMNS,
        entity_name="task",
        id_field="task_id",
        ensure_ready=ensure_daemon,
    )
    filters = _filter_kwargs(status, platform, endpoint, created_before, created_after)
    sys.exit(run_bulk(spec, task_ids, filters,
                      action_label="RETRY", yes=yes, dry_run=dry_run))


# ── task delete ───────────────────────────────────────────────────────

@task.command("delete")
@click.argument("task_ids", nargs=-1)
@task_filter_options(include_archived=True)
@click.option("--force", is_flag=True, help="Hard delete (skip recycle bin).")
@click.option("--undo", is_flag=True, help="Restore tasks from recycle bin.")
@bulk_options
@click.pass_context
def task_delete(ctx, task_ids, status, platform, endpoint, created_before,
                created_after, archived, force, undo,
                yes, dry_run):
    """Delete tasks (moves to recycle bin by default).

    --undo : restore from recycle bin (implies --archived for filter mode).
    --force: permanently delete without recycle bin.
    """
    if force and undo:
        click.echo("[ERR] --force and --undo are mutually exclusive.", err=True)
        sys.exit(2)

    # Hard-delete is irreversible. AI-friendly CLI never prompts on stdin, so
    # we require -y for any --force invocation, including single-ID mode.
    # (Bulk runner also enforces -y for >=2 targets, which covers filter mode.)
    if force and task_ids and not yes:
        click.echo(
            "[ERR] --force is irreversible (purges from recycle bin); "
            "pass -y/--yes to confirm.",
            err=True,
        )
        sys.exit(2)

    base_url = get_base_url()
    # `archived` is only a filter scope (recycle-bin search). In ID mode it is
    # meaningless because we fetch each task by ID directly. So only auto-enable
    # it for filter mode (i.e. no explicit IDs were given). Otherwise it would
    # leak into the filter dict and trip the "IDs vs filters" mutex check.
    #
    # Both --undo (restore) and --force (hard delete) operate on tasks already
    # in the recycle bin, so when no IDs are given we must search inside the
    # recycle bin, not the live list.
    if (undo or force) and not task_ids:
        archived = True  # restore/purge via filters only makes sense in recycle bin

    def action(t: dict) -> BulkOutcome:
        tid = t.get("task_id")
        if t.get("_missing"):
            return BulkOutcome(BulkResult.SKIP, "not found")
        try:
            if undo:
                r = httpx.post(f"{base_url}/api/tasks/{tid}/restore",
                               json={}, timeout=10)
                ok_msg = "restored"
            else:
                # Backend query param is `purge` (not `force`).
                # FastAPI silently drops unknown params, so passing `force` would
                # downgrade hard-delete to soft-delete without any error.
                params = {"purge": "true"} if force else {}
                r = httpx.delete(f"{base_url}/api/tasks/{tid}",
                                 params=params, timeout=10)
                ok_msg = "hard deleted" if force else "moved to recycle bin"
        except httpx.RequestError as e:
            return BulkOutcome(BulkResult.FAIL, f"network: {e}")
        if r.status_code in (200, 204):
            return BulkOutcome(BulkResult.OK, ok_msg)
        if r.status_code == 404:
            return BulkOutcome(BulkResult.SKIP, "not found")
        return BulkOutcome(BulkResult.FAIL, f"{r.status_code} {r.text}")

    spec = BulkSpec(
        resolver=_task_resolver,
        fetch_by_id=_task_fetch_by_id,
        action=action,
        columns=_TASK_BULK_COLUMNS,
        entity_name="task",
        id_field="task_id",
        ensure_ready=ensure_daemon,
    )
    filters = _filter_kwargs(status, platform, endpoint, created_before,
                             created_after, archived=archived)
    label = "RESTORE" if undo else ("HARD-DELETE" if force else "DELETE")
    sys.exit(run_bulk(spec, task_ids, filters,
                      action_label=label, yes=yes, dry_run=dry_run))


# ── task note ─────────────────────────────────────────────────────────

@task.command("note")
@click.argument("task_id")
@click.option("--set", "set_note", default=None, help="Set note text")
@click.option("--clear", is_flag=True, help="Clear note")
@click.option("--json", "json_output", is_flag=True, help="Output raw JSON")
@click.pass_context
def task_note(ctx, task_id: str, set_note: str, clear: bool, json_output: bool):
    """View or update task note.

    Without options: show current note.
    --set <text>: update note.
    --clear: remove note.

    --set and --clear are mutually exclusive. --set "" is rejected (use --clear).
    """
    import httpx

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
        note_value = None if clear else set_note
        resp = httpx.patch(f"{base_url}/api/tasks/{task_id}/note",
                           json={"note": note_value}, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if json_out:
                click.echo(json.dumps(data, indent=2, ensure_ascii=False))
            else:
                action = "cleared" if clear else "updated"
                click.echo(f"[OK] Note {action}: {task_id}")
        elif resp.status_code == 404:
            click.echo(f"[ERR] Task {task_id} not found.")
        else:
            click.echo(f"[ERR] {resp.status_code}: {resp.text}")
    else:
        # Show current note
        resp = httpx.get(f"{base_url}/api/tasks/{task_id}", timeout=10)
        if resp.status_code == 200:
            t = resp.json()
            note = t.get("note")
            if json_out:
                click.echo(json.dumps({"task_id": task_id, "note": note}, indent=2))
            else:
                click.echo(f"  note: {note or '(empty)'}")
        elif resp.status_code == 404:
            click.echo(f"[ERR] Task {task_id} not found.")
        else:
            click.echo(f"[ERR] {resp.status_code}: {resp.text}")


# ── task force-complete ───────────────────────────────────────────────

def _force_action_factory(endpoint_path: str, ok_msg: str):
    """Build a per-task action that POSTs to the given /force-* endpoint."""
    base_url = get_base_url()

    def action(t: dict) -> BulkOutcome:
        tid = t.get("task_id")
        if t.get("_missing"):
            return BulkOutcome(BulkResult.SKIP, "not found")
        try:
            r = httpx.post(f"{base_url}/api/tasks/{tid}/{endpoint_path}",
                           json={}, timeout=10)
        except httpx.RequestError as e:
            return BulkOutcome(BulkResult.FAIL, f"network: {e}")
        if r.status_code == 200:
            return BulkOutcome(BulkResult.OK, ok_msg)
        if r.status_code == 404:
            return BulkOutcome(BulkResult.SKIP, "not found")
        if r.status_code == 409:
            try:
                detail = r.json().get("detail", "") or r.json().get("error", "")
            except Exception:
                detail = r.text
            return BulkOutcome(BulkResult.SKIP, f"not applicable ({detail})")
        return BulkOutcome(BulkResult.FAIL, f"{r.status_code} {r.text}")

    return action


@task.command("force-complete")
@click.argument("task_ids", nargs=-1)
@task_filter_options()
@bulk_options
@click.pass_context
def task_force_complete(ctx, task_ids, status, platform, endpoint, created_before,
                        created_after, yes, dry_run):
    """Force tasks to completed state (use for stuck tasks)."""
    spec = BulkSpec(
        resolver=_task_resolver,
        fetch_by_id=_task_fetch_by_id,
        action=_force_action_factory("force-complete", "force-completed"),
        columns=_TASK_BULK_COLUMNS,
        entity_name="task",
        id_field="task_id",
        ensure_ready=ensure_daemon,
    )
    filters = _filter_kwargs(status, platform, endpoint, created_before, created_after)
    sys.exit(run_bulk(spec, task_ids, filters,
                      action_label="FORCE-COMPLETE", yes=yes, dry_run=dry_run))


# ── task force-start ──────────────────────────────────────────────────

@task.command("force-start")
@click.argument("task_ids", nargs=-1)
@task_filter_options()
@bulk_options
@click.pass_context
def task_force_start(ctx, task_ids, status, platform, endpoint, created_before,
                     created_after, yes, dry_run):
    """Force tasks to start (use for stuck/pending tasks)."""
    spec = BulkSpec(
        resolver=_task_resolver,
        fetch_by_id=_task_fetch_by_id,
        action=_force_action_factory("force-start", "force-started"),
        columns=_TASK_BULK_COLUMNS,
        entity_name="task",
        id_field="task_id",
        ensure_ready=ensure_daemon,
    )
    filters = _filter_kwargs(status, platform, endpoint, created_before, created_after)
    sys.exit(run_bulk(spec, task_ids, filters,
                      action_label="FORCE-START", yes=yes, dry_run=dry_run))


# ── task output group ──────────────────────────────────────────────

@task.group("output")
def task_output_group():
    """View task output files (log / data / requests / summary / error)."""
    pass


def _get_task_output_dir(base_url: str, task_id: str) -> tuple[str | None, str | None]:
    """Fetch task output_dir. Returns (output_dir, error_msg)."""
    import httpx

    resp = httpx.get(f"{base_url}/api/tasks/{task_id}", timeout=10)
    if resp.status_code == 404:
        return None, f"Task {task_id} not found."
    if resp.status_code != 200:
        return None, f"API error {resp.status_code}: {resp.text}"
    task_data = resp.json()
    output_dir = task_data.get("output_dir", "")
    if not output_dir:
        return None, "Task has no output directory."
    return output_dir, None


def _view_task_file(ctx, task_id: str, filename: str, limit: int, export_path: str | None,
                    json_output: bool, desc_name: str):
    """Common implementation for task output data/requests/summary/error."""
    import httpx
    from pathlib import Path
    import shutil

    ensure_daemon()
    base_url = get_base_url()
    json_out = json_output or (ctx.obj and ctx.obj.get("json_output"))

    # Export mode: copy raw file to target path via filesystem
    if export_path:
        output_dir, err = _get_task_output_dir(base_url, task_id)
        if err:
            click.echo(f"[ERR] {err}")
            return

        src_path = Path(output_dir) / filename
        if not src_path.exists():
            click.echo(f"[ERR] File not found: {filename}")
            click.echo(f"  Output dir: {output_dir}")
            return

        target = Path(export_path)
        if target.is_dir():
            target = target / filename
        target.parent.mkdir(parents=True, exist_ok=True)

        shutil.copy2(str(src_path), str(target))
        click.echo(f"[OK] Copied {filename} -> {target}")
        click.echo(f"  size: {src_path.stat().st_size:,} bytes")
        return

    # Display mode: first check task exists, then try file endpoint
    output_dir, err = _get_task_output_dir(base_url, task_id)
    if err:
        click.echo(f"[ERR] {err}")
        return

    resp = httpx.get(
        f"{base_url}/api/tasks/{task_id}/files/{filename}",
        timeout=30,
    )
    if resp.status_code == 404:
        # Task exists (checked above), so it's the file that's missing
        click.echo(f"[ERR] {desc_name} does not exist for task {task_id}.")
        click.echo(f"  Output dir: {output_dir}")
        return
    if resp.status_code != 200:
        click.echo(f"[ERR] {resp.status_code}: {resp.text}")
        return

    data = resp.json()
    content = data.get("content", "")
    size = data.get("size", 0)

    if json_out:
        click.echo(json.dumps(data, indent=2, ensure_ascii=False))
        return

    if not content:
        click.echo(f"[INFO] {desc_name} is empty for task {task_id}.")
        return

    # Apply character limit
    if limit > 0 and len(content) > limit:
        displayed = content[:limit]
        click.echo(displayed)
        click.echo(f"\n... [truncated: showing {limit} / {len(content)} chars, "
                    f"file size: {size:,} bytes]")
        click.echo(f"  Use --limit 0 to show all, or --export <path> to save the full file.")
    else:
        click.echo(content)
        if size > len(content):
            click.echo(f"\n[INFO] File size: {size:,} bytes")


@task_output_group.command("log")
@click.argument("task_id")
@click.option("--limit", "-l", default=500, type=int,
              help="Max characters to display (default: 500, 0=unlimited)")
@click.option("--export", "export_path", default=None,
              help="Copy the log file to this path (directory or full path)")
@click.option("--tail", "-n", default=None, type=click.IntRange(1, 5000),
              help="Number of lines from the end (default: 200 if --since used)")
@click.option("--since", default=None,
              help="Only show lines after this time (ISO or date-only)")
@click.option("--json", "json_output", is_flag=True, help="Output raw JSON")
@click.pass_context
def task_output_log(ctx, task_id: str, limit: int, export_path: str | None,
                    tail: int | None, since: str | None, json_output: bool):
    """View task log.

    Logs are stored at ~/.crawlhub/logs/tasks/<date>/<task_id>.log,
    not in the task output directory.

    \b
    Examples:
        crawlhub task output log abc-123              # last 200 lines, first 500 chars
        crawlhub task output log abc-123 --tail 500   # last 500 lines
        crawlhub task output log abc-123 --limit 2000 # more content
        crawlhub task output log abc-123 --limit 0    # show all
        crawlhub task output log abc-123 --export ./  # copy log to cwd
        crawlhub task output log abc-123 --since 2026-05-20T08:00:00
    """
    import httpx
    from pathlib import Path
    import shutil

    ensure_daemon()
    base_url = get_base_url()
    json_out = json_output or (ctx.obj and ctx.obj.get("json_output"))

    # Export mode: find and copy the actual log file from filesystem
    if export_path:
        # Verify task exists
        output_dir, err = _get_task_output_dir(base_url, task_id)
        if err:
            click.echo(f"[ERR] {err}")
            return

        # Find log file: ~/.crawlhub/logs/tasks/<date>/<task_id>.log
        log_dir = Path.home() / ".crawlhub" / "logs" / "tasks"
        log_file = None
        if log_dir.exists():
            for date_dir in sorted(log_dir.iterdir(), reverse=True):
                if date_dir.is_dir():
                    candidate = date_dir / f"{task_id}.log"
                    if candidate.exists():
                        log_file = candidate
                        break

        if log_file is None:
            click.echo(f"[ERR] Log file not found for task {task_id}.")
            return

        target = Path(export_path)
        if target.is_dir():
            target = target / f"{task_id}.log"
        target.parent.mkdir(parents=True, exist_ok=True)

        shutil.copy2(str(log_file), str(target))
        click.echo(f"[OK] Copied {log_file.name} -> {target}")
        click.echo(f"  size: {log_file.stat().st_size:,} bytes")
        return

    # Display mode: always use /api/tasks/{id}/logs endpoint
    # (log file is NOT in the task output directory, it's in ~/.crawlhub/logs/tasks/)
    params = {"tail": tail or 200}
    if since:
        params["since"] = since

    resp = httpx.get(f"{base_url}/api/tasks/{task_id}/logs", params=params, timeout=10)
    if resp.status_code == 404:
        click.echo(f"[ERR] Task {task_id} not found.")
        return
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
        click.echo(f"[INFO] No logs found for task {task_id}.")
        return

    # Join lines and apply character limit
    full_text = "\n".join(lines)

    if limit > 0 and len(full_text) > limit:
        displayed = full_text[:limit]
        click.echo(displayed)
        click.echo(f"\n... [truncated: showing {limit} / {len(full_text)} chars, "
                    f"{len(lines)} / {total} lines]")
        click.echo(f"  Use --limit 0 to show all, or --export <path> to save the full file.")
    else:
        click.echo(full_text)
        if total > len(lines):
            click.echo(f"\n[INFO] Showing {len(lines)} / {total} total lines.")


@task_output_group.command("data")
@click.argument("task_id")
@click.option("--limit", "-l", default=500, type=int,
              help="Max characters to display (default: 500, 0=unlimited)")
@click.option("--export", "export_path", default=None,
              help="Copy the file to this path (directory or full path)")
@click.option("--json", "json_output", is_flag=True, help="Output raw JSON")
@click.pass_context
def task_output_data(ctx, task_id: str, limit: int, export_path: str | None,
                     json_output: bool):
    """View task data file (data.jsonl).

    \b
    Examples:
        crawlhub task output data abc-123              # first 500 chars
        crawlhub task output data abc-123 --limit 0    # show all
        crawlhub task output data abc-123 --export ./  # copy data.jsonl to cwd
        crawlhub task output data abc-123 --export ./result.jsonl
    """
    _view_task_file(ctx, task_id, "data.jsonl", limit, export_path, json_output, "data.jsonl")


@task_output_group.command("requests")
@click.argument("task_id")
@click.option("--limit", "-l", default=500, type=int,
              help="Max characters to display (default: 500, 0=unlimited)")
@click.option("--export", "export_path", default=None,
              help="Copy the file to this path (directory or full path)")
@click.option("--json", "json_output", is_flag=True, help="Output raw JSON")
@click.pass_context
def task_output_requests(ctx, task_id: str, limit: int, export_path: str | None,
                         json_output: bool):
    """View task requests file (requests.jsonl).

    \b
    Examples:
        crawlhub task output requests abc-123              # first 500 chars
        crawlhub task output requests abc-123 --limit 0    # show all
        crawlhub task output requests abc-123 --export ./  # copy requests.jsonl to cwd
    """
    _view_task_file(ctx, task_id, "requests.jsonl", limit, export_path, json_output, "requests.jsonl")


@task_output_group.command("summary")
@click.argument("task_id")
@click.option("--limit", "-l", default=500, type=int,
              help="Max characters to display (default: 500, 0=unlimited)")
@click.option("--export", "export_path", default=None,
              help="Copy the file to this path (directory or full path)")
@click.option("--json", "json_output", is_flag=True, help="Output raw JSON")
@click.pass_context
def task_output_summary(ctx, task_id: str, limit: int, export_path: str | None,
                        json_output: bool):
    """View task summary (summary.json).

    \b
    Examples:
        crawlhub task output summary abc-123              # first 500 chars
        crawlhub task output summary abc-123 --limit 0    # show all
        crawlhub task output summary abc-123 --export ./  # copy summary.json to cwd
        crawlhub task output summary abc-123 --json       # raw API JSON
    """
    _view_task_file(ctx, task_id, "summary.json", limit, export_path, json_output, "summary.json")


@task_output_group.command("error")
@click.argument("task_id")
@click.option("--limit", "-l", default=500, type=int,
              help="Max characters to display (default: 500, 0=unlimited)")
@click.option("--export", "export_path", default=None,
              help="Copy the file to this path (directory or full path)")
@click.option("--json", "json_output", is_flag=True, help="Output raw JSON")
@click.pass_context
def task_output_error(ctx, task_id: str, limit: int, export_path: str | None,
                      json_output: bool):
    """View task error info (error.json).

    Only exists for tasks that failed with structured error details.

    \b
    Examples:
        crawlhub task output error abc-123              # first 500 chars
        crawlhub task output error abc-123 --limit 0    # show all
        crawlhub task output error abc-123 --export ./  # copy error.json to cwd
        crawlhub task output error abc-123 --json       # raw API JSON
    """
    _view_task_file(ctx, task_id, "error.json", limit, export_path, json_output, "error.json")


# ── task open-dir ────────────────────────────────────────────────────

@task.command("open-dir")
@click.argument("task_id")
@click.pass_context
def task_open_dir(ctx, task_id: str):
    """Open task output directory in file manager."""
    import httpx

    ensure_daemon()
    base_url = get_base_url()
    resp = httpx.get(f"{base_url}/api/tasks/{task_id}/open-dir", timeout=10)
    if resp.status_code == 200:
        data = resp.json()
        click.echo(f"[OK] Opened: {data.get('path', '-')}")
    elif resp.status_code == 404:
        detail = resp.json().get("detail", "")
        if "output directory" in detail:
            click.echo(f"[ERR] Output directory does not exist for task {task_id}.")
        else:
            click.echo(f"[ERR] Task {task_id} not found.")
    elif resp.status_code == 400:
        click.echo(f"[ERR] {resp.json().get('detail', 'Bad request')}")
    else:
        click.echo(f"[ERR] {resp.status_code}: {resp.text}")


# ── task logs (DEPRECATED, use `crawlhub task output log <id>`) ─────

@task.command("logs", hidden=True)
@click.argument("task_id")
@click.option("--tail", "-n", default=200, type=click.IntRange(1, 5000),
              help="Number of lines from the end (default: 200, max: 5000)")
@click.option("--json", "json_output", is_flag=True, help="Output raw JSON")
@click.pass_context
def task_logs(ctx, task_id: str, tail: int, json_output: bool):
    """[DEPRECATED] Use `crawlhub task output log <task_id>` instead."""
    click.echo(
        "[WARN] `task logs` is deprecated; use `crawlhub task output log <task_id>` instead.",
        err=True,
    )
    # Delegate to the new task output log sub-command
    argv = ["log", task_id, "--tail", str(tail)]
    if json_output:
        argv.append("--json")
    ctx_obj = ctx.ensure_object(dict)
    try:
        task_output_group(args=argv, obj=ctx_obj, standalone_mode=False)
    except SystemExit:
        pass


# ── task export ───────────────────────────────────────────────────────

@task.command("export")
@click.argument("task_id")
@click.option("--format", "-f", "fmt", default="csv",
              type=click.Choice(["csv", "xlsx", "json", "jsonl"]),
              help="Export format (default: csv)")
@click.option("--output", "-o", default=None, help="Output file path (default: auto-generated)")
@click.option("--json", "json_output", is_flag=True, help="Output raw JSON")
@click.pass_context
def task_export(ctx, task_id: str, fmt: str, output: str, json_output: bool):
    """Export task results to file."""
    import httpx

    ensure_daemon()
    base_url = get_base_url()
    json_out = json_output or (ctx.obj and ctx.obj.get("json_output"))
    body: dict = {"format": fmt}
    if output:
        body["output_path"] = output

    resp = httpx.post(f"{base_url}/api/tasks/{task_id}/export", json=body, timeout=60)
    if resp.status_code == 200:
        data = resp.json()
        if json_out:
            click.echo(json.dumps(data, indent=2, ensure_ascii=False))
        else:
            click.echo(f"[OK] Exported: {data.get('path', 'unknown')}")
            click.echo(f"  format : {data.get('format', fmt)}")
            click.echo(f"  size   : {data.get('size', 0):,} bytes")
            click.echo(f"  records: {data.get('record_count', '-')}")
    elif resp.status_code == 404:
        click.echo(f"[ERR] Task {task_id} not found.")
    else:
        click.echo(f"[ERR] {resp.status_code}: {resp.text}")


# ── task schema ───────────────────────────────────────────────────────
#
# Inspecting the parameter schema for a (platform, action) lives under the
# `task` group because it directly serves task-submission workflows: the
# answer to "what JSON do I pass to `task submit single/batch`?" should be
# one command away from the submit commands themselves.

def _format_type(prop: dict) -> str:
    """Render a JSON schema property type into a compact human label."""
    t = prop.get("type", "any")
    if "enum" in prop:
        vals = "|".join(str(v) for v in prop["enum"])
        return f"{t}{{{vals}}}"
    return t


def _example_payload(schema: dict) -> dict:
    """Build a minimal example payload covering all required fields."""
    props = schema.get("properties", {}) or {}
    required = schema.get("required", []) or []
    placeholders = {
        "string": "...", "integer": 0, "number": 0,
        "boolean": False, "array": [], "object": {},
    }
    example: dict = {}
    for name in required:
        spec = props.get(name, {})
        if "default" in spec:
            example[name] = spec["default"]
        elif "enum" in spec and spec["enum"]:
            example[name] = spec["enum"][0]
        else:
            example[name] = placeholders.get(spec.get("type", "string"), "...")
    return example


@task.command("schema")
@click.argument("platform_name")
@click.argument("action_name")
@click.pass_context
def task_schema(ctx, platform_name: str, action_name: str):
    """Show parameter schema for a (PLATFORM, ACTION) pair.

    Prints required/optional parameters with type, description, default,
    and a ready-to-use example payload for `task submit single`. Use --json
    (global flag) to dump raw JSON Schema for programmatic / AI consumption.

    Examples:
        crawlhub task schema steam get_game_detail
        crawlhub --json task schema steam get_game_detail
    """
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

    platforms = resp.json().get("platforms", [])
    plat = next((p for p in platforms if p.get("platform") == platform_name), None)
    if plat is None:
        avail = ", ".join(p.get("platform", "") for p in platforms) or "(none)"
        click.echo(f"[ERR] Platform '{platform_name}' not found. Available: {avail}",
                   err=True)
        raise SystemExit(1)

    action = next((a for a in plat.get("actions", [])
                   if a.get("name") == action_name), None)
    if action is None:
        avail = ", ".join(a.get("name", "") for a in plat.get("actions", [])) or "(none)"
        click.echo(f"[ERR] Action '{action_name}' not found on platform "
                   f"'{platform_name}'. Available: {avail}", err=True)
        raise SystemExit(1)

    schema = action.get("schema", {}) or {}

    if ctx.obj and ctx.obj.get("json_output"):
        click.echo(json.dumps({
            "platform": platform_name,
            "action": action_name,
            "description": action.get("description", ""),
            "schema": schema,
            "output_schema": action.get("output_schema"),
        }, indent=2, ensure_ascii=False))
        return

    desc = action.get("description") or schema.get("description") or ""
    click.echo(f"Action: {platform_name}.{action_name}")
    if desc:
        click.echo(f"Description: {desc}")
    click.echo("")

    props: dict = schema.get("properties", {}) or {}
    required: list = schema.get("required", []) or []
    required_set = set(required)

    if not props:
        click.echo("(no parameters declared)")
        return

    req_keys = [k for k in props.keys() if k in required_set]
    opt_keys = [k for k in props.keys() if k not in required_set]

    def _print_param(key: str, spec: dict) -> None:
        type_str = _format_type(spec)
        line = f"  {key}  ({type_str})"
        if "default" in spec:
            line += f"  [default: {spec['default']!r}]"
        click.echo(line)
        d = (spec.get("description") or "").strip()
        if d:
            click.echo(f"      {d}")
        labels = spec.get("enum_labels")
        if labels and "enum" in spec:
            pairs = ", ".join(f"{v}={lbl}" for v, lbl
                              in zip(spec["enum"], labels))
            click.echo(f"      enum: {pairs}")

    if req_keys:
        click.echo("Required parameters:")
        for k in req_keys:
            _print_param(k, props[k])
        click.echo("")

    if opt_keys:
        click.echo("Optional parameters:")
        for k in opt_keys:
            _print_param(k, props[k])
        click.echo("")

    example = _example_payload(schema)
    click.echo("Example:")
    payload = json.dumps(example, ensure_ascii=False)
    click.echo(f"  crawlhub task submit single {platform_name} {action_name} \\")
    click.echo(f"      --data '{payload}'")

    out_schema = action.get("output_schema")
    if out_schema:
        click.echo("")
        click.echo("Output columns (for items_from SQL):")
        for col, ctype in out_schema.items():
            click.echo(f"  {col}: {ctype}")
