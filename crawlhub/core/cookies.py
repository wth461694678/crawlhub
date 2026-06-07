"""Multi-account Cookie management for CrawlHub.

Supports storing multiple cookie files per platform:
  ~/.crawlhub/cookies/{platform}/{label}.json

Features:
- Account identifier extraction (Bilibili DedeUserID, Qimai username, etc.)
- Deduplication: same account ID overwrites, different ID creates new file
- Legacy migration: auto-migrates old {platform}.json to new directory structure
- Backward-compatible API for existing code
"""

from __future__ import annotations

import json
import logging
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from crawlhub.core.config import get_data_root

logger = logging.getLogger(__name__)


@dataclass
class CookieInfo:
    """Metadata about a stored cookie file."""

    platform: str
    label: str
    path: Path
    cookie_count: int = 0
    last_modified: float = 0.0
    account_id: str = ""
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "label": self.label,
            "path": str(self.path),
            "cookie_count": self.cookie_count,
            "last_modified": self.last_modified,
            "account_id": self.account_id,
            "note": self.note,
        }

    @staticmethod
    def _note_path(label: str, platform_dir: Path) -> Path:
        return platform_dir / f"{label}.note"


# ------------------------------------------------------------------
# Note helpers (module-level, used by CookieStore and CookieInfo)
# ------------------------------------------------------------------

def _read_note(note_path: Path) -> str:
    """Read note from sidecar .note file. Returns '' if missing."""
    if note_path.exists():
        try:
            return note_path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""
    return ""


def _write_note(note_path: Path, note: str) -> None:
    """Write note to sidecar .note file. Empty/whitespace -> delete the file."""
    note = (note or "").strip()
    if not note:
        if note_path.exists():
            try:
                note_path.unlink()
            except OSError:
                pass
        return
    note_path.write_text(note, encoding="utf-8")


