"""
app/db/groups.py
────────────────
Computer group CRUD — logical groupings of monitored machines
(e.g. "Lab 1", "Exam Room").

Computers can belong to multiple groups (many-to-many via computer_group_member).
"""
from __future__ import annotations

from typing import Optional

from app.db._core import _conn, _now


def list_groups() -> list[dict]:
    c = _conn()
    rows = c.execute("""
        SELECT g.*,
               COUNT(cgm.computer_id) AS computer_count
        FROM   computer_group g
        LEFT JOIN computer_group_member cgm ON cgm.group_id = g.id
        GROUP BY g.id
        ORDER BY g.name
    """).fetchall()
    return [dict(r) for r in rows]


def get_group(group_id: int) -> Optional[dict]:
    c = _conn()
    row = c.execute(
        "SELECT * FROM computer_group WHERE id = ?", (group_id,)
    ).fetchone()
    return dict(row) if row else None


def get_or_create_group(name: str, description: str = "") -> int:
    """Return the id of an existing group with this name, or create one."""
    c = _conn()
    row = c.execute("SELECT id FROM computer_group WHERE name = ?", (name,)).fetchone()
    if row:
        return row["id"]
    return create_group(name, description)


def create_group(name: str, description: str = "") -> int:
    c = _conn()
    cur = c.execute(
        "INSERT INTO computer_group (name, description, created_at) VALUES (?, ?, ?)",
        (name, description or None, _now()),
    )
    c.commit()
    return cur.lastrowid


def update_group(group_id: int, name: str, description: str = "") -> None:
    c = _conn()
    c.execute(
        "UPDATE computer_group SET name = ?, description = ? WHERE id = ?",
        (name, description or None, group_id),
    )
    c.commit()


def delete_group(group_id: int) -> None:
    """Delete a group; membership rows cascade-delete via computer_group_member."""
    c = _conn()
    c.execute("DELETE FROM computer_group WHERE id = ?", (group_id,))
    c.commit()


def add_computer_to_group(computer_id: int, group_id: int) -> None:
    """Add a computer to a group (idempotent — safe to call multiple times)."""
    c = _conn()
    c.execute(
        "INSERT OR IGNORE INTO computer_group_member (computer_id, group_id) VALUES (?, ?)",
        (computer_id, group_id),
    )
    c.commit()


def remove_computer_from_group(computer_id: int, group_id: int) -> None:
    """Remove a computer from a specific group (does not affect other groups)."""
    c = _conn()
    c.execute(
        "DELETE FROM computer_group_member WHERE computer_id = ? AND group_id = ?",
        (computer_id, group_id),
    )
    c.commit()


def assign_computer_to_group(computer_id: int, group_id: Optional[int]) -> None:
    """
    Backward-compatible shim.
    group_id=<int>  → add_computer_to_group
    group_id=None   → remove_computer_from_group for all groups (legacy behaviour)
    Prefer add_computer_to_group / remove_computer_from_group directly.
    """
    if group_id is None:
        c = _conn()
        c.execute(
            "DELETE FROM computer_group_member WHERE computer_id = ?", (computer_id,)
        )
        c.commit()
    else:
        add_computer_to_group(computer_id, group_id)


def list_computers_in_group(group_id: int) -> list[dict]:
    c = _conn()
    rows = c.execute("""
        SELECT comp.*
        FROM   computer comp
        JOIN   computer_group_member cgm ON cgm.computer_id = comp.id
        WHERE  cgm.group_id = ?
        ORDER  BY comp.name
    """, (group_id,)).fetchall()
    return [dict(r) for r in rows]
