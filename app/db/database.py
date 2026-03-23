"""
app/db/database.py
──────────────────
Schema
──────
  computer          — monitored machines
  user              — teacher/admin or student accounts
  detection_class   — YOLO class registry
  detection_event   — one row per captured frame; always logged even with no detections.
                      windows_username stores the raw OS login (e.g. "Lina") so events
                      can be matched retroactively when an account is later created.
  detection         — one row per bounding box inside an event
"""
from __future__ import annotations

import base64
import json
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
                role            TEXT    NOT NULL CHECK(role IN ('admin','teacher','student')),
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
            CREATE TABLE IF NOT EXISTS alert_rule (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                class_id   INTEGER NOT NULL UNIQUE
                               REFERENCES detection_class(id) ON DELETE CASCADE,
                enabled    INTEGER NOT NULL DEFAULT 1,
                created_at TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS notification (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id    INTEGER REFERENCES detection_event(id) ON DELETE SET NULL,
                class_name  TEXT    NOT NULL,
                class_color TEXT    NOT NULL DEFAULT '#888888',
                computer    TEXT    NOT NULL,
                student     TEXT    NOT NULL,
                is_read     INTEGER NOT NULL DEFAULT 0,
                created_at  TEXT    NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_notif_read ON notification(is_read);
            CREATE INDEX IF NOT EXISTS idx_notif_time ON notification(created_at);

            -- ── ML Models ────────────────────────────────────────────────────
            CREATE TABLE IF NOT EXISTS ml_model (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                name         TEXT    NOT NULL UNIQUE,
                pt_path      TEXT,
                onnx_path    TEXT,
                nc           INTEGER NOT NULL,
                classes_json TEXT    NOT NULL,
                map50        REAL,
                map50_95     REAL,
                precision    REAL,
                recall       REAL,
                is_active    INTEGER NOT NULL DEFAULT 0,
                status       TEXT    NOT NULL DEFAULT 'ready',
                created_at   TEXT    NOT NULL,
                finished_at  TEXT
            );

            -- ── Training sessions ─────────────────────────────────────────────
            CREATE TABLE IF NOT EXISTS training_session (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                model_id     INTEGER REFERENCES ml_model(id) ON DELETE CASCADE,
                dataset_path TEXT    NOT NULL,
                base_model   TEXT    NOT NULL,
                epochs       INTEGER NOT NULL,
                imgsz        INTEGER NOT NULL,
                batch        INTEGER NOT NULL,
                device       TEXT,
                status       TEXT    NOT NULL DEFAULT 'running',
                started_at   TEXT    NOT NULL,
                finished_at  TEXT
            );

        """)
        c.commit()


def _migrate(c: sqlite3.Connection) -> None:
    """Add columns and fix constraints introduced after initial release."""
    # ── detection_event.windows_username ─────────────────────────────────────
    de_exists = c.execute(
        "SELECT COUNT(*) FROM sqlite_master "
        "WHERE type='table' AND name='detection_event'"
    ).fetchone()[0]
    if de_exists:
        de_cols = {r[1] for r in c.execute(
            "PRAGMA table_info(detection_event)").fetchall()}
        if "windows_username" not in de_cols:
            c.execute(
                "ALTER TABLE detection_event ADD COLUMN windows_username TEXT")

    # ── ml_model.imgsz ───────────────────────────────────────────────────────
    ml_exists = c.execute(
        "SELECT COUNT(*) FROM sqlite_master "
        "WHERE type='table' AND name='ml_model'"
    ).fetchone()[0]
    if ml_exists:
        ml_cols = {r[1] for r in c.execute(
            "PRAGMA table_info(ml_model)").fetchall()}
        if "imgsz" not in ml_cols:
            c.execute(
                "ALTER TABLE ml_model ADD COLUMN imgsz INTEGER DEFAULT 640")

    # ── user role: add 'admin' to allowed values ──────────────────────────────
    # SQLite can't ALTER a CHECK constraint, so recreate the table if the old
    # constraint is still in place (detectable by checking for 'admin' in schema).
    user_exists = c.execute(
        "SELECT COUNT(*) FROM sqlite_master "
        "WHERE type='table' AND name='user'"
    ).fetchone()[0]
    if user_exists:
        schema = c.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='user'"
        ).fetchone()
        if schema and "'admin'" not in schema[0]:
            # Recreate user table with updated CHECK constraint
            c.executescript("""
                PRAGMA foreign_keys = OFF;
                CREATE TABLE IF NOT EXISTS user_new (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    username        TEXT    NOT NULL UNIQUE,
                    hashed_password TEXT    NOT NULL,
                    role            TEXT    NOT NULL
                                    CHECK(role IN ('admin','teacher','student')),
                    created_by      INTEGER REFERENCES user_new(id)
                                    ON DELETE SET NULL,
                    created_at      TEXT    NOT NULL
                );
                INSERT INTO user_new
                    SELECT id, username, hashed_password, role,
                           created_by, created_at FROM user;
                DROP TABLE user;
                ALTER TABLE user_new RENAME TO user;
                PRAGMA foreign_keys = ON;
            """)


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
    """Seed default classes only if table is completely empty."""
    with _conn() as c:
        if c.execute("SELECT COUNT(*) FROM detection_class").fetchone()[0] == 0:
            now = _now()
            c.executemany(
                "INSERT INTO detection_class (class_index, name, color_hex, created_at) "
                "VALUES (:index, :name, :color, :now)",
                [{**cls, "now": now} for cls in DEFAULT_CLASSES],
            )
            c.commit()


def sync_classes_from_model(model_id: int) -> None:
    """
    Upsert detection_class rows from a trained model's class list.
    Existing rows are updated (name + color); old rows beyond the new model's
    class count are left intact (FK safety) but won't appear in detections.
    """
    from app.core.colors import class_hex
    model = get_model_by_id(model_id)
    if not model:
        return
    names = model.get("class_names", [])
    with _conn() as c:
        for i, name in enumerate(names):
            c.execute("""
                INSERT INTO detection_class (class_index, name, color_hex, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(class_index) DO UPDATE SET
                    name      = excluded.name,
                    color_hex = excluded.color_hex
            """, (i, name, class_hex(i), _now()))
        c.commit()


def ensure_default_teacher(username: str = "admin", password: str = "admin") -> None:
    """Create a default admin account if no users exist at all."""
    with _conn() as c:
        if c.execute("SELECT COUNT(*) FROM user").fetchone()[0] == 0:
            c.execute(
                "INSERT INTO user (username, hashed_password, role, created_at) "
                "VALUES (?, ?, 'admin', ?)",
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


# ── Alert rules ───────────────────────────────────────────────────────────────

def list_alert_rules() -> list[dict]:
    """All detection classes joined with their alert rule (enabled flag)."""
    with _conn() as c:
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
    with _conn() as c:
        c.execute("""
            INSERT INTO alert_rule (class_id, enabled, created_at)
            VALUES (?, ?, ?)
            ON CONFLICT(class_id) DO UPDATE SET enabled = excluded.enabled
        """, (class_id, 1 if enabled else 0, _now()))
        c.commit()


def get_prohibited_class_ids() -> dict[int, str]:
    """Return {class_index: color_hex} for all enabled alert rules."""
    with _conn() as c:
        rows = c.execute("""
            SELECT dc.class_index, dc.color_hex
            FROM alert_rule ar
            JOIN detection_class dc ON dc.id = ar.class_id
            WHERE ar.enabled = 1
        """).fetchall()
    return {r[0]: r[1] for r in rows}


# ── Notifications ─────────────────────────────────────────────────────────────

def insert_notification(
    event_id:    int,
    class_name:  str,
    class_color: str,
    computer:    str,
    student:     str,
) -> int:
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO notification "
            "(event_id, class_name, class_color, computer, student, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (event_id, class_name, class_color, computer, student, _now()),
        )
        c.commit()
        return cur.lastrowid


def list_notifications(limit: int = 60) -> list[dict]:
    with _conn() as c:
        rows = c.execute("""
            SELECT
                n.id, n.event_id, n.class_name, n.class_color,
                n.computer, n.student, n.is_read, n.created_at,
                CASE WHEN e.frame_blob IS NOT NULL THEN 1 ELSE 0 END AS has_frame
            FROM notification n
            LEFT JOIN detection_event e ON e.id = n.event_id
            ORDER BY n.created_at DESC
            LIMIT ?
        """, (limit,)).fetchall()
    return [dict(r) for r in rows]


def count_unread_notifications() -> int:
    with _conn() as c:
        return c.execute(
            "SELECT COUNT(*) FROM notification WHERE is_read = 0"
        ).fetchone()[0]


def mark_read(notification_id: int) -> None:
    with _conn() as c:
        c.execute("UPDATE notification SET is_read = 1 WHERE id = ?",
                  (notification_id,))
        c.commit()


def mark_all_read() -> None:
    with _conn() as c:
        c.execute("UPDATE notification SET is_read = 1")
        c.commit()


# ── ML Models ────────────────────────────────────────────────────────────────

def create_ml_model(
    name:        str,
    nc:          int,
    class_names: list[str],
    pt_path:     Optional[str] = None,
    onnx_path:   Optional[str] = None,
    map50:       float = 0.0,
    map50_95:    float = 0.0,
    precision:   float = 0.0,
    recall:      float = 0.0,
    status:      str   = "ready",
    imgsz:       int   = 640,
) -> int:
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO ml_model "
            "(name, pt_path, onnx_path, nc, classes_json, map50, map50_95, "
            " precision, recall, status, imgsz, created_at, finished_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (name, pt_path, onnx_path, nc,
             json.dumps(class_names),
             map50, map50_95, precision, recall,
             status, imgsz, _now(), _now()),
        )
        c.commit()
        return cur.lastrowid


def update_ml_model(model_id: int, **kwargs) -> None:
    allowed = {"pt_path","onnx_path","map50","map50_95","precision",
               "recall","status","finished_at","is_active"}
    sets    = ", ".join(f"{k}=?" for k in kwargs if k in allowed)
    vals    = [v for k, v in kwargs.items() if k in allowed]
    if not sets:
        return
    with _conn() as c:
        c.execute(f"UPDATE ml_model SET {sets} WHERE id=?", [*vals, model_id])
        c.commit()


def get_active_model() -> Optional[dict]:
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM ml_model WHERE is_active = 1 LIMIT 1"
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    d["class_names"] = json.loads(d.get("classes_json", "[]"))
    return d


def get_model_by_id(model_id: int) -> Optional[dict]:
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM ml_model WHERE id = ?", (model_id,)
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    d["class_names"] = json.loads(d.get("classes_json", "[]"))
    return d


def set_active_model(model_id: int) -> None:
    with _conn() as c:
        c.execute("UPDATE ml_model SET is_active = 0")
        c.execute("UPDATE ml_model SET is_active = 1 WHERE id = ?", (model_id,))
        c.commit()


def list_models() -> list[dict]:
    """Return all models joined with their most recent training session."""
    with _conn() as c:
        rows = c.execute("""
            SELECT m.*,
                (SELECT ts.base_model FROM training_session ts
                 WHERE ts.model_id = m.id
                 ORDER BY ts.started_at DESC LIMIT 1) AS base_model
            FROM ml_model m
            ORDER BY m.created_at DESC
        """).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["class_names"] = json.loads(d.get("classes_json", "[]"))
        result.append(d)
    return result


def list_model_sessions(model_id: int) -> list[dict]:
    """Return all training sessions for a model, newest first."""
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM training_session WHERE model_id = ? "
            "ORDER BY started_at DESC",
            (model_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def delete_model(model_id: int) -> None:
    with _conn() as c:
        c.execute("DELETE FROM ml_model WHERE id = ?", (model_id,))
        c.commit()


# ── Training sessions ─────────────────────────────────────────────────────────

def create_training_session(
    model_id:     int,
    dataset_path: str,
    base_model:   str,
    epochs:       int,
    imgsz:        int,
    batch:        int,
    device:       str,
) -> int:
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO training_session "
            "(model_id, dataset_path, base_model, epochs, imgsz, batch, "
            " device, started_at) VALUES (?,?,?,?,?,?,?,?)",
            (model_id, dataset_path, base_model, epochs,
             imgsz, batch, device, _now()),
        )
        c.commit()
        return cur.lastrowid


def update_training_session(session_id: int, **kwargs) -> None:
    allowed = {"status", "finished_at"}
    sets    = ", ".join(f"{k}=?" for k in kwargs if k in allowed)
    vals    = [v for k, v in kwargs.items() if k in allowed]
    if not sets:
        return
    with _conn() as c:
        c.execute(
            f"UPDATE training_session SET {sets} WHERE id=?",
            [*vals, session_id],
        )
        c.commit()


# ── Analytics queries ─────────────────────────────────────────────────────────

def get_summary_stats(
    computer_id: Optional[int] = None,
    user_id:     Optional[int] = None,
    from_date:   str           = "",
    to_date:     str           = "",
) -> dict:
    """Total events, detection events, unique active students, busiest class."""
    w, p = _analytics_where(computer_id, user_id, from_date, to_date)
    with _conn() as c:
        base = f"FROM detection_event e LEFT JOIN user u ON u.id=e.user_id {w}"
        total   = c.execute(f"SELECT COUNT(*) {base}", p).fetchone()[0]
        hits    = c.execute(f"SELECT COUNT(*) {base} {'AND' if w else 'WHERE'} e.had_detection=1", p).fetchone()[0]
        students = c.execute(
            f"SELECT COUNT(DISTINCT COALESCE(e.user_id, e.windows_username)) {base}", p
        ).fetchone()[0]
        # busiest class
        row = c.execute(f"""
            SELECT dc.name, COUNT(*) AS cnt
            FROM detection d
            JOIN detection_event e ON e.id = d.event_id
            JOIN detection_class dc ON dc.id = d.class_id
            LEFT JOIN user u ON u.id = e.user_id
            {w}
            GROUP BY dc.name ORDER BY cnt DESC LIMIT 1
        """, p).fetchone()
        top_class = row[0] if row else "—"
    return {
        "total_events":      total,
        "detection_events":  hits,
        "active_students":   students,
        "top_class":         top_class,
    }


def get_class_distribution(
    computer_id: Optional[int] = None,
    user_id:     Optional[int] = None,
    from_date:   str           = "",
    to_date:     str           = "",
) -> list[dict]:
    """Detection count per class, sorted descending."""
    w, p = _analytics_where(computer_id, user_id, from_date, to_date)
    with _conn() as c:
        rows = c.execute(f"""
            SELECT dc.name, dc.color_hex, COUNT(*) AS cnt
            FROM detection d
            JOIN detection_event e ON e.id = d.event_id
            JOIN detection_class dc ON dc.id = d.class_id
            LEFT JOIN user u ON u.id = e.user_id
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
    """Events and detection-hits grouped by calendar day."""
    w, p = _analytics_where(computer_id, user_id, from_date, to_date)
    with _conn() as c:
        rows = c.execute(f"""
            SELECT
                SUBSTR(e.detected_at, 1, 10)  AS day,
                COUNT(*)                       AS total,
                SUM(e.had_detection)           AS hits
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
    """Detection-hit count per student (teacher-only view)."""
    w, p = _analytics_where(computer_id, None, from_date, to_date)
    with _conn() as c:
        rows = c.execute(f"""
            SELECT
                COALESCE(u.username, e.windows_username, '(unknown)') AS student,
                COUNT(*) AS hits
            FROM detection_event e
            LEFT JOIN user u ON u.id = e.user_id
            {w} {'AND' if w else 'WHERE'} e.had_detection = 1
            GROUP BY student
            ORDER BY hits DESC
            LIMIT 20
        """, p).fetchall()
    return [{"student": r[0], "hits": r[1]} for r in rows]


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