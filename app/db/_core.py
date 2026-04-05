"""
app/db/_core.py
───────────────
Thread-local SQLite connection pool and low-level schema helpers.

Imported by every other db sub-module — nothing in this file imports
from any other app.db.* module.
"""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime
from pathlib import Path

DB_PATH = Path("data/monitor.db")
_tls    = threading.local()


def _conn() -> sqlite3.Connection:
    """
    Return a per-thread SQLite connection, reopening it when DB_PATH changes
    (important for test isolation where each fixture swaps the path).
    """
    conn    = getattr(_tls, "conn",    None)
    db_path = getattr(_tls, "db_path", None)
    if conn is None or db_path != str(DB_PATH):
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode  = WAL")
        conn.execute("PRAGMA synchronous   = NORMAL")
        conn.execute("PRAGMA cache_size    = -8192")  # 8 MB per-thread page cache
        conn.execute("PRAGMA temp_store    = MEMORY")
        _tls.conn    = conn
        _tls.db_path = str(DB_PATH)
    return conn


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _table_exists(c: sqlite3.Connection, name: str) -> bool:
    return c.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()[0] > 0


def _cols(c: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()}
