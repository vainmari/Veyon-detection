"""
app/db/schedules.py
───────────────────
Monitoring schedule CRUD.

A schedule ties a computer_group to a weekly time window:
  days_of_week              — comma-separated integers, 0 = Monday … 6 = Sunday
  start_time                — "HH:MM"
  end_time                  — "HH:MM"
  model_id                  — FK to ml_model; NULL = use the active model at runtime
  use_custom_notify_classes — 0 = global detection_class.notification_enabled;
                              1 = schedule_notification_class rows for this schedule

is_active_now() checks whether any schedule for a given group
should be running at the current local time.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from app.db._core import _conn, _now


def list_schedules() -> list[dict]:
    c = _conn()
    rows = c.execute("""
        SELECT s.*,
               g.name AS group_name,
               m.name AS model_name,
               (SELECT COUNT(*) FROM schedule_notification_class snc
                WHERE snc.schedule_id = s.id) AS notify_class_count,
               (SELECT GROUP_CONCAT(dc.name, '||')
                FROM   schedule_notification_class snc
                JOIN   detection_class dc ON dc.id = snc.class_id
                WHERE  snc.schedule_id = s.id) AS notify_class_names
        FROM   schedule s
        LEFT JOIN computer_group g ON g.id = s.group_id
        LEFT JOIN ml_model       m ON m.id = s.model_id
        ORDER BY g.name, s.start_time
    """).fetchall()
    return [dict(r) for r in rows]


def list_schedules_for_group(group_id: int) -> list[dict]:
    c = _conn()
    rows = c.execute(
        "SELECT * FROM schedule WHERE group_id = ? ORDER BY start_time",
        (group_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_schedule(schedule_id: int) -> Optional[dict]:
    c = _conn()
    row = c.execute("""
        SELECT s.*,
               g.name AS group_name,
               m.name AS model_name
        FROM   schedule s
        LEFT JOIN computer_group g ON g.id = s.group_id
        LEFT JOIN ml_model       m ON m.id = s.model_id
        WHERE  s.id = ?
    """, (schedule_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["notify_class_ids"] = get_notify_class_ids_for_schedule(schedule_id)
    return d


def create_schedule(
    group_id:                  int,
    name:                      str,
    days_of_week:              str,
    start_time:                str,
    end_time:                  str,
    created_by:                Optional[int]       = None,
    model_id:                  Optional[int]       = None,
    use_custom_notify_classes: bool                = False,
    notify_class_ids:          Optional[list[int]] = None,
) -> int:
    c = _conn()
    cur = c.execute(
        "INSERT INTO schedule "
        "(group_id, name, days_of_week, start_time, end_time, "
        " model_id, use_custom_notify_classes, created_by, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (group_id, name, days_of_week, start_time, end_time,
         model_id, 1 if use_custom_notify_classes else 0,
         created_by, _now()),
    )
    c.commit()
    sid = cur.lastrowid
    if use_custom_notify_classes and notify_class_ids:
        set_schedule_notify_classes(sid, notify_class_ids)
    return sid


def update_schedule(
    schedule_id:               int,
    name:                      str,
    days_of_week:              str,
    start_time:                str,
    end_time:                  str,
    is_active:                 bool,
    model_id:                  Optional[int]       = None,
    use_custom_notify_classes: bool                = False,
    notify_class_ids:          Optional[list[int]] = None,
) -> None:
    c = _conn()
    c.execute(
        "UPDATE schedule SET name=?, days_of_week=?, start_time=?, "
        "end_time=?, is_active=?, model_id=?, use_custom_notify_classes=? "
        "WHERE id=?",
        (name, days_of_week, start_time, end_time,
         1 if is_active else 0,
         model_id, 1 if use_custom_notify_classes else 0,
         schedule_id),
    )
    c.commit()
    set_schedule_notify_classes(
        schedule_id,
        notify_class_ids if (use_custom_notify_classes and notify_class_ids is not None) else [],
    )


def delete_schedule(schedule_id: int) -> None:
    c = _conn()
    c.execute("DELETE FROM schedule WHERE id = ?", (schedule_id,))
    c.commit()


# ── Notify-class helpers ──────────────────────────────────────────────────────

def get_notify_class_ids_for_schedule(schedule_id: int) -> list[int]:
    """Return the list of detection_class IDs configured for this schedule."""
    c = _conn()
    rows = c.execute(
        "SELECT class_id FROM schedule_notification_class WHERE schedule_id = ?",
        (schedule_id,),
    ).fetchall()
    return [r[0] for r in rows]


def set_schedule_notify_classes(schedule_id: int, class_ids: list[int]) -> None:
    """Replace the full set of per-schedule notification classes atomically."""
    c = _conn()
    c.execute(
        "DELETE FROM schedule_notification_class WHERE schedule_id = ?",
        (schedule_id,),
    )
    if class_ids:
        c.executemany(
            "INSERT INTO schedule_notification_class (schedule_id, class_id) VALUES (?, ?)",
            [(schedule_id, cid) for cid in class_ids],
        )
    c.commit()


def list_schedules_using_model(model_id: int) -> list[dict]:
    """Return all schedules whose model_id matches — used before model deletion."""
    c = _conn()
    rows = c.execute(
        "SELECT s.*, g.name AS group_name "
        "FROM schedule s "
        "LEFT JOIN computer_group g ON g.id = s.group_id "
        "WHERE s.model_id = ?",
        (model_id,),
    ).fetchall()
    return [dict(r) for r in rows]


# ── Overlap / active-now queries ──────────────────────────────────────────────

def find_overlapping_schedules(
    days:       str,
    start_time: str,
    end_time:   str,
    exclude_id: Optional[int] = None,
) -> list[dict]:
    """
    Return ALL schedules (any group) that overlap with the given window.
    Two windows overlap when they share ≥1 day AND new_start < existing_end AND
    existing_start < new_end  (standard interval-overlap test).
    The check is global because the monitor is a single shared process — overlapping
    schedules across different groups are just as ambiguous as within one group.
    Pass exclude_id=<id> when editing to skip the schedule being modified.
    """
    existing = list_schedules()
    new_days = {d for d in days.split(",") if d}
    result = []
    for s in existing:
        if exclude_id is not None and s["id"] == exclude_id:
            continue
        existing_days = {d for d in s["days_of_week"].split(",") if d}
        if not new_days & existing_days:
            continue
        if start_time < s["end_time"] and s["start_time"] < end_time:
            result.append(s)
    return result


def get_active_schedules_now() -> list[dict]:
    """
    Return all is_active schedules whose day-of-week and time window
    match the current local time.  Used by a background ticker to decide
    whether to auto-start monitoring.
    """
    now   = datetime.now()
    today = str(now.weekday())          # 0=Mon … 6=Sun
    hhmm  = now.strftime("%H:%M")

    c = _conn()
    rows = c.execute("""
        SELECT s.*, g.name AS group_name
        FROM   schedule s
        LEFT JOIN computer_group g ON g.id = s.group_id
        WHERE  s.is_active = 1
          AND  s.start_time <= ? AND ? < s.end_time
    """, (hhmm, hhmm)).fetchall()

    return [
        dict(r) for r in rows
        if today in r["days_of_week"].split(",")
    ]
