"""Stable key for browser session reuse."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SessionKey:
    """Browser identity boundary.

    R5 keeps the default key at platform + cookie_id. Action is deliberately
    excluded so different BBA actions can reuse one authenticated browser state.
    """

    platform: str
    cookie_id: str
    cookie_path: str = ""
    browser_profile_id: str = "default"
