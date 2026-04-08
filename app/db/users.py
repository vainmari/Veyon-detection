"""
app/db/users.py
───────────────
Role lookups and user CRUD (create, read, update, delete).
"""
from __future__ import annotations

from typing import Optional

import bcrypt

from app.db._core import _conn, _now

# ── Password helpers ──────────────────────────────────────────────────────────

def _hash_pw(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def _verify_pw(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode(), hashed.encode())


# All user-returning queries JOIN role so callers get user["role"] as a string.
_USER_SELECT = """
    SELECT u.id, u.username, u.hashed_password,
           u.role_id, r.name AS role,
           u.created_by, u.created_at
    FROM   user u
    JOIN   role r ON r.id = u.role_id
"""


# ── Roles ─────────────────────────────────────────────────────────────────────

def list_roles() -> list[dict]:
    c = _conn()
    return [dict(r) for r in c.execute("SELECT * FROM role ORDER BY id").fetchall()]


def get_role_id(name: str) -> Optional[int]:
    c = _conn()
    row = c.execute("SELECT id FROM role WHERE name = ?", (name,)).fetchone()
    return int(row["id"]) if row else None


# ── Users ─────────────────────────────────────────────────────────────────────

def create_user(
    username:      str,
    password:      str,
    role:          str,
    created_by_id: Optional[int] = None,
) -> int:
    """
    Insert the user row and log the audit entry.  Returns the new user id.

    Historical event assignment is intentionally NOT done here — call
    auto_assign_user_events(username, new_id) separately (ideally in a
    background task) so this function stays fast.
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
    from app.db.audit import log_action
    log_action("user.create", entity="user", entity_id=new_id,
               detail=f"role={role}, created_by={created_by_id}")
    return new_id


def auto_assign_user_events(username: str, user_id: int, batch_size: int = 500) -> int:
    """
    Assign all anonymous detection_event rows whose os_username matches
    *username* (case-insensitive) to *user_id*, working in small batches so
    the SQLite write lock is held for only a few milliseconds at a time.
    Returns the total number of rows updated.
    """
    c = _conn()
    total = 0
    while True:
        cur = c.execute(
            """
            UPDATE detection_event SET user_id = ?
            WHERE id IN (
                SELECT id FROM detection_event
                WHERE LOWER(os_username) = LOWER(?) AND user_id IS NULL
                LIMIT ?
            )
            """,
            (user_id, username, batch_size),
        )
        c.commit()
        n = cur.rowcount
        total += n
        if n < batch_size:
            break
    return total


def nullify_user_events(user_id: int, batch_size: int = 500) -> int:
    """
    Set user_id = NULL on all detection_event rows for *user_id* in batches
    before the user row is deleted.  This way the subsequent DELETE has no
    ON DELETE SET NULL cascade work to do and completes instantly.
    Returns the total number of rows cleared.
    """
    c = _conn()
    total = 0
    while True:
        cur = c.execute(
            """
            UPDATE detection_event SET user_id = NULL
            WHERE id IN (
                SELECT id FROM detection_event WHERE user_id = ? LIMIT ?
            )
            """,
            (user_id, batch_size),
        )
        c.commit()
        n = cur.rowcount
        total += n
        if n < batch_size:
            break
    return total


def update_password(user_id: int, new_password: str) -> None:
    c = _conn()
    c.execute(
        "UPDATE user SET hashed_password = ? WHERE id = ?",
        (_hash_pw(new_password), user_id),
    )
    c.commit()
    from app.db.audit import log_action
    log_action("user.password_change", entity="user", entity_id=user_id)


def delete_user(user_id: int) -> None:
    from app.db.audit import _insert_audit
    c = _conn()
    c.execute("DELETE FROM user WHERE id = ?", (user_id,))
    _insert_audit(c, "user.delete", entity="user", entity_id=user_id)
    c.commit()


def get_user_by_username(username: str) -> Optional[dict]:
    c = _conn()
    row = c.execute(_USER_SELECT + "WHERE u.username = ?", (username,)).fetchone()
    return dict(row) if row else None


def get_user_by_id(user_id: int) -> Optional[dict]:
    c = _conn()
    row = c.execute(_USER_SELECT + "WHERE u.id = ?", (user_id,)).fetchone()
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
