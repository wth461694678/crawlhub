"""crawlhub.core.platform.cookie_jar — uniform cookie container protocol.

R4 plan §2.4. ``CookieJar`` is a runtime-checkable Protocol so any class
with the right four methods passes ``isinstance(x, CookieJar)``. The
default ``StringCookieJar`` covers the simplest case (raw cookie string).

The other production cookie shapes (FileCookieJar over cookie.json,
DictCookieJar) live alongside their owning platforms and just need to
satisfy the same four methods.

R4 P12 (2026-05-25):
  * ``as_string`` and ``as_dict`` accept an optional ``site`` parameter
    so multi-site jars (kuaishou main / live) can satisfy the Protocol.
    Single-site jars ignore the parameter.
"""
from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable


@runtime_checkable
class CookieJar(Protocol):
    """Every platform cookie container must answer four questions.

    1. ``is_logged_in()`` — is the current cookie usable?
    2. ``as_string(site=None)`` — flatten to a "k=v; k=v" string for HTTP headers
    3. ``as_dict(site=None)``   — flatten to ``{name: value}`` for cookie-jar APIs
    4. ``source()``             — origin of the cookie (file path, "memory", URL)

    The optional ``site`` parameter on ``as_string`` / ``as_dict`` lets
    multi-site jars (e.g. kuaishou's main / live split) pick which slice
    of cookies to render. Single-site jars ignore it.
    """

    def is_logged_in(self) -> bool: ...

    def as_string(self, site: str | None = None) -> str: ...

    def as_dict(self, site: str | None = None) -> dict[str, str]: ...

    def source(self) -> str: ...


class StringCookieJar:
    """Default implementation backed by a single cookie string.

    Used by platforms whose cookie is a raw header value (weibo, qimai).
    For richer shapes (cookie.json files, browser DBs) implement another
    class that satisfies the ``CookieJar`` Protocol.
    """

    def __init__(self, cookie_str: str, source_path: Path | None = None) -> None:
        self._cookie_str = cookie_str.strip() if cookie_str else ""
        self._source = str(source_path) if source_path else "memory"

    def is_logged_in(self) -> bool:
        # Non-empty string is the cheapest possible "looks like a cookie"
        # heuristic. Real validity is decided by ``BaseHttpClient.probe``.
        return bool(self._cookie_str)

    def as_string(self, site: str | None = None) -> str:
        # site is ignored: this jar has only one site.
        return self._cookie_str

    def as_dict(self, site: str | None = None) -> dict[str, str]:
        # site is ignored: this jar has only one site.
        out: dict[str, str] = {}
        for chunk in self._cookie_str.split(";"):
            chunk = chunk.strip()
            if not chunk or "=" not in chunk:
                continue
            k, v = chunk.split("=", 1)
            out[k.strip()] = v.strip()
        return out

    def source(self) -> str:
        return self._source
