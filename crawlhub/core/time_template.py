"""Time template renderer for scheduling plans.

Public API
----------
- render(template, instance_dt, tz) -> str
- render_obj(obj, instance_dt, tz) -> Any   # recursive on dict/list, only
                                              renders string leaves.

Grammar
-------
A template is a regular string with zero or more ``${...}`` blocks.
Inside a block:
* TOKENs are ``YYYY``, ``MM``, ``DD``, ``HH``, ``mm``, ``ss``.
* Token offsets MUST be parenthesized: ``(TOKEN+N)`` / ``(TOKEN-N)``. N is a
  non-negative integer.
* An optional trailing ``_WEEKDAY`` (``MONDAY``..``SUNDAY``) snaps the
  instance datetime to that weekday in the SAME Monday-anchored week.
* Anything else inside the block (incl. dashes, digits, spaces) is LITERAL.

Semantics
---------
1. Snap to the requested weekday first (if any).
2. Apply each parenthesized offset, left to right, to a SHARED working
   datetime ``dt'``. ``YYYY``/``MM`` use ``relativedelta``, others use
   ``timedelta``.
3. Each TOKEN renders the corresponding component of the final ``dt'``,
   zero-padded (4 digits for YYYY, 2 digits for the rest).
4. Literal characters between/around tokens are emitted unchanged.

Errors
------
* Unbalanced ``${``: ``ValueError``.
* More than one weekday suffix: ``ValueError``.
* Nested parens (e.g. ``((MM-1)-1)``): ``ValueError``.
* Paren content not of form ``TOKEN[+/-N]``: ``ValueError``.
* Unknown weekday: ``ValueError``.

Locked design notes
-------------------
* Bare ``-`` followed by digits/letters in a template is LITERAL — see
  user decision 2026-05-12: arithmetic must use parens.
* Weekday snap precedes offsets — see spec note "先算 MONDAY...再算 +/-".
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from dateutil.relativedelta import relativedelta


_TOKEN_NAMES = ("YYYY", "MM", "DD", "HH", "mm", "ss")
_WEEKDAY_NAMES = (
    "MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY",
    "FRIDAY", "SATURDAY", "SUNDAY",
)
# longer names first to avoid prefix collision (none here, but defensive)
_TOKEN_RE = re.compile(r"YYYY|MM|DD|HH|mm|ss")


@dataclass
class _Token:
    name: str          # one of _TOKEN_NAMES
    offset: int = 0    # signed integer; unit depends on name


# ---------------------------------------------------------------------------
# Block parser
# ---------------------------------------------------------------------------

def _parse_block(body: str) -> tuple[list[object], int | None]:
    """Parse the inside of a ``${...}`` block.

    Returns
    -------
    (parts, weekday_idx)
        parts: list of ``_Token`` and ``str`` (literal) fragments, in order.
        weekday_idx: 0..6 (Mon..Sun) or None.
    """
    # Strip optional trailing _WEEKDAY (must be the very last segment).
    weekday_idx: int | None = None
    # Find the LAST '_' not inside parens.
    depth = 0
    last_underscore = -1
    for i, ch in enumerate(body):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "_" and depth == 0:
            last_underscore = i
    if last_underscore >= 0:
        suffix = body[last_underscore + 1:]
        if suffix in _WEEKDAY_NAMES:
            weekday_idx = _WEEKDAY_NAMES.index(suffix)
            body = body[:last_underscore]
        else:
            # Underscore present but suffix isn't a known weekday — reject
            # to surface typos like ${YYYYMMDD_FOOBAR}.
            raise ValueError(
                f"Unknown weekday suffix: {suffix!r}"
            )

    # Reject any remaining underscore (means more than one).
    if "_" in body:
        raise ValueError("Multiple weekday suffixes are not supported")

    parts: list[object] = []
    i = 0
    n = len(body)
    while i < n:
        ch = body[i]
        if ch == "(":
            # Find matching close paren; nesting forbidden.
            j = i + 1
            inner = []
            while j < n and body[j] != ")":
                if body[j] == "(":
                    raise ValueError(
                        "nested operators not supported"
                    )
                inner.append(body[j])
                j += 1
            if j >= n:
                raise ValueError("unbalanced parenthesis in block")
            tok = _parse_paren(''.join(inner))
            parts.append(tok)
            i = j + 1
            continue
        if ch == ")":
            raise ValueError("stray closing parenthesis")
        # Try to match a bare token (no offset).
        m = _TOKEN_RE.match(body, i)
        if m:
            parts.append(_Token(name=m.group(0), offset=0))
            i = m.end()
            continue
        # Otherwise, accumulate as literal until next token-start / paren.
        j = i
        while j < n and body[j] not in "()" and not _TOKEN_RE.match(body, j):
            j += 1
        parts.append(body[i:j])
        i = j

    return parts, weekday_idx


def _parse_paren(inner: str) -> _Token:
    """Parse ``TOKEN+/-N`` from inside parens. ``inner`` has no parens."""
    inner = inner.strip()
    m = re.fullmatch(r"(YYYY|MM|DD|HH|mm|ss)\s*([+\-])\s*(\d+)", inner)
    if not m:
        # Allow bare TOKEN inside parens too? Spec doesn't show that;
        # reject to keep grammar tight.
        raise ValueError(
            f"Parenthesized offset must be TOKEN+/-N, got: ({inner})"
        )
    name, op, num = m.group(1), m.group(2), int(m.group(3))
    if op == "-":
        num = -num
    return _Token(name=name, offset=num)


# ---------------------------------------------------------------------------
# Block renderer
# ---------------------------------------------------------------------------

def _snap_weekday(dt: datetime, target: int) -> datetime:
    """Snap ``dt`` to the requested weekday (0=Mon..6=Sun) in the SAME
    Monday-anchored week.
    """
    current = dt.weekday()  # 0=Mon..6=Sun
    delta_days = target - current
    return dt + timedelta(days=delta_days)


_OFFSET_UNITS = {
    "YYYY": "years",
    "MM": "months",
    "DD": "days",
    "HH": "hours",
    "mm": "minutes",
    "ss": "seconds",
}


def _apply_offset(dt: datetime, tok: _Token) -> datetime:
    if tok.offset == 0:
        return dt
    unit = _OFFSET_UNITS[tok.name]
    if unit in ("years", "months"):
        return dt + relativedelta(**{unit: tok.offset})
    return dt + timedelta(**{unit: tok.offset})


def _format_token(dt: datetime, name: str) -> str:
    if name == "YYYY":
        return f"{dt.year:04d}"
    if name == "MM":
        return f"{dt.month:02d}"
    if name == "DD":
        return f"{dt.day:02d}"
    if name == "HH":
        return f"{dt.hour:02d}"
    if name == "mm":
        return f"{dt.minute:02d}"
    if name == "ss":
        return f"{dt.second:02d}"
    raise AssertionError(f"unreachable: unknown token {name!r}")


def _render_block(body: str, instance_dt: datetime) -> str:
    parts, weekday_idx = _parse_block(body)
    dt = instance_dt
    if weekday_idx is not None:
        dt = _snap_weekday(dt, weekday_idx)
    # Apply offsets left-to-right.
    for p in parts:
        if isinstance(p, _Token):
            dt = _apply_offset(dt, p)
    # Now render parts.
    out: list[str] = []
    for p in parts:
        if isinstance(p, _Token):
            out.append(_format_token(dt, p.name))
        else:
            out.append(p)
    return "".join(out)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_BLOCK_RE = re.compile(r"\$\{([^{}]*)\}")


def render(template: str, instance_dt: datetime, tz: ZoneInfo) -> str:
    """Render a single template string against ``instance_dt`` (in ``tz``).

    ``instance_dt`` should be tz-aware; if naive it is assumed to already
    be in ``tz``. The ``tz`` parameter is reserved for future use (e.g.
    rendering tokens that depend on timezone offset); current implementation
    ignores it after we ensure ``instance_dt`` is tz-aware.
    """
    if instance_dt.tzinfo is None:
        instance_dt = instance_dt.replace(tzinfo=tz)
    else:
        instance_dt = instance_dt.astimezone(tz)

    # Reject unbalanced ${ before any rendering.
    # A simple count-based check suffices because nesting of blocks is not
    # part of the grammar.
    if template.count("${") != _count_balanced(template):
        raise ValueError("unbalanced '${' in template")

    def _sub(m: re.Match) -> str:
        return _render_block(m.group(1), instance_dt)

    return _BLOCK_RE.sub(_sub, template)


def _count_balanced(s: str) -> int:
    """Count ${...} blocks that have a matching closing brace."""
    n = 0
    i = 0
    while True:
        idx = s.find("${", i)
        if idx == -1:
            return n
        end = s.find("}", idx + 2)
        if end == -1:
            return n  # one fewer than ``${`` count -> mismatch detected upstream
        n += 1
        i = end + 1


def render_obj(obj: Any, instance_dt: datetime, tz: ZoneInfo) -> Any:
    """Recursively render every string leaf in ``obj``.

    dict / list / tuple are walked; other types are returned unchanged.
    Tuples become tuples again, preserving the input shape.
    """
    if isinstance(obj, str):
        return render(obj, instance_dt, tz)
    if isinstance(obj, dict):
        return {k: render_obj(v, instance_dt, tz) for k, v in obj.items()}
    if isinstance(obj, list):
        return [render_obj(v, instance_dt, tz) for v in obj]
    if isinstance(obj, tuple):
        return tuple(render_obj(v, instance_dt, tz) for v in obj)
    return obj
