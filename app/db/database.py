"""
app/db/database.py
──────────────────
Schema
──────
  role              — role definitions (admin · teacher · student)
  computer          — monitored machines
  user              — accounts; role_id FK → role
                      All user-returning queries JOIN role so callers still
                      receive user["role"] as a plain string.
  detection_class   — YOLO class registry
  detection_event   — one row per captured frame (always logged)
                      windows_username stores the raw OS login for retroactive
                      assignment when an account is later created.
  detection         — one row per bounding box inside an event
  alert_rule        — prohibited-class rules
  notification      — fired alert records
  ml_model          — trained / imported YOLO models
  training_session  — training run records

Performance notes
─────────────────
  • Thread-local connections — each thread keeps one open sqlite3 connection.
    WAL journal mode lets readers and a writer proceed concurrently without
    blocking each other (important: multiple browser tabs + drain worker +
    detect worker all read/write simultaneously).
  • NORMAL synchronous is durably safe on WAL and ~3× faster than FULL.
  • Composite indexes on (computer_id, detected_at) and (user_id, detected_at)
    speed up the most common analytics queries.
  • DB_PATH change detection — if DB_PATH is swapped between calls (test
    fixtures) the stale per-thread connection is closed and a fresh one opened.
"""
from __future__ import annotations

import base64
import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

import bcrypt

DB_PATH = Path("data/monitor.db")

# ── Thread-local connection pool ──────────────────────────────────────────────
_tls = threading.local()


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


# ── Schema helpers ────────────────────────────────────────────────────────────

def _table_exists(c: sqlite3.Connection, name: str) -> bool:
    return c.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()[0] > 0


def _cols(c: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()}


# ── Migration ─────────────────────────────────────────────────────────────────

def _migrate(c: sqlite3.Connection) -> None:
    """
    Incremental, idempotent migrations applied before CREATE TABLE IF NOT EXISTS.
    Each block is guarded so it is safe to re-run on any existing database.
    """
    # 1. role table — must exist before any user migration references it
    c.execute("""
        CREATE TABLE IF NOT EXISTS role (
            id   INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT    NOT NULL UNIQUE
        )
    """)
    c.executemany(
        "INSERT OR IGNORE INTO role (name) VALUES (?)",
        [("admin",), ("teacher",), ("student",)],
    )
    c.commit()

    # 2. detection_event.windows_username (added after initial release)
    if _table_exists(c, "detection_event"):
        if "windows_username" not in _cols(c, "detection_event"):
            c.execute("ALTER TABLE detection_event ADD COLUMN windows_username TEXT")
            c.commit()

    # 3. ml_model.imgsz (added after initial release)
    if _table_exists(c, "ml_model"):
        if "imgsz" not in _cols(c, "ml_model"):
            c.execute("ALTER TABLE ml_model ADD COLUMN imgsz INTEGER DEFAULT 640")
            c.commit()

    # 4. user: migrate role TEXT → role_id INTEGER FK
    #    Handles both the original schema and the old CHECK-constraint variant.
    if _table_exists(c, "user") and "role_id" not in _cols(c, "user"):
        c.executescript("""
            PRAGMA foreign_keys = OFF;

            CREATE TABLE IF NOT EXISTS user_v2 (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                username        TEXT    NOT NULL UNIQUE,
                hashed_password TEXT    NOT NULL,
                role_id         INTEGER NOT NULL REFERENCES role(id),
                created_by      INTEGER REFERENCES user_v2(id) ON DELETE SET NULL,
                created_at      TEXT    NOT NULL
            );

            INSERT INTO user_v2
                    (id, username, hashed_password, role_id, created_by, created_at)
            SELECT  u.id, u.username, u.hashed_password,
                    r.id, u.created_by, u.created_at
            FROM    user u
            JOIN    role r ON r.name = u.role;

            DROP   TABLE user;
            ALTER  TABLE user_v2 RENAME TO user;

            PRAGMA foreign_keys = ON;
        """)
        # executescript auto-commits


# ── Init ──────────────────────────────────────────────────────────────────────

