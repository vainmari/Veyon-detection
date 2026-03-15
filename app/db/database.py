"""
app/db/database.py
──────────────────
Schema
──────
  computer          — monitored machines
  user              — teacher/admin or student accounts
  detection_class   — YOLO class registry
  detection_event   — one row per captured frame; always logged even with no detections.
                      windows_username stores the raw OS login so events
                      can be matched retroactively when an account is later created.
  detection         — one row per bounding box inside an event
"""
from __future__ import annotations

import base64
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

import bcrypt

DB_PATH = Path("data/monitor.db")


# ── Connection ────────────────────────────────────────────────────────────────

def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ── Schema ────────────────────────────────────────────────────────────────────

def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _conn() as c:
        # Migrate FIRST so all columns exist before indexes reference them
        _migrate(c)
        c.executescript("""
            CREATE TABLE IF NOT EXISTS computer (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                name         TEXT    NOT NULL UNIQUE,
                host_address TEXT    NOT NULL,
                created_at   TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS user (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                username        TEXT    NOT NULL UNIQUE,
                hashed_password TEXT    NOT NULL,
                role            TEXT    NOT NULL CHECK(role IN ('teacher','student')),
                created_by      INTEGER REFERENCES user(id) ON DELETE SET NULL,
                created_at      TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS detection_class (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                class_index  INTEGER NOT NULL UNIQUE,
                name         TEXT    NOT NULL UNIQUE,
                color_hex    TEXT    NOT NULL,
                created_at   TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS detection_event (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                computer_id      INTEGER NOT NULL REFERENCES computer(id)  ON DELETE CASCADE,
                user_id          INTEGER          REFERENCES user(id)       ON DELETE SET NULL,
                windows_username TEXT,
                detected_at      TEXT    NOT NULL,
                frame_blob       BLOB,
                had_detection    INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_event_computer  ON detection_event(computer_id);
            CREATE INDEX IF NOT EXISTS idx_event_user      ON detection_event(user_id);
            CREATE INDEX IF NOT EXISTS idx_event_winuser   ON detection_event(windows_username);
            CREATE INDEX IF NOT EXISTS idx_event_time      ON detection_event(detected_at);

            CREATE TABLE IF NOT EXISTS detection (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id   INTEGER NOT NULL REFERENCES detection_event(id) ON DELETE CASCADE,
                class_id   INTEGER NOT NULL REFERENCES detection_class(id) ON DELETE RESTRICT,
                confidence REAL    NOT NULL,
                box_x1     INTEGER NOT NULL,
                box_y1     INTEGER NOT NULL,
                box_x2     INTEGER NOT NULL,
                box_y2     INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_det_event ON detection(event_id);
            CREATE INDEX IF NOT EXISTS idx_det_class ON detection(class_id);
        """)
        c.commit()


def _migrate(c: sqlite3.Connection) -> None:
    """Add columns introduced after the initial release — safe on both new and existing DBs."""
    # Check the table exists before inspecting its columns (won't exist on first run)
    table_exists = c.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='detection_event'"
    ).fetchone()[0]
    if not table_exists:
        return   # fresh DB — executescript will create everything correctly

    existing = {row[1] for row in
                c.execute("PRAGMA table_info(detection_event)").fetchall()}
    if "windows_username" not in existing:
        c.execute(
            "ALTER TABLE detection_event ADD COLUMN windows_username TEXT"
        )


# ── Seed ─────────────────────────────────────────────────────────────────────

DEFAULT_CLASSES: list[dict] = [
    {"index": 0, "name": "DI",               "color": "#00ff00"},
    {"index": 1, "name": "Ekrano nuotraukos", "color": "#ff8000"},
    {"index": 2, "name": "Narsykle",          "color": "#0080ff"},
    {"index": 3, "name": "Notepad",           "color": "#8000ff"},
    {"index": 4, "name": "Paint",             "color": "#00ffff"},
    {"index": 5, "name": "PowerPoint",        "color": "#ff0080"},
    {"index": 6, "name": "Word",              "color": "#40c840"},
]


def seed_classes() -> None:
    with _conn() as c:
        if c.execute("SELECT COUNT(*) FROM detection_class").fetchone()[0] == 0:
            now = _now()
            c.executemany(
                "INSERT INTO detection_class (class_index, name, color_hex, created_at) "
                "VALUES (:index, :name, :color, :now)",
                [{**cls, "now": now} for cls in DEFAULT_CLASSES],
            )
            c.commit()


def ensure_default_teacher(username: str = "admin", password: str = "admin") -> None:
    with _conn() as c:
        if c.execute("SELECT COUNT(*) FROM user").fetchone()[0] == 0:
            c.execute(
                "INSERT INTO user (username, hashed_password, role, created_at) "
                "VALUES (?, ?, 'teacher', ?)",
                (username, _hash_pw(password), _now()),
            )
            c.commit()


# ── Password helpers ──────────────────────────────────────────────────────────

