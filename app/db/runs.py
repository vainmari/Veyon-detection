"""
app/db/runs.py
──────────────
Monitoring-run lifecycle and per-run report queries.

A monitoring_run row is created by MonitorController when a session actually
starts (manual Start button or scheduler), and finished when it stops. Every
detection_event written during the session carries the run's id, so a report
is just a set of aggregate queries filtered by run_id.
"""
from __future__ import annotations

from typing import Optional

from app.db._core import _conn, _now


# ── Lifecycle ─────────────────────────────────────────────────────────────────

def create_run(
    trigger_type: str            = "manual",
    schedule_id:  Optional[int]  = None,
    group_name:   Optional[str]  = None,
    model_id:     Optional[int]  = None,
    started_by:   Optional[int]  = None,
) -> int:
    c = _conn()
    cur = c.execute(
        "INSERT INTO monitoring_run "
        "(trigger_type, schedule_id, group_name, model_id, started_by, "
        " status, started_at) "
        "VALUES (?, ?, ?, ?, ?, 'running', ?)",
        (trigger_type, schedule_id, group_name, model_id, started_by, _now()),
    )
    c.commit()
    return cur.lastrowid


def set_run_model(run_id: int, model_id: Optional[int]) -> None:
    """
    Record which model the session actually runs with. Called from the detect
    worker after it resolves schedule-pinned vs. currently-active model —
    the run row is created before that resolution happens.
    """
    c = _conn()
    c.execute(
        "UPDATE monitoring_run SET model_id = ? WHERE id = ?",
        (model_id, run_id),
    )
    c.commit()


def finish_run(run_id: int, status: str = "finished") -> None:
    """Mark the run ended now. No-op if the run is already finished."""
    c = _conn()
    c.execute(
        "UPDATE monitoring_run SET status = ?, ended_at = ? "
        "WHERE id = ? AND status = 'running'",
        (status, _now(), run_id),
    )
    c.commit()


def finish_stale_runs() -> int:
    """
    Repair runs left in 'running' state by a process crash or hard kill.
    Called once at startup, before the scheduler can start a new session.
    ended_at falls back to the run's last event time (or started_at when the
    run never produced events) — closest available approximation of when
    monitoring actually died.
    """
    c = _conn()
    cur = c.execute("""
        UPDATE monitoring_run
        SET status   = 'interrupted',
            ended_at = COALESCE(
                (SELECT MAX(e.detected_at) FROM detection_event e
                 WHERE e.run_id = monitoring_run.id),
                started_at
            )
        WHERE status = 'running'
    """)
    c.commit()
    return cur.rowcount


# ── Run list / lookup ─────────────────────────────────────────────────────────

_RUN_SELECT = """
    SELECT r.id, r.trigger_type, r.schedule_id, r.group_name, r.model_id,
           r.started_by, r.status, r.started_at, r.ended_at,
           s.name  AS schedule_name,
           m.name  AS model_name,
           u.username AS started_by_name,
           (SELECT COUNT(*) FROM detection_event e
            WHERE e.run_id = r.id)                          AS total_events,
           (SELECT COUNT(*) FROM detection_event e
            WHERE e.run_id = r.id AND e.had_detection = 1)  AS detection_events,
           (SELECT COUNT(*) FROM notification n
            JOIN detection_event e2 ON e2.id = n.event_id
            WHERE e2.run_id = r.id)                         AS alert_count,
           (SELECT COUNT(DISTINCT e.computer_id) FROM detection_event e
            WHERE e.run_id = r.id)                          AS computer_count,
           (SELECT COUNT(DISTINCT COALESCE(e.user_id, e.os_username))
            FROM detection_event e
            WHERE e.run_id = r.id
              AND (e.user_id IS NOT NULL OR e.os_username IS NOT NULL))
                                                            AS student_count
    FROM monitoring_run r
    LEFT JOIN schedule s ON s.id = r.schedule_id
    LEFT JOIN ml_model m ON m.id = r.model_id
    LEFT JOIN user     u ON u.id = r.started_by
"""


