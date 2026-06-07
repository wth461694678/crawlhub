"""Cookie Throttle for CrawlHub.

CookieThrottle - Global per-cookie interval control with exponential backoff.
Provides per-platform throttling configured via Platform Management UI
(see ThrottleConfig.expected_interval). Used by daemon._run_task to space
out HTTP requests across all running tasks.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from crawlhub.core.config import get_config, ThrottleConfig, DEFAULT_THROTTLE_INTERVALS
from crawlhub.core.cookies import CookieInfo, get_cookie_store
from crawlhub.core.failure_detector import FailureMode

logger = logging.getLogger("crawlhub.cookie_dispatcher")


# ══════════════════════════════════════════════════════════════
# CookieThrottle - Global per-cookie interval control
# ══════════════════════════════════════════════════════════════


class CookieStatus(str, Enum):
    """Cookie health status."""

    VALID = "valid"
    UNKNOWN = "unknown"
    EXPIRED = "expired"
    BACKOFF = "backoff"


@dataclass
class CookieState:
    """Runtime state for a single cookie in the throttle system."""

    cookie_id: str  # "{platform}:{label}"
    platform: str
    label: str
    path: str
    status: CookieStatus = CookieStatus.UNKNOWN
    last_request_at: float = 0.0  # Timestamp of last request completion
    last_success_at: float = 0.0  # Timestamp of last successful request
    backoff_until: float = 0.0  # Timestamp when backoff expires
    backoff_count: int = 0  # Consecutive backoff triggers (for exponential calc)
    next_available_at: float = 0.0  # Earliest time this cookie can be used

    @property
    def is_in_backoff(self) -> bool:
        return time.time() < self.backoff_until

    @property
    def is_available(self) -> bool:
        """Cookie is available if not expired and not in backoff."""
        if self.status == CookieStatus.EXPIRED:
            return False
        if self.is_in_backoff:
            return False
        return True

    def to_dict(self) -> dict[str, Any]:
        now = time.time()
        return {
            "cookie_id": self.cookie_id,
            "platform": self.platform,
            "label": self.label,
            "status": self.status.value,
            "last_request_at": self.last_request_at,
            "last_success_at": self.last_success_at,
            "is_in_backoff": self.is_in_backoff,
            "backoff_until": self.backoff_until if self.is_in_backoff else None,
            "backoff_remaining_seconds": max(0, self.backoff_until - now) if self.is_in_backoff else 0,
            "backoff_count": self.backoff_count,
        }


class CookieThrottle:
    """Global per-cookie request interval controller (singleton).

    Controls request frequency for each cookie across ALL tasks:
    - Exponential distribution random intervals (simulates natural user behavior)
    - Min floor protection (never go below minimum interval)
    - FIFO queuing when multiple tasks compete for the same cookie
    - Exponential backoff on rate-limit/anti-crawl detection
    - Thread-safe for use with ThreadPoolExecutor

    Usage:
        throttle = get_cookie_throttle()
        throttle.acquire(cookie_id, platform)  # Blocks until interval satisfied
        try:
            response = make_request(...)
            throttle.report_success(cookie_id)
        except Exception:
            throttle.report_failure(cookie_id, failure_mode)
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._states: dict[str, CookieState] = {}  # cookie_id -> CookieState
        self._queues: dict[str, deque] = {}  # cookie_id -> FIFO queue of waiting thread events

    def _get_or_create_state(self, cookie_id: str, platform: str = "", label: str = "", path: str = "") -> CookieState:
        """Get or create cookie state. Must be called with lock held."""
        if cookie_id not in self._states:
            self._states[cookie_id] = CookieState(
                cookie_id=cookie_id,
                platform=platform or cookie_id.split(":")[0],
                label=label or cookie_id.split(":")[-1],
                path=path,
            )
        return self._states[cookie_id]

    def register_cookie(self, platform: str, label: str, path: str, status: CookieStatus = CookieStatus.UNKNOWN) -> str:
        """Register a cookie in the throttle system.

        Returns the cookie_id for future reference.
        """
        cookie_id = f"{platform}:{label}"
        with self._lock:
            state = self._get_or_create_state(cookie_id, platform, label, path)
            state.path = path
            if status != CookieStatus.UNKNOWN:
                state.status = status
        return cookie_id

    def acquire(self, cookie_id: str, platform: str | None = None) -> None:
        """Acquire permission to make a request with this cookie (blocking).

        Blocks until:
        1. The cookie's interval requirement is satisfied
        2. All earlier waiters in the FIFO queue have been served

        Args:
            cookie_id: The cookie identifier ("{platform}:{label}")
            platform: Platform name (for config lookup; extracted from cookie_id if None)
        """
        if platform is None:
            platform = cookie_id.split(":")[0]

        # Get throttle config for this platform
        config = get_config()
        tc = config.get_throttle_config(platform)

        # ──────────────────────────────────────────────────────────────
        #  R4-P14 观测增强：wall-clock 计时 + 决策路径分流标记
        # --------------------------------------------------------------
        #  目标：把"throttle 真正等了多久 + 是为什么等的"翻译给人看：
        #    A) gen=N.NNs        ← random.expovariate 这一发摇到了什么
        #    B) queue_pos        ← 进队列时身位（=0 表示队头）
        #    C) interval_left    ← 入口处距 next_available_at 还差多少
        #    D) waited=M.MMs     ← 真正阻塞了多久（time.perf_counter 一刀切）
        #    E) reason           ← fast / interval / queue / queue+interval
        #  外层 fetch_json 已经把 throttle_wait_ms 包了一层，但只有这里
        #  能告诉我们"长尾到底是 expovariate 摇高了，还是被前面排队卡住"。
        # ──────────────────────────────────────────────────────────────
        t_enter = time.perf_counter()

        # Generate random interval using exponential distribution
        interval = self._generate_interval(tc)

        # FIFO: create an event for this waiter
        my_event = threading.Event()

        with self._lock:
            state = self._get_or_create_state(cookie_id, platform)

            # Add to FIFO queue
            if cookie_id not in self._queues:
                self._queues[cookie_id] = deque()
            self._queues[cookie_id].append(my_event)
            queue_pos_on_enter = len(self._queues[cookie_id]) - 1  # 0 = 队头

            now = time.time()
            interval_left_on_enter = max(0.0, state.next_available_at - now)

            # If we're first in queue, check if we can go immediately
            if self._queues[cookie_id][0] is my_event:
                wait_until = state.next_available_at
                if now >= wait_until:
                    # Can go immediately
                    self._queues[cookie_id].popleft()
                    state.next_available_at = now + interval
                    state.last_request_at = now
                    waited = time.perf_counter() - t_enter
                    logger.info(
                        "[throttle] acquire cookie=%s waited=%.3fs gen=%.3fs "
                        "queue_pos=%d interval_left=%.3fs reason=fast",
                        cookie_id, waited, interval,
                        queue_pos_on_enter, interval_left_on_enter,
                    )
                    return

        # Need to wait - release lock and sleep
        # Default poll interval when we are not at the queue head (waiting for predecessors)
        default_poll = 0.1
        max_sleep = 0.5
        was_queued_behind = queue_pos_on_enter > 0
        while True:
            sleep_time = default_poll
            with self._lock:
                state = self._get_or_create_state(cookie_id, platform)

                # Check if we're at the front of the queue
                queue = self._queues.get(cookie_id, deque())
                if not queue or queue[0] is not my_event:
                    # Not our turn yet, poll at default cadence
                    sleep_time = default_poll
                else:
                    now = time.time()
                    wait_until = state.next_available_at
                    if now >= wait_until:
                        # Our turn and interval satisfied
                        queue.popleft()
                        # Recalculate interval for next waiter
                        new_interval = self._generate_interval(tc)
                        state.next_available_at = now + new_interval
                        state.last_request_at = now
                        # Wake up next waiter if any
                        if queue:
                            queue[0].set()
                        waited = time.perf_counter() - t_enter
                        # reason 三路分诊：
                        #   queue+interval = 既排了队又等了 interval（最贵）
                        #   queue          = 只是排队等前面那位
                        #   interval       = 自己队头但 next_available_at 没到
                        if was_queued_behind and interval_left_on_enter > 0:
                            reason = "queue+interval"
                        elif was_queued_behind:
                            reason = "queue"
                        else:
                            reason = "interval"
                        logger.info(
                            "[throttle] acquire cookie=%s waited=%.3fs gen=%.3fs "
                            "queue_pos=%d interval_left=%.3fs reason=%s",
                            cookie_id, waited, new_interval,
                            queue_pos_on_enter, interval_left_on_enter, reason,
                        )
                        return
                    else:
                        # Our turn but need to wait for interval to elapse
                        sleep_time = wait_until - now
            # Sleep outside lock; cap at max_sleep to stay responsive
            time.sleep(min(max(sleep_time, 0.0), max_sleep))

    def report_success(self, cookie_id: str) -> None:
        """Report a successful request for this cookie.

        Resets backoff counter and updates status to VALID.
        """
        with self._lock:
            if cookie_id in self._states:
                state = self._states[cookie_id]
                state.last_success_at = time.time()
                state.backoff_count = 0
                state.backoff_until = 0.0
                if state.status in (CookieStatus.UNKNOWN, CookieStatus.BACKOFF):
                    state.status = CookieStatus.VALID

    def report_failure(self, cookie_id: str, failure_mode: FailureMode) -> bool:
        """Report a failed request and apply appropriate action.

        Args:
            cookie_id: The cookie that failed
            failure_mode: The detected failure mode

        Returns:
            True if this failure caused the cookie to transition to EXPIRED state
            (either directly via COOKIE_EXPIRED, or by exhausting backoff retries).
            Caller can use this signal to emit on_cookie_invalid notifications.
        """
        with self._lock:
            if cookie_id not in self._states:
                return False
            state = self._states[cookie_id]
            already_expired = state.status == CookieStatus.EXPIRED

            if failure_mode == FailureMode.COOKIE_EXPIRED:
                # Direct expiration: cookie is definitively dead, no backoff dance.
                state.status = CookieStatus.EXPIRED
                state.backoff_until = 0.0
                logger.warning(
                    "[throttle] Cookie %s marked as EXPIRED (direct)", cookie_id
                )
                return not already_expired

            elif failure_mode == FailureMode.RATE_LIMITED:
                escalated = self._apply_backoff(state, base_multiplier=1.0)
                if escalated:
                    logger.warning(
                        "[throttle] Cookie %s ESCALATED to EXPIRED after exhausting backoff retries (rate limited)",
                        cookie_id,
                    )
                    return not already_expired
                logger.warning(
                    "[throttle] Cookie %s entered BACKOFF (rate limited) until %.1fs from now",
                    cookie_id, state.backoff_until - time.time(),
                )
                return False

            elif failure_mode == FailureMode.ANTI_CRAWL:
                # Anti-crawl uses doubled base (120s instead of 60s)
                escalated = self._apply_backoff(state, base_multiplier=2.0)
                if escalated:
                    logger.warning(
                        "[throttle] Cookie %s ESCALATED to EXPIRED after exhausting backoff retries (anti-crawl)",
                        cookie_id,
                    )
                    return not already_expired
                logger.warning(
                    "[throttle] Cookie %s entered BACKOFF (anti-crawl) until %.1fs from now",
                    cookie_id, state.backoff_until - time.time(),
                )
                return False

            return False

    def _apply_backoff(self, state: CookieState, base_multiplier: float = 1.0) -> bool:
        """Apply exponential backoff to a cookie. Must be called with lock held.

        Returns True if the backoff has been exhausted (count > max_exp), in which
        case the cookie is escalated to EXPIRED instead of entering BACKOFF.
        """
        config = get_config()
        tc = config.get_throttle_config(state.platform)

        base = tc.backoff_base_seconds * base_multiplier
        max_exp = tc.max_backoff_exponent

        # Increment without capping so we can detect "one more failure past the ceiling".
        state.backoff_count += 1

        if state.backoff_count > max_exp:
            # Already waited the maximum backoff and STILL failed -> cookie is truly dead.
            state.status = CookieStatus.EXPIRED
            state.backoff_until = 0.0
            state.next_available_at = 0.0
            return True

        backoff_seconds = (2 ** state.backoff_count) * base
        state.backoff_until = time.time() + backoff_seconds
        state.status = CookieStatus.BACKOFF
        # Also push next_available_at forward
        state.next_available_at = state.backoff_until
        return False

    def mark_expired(self, cookie_id: str) -> None:
        """Explicitly mark a cookie as expired."""
        with self._lock:
            if cookie_id in self._states:
                self._states[cookie_id].status = CookieStatus.EXPIRED

    def mark_valid(self, cookie_id: str) -> None:
        """Explicitly mark a cookie as valid (e.g. after probe)."""
        with self._lock:
            if cookie_id in self._states:
                state = self._states[cookie_id]
                state.status = CookieStatus.VALID
                state.backoff_count = 0
                state.backoff_until = 0.0

    def mark_unknown(self, cookie_id: str) -> None:
        """Mark a cookie as unknown (e.g. probe network error)."""
        with self._lock:
            if cookie_id in self._states:
                self._states[cookie_id].status = CookieStatus.UNKNOWN

    def get_cookie_state(self, cookie_id: str) -> CookieState | None:
        """Get current state of a cookie (thread-safe copy)."""
        with self._lock:
            state = self._states.get(cookie_id)
            if state is None:
                return None
            # Return a snapshot
            return CookieState(
                cookie_id=state.cookie_id,
                platform=state.platform,
                label=state.label,
                path=state.path,
                status=state.status,
                last_request_at=state.last_request_at,
                last_success_at=state.last_success_at,
                backoff_until=state.backoff_until,
                backoff_count=state.backoff_count,
                next_available_at=state.next_available_at,
            )

    def get_platform_states(self, platform: str) -> list[CookieState]:
        """Get states of all cookies for a platform."""
        with self._lock:
            return [
                CookieState(
                    cookie_id=s.cookie_id,
                    platform=s.platform,
                    label=s.label,
                    path=s.path,
                    status=s.status,
                    last_request_at=s.last_request_at,
                    last_success_at=s.last_success_at,
                    backoff_until=s.backoff_until,
                    backoff_count=s.backoff_count,
                    next_available_at=s.next_available_at,
                )
                for s in self._states.values()
                if s.platform == platform
            ]

    def get_all_states(self) -> dict[str, list[dict[str, Any]]]:
        """Get all cookie states grouped by platform."""
        with self._lock:
            result: dict[str, list[dict[str, Any]]] = {}
            for state in self._states.values():
                if state.platform not in result:
                    result[state.platform] = []
                result[state.platform].append(state.to_dict())
            return result

    def ensure_virtual_cookie(self, platform: str) -> str:
        """Register a virtual cookie for platforms that don't use real cookies (e.g. steam).

        This allows throttle to work for cookie-less platforms by creating a
        memory-only CookieState with no file path. The virtual cookie is always
        VALID and never expires (no real cookie file to go stale).

        Returns the virtual cookie_id.
        """
        virtual_id = f"{platform}:__virtual__"
        with self._lock:
            if virtual_id not in self._states:
                self._states[virtual_id] = CookieState(
                    cookie_id=virtual_id,
                    platform=platform,
                    label="__virtual__",
                    path="",  # no file path needed
                    status=CookieStatus.VALID,
                )
        return virtual_id

    def select_best_cookie(self, platform: str) -> CookieState | None:
        """Select the best available cookie for a platform.

        Priority: VALID > UNKNOWN > (wait for backoff)
        EXPIRED cookies are NEVER dispatched - they are considered permanently dead
        until the user manually refreshes them.
        Within same priority: prefer most recently successful.

        Returns None if no usable cookies registered for this platform.
        """
        with self._lock:
            platform_cookies = [
                s for s in self._states.values() if s.platform == platform
            ]
            if not platform_cookies:
                return None

            # Group by availability (EXPIRED cookies are excluded from all groups)
            valid = [s for s in platform_cookies if s.status == CookieStatus.VALID and not s.is_in_backoff]
            unknown = [s for s in platform_cookies if s.status == CookieStatus.UNKNOWN and not s.is_in_backoff]
            backoff = [s for s in platform_cookies if s.is_in_backoff and s.status != CookieStatus.EXPIRED]

            # R7 §6.2: Round-robin —— batch 多 child 时均匀分到不同 cookie
            # 排序键：(last_request_at asc, -last_success_at desc)
            # - last_request_at asc: "最久未用"优先（round-robin 核心）
            # - last_success_at desc: 同时间则健康度高的优先（次要 tiebreaker）
            for group in (valid, unknown):
                group.sort(key=lambda s: (s.last_request_at, -s.last_success_at))

            # Pick from best available group
            if valid:
                chosen = valid[0]
            elif unknown:
                chosen = unknown[0]
            elif backoff:
                # All in backoff - pick the one that expires soonest
                backoff.sort(key=lambda s: s.backoff_until)
                chosen = backoff[0]
            else:
                # All cookies are EXPIRED or there are no cookies left to use.
                return None

            # R7 §6.2: 选中即刷新 last_request_at（防同一时刻多 child 选到同 cookie）
            # 锁内原子操作（仍在 self._lock 内）；throttle.acquire 后续也会刷新
            # 同一字段，双重刷新无副作用（都是 time.time()）
            chosen.last_request_at = time.time()

            # Return a snapshot
            return CookieState(
                cookie_id=chosen.cookie_id,
                platform=chosen.platform,
                label=chosen.label,
                path=chosen.path,
                status=chosen.status,
                last_request_at=chosen.last_request_at,
                last_success_at=chosen.last_success_at,
                backoff_until=chosen.backoff_until,
                backoff_count=chosen.backoff_count,
                next_available_at=chosen.next_available_at,
            )

    def select_next_cookie(self, platform: str, exclude_ids: set[str] | None = None) -> CookieState | None:
        """Select next best cookie excluding already-tried ones.

        Used for retry logic: after a cookie fails, pick the next best one.
        """
        exclude = exclude_ids or set()
        with self._lock:
            platform_cookies = [
                s for s in self._states.values()
                if s.platform == platform and s.cookie_id not in exclude
            ]
            if not platform_cookies:
                return None

            # Same priority logic as select_best_cookie - EXPIRED cookies are NEVER picked.
            valid = [s for s in platform_cookies if s.status == CookieStatus.VALID and not s.is_in_backoff]
            unknown = [s for s in platform_cookies if s.status == CookieStatus.UNKNOWN and not s.is_in_backoff]
            available = valid + unknown

            if not available:
                # No usable cookies left (all expired or all in backoff among the remaining).
                # We do NOT fall back to EXPIRED cookies anymore - they are permanently dead.
                return None
            else:
                # R7 §6.2: 同 select_best_cookie 用 round-robin（retry 路径同样均匀分发）
                available.sort(key=lambda s: (s.last_request_at, -s.last_success_at))
                chosen = available[0]
                # R7: 选中即刷新
                chosen.last_request_at = time.time()

            return CookieState(
                cookie_id=chosen.cookie_id,
                platform=chosen.platform,
                label=chosen.label,
                path=chosen.path,
                status=chosen.status,
                last_request_at=chosen.last_request_at,
                last_success_at=chosen.last_success_at,
                backoff_until=chosen.backoff_until,
                backoff_count=chosen.backoff_count,
                next_available_at=chosen.next_available_at,
            )

    def all_expired(self, platform: str) -> bool:
        """Check if ALL cookies for a platform are expired."""
        with self._lock:
            platform_cookies = [
                s for s in self._states.values() if s.platform == platform
            ]
            if not platform_cookies:
                return True  # No cookies = effectively expired
            return all(s.status == CookieStatus.EXPIRED for s in platform_cookies)

    def cookie_count(self, platform: str) -> int:
        """Get number of registered cookies for a platform."""
        with self._lock:
            return sum(1 for s in self._states.values() if s.platform == platform)

    def load_platform_cookies(self, platform: str) -> int:
        """Load all cookies for a platform from CookieStore into throttle.

        Returns number of cookies loaded.
        """
        store = get_cookie_store()
        cookies = store.list_cookies(platform)
        count = 0
        for cookie_info in cookies:
            self.register_cookie(
                platform=platform,
                label=cookie_info.label,
                path=str(cookie_info.path),
            )
            count += 1
        if count:
            logger.info("[throttle] Loaded %d cookies for %s", count, platform)
        return count

    def refresh_config(self) -> None:
        """Refresh throttle config from disk (called after config update)."""
        # Config is read dynamically via get_config(), so this is a no-op
        # but exists for explicit signaling
        pass

    def reload_config(self, platform: str, config_dict: dict) -> None:
        """Hot-reload throttle config for a specific platform.

        Updates the in-memory config singleton so subsequent acquire() calls
        use the new intervals.
        """
        from crawlhub.core.config import get_config, ThrottleConfig
        config = get_config()
        tc = ThrottleConfig.from_dict(config_dict) if isinstance(config_dict, dict) else config_dict
        config.throttle[platform] = tc
        cap = tc.effective_truncate_cap
        cap_str = f"{cap:.2f}s(p={tc.truncate_percentile})" if cap is not None else "off"
        logger.info("[throttle] Reloaded config for %s: interval=%.2fs, floor=%.2fs, cap=%s",
                    platform, tc.expected_interval, tc.effective_min_floor, cap_str)

    @staticmethod
    def _generate_interval(tc: ThrottleConfig) -> float:
        """Generate a random interval using exponential distribution.

        Uses expovariate(1/expected) which produces intervals with mean = expected.
        Applies min_floor as lower bound and optional truncate_percentile as
        upper bound (clamp) to eliminate extreme long tails while preserving
        the human-like exponential shape.

        ──────────────────────────────────────────────────────────────
         设计哲学：clamp 而非 reject-resample
        ──────────────────────────────────────────────────────────────
         - clamp 一行钳位，无循环无分支，确定性 O(1)
         - 截断分布"右尾全堆在 cap 上"，等价于真实指数分布
           在 [min_floor, cap] 区间内的截尾分布
         - 无 cap 时（truncate_percentile=None）行为与改造前完全一致
        ──────────────────────────────────────────────────────────────
        """
        expected = tc.expected_interval
        if expected <= 0:
            return 0.0

        # Exponential distribution: mean = expected
        interval = random.expovariate(1.0 / expected)

        # Upper bound: percentile-based truncation (e.g. 0.95 → cap top 5%)
        cap = tc.effective_truncate_cap
        if cap is not None:
            interval = min(interval, cap)

        # Lower bound: min floor
        min_floor = tc.effective_min_floor
        return max(interval, min_floor)


# --- Singleton ---

_throttle_instance: CookieThrottle | None = None
_throttle_lock = threading.Lock()


def get_cookie_throttle() -> CookieThrottle:
    """Get the global CookieThrottle singleton."""
    global _throttle_instance
    if _throttle_instance is None:
        with _throttle_lock:
            if _throttle_instance is None:
                _throttle_instance = CookieThrottle()
    return _throttle_instance

