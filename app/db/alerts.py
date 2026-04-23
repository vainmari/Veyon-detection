"""
app/db/alerts.py
────────────────
Alert rules (via detection_class.notification_enabled) and notifications.

Alert rules are stored directly on the detection_class table as a boolean
column (notification_enabled) — no separate alert_rule table needed.

The notification table stores only event_id + class_id; computer name and
student name are derived via JOINs at query time.
"""
from __future__ import annotations

from typing import Optional

from app.db._core import _conn, _now


# ── Alert rules (on detection_class) ─────────────────────────────────────────

def list_alert_rules() -> list[dict]:
    """All detection classes with their notification_enabled flag."""
    c = _conn()
    rows = c.execute("""
        SELECT
            dc.id  AS class_id,
            dc.name,
            dc.color_hex,
            dc.notification_enabled AS enabled
        FROM detection_class dc
        ORDER BY dc.name
    """).fetchall()
    return [dict(r) for r in rows]


def set_alert_rule(class_id: int, enabled: bool) -> None:
    from app.db.audit import _insert_audit
    c = _conn()
    c.execute(
        "UPDATE detection_class SET notification_enabled = ? WHERE id = ?",
        (1 if enabled else 0, class_id),
    )
    _insert_audit(c, "alert.rule", entity="detection_class", entity_id=class_id,
                  detail="enabled" if enabled else "disabled")
    c.commit()
    # Drop the in-process alert-rule cache so detect worker picks up the change
    # on the very next frame instead of waiting for the TTL to expire.
    try:
        from app.services.alert_service import invalidate_prohibited_cache
        invalidate_prohibited_cache()
    except Exception:
        pass


def get_prohibited_class_ids(
    model_id:    Optional[int],
    schedule_id: Optional[int] = None,
) -> dict[int, dict]:
    """
    Return {class_index: {"id": db_id, "color_hex": str}} for all alert-enabled
    classes scoped to the given model.

    When schedule_id is provided and that schedule has use_custom_notify_classes=1,
    the class set comes from schedule_notification_class instead of
    detection_class.notification_enabled (global setting).

    Returns an empty dict when model_id is None (no active model → no alerts).
    """
    if model_id is None:
        return {}
    c = _conn()

    # Determine whether to use per-schedule class set
    use_custom = False
    if schedule_id is not None:
        row = c.execute(
            "SELECT use_custom_notify_classes FROM schedule WHERE id = ?",
            (schedule_id,),
        ).fetchone()
        use_custom = bool(row and row[0])

    if use_custom:
        rows = c.execute("""
            SELECT mc.class_index, dc.id, dc.color_hex
            FROM   schedule_notification_class snc
            JOIN   detection_class dc ON dc.id = snc.class_id
            JOIN   model_class mc     ON mc.class_id = dc.id
            WHERE  snc.schedule_id = ?
              AND  mc.model_id = ?
        """, (schedule_id, model_id)).fetchall()
    else:
        rows = c.execute("""
            SELECT mc.class_index, dc.id, dc.color_hex
            FROM   detection_class dc
            JOIN   model_class mc ON mc.class_id = dc.id
            WHERE  dc.notification_enabled = 1
              AND  mc.model_id = ?
        """, (model_id,)).fetchall()

    return {r[0]: {"id": r[1], "color_hex": r[2]} for r in rows}


# ── Notifications ─────────────────────────────────────────────────────────────

def insert_notification(event_id: int, class_id: int, _commit: bool = True) -> int:
    c = _conn()
    cur = c.execute(
        "INSERT INTO notification (event_id, class_id, created_at) "
        "VALUES (?, ?, ?)",
        (event_id, class_id, _now()),
    )
    if _commit:
        c.commit()
    return cur.lastrowid


def list_notifications(limit: int = 60) -> list[dict]:
    """
    Returns rows with class_name, class_color, computer, and student resolved
    via JOINs so callers get a complete picture without stored duplicates.
    """
    c = _conn()
    rows = c.execute("""
        SELECT
            n.id, n.event_id, n.class_id,
            COALESCE(dc.name,      '(unknown)') AS class_name,
            COALESCE(dc.color_hex, '#888888')   AS class_color,
            COALESCE(comp.name,    '(unknown)')  AS computer,
            COALESCE(u.username, e.os_username, '(unknown)') AS student,
            n.is_read, n.created_at,
            CASE WHEN e.frame_blob IS NOT NULL THEN 1 ELSE 0 END AS has_frame
        FROM notification n
        LEFT JOIN detection_class dc ON dc.id = n.class_id
        LEFT JOIN detection_event e  ON e.id  = n.event_id
        LEFT JOIN computer comp      ON comp.id = e.computer_id
        LEFT JOIN user u             ON u.id    = e.user_id
        ORDER BY n.created_at DESC
        LIMIT ?
    """, (limit,)).fetchall()
    return [dict(r) for r in rows]


def count_unread_notifications() -> int:
    c = _conn()
    return c.execute(
        "SELECT COUNT(*) FROM notification WHERE is_read = 0"
    ).fetchone()[0]


def mark_read(notification_id: int) -> None:
    c = _conn()
    c.execute("UPDATE notification SET is_read = 1 WHERE id = ?", (notification_id,))
    c.commit()


def mark_all_read() -> None:
    c = _conn()
    c.execute("UPDATE notification SET is_read = 1")
    c.commit()
