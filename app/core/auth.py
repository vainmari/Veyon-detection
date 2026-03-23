"""
app/core/auth.py
────────────────
Session helpers using NiceGUI's per-browser app.storage.user.

Role definitions
────────────────
  admin   — user management (/users) + model management (/models)
  teacher — monitoring pages: dashboard, history, analytics, alerts, settings
            + student management (create/delete students in /users)
  student — own history + analytics only

Roles are DISTINCT — admin does NOT get teacher pages and vice-versa.
The only shared page is /users (both admin and teacher, with different capabilities).
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


def require_auth(required_role: Optional[str] = None) -> Optional[dict]:
    """
    Return current user or redirect.

    required_role=None       → any authenticated user
    required_role="admin"    → admin only
    required_role="teacher"  → teacher only  (NOT admin)
    required_role="teacher_or_admin" → teacher or admin
    """
    user = get_session_user()
    if not user:
        ui.navigate.to("/login")
        return None

    if required_role == "admin" and user["role"] != "admin":
        ui.navigate.to("/")
        return None

    if required_role == "teacher" and user["role"] != "teacher":
        ui.navigate.to("/")
        return None

    if required_role == "teacher_or_admin" and \
            user["role"] not in ("admin", "teacher"):
        ui.navigate.to("/")
        return None

    return user