def _hash_pw(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def _verify_pw(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode(), hashed.encode())


# ── Users ─────────────────────────────────────────────────────────────────────

def create_user(
    username:      str,
    password:      str,
    role:          str,
    created_by_id: Optional[int] = None,
) -> int:
    """
    Create user account and immediately assign any anonymous events whose
    windows_username matches (case-insensitive) — so past data is never lost.
    """
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO user (username, hashed_password, role, created_by, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (username, _hash_pw(password), role, created_by_id, _now()),
        )
        new_id = cur.lastrowid
        c.commit()

    # Auto-assign previously anonymous events that match this Windows username
    _auto_assign_by_username(username, new_id)
    return new_id


def _auto_assign_by_username(username: str, user_id: int) -> int:
    """
    Assign all anonymous events (user_id IS NULL) whose windows_username
    matches username (case-insensitive) to user_id.
    Returns number of rows updated.
    """
    with _conn() as c:
        cur = c.execute(
            "UPDATE detection_event SET user_id = ? "
            "WHERE LOWER(windows_username) = LOWER(?) AND user_id IS NULL",
            (user_id, username),
        )
        c.commit()
        return cur.rowcount


def update_password(user_id: int, new_password: str) -> None:
    with _conn() as c:
        c.execute(
            "UPDATE user SET hashed_password = ? WHERE id = ?",
            (_hash_pw(new_password), user_id),
        )
        c.commit()


def delete_user(user_id: int) -> None:
    with _conn() as c:
        c.execute("DELETE FROM user WHERE id = ?", (user_id,))
        c.commit()


def get_user_by_username(username: str) -> Optional[dict]:
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM user WHERE username = ?", (username,)
        ).fetchone()
        return dict(row) if row else None


def get_user_by_id(user_id: int) -> Optional[dict]:
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM user WHERE id = ?", (user_id,)
        ).fetchone()
        return dict(row) if row else None


def verify_password(username: str, password: str) -> Optional[dict]:
    user = get_user_by_username(username)
    if user and _verify_pw(password, user["hashed_password"]):
        return user
    return None


def list_users() -> list[dict]:
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT id, username, role, created_at FROM user ORDER BY role, username"
        ).fetchall()]


# ── Computers ─────────────────────────────────────────────────────────────────

def upsert_computer(name: str, host_address: str) -> int:
    with _conn() as c:
        row = c.execute(
            "SELECT id FROM computer WHERE name = ?", (name,)
        ).fetchone()
        if row:
            return row["id"]
        cur = c.execute(
            "INSERT INTO computer (name, host_address, created_at) VALUES (?, ?, ?)",
            (name, host_address, _now()),
        )
        c.commit()
        return cur.lastrowid


def list_computers() -> list[dict]:
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM computer ORDER BY name"
        ).fetchall()]


# ── Detection classes ─────────────────────────────────────────────────────────

def list_classes() -> list[dict]:
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM detection_class ORDER BY class_index"
        ).fetchall()]


def get_class_by_index(class_index: int) -> Optional[dict]:
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM detection_class WHERE class_index = ?", (class_index,)
        ).fetchone()
        return dict(row) if row else None


# ── Detection events ──────────────────────────────────────────────────────────

def insert_event(
    computer_id:      int,
    dets:             list[dict],
    user_id:          Optional[int]   = None,
    windows_username: Optional[str]   = None,
    frame_bytes:      Optional[bytes] = None,
) -> int:
    """
    Write one detection_event row + one detection row per bounding box.
    Always called — even when dets is empty (had_detection = 0).
    windows_username is stored regardless of whether a system account exists,
    enabling retroactive assignment when the account is later created.
    """
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO detection_event "
            "(computer_id, user_id, windows_username, detected_at, frame_blob, had_detection) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (computer_id, user_id, windows_username, _now(),
             frame_bytes, 1 if dets else 0),
        )
        event_id = cur.lastrowid

        for d in dets:
            cls_row = c.execute(
                "SELECT id FROM detection_class WHERE class_index = ?",
                (d["class_id"],),
            ).fetchone()
            if cls_row is None:
                continue
            c.execute(
                "INSERT INTO detection "
                "(event_id, class_id, confidence, box_x1, box_y1, box_x2, box_y2) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (event_id, cls_row["id"], d["conf"], *d["box"]),
            )
        c.commit()
        return event_id


def get_event_frame_b64(event_id: int) -> Optional[str]:
    with _conn() as c:
        row = c.execute(
            "SELECT frame_blob FROM detection_event WHERE id = ?", (event_id,)
        ).fetchone()
    if row and row["frame_blob"]:
        b64 = base64.b64encode(bytes(row["frame_blob"])).decode()
        return f"data:image/jpeg;base64,{b64}"
    return None


# ── Retroactive assignment ────────────────────────────────────────────────────

def count_anonymous_events(computer_id: int) -> int:
    with _conn() as c:
        return c.execute(
            "SELECT COUNT(*) FROM detection_event "
            "WHERE computer_id = ? AND user_id IS NULL",
            (computer_id,),
        ).fetchone()[0]


def assign_anonymous_events(user_id: int, computer_id: int) -> int:
    """Manually assign anonymous events on a specific computer to a user."""
    with _conn() as c:
        cur = c.execute(
            "UPDATE detection_event SET user_id = ? "
            "WHERE computer_id = ? AND user_id IS NULL",
            (user_id, computer_id),
        )
        c.commit()
        return cur.rowcount


# ── Query ─────────────────────────────────────────────────────────────────────

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

    if class_name:
        joins.append("JOIN detection d ON d.event_id = e.id "
                     "JOIN detection_class dc ON dc.id = d.class_id")
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
            COALESCE(u.username, e.windows_username, '—')
                                AS student,
            e.had_detection,
            CASE WHEN e.frame_blob IS NOT NULL THEN 1 ELSE 0 END AS has_frame,
            GROUP_CONCAT(dc.name || ' (' || ROUND(d.confidence*100) || '%)', ', ')
                                AS detections
        FROM {' '.join(joins)}
        {where}
        GROUP BY e.id
        ORDER BY e.detected_at DESC
        LIMIT ?
    """
    with _conn() as c:
        return [dict(r) for r in c.execute(sql, params).fetchall()]