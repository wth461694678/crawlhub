"""Throttle (cookie request interval) management commands.

Subcommands:
    throttle list              List throttle configs for all platforms.
    throttle get <platform>    Show throttle config + cookie backoff states.
    throttle update <platform> [options]   Hot-update throttle config.

Throttle controls the minimum interval between requests for each cookie,
plus exponential backoff when rate-limits or anti-crawl blocks are hit.

Config fields:
    expected_interval     Mean interval in seconds (exponential distribution).
    min_floor            Minimum interval floor (default: expected * 0.3).
    backoff_base_seconds Base seconds for exponential backoff (default: 60).
    max_backoff_exponent Max backoff exponent (default: 4, i.e. 2^4 * base).
    truncate_percentile  Cap exponential long-tail at this percentile, e.g.
                         0.95 = clamp top 5% (default). Set "off" or >=1.0
                         to disable truncation.
"""

import json
from typing import Optional

import click
import httpx

from crawlhub.cli._utils import ensure_daemon, get_base_url


# ── Helpers ──────────────────────────────────────────────────────────


def _fetch_all() -> dict:
    """GET /api/platform-config and return the full response body."""
    ensure_daemon()
    base_url = get_base_url()
    try:
        resp = httpx.get(f"{base_url}/api/platform-config", timeout=10)
    except httpx.RequestError as e:
        raise click.ClickException(f"network: {e}")
    if resp.status_code != 200:
        raise click.ClickException(f"HTTP {resp.status_code}: {resp.text}")
    return resp.json().get("platforms", {})


def _fetch_platform(platform: str) -> dict:
    """GET /api/platform-config and return one platform's data, or raise 404."""
    data = _fetch_all()
    if platform not in data:
        raise click.ClickException(f"Platform '{platform}' not found or has no throttle config.")
    return data[platform]


def _print_throttle_config(tc: dict) -> None:
    """Print throttle_config dict in key: value format."""
    click.echo(f"  expected_interval     : {tc.get('expected_interval')} s")
    click.echo(f"  min_floor            : {tc.get('min_floor')} s")
    click.echo(f"  backoff_base_seconds : {tc.get('backoff_base_seconds')} s")
    click.echo(f"  max_backoff_exponent : {tc.get('max_backoff_exponent')}")
    # ───── R4-P15 长尾截断展示 ─────
    tp = tc.get("truncate_percentile")
    cap = tc.get("truncate_cap_seconds")
    if tp is None:
        click.echo("  truncate_percentile  : off (no long-tail cap)")
    else:
        cap_str = f"{cap:.2f}s" if cap is not None else "n/a"
        click.echo(f"  truncate_percentile  : {tp}  -> cap={cap_str}")


def _print_cookie_states(cookie_states: list) -> None:
    """Print cookie backoff states as a table."""
    if not cookie_states:
        click.echo("  (no cookie states tracked yet)")
        return

    headers = ["label", "status", "backoff_remain_s", "failures", "last_req", "last_success"]
    rows = []
    for cs in cookie_states:
        remain = cs.get("backoff_remaining_seconds", 0)
        remain_str = f"{remain:.0f}" if remain else "0"
        last_req = cs.get("last_request_time")
        last_succ = cs.get("last_success_time")
        rows.append([
            str(cs.get("label") or "-"),
            str(cs.get("status") or "?"),
            remain_str,
            str(cs.get("consecutive_failures") or 0),
            str(last_req or "-")[:19],
            str(last_succ or "-")[:19],
        ])

    widths = [max(len(h), max(len(r[i]) for r in rows)) for i, h in enumerate(headers)]
    fmt = "    " + "  ".join(f"{{:<{w}}}" for w in widths)

    click.echo("")
    click.echo(fmt.format(*headers))
    click.echo(fmt.format(*["-" * w for w in widths]))
    for row in rows:
        click.echo(fmt.format(*row))


# ── throttle group ───────────────────────────────────────────────────

@click.group()
def throttle():
    """Throttle (cookie request interval) management."""
    pass


# ── throttle list ─────────────────────────────────────────────────────

@throttle.command("list")
@click.option("--json", "json_output", is_flag=True, help="Output raw JSON.")
@click.pass_context
def throttle_list(ctx, json_output: bool):
    """List throttle configs for all platforms."""
    data = _fetch_all()

    json_out = json_output or (ctx.obj and ctx.obj.get("json_output"))
    if json_out:
        # Strip cookie_states for the list view (too verbose), keep only configs.
        compact = {
            platform: info.get("throttle_config", {})
            for platform, info in data.items()
        }
        click.echo(json.dumps(compact, indent=2, ensure_ascii=False))
        return

    if not data:
        click.echo("[INFO] No platform configs found.")
        return

    for platform, info in data.items():
        tc = info.get("throttle_config", {})
        n_cookies = len(info.get("cookie_states", []))
        click.echo(f"\n  [{platform}] ({n_cookies} cookie(s) tracked)")
        _print_throttle_config(tc)

    click.echo(f"\n[INFO] {len(data)} platform(s).")


