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
    {"name": "DI",               "color": "#00ff00"},
    {"name": "Ekrano nuotraukos", "color": "#ff8000"},
    {"name": "Narsykle",          "color": "#0080ff"},
    {"name": "Notepad",           "color": "#8000ff"},
    {"name": "Paint",             "color": "#00ffff"},
    {"name": "PowerPoint",        "color": "#ff0080"},
    {"name": "Word",              "color": "#40c840"},
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

    # 2. detection_event.os_username (originally windows_username, renamed to be OS-agnostic)
    if _table_exists(c, "detection_event"):
        if "os_username" not in _cols(c, "detection_event"):
            if "windows_username" in _cols(c, "detection_event"):
                c.execute("ALTER TABLE detection_event RENAME COLUMN windows_username TO os_username")
            else:
                c.execute("ALTER TABLE detection_event ADD COLUMN os_username TEXT")
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

    # 6b. Merge alert_rule into detection_class as notification_enabled column.
    #     Migrate existing enabled flags, then drop the alert_rule table.
    if _table_exists(c, "detection_class") and "notification_enabled" not in _cols(c, "detection_class"):
        c.execute(
            "ALTER TABLE detection_class "
            "ADD COLUMN notification_enabled INTEGER NOT NULL DEFAULT 0"
        )
        if _table_exists(c, "alert_rule"):
            c.execute("""
                UPDATE detection_class
                SET notification_enabled = (
                    SELECT ar.enabled FROM alert_rule ar
                    WHERE ar.class_id = detection_class.id
                )
                WHERE id IN (SELECT class_id FROM alert_rule)
            """)
            c.execute("DROP TABLE alert_rule")
        c.commit()

    # 8. Merge training_session into ml_model — add training config columns,
    #    copy latest session data per model, then drop the training_session table.
    if _table_exists(c, "ml_model") and "dataset_path" not in _cols(c, "ml_model"):
        for col, typedef in [
            ("dataset_path", "TEXT"),
            ("base_model",   "TEXT"),
            ("epochs",       "INTEGER"),
            ("batch",        "INTEGER"),
            ("device",       "TEXT"),
        ]:
            c.execute(f"ALTER TABLE ml_model ADD COLUMN {col} {typedef}")
        if _table_exists(c, "training_session"):
            c.execute("""
                UPDATE ml_model
                SET dataset_path = (SELECT ts.dataset_path FROM training_session ts
                                    WHERE ts.model_id = ml_model.id
                                    ORDER BY ts.started_at DESC LIMIT 1),
                    base_model   = (SELECT ts.base_model   FROM training_session ts
                                    WHERE ts.model_id = ml_model.id
                                    ORDER BY ts.started_at DESC LIMIT 1),
                    epochs       = (SELECT ts.epochs       FROM training_session ts
                                    WHERE ts.model_id = ml_model.id
                                    ORDER BY ts.started_at DESC LIMIT 1),
                    batch        = (SELECT ts.batch        FROM training_session ts
                                    WHERE ts.model_id = ml_model.id
                                    ORDER BY ts.started_at DESC LIMIT 1),
                    device       = (SELECT ts.device       FROM training_session ts
                                    WHERE ts.model_id = ml_model.id
                                    ORDER BY ts.started_at DESC LIMIT 1)
            """)
            c.execute("DROP TABLE training_session")
        c.commit()

    # 9. computer.group_id — added when computer_group was introduced.
    if _table_exists(c, "computer") and "group_id" not in _cols(c, "computer"):
        c.execute(
            "ALTER TABLE computer ADD COLUMN group_id INTEGER "
            "REFERENCES computer_group(id) ON DELETE SET NULL"
        )
        c.commit()

    # 10. computer_group_member: many-to-many computer ↔ group.
    #     Replaces the one-to-many computer.group_id FK.
    if not _table_exists(c, "computer_group_member"):
        c.execute("""
            CREATE TABLE computer_group_member (
                computer_id INTEGER NOT NULL REFERENCES computer(id) ON DELETE CASCADE,
                group_id    INTEGER NOT NULL REFERENCES computer_group(id) ON DELETE CASCADE,
                PRIMARY KEY (computer_id, group_id)
            )
        """)
        # Migrate existing single-group assignments into the join table
        if _table_exists(c, "computer") and "group_id" in _cols(c, "computer"):
            c.execute("""
                INSERT OR IGNORE INTO computer_group_member (computer_id, group_id)
                SELECT id, group_id FROM computer WHERE group_id IS NOT NULL
            """)
        c.commit()

    # 11. Expression index on LOWER(os_username) for case-insensitive auto-assign.
    #     The plain idx_event_winuser index on os_username cannot be used by
    #     WHERE LOWER(os_username) = LOWER(?), so we add a separate expression index.
    if _table_exists(c, "detection_event"):
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_event_winuser_lower "
            "ON detection_event(LOWER(os_username))"
        )
        c.commit()

    # 12. detection_class: drop class_index — it is model-specific and belongs in
    #     model_class(model_id, class_index, class_id).  Having it on detection_class
    #     meant two models with the same class at different indices would corrupt
    #     each other's name mapping on sync.
    if _table_exists(c, "detection_class") and "class_index" in _cols(c, "detection_class"):
        c.executescript("""
            PRAGMA foreign_keys = OFF;

            CREATE TABLE detection_class_v2 (
                id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                name                  TEXT    NOT NULL UNIQUE,
                color_hex             TEXT    NOT NULL,
                notification_enabled  INTEGER NOT NULL DEFAULT 0,
                created_at            TEXT    NOT NULL
            );

            INSERT INTO detection_class_v2
                   (id, name, color_hex, notification_enabled, created_at)
            SELECT  id, name, color_hex, notification_enabled, created_at
            FROM    detection_class;

            DROP TABLE detection_class;
            ALTER TABLE detection_class_v2 RENAME TO detection_class;

            PRAGMA foreign_keys = ON;
        """)
        # executescript auto-commits; create model_class and populate it now.
        c.execute("""
            CREATE TABLE IF NOT EXISTS model_class (
                model_id    INTEGER NOT NULL REFERENCES ml_model(id)        ON DELETE CASCADE,
                class_index INTEGER NOT NULL,
                class_id    INTEGER NOT NULL REFERENCES detection_class(id) ON DELETE CASCADE,
                UNIQUE(model_id, class_index)
            )
        """)
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_mc_model ON model_class(model_id)"
        )
        # Back-fill model_class from each model's stored classes_json.
        import json as _json
        for m_id, classes_json in c.execute(
            "SELECT id, classes_json FROM ml_model"
        ).fetchall():
            try:
                names = _json.loads(classes_json or "[]")
            except Exception:
                names = []
            for idx, name in enumerate(names):
                dc = c.execute(
                    "SELECT id FROM detection_class WHERE name = ?", (name,)
                ).fetchone()
                if dc:
                    c.execute(
                        "INSERT OR IGNORE INTO model_class "
                        "(model_id, class_index, class_id) VALUES (?, ?, ?)",
                        (m_id, idx, dc[0]),
                    )
        c.commit()

    # 13. notification.event_id: ON DELETE SET NULL → ON DELETE CASCADE.
    #     A notification whose event has been deleted (e.g. because its computer
    #     was removed) has no frame, no computer, and no student to display — it
    #     is a ghost record that counts against the unread badge but can never be
    #     investigated.  Cascade-delete removes it together with its event.
    #     Existing NULL event_id rows (already-orphaned ghosts) are pruned here too.
    #     Detection is based on absence of CASCADE in the stored DDL.
    if _table_exists(c, "notification"):
        ddl = c.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='notification'"
        ).fetchone()
        if ddl and "CASCADE" not in (ddl[0] or "").upper():
            c.executescript("""
                PRAGMA foreign_keys = OFF;

                CREATE TABLE notification_v3 (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id   INTEGER REFERENCES detection_event(id) ON DELETE CASCADE,
                    class_id   INTEGER REFERENCES detection_class(id) ON DELETE SET NULL,
                    is_read    INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT    NOT NULL
                );

                INSERT INTO notification_v3 (id, event_id, class_id, is_read, created_at)
                SELECT id, event_id, class_id, is_read, created_at
                FROM   notification
                WHERE  event_id IS NOT NULL;

                DROP TABLE notification;
                ALTER TABLE notification_v3 RENAME TO notification;

                PRAGMA foreign_keys = ON;
            """)
            c.commit()

    # 10b. Drop computer.group_id — superseded by computer_group_member.
    if _table_exists(c, "computer") and "group_id" in _cols(c, "computer"):
        c.executescript("""
            PRAGMA foreign_keys = OFF;

            CREATE TABLE computer_v2 (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                name         TEXT    NOT NULL UNIQUE,
                host_address TEXT    NOT NULL,
                created_at   TEXT    NOT NULL
            );

            INSERT INTO computer_v2 (id, name, host_address, created_at)
            SELECT id, name, host_address, created_at FROM computer;

            DROP TABLE computer;
            ALTER TABLE computer_v2 RENAME TO computer;

            PRAGMA foreign_keys = ON;
        """)
        c.commit()

    # 14. user.is_active — 1 = active (can log in), 0 = auto-created placeholder.
    if _table_exists(c, "user") and "is_active" not in _cols(c, "user"):
        c.execute(
            "ALTER TABLE user ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1"
        )
        c.commit()

    # 7. notification: remove redundant computer/student TEXT columns.
    #    These are now derived via JOINs through event_id.
    if _table_exists(c, "notification") and "computer" in _cols(c, "notification"):
        c.executescript("""
            PRAGMA foreign_keys = OFF;

            CREATE TABLE IF NOT EXISTS notification_v2 (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id   INTEGER REFERENCES detection_event(id) ON DELETE SET NULL,
                class_id   INTEGER REFERENCES detection_class(id) ON DELETE SET NULL,
                is_read    INTEGER NOT NULL DEFAULT 0,
                created_at TEXT    NOT NULL
            );

            INSERT INTO notification_v2
                   (id, event_id, class_id, is_read, created_at)
            SELECT  n.id, n.event_id, n.class_id, n.is_read, n.created_at
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
            is_active       INTEGER NOT NULL DEFAULT 1,
            created_by      INTEGER REFERENCES user(id) ON DELETE SET NULL,
            created_at      TEXT    NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_user_role ON user(role_id);

        -- computer groups ─────────────────────────────────────────────────────
        -- Logical groupings: Lab 1, Exam Room, etc.
        CREATE TABLE IF NOT EXISTS computer_group (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT    NOT NULL UNIQUE,
            description TEXT,
            created_at  TEXT    NOT NULL
        );

        -- computers ───────────────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS computer (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            name         TEXT    NOT NULL UNIQUE,
            host_address TEXT    NOT NULL,
            created_at   TEXT    NOT NULL
        );

        -- computer ↔ group membership (many-to-many) ──────────────────────────
        CREATE TABLE IF NOT EXISTS computer_group_member (
            computer_id INTEGER NOT NULL REFERENCES computer(id) ON DELETE CASCADE,
            group_id    INTEGER NOT NULL REFERENCES computer_group(id) ON DELETE CASCADE,
            PRIMARY KEY (computer_id, group_id)
        );
        CREATE INDEX IF NOT EXISTS idx_cgm_computer ON computer_group_member(computer_id);
        CREATE INDEX IF NOT EXISTS idx_cgm_group    ON computer_group_member(group_id);

        -- monitoring schedules ────────────────────────────────────────────────
        -- When to automatically start/stop monitoring a group.
        -- days_of_week: comma-separated 0-6 (0=Mon … 6=Sun)
        CREATE TABLE IF NOT EXISTS schedule (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id     INTEGER REFERENCES computer_group(id) ON DELETE CASCADE,
            name         TEXT    NOT NULL,
            days_of_week TEXT    NOT NULL DEFAULT '0,1,2,3,4',
            start_time   TEXT    NOT NULL,
            end_time     TEXT    NOT NULL,
            is_active    INTEGER NOT NULL DEFAULT 1,
            created_by   INTEGER REFERENCES user(id) ON DELETE SET NULL,
            created_at   TEXT    NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_schedule_group ON schedule(group_id);

        -- audit log ───────────────────────────────────────────────────────────
        -- Immutable trail of every significant action performed by a user.
        CREATE TABLE IF NOT EXISTS audit_log (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER REFERENCES user(id) ON DELETE SET NULL,
            action     TEXT    NOT NULL,
            entity     TEXT,
            entity_id  INTEGER,
            detail     TEXT,
            created_at TEXT    NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_audit_user ON audit_log(user_id);
        CREATE INDEX IF NOT EXISTS idx_audit_time ON audit_log(created_at);

        -- detection classes ───────────────────────────────────────────────────
        -- class_index is intentionally absent: it is model-specific and lives in
        -- model_class instead, so two models can share a class name at different
        -- indices without corrupting each other's mapping.
        CREATE TABLE IF NOT EXISTS detection_class (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            name                  TEXT    NOT NULL UNIQUE,
            color_hex             TEXT    NOT NULL,
            notification_enabled  INTEGER NOT NULL DEFAULT 0,
            created_at            TEXT    NOT NULL
        );

        -- detection events ────────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS detection_event (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            computer_id      INTEGER NOT NULL REFERENCES computer(id)   ON DELETE CASCADE,
            user_id          INTEGER          REFERENCES user(id)        ON DELETE SET NULL,
            model_id         INTEGER          REFERENCES ml_model(id)   ON DELETE SET NULL,
            os_username      TEXT,
            detected_at      TEXT    NOT NULL,
            frame_blob       BLOB,
            had_detection    INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_event_computer  ON detection_event(computer_id);
        CREATE INDEX IF NOT EXISTS idx_event_user      ON detection_event(user_id);
        CREATE INDEX IF NOT EXISTS idx_event_model     ON detection_event(model_id);
        CREATE INDEX IF NOT EXISTS idx_event_winuser       ON detection_event(os_username);
        CREATE INDEX IF NOT EXISTS idx_event_winuser_lower ON detection_event(LOWER(os_username));
        CREATE INDEX IF NOT EXISTS idx_event_time          ON detection_event(detected_at);
        CREATE INDEX IF NOT EXISTS idx_event_comp_time ON detection_event(computer_id, detected_at);
        CREATE INDEX IF NOT EXISTS idx_event_user_time ON detection_event(user_id, detected_at);

        -- detections ──────────────────────────────────────────────────────────
        -- class_id uses RESTRICT (not CASCADE/SET NULL) so that deleting a
        -- detection_class is permanently blocked while any detection references it.
        -- To remove a class you must first delete all detection_event rows for it
        -- (which cascade-delete their detection children automatically).
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

        -- notifications ───────────────────────────────────────────────────────
        -- computer and student are derived via event_id JOINs at query time.
        -- event_id uses CASCADE: a notification whose event is gone has no frame,
        -- computer, or student to display and should not survive its event.
        CREATE TABLE IF NOT EXISTS notification (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id   INTEGER REFERENCES detection_event(id) ON DELETE CASCADE,
            class_id   INTEGER REFERENCES detection_class(id) ON DELETE SET NULL,
            is_read    INTEGER NOT NULL DEFAULT 0,
            created_at TEXT    NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_notif_read ON notification(is_read);
        CREATE INDEX IF NOT EXISTS idx_notif_time ON notification(created_at);

        -- ml models ───────────────────────────────────────────────────────────
        -- Training config (dataset_path, base_model, epochs, batch, device)
        -- is stored directly here — no separate training_session table.
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
            dataset_path TEXT,
            base_model   TEXT,
            epochs       INTEGER,
            batch        INTEGER,
            device       TEXT,
            created_at   TEXT    NOT NULL,
            finished_at  TEXT
        );

        -- model → class index mapping ─────────────────────────────────────────
        -- Maps each model's raw output class indices to detection_class rows.
        -- Kept separate so two models can assign the same class at different
        -- indices without corrupting each other's mapping.
        CREATE TABLE IF NOT EXISTS model_class (
            model_id    INTEGER NOT NULL REFERENCES ml_model(id)        ON DELETE CASCADE,
            class_index INTEGER NOT NULL,
            class_id    INTEGER NOT NULL REFERENCES detection_class(id) ON DELETE CASCADE,
            UNIQUE(model_id, class_index)
        );
        CREATE INDEX IF NOT EXISTS idx_mc_model ON model_class(model_id);
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
            "INSERT OR IGNORE INTO detection_class (name, color_hex, created_at) "
            "VALUES (:name, :color, :now)",
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
