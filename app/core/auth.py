"""
app/core/auth.py
────────────────
Session helpers using NiceGUI's per-browser app.storage.user.

Role definitions
────────────────
  admin   — /users, /models, /audit, /settings
  teacher — /  (dashboard), /history, /analytics, /reports, /alerts,
            /groups, /schedules, /users, /models, /audit
            + Start/Stop monitor and the notification bell
  student — own /history + /analytics only

Roles are DISTINCT — admin does NOT get teacher-only pages (dashboard,
schedules, …) and teacher does NOT get /settings. The shared pages are
/users, /models, and /audit (admin and teacher, with different capabilities).
"""
from __future__ import annotations
from typing import Optional
from nicegui import app as nicegui_app, ui


def get_session_user() -> Optional[dict]:
    return nicegui_app.storage.user.get("auth")


def set_session_user(user: dict) -> None:
    nicegui_app.storage.user["auth"] = {
        "id":       user["id"],
        "username": user["username"],
        "role":     user["role"],
    }


def clear_session() -> None:
    nicegui_app.storage.user.pop("auth", None)


def is_admin(user: dict) -> bool:
    return user.get("role") == "admin"


def is_teacher(user: dict) -> bool:
    return user.get("role") == "teacher"


def is_teacher_or_admin(user: dict) -> bool:
    return user.get("role") in ("admin", "teacher")


def home_path_for(user: dict) -> str:
    """Landing page the given user is actually allowed to open."""
    role = user.get("role")
    if role == "admin":
        return "/users"
    if role == "teacher":
        return "/"
    return "/history"


def require_auth(required_role: Optional[str] = None) -> Optional[dict]:
    """
    Return current user or redirect.

    required_role=None       → any authenticated user
    required_role="admin"    → admin only
    required_role="teacher"  → teacher only  (NOT admin)
    required_role="teacher_or_admin" → teacher or admin

    When the role check fails, we redirect to the user's own home page
    (not a fixed "/") so the destination is always a page they can open.
    Redirecting everyone to "/" caused an infinite reload loop for students
    and admins, because "/" is teacher-only.
    """
    user = get_session_user()
    if not user:
        ui.navigate.to("/login")
        return None

    home = home_path_for(user)

    if required_role == "admin" and user["role"] != "admin":
        ui.navigate.to(home)
        return None

    if required_role == "teacher" and user["role"] != "teacher":
        ui.navigate.to(home)
        return None

    if required_role == "teacher_or_admin" and \
            user["role"] not in ("admin", "teacher"):
        ui.navigate.to(home)
        return None

    return user