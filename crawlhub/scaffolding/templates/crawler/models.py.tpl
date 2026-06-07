"""{{platform_display}} data models — the record-shape contract (R4).

Every record dataclass inherits :class:`crawlhub.core.platform.base_models.BaseRecord`,
which provides:

    * ``to_dict()``         — same shape as ``dataclasses.asdict``.
    * ``expected_schema()`` — reverse-derives DuckDB types from type hints,
      used by R7 / C3 to confirm the dataclass and ``plugin.yaml`` agree.

R3 / Layer responsibility:
    * Define one ``@dataclass`` per action whose record shape is written
      to DuckDB. Field names and count MUST equal the action's
      ``output_schema`` keys in ``plugin.yaml`` (C3).
    * Inherit ``BaseRecord`` for free ``to_dict`` + schema derivation.

This file MUST NOT:
    * Make HTTP requests.
    * Touch ``TaskContext`` or any crawlhub runtime API.
"""

from __future__ import annotations

from dataclasses import dataclass

from crawlhub.core.platform.base_models import BaseRecord


@dataclass
class PingResult(BaseRecord):
    """Record shape for ``actions.ping`` (see ``plugin.yaml``).

    Field names and count MUST exactly match ``output_schema`` in
    ``plugin.yaml`` (schema v2 form)::

        output_schema:
          ok:
            type: BOOLEAN
            label: "成功标记"
          message:
            type: VARCHAR
            label: "返回消息"
    """

    ok: bool
    message: str
