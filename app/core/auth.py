"""
app/core/auth.py
────────────────
Session helpers using NiceGUI's per-browser app.storage.user.
Stored value: {"id": int, "username": str, "role": "teacher"|"student"}
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


def require_auth(required_role: Optional[str] = None) -> Optional[dict]:
    """
    Call at the top of every protected page.
    Returns current user dict, or None after triggering a redirect.
    """
    user = get_session_user()
    if not user:
        ui.navigate.to("/login")
        return None
    if required_role and user["role"] != required_role:
        ui.navigate.to("/")
        return None
    return user