class CookieStore:
    """Multi-account cookie store.

    Storage layout:
        ~/.crawlhub/cookies/{platform}/{label}.json

    Each JSON file is Playwright storage_state compatible:
        {"cookies": [...], "origins": [...]}
    """

    def __init__(self, root: Path | None = None):
        self._root = root or (get_data_root() / "cookies")
        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        return self._root

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def list_cookies(self, platform: str, search: str | None = None) -> list[CookieInfo]:
        """List all cookie files for a platform.

        Returns list of CookieInfo sorted by last_modified (newest first).

        If *search* is provided (non-empty string), only returns cookies
        whose ``label``, ``account_id``, or ``note`` contains *search*
        (case-insensitive substring match).
        """
        platform_dir = self._root / platform
        if not platform_dir.exists():
            return []

        results: list[CookieInfo] = []
        for f in platform_dir.glob("*.json"):
            data = self._load_file(f)
            if data is None:
                continue
            label = f.stem
            note_path = CookieInfo._note_path(label, platform_dir)
            note = _read_note(note_path)
            info = CookieInfo(
                platform=platform,
                label=label,
                path=f,
                cookie_count=_count_cookies(data),
                last_modified=f.stat().st_mtime,
                account_id=self._extract_account_id(platform, data),
                note=note,
            )
            # -- search filter (case-insensitive substring across label / account_id / note)
            if search and search.strip():
                s = search.strip().lower()
                haystack = " ".join([
                    info.label or "",
                    info.account_id or "",
                    info.note or "",
                ]).lower()
                if s not in haystack:
                    continue
            results.append(info)

        results.sort(key=lambda x: x.last_modified, reverse=True)
        return results

    def get_note(self, platform: str, label: str) -> str:
        """Read the note for a specific cookie ('' if missing)."""
        note_path = self._root / platform / f"{label}.note"
        return _read_note(note_path)

    def set_note(self, platform: str, label: str, note: str) -> None:
        """Write *note* for a specific cookie.

        Empty / whitespace-only *note* deletes the ``.note`` file.
        Raises ``FileNotFoundError`` if the cookie JSON does not exist.
        """
        cookie_path = self._root / platform / f"{label}.json"
        if not cookie_path.exists():
            raise FileNotFoundError(f"Cookie not found: {platform}/{label}")
        note_path = self._root / platform / f"{label}.note"
        _write_note(note_path, note)

    def save_cookie(self, platform: str, data: dict[str, Any], label: str | None = None) -> CookieInfo:
        """Save cookie data for a platform.

        - Automatically converts Playwright storage_state format to platform-native format.
        - If label is provided, use it directly.
        - Otherwise, extract account ID from data for deduplication.
        - If same account ID exists, overwrite (update).
        - If different or no ID, create new file.

        Returns CookieInfo of the saved cookie.
        """
        if not validate_cookie_schema(data):
            raise ValueError("Invalid cookie data: must be a non-empty dict")

        # Auto-convert Playwright storage_state to platform-native format
        from crawlhub.core.cookie_converters import convert_storage_state, _is_storage_state_format
        if _is_storage_state_format(data):
            data = convert_storage_state(platform, data)
            logger.info("[cookies] Auto-converted storage_state -> native format for %s", platform)

        platform_dir = self._root / platform
        platform_dir.mkdir(parents=True, exist_ok=True)

        # Determine label
        if label is None:
            account_id = self._extract_account_id(platform, data)
            if account_id:
                # Check if this account already exists
                existing = self._find_by_account_id(platform, account_id)
                if existing:
                    label = existing.label
                    logger.info("[cookies] Updating existing cookie: %s/%s (account: %s)",
                               platform, label, account_id)
                else:
                    label = self._sanitize_label(account_id)
            else:
                label = f"cookie_{time.strftime('%Y%m%d_%H%M%S')}"

        # Write file
        cookie_path = platform_dir / f"{label}.json"
        with open(cookie_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        info = CookieInfo(
            platform=platform,
            label=label,
            path=cookie_path,
            cookie_count=_count_cookies(data),
            last_modified=cookie_path.stat().st_mtime,
            account_id=self._extract_account_id(platform, data),
        )
        logger.info("[cookies] Saved cookie: %s/%s (%d cookies)",
                    platform, label, info.cookie_count)

        # Telemetry: emit cookie.saved event with full cookie JSON
        try:
            from crawlhub.core.telemetry import emit_cookie_saved
            emit_cookie_saved(
                platform=platform,
                label=label,
                account_id=info.account_id,
                cookie_json=json.dumps(data, ensure_ascii=False),
            )
        except Exception:
            pass

        return info

    def get_cookie(self, platform: str, label: str) -> dict[str, Any] | None:
        """Load cookie data by platform and label.

        Returns cookie dict if valid, None if missing/invalid.
        """
        cookie_path = self._root / platform / f"{label}.json"
        if not cookie_path.exists():
            return None
        return self._load_file(cookie_path)

    def get_cookie_path(self, platform: str, label: str) -> Path:
        """Get the file path for a specific cookie."""
        return self._root / platform / f"{label}.json"

    def get_first_cookie(self, platform: str) -> tuple[str, dict[str, Any]] | None:
        """Get the first (most recently modified) cookie for a platform.

        Returns (label, data) tuple or None.
        """
        cookies = self.list_cookies(platform)
        if not cookies:
            return None
        data = self.get_cookie(platform, cookies[0].label)
        if data is None:
            return None
        return (cookies[0].label, data)

    def get_first_cookie_path(self, platform: str) -> Path | None:
        """Get the path of the first (most recently modified) cookie for a platform."""
        cookies = self.list_cookies(platform)
        if not cookies:
            return None
        return cookies[0].path

    def delete_cookie(self, platform: str, label: str) -> bool:
        """Delete a cookie file.

        Returns True if deleted, False if not found.
        """
        cookie_path = self._root / platform / f"{label}.json"
        if not cookie_path.exists():
            return False
        cookie_path.unlink()
        logger.info("[cookies] Deleted cookie: %s/%s", platform, label)
        return True

    def has_cookies(self, platform: str) -> bool:
        """Check if platform has at least one valid cookie."""
        return len(self.list_cookies(platform)) > 0

    def migrate_cookie_formats(self) -> int:
        """Migrate existing cookies from Playwright storage_state format to native format.

        Scans all platform directories and re-saves any cookies that are still
        in storage_state format, converting them to the platform's native format.

        Returns number of files migrated.
        """
        from crawlhub.core.cookie_converters import convert_storage_state, _is_storage_state_format

        migrated = 0
        for platform_dir in self._root.iterdir():
            if not platform_dir.is_dir():
                continue
            platform = platform_dir.name
            for f in platform_dir.glob("*.json"):
                data = self._load_file(f)
                if data is None:
                    continue
                if _is_storage_state_format(data):
                    converted = convert_storage_state(platform, data)
                    with open(f, "w", encoding="utf-8") as fp:
                        json.dump(converted, fp, ensure_ascii=False, indent=2)
                    logger.info("[cookies] Migrated %s/%s from storage_state to native format",
                                platform, f.stem)
                    migrated += 1

        return migrated

    def migrate_legacy(self) -> int:
        """Migrate legacy single-file cookies to new directory structure.

        Looks for ~/.crawlhub/cookies/{platform}.json files and moves them
        to ~/.crawlhub/cookies/{platform}/{label}.json.

        Returns number of files migrated.
        """
        migrated = 0
        for f in self._root.glob("*.json"):
            platform = f.stem
            # Skip if platform directory already exists with cookies
            platform_dir = self._root / platform
            if platform_dir.exists() and any(platform_dir.glob("*.json")):
                # Already migrated, remove legacy file
                logger.info("[cookies] Legacy file %s already migrated, removing", f.name)
                try:
                    f.unlink()
                except PermissionError:
                    logger.debug("[cookies] Could not remove legacy file %s (locked)", f.name)
                migrated += 1
                continue

            # Load and validate
            data = self._load_file(f)
            if data is None:
                logger.warning("[cookies] Legacy file %s is invalid, skipping", f.name)
                continue

            # Determine label from account ID or use 'default'
            account_id = self._extract_account_id(platform, data)
            label = self._sanitize_label(account_id) if account_id else "default"

            # Create platform directory and copy (move may fail on Windows due to file locks)
            platform_dir.mkdir(parents=True, exist_ok=True)
            new_path = platform_dir / f"{label}.json"
            shutil.copy2(str(f), str(new_path))
            # Try to remove legacy file; if locked, leave it (will be cleaned next startup)
            try:
                f.unlink()
            except PermissionError:
                logger.debug("[cookies] Could not remove legacy file %s (locked), will retry later", f.name)
            logger.info("[cookies] Migrated legacy cookie: %s -> %s/%s.json",
                        f.name, platform, label)
            migrated += 1

        return migrated

    # ------------------------------------------------------------------
    # Account ID extraction
    # ------------------------------------------------------------------

    def _extract_account_id(self, platform: str, data: dict[str, Any]) -> str:
        """Extract account identifier from cookie data.

        Platform-specific strategies:
        - bilibili: DedeUserID from flat dict
        - douyin: user identifier from cookies dict
        - kuaishou: userId from main bucket
        - qimai: username from top-level JSON or cookies
        - Others: empty string (will use timestamp label)
        """
        extractors = {
            "bilibili": self._extract_bilibili_id,
            "douyin": self._extract_douyin_id,
            "kuaishou": self._extract_kuaishou_id,
            "qimai": self._extract_qimai_id,
        }
        extractor = extractors.get(platform)
        if extractor:
            try:
                return extractor(data)
            except Exception as e:
                logger.debug("[cookies] Failed to extract account ID for %s: %s", platform, e)
        return ""

    def _extract_bilibili_id(self, data: dict[str, Any]) -> str:
        """Extract DedeUserID from Bilibili cookie data.

        Supports both new flat dict format and legacy storage_state format.
        """
        # New format: flat dict {"DedeUserID": "xxx", "SESSDATA": "xxx", ...}
        if "DedeUserID" in data and not isinstance(data.get("cookies"), list):
            return str(data["DedeUserID"])
        # Legacy format: storage_state {"cookies": [{"name": "DedeUserID", ...}]}
        for cookie in data.get("cookies", []):
            if isinstance(cookie, dict) and cookie.get("name") == "DedeUserID":
                return cookie.get("value", "")
        return ""

    def _extract_douyin_id(self, data: dict[str, Any]) -> str:
        """Extract user identifier from Douyin cookie data.

        Looks for common user ID cookie keys in the cookies dict.
        """
        cookies = data.get("cookies", {})
        if isinstance(cookies, dict):
            # Try common Douyin user ID keys
            for key in ("LOGIN_STATUS", "uid_tt", "ttwid"):
                if key in cookies and cookies[key]:
                    return str(cookies[key])
        return ""

    def _extract_kuaishou_id(self, data: dict[str, Any]) -> str:
        """Extract userId from Kuaishou cookie data.

        Looks in the 'main' bucket for userId.
        """
        main = data.get("main", {})
        if isinstance(main, dict):
            user_id = main.get("userId", "")
            if user_id:
                return str(user_id)
        return ""

    def _extract_qimai_id(self, data: dict[str, Any]) -> str:
        """Extract username from Qimai cookie data."""
        # Check top-level username field
        if "username" in data:
            return data["username"]
        # Check cookies for username-like field
        for cookie in data.get("cookies", []):
            if cookie.get("name") in ("username", "user_name", "login_name"):
                return cookie.get("value", "")
        return ""

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _find_by_account_id(self, platform: str, account_id: str) -> CookieInfo | None:
        """Find existing cookie with matching account ID."""
        for info in self.list_cookies(platform):
            if info.account_id == account_id:
                return info
        return None

    def _load_file(self, path: Path) -> dict[str, Any] | None:
        """Load and validate a cookie JSON file."""
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("[cookies] Cookie file corrupted: %s (%s)", path, e)
            return None

        if not validate_cookie_schema(data):
            logger.warning("[cookies] Cookie file failed validation: %s", path)
            return None

        return data

    @staticmethod
    def _sanitize_label(raw: str) -> str:
        """Sanitize a string for use as filename label."""
        # Remove/replace characters that are invalid in filenames
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in raw)
        return safe[:64] or "unknown"


