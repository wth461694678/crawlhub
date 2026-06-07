"""Notification service for CrawlHub.

Handles:
- Webhook-based notifications (Enterprise WeChat Bot)
- Event subscription and routing
- Exponential backoff retry (5s -> 10s -> 20s, 3 attempts)
- Cookie failure counter (rolling 24h window, >=3 triggers, per-platform, 24h dedup)
- Message templates (markdown format, pure ASCII markers)
"""

from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from crawlhub.core.daemon import CrawlHubDaemon

logger = logging.getLogger(__name__)

# Retry delays (exponential backoff)
_RETRY_DELAYS = [5, 10, 20]

# Dedup window for cookie_invalid notifications (24h)
_DEDUP_WINDOW = 86400


class NotificationService:
    """Async notification service decoupled from task execution."""

    def __init__(self, daemon: CrawlHubDaemon):
        self.daemon = daemon
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="notify")
        self._lock = threading.Lock()
        # Dedup tracking: {event_key: last_sent_timestamp}
        self._sent_cache: dict[str, float] = {}

    def start(self) -> None:
        """Register as event listener on the daemon."""
        self.daemon.register_event_listener(self._on_event)
        logger.info("[OK] NotificationService started")

    def stop(self) -> None:
        """Shutdown the notification executor."""
        self._executor.shutdown(wait=False)

    def _on_event(self, event_type: str, data: dict) -> None:
        """Handle daemon events and dispatch notifications."""
        # Check if any rules match this event
        # Normalize: match both "on_task_completed" and "task_completed" forms
        event_type_bare = event_type.removeprefix("on_")
        rules = self.daemon.store.list_rules()
        matching_rules = [
            r for r in rules
            if (r["event_type"] == event_type or r["event_type"] == event_type_bare)
            and r.get("enabled", 1)
        ]

        if not matching_rules:
            return

        # Special handling for cookie_invalid: rolling window + dedup
        if event_type == "on_cookie_invalid":
            platform = data.get("platform", "unknown")
            # Record failure
            self.daemon.store.record_cookie_failure(platform, time.time())
            # Check threshold (>=3 in 24h)
            count = self.daemon.store.get_cookie_failure_count(platform, window_seconds=_DEDUP_WINDOW)
            if count < 3:
                return
            # Dedup: only send once per 24h per platform
            dedup_key = f"cookie_invalid_{platform}"
            if self._is_deduped(dedup_key):
                return
            self._mark_sent(dedup_key)

        # Build message
        message = self._build_message(event_type, data)

        # Send to all matching channels
        channels = self.daemon.store.list_channels()
        channel_map = {c["name"]: c for c in channels}

        for rule in matching_rules:
            channel = channel_map.get(rule["channel_name"])
            if channel and channel.get("enabled", 1):
                webhook_url = channel["webhook_url"]
                self._executor.submit(self._send_with_retry, webhook_url, message)

    def send_test(self, channel_name: str) -> bool:
        """Send a test notification to verify webhook configuration."""
        channels = self.daemon.store.list_channels()
        channel = next((c for c in channels if c["name"] == channel_name), None)
        if not channel:
            return False

        message = _TEMPLATES["test"].format(
            time=time.strftime("%Y-%m-%d %H:%M:%S"),
        )
        return self._send_webhook(channel["webhook_url"], message)

    def _send_with_retry(self, webhook_url: str, message: str) -> bool:
        """Send notification with exponential backoff retry."""
        for attempt, delay in enumerate(_RETRY_DELAYS):
            success = self._send_webhook(webhook_url, message)
            if success:
                return True
            logger.warning("[WARN] Notification attempt %d failed, retrying in %ds", attempt + 1, delay)
            time.sleep(delay)

        logger.error("[ERR] Notification failed after %d attempts", len(_RETRY_DELAYS))
        return False

    def _send_webhook(self, webhook_url: str, message: str) -> bool:
        """Send a single webhook request (Enterprise WeChat Bot format)."""
        payload = {
            "msgtype": "markdown",
            "markdown": {
                "content": message,
            },
        }
        try:
            resp = httpx.post(webhook_url, json=payload, timeout=10)
            if resp.status_code == 200:
                body = resp.json()
                if body.get("errcode") == 0:
                    return True
                logger.warning("[WARN] Webhook response error: %s", body)
            else:
                logger.warning("[WARN] Webhook HTTP %d: %s", resp.status_code, resp.text[:200])
        except Exception as e:
            logger.warning("[WARN] Webhook request failed: %s", e)
        return False

    def _build_message(self, event_type: str, data: dict) -> str:
        """Build notification message from template."""
        template = _TEMPLATES.get(event_type, _TEMPLATES["default"])
        try:
            return template.format(**data, time=time.strftime("%Y-%m-%d %H:%M:%S"))
        except KeyError:
            # Fallback: dump data as-is
            return _TEMPLATES["default"].format(
                event_type=event_type,
                data=json.dumps(data, ensure_ascii=False, indent=2),
                time=time.strftime("%Y-%m-%d %H:%M:%S"),
            )

    def _is_deduped(self, key: str) -> bool:
        """Check if this notification was already sent within dedup window."""
        with self._lock:
            last_sent = self._sent_cache.get(key, 0)
            return (time.time() - last_sent) < _DEDUP_WINDOW

    def _mark_sent(self, key: str) -> None:
        """Mark a notification as sent for dedup tracking."""
        with self._lock:
            self._sent_cache[key] = time.time()


# ═══════════════════════════════════════════════════════════
#  Message Templates (Markdown, pure ASCII markers)
# ═══════════════════════════════════════════════════════════

_TEMPLATES = {
    "on_task_completed": (
        "**[OK] Task Completed**\n"
        "> Platform: {platform}\n"
        "> Task ID: {task_id}\n"
        "> Time: {time}"
    ),
    "on_task_failed": (
        "**[ERR] Task Failed**\n"
        "> Platform: {platform}\n"
        "> Task ID: {task_id}\n"
        "> Error: {error}\n"
        "> Time: {time}"
    ),
    "on_cookie_invalid": (
        "**[WARN] Cookie Invalid**\n"
        "> Platform: {platform}\n"
        "> Multiple auth failures detected (>=3 in 24h).\n"
        "> Please refresh cookies.\n"
        "> Time: {time}"
    ),
    "on_disk_low": (
        "**[WARN] Disk Space Low**\n"
        "> Free space: {free_mb} MB\n"
        "> Automatic cleanup triggered.\n"
        "> Time: {time}"
    ),
    "on_daemon_unexpected_exit": (
        "**[ERR] Daemon Unexpected Exit**\n"
        "> Reason: {reason}\n"
        "> Severity: {severity}\n"
        "> Time: {time}"
    ),
    "on_plan_step_submit_failed": (
        "**[ERR] Scheduling Plan Step Submit Failed**\n"
        "> Group: {group_name}\n"
        "> Plan: {plan_name}\n"
        "> Step: {step_index}\n"
        "> Error: {error}\n"
        "> Instance Time: {instance_time}\n"
        "> Time: {time}"
    ),
    "test": (
        "**[TEST] CrawlHub Notification Test**\n"
        "> This is a test message.\n"
        "> If you see this, webhook is working correctly.\n"
        "> Time: {time}"
    ),
    "default": (
        "**[INFO] CrawlHub Event**\n"
        "> Event: {event_type}\n"
        "> Data: {data}\n"
        "> Time: {time}"
    ),
}
