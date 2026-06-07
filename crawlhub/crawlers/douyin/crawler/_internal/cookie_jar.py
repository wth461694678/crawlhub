"""Douyin cookie jar — pure data layer (R4 P12 + R5).

Subclass of ``MultiTokenCookieJar`` that:

* exposes douyin-specific token getters (``ms_token``, ``ttwid``, ``webid``,
  ``uifid``, ``verify_fp``) on top of the base cookies/extra_params dicts
* declares ``is_logged_in()`` based on the actual session cookie
  (``sessionid_ss`` or ``sessionid``) instead of the generic KEY_TOKENS-all
  rule, because that's the contract douyin's web auth uses

This module is the pure data layer — no HTTP, no Playwright, no failure
tracking. Health metrics live in ``health_tracker.py``; refresh logic
lives in ``refresh_orchestrator.py``.
"""
from __future__ import annotations

from pathlib import Path

from crawlhub.core.platform import MultiTokenCookieJar


class DouyinCookieJar(MultiTokenCookieJar):
    """Cookie container for douyin web crawler.

    Login is decided by ``sessionid_ss`` or ``sessionid`` — these are the
    cookies douyin's web auth issues post-login. Everything else (ttwid,
    msToken, ...) is auxiliary signing material that may exist before
    login but does not by itself constitute a session.
    """

    # KEY_TOKENS is left empty: we override is_logged_in() with a custom
    # disjunction (sessionid_ss OR sessionid). Subclassing this jar to add
    # required tokens is still possible by setting KEY_TOKENS — the parent's
    # rule (all KEY_TOKENS present) will then be ANDed with sessionid.
    KEY_TOKENS: tuple[str, ...] = ()

    def __init__(self, file_path: Path | str) -> None:
        super().__init__(file_path)

    # ── Login state ─────────────────────────────────────────

    def is_logged_in(self) -> bool:
        cookies = self.cookies
        return bool(cookies.get("sessionid_ss") or cookies.get("sessionid"))

    # ── Douyin-specific token accessors ─────────────────────

    @property
    def ms_token(self) -> str:
        """Get current msToken from cookies or extra_params."""
        cookies = self.cookies
        return cookies.get("msToken", "") or self.extra_params.get("msToken", "")

    @property
    def ttwid(self) -> str:
        """Get current ttwid from cookies."""
        return self.cookies.get("ttwid", "")

    @property
    def webid(self) -> str:
        """Get webid from extra_params or cookies (extra_params wins)."""
        return self.extra_params.get("webid", "") or self.cookies.get("webid", "")

    @property
    def uifid(self) -> str:
        """Get UIFID from cookies."""
        return self.cookies.get("UIFID", "")

    @property
    def verify_fp(self) -> str:
        """Get verifyFp / s_v_web_id from cookies."""
        return self.cookies.get("s_v_web_id", "")

    def __repr__(self) -> str:  # pragma: no cover
        state = "loaded" if self.is_logged_in() else "empty"
        return f"DouyinCookieJar(path={self._path!r}, state={state})"