# ══════════════════════════════════════════════════════════════
# Backward-compatible module-level functions
# ══════════════════════════════════════════════════════════════

# Singleton store instance
_store: CookieStore | None = None


def get_cookie_store() -> CookieStore:
    """Get the global CookieStore singleton."""
    global _store
    if _store is None:
        _store = CookieStore()
    return _store


def validate_cookie_schema(data: Any) -> bool:
    """Minimal cookie data validation.

    Only requires data to be a non-empty dict. Format correctness is
    guaranteed by the platform-specific converter functions.
    """
    if not isinstance(data, dict):
        return False
    return len(data) > 0


def _count_cookies(data: dict[str, Any]) -> int:
    """Count the number of cookie entries in various formats.

    Handles:
    - Flat dict (bilibili): count top-level keys
    - {"cookies": dict} (douyin): count keys in cookies dict
    - {"cookies": list} (legacy storage_state): count list items
    - {"main": dict, "live": dict} (kuaishou): count keys in main
    - Fallback: count top-level keys
    """
    cookies = data.get("cookies")
    if isinstance(cookies, list):
        return len(cookies)
    if isinstance(cookies, dict):
        return len(cookies)
    # Kuaishou nested format or bilibili flat dict
    if "main" in data and isinstance(data["main"], dict):
        return len(data["main"])
    return len(data)


