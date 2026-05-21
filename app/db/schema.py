"""
app/db/schema.py
────────────────
Database schema creation and seed data.

init_db()              — idempotent CREATE TABLE IF NOT EXISTS for every table.
seed_classes()         — inserts the default detection class set if empty.
ensure_default_admin() — creates the initial admin account when DB is empty.

This is the initial public release; no incremental migrations are kept. If you
need to change the schema after a release, add a versioned migration step here.
"""
from __future__ import annotations

from app.db._core import DB_PATH, _conn, _now

DEFAULT_CLASSES: list[dict] = [
    {"name": "DI",                "color": "#00ff00"},
    {"name": "Ekrano nuotraukos", "color": "#ff8000"},
    {"name": "Narsykle",          "color": "#0080ff"},
    {"name": "Notepad",           "color": "#8000ff"},
    {"name": "Paint",             "color": "#00ffff"},
    {"name": "PowerPoint",        "color": "#ff0080"},
    {"name": "Word",              "color": "#40c840"},
]


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = _conn()
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
        -- model_id: NULL = use the currently-active model at runtime
        -- use_custom_notify_classes: 0 = global detection_class.notification_enabled,
        --                            1 = schedule_notification_class rows
        CREATE TABLE IF NOT EXISTS schedule (
            id                        INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id                  INTEGER REFERENCES computer_group(id) ON DELETE CASCADE,
            name                      TEXT    NOT NULL,
            days_of_week              TEXT    NOT NULL DEFAULT '0,1,2,3,4',
            start_time                TEXT    NOT NULL,
            end_time                  TEXT    NOT NULL,
            is_active                 INTEGER NOT NULL DEFAULT 1,
            model_id                  INTEGER REFERENCES ml_model(id) ON DELETE SET NULL,
            use_custom_notify_classes INTEGER NOT NULL DEFAULT 0,
            created_by                INTEGER REFERENCES user(id) ON DELETE SET NULL,
            created_at                TEXT    NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_schedule_group ON schedule(group_id);

        -- per-schedule notification class overrides ───────────────────────────
        -- Only consulted when schedule.use_custom_notify_classes = 1.
        CREATE TABLE IF NOT EXISTS schedule_notification_class (
            schedule_id INTEGER NOT NULL REFERENCES schedule(id)        ON DELETE CASCADE,
            class_id    INTEGER NOT NULL REFERENCES detection_class(id) ON DELETE CASCADE,
            PRIMARY KEY (schedule_id, class_id)
        );
        CREATE INDEX IF NOT EXISTS idx_snc_schedule ON schedule_notification_class(schedule_id);

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
        -- Fine-tune provenance: parent_model_id, finetune_lr, finetune_frozen
        CREATE TABLE IF NOT EXISTS ml_model (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            name            TEXT    NOT NULL UNIQUE,
            pt_path         TEXT,
            onnx_path       TEXT,
            nc              INTEGER NOT NULL,
            classes_json    TEXT    NOT NULL,
            imgsz           INTEGER NOT NULL DEFAULT 640,
            map50           REAL,
            map50_95        REAL,
            precision       REAL,
            recall          REAL,
            is_active       INTEGER NOT NULL DEFAULT 0,
            status          TEXT    NOT NULL DEFAULT 'ready',
            dataset_path    TEXT,
            base_model      TEXT,
            epochs          INTEGER,
            batch           INTEGER,
            device          TEXT,
            parent_model_id INTEGER REFERENCES ml_model(id) ON DELETE SET NULL,
            finetune_lr     REAL,
            finetune_frozen INTEGER,
            created_at      TEXT    NOT NULL,
            finished_at     TEXT
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


def ensure_default_admin(username: str, password: str) -> None:
    """
    Create the initial admin account when no users exist yet. Both arguments
    are required — there is no fallback to a hard-coded credential. The
    bootstrap layer (`app/main.py:_bootstrap_admin`) is responsible for
    sourcing them from the environment.
    """
    from app.db.users import _hash_pw
    if not username or not password:
        raise ValueError("ensure_default_admin requires both username and password")
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


# Backward-compatible alias — older imports may still reference this.
ensure_default_teacher = ensure_default_admin
