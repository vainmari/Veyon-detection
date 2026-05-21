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

Two write entry points (otherwise identical):
  _insert_audit  → caller owns the transaction; the audit row joins it.
  log_action     → standalone write that commits immediately.
"""
from __future__ import annotations

import sqlite3
from typing import Optional

from app.db._core import _conn, _now

_INSERT_SQL = (
    "INSERT INTO audit_log (user_id, action, entity, entity_id, detail, created_at) "
    "VALUES (?, ?, ?, ?, ?, ?)"
)


def _insert_audit(
    c:         sqlite3.Connection,
    action:    str,
    user_id:   Optional[int] = None,
    entity:    Optional[str] = None,
    entity_id: Optional[int] = None,
    detail:    Optional[str] = None,
) -> int:
    """Insert an audit row into an already-open connection without committing.

    Returns the new audit row id so callers that want to log a side-effect
    (e.g. "audit_id={n}") can do so without an extra round-trip.
    """
    cur = c.execute(
        _INSERT_SQL,
        (user_id, action, entity, entity_id, detail, _now()),
    )
    return cur.lastrowid


def log_action(
    action:    str,
    user_id:   Optional[int] = None,
    entity:    Optional[str] = None,
    entity_id: Optional[int] = None,
    detail:    Optional[str] = None,
) -> int:
    """Standalone audit write: insert + commit in one call."""
    c = _conn()
    audit_id = _insert_audit(c, action, user_id, entity, entity_id, detail)
    c.commit()
    return audit_id


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