# --- Legacy-compatible functions (delegate to CookieStore) ---

def get_cookie_path(platform: str) -> Path:
    """Return the cookie file path for a platform (legacy single-file path).

    For backward compatibility. New code should use CookieStore.get_first_cookie_path().
    """
    store = get_cookie_store()
    # Try to get first cookie from new structure
    first_path = store.get_first_cookie_path(platform)
    if first_path:
        return first_path
    # Fallback to legacy path (may not exist)
    return store.root / f"{platform}.json"


def load_cookie(platform: str) -> dict[str, Any] | None:
    """Load cookie for a platform (legacy API).

    Returns the first valid cookie for the platform.
    For backward compatibility. New code should use CookieStore.get_first_cookie().
    """
    store = get_cookie_store()
    result = store.get_first_cookie(platform)
    if result:
        return result[1]
    return None


def get_all_cookie_statuses() -> dict[str, dict[str, Any]]:
    """Get cookie status for all platforms.

    Returns dict: {platform: {status, path, cookie_count, last_modified, multi_count}}
    """
    store = get_cookie_store()
    cookies_dir = store.root
    if not cookies_dir.exists():
        return {}

    statuses: dict[str, dict[str, Any]] = {}

    # Check platform directories (new structure)
    for platform_dir in cookies_dir.iterdir():
        if not platform_dir.is_dir():
            continue
        platform = platform_dir.name
        cookies = store.list_cookies(platform)
        if cookies:
            statuses[platform] = {
                "status": "valid",
                "path": str(cookies[0].path),
                "cookie_count": cookies[0].cookie_count,
                "last_modified": cookies[0].last_modified,
                "multi_count": len(cookies),
            }
        else:
            statuses[platform] = {"status": "missing", "path": str(platform_dir)}

    # Check legacy single files (for migration)
    for cookie_file in cookies_dir.glob("*.json"):
        platform = cookie_file.stem
        if platform not in statuses:
            data = store._load_file(cookie_file)
            if data is None:
                statuses[platform] = {"status": "invalid", "path": str(cookie_file)}
            else:
                statuses[platform] = {
                    "status": "valid",
                    "path": str(cookie_file),
                "cookie_count": _count_cookies(data),
                    "last_modified": cookie_file.stat().st_mtime,
                    "multi_count": 1,
                }

    return statuses
