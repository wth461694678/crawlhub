"""SQL items_from exception hierarchy.

All errors raised by the items_from SQL pipeline (validator / artifact_resolver /
sql_runner) inherit from `SQLItemsFromError`. The string form is stable:

    [ERR_CODE] message | source=<alias> sql_snippet=<...>

so callers can persist `str(exc)` directly to `task.error` and surface it
verbatim to the API client / UI without further parsing.

NOTE: error codes are part of the public contract (frontend matches on them).
Do not rename without bumping the API surface.
"""

from __future__ import annotations


class SQLItemsFromError(Exception):
    """Base class for every items_from SQL failure.

    Subclasses MUST set the class-level `code` attribute. The constructor
    accepts an optional `source` (alias of the offending source binding)
    and `sql_snippet` (truncated user SQL for context). Both are appended
    to the string form when present.
    """

    code: str = "ERR_SQL_GENERIC"

    def __init__(
        self,
        message: str,
        *,
        source: str | None = None,
        sql_snippet: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.source = source
        self.sql_snippet = sql_snippet

    def __str__(self) -> str:
        parts = [f"[{self.code}] {self.message}"]
        ctx: list[str] = []
        if self.source:
            ctx.append(f"source={self.source}")
        if self.sql_snippet:
            snippet = self.sql_snippet.replace("\n", " ")
            if len(snippet) > 120:
                snippet = snippet[:117] + "..."
            ctx.append(f"sql_snippet={snippet}")
        if ctx:
            parts.append(" | ".join(ctx))
        return " | ".join(parts)

    def to_dict(self) -> dict[str, str | None]:
        """Serialize for JSON API responses."""
        return {
            "error_code": self.code,
            "message": self.message,
            "source": self.source,
            "sql_snippet": self.sql_snippet,
        }


# ---------------------------------------------------------------------------
# L1 — policy / safety
# ---------------------------------------------------------------------------

class SQLPolicyError(SQLItemsFromError):
    """The SQL violates the safety policy (non-SELECT or denylisted keyword)."""

    code = "ERR_SQL_POLICY"


# ---------------------------------------------------------------------------
# L0 — syntax
# ---------------------------------------------------------------------------

class SQLSyntaxError(SQLItemsFromError):
    """DuckDB parser failed to parse the SQL (purely syntactic)."""

    code = "ERR_SQL_SYNTAX"


# ---------------------------------------------------------------------------
# L2 — schema binding
# ---------------------------------------------------------------------------

class SQLBindError(SQLItemsFromError):
    """SQL is syntactically valid but binds against an unknown column / table.

    Raised when EXPLAIN against the schema-stub tables fails.
    """

    code = "ERR_SQL_BIND"


class SQLFieldNotInResultError(SQLItemsFromError):
    """`field` is not among the SELECT result columns of the user SQL."""

    code = "ERR_SQL_FIELD_NOT_IN_RESULT"


class SQLSchemaUndeclaredError(SQLItemsFromError):
    """A run_id source references an action that has no declared output_schema.

    Most upstream platforms have not yet declared output schemas; using one of
    them as an SQL source is rejected at L2 with this error so the user gets
    a clear "the upstream contract is missing" signal instead of a runtime
    bind failure.
    """

    code = "ERR_SCHEMA_UNDECLARED"


# ---------------------------------------------------------------------------
# Artifact resolution (pre-execution)
# ---------------------------------------------------------------------------

class ArtifactNotFoundError(SQLItemsFromError):
    """The referenced run_id does not exist in the task store, or the path
    points to a non-existent file."""

    code = "ERR_ARTIFACT_NOT_FOUND"


class ArtifactNotReadyError(SQLItemsFromError):
    """The referenced run_id exists but its task is not yet completed.

    This is distinct from NOT_FOUND because the dependency layer is expected
    to wait for it; surfacing this error during preview tells the UI to show
    "schema OK but upstream not ready" (HTTP 409).
    """

    code = "ERR_ARTIFACT_NOT_READY"


# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------

class SQLTimeoutError(SQLItemsFromError):
    """The SQL exceeded the configured statement_timeout."""

    code = "ERR_SQL_TIMEOUT"


__all__ = [
    "SQLItemsFromError",
    "SQLPolicyError",
    "SQLSyntaxError",
    "SQLBindError",
    "SQLFieldNotInResultError",
    "SQLSchemaUndeclaredError",
    "ArtifactNotFoundError",
    "ArtifactNotReadyError",
    "SQLTimeoutError",
]
