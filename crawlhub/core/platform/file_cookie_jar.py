"""crawlhub.core.platform.file_cookie_jar — JSON-file backed cookie jar.

R4 plan §2.4 calls out three production cookie shapes:

* ``StringCookieJar``  — raw header value (weibo, qimai)            [done]
* ``FileCookieJar``    — cookie.json on disk (steam, bilibili, …)   [this file]
* ``DictCookieJar``    — in-memory ``dict[str, str]``               [future]

``FileCookieJar`` lazily reads the JSON file on each accessor call so that
hot-swapping cookies without restarting the process keeps working. The file
must be **either**:

* a flat ``{name: value}`` dict, or
* a list of ``{"name": ..., "value": ..., "domain": ...}`` records (browser
  export shape).

Both shapes are auto-normalised to ``dict[str, str]``.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class FileCookieJar:
    """Cookie container backed by a JSON file on disk.

    Satisfies the ``CookieJar`` Protocol (4 methods). Missing/empty/malformed
    files degrade gracefully to "logged out" — never raise.
    """

    def __init__(self, file_path: Path | str) -> None:
        self._path = Path(file_path)

    # ── CookieJar protocol ─────────────────────────────────

    def is_logged_in(self) -> bool:
        """True iff the file exists and parses to at least one cookie."""
        return bool(self.as_dict())

    def as_string(self, site: str | None = None) -> str:
        """Flatten to ``"k=v; k=v"`` for ``Cookie:`` HTTP header use.

        ``site`` is ignored: FileCookieJar is single-site. The parameter
        is kept only to satisfy the CookieJar Protocol contract.
        """
        return "; ".join(f"{k}={v}" for k, v in self.as_dict().items())

    # Reserved top-level keys in the JSON cookie file that are **not** real
    # cookie name/value pairs. These are sidecar fields written by the BBA
    # login flow (e.g. ``metadata.profile_dir`` binds the cookie to a
    # persistent browser user_data_dir). They must NOT be returned from
    # ``as_dict()`` because callers loop over the dict and inject every entry
    # into ``requests.Session.cookies`` — silently turning a metadata blob
    # into a fake cookie that the server then sees in the ``Cookie:`` header.
    _RESERVED_KEYS: frozenset[str] = frozenset({"metadata"})

    def as_dict(self, site: str | None = None) -> dict[str, str]:
        """Read & normalise the file to ``{name: value}``.

        ``site`` is ignored: FileCookieJar is single-site.

        Reserved top-level keys (see :attr:`_RESERVED_KEYS`) and any non-string
        values are filtered out — cookies are always strings, so a dict/list/
        None value indicates a sidecar field, never a real cookie.
        """
        if not self._path.exists():
            return {}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("FileCookieJar: cannot read %s: %s", self._path, exc)
            return {}

        # Shape 1: already a flat dict
        if isinstance(raw, dict):
            out: dict[str, str] = {}
            for k, v in raw.items():
                if k in self._RESERVED_KEYS:
                    continue
                if not isinstance(v, (str, int, float)):
                    # dict / list / None / bool → not a cookie
                    continue
                if not v:
                    continue
                out[str(k)] = str(v)
            return out

        # Shape 2: browser-export style list of records
        if isinstance(raw, list):
            out_list: dict[str, str] = {}
            for item in raw:
                if not isinstance(item, dict):
                    continue
                name = item.get("name")
                value = item.get("value")
                if name and value is not None:
                    out_list[str(name)] = str(value)
            return out_list

        logger.warning(
            "FileCookieJar: unsupported JSON shape in %s (expected dict or list, got %s)",
            self._path, type(raw).__name__,
        )
        return {}

    def source(self) -> str:
        return str(self._path)

    # ── Convenience for platform clients ───────────────────

    @property
    def path(self) -> Path:
        """Underlying file path (useful for save/refresh flows)."""
        return self._path

    def __repr__(self) -> str:  # pragma: no cover — debugging only
        state = "loaded" if self.is_logged_in() else "empty"
        return f"FileCookieJar(path={self._path!r}, state={state})"
