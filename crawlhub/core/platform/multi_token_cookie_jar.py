"""crawlhub.core.platform.multi_token_cookie_jar — file jar with side tokens.

R4 P12 §1.1 / Phase 1.

``MultiTokenCookieJar`` is a ``FileCookieJar`` that also tracks platform-
specific tokens that may live next to the main cookie blob (e.g. ttwid,
passport_csrf_token for douyin). On top of the file-backed ``cookies``
container it exposes a ``tokens`` dict and includes them in
``as_string()`` / ``as_dict()`` output.

The on-disk schema this jar reads is intentionally lenient — to support
existing platform cookie files we accept three shapes:

  Shape A: ``{"cookie_string": "k=v; k=v", "cookies": {...},
              "extra_headers": {...}, "extra_params": {...}, ...}``
           (douyin's current format)

  Shape B: flat dict ``{name: value, ...}`` (browser export)

  Shape C: list of records ``[{"name": ..., "value": ...}, ...]``

Tokens declared in ``KEY_TOKENS`` are extracted from the cookies dict
itself; ``extra_params`` (douyin's webid / msToken) is preserved as a
separate ``extra_params`` attribute for callers that need URL-param
injection.

Subclasses set ``KEY_TOKENS`` to declare which tokens decide
``is_logged_in()``. For a no-op default we treat any non-empty cookies
dict as logged-in (matching ``FileCookieJar`` behaviour).
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from .file_cookie_jar import FileCookieJar

logger = logging.getLogger(__name__)


class MultiTokenCookieJar(FileCookieJar):
    """File-backed jar that also tracks platform-specific tokens.

    On top of ``FileCookieJar``, exposes ``self.tokens`` and (optional)
    ``self.extra_params`` / ``self.extra_headers`` dicts. ``KEY_TOKENS``
    declared by a subclass controls ``is_logged_in()`` semantics:

      * empty tuple — fall back to ``FileCookieJar.is_logged_in()``
      * non-empty   — ALL tokens must be present and non-empty
    """

    #: Subclasses override; keys to extract from cookies as required tokens.
    KEY_TOKENS: tuple[str, ...] = ()

    def __init__(self, file_path: Path | str) -> None:
        super().__init__(file_path)
        # Lazy state — populated on first read.
        self._cache_cookies: dict[str, str] | None = None
        self._cache_tokens: dict[str, str] | None = None
        self._cache_extra_headers: dict[str, str] = {}
        self._cache_extra_params: dict[str, str] = {}
        self._cache_mtime: float = -1.0

    # ── Loading / caching ───────────────────────────────────

    def _refresh_cache(self) -> None:
        """Re-read the file if its mtime changed (or first call)."""
        path = self._path
        if not path.exists():
            self._cache_cookies = {}
            self._cache_tokens = {}
            self._cache_extra_headers = {}
            self._cache_extra_params = {}
            self._cache_mtime = -1.0
            return

        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = -1.0
        if (
            self._cache_cookies is not None
            and abs(mtime - self._cache_mtime) < 1e-6
        ):
            return  # Cache still valid.

        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("MultiTokenCookieJar: cannot read %s: %s", path, exc)
            self._cache_cookies = {}
            self._cache_tokens = {}
            self._cache_extra_headers = {}
            self._cache_extra_params = {}
            self._cache_mtime = mtime
            return

        cookies, extra_headers, extra_params = self._parse_payload(raw)
        self._cache_cookies = cookies
        self._cache_tokens = {
            k: cookies.get(k, "") for k in self.KEY_TOKENS
        }
        self._cache_extra_headers = extra_headers
        self._cache_extra_params = extra_params
        self._cache_mtime = mtime

    @staticmethod
    def _parse_payload(raw):
        """Normalise any of the 3 supported shapes into (cookies, headers, params)."""
        cookies: dict[str, str] = {}
        extra_headers: dict[str, str] = {}
        extra_params: dict[str, str] = {}

        if isinstance(raw, dict):
            extra_headers = dict(raw.get("extra_headers") or {})
            extra_params = dict(raw.get("extra_params") or {})

            cookie_blob = raw.get("cookies")
            if isinstance(cookie_blob, dict):
                cookies = {str(k): str(v) for k, v in cookie_blob.items() if v}
            elif isinstance(cookie_blob, list):
                for item in cookie_blob:
                    if isinstance(item, dict):
                        name = item.get("name")
                        value = item.get("value")
                        if name and value is not None:
                            cookies[str(name)] = str(value)
            elif "cookie_string" in raw and isinstance(raw["cookie_string"], str):
                # Shape A fallback: parse the string itself.
                for chunk in raw["cookie_string"].split(";"):
                    chunk = chunk.strip()
                    if "=" in chunk:
                        k, v = chunk.split("=", 1)
                        cookies[k.strip()] = v.strip()
            else:
                # Shape B: flat dict at top level (no 'cookies' key).
                for k, v in raw.items():
                    if k in ("extra_headers", "extra_params", "saved_at",
                             "cookie_string"):
                        continue
                    if isinstance(v, (str, int, float)):
                        cookies[str(k)] = str(v)

            # Shape A precedence: if cookie_string disagrees with cookies dict,
            # prefer the explicit cookies dict (it's what the writer regenerates
            # from). Only fall back to cookie_string if cookies dict is empty.
            if not cookies and isinstance(raw.get("cookie_string"), str):
                for chunk in raw["cookie_string"].split(";"):
                    chunk = chunk.strip()
                    if "=" in chunk:
                        k, v = chunk.split("=", 1)
                        cookies[k.strip()] = v.strip()

        elif isinstance(raw, list):
            for item in raw:
                if isinstance(item, dict):
                    name = item.get("name")
                    value = item.get("value")
                    if name and value is not None:
                        cookies[str(name)] = str(value)

        return cookies, extra_headers, extra_params

    # ── Public accessors (cookies + tokens + extras) ────────

    @property
    def cookies(self) -> dict[str, str]:
        """Live view of the parsed cookies dict (read on demand)."""
        self._refresh_cache()
        return dict(self._cache_cookies or {})

    @property
    def tokens(self) -> dict[str, str]:
        """Tokens declared by ``KEY_TOKENS`` (read on demand)."""
        self._refresh_cache()
        return dict(self._cache_tokens or {})

    @property
    def extra_headers(self) -> dict[str, str]:
        self._refresh_cache()
        return dict(self._cache_extra_headers)

    @property
    def extra_params(self) -> dict[str, str]:
        self._refresh_cache()
        return dict(self._cache_extra_params)

    def get_token(self, name: str) -> str:
        """Convenience: read a single token (or look it up in extra_params)."""
        self._refresh_cache()
        if name in (self._cache_tokens or {}):
            return self._cache_tokens[name]
        return self._cache_extra_params.get(name, "")

    # ── CookieJar protocol ──────────────────────────────────

    def is_logged_in(self) -> bool:
        self._refresh_cache()
        cookies = self._cache_cookies or {}
        if self.KEY_TOKENS:
            return all(cookies.get(k) for k in self.KEY_TOKENS)
        return bool(cookies)

    def as_dict(self, site: str | None = None) -> dict[str, str]:
        # site is ignored: MultiTokenCookieJar is single-site.
        self._refresh_cache()
        return dict(self._cache_cookies or {})

    def as_string(self, site: str | None = None) -> str:
        # site is ignored: MultiTokenCookieJar is single-site.
        self._refresh_cache()
        return "; ".join(f"{k}={v}" for k, v in (self._cache_cookies or {}).items() if v)

    def source(self) -> str:
        return str(self._path)

    # ── Write path (subclass-friendly) ──────────────────────

    def replace_all(
        self,
        cookies: dict[str, str],
        *,
        extra_headers: dict[str, str] | None = None,
        extra_params: dict[str, str] | None = None,
    ) -> None:
        """Replace the in-memory cookie state.

        Call ``save()`` afterwards to persist. This is the canonical
        "main-cookie write path" used by login channels.
        """
        self._cache_cookies = {str(k): str(v) for k, v in (cookies or {}).items() if v != ""}
        self._cache_tokens = {k: self._cache_cookies.get(k, "") for k in self.KEY_TOKENS}
        if extra_headers is not None:
            self._cache_extra_headers = dict(extra_headers)
        if extra_params is not None:
            self._cache_extra_params = dict(extra_params)
        # Mark cache as fresh so the next is_logged_in / as_dict skips re-read.
        self._cache_mtime = time.time()

    def update_token(self, name: str, value: str) -> None:
        """Update a single cookie value (in memory; call ``save()`` to persist)."""
        self._refresh_cache()
        if self._cache_cookies is None:
            self._cache_cookies = {}
        self._cache_cookies[name] = value
        if name in self.KEY_TOKENS:
            self._cache_tokens = self._cache_tokens or {}
            self._cache_tokens[name] = value

    def merge_response_cookies(self, set_cookie_headers) -> None:
        """Merge HTTP ``Set-Cookie`` header(s) into the in-memory cookie dict.

        ``set_cookie_headers`` may be a single string or an iterable of
        strings (response.headers.get_all("set-cookie")). Existing cookies
        with the same name are overwritten.
        """
        from http.cookies import SimpleCookie

        if isinstance(set_cookie_headers, str):
            set_cookie_headers = [set_cookie_headers]

        self._refresh_cache()
        cookies = self._cache_cookies or {}
        changed = False
        for raw_header in set_cookie_headers or []:
            if not raw_header:
                continue
            sc = SimpleCookie()
            try:
                sc.load(raw_header)
            except Exception:  # noqa: BLE001
                continue
            for name, morsel in sc.items():
                cookies[name] = morsel.value
                changed = True

        if changed:
            self._cache_cookies = cookies
            self._cache_tokens = {k: cookies.get(k, "") for k in self.KEY_TOKENS}

    def save(self) -> None:
        """Persist the current in-memory state back to the file.

        Schema written matches the douyin-style envelope so existing
        readers (browser_bridge, anything that scrapes cookie.json
        directly) keep working::

            {"cookie_string": "...",
             "cookies": {...},
             "extra_headers": {...},
             "extra_params": {...},
             "saved_at": "YYYY-MM-DD HH:MM:SS"}

        Note: ``save()`` does NOT call ``_refresh_cache()`` first — that
        would clobber any pending ``replace_all``/``update_token`` edits
        if the on-disk file is older. Callers that want to discard
        in-memory edits should construct a new jar instance.
        """
        cookies = self._cache_cookies or {}
        cookie_string = "; ".join(f"{k}={v}" for k, v in cookies.items() if v)
        payload = {
            "cookie_string": cookie_string,
            "cookies": cookies,
            "extra_headers": self._cache_extra_headers,
            "extra_params": self._cache_extra_params,
            "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        try:
            self._cache_mtime = self._path.stat().st_mtime
        except OSError:
            pass

    def __repr__(self) -> str:  # pragma: no cover — debugging only
        state = "loaded" if self.is_logged_in() else "empty"
        return f"MultiTokenCookieJar(path={self._path!r}, state={state}, key_tokens={self.KEY_TOKENS})"