def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = _conn()
    _migrate(c)
    c.executescript("""
        -- roles ──────────────────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS role (
            id   INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT    NOT NULL UNIQUE
        );

        -- users ──────────────────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS user (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            username        TEXT    NOT NULL UNIQUE,
            hashed_password TEXT    NOT NULL,
            role_id         INTEGER NOT NULL REFERENCES role(id),
            created_by      INTEGER REFERENCES user(id) ON DELETE SET NULL,
            created_at      TEXT    NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_user_role ON user(role_id);

        -- computers ───────────────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS computer (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            name         TEXT    NOT NULL UNIQUE,
            host_address TEXT    NOT NULL,
            created_at   TEXT    NOT NULL
        );

        -- detection classes ───────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS detection_class (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            class_index  INTEGER NOT NULL UNIQUE,
            name         TEXT    NOT NULL UNIQUE,
            color_hex    TEXT    NOT NULL,
            created_at   TEXT    NOT NULL
        );

        -- detection events ────────────────────────────────────────────────────
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
        CREATE INDEX IF NOT EXISTS idx_event_comp_time ON detection_event(computer_id, detected_at);
        CREATE INDEX IF NOT EXISTS idx_event_user_time ON detection_event(user_id, detected_at);

        -- detections ──────────────────────────────────────────────────────────
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

        -- alert rules ─────────────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS alert_rule (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            class_id   INTEGER NOT NULL UNIQUE
                           REFERENCES detection_class(id) ON DELETE CASCADE,
            enabled    INTEGER NOT NULL DEFAULT 1,
            created_at TEXT    NOT NULL
        );

        -- notifications ───────────────────────────────────────────────────────
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

        -- ml models ───────────────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS ml_model (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            name         TEXT    NOT NULL UNIQUE,
            pt_path      TEXT,
            onnx_path    TEXT,
            nc           INTEGER NOT NULL,
            classes_json TEXT    NOT NULL,
            imgsz        INTEGER NOT NULL DEFAULT 640,
            map50        REAL,
            map50_95     REAL,
            precision    REAL,
            recall       REAL,
            is_active    INTEGER NOT NULL DEFAULT 0,
            status       TEXT    NOT NULL DEFAULT 'ready',
            created_at   TEXT    NOT NULL,
            finished_at  TEXT
        );

        -- training sessions ───────────────────────────────────────────────────
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
    # Seed roles idempotently after CREATE TABLE IF NOT EXISTS
    c.executemany(
        "INSERT OR IGNORE INTO role (name) VALUES (?)",
        [("admin",), ("teacher",), ("student",)],
    )
    c.commit()


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
    """Seed default detection classes only if the table is completely empty."""
    c = _conn()
    if c.execute("SELECT COUNT(*) FROM detection_class").fetchone()[0] == 0:
        now = _now()
        c.executemany(
            "INSERT INTO detection_class (class_index, name, color_hex, created_at) "
            "VALUES (:index, :name, :color, :now)",
            [{**cls, "now": now} for cls in DEFAULT_CLASSES],
        )
        c.commit()


def sync_classes_from_model(model_id: int) -> None:
    """Upsert detection_class rows from a trained model's class list."""
    from app.core.colors import class_hex
    model = get_model_by_id(model_id)
    if not model:
        return
    names = model.get("class_names", [])
    c = _conn()
    for i, name in enumerate(names):
        c.execute("""
            INSERT INTO detection_class (class_index, name, color_hex, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(class_index) DO UPDATE SET
                name      = excluded.name,
                color_hex = excluded.color_hex
        """, (i, name, class_hex(i), _now()))
    c.commit()


def ensure_default_admin(username: str = "admin", password: str = "admin") -> None:
    """Create a default admin account if no users exist at all."""
    c = _conn()
    if c.execute("SELECT COUNT(*) FROM user").fetchone()[0] == 0:
        role_row = c.execute("SELECT id FROM role WHERE name='admin'").fetchone()
        if role_row:
            c.execute(
                "INSERT INTO user (username, hashed_password, role_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (username, _hash_pw(password), role_row["id"], _now()),
            )
            c.commit()


# Backward-compatible alias — main.py calls ensure_default_teacher
ensure_default_teacher = ensure_default_admin


# ── Roles ─────────────────────────────────────────────────────────────────────

def list_roles() -> list[dict]:
    """Return all roles ordered by id."""
    c = _conn()
    return [dict(r) for r in c.execute("SELECT * FROM role ORDER BY id").fetchall()]


def get_role_id(name: str) -> Optional[int]:
    """Return the integer PK for a role name, or None if unknown."""
    c = _conn()
    row = c.execute("SELECT id FROM role WHERE name = ?", (name,)).fetchone()
    return int(row["id"]) if row else None


# ── Password helpers ──────────────────────────────────────────────────────────

def _hash_pw(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def _verify_pw(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode(), hashed.encode())


# ── Users ─────────────────────────────────────────────────────────────────────

# All user-returning queries JOIN role so callers use user["role"] as a plain string.
_USER_SELECT = """
    SELECT u.id, u.username, u.hashed_password,
           u.role_id, r.name AS role,
           u.created_by, u.created_at
    FROM   user u
    JOIN   role r ON r.id = u.role_id
"""


def create_user(
    username:      str,
    password:      str,
    role:          str,
    created_by_id: Optional[int] = None,
) -> int:
    """
    Create a user account and immediately auto-assign matching anonymous events
    (windows_username case-insensitive match) so past data is never lost.
    Returns the new user's integer id.
    """
    role_id = get_role_id(role)
    if role_id is None:
        raise ValueError(f"Unknown role: {role!r}")
    c = _conn()
    cur = c.execute(
        "INSERT INTO user (username, hashed_password, role_id, created_by, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (username, _hash_pw(password), role_id, created_by_id, _now()),
    )
    new_id = cur.lastrowid
    c.commit()
    _auto_assign_by_username(username, new_id)
    return new_id


def _auto_assign_by_username(username: str, user_id: int) -> int:
    """
    Assign all anonymous events whose windows_username matches username
    (case-insensitive).  Returns number of rows updated.
    """
    c = _conn()
    cur = c.execute(
        "UPDATE detection_event SET user_id = ? "
        "WHERE LOWER(windows_username) = LOWER(?) AND user_id IS NULL",
        (user_id, username),
    )
    c.commit()
    return cur.rowcount


def update_password(user_id: int, new_password: str) -> None:
    c = _conn()
    c.execute(
        "UPDATE user SET hashed_password = ? WHERE id = ?",
        (_hash_pw(new_password), user_id),
    )
    c.commit()


def delete_user(user_id: int) -> None:
    c = _conn()
    c.execute("DELETE FROM user WHERE id = ?", (user_id,))
    c.commit()


def get_user_by_username(username: str) -> Optional[dict]:
    c = _conn()
    row = c.execute(
        _USER_SELECT + "WHERE u.username = ?", (username,)
    ).fetchone()
    return dict(row) if row else None


def get_user_by_id(user_id: int) -> Optional[dict]:
    c = _conn()
    row = c.execute(
        _USER_SELECT + "WHERE u.id = ?", (user_id,)
    ).fetchone()
    return dict(row) if row else None


def verify_password(username: str, password: str) -> Optional[dict]:
    user = get_user_by_username(username)
    if user and _verify_pw(password, user["hashed_password"]):
        return user
    return None


def list_users() -> list[dict]:
    c = _conn()
    return [dict(r) for r in c.execute(
        _USER_SELECT + "ORDER BY r.name, u.username"
    ).fetchall()]


# ── Computers ─────────────────────────────────────────────────────────────────

def upsert_computer(name: str, host_address: str) -> int:
    c = _conn()
    row = c.execute("SELECT id FROM computer WHERE name = ?", (name,)).fetchone()
    if row:
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


# ── Detection classes ─────────────────────────────────────────────────────────

def list_classes() -> list[dict]:
    c = _conn()
    return [dict(r) for r in c.execute(
        "SELECT * FROM detection_class ORDER BY class_index"
    ).fetchall()]


def get_class_by_index(class_index: int) -> Optional[dict]:
    c = _conn()
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
    """
    c = _conn()
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
    c = _conn()
    row = c.execute(
        "SELECT frame_blob FROM detection_event WHERE id = ?", (event_id,)
    ).fetchone()
    if row and row["frame_blob"]:
        b64 = base64.b64encode(bytes(row["frame_blob"])).decode()
        return f"data:image/jpeg;base64,{b64}"
    return None


# ── Retroactive assignment ────────────────────────────────────────────────────

def count_anonymous_events(computer_id: int) -> int:
    c = _conn()
    return c.execute(
        "SELECT COUNT(*) FROM detection_event "
        "WHERE computer_id = ? AND user_id IS NULL",
        (computer_id,),
    ).fetchone()[0]


def assign_anonymous_events(user_id: int, computer_id: int) -> int:
    """Manually assign anonymous events on a specific computer to a user."""
    c = _conn()
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
    c = _conn()
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
    c = _conn()
    c.execute("""
        INSERT INTO alert_rule (class_id, enabled, created_at)
        VALUES (?, ?, ?)
        ON CONFLICT(class_id) DO UPDATE SET enabled = excluded.enabled
    """, (class_id, 1 if enabled else 0, _now()))
    c.commit()


def get_prohibited_class_ids() -> dict[int, str]:
    """Return {class_index: color_hex} for all enabled alert rules."""
    c = _conn()
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
    c = _conn()
    cur = c.execute(
        "INSERT INTO notification "
        "(event_id, class_name, class_color, computer, student, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (event_id, class_name, class_color, computer, student, _now()),
    )
    c.commit()
    return cur.lastrowid


def list_notifications(limit: int = 60) -> list[dict]:
    c = _conn()
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
    c = _conn()
    return c.execute(
        "SELECT COUNT(*) FROM notification WHERE is_read = 0"
    ).fetchone()[0]


def mark_read(notification_id: int) -> None:
    c = _conn()
    c.execute("UPDATE notification SET is_read = 1 WHERE id = ?", (notification_id,))
    c.commit()


def mark_all_read() -> None:
    c = _conn()
    c.execute("UPDATE notification SET is_read = 1")
    c.commit()


# ── ML Models ─────────────────────────────────────────────────────────────────

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
    c = _conn()
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
    allowed = {
        "name", "pt_path", "onnx_path", "nc", "classes_json",
        "imgsz", "map50", "map50_95", "precision",
        "recall", "status", "finished_at", "is_active",
    }
    sets = ", ".join(f"{k}=?" for k in kwargs if k in allowed)
    vals = [v for k, v in kwargs.items() if k in allowed]
    if not sets:
        return
    c = _conn()
    c.execute(f"UPDATE ml_model SET {sets} WHERE id=?", [*vals, model_id])
    c.commit()


def get_active_model() -> Optional[dict]:
    c = _conn()
    row = c.execute(
        "SELECT * FROM ml_model WHERE is_active = 1 LIMIT 1"
    ).fetchone()
    if not row:
        return None
    d = dict(row)
    d["class_names"] = json.loads(d.get("classes_json", "[]"))
    return d


def get_model_by_id(model_id: int) -> Optional[dict]:
    c = _conn()
    row = c.execute(
        "SELECT * FROM ml_model WHERE id = ?", (model_id,)
    ).fetchone()
    if not row:
        return None
    d = dict(row)
    d["class_names"] = json.loads(d.get("classes_json", "[]"))
    return d


def set_active_model(model_id: int) -> None:
    c = _conn()
    c.execute("UPDATE ml_model SET is_active = 0")
    c.execute("UPDATE ml_model SET is_active = 1 WHERE id = ?", (model_id,))
    c.commit()


def list_models() -> list[dict]:
    """Return all models joined with their most recent training session."""
    c = _conn()
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
    c = _conn()
    rows = c.execute(
        "SELECT * FROM training_session WHERE model_id = ? "
        "ORDER BY started_at DESC",
        (model_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def delete_model(model_id: int) -> None:
    c = _conn()
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
    c = _conn()
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
    c = _conn()
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
    w, p = _analytics_where(computer_id, user_id, from_date, to_date)
    c = _conn()
    base = f"FROM detection_event e LEFT JOIN user u ON u.id=e.user_id {w}"
    total    = c.execute(f"SELECT COUNT(*) {base}", p).fetchone()[0]
    and_or   = "AND" if w else "WHERE"
    hits     = c.execute(
        f"SELECT COUNT(*) {base} {and_or} e.had_detection=1", p
    ).fetchone()[0]
    students = c.execute(
        f"SELECT COUNT(DISTINCT COALESCE(e.user_id, e.windows_username)) {base}", p
    ).fetchone()[0]
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
                                AS detections
        FROM {' '.join(joins)}
        {where}
        GROUP BY e.id
        ORDER BY e.detected_at DESC
        LIMIT ?
    """
    c = _conn()
    return [dict(r) for r in c.execute(sql, params).fetchall()]