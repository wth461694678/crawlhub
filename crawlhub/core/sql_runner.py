"""Execute the validated items_from SQL and turn the result into a list of items.

Two entry points:
    run_items_from(items_from, store) -> list[Any]
        Real run; reads every source's data.jsonl into DuckDB views, executes
        the user's SQL with a 30-second hard timeout, dedups (if requested),
        and returns the values of the `field` column.

    preview_items_from(items_from, store, *, limit=10, timeout_s=10.0)
            -> {"rows": [...], "field_column": str, "total_rows": int}
        Used by the UI's "预览结果" button. Same plumbing, but wraps the user
        SQL in a LIMIT and returns full rows (column dicts) instead of items,
        so the user can inspect the shape before committing.

Both functions assume the items_from spec has already passed
`validate_items_from()`. They re-resolve sources to actual paths at execution
time, so a successful preview at submission time + a successful run later are
two independent "now" snapshots — the upstream may have produced more data
between the two, that's expected.

Timeout strategy:
    DuckDB does not have Postgres-style `statement_timeout`. Instead we start
    a daemon thread that calls `connection.interrupt()` after the deadline.
    The blocking `execute(...)` in the main thread then raises
    duckdb.InterruptException, which we translate to SQLTimeoutError.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

import duckdb

from crawlhub.core.artifact_resolver import resolve_artifact
from crawlhub.core.sql_errors import (
    SQLBindError,
    SQLItemsFromError,
    SQLTimeoutError,
)

if TYPE_CHECKING:
    from crawlhub.core.sqlite_store import SqliteStateStore


DEFAULT_RUN_TIMEOUT_S = 30.0
DEFAULT_PREVIEW_LIMIT = 10
DEFAULT_PREVIEW_TIMEOUT_S = 10.0


def run_items_from(
    items_from: dict[str, Any],
    store: "SqliteStateStore",
    *,
    timeout_s: float = DEFAULT_RUN_TIMEOUT_S,
) -> list[Any]:
    """Execute the SQL and return the deduplicated `field` column as a list.

    Caller must have already passed `validate_items_from(items_from, store)`
    or this function may raise raw DuckDB errors that don't fit the SQL error
    hierarchy. Failures during *resolution* (upstream not ready, file missing)
    do raise the proper Artifact* errors though.
    """
    sources: dict[str, dict] = items_from["sources"]
    sql: str = items_from["sql"]
    field: str = items_from.get("field", "item")
    dedup: bool = bool(items_from.get("dedup", True))

    # Resolve every source to a real on-disk jsonl path (raises Artifact*
    # errors if not ready).
    paths: dict[str, Path] = {
        alias: resolve_artifact(ref, store, alias=alias)
        for alias, ref in sources.items()
    }

    rows = _execute_sql(sql, paths, timeout_s=timeout_s, sql_snippet=sql.strip()[:120])

    cols = [d[0] for d in rows.description]
    if field not in cols:
        # Defensive: validator should have caught this. Surface anyway so we
        # never silently ship garbage downstream.
        raise SQLBindError(
            f"field {field!r} not in result columns {cols!r}",
            sql_snippet=sql.strip()[:120],
        )
    field_idx = cols.index(field)
    fetched = rows.fetchall()
    items = [row[field_idx] for row in fetched]

    if dedup:
        items = _dedup_preserve_order(items)
    return items


def preview_items_from(
    items_from: dict[str, Any],
    store: "SqliteStateStore",
    *,
    limit: int = DEFAULT_PREVIEW_LIMIT,
    timeout_s: float = DEFAULT_PREVIEW_TIMEOUT_S,
) -> dict[str, Any]:
    """Run the SQL with a LIMIT clause and return preview rows + meta.

    Returns:
        {
          "rows": [{col: value, ...}, ...],   # up to `limit` rows
          "field_column": "<the configured field name>",
          "total_rows": <int>,                # rows actually returned (<= limit)
        }
    """
    sources: dict[str, dict] = items_from["sources"]
    sql: str = items_from["sql"]
    field: str = items_from.get("field", "item")

    paths: dict[str, Path] = {
        alias: resolve_artifact(ref, store, alias=alias)
        for alias, ref in sources.items()
    }

    # Wrap the user's SQL in a subquery so we can LIMIT without trampling
    # any LIMIT they wrote themselves (we take min of the two implicitly via
    # the outer LIMIT — DuckDB pushes the limit through).
    wrapped = f"SELECT * FROM ({sql}) AS _preview_sub LIMIT {int(limit)}"

    cur = _execute_sql(
        wrapped,
        paths,
        timeout_s=timeout_s,
        sql_snippet=sql.strip()[:120],
    )

    cols = [d[0] for d in cur.description]
    rows = cur.fetchall()
    return {
        "rows": [dict(zip(cols, row)) for row in rows],
        "field_column": field,
        "total_rows": len(rows),
        "columns": cols,
    }


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------

def _execute_sql(
    sql: str,
    paths: dict[str, Path],
    *,
    timeout_s: float,
    sql_snippet: str,
) -> "duckdb.DuckDBPyConnection":
    """Open a fresh DuckDB connection, register source views, run SQL, return cursor.

    The connection's lifetime is bound to the cursor we return; caller fetches
    from it before letting it go out of scope. We don't close the connection
    here because closing invalidates the cursor on some DuckDB versions.

    Uses a watchdog Timer for timeout: schedules `con.interrupt()` to fire
    after `timeout_s`; if execute() returns first, we cancel the timer.
    """
    con = duckdb.connect(":memory:")

    # Register one view per source from the actual jsonl file. read_json_auto
    # handles ndjson well; format='newline_delimited' is the safe default for
    # our writers (one record per line).
    #
    # NOTE: DuckDB doesn't accept prepared parameters in DDL/table-function
    # arguments here, so we inline the path. Paths come from resolve_artifact()
    # which only returns either an indexed task's output_dir or an absolute
    # external path — never user-supplied raw strings. Single-quote escaping
    # is sufficient.
    for alias, path in paths.items():
        path_lit = str(path).replace("'", "''")
        con.execute(
            f'CREATE VIEW "{alias}" AS '
            f"SELECT * FROM read_json_auto('{path_lit}', format='newline_delimited')"
        )

    timer: threading.Timer | None = None
    timed_out = False

    def _on_timeout() -> None:
        nonlocal timed_out
        timed_out = True
        try:
            con.interrupt()
        except Exception:  # noqa: BLE001 — interrupt is best-effort
            pass

    timer = threading.Timer(timeout_s, _on_timeout)
    timer.daemon = True
    timer.start()
    try:
        try:
            return con.execute(sql)
        except duckdb.InterruptException as e:  # type: ignore[attr-defined]
            raise SQLTimeoutError(
                f"SQL execution exceeded {timeout_s:.0f}s and was interrupted",
                sql_snippet=sql_snippet,
            ) from e
        except SQLItemsFromError:
            raise
        except duckdb.Error as e:
            # Surface DuckDB runtime errors (file format issues, type cast
            # failures...) as a generic SQLBindError so they go through the
            # same plumbing as validation-time failures.
            if timed_out:
                raise SQLTimeoutError(
                    f"SQL execution exceeded {timeout_s:.0f}s and was interrupted",
                    sql_snippet=sql_snippet,
                ) from e
            raise SQLBindError(
                f"SQL runtime error: {_clean(str(e))}",
                sql_snippet=sql_snippet,
            ) from e
    finally:
        if timer is not None:
            timer.cancel()


def _dedup_preserve_order(items: list[Any]) -> list[Any]:
    """Order-preserving dedup. Falls back to str-keyed dedup for unhashable values."""
    seen: set = set()
    out: list[Any] = []
    for x in items:
        try:
            key = x
            if key in seen:
                continue
            seen.add(key)
        except TypeError:
            # Unhashable (dict/list) — degrade to repr key.
            key = repr(x)
            if key in seen:
                continue
            seen.add(key)
        out.append(x)
    return out


def _clean(msg: str) -> str:
    out = []
    for line in msg.strip().splitlines():
        if line.startswith("LINE ") or line.strip().startswith("^"):
            continue
        out.append(line)
    return " ".join(out).strip()


__all__ = [
    "run_items_from",
    "preview_items_from",
    "DEFAULT_RUN_TIMEOUT_S",
    "DEFAULT_PREVIEW_LIMIT",
    "DEFAULT_PREVIEW_TIMEOUT_S",
]
