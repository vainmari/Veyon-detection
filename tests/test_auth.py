"""
tests/test_auth.py
──────────────────
Tests for app/core/auth.py session helpers.
NiceGUI's app.storage.user and ui.navigate.to are mocked so these tests
run without a running NiceGUI server.

Run:  pytest tests/test_auth.py -v
"""
from __future__ import annotations
from unittest.mock import MagicMock, patch


# ── Fixture: patch NiceGUI at import time ─────────────────────────────────────

def _make_storage(initial: dict | None = None):
    """Return a plain dict that behaves like app.storage.user."""
    return initial or {}


def _import_auth(storage: dict):
    """Re-import auth with a fresh NiceGUI mock each time."""
    import sys
    mock_app = MagicMock()
    mock_app.storage.user = storage
    mock_ui  = MagicMock()
    with patch.dict(sys.modules, {
        "nicegui":    MagicMock(app=mock_app, ui=mock_ui),
    }):
        if "app.core.auth" in sys.modules:
            del sys.modules["app.core.auth"]
        import app.core.auth as auth
        auth.nicegui_app = mock_app
        auth.ui          = mock_ui
    return auth, mock_app, mock_ui


# ── get_session_user ──────────────────────────────────────────────────────────

class TestGetSessionUser:
    def test_returns_none_when_empty(self):
        auth, app_, _ = _import_auth({})
        assert auth.get_session_user() is None

    def test_returns_stored_user(self):
        user = {"id": 1, "username": "admin", "role": "teacher"}
        auth, app_, _ = _import_auth({"auth": user})
        assert auth.get_session_user() == user


# ── set_session_user ──────────────────────────────────────────────────────────

class TestSetSessionUser:
    def test_stores_correct_fields(self):
        storage = {}
        auth, _, _ = _import_auth(storage)
        auth.set_session_user({"id": 5, "username": "jonas", "role": "student",
                               "extra": "should_be_dropped"})
        assert storage["auth"] == {"id": 5, "username": "jonas", "role": "student"}

    def test_overwrites_existing(self):
        storage = {"auth": {"id": 1, "username": "old", "role": "teacher"}}
        auth, _, _ = _import_auth(storage)
        auth.set_session_user({"id": 2, "username": "new", "role": "student"})
        assert storage["auth"]["username"] == "new"


# ── clear_session ─────────────────────────────────────────────────────────────

class TestClearSession:
    def test_removes_auth_key(self):
        storage = {"auth": {"id": 1, "username": "x", "role": "teacher"}}
        auth, _, _ = _import_auth(storage)
        auth.clear_session()
        assert "auth" not in storage

    def test_safe_when_already_empty(self):
        auth, _, _ = _import_auth({})
        auth.clear_session()   # should not raise


# ── require_auth ──────────────────────────────────────────────────────────────

class TestRequireAuth:
    def test_redirects_to_login_when_no_session(self):
        auth, _, mock_ui = _import_auth({})
        result = auth.require_auth()
        mock_ui.navigate.to.assert_called_once_with("/login")
        assert result is None

    def test_returns_user_when_authenticated(self):
        user = {"id": 1, "username": "admin", "role": "teacher"}
        auth, _, _ = _import_auth({"auth": user})
        assert auth.require_auth() == user

    def test_redirects_when_wrong_role(self):
        # Wrong-role users get sent to their own home page (not a fixed "/"),
        # otherwise students/admins hit an infinite reload loop on teacher-only "/".
        user = {"id": 2, "username": "jonas", "role": "student"}
        auth, _, mock_ui = _import_auth({"auth": user})
        result = auth.require_auth(required_role="teacher")
        mock_ui.navigate.to.assert_called_once_with("/history")
        assert result is None

    def test_passes_when_correct_role(self):
        user = {"id": 1, "username": "admin", "role": "teacher"}
        auth, _, _ = _import_auth({"auth": user})
        assert auth.require_auth(required_role="teacher") == user

    def test_no_role_required_accepts_any_role(self):
        for role in ("teacher", "student"):
            user = {"id": 1, "username": "x", "role": role}
            auth, _, _ = _import_auth({"auth": user})
            assert auth.require_auth() is not None