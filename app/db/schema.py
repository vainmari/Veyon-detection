"""
app/db/schema.py
────────────────
Database schema creation, incremental migrations, and seed data.

init_db()    — idempotent; safe to call on an existing database.
_migrate()   — runs before CREATE TABLE so it can reshape old schemas.
seed_classes() — inserts the default YOLO class set if the table is empty.
"""
from __future__ import annotations

import sqlite3

from app.db._core import DB_PATH, _conn, _now, _table_exists, _cols

DEFAULT_CLASSES: list[dict] = [
    {"index": 0, "name": "DI",               "color": "#00ff00"},
    {"index": 1, "name": "Ekrano nuotraukos", "color": "#ff8000"},
    {"index": 2, "name": "Narsykle",          "color": "#0080ff"},
    {"index": 3, "name": "Notepad",           "color": "#8000ff"},
    {"index": 4, "name": "Paint",             "color": "#00ffff"},
    {"index": 5, "name": "PowerPoint",        "color": "#ff0080"},
    {"index": 6, "name": "Word",              "color": "#40c840"},
]


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

    # 6. detection_event.model_id — link each event to the ML model that ran it
    if _table_exists(c, "detection_event"):
        if "model_id" not in _cols(c, "detection_event"):
            c.execute(
                "ALTER TABLE detection_event "
                "ADD COLUMN model_id INTEGER REFERENCES ml_model(id) ON DELETE SET NULL"
            )
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

    # 5. notification: replace denormalized class_name/class_color TEXT columns
    #    with a class_id FK into detection_class.  Existing rows are migrated by
    #    matching the stored class_name against detection_class.name.
    if _table_exists(c, "notification") and "class_name" in _cols(c, "notification"):
        c.executescript("""
            PRAGMA foreign_keys = OFF;

            CREATE TABLE IF NOT EXISTS notification_v2 (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id   INTEGER REFERENCES detection_event(id) ON DELETE SET NULL,
                class_id   INTEGER REFERENCES detection_class(id) ON DELETE SET NULL,
                computer   TEXT    NOT NULL,
                student    TEXT    NOT NULL,
                is_read    INTEGER NOT NULL DEFAULT 0,
                created_at TEXT    NOT NULL
            );

            INSERT INTO notification_v2
                   (id, event_id, class_id, computer, student, is_read, created_at)
            SELECT  n.id, n.event_id,
                    (SELECT dc.id FROM detection_class dc
                     WHERE dc.name = n.class_name LIMIT 1),
                    n.computer, n.student, n.is_read, n.created_at
            FROM notification n;

            DROP TABLE notification;
            ALTER TABLE notification_v2 RENAME TO notification;

            PRAGMA foreign_keys = ON;
        """)


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
            computer_id      INTEGER NOT NULL REFERENCES computer(id)   ON DELETE CASCADE,
            user_id          INTEGER          REFERENCES user(id)        ON DELETE SET NULL,
            model_id         INTEGER          REFERENCES ml_model(id)   ON DELETE SET NULL,
            windows_username TEXT,
            detected_at      TEXT    NOT NULL,
            frame_blob       BLOB,
            had_detection    INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_event_computer  ON detection_event(computer_id);
        CREATE INDEX IF NOT EXISTS idx_event_user      ON detection_event(user_id);
        CREATE INDEX IF NOT EXISTS idx_event_model     ON detection_event(model_id);
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
        -- class_id FK replaces the old denormalized class_name / class_color columns.
        -- Join detection_class to get name and color at query time.
        CREATE TABLE IF NOT EXISTS notification (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id   INTEGER REFERENCES detection_event(id) ON DELETE SET NULL,
            class_id   INTEGER REFERENCES detection_class(id) ON DELETE SET NULL,
            computer   TEXT    NOT NULL,
            student    TEXT    NOT NULL,
            is_read    INTEGER NOT NULL DEFAULT 0,
            created_at TEXT    NOT NULL
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


def ensure_default_admin(username: str = "admin", password: str = "admin") -> None:
    """Create a default admin account if no users exist at all."""
    from app.db.users import _hash_pw
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
