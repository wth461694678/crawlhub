"""crawlhub.core.platform.base_models — BaseRecord (R4 plan §2.3).

A *method-only* base class for every platform record dataclass. It has
no fields of its own because Python forbids a non-default field after a
default field across an inheritance chain — the safest design is to
keep ``BaseRecord`` field-less and let each subclass be a plain
``@dataclass`` with whatever fields it needs.

Hard rules (pinned by tests T6, T7, T8):
  * ``to_dict()`` returns ``dataclasses.asdict(self)``, no transformations.
  * ``expected_schema()`` reverse-derives DuckDB types from type hints.
  * datetime fields are NOT auto-coerced to ISO strings.
"""
from __future__ import annotations

from dataclasses import asdict, fields
from typing import Any, get_type_hints


# Mapping from Python types to the DuckDB names used in plugin.yaml output_schema.
_TYPE_MAP: dict[type, str] = {
    bool: "BOOLEAN",
    int: "BIGINT",
    str: "VARCHAR",
    float: "DOUBLE",
}


class BaseRecord:
    """Method-only base for every platform record dataclass.

    Subclass example::

        @dataclass
        class PingResult(BaseRecord):
            ok: bool
            message: str
    """

    def to_dict(self) -> dict[str, Any]:
        """Return ``dataclasses.asdict(self)``. No coercion."""
        return asdict(self)

    @classmethod
    def expected_schema(cls) -> dict[str, str]:
        """Reverse-derive a DuckDB-style schema from this dataclass's type hints.

        Returns ``{field_name: DUCKDB_TYPE}`` where DUCKDB_TYPE is one of
        ``BOOLEAN | BIGINT | VARCHAR | DOUBLE`` for the basic mapped
        types, and ``"JSON"`` for everything else (datetimes, lists,
        dicts, custom classes — they round-trip as JSON in DuckDB).
        """
        hints = get_type_hints(cls)
        out: dict[str, str] = {}
        for f in fields(cls):  # type: ignore[arg-type]
            tp = hints.get(f.name, f.type)
            out[f.name] = _TYPE_MAP.get(tp, "JSON")
        return out
