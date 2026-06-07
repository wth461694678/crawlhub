"""Platform-specific login state detection.

Each platform defines which cookies indicate a valid login session.
Used by the Playwright login flow to determine when the user has
successfully logged in, replacing the generic "cookie count increase"
and "URL change" heuristics.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


# Platform login indicators: {platform: [list of cookie names that indicate login]}
# If ANY of the required cookies are present and non-empty, login is considered successful.
#
# IMPORTANT: indicator cookies must be ones that are ONLY issued AFTER successful login,
# never seeded by the login page for unauthenticated visitors. False positives here cause
# the Playwright login flow to "save and close" before the user has actually logged in.
#
# weibo notes (verified 2026-05-15 via fresh-chromium probe):
#   - SUB / SUBP exist on .weibo.com even for unauthenticated visitors who just opened
#     the SSO page (94 / 56 bytes long). DO NOT use them as login indicators.
#   - SCF (.weibo.com, ~88 bytes) and ALF (.weibo.com, ~13 bytes) are only issued after
#     a successful login. SCF is the session credential; ALF is the long-term token expiry.
_LOGIN_INDICATORS: dict[str, list[str]] = {
    "douyin": ["LOGIN_STATUS", "uid_tt", "sessionid_ss"],
    "kuaishou": ["userId", "kuaishou.server.web_st"],
    "bilibili": ["SESSDATA", "DedeUserID"],
    "weibo": ["SCF", "ALF"],
    # qimai notes (verified 2026-05-16 via cookie comparison):
    #   - USERINFO, AUTHKEY, aso_ucenter are ONLY present after successful login
    #   - ci_session and kc_fit_v are NOT present in actual cookies, removed
    #   - USERINFO contains encrypted user info, AUTHKEY is the auth token
    "qimai": ["USERINFO", "AUTHKEY"],
}


def check_login_cookies(platform: str, cookies: list[dict[str, Any]]) -> bool:
    """Check if cookies contain valid login state for the given platform.

    Args:
        platform: Platform name (douyin, kuaishou, bilibili, weibo, qimai)
        cookies: List of Playwright cookie dicts, each with "name" and "value" keys.

    Returns:
        True if login indicators are found in cookies, False otherwise.
    """
    indicators = _LOGIN_INDICATORS.get(platform)
    if indicators is None:
        # Unknown platform: fallback to "has any cookies" check
        logger.debug("[login_detectors] No indicators defined for %s, fallback to cookie count", platform)
        return len(cookies) > 5

    # Build a lookup of cookie name -> value
    cookie_map: dict[str, str] = {}
    for c in cookies:
        name = c.get("name", "")
        value = c.get("value", "")
        if name:
            cookie_map[name] = value

    # Check if any indicator cookie is present and non-empty
    for indicator in indicators:
        value = cookie_map.get(indicator, "")
        if value and value != "0":
            logger.debug(
                "[login_detectors] %s login detected via cookie '%s'",
                platform, indicator,
            )
            return True

    return False


def get_login_indicators(platform: str) -> list[str]:
    """Get the list of login indicator cookie names for a platform.

    Args:
        platform: Platform name.

    Returns:
        List of cookie names that indicate login, or empty list if unknown.
    """
    return _LOGIN_INDICATORS.get(platform, [])
