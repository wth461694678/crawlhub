"""Generic bulk-action runner for CLI commands.

Domain-agnostic. Each command group (task / favorite / ...) plugs in its own
resolver (filters -> entity list) and per-entity action callable. This module
handles:

  * mutual exclusion between explicit IDs and filter flags
  * 0-hit and >=2-hit safety prompts
  * dry-run preview rendering
  * sequential execution with OK / SKIP / FAIL accounting
  * exit-code mapping (0 ok, 1 any FAIL, 2 invalid args / 0 hit)

Concurrency is intentionally NOT supported — keep it simple, daemon side
already serializes most state-mutating operations.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Iterable, List, Sequence, Tuple

import click


class BulkResult(Enum):
    OK = "OK"
    SKIP = "SKIP"
    FAIL = "FAIL"


@dataclass
class BulkOutcome:
    result: BulkResult
    message: str = ""  # human-readable detail; shown after status tag


# ── BulkSpec ──────────────────────────────────────────────────────────

@dataclass
class BulkSpec:
    """Plug-in contract for a bulk-able command.

    resolver
        Callable that takes the dict of filter kwargs and returns the matched
        entity list. Used only when the user passes filter flags instead of
        explicit IDs.
    fetch_by_id
        Callable that takes a single ID string and returns the entity dict
        (or None if not found). Used when the user passes explicit IDs.
    action
        Callable that takes one entity dict and performs the actual work.
        Returns a BulkOutcome. Network/HTTP errors should be caught inside
        and turned into BulkOutcome(FAIL, message=...).
    columns
        List of (header, getter) tuples used to render preview / progress
        rows. getter is `entity -> str`.
    entity_name
        Singular noun used in prompts (e.g. "task", "favorite").
    id_field
        Key on the entity dict that uniquely identifies it. Default "task_id".
    ensure_ready
        Optional callback fired AFTER argument validation passes but BEFORE
        any resolver / fetch_by_id call. Use to lazily start a daemon /
        check connectivity, so invalid invocations exit fast without side
        effects.
    """
    resolver: Callable[[dict], List[dict]]
    fetch_by_id: Callable[[str], dict | None]
    action: Callable[[dict], BulkOutcome]
    columns: Sequence[Tuple[str, Callable[[dict], str]]]
    entity_name: str = "task"
    id_field: str = "task_id"
    ensure_ready: Callable[[], None] | None = None


# ── Click decorator: shared bulk options ──────────────────────────────

def bulk_options(f):
    """Attach the universal bulk flags to a Click command.

    Adds: -y/--yes, --dry-run.

    NOTE: failures never abort the run — every target is attempted, and a
    final FAIL list is printed at the end. There is intentionally no
    --continue-on-error flag (it was the implicit default and adding the
    opposite "abort on first failure" mode just hid surprises).
    """
    f = click.option(
        "--dry-run", is_flag=True,
        help="Show what would be done, don't actually do it.",
    )(f)
    f = click.option(
        "-y", "--yes", "yes", is_flag=True,
        help="Skip confirmation prompt.",
    )(f)
    return f


# ── Resolution: explicit IDs vs filters ───────────────────────────────

def _has_any_filter(filters: dict) -> bool:
    """True if any filter kwarg has a meaningful (truthy) value.

    None / False / "" / empty list -> not provided.
    """
    return any(bool(v) for v in filters.values())


def _validate_inputs(spec: BulkSpec, ids: Sequence[str], filters: dict) -> None:
    """Cheap upfront check: enforce mutual exclusion + required-one-of.

    Doesn't touch the network. Raises click.UsageError on bad input so the
    daemon stays cold for invalid invocations.
    """
    has_ids = bool(ids)
    has_filters = _has_any_filter(filters)
    if has_ids and has_filters:
        raise click.UsageError(
            f"Explicit {spec.entity_name} IDs and filter flags are mutually exclusive."
        )
    if not has_ids and not has_filters:
        raise click.UsageError(
            f"Provide one or more {spec.entity_name} IDs, or filter flags."
        )


def _resolve_targets(spec: BulkSpec, ids: Sequence[str], filters: dict) -> List[dict]:
    """Resolve to the final entity list. Assumes _validate_inputs already passed."""
    targets: List[dict] = []
    if ids:
        seen = set()
        for tid in ids:
            if tid in seen:
                continue
            seen.add(tid)
            ent = spec.fetch_by_id(tid)
            if ent is None:
                # Synthesize a minimal stub so the action callable can decide
                # how to report the missing entity (typically SKIP / FAIL).
                ent = {spec.id_field: tid, "_missing": True}
            targets.append(ent)
    else:
        targets = list(spec.resolver(filters))

    return targets


# ── Preview rendering ─────────────────────────────────────────────────

def _render_preview(spec: BulkSpec, targets: List[dict], limit: int = 5) -> str:
    """Build a small fixed-width table for confirmation / dry-run output."""
    headers = [h for h, _ in spec.columns]
    getters = [g for _, g in spec.columns]

    rows: List[List[str]] = []
    for ent in targets[:limit]:
        rows.append([str(g(ent) or "-") for g in getters])

    # Compute column widths.
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            if len(cell) > widths[i]:
                widths[i] = len(cell)

    def fmt_row(cells: List[str]) -> str:
        return "  " + "  ".join(c.ljust(widths[i]) for i, c in enumerate(cells))

    lines = [fmt_row(headers), fmt_row(["-" * w for w in widths])]
    lines.extend(fmt_row(r) for r in rows)
    if len(targets) > limit:
        lines.append(f"  ... and {len(targets) - limit} more")
    return "\n".join(lines)


# ── Main entry ────────────────────────────────────────────────────────

def run_bulk(
    spec: BulkSpec,
    ids: Sequence[str],
    filters: dict,
    *,
    action_label: str,
    yes: bool = False,
    dry_run: bool = False,
) -> int:
    """Execute a bulk action. Returns the process exit code.

    Caller is expected to `raise SystemExit(run_bulk(...))` (or just return
    it from the Click command — Click will use it as the exit code).

    Failures never abort the run; every target is attempted, and a final
    list of failed targets is printed at the end.
    """
    try:
        _validate_inputs(spec, ids, filters)
    except click.UsageError as e:
        click.echo(f"[ERR] {e.message}", err=True)
        return 2

    # Lazy: only spin up the daemon / network checks once we know args are valid.
    if spec.ensure_ready is not None:
        try:
            spec.ensure_ready()
        except SystemExit as e:
            return int(e.code) if isinstance(e.code, int) else 1

    try:
        targets = _resolve_targets(spec, ids, filters)
    except click.ClickException as e:
        click.echo(f"[ERR] {e.format_message()}", err=True)
        return 1

    if not targets:
        click.echo(f"[ERR] No {spec.entity_name}s matched.")
        return 2

    n = len(targets)

    # AI-friendly CLI: never block on stdin. For >=2 targets we require an
    # explicit -y/--yes (or --dry-run); otherwise we print the plan and exit
    # with code 2 so the caller sees a clear, deterministic refusal instead
    # of a hang on an interactive prompt.
    if not yes and not dry_run and n >= 2:
        click.echo(f"About to {action_label} {n} {spec.entity_name}(s):", err=True)
        click.echo(_render_preview(spec, targets), err=True)
        click.echo(
            f"[ERR] Destructive batch ({n} {spec.entity_name}s); "
            f"pass -y/--yes to confirm or --dry-run to preview.",
            err=True,
        )
        return 2

    # Dry-run: print plan and exit 0.
    if dry_run:
        click.echo(f"[DRY-RUN] Would {action_label} {n} {spec.entity_name}(s):")
        click.echo(_render_preview(spec, targets, limit=n))
        return 0

    # Execute sequentially. Failures never abort — record them and continue.
    ok = skip = fail = 0
    failures: List[Tuple[str, str]] = []
    for i, ent in enumerate(targets, 1):
        ent_id = ent.get(spec.id_field, "?")
        try:
            outcome = spec.action(ent)
        except Exception as e:  # safety net; action is expected to handle its own errors
            outcome = BulkOutcome(BulkResult.FAIL, f"unexpected error: {e}")

        tag = outcome.result.value
        msg = f": {outcome.message}" if outcome.message else ""
        click.echo(f"[{i}/{n}] {ent_id} ... {tag}{msg}")

        if outcome.result == BulkResult.OK:
            ok += 1
        elif outcome.result == BulkResult.SKIP:
            skip += 1
        else:
            fail += 1
            failures.append((str(ent_id), outcome.message or ""))

    click.echo(f"\nDone: {ok} OK / {skip} SKIP / {fail} FAIL")
    if failures:
        click.echo(f"Failed {spec.entity_name}s ({len(failures)}):")
        for fid, fmsg in failures:
            suffix = f" -- {fmsg}" if fmsg else ""
            click.echo(f"  - {fid}{suffix}")
    return 1 if fail > 0 else 0
