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
    Create a user account and immediately auto-assign matching anonymous events
    (os_username case-insensitive match) so past data is never lost.
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
    from app.db.audit import log_action
    log_action("user.create", entity="user", entity_id=new_id,
                detail=f"role={role}, created_by={created_by_id}")
    return new_id


def _auto_assign_by_username(username: str, user_id: int) -> int:
    """
    Assign all anonymous events whose os_username matches username
    (case-insensitive).  Returns number of rows updated.
    """
    c = _conn()
    cur = c.execute(
        "UPDATE detection_event SET user_id = ? "
        "WHERE LOWER(os_username) = LOWER(?) AND user_id IS NULL",
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
    from app.db.audit import log_action
    log_action("user.password_change", entity="user", entity_id=user_id)


def delete_user(user_id: int) -> None:
    from app.db.audit import log_action
    log_action("user.delete", entity="user", entity_id=user_id)
    c = _conn()
    c.execute("DELETE FROM user WHERE id = ?", (user_id,))
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
