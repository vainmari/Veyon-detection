"""
app/db/database.py
──────────────────
Thin SQLite wrapper — synchronous, no ORM overhead.
Swap to SQLAlchemy async + Alembic migrations when scaling up.
"""
from __future__ import annotations
import sqlite3
from datetime import datetime
from pathlib import Path

DB_PATH = Path("data/monitor.db")


def init_db() -> None:
    """Create tables if they don't exist. Safe to call on every startup."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS detections (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                computer    TEXT    NOT NULL,
                class_id    INTEGER NOT NULL,
                class_name  TEXT    NOT NULL,
                confidence  REAL    NOT NULL,
                box_x1      INTEGER,
                box_y1      INTEGER,
                box_x2      INTEGER,
                box_y2      INTEGER,
                detected_at TEXT    NOT NULL
            )
        """)
        conn.commit()


def insert_detection(computer: str, det: dict) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO detections "
            "(computer, class_id, class_name, confidence, "
            " box_x1, box_y1, box_x2, box_y2, detected_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                computer,
                det["class_id"], det["class_name"], det["conf"],
                *det["box"],
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
        conn.commit()


def query_detections(
    computer: str = "",
    class_name: str = "",
    limit: int = 100,
) -> list[dict]:
    clauses, params = [], []
    if computer:
        clauses.append("computer = ?");   params.append(computer)
    if class_name:
        clauses.append("class_name = ?"); params.append(class_name)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"SELECT * FROM detections {where} "
            f"ORDER BY detected_at DESC LIMIT ?",
            params,
        ).fetchall()
    return [dict(r) for r in rows]