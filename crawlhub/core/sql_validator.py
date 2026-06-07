"""Three-layer SQL validator for items_from.

Layers:
    L0  syntax        — DuckDB parser must succeed
    L1  policy        — exactly one statement, must be SELECT-only
    L2  schema bind   — column refs must resolve against declared upstream schemas;
                        the configured `field` must appear in the result columns

L0 and L1 are folded into a single call to `json_serialize_sql(...)`:
    - parse failure  → raise SQLSyntaxError
    - non-SELECT     → DuckDB itself errors with "Only SELECT statements can be
                       serialized to json!" → raise SQLPolicyError
    - statement count != 1 → raise SQLPolicyError

L2 builds a per-call in-memory DuckDB connection, registers each declared source
as an *empty* view typed against its schema (no actual file is read), then runs
EXPLAIN to surface unknown-column errors and DESCRIBE to extract result columns.

Public entrypoint:
    validate_items_from(items_from, store) -> None
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

import duckdb

from crawlhub.core.artifact_resolver import resolve_artifact
from crawlhub.core.registry import get_output_schema
from crawlhub.core.sql_errors import (
    ArtifactNotFoundError,
    ArtifactNotReadyError,
    SQLBindError,
    SQLFieldNotInResultError,
    SQLPolicyError,
    SQLSchemaUndeclaredError,
    SQLSyntaxError,
)

if TYPE_CHECKING:
    from crawlhub.core.sqlite_store import SqliteStateStore


# Identifier regex used to validate source aliases / field names before they are
# embedded into generated SQL. We never substitute user input into SQL strings
# without this guard. (DuckDB identifiers can be arbitrary inside quotes, but
# we want a simple, safe subset for items_from aliases.)
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def validate_items_from(
    items_from: dict[str, Any],
    store: "SqliteStateStore",
) -> None:
    """Validate an items_from spec without executing the SQL.

    Spec shape (current):
        {
            "sources": {alias: {"run_id": "..."} | {"path": "..."}, ...},
            "sql": "<SELECT ...>",
            "field": "<column name>",          # optional, default "item"
            "dedup": true | false,             # optional, ignored here (runtime concern)
            # NOTE: legacy keys {task_id, field at top level} are explicitly NOT supported.
        }

    Raises one of the SQLItemsFromError subclasses on any layer failing.
    Returns None on success.
    """
    _check_shape(items_from)

    sources: dict[str, dict] = items_from["sources"]
    sql: str = items_from["sql"]
    field: str = items_from.get("field", "item")
    sql_snippet = sql.strip()[:120]

    # ------------------------------------------------------------------
    # L1 + L0: policy and syntax via json_serialize_sql
    # ------------------------------------------------------------------
    _check_l0_l1(sql, sql_snippet)

    # ------------------------------------------------------------------
    # Resolve upstream schemas (collect, don't fail-fast yet — we want
    # to report the *first* offender clearly with its alias).
    # ------------------------------------------------------------------
    source_schemas: dict[str, dict[str, str]] = {}
    for alias, ref in sources.items():
        if not _IDENT_RE.match(alias):
            raise SQLPolicyError(
                f"source alias must match [A-Za-z_][A-Za-z0-9_]*, got {alias!r}",
                source=alias,
            )
        source_schemas[alias] = _resolve_source_schema(alias, ref, store, sql_snippet)

    # ------------------------------------------------------------------
    # L2: schema binding via empty stub views + EXPLAIN + DESCRIBE
    # ------------------------------------------------------------------
    con = duckdb.connect(":memory:")
    try:
        for alias, schema in source_schemas.items():
            con.execute(_build_stub_view_sql(alias, schema))

        # Bind check — DuckDB raises BinderException for unknown columns.
        try:
            con.execute(f"EXPLAIN {sql}")
        except duckdb.BinderException as e:  # type: ignore[attr-defined]
            raise SQLBindError(_clean_duckdb_msg(e), sql_snippet=sql_snippet) from e
        except duckdb.CatalogException as e:  # type: ignore[attr-defined]
            # Reference to a table alias that wasn't declared in `sources`.
            raise SQLBindError(_clean_duckdb_msg(e), sql_snippet=sql_snippet) from e

        # Result column check — `field` must appear in the SELECT list.
        try:
            described = con.execute(f"DESCRIBE {sql}").fetchall()
        except duckdb.Error as e:
            # DESCRIBE shouldn't fail if EXPLAIN passed, but be defensive.
            raise SQLBindError(_clean_duckdb_msg(e), sql_snippet=sql_snippet) from e

        result_cols = [row[0] for row in described]
        # Single-column policy: items_from feeds a single batch parameter, so
        # the SELECT list must produce exactly one column. Multiple columns
        # would be ambiguous (which one is the item value?) and silently
        # picking by `field` name hides bugs at submission time.
        if len(result_cols) != 1:
            raise SQLPolicyError(
                f"items_from.sql must SELECT exactly one column, found {len(result_cols)}: {result_cols!r}",
                sql_snippet=sql_snippet,
            )
        if field not in result_cols:
            raise SQLFieldNotInResultError(
                f"field {field!r} not in SELECT result columns {result_cols!r}",
                sql_snippet=sql_snippet,
            )
    finally:
        con.close()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _check_shape(items_from: dict[str, Any]) -> None:
    """Reject legacy {task_id, field} shape and require the new contract."""
    if not isinstance(items_from, dict):
        raise SQLPolicyError(f"items_from must be a dict, got {type(items_from).__name__}")

    # Legacy detection: surface a clear deprecation message rather than a
    # confusing "missing sources" error.
    if "task_id" in items_from and "sources" not in items_from:
        raise SQLPolicyError(
            "legacy items_from shape {task_id, field} is no longer supported. "
            "Use the new shape: {sources: {<alias>: {run_id|path}}, sql, field}."
        )

    if "sources" not in items_from or not isinstance(items_from["sources"], dict) or not items_from["sources"]:
        raise SQLPolicyError("items_from.sources must be a non-empty dict of {alias: ref}")
    if "sql" not in items_from or not isinstance(items_from["sql"], str) or not items_from["sql"].strip():
        raise SQLPolicyError("items_from.sql must be a non-empty string")


def _check_l0_l1(sql: str, sql_snippet: str) -> None:
    """Use DuckDB's json_serialize_sql to enforce SELECT-only single-statement.

    Behaviour table (DuckDB 1.x):
      - Parser failure              → execute() raises ParserException
      - Non-SELECT (DDL/DML/PRAGMA) → returns {"error": true,
                                       "error_message": "Only SELECT statements can be serialized to json!"}
      - Multiple SELECTs            → returns statements list of length > 1
      - Single SELECT               → statements list has exactly 1 entry

    Note: superficially valid SELECTs that fail later (e.g. `SELECT FORM y`
    where FORM is interpreted as a column alias) parse successfully here and
    fall through to L2's BinderException — that's correct, those are binding
    errors, not syntax errors.
    """
    con = duckdb.connect(":memory:")
    try:
        try:
            row = con.execute(
                "SELECT json_serialize_sql(?)", [sql]
            ).fetchone()
        except duckdb.ParserException as e:  # type: ignore[attr-defined]
            raise SQLSyntaxError(_clean_duckdb_msg(e), sql_snippet=sql_snippet) from e
        except duckdb.Error as e:
            # Catch-all for any other DuckDB error during serialization
            raise SQLSyntaxError(_clean_duckdb_msg(e), sql_snippet=sql_snippet) from e

        if row is None or row[0] is None:
            raise SQLSyntaxError("DuckDB returned no parse result", sql_snippet=sql_snippet)

        try:
            parsed = json.loads(row[0])
        except json.JSONDecodeError as e:
            raise SQLSyntaxError(f"failed to parse DuckDB AST JSON: {e}", sql_snippet=sql_snippet) from e

        if parsed.get("error"):
            err_msg = parsed.get("error_message", "unknown parser error")
            # DDL/DML/PRAGMA all yield this exact message.
            if "Only SELECT" in err_msg or "SELECT statements" in err_msg:
                raise SQLPolicyError(
                    "only SELECT statements are allowed in items_from.sql",
                    sql_snippet=sql_snippet,
                )
            raise SQLSyntaxError(err_msg, sql_snippet=sql_snippet)

        statements = parsed.get("statements", [])
        if len(statements) == 0:
            raise SQLPolicyError("SQL contains no executable statement", sql_snippet=sql_snippet)
        if len(statements) > 1:
            raise SQLPolicyError(
                f"items_from.sql must contain exactly one SELECT, found {len(statements)}",
                sql_snippet=sql_snippet,
            )
    finally:
        con.close()


def _resolve_source_schema(
    alias: str,
    ref: dict[str, Any],
    store: "SqliteStateStore",
    sql_snippet: str,
) -> dict[str, str]:
    """Determine the column->type schema for a single source binding.

    Decision tree:
        - {run_id}: look up the task; if its platform+action declares an
          ``output_schema`` in plugin.yaml, use the declared schema.
          Otherwise raise SQLSchemaUndeclaredError — we refuse to silently
          fall back to runtime inference, because that would defer breakage
          from "submission time" to "execution time" and confuse users.
        - {path}: external file; we don't know its schema declaratively, so
          we sample it via read_json_auto LIMIT 0 to infer columns. If the
          file doesn't exist yet, raise ArtifactNotFoundError (the user must
          point at a real file).

    Side note: for run_id sources we *don't* require the upstream task to be
    completed — schema validation is structural, not data-dependent. That means
    you can submit a downstream batch task while its upstream is still running;
    daemon-side dependency wiring will hold execution until the upstream is
    ready.
    """
    has_run_id = "run_id" in ref and ref["run_id"]
    has_path = "path" in ref and ref["path"]

    if has_run_id and has_path:
        raise SQLPolicyError(
            "source must have exactly one of {run_id, path}",
            source=alias,
        )
    if not has_run_id and not has_path:
        raise SQLPolicyError(
            "source must have one of {run_id, path}",
            source=alias,
        )

    # Reject reserved future keys at the validator boundary too — keeps the
    # contract consistent with resolve_artifact even before we hit runtime.
    for k in ("partition", "attempt", "template", "instance"):
        if k in ref:
            raise SQLPolicyError(
                f"source key {k!r} is reserved for the future scheduling model",
                source=alias,
            )

    if has_run_id:
        return _schema_for_run_id(alias, str(ref["run_id"]), store)

    # path source — sample the actual file
    return _schema_for_path(alias, str(ref["path"]), store, sql_snippet)


def _schema_for_run_id(
    alias: str,
    run_id: str,
    store: "SqliteStateStore",
) -> dict[str, str]:
    task = store.get_task(run_id)
    if task is None:
        raise ArtifactNotFoundError(
            f"task not found: run_id={run_id}",
            source=alias,
        )

    platform = task.get("platform")
    task_type = task.get("task_type")
    # A batch parent task has task_type="batch_run"; its real action lives in
    # snapshot_param.action. All children share that same action by construction
    # (a batch is just a union of N identical-shape single tasks), so the
    # parent's effective output schema is the child action's schema. This is
    # what users naturally expect — they pass the parent run_id they see in
    # the UI without knowing about the parent/child split.
    if task_type == "batch_run":
        parent_snapshot = task.get("snapshot_param") or {}
        action = parent_snapshot.get("action")
    else:
        action = task_type or task.get("action")
    if not platform or not action:
        raise SQLSchemaUndeclaredError(
            f"upstream task has no platform/action recorded: run_id={run_id}",
            source=alias,
        )

    schema = get_output_schema(platform, action)
    if not schema:
        raise SQLSchemaUndeclaredError(
            f"upstream {platform}.{action} has not declared an output schema; "
            f"it cannot be used as an items_from SQL source",
            source=alias,
        )
    return schema


def _schema_for_path(
    alias: str,
    path: str,
    store: "SqliteStateStore",
    sql_snippet: str,
) -> dict[str, str]:
    # This will raise ArtifactNotFoundError if the file is missing — caller
    # propagates it as-is.
    abs_path = resolve_artifact({"path": path}, store, alias=alias)

    con = duckdb.connect(":memory:")
    try:
        try:
            described = con.execute(
                "DESCRIBE SELECT * FROM read_json_auto(?, format='newline_delimited') LIMIT 0",
                [str(abs_path)],
            ).fetchall()
        except duckdb.Error as e:
            raise SQLBindError(
                f"failed to infer schema from path {path!r}: {_clean_duckdb_msg(e)}",
                source=alias,
                sql_snippet=sql_snippet,
            ) from e

    finally:
        con.close()

    return {row[0]: row[1] for row in described}


def _build_stub_view_sql(alias: str, schema: dict[str, str]) -> str:
    """Build CREATE VIEW <alias> AS SELECT NULL::T1 AS c1, ... WHERE 1=0.

    Empty view = correct types, zero rows. EXPLAIN/DESCRIBE on user SQL pass
    through it without touching real files. This is the workhorse trick that
    lets us validate schema before the upstream has produced anything.
    """
    if not schema:
        # Defensive: shouldn't happen because _resolve_source_schema raises.
        raise SQLSchemaUndeclaredError(
            f"empty schema for source {alias!r}", source=alias
        )
    cols = []
    for col, dtype in schema.items():
        # Column names embedded into SQL — quote them with DuckDB identifiers.
        safe_col = col.replace('"', '""')
        cols.append(f'NULL::{dtype} AS "{safe_col}"')
    select_list = ", ".join(cols)
    return f'CREATE VIEW "{alias}" AS SELECT {select_list} WHERE 1=0'


def _clean_duckdb_msg(e: BaseException) -> str:
    """Trim DuckDB's verbose multi-line error to a single readable line."""
    msg = str(e).strip()
    # Strip "LINE 1: ..." caret lines; they're not useful for users.
    out = []
    for line in msg.splitlines():
        if line.startswith("LINE ") or line.strip().startswith("^"):
            continue
        out.append(line)
    return " ".join(out).strip()


__all__ = ["validate_items_from"]
