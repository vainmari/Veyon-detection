"""
app/db/computers.py
───────────────────
Computer registry — upsert and list monitored machines.
"""
from __future__ import annotations

from app.db._core import _conn, _now


def upsert_computer(name: str, host_address: str) -> int:
    """
    Insert a new computer or update its host_address if it already exists.
    Always returns the computer's id.
    """
    c = _conn()
    row = c.execute("SELECT id FROM computer WHERE name = ?", (name,)).fetchone()
    if row:
        c.execute(
            "UPDATE computer SET host_address = ? WHERE id = ?",
            (host_address, row["id"]),
        )
        c.commit()
        return row["id"]
    cur = c.execute(
        "INSERT INTO computer (name, host_address, created_at) VALUES (?, ?, ?)",
        (name, host_address, _now()),
    )
    c.commit()
    return cur.lastrowid


def list_computers() -> list[dict]:
    c = _conn()
    return [dict(r) for r in c.execute(
        "SELECT * FROM computer ORDER BY name"
    ).fetchall()]
