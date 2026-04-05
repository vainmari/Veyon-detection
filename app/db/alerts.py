"""
app/db/alerts.py
────────────────
Alert rules and notifications.

The notification table stores a class_id FK into detection_class instead of
duplicating the class name and colour as plain text.  list_notifications() joins
detection_class so callers still receive class_name and class_color fields.
"""
from __future__ import annotations

from typing import Optional

from app.db._core import _conn, _now


# ── Alert rules ───────────────────────────────────────────────────────────────

def list_alert_rules() -> list[dict]:
    """All detection classes joined with their alert rule (enabled flag)."""
    c = _conn()
    rows = c.execute("""
        SELECT
            dc.id          AS class_id,
            dc.class_index,
            dc.name,
            dc.color_hex,
            COALESCE(ar.enabled, 0) AS enabled
        FROM detection_class dc
        LEFT JOIN alert_rule ar ON ar.class_id = dc.id
        ORDER BY dc.class_index
    """).fetchall()
    return [dict(r) for r in rows]


def set_alert_rule(class_id: int, enabled: bool) -> None:
    c = _conn()
    c.execute("""
        INSERT INTO alert_rule (class_id, enabled, created_at)
        VALUES (?, ?, ?)
        ON CONFLICT(class_id) DO UPDATE SET enabled = excluded.enabled
    """, (class_id, 1 if enabled else 0, _now()))
    c.commit()


def get_prohibited_class_ids() -> dict[int, dict]:
    """
    Return {class_index: {"id": db_id, "color_hex": str}} for all enabled rules.
    Used by alert_service to resolve detections to notification rows.
    """
    c = _conn()
    rows = c.execute("""
        SELECT dc.class_index, dc.id, dc.color_hex
        FROM alert_rule ar
        JOIN detection_class dc ON dc.id = ar.class_id
        WHERE ar.enabled = 1
    """).fetchall()
    return {r[0]: {"id": r[1], "color_hex": r[2]} for r in rows}


# ── Notifications ─────────────────────────────────────────────────────────────

def insert_notification(
    event_id: int,
    class_id: int,
    computer: str,
    student:  str,
) -> int:
    c = _conn()
    cur = c.execute(
        "INSERT INTO notification "
        "(event_id, class_id, computer, student, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (event_id, class_id, computer, student, _now()),
    )
    c.commit()
    return cur.lastrowid


def list_notifications(limit: int = 60) -> list[dict]:
    """
    Returns rows with class_name and class_color resolved via JOIN so callers
    don't need to know about the FK — the interface is identical to the old schema.
    """
    c = _conn()
    rows = c.execute("""
        SELECT
            n.id, n.event_id, n.class_id,
            COALESCE(dc.name,      '(unknown)') AS class_name,
            COALESCE(dc.color_hex, '#888888')   AS class_color,
            n.computer, n.student, n.is_read, n.created_at,
            CASE WHEN e.frame_blob IS NOT NULL THEN 1 ELSE 0 END AS has_frame
        FROM notification n
        LEFT JOIN detection_class dc ON dc.id = n.class_id
        LEFT JOIN detection_event  e  ON e.id  = n.event_id
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