# ── throttle get ──────────────────────────────────────────────────────

@throttle.command("get")
@click.argument("platform")
@click.option("--json", "json_output", is_flag=True, help="Output raw JSON.")
@click.pass_context
def throttle_get(ctx, platform: str, json_output: bool):
    """Show throttle config and cookie backoff states for one platform."""
    info = _fetch_platform(platform)

    json_out = json_output or (ctx.obj and ctx.obj.get("json_output"))
    if json_out:
        click.echo(json.dumps({platform: info}, indent=2, ensure_ascii=False))
        return

    tc = info.get("throttle_config", {})
    cookie_states = info.get("cookie_states", [])

    click.echo(f"  platform          : {platform}")
    click.echo("\n  Throttle config:")
    _print_throttle_config(tc)

    click.echo(f"\n  Cookie states ({len(cookie_states)} tracked):")
    _print_cookie_states(cookie_states)


# ── throttle update ──────────────────────────────────────────────────

@throttle.command("update")
@click.argument("platform")
@click.option("--expected-interval", type=float, default=None,
              help="Mean interval in seconds (exponential distribution).")
@click.option("--min-floor", type=float, default=None,
              help="Minimum interval floor in seconds (default: expected * 0.3).")
@click.option("--backoff-base", type=float, default=None,
              help="Base seconds for exponential backoff (default: 60).")
@click.option("--max-backoff-exp", type=int, default=None,
              help="Max backoff exponent (default: 4).")
@click.option("--truncate-percentile", "truncate_percentile", type=str, default=None,
              help='Cap exponential long-tail at this percentile, e.g. 0.95 '
                   '(default). Pass "off" or a value >=1.0 to disable.')
@click.option("--json", "json_output", is_flag=True, help="Output raw JSON.")
@click.pass_context
def throttle_update(ctx, platform: str,
                    expected_interval: Optional[float],
                    min_floor: Optional[float],
                    backoff_base: Optional[float],
                    max_backoff_exp: Optional[int],
                    truncate_percentile: Optional[str],
                    json_output: bool):
    """Hot-update throttle config for a platform (no daemon restart needed).

    All options are optional — only the flags you provide are updated.
    Omitted fields keep their current values.

    Examples:
        crawlhub throttle update bilibili --expected-interval 3.0
        crawlhub throttle update douyin --min-floor 1.0 --backoff-base 120
        crawlhub throttle update douyin --truncate-percentile 0.90
        crawlhub throttle update douyin --truncate-percentile off
    """
    # Build body with only the fields the user explicitly set.
    body = {}
    if expected_interval is not None:
        body["expected_interval"] = expected_interval
    if min_floor is not None:
        body["min_floor"] = min_floor
    if backoff_base is not None:
        body["backoff_base_seconds"] = backoff_base
    if max_backoff_exp is not None:
        body["max_backoff_exponent"] = max_backoff_exp
    # ────────────────────────────────────────────────────────────────
    #  truncate_percentile 三种语义：
    #    --truncate-percentile 0.95   → 截 top 5%
    #    --truncate-percentile off    → 关闭截断（传 null 到 API）
    #    （不传该选项）                → 保持现有值不变
    # ────────────────────────────────────────────────────────────────
    if truncate_percentile is not None:
        tp_lower = truncate_percentile.strip().lower()
        if tp_lower in ("off", "none", "null", "false"):
            body["truncate_percentile"] = None
        else:
            try:
                body["truncate_percentile"] = float(truncate_percentile)
            except ValueError:
                raise click.ClickException(
                    f"--truncate-percentile: expected float or 'off', got {truncate_percentile!r}"
                )

    if not body:
        raise click.ClickException(
            "Nothing to update. Provide at least one option:\n"
            "  --expected-interval FLOAT\n"
            "  --min-floor FLOAT\n"
            "  --backoff-base FLOAT\n"
            "  --max-backoff-exp INT\n"
            "  --truncate-percentile FLOAT|off"
        )

    ensure_daemon()
    base_url = get_base_url()
    try:
        resp = httpx.put(
            f"{base_url}/api/platform-config/{platform}",
            json=body,
            timeout=10,
        )
    except httpx.RequestError as e:
        raise click.ClickException(f"network: {e}")

    if resp.status_code == 404:
        raise click.ClickException(f"Platform '{platform}' not found.")
    if resp.status_code != 200:
        raise click.ClickException(f"HTTP {resp.status_code}: {resp.text}")

    json_out = json_output or (ctx.obj and ctx.obj.get("json_output"))
    if json_out:
        click.echo(json.dumps(resp.json(), indent=2, ensure_ascii=False))
    else:
        click.echo(f"[OK] Throttle config updated for '{platform}':")
        for k, v in resp.json().get("updated", {}).items():
            click.echo(f"  {k} = {v}")
