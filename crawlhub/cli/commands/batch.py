"""Batch task commands."""

import json

import click

from crawlhub.cli._utils import ensure_daemon, get_base_url


@click.group()
def batch():
    """Batch task commands."""
    pass


@batch.command("submit")
@click.argument("platform")
@click.argument("action")
@click.option("--items", default=None, help="Comma-separated list of items to process")
@click.option("--item-key", required=True, help="Parameter key name for each item (e.g. 'app_id')")
@click.option("--items-file", default=None, type=click.Path(exists=True), help="File with one item per line")
@click.option("--items-from-task", default=None, help="(Removed) Use SQL pipeline via web UI/API instead")
@click.option("--items-field", default=None, help="(Removed) Use SQL pipeline via web UI/API instead")
@click.option("--concurrency", default=1, type=int, help="Max concurrent tasks (default: 1)")
@click.option("--fail-strategy", default="continue", type=click.Choice(["continue", "abort"]),
              help="Strategy on failure: continue (default) or abort")
# @click.option("--cookie-policy", default=None, help="Cookie policy as JSON string (optional)")
@click.option("--allow-partial-upstream/--no-allow-partial-upstream", default=True,
              help="Allow downstream to start even if upstream has partial failures (default: true)")
@click.pass_context
def batch_submit(ctx, platform, action, items, item_key, items_file, items_from_task, items_field,
                 concurrency, fail_strategy, cookie_policy, allow_partial_upstream):
    """Submit a batch task to run ACTION on multiple items for PLATFORM.

    Examples:
        crawlhub batch submit steam get_game_detail --items 730,570,440 --item-key app_id
        crawlhub batch submit steam get_game_detail --items-file app_ids.txt --item-key app_id
    """
    import httpx

    ensure_daemon()
    base_url = get_base_url()

    body: dict = {
        "platform": platform,
        "action": action,
        "item_key": item_key,
        "concurrency": concurrency,
        "fail_strategy": fail_strategy,
        "allow_partial_upstream": allow_partial_upstream,
    }

    if items:
        body["items"] = [i.strip() for i in items.split(",") if i.strip()]
    elif items_file:
        with open(items_file, "r", encoding="utf-8") as f:
            body["items"] = [line.strip() for line in f if line.strip()]
    elif items_from_task and items_field:
        click.echo(
            "[ERR] --items-from-task / --items-field is no longer supported. "
            "Use the SQL pipeline (items_from = {sources, sql, field}) via the API or web UI."
        )
        raise SystemExit(1)
    else:
        click.echo("[ERR] Must provide --items or --items-file")
        raise SystemExit(1)

    # if cookie_policy:
    #     try:
    #         body["cookie_policy"] = json.loads(cookie_policy)
    #     except json.JSONDecodeError:
    #         click.echo("[ERR] --cookie-policy must be valid JSON")
    #         raise SystemExit(1)

    try:
        resp = httpx.post(f"{base_url}/api/batch", json=body, timeout=30)
        if resp.status_code == 200:
            data = resp.json()
            if ctx.obj and ctx.obj.get("json_output"):
                click.echo(json.dumps(data, indent=2, ensure_ascii=False))
            else:
                click.echo(f"[OK] Batch submitted: task_id={data['task_id']}, children={data['child_count']}")
        else:
            click.echo(f"[ERR] {resp.status_code}: {resp.text}")
    except httpx.ConnectError:
        click.echo("[ERR] Cannot connect to daemon. Is it running?")
