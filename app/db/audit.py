"""
app/db/audit.py
───────────────
Audit log — append-only record of every significant action performed
by a user (user management, model activation, alert rule changes,
monitoring start/stop, etc.).

Columns
───────
  user_id   — who did it (NULL if system/startup)
  action    — short verb, e.g. "user.create", "model.activate"
  entity    — table / domain, e.g. "user", "ml_model", "schedule"
  entity_id — PK of the affected row (nullable)
  detail    — free-form JSON or text with extra context
"""
from __future__ import annotations

from typing import Optional

from app.db._core import _conn, _now


def _insert_audit(
    c,
    action:    str,
    user_id:   Optional[int] = None,
    entity:    Optional[str] = None,
    entity_id: Optional[int] = None,
    detail:    Optional[str] = None,
) -> None:
    """Insert an audit row into an already-open connection without committing."""
    c.execute(
        "INSERT INTO audit_log (user_id, action, entity, entity_id, detail, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, action, entity, entity_id, detail, _now()),
    )


def log_action(
    action:    str,
    user_id:   Optional[int] = None,
    entity:    Optional[str] = None,
    entity_id: Optional[int] = None,
    detail:    Optional[str] = None,
) -> int:
    c = _conn()
    cur = c.execute(
        "INSERT INTO audit_log (user_id, action, entity, entity_id, detail, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, action, entity, entity_id, detail, _now()),
    )
    c.commit()
    return cur.lastrowid


def list_audit_log(
    limit:   int           = 200,
    user_id: Optional[int] = None,
    action:  Optional[str] = None,
) -> list[dict]:
    clauses, params = [], []
    if user_id:
        clauses.append("al.user_id = ?"); params.append(user_id)
    if action:
        clauses.append("al.action LIKE ?"); params.append(f"%{action}%")
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)

    c = _conn()
    rows = c.execute(f"""
        SELECT al.*,
               u.username AS username
        FROM   audit_log al
        LEFT JOIN user u ON u.id = al.user_id
        {where}
        ORDER BY al.created_at DESC
        LIMIT ?
    """, params).fetchall()
    return [dict(r) for r in rows]
