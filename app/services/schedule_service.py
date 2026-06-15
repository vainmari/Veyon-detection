"""
app/services/schedule_service.py
─────────────────────────────────
Background scheduler that automatically starts and stops monitoring according
to the active Schedule rows in the database.

How it works
────────────
A single daemon thread wakes up every TICK_SECONDS (30 s) and evaluates:

  • If any schedule is currently active (right day + inside time window)
    AND the monitor is NOT running → start monitoring automatically.

  • If NO schedule is currently active AND the monitor IS running AND it was
    started by this scheduler (state.schedule_triggered == True) → stop it.
    A session the teacher started manually is never touched.

Concurrency
───────────
Every read/write of `state.monitor` is wrapped in `state.monitor_lock` so the
UI thread (dashboard.do_start / do_stop) and this daemon never race on the
check-then-assign pattern. Without the lock a teacher clicking Start at the
exact moment _tick() fires can create two MonitorController instances and
leak one of them forever.

Primary-schedule selection
──────────────────────────
When several schedules are active at the same instant (typically because they
cover non-overlapping groups), the one with the lowest `id` is treated as the
"primary" for picking the model and the per-schedule notification class set.
This is a deliberate, deterministic choice — it means the oldest schedule
wins, which is predictable and easy to reason about, even if a newer schedule
would technically be more specific. Operators who want a different schedule
to govern those settings should disable the older one for that time window.
"""
from __future__ import annotations

import logging
import threading
import time

import app.state as state

log = logging.getLogger(__name__)

TICK_SECONDS = 30   # check interval — max reaction time is 30 s


def _tick() -> None:
    """One evaluation cycle — safe to call from any thread."""
    from app.db.database import get_active_schedules_now, log_action
    from app.config import collect_cfg
    from app.core.yolo import reset_model
    from app.services.monitor_service import MonitorController

    try:
        active = get_active_schedules_now()
    except Exception as exc:
        log.warning("schedule_service: DB error checking schedules: %s", exc)
        return

    should_run = bool(active)

    # ── Auto-START ────────────────────────────────────────────────────────────
    if should_run:
        # Pre-check outside the lock to avoid the heavy collect_cfg work on the
        # common case where the monitor is already running.
        if state.monitor is not None:
            pass  # fall through to STOP branch evaluation below
        else:
            try:
                cfg = collect_cfg()
            except Exception as exc:
                log.warning("schedule_service: collect_cfg failed: %s", exc)
                return

            # Refuse to start with an incomplete key-auth config — silently
            # constructing the controller leads to endless auth errors with
            # no clear cause in the UI logs.
            if cfg.get("auth_method", "key") == "key" and not cfg.get("key_data"):
                log.error(
                    "schedule_service: auth_method=key but key file at %r is "
                    "missing or unreadable — skipping auto-start. "
                    "Fix the key path in Settings.",
                    cfg.get("key_path"),
                )
                try:
                    log_action(
                        "schedule.auto_start_failed",
                        entity="schedule",
                        detail="key file missing/unreadable",
                    )
                except Exception:
                    pass
                return

            with state.monitor_lock:
                # Re-check now that we hold the lock — the UI could have
                # claimed `state.monitor` while we were validating cfg.
                if state.monitor is not None:
                    return
                try:
                    from app.db.database import list_computers_in_group

                    # Collect computers from all active schedules' groups.
                    # If a schedule has no group_id, monitor all (computers=None).
                    computers_map: dict[str, dict] = {}   # keyed by host to deduplicate
                    monitor_all   = False
                    group_names:  list[str] = []

                    for s in active:
                        gid   = s.get("group_id")
                        gname = s.get("group_name") or "?"
                        if gid is None:
                            monitor_all = True
                            break
                        for row in list_computers_in_group(gid):
                            computers_map[row["host_address"]] = {
                                "name": row["name"],
                                "host": row["host_address"],
                            }
                        group_names.append(gname)

                    computers   = None if monitor_all else list(computers_map.values())
                    group_label = ", ".join(group_names) if not monitor_all else ""

                    # See module docstring on primary-schedule selection.
                    primary = min(active, key=lambda s: s["id"])
                    sched_model_id = primary.get("model_id")
                    sched_id       = primary["id"]

                    reset_model()
                    state.monitor = MonitorController(
                        cfg,
                        computers=computers,
                        model_id=sched_model_id,
                        schedule_id=sched_id,
                        group_name=group_label,
                    )
                    state.monitor.start()
                    state.schedule_triggered   = True
                    state.monitored_group_name = group_label

                    names = ", ".join(s.get("name", "?") for s in active)
                    log.info("schedule_service: monitoring started (schedules: %s)", names)
                    log_action(
                        "schedule.auto_start",
                        entity="schedule",
                        detail=f"triggered by: {names}",
                    )
                except Exception as exc:
                    log.error("schedule_service: failed to start monitor: %s", exc)
                    state.monitor = None
                    state.schedule_triggered   = False
                    state.monitored_group_name = None
            return  # done — STOP branch can't apply when we just started

    # ── Auto-STOP ─────────────────────────────────────────────────────────────
    if not should_run:
        with state.monitor_lock:
            if state.monitor is None or not state.schedule_triggered:
                return
            try:
                state.monitor.stop()
            except Exception as exc:
                log.warning("schedule_service: error stopping monitor: %s", exc)
            finally:
                state.monitor = None
                state.schedule_triggered   = False
                state.monitored_group_name = None
                log.info("schedule_service: monitoring stopped (no active schedules)")
                try:
                    log_action(
                        "schedule.auto_stop", entity="schedule",
                        detail="no active schedules",
                    )
                except Exception:
                    pass


def _loop() -> None:
    """Daemon thread main loop."""
    log.info("schedule_service: started (tick every %d s)", TICK_SECONDS)
    while True:
        try:
            _tick()
        except Exception as exc:
            log.error("schedule_service: unhandled error in tick: %s", exc)
        time.sleep(TICK_SECONDS)


def start_scheduler() -> threading.Thread:
    """Start the background scheduler thread. Call once from app startup."""
    t = threading.Thread(target=_loop, daemon=True, name="scheduler")
    t.start()
    return t
