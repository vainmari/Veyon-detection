"""
app/db/analytics.py
───────────────────
Read-only analytics queries used by the /analytics and /history pages.
"""
from __future__ import annotations

from typing import Optional

from app.db._core import _conn


def _analytics_where(
    computer_id: Optional[int],
    user_id:     Optional[int],
    from_date:   str,
    to_date:     str,
) -> tuple[str, list]:
    clauses: list[str] = []
    params:  list      = []
    if computer_id:
        clauses.append("e.computer_id = ?"); params.append(computer_id)
    if user_id:
        clauses.append("e.user_id = ?");     params.append(user_id)
    if from_date:
        clauses.append("e.detected_at >= ?"); params.append(from_date + " 00:00:00")
    if to_date:
        clauses.append("e.detected_at <= ?"); params.append(to_date + " 23:59:59")
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


def get_summary_stats(
    computer_id: Optional[int] = None,
    user_id:     Optional[int] = None,
    from_date:   str           = "",
    to_date:     str           = "",
) -> dict:
    w, p = _analytics_where(computer_id, user_id, from_date, to_date)
    c = _conn()
    base   = f"FROM detection_event e LEFT JOIN user u ON u.id=e.user_id {w}"
    total  = c.execute(f"SELECT COUNT(*) {base}", p).fetchone()[0]
    and_or = "AND" if w else "WHERE"
    hits   = c.execute(
        f"SELECT COUNT(*) {base} {and_or} e.had_detection=1", p
    ).fetchone()[0]
    students = c.execute(
        f"SELECT COUNT(DISTINCT COALESCE(e.user_id, e.windows_username)) {base}", p
    ).fetchone()[0]
    row = c.execute(f"""
        SELECT dc.name, COUNT(*) AS cnt
        FROM detection d
        JOIN detection_event e  ON e.id  = d.event_id
        JOIN detection_class dc ON dc.id = d.class_id
        LEFT JOIN user u        ON u.id  = e.user_id
        {w}
        GROUP BY dc.name ORDER BY cnt DESC LIMIT 1
    """, p).fetchone()
    top_class = row[0] if row else "—"
    return {
        "total_events":     total,
        "detection_events": hits,
        "active_students":  students,
        "top_class":        top_class,
    }


def get_class_distribution(
    computer_id: Optional[int] = None,
    user_id:     Optional[int] = None,
    from_date:   str           = "",
    to_date:     str           = "",
) -> list[dict]:
    w, p = _analytics_where(computer_id, user_id, from_date, to_date)
    c = _conn()
    rows = c.execute(f"""
        SELECT dc.name, dc.color_hex, COUNT(*) AS cnt
        FROM detection d
        JOIN detection_event e  ON e.id  = d.event_id
        JOIN detection_class dc ON dc.id = d.class_id
        LEFT JOIN user u        ON u.id  = e.user_id
        {w}
        GROUP BY dc.name, dc.color_hex
        ORDER BY cnt DESC
    """, p).fetchall()
    return [{"name": r[0], "color": r[1], "count": r[2]} for r in rows]


def get_daily_detections(
    computer_id: Optional[int] = None,
    user_id:     Optional[int] = None,
    from_date:   str           = "",
    to_date:     str           = "",
) -> list[dict]:
    w, p = _analytics_where(computer_id, user_id, from_date, to_date)
    c = _conn()
    rows = c.execute(f"""
        SELECT
            SUBSTR(e.detected_at, 1, 10) AS day,
            COUNT(*)                     AS total,
            SUM(e.had_detection)         AS hits
        FROM detection_event e
        LEFT JOIN user u ON u.id = e.user_id
        {w}
        GROUP BY day
        ORDER BY day
    """, p).fetchall()
    return [{"day": r[0], "total": r[1], "hits": r[2]} for r in rows]


def get_student_activity(
    computer_id: Optional[int] = None,
    from_date:   str           = "",
    to_date:     str           = "",
) -> list[dict]:
    w, p = _analytics_where(computer_id, None, from_date, to_date)
    and_or = "AND" if w else "WHERE"
    c = _conn()
    rows = c.execute(f"""
        SELECT
            COALESCE(u.username, e.windows_username, '(unknown)') AS student,
            COUNT(*) AS hits
        FROM detection_event e
        LEFT JOIN user u ON u.id = e.user_id
        {w} {and_or} e.had_detection = 1
        GROUP BY student
        ORDER BY hits DESC
        LIMIT 20
    """, p).fetchall()
    return [{"student": r[0], "hits": r[1]} for r in rows]


def query_events(
    computer_id: Optional[int] = None,
    user_id:     Optional[int] = None,
    class_name:  str           = "",
    only_hits:   bool          = False,
    limit:       int           = 200,
) -> list[dict]:
    joins:   list[str] = ["detection_event e"]
    clauses: list[str] = []
    params:  list      = []

    joins.append("JOIN computer c ON c.id = e.computer_id")
    joins.append("LEFT JOIN user u ON u.id = e.user_id")
    joins.append("LEFT JOIN ml_model mm ON mm.id = e.model_id")

    if class_name:
        joins.append(
            "JOIN detection d ON d.event_id = e.id "
            "JOIN detection_class dc ON dc.id = d.class_id"
        )
        clauses.append("dc.name = ?"); params.append(class_name)
    else:
        joins.append("LEFT JOIN detection d ON d.event_id = e.id")
        joins.append("LEFT JOIN detection_class dc ON dc.id = d.class_id")

    if computer_id:
        clauses.append("e.computer_id = ?"); params.append(computer_id)
    if user_id:
        clauses.append("e.user_id = ?");     params.append(user_id)
    if only_hits:
        clauses.append("e.had_detection = 1")

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)

    sql = f"""
        SELECT
            e.id                AS event_id,
            e.detected_at,
            c.name              AS computer,
            COALESCE(u.username, e.windows_username, '—') AS student,
            e.had_detection,
            CASE WHEN e.frame_blob IS NOT NULL THEN 1 ELSE 0 END AS has_frame,
            GROUP_CONCAT(dc.name || ' (' || ROUND(d.confidence*100) || '%)', ', ')
                                AS detections,
            mm.name             AS model_name
        FROM {' '.join(joins)}
        {where}
        GROUP BY e.id
        ORDER BY e.detected_at DESC
        LIMIT ?
    """
    c = _conn()
    return [dict(r) for r in c.execute(sql, params).fetchall()]
