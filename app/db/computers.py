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

    Uses INSERT … ON CONFLICT … DO UPDATE so two concurrent threads calling
    this with the same name cannot race past a SELECT-then-INSERT window and
    hit the UNIQUE constraint. RETURNING gives us the id in either branch
    (new row or already-existing row) in a single statement.
    """
    c = _conn()
    row = c.execute(
        """
        INSERT INTO computer (name, host_address, created_at)
        VALUES (?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET host_address = excluded.host_address
        RETURNING id
        """,
        (name, host_address, _now()),
    ).fetchone()
    c.commit()
    return int(row["id"])


def list_computers() -> list[dict]:
    c = _conn()
    return [dict(r) for r in c.execute(
        "SELECT * FROM computer ORDER BY name"
    ).fetchall()]
