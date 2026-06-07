"""Scheduling-plan runtime: cron/interval/once trigger management + fire path.

This module owns the second APScheduler instance in the daemon (the first
being the legacy ``crawlhub.core.scheduler`` cron loop, which is dead code
at the time this was written but kept for reference).

Lifecycle (managed by daemon.py — see Task 2.6):
    1. ``daemon.initialize()``  -> ``PlanScheduler(daemon)`` constructed.
    2. ``daemon.start()`` (after notification_service.start) -> ``start()``,
       which calls ``bootstrap()`` to register all enabled triggers.
    3. ``daemon.graceful_shutdown()`` -> ``shutdown(wait=True)`` BEFORE
       executor shutdowns, so any in-flight ``fire()`` finishes cleanly.

Concurrency model (per requirements.md §7.4):
    - Different plans run independently — they share the BackgroundScheduler
      thread pool but never serialize on each other.
    - The same plan's two triggers MAY overlap if they fire near-simultaneously.
      ``fire()`` is reentrant; each call is one fire-instance with its own
      step_id list. Tasks within one fire are still strictly sequential
      (per spec §7.2).
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

from crawlhub.core.batch import BatchConfig
from crawlhub.core.plan_runtime import extract_resolved_deps, resolve_step_refs
from crawlhub.core.time_template import render_obj


logger = logging.getLogger(__name__)


class PlanScheduler:
    """Owns one BackgroundScheduler + the fire/preview/sync API."""

    def __init__(self, daemon):
        self._daemon = daemon
        self._store = daemon.store
        # Background, not blocking — caller (daemon) is in charge of lifecycle.
        # We do NOT pass a job_store: APScheduler's MemoryJobStore is fine
        # because we re-bootstrap every daemon start from the DB. Persisting
        # the jobs would fight the source-of-truth (plans + plan_triggers).
        self._scheduler = BackgroundScheduler(daemon=True)
        self._started = False

    # -- Lifecycle -----------------------------------------------------------

    def start(self) -> None:
        """Bootstrap from DB and start the scheduler thread."""
        if self._started:
            return
        self._scheduler.start()
        self._started = True
        n = self.bootstrap()
        logger.info("[PLAN] Bootstrap registered %d triggers", n)

    def shutdown(self, wait: bool = True) -> None:
        """Stop the scheduler thread.

        ``wait=True`` blocks until any in-flight ``fire()`` returns. We do
        not interrupt mid-fire because that would leave a partial step
        sequence with dangling task IDs.
        """
        if not self._started:
            return
        try:
            self._scheduler.shutdown(wait=wait)
        finally:
            self._started = False

    def bootstrap(self) -> int:
        """Read plans+triggers from DB, register a job per qualifying trigger.

        A trigger qualifies iff:
            plan.enabled = 1 AND trigger.enabled = 1 AND
            NOT (kind='once' AND now > expr in plan.tz)

        Returns the number of jobs registered.
        """
        registered = 0
        for plan in self._store.list_plans(enabled=1):
            registered += self._register_plan_triggers(plan)
        return registered

    # -- Sync API (called by routes after mutations) -------------------------

    def sync_plan(self, plan_id: str) -> None:
        """Drop+rebuild every job that belongs to this plan.

        Spec §7.3: "after any plan mutation, drop+rebuild that plan's jobs."
        We don't try to be incremental — the trigger set is small and
        idempotency wins over micro-optimization here.
        """
        # Remove existing jobs for this plan's triggers.
        triggers = self._store.list_plan_triggers(plan_id)
        for tr in triggers:
            self._safe_remove_job(tr["trigger_id"])
        # Re-register if plan is still enabled.
        plan = self._store.get_plan(plan_id)
        if plan is None or plan.get("enabled") != 1:
            return
        self._register_plan_triggers(plan)

    def _register_plan_triggers(self, plan: dict) -> int:
        plan_id = plan["plan_id"]
        plan_tz = plan.get("timezone") or "Asia/Shanghai"
        count = 0
        for tr in self._store.list_plan_triggers(plan_id):
            if tr.get("enabled") != 1:
                continue
            try:
                aps_trigger = self._build_aps_trigger(tr, plan_tz)
            except _PastOnceTrigger:
                # kind='once' whose datetime is already in the past — skip.
                logger.info(
                    "[PLAN] Skipping past 'once' trigger %s for plan %s",
                    tr["trigger_id"], plan_id,
                )
                continue
            except Exception as e:
                logger.error(
                    "[PLAN] Bad trigger expr (plan=%s, trigger=%s): %s",
                    plan_id, tr["trigger_id"], e,
                )
                continue

            self._scheduler.add_job(
                self._make_job_fn(plan_id, tr["trigger_id"], plan_tz, tr["kind"]),
                trigger=aps_trigger,
                id=tr["trigger_id"],
                replace_existing=True,
                max_instances=1,
                coalesce=True,
                misfire_grace_time=None,  # spec §10.3: missed ticks dropped silently
            )
            count += 1
        return count

    def _safe_remove_job(self, job_id: str) -> None:
        try:
            self._scheduler.remove_job(job_id)
        except Exception:
            pass  # Already gone — that's fine.

    def _build_aps_trigger(self, tr: dict, plan_tz: str):
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(plan_tz)
        kind = tr["kind"]
        expr = tr["expr"]
        if kind == "cron":
            return CronTrigger.from_crontab(expr, timezone=tz)
        if kind == "interval":
            spec = json.loads(expr) if isinstance(expr, str) else expr
            return IntervalTrigger(
                seconds=spec.get("seconds", 0),
                minutes=spec.get("minutes", 0),
                hours=spec.get("hours", 0),
                days=spec.get("days", 0),
                timezone=tz,
            )
        if kind == "once":
            run_date = datetime.fromisoformat(expr).replace(tzinfo=tz)
            if run_date < datetime.now(tz=tz):
                raise _PastOnceTrigger()
            return DateTrigger(run_date=run_date, timezone=tz)
        raise ValueError(f"Unknown trigger kind: {kind}")

    def _make_job_fn(self, plan_id: str, trigger_id: str, plan_tz: str, kind: str):
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(plan_tz)

        def _job():
            now = datetime.now(tz=tz)
            try:
                self.fire(plan_id, now, manual=False)
            except Exception as e:
                # fire() catches its own exceptions; this is a safety net for
                # anything outside the per-step try/except.
                logger.error("[PLAN] Unhandled fire() error plan=%s: %s", plan_id, e)
            # `kind='once'` triggers are self-disabling: APScheduler's
            # DateTrigger only fires once anyway, but we also flip the DB
            # flag so the UI shows it as expired.
            if kind == "once":
                try:
                    self._store.update_plan_trigger(trigger_id, {"enabled": 0})
                except Exception as e:
                    logger.warning(
                        "[PLAN] Failed to disable spent once-trigger %s: %s",
                        trigger_id, e,
                    )

        return _job

    # -- Fire path -----------------------------------------------------------

    def fire(self, plan_id: str, instance_dt: datetime, *, manual: bool) -> list[str]:
        """Run all steps of plan ``plan_id``, in order.

        Per spec §7.2:
          1. For each step in ``step_index`` order:
             a. resolve_step_refs against IDs of prior steps in this fire.
             b. render_obj using ``instance_dt``.
             c. extract_resolved_deps from the resolved input.
             d. submit_task / submit_batch with origin tagged.
          2. On any submit failure, abort remaining steps; emit
             ``on_plan_step_submit_failed``; record last_fired_at; return
             whatever was submitted so far.
        """
        from crawlhub.core.telemetry import emit_plan_fired

        _telemetry_status = 200
        _telemetry_plan_name = ""
        plan = self._store.get_plan(plan_id)
        if plan is None:
            logger.warning("[PLAN] fire(): plan %s not found", plan_id)
            emit_plan_fired(plan_id=plan_id, plan_name="", status_code=404)
            return []
        _telemetry_plan_name = plan.get("name", "") or ""
        steps = self._store.list_plan_steps(plan_id)
        if not steps:
            # Spec edge case: empty plan -> still record last_fired_at? We
            # choose YES, because "the trigger fired and we did our (empty)
            # job" is the most accurate UI signal. No tasks submitted.
            self._store.update_plan(plan_id, {"last_fired_at": time.time()})
            emit_plan_fired(plan_id=plan_id, plan_name=_telemetry_plan_name, status_code=200)
            return []

        from zoneinfo import ZoneInfo
        tz = ZoneInfo(plan.get("timezone") or "Asia/Shanghai")

        origin_type = "plan_manual" if manual else "plan"
        submitted_ids: list[str] = []
        try:
            logger.info("[FIRE] plan=%s manual=%s steps=%d", plan_id, manual, len(steps))
            for step in steps:
                logger.info("[FIRE] processing step_index=%s request_kind=%s",
                            step["step_index"], step["request_kind"])
                tid = self._fire_step(plan, step, instance_dt, submitted_ids,
                                       origin_type=origin_type, tz=tz)
                submitted_ids.append(tid)
                logger.info("[FIRE] step_index=%s submitted tid=%s", step["step_index"], tid)
        except _StepSubmitFailure as failure:
            logger.error("[FIRE] step_index=%s FAILED: %s", failure.step_index, failure.error)
            self._notify_step_failure(plan, failure.step_index, failure.error,
                                       instance_dt, manual=manual)
            _telemetry_status = 500
            # Stop firing — but DO record last_fired_at so the UI shows we tried.
        except Exception:
            _telemetry_status = 500
            raise
        finally:
            self._store.update_plan(plan_id, {"last_fired_at": time.time()})
            logger.info("[FIRE] plan=%s done, submitted=%d", plan_id, len(submitted_ids))
            emit_plan_fired(
                plan_id=plan_id,
                plan_name=_telemetry_plan_name,
                status_code=_telemetry_status,
            )
        return submitted_ids

    def _fire_step(self, plan: dict, step: dict, instance_dt: datetime,
                   prior_ids: list[str], *, origin_type: str, tz) -> str:
        step_index = step["step_index"]
        request_kind = step["request_kind"]
        # store.list_plan_steps already deserializes request_payload (TEXT column)
        # into a dict — see sqlite_store.list_plan_steps. Defensive copy so we
        # don't mutate the caller's view (we pop keys below for batch).
        raw_payload = step.get("request_payload") or {}
        if isinstance(raw_payload, str):
            try:
                raw_payload = json.loads(raw_payload or "{}")
            except json.JSONDecodeError as e:
                raise _StepSubmitFailure(step_index, f"Bad request_payload JSON: {e}")
        payload = dict(raw_payload)

        # Resolution pipeline (order matters — see plan_runtime docs).
        # 1. Substitute ${step[K].task_id} against prior step ids.
        # 2. Render time templates (${YYYYMMDD} etc.).
        try:
            payload_resolved = resolve_step_refs(payload, prior_ids)
            payload_rendered = render_obj(payload_resolved, instance_dt, tz)
        except Exception as e:
            raise _StepSubmitFailure(step_index, f"Template render failed: {e}")

        deps = extract_resolved_deps(payload_rendered)

        try:
            if request_kind == "task":
                # POST /api/task body shape — platform/task_type live on the
                # step row (URL-style), the rest is the input dict.
                task = self._daemon.submit_task(
                    step["platform"], step["task_type"], payload_rendered,
                    depends_on_task_ids=deps,
                    origin_type=origin_type,
                    origin_plan_id=plan["plan_id"],
                )
                return task.task_id
            elif request_kind == "batch":
                # POST /api/batch body shape — split off items_from (the
                # async source spec) and feed the remainder into BatchConfig.
                items_from = payload_rendered.pop("items_from", None)
                config = BatchConfig.from_dict(payload_rendered)
                # Default platform/action from the step row if not in payload
                # (UI may save them only at the step level).
                if not config.platform:
                    config.platform = step["platform"]
                if not config.action:
                    config.action = step["task_type"]
                parent, _ = self._daemon.submit_batch(
                    config,
                    items_from=items_from,
                    depends_on_task_ids=deps,
                    origin_type=origin_type,
                    origin_plan_id=plan["plan_id"],
                )
                return parent.task_id
            else:
                raise _StepSubmitFailure(
                    step_index, f"Unknown request_kind: {request_kind!r}",
                )
        except _StepSubmitFailure:
            raise
        except Exception as e:
            raise _StepSubmitFailure(step_index, str(e))

    def _notify_step_failure(self, plan: dict, step_index: int, error: str,
                              instance_dt: datetime, *, manual: bool) -> None:
        if plan.get("notify_on_fire_fail") != 1:
            return
        group = self._store.get_plan_group(plan["group_id"]) or {}
        payload = {
            "plan_id": plan["plan_id"],
            "plan_name": plan.get("name", ""),
            "group_name": group.get("name", ""),
            "step_index": step_index,
            "error": error,
            "instance_time": instance_dt.isoformat(),
            "manual": manual,
        }
        try:
            self._daemon._emit_event("on_plan_step_submit_failed", payload)
        except Exception as e:
            logger.error("[PLAN] Failed to emit step-failure event: %s", e)

    # -- Preview -------------------------------------------------------------

    def preview(self, plan_id: str, instance_dt: datetime) -> list[dict]:
        """Render every step's templates without submitting.

        ``${step[K].task_id}`` placeholders are replaced with the literal
        ``"<step[K].task_id>"`` so the user sees the shape unambiguously.
        """
        plan = self._store.get_plan(plan_id)
        if plan is None:
            return []
        steps = self._store.list_plan_steps(plan_id)
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(plan.get("timezone") or "Asia/Shanghai")
        out: list[dict] = []
        # Pre-build "fake IDs" of length N so any K < N is in range.
        fake_ids = [f"<step[{i}].task_id>" for i in range(len(steps))]
        for step in steps:
            try:
                raw_payload = step.get("request_payload") or {}
                if isinstance(raw_payload, str):
                    raw_payload = json.loads(raw_payload or "{}")
                payload = dict(raw_payload)
                payload_resolved = resolve_step_refs(payload, fake_ids)
                rendered_payload = render_obj(payload_resolved, instance_dt, tz)
                row = {
                    "step_index": step["step_index"],
                    "request_kind": step["request_kind"],
                    "platform": step["platform"],
                    "task_type": step["task_type"],
                    "rendered_payload": rendered_payload,
                }
                out.append(row)
            except Exception as e:
                out.append({
                    "step_index": step["step_index"],
                    "error": str(e),
                })
        return out


# -- Internal helpers --------------------------------------------------------


class _StepSubmitFailure(Exception):
    """Raised inside fire() when a step can't be submitted. Carries the
    step_index and error string for the notification payload.
    """

    def __init__(self, step_index: int, error: str):
        super().__init__(f"step[{step_index}]: {error}")
        self.step_index = step_index
        self.error = error


class _PastOnceTrigger(Exception):
    """Raised by _build_aps_trigger when a kind='once' trigger's datetime
    is already in the past. Caller silently skips the registration.
    """
