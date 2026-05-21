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
           u.is_active, u.created_by, u.created_at
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
    is_active:     bool          = True,
) -> int:
    """
    Insert the user row and audit entry in a single transaction.
    Returns the new user id.

    Historical event assignment is intentionally NOT done here — call
    auto_assign_user_events(username, new_id) separately (ideally in a
    background task) so this function stays fast.
    """
    from app.db.audit import _insert_audit
    role_id = get_role_id(role)
    if role_id is None:
        raise ValueError(f"Unknown role: {role!r}")
    c = _conn()
    cur = c.execute(
        "INSERT INTO user (username, hashed_password, role_id, is_active, created_by, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (username, _hash_pw(password), role_id, 1 if is_active else 0,
         created_by_id, _now()),
    )
    new_id = cur.lastrowid
    _insert_audit(c, "user.create", entity="user", entity_id=new_id,
                  detail=f"role={role}, active={is_active}, created_by={created_by_id}")
    c.commit()
    return new_id


def auto_create_student(username: str) -> int:
    """
    Create an inactive student account for a detected OS username.
    Uses INSERT OR IGNORE so concurrent calls are safe.
    Historical event assignment is intentionally NOT done here — it is
    deferred to activate_user() so the IO worker thread never holds a
    long write lock while other threads also need to write.

    Returns the user id (new or pre-existing). Raises RuntimeError on the
    truly unexpected "row vanished between INSERT and SELECT" case rather
    than silently returning -1 — historically callers used the result as
    a foreign key and `-1` slipped into detection_event.user_id.
    """
    import secrets
    from app.db.audit import _insert_audit
    role_id = get_role_id("student")
    if role_id is None:
        raise ValueError("Role 'student' not found")
    # Random password — cannot be used to log in (is_active=0 blocks login)
    random_pw = secrets.token_hex(32)
    c = _conn()
    cur = c.execute(
        "INSERT OR IGNORE INTO user "
        "(username, hashed_password, role_id, is_active, created_at) "
        "VALUES (?, ?, ?, 0, ?)",
        (username, _hash_pw(random_pw), role_id, _now()),
    )
    if cur.rowcount > 0:
        new_id = cur.lastrowid
        _insert_audit(c, "user.auto_create", entity="user", entity_id=new_id,
                      detail="auto-created inactive student from OS login")
    c.commit()
    row = c.execute("SELECT id FROM user WHERE username = ?", (username,)).fetchone()
    if row is None:
        raise RuntimeError(
            f"auto_create_student: user {username!r} not found after "
            "INSERT OR IGNORE — DB inconsistent"
        )
    return int(row["id"])


def activate_user(user_id: int, new_password: str) -> None:
    """
    Set a real password and mark the user active so they can log in.
    Also assigns any historical anonymous events that match this user's
    username (safe here because this runs in a UI executor thread, not
    a hot-path IO worker).
    """
    from app.db.audit import _insert_audit
    c = _conn()
    row = c.execute("SELECT username FROM user WHERE id = ?", (user_id,)).fetchone()
    c.execute(
        "UPDATE user SET hashed_password = ?, is_active = 1 WHERE id = ?",
        (_hash_pw(new_password), user_id),
    )
    _insert_audit(c, "user.activate", entity="user", entity_id=user_id)
    c.commit()
    if row:
        auto_assign_user_events(row["username"], user_id)


def _batched_update(sql: str, params: tuple, batch_size: int) -> int:
    """
    Execute `sql` in a loop, committing after each pass, until a pass updates
    fewer than `batch_size` rows. Keeps the SQLite write lock held for only a
    few milliseconds at a time so concurrent IO-worker writes aren't blocked.

    `sql` must end with `LIMIT ?` and the final element of `params` must be
    `batch_size`. Returns the total number of rows updated across all passes.
    """
    c = _conn()
    total = 0
    while True:
        cur = c.execute(sql, params)
        c.commit()
        n = cur.rowcount
        total += n
        if n < batch_size:
            return total


def auto_assign_user_events(username: str, user_id: int, batch_size: int = 500) -> int:
    """
    Assign all anonymous detection_event rows whose os_username matches
    *username* (case-insensitive) to *user_id*. Returns the total updated.
    """
    return _batched_update(
        """
        UPDATE detection_event SET user_id = ?
        WHERE id IN (
            SELECT id FROM detection_event
            WHERE LOWER(os_username) = LOWER(?) AND user_id IS NULL
            LIMIT ?
        )
        """,
        (user_id, username, batch_size),
        batch_size,
    )


def nullify_user_events(user_id: int, batch_size: int = 500) -> int:
    """
    Set user_id = NULL on all detection_event rows for *user_id* before the
    user row is deleted, so the subsequent DELETE has no ON DELETE SET NULL
    cascade work to do. Returns the total cleared.
    """
    return _batched_update(
        """
        UPDATE detection_event SET user_id = NULL
        WHERE id IN (
            SELECT id FROM detection_event WHERE user_id = ? LIMIT ?
        )
        """,
        (user_id, batch_size),
        batch_size,
    )


def update_password(user_id: int, new_password: str) -> None:
    from app.db.audit import _insert_audit
    c = _conn()
    c.execute(
        "UPDATE user SET hashed_password = ? WHERE id = ?",
        (_hash_pw(new_password), user_id),
    )
    _insert_audit(c, "user.password_change", entity="user", entity_id=user_id)
    c.commit()


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
    if user and user.get("is_active") and _verify_pw(password, user["hashed_password"]):
        return user
    return None


def list_users() -> list[dict]:
    c = _conn()
    return [dict(r) for r in c.execute(
        _USER_SELECT + "ORDER BY r.name, u.username"
    ).fetchall()]