def list_runs(limit: int = 100) -> list[dict]:
    c = _conn()
    rows = c.execute(
        _RUN_SELECT + "ORDER BY r.id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_run(run_id: int) -> Optional[dict]:
    c = _conn()
    row = c.execute(_RUN_SELECT + "WHERE r.id = ?", (run_id,)).fetchone()
    return dict(row) if row else None


def run_short_label(run: dict, manual_label: str) -> str:
    """
    Short identifier for a run, used in report titles. Combines a name with the
    run's date and time window (more meaningful than a bare sequence number):
      • scheduler-triggered runs → "{schedule name} {date} {start}–{end}"
        (falls back to the manual name if the schedule was deleted),
      • manual runs              → "{manual_label} {date} {start}–{end}".

    A still-running session shows "…" as the end time. `manual_label` is passed
    in (already translated) so this stays UI-agnostic. Falls back to the bare
    name (no window) if the timestamps are missing/malformed.
    """
    if run.get("trigger_type") == "schedule" and run.get("schedule_name"):
        name = run["schedule_name"]
    else:
        name = manual_label

    started = run.get("started_at") or ""
    ended   = run.get("ended_at")
    # Stored format is "YYYY-MM-DD HH:MM:SS"; slice defensively.
    date     = started[:10]
    start_hm = started[11:16]
    end_hm   = ended[11:16] if ended else "…"
    if not date or not start_hm:
        return name
    return f"{name} | {date} {start_hm}–{end_hm}"


# ── Per-run report aggregates ─────────────────────────────────────────────────

def get_run_class_summary(run_id: int) -> list[dict]:
    """
    Per detection class: bounding-box count, that class's share of all
    detections in the run (`pct`, 0–100), and the mean confidence.
    """
    c = _conn()
    rows = c.execute("""
        SELECT dc.name, dc.color_hex,
               COUNT(*)           AS cnt,
               AVG(d.confidence)  AS avg_conf
        FROM detection d
        JOIN detection_event e  ON e.id  = d.event_id
        JOIN detection_class dc ON dc.id = d.class_id
        WHERE e.run_id = ?
        GROUP BY dc.id
        ORDER BY cnt DESC
    """, (run_id,)).fetchall()
    result = [dict(r) for r in rows]
    total = sum(r["cnt"] for r in result)
    for r in result:
        r["pct"] = (100.0 * r["cnt"] / total) if total else 0.0
    return result


def get_run_student_summary(run_id: int) -> list[dict]:
    """Per student: frames captured, frames with detections, classes seen."""
    c = _conn()
    rows = c.execute("""
        SELECT COALESCE(u.username, e.os_username, '(unknown)') AS student,
               COUNT(*)             AS frames,
               SUM(e.had_detection) AS hits
        FROM detection_event e
        LEFT JOIN user u ON u.id = e.user_id
        WHERE e.run_id = ?
        GROUP BY student
        ORDER BY hits DESC, frames DESC
    """, (run_id,)).fetchall()
    result = [dict(r) for r in rows]

    # Distinct detected classes per student, merged in Python — simpler and
    # cheaper than a correlated GROUP_CONCAT subquery per output row.
    cls_rows = c.execute("""
        SELECT COALESCE(u.username, e.os_username, '(unknown)') AS student,
               dc.name AS class_name
        FROM detection d
        JOIN detection_event e  ON e.id  = d.event_id
        JOIN detection_class dc ON dc.id = d.class_id
        LEFT JOIN user u        ON u.id  = e.user_id
        WHERE e.run_id = ?
        GROUP BY student, dc.name
        ORDER BY student, dc.name
    """, (run_id,)).fetchall()
    classes_by_student: dict[str, list[str]] = {}
    for r in cls_rows:
        classes_by_student.setdefault(r["student"], []).append(r["class_name"])
    for entry in result:
        entry["classes"] = ", ".join(classes_by_student.get(entry["student"], []))
    return result


def get_run_computer_summary(run_id: int) -> list[dict]:
    """Per computer: frames captured and frames with detections."""
    c = _conn()
    rows = c.execute("""
        SELECT comp.name           AS computer,
               COUNT(*)            AS frames,
               SUM(e.had_detection) AS hits
        FROM detection_event e
        JOIN computer comp ON comp.id = e.computer_id
        WHERE e.run_id = ?
        GROUP BY comp.id
        ORDER BY hits DESC, frames DESC
    """, (run_id,)).fetchall()
    return [dict(r) for r in rows]


def get_run_alerts(run_id: int) -> list[dict]:
    """
    Every notification fired during the run (prohibited-class detections),
    newest first, with class / computer / student resolved and a has_frame
    flag so the UI can offer a screenshot button per alert.
    """
    c = _conn()
    rows = c.execute("""
        SELECT n.id, n.event_id, n.created_at,
               COALESCE(dc.name, '?')                    AS class_name,
               COALESCE(dc.color_hex, '#888888')         AS class_color,
               comp.name                                 AS computer,
               COALESCE(u.username, e.os_username, '(unknown)') AS student,
               (e.frame_blob IS NOT NULL)                AS has_frame
        FROM notification n
        JOIN detection_event e       ON e.id  = n.event_id
        JOIN computer comp           ON comp.id = e.computer_id
        LEFT JOIN detection_class dc ON dc.id = n.class_id
        LEFT JOIN user u             ON u.id  = e.user_id
        WHERE e.run_id = ?
        ORDER BY n.created_at DESC, n.id DESC
    """, (run_id,)).fetchall()
    return [dict(r) for r in rows]


def get_run_detections(run_id: int) -> list[dict]:
    """
    Every individual detection of the run, newest first — the row set used
    for the CSV export.
    """
    c = _conn()
    rows = c.execute("""
        SELECT e.detected_at,
               comp.name                                         AS computer,
               COALESCE(u.username, e.os_username, '')           AS student,
               dc.name                                           AS class_name,
               d.confidence,
               d.box_x1, d.box_y1, d.box_x2, d.box_y2
        FROM detection d
        JOIN detection_event e  ON e.id  = d.event_id
        JOIN computer comp      ON comp.id = e.computer_id
        JOIN detection_class dc ON dc.id = d.class_id
        LEFT JOIN user u        ON u.id  = e.user_id
        WHERE e.run_id = ?
        ORDER BY e.detected_at DESC, d.id DESC
    """, (run_id,)).fetchall()
    return [dict(r) for r in rows]
