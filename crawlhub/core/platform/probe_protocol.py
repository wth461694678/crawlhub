"""crawlhub.core.platform.probe_protocol — capability-probe contract.

Defines the *five-field* ProbeResult dataclass (R4 plan §2.6) and the
``CapabilityProbe`` Protocol that ``BaseHttpClient`` directly inherits
to force every platform client to implement ``probe()``.

Why a Protocol *and* a base class? The Protocol documents the contract
in a way mypy/static checkers understand. ``BaseHttpClient`` then mixes
it into a concrete ABC so Python raises ``TypeError`` at instantiation
if a subclass forgot ``probe()``.
"""
from __future__ import annotations

import re as _re
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass
class ProbeResult:
    """Five-field contract returned by ``BaseHttpClient.probe()``.

    Fields:
        ok: True iff the probed capability is available.
        api: Identifier of the endpoint that was probed (e.g. "user/info").
        latency_ms: Round-trip time of the probe request, in milliseconds.
        error: Short error message when ok=False, else None.
        extras: Free-form per-platform diagnostics (must default to {}).
    """

    ok: bool
    api: str
    latency_ms: int
    error: str | None = None
    extras: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class CapabilityProbe(Protocol):
    """Protocol pinning the ``probe(task_type)`` signature.

    ``BaseHttpClient`` inherits this Protocol, so any concrete client
    that omits ``probe()`` cannot be instantiated.
    """

    def probe(self, task_type: str = "default") -> ProbeResult: ...


# ---------------------------------------------------------------------------
# HTML login-detection helpers
# ---------------------------------------------------------------------------
# Shared utilities for probe() and BBA login polling so that HTML-parsing
# logic is written once and reused across both paths (per R7 P6 design
# decision: "probe 一致，不重复写 HTML 解析逻辑").

def _css_class_pattern(class_name: str) -> _re.Pattern[str]:
    """Return a compiled regex that matches an HTML ``class`` attribute
    containing *class_name* as a standalone CSS class.

    Handles ``class="foo"`` as well as ``class="bar foo baz"``.
    ``data-v-*`` attributes are ignored — only the ``class=""`` attribute
    is inspected.
    """
    esc = _re.escape(class_name)
    # Match class="..." where the class is a standalone token: preceded by
    # start-of-attribute or whitespace, followed by end-of-attribute or
    # whitespace.
    #
    # ⚠️ Earlier version used `(?<=\s|^){esc}(?=\s|")` — the lookbehind
    # `(?<=\s|^)` is variable-width (\s is 1 char, ^ is 0), which Python's
    # stdlib `re` rejects with "look-behind requires fixed-width pattern".
    # That compile error was silently swallowed up the call chain and
    # surfaced as "probe failed" — even when cookies were perfectly valid.
    # Use a non-capturing alternation around the class token instead, which
    # is fixed-width-safe and semantically equivalent.
    #
    # \b doesn't help here because hyphenated names like
    # "sidebar-login-button" have hyphens as word boundaries internally.
    return _re.compile(
        rf'''class="(?:[^"]*\s)?{esc}(?:\s[^"]*)?"''',
        _re.IGNORECASE,
    )


def has_css_class(html: str, class_name: str) -> bool:
    """Return True if *html* contains an element whose ``class`` attribute
    includes *class_name* as a standalone CSS class.

    This is the shared building block for every platform's
    ``check_login_from_html()`` — both the HTTP probe path (``probe()``)
    and the BBA browser-polling path call into this, ensuring identical
    detection logic.
    """
    return bool(_css_class_pattern(class_name).search(html))


def check_login_from_html(
    html: str,
    *,
    logged_out_class: str | None = None,
    logged_out_text: str | None = None,
) -> tuple[bool, dict[str, Any]]:
    """Generic "is the user logged in?" check from page HTML.

    Convention: if the page **does NOT** contain a "logged-out indicator"
    element (typically a login button), the cookie is valid.

    Parameters
    ----------
    html:
        Full page HTML source.
    logged_out_class:
        CSS class name of the element that appears ONLY when the user is
        NOT logged in (e.g. ``"sidebar-login-button"``).  If absent from
        the HTML → logged in.
    logged_out_text:
        Optional text that must appear alongside the class to confirm
        the element is a login indicator (e.g. ``"登录/注册"``).
        Only checked when *logged_out_class* is also provided.

    Returns
    -------
    (is_logged_in, extras) tuple.
    """
    if logged_out_class is None:
        return False, {"reason": "no logged_out_class configured"}

    class_present = has_css_class(html, logged_out_class)

    if logged_out_text:
        indicator_present = class_present and logged_out_text in html
    else:
        indicator_present = class_present

    if indicator_present:
        return False, {"reason": f"{logged_out_class} present (not logged in)"}
    return True, {}
