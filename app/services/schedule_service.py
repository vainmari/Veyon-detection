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

state.schedule_triggered tracks ownership so manual sessions are safe.
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
    from app.config import get_settings, collect_cfg
    from app.core.yolo import reset_model
    from app.services.monitor_service import MonitorController

    try:
        active = get_active_schedules_now()
    except Exception as exc:
        log.warning("schedule_service: DB error checking schedules: %s", exc)
        return

    should_run = bool(active)

    # ── Auto-START ────────────────────────────────────────────────────────────
    if should_run and state.monitor is None:
        try:
            cfg = collect_cfg()
        except Exception as exc:
            log.warning("schedule_service: collect_cfg failed: %s", exc)
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

            computers       = None if monitor_all else list(computers_map.values())
            group_label     = ", ".join(group_names) if not monitor_all else ""

            # Pick the lowest-id schedule as the authoritative source for
            # model and notification-class settings when multiple schedules
            # are active simultaneously (different non-overlapping groups).
            primary = min(active, key=lambda s: s["id"])
            sched_model_id = primary.get("model_id")
            sched_id       = primary["id"]

            reset_model()
            state.monitor = MonitorController(
                cfg,
                computers=computers,
                model_id=sched_model_id,
                schedule_id=sched_id,
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

    # ── Auto-STOP ─────────────────────────────────────────────────────────────
    elif not should_run and state.monitor is not None and state.schedule_triggered:
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
                log_action("schedule.auto_stop", entity="schedule",
                           detail="no active schedules")
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
