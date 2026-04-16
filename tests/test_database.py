"""
tests/test_database.py
──────────────────────
Unit tests for app/db/database.py.
Each test gets a fresh isolated DB via the autouse `db` fixture.

Run:  pytest tests/test_database.py -v
"""
from __future__ import annotations
import sqlite3
import pytest


# ── Fixture ───────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def db(tmp_path, monkeypatch):
    import app.db.database as m
    import app.db._core as _core
    # Patch DB_PATH in _core (where _conn() reads it) and on the facade for
    # attribute access compatibility.
    monkeypatch.setattr(_core, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(m,     "DB_PATH", tmp_path / "test.db")
    # Force a fresh connection for this test's thread.
    if hasattr(_core._tls, "conn"):
        try:
            _core._tls.conn.close()
        except Exception:
            pass
        del _core._tls.conn
        del _core._tls.db_path
    m.init_db()
    m.seed_classes()
    return m


# ── Helpers ───────────────────────────────────────────────────────────────────

def _teacher(db, name="admin"):
    db.create_user(name, "pw", "teacher")
    return db.get_user_by_username(name)


def _student(db, name="jonas", created_by=None):
    db.create_user(name, "pw", "student", created_by_id=created_by)
    return db.get_user_by_username(name)


def _computer(db, name="PC-01", host="10.0.0.1"):
    return db.upsert_computer(name, host)


DETS = [
    {"class_id": 0, "class_name": "DI",      "conf": 0.92, "box": [0, 0, 10, 10]},
    {"class_id": 2, "class_name": "Narsykle", "conf": 0.85, "box": [5, 5, 50, 50]},
]

# Class names that match DETS indices 0 and 2 — used for model_class setup.
_DETS_CLASS_NAMES = ["DI", "Ekrano nuotraukos", "Narsykle"]


def _model_for_dets(db) -> int:
    """Create a model whose class list resolves DETS class indices, return model_id."""
    model_id = db.create_ml_model(
        name="test-model",
        nc=len(_DETS_CLASS_NAMES),
        class_names=_DETS_CLASS_NAMES,
    )
    db.sync_classes_from_model(model_id)
    return model_id


# ── Schema ────────────────────────────────────────────────────────────────────

class TestSchema:
    def test_all_tables_exist(self, db):
        with sqlite3.connect(db.DB_PATH) as c:
            tables = {r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
        assert {"role", "computer", "user", "detection_class",
                "detection_event", "detection"}.issubset(tables)

    def test_role_table_seeded(self, db):
        roles = {r["name"] for r in db.list_roles()}
        assert roles == {"admin", "teacher", "student"}

    def test_get_role_id_known(self, db):
        rid = db.get_role_id("teacher")
        assert isinstance(rid, int) and rid > 0

    def test_get_role_id_unknown_returns_none(self, db):
        assert db.get_role_id("superuser") is None

    def test_detection_event_has_windows_username_col(self, db):
        with sqlite3.connect(db.DB_PATH) as c:
            cols = {r[1] for r in c.execute(
                "PRAGMA table_info(detection_event)"
            )}
        assert "os_username" in cols

    def test_seed_classes_count(self, db):
        assert len(db.list_classes()) == 7

    def test_seed_idempotent(self, db):
        db.seed_classes()
        assert len(db.list_classes()) == 7

    def test_default_admin_created_with_admin_role(self, db):
        """ensure_default_teacher/admin creates an admin-role account."""
        db.ensure_default_teacher("admin", "admin")
        u = db.get_user_by_username("admin")
        assert u is not None
        assert u["role"] == "admin"

    def test_default_admin_no_duplicate(self, db):
        db.ensure_default_teacher()
        db.ensure_default_teacher()
        with sqlite3.connect(db.DB_PATH) as c:
            assert c.execute("SELECT COUNT(*) FROM user").fetchone()[0] == 1

    def test_migration_safe_on_existing_db(self, db):
        """Running init_db a second time must not crash on an existing schema."""
        db.init_db()
        assert len(db.list_classes()) == 7


# ── Users ─────────────────────────────────────────────────────────────────────

class TestUsers:
    def test_create_and_retrieve(self, db):
        uid = db.create_user("bob", "pw", "student")
        u = db.get_user_by_id(uid)
        assert u["username"] == "bob" and u["role"] == "student"

    def test_role_string_returned_from_join(self, db):
        """user["role"] must be the role name string, not an integer id."""
        db.create_user("alice", "pw", "teacher")
        u = db.get_user_by_username("alice")
        assert u["role"] == "teacher"
        assert isinstance(u["role"], str)

    def test_unknown_role_raises(self, db):
        with pytest.raises(ValueError, match="Unknown role"):
            db.create_user("x", "pw", "superuser")

    def test_duplicate_raises(self, db):
        db.create_user("alice", "pw", "student")
        with pytest.raises(Exception):
            db.create_user("alice", "pw2", "student")

    def test_verify_correct(self, db):
        db.create_user("carol", "secret", "teacher")
        assert db.verify_password("carol", "secret") is not None

    def test_verify_wrong(self, db):
        db.create_user("dave", "right", "student")
        assert db.verify_password("dave", "wrong") is None

    def test_verify_unknown(self, db):
        assert db.verify_password("ghost", "pw") is None

    def test_update_password(self, db):
        db.create_user("eve", "old", "student")
        uid = db.get_user_by_username("eve")["id"]
        db.update_password(uid, "new")
        assert db.verify_password("eve", "new")
        assert not db.verify_password("eve", "old")

    def test_delete_user(self, db):
        db.create_user("frank", "pw", "student")
        uid = db.get_user_by_username("frank")["id"]
        db.delete_user(uid)
        assert db.get_user_by_username("frank") is None

    def test_list_users_returns_role_as_string(self, db):
        db.create_user("u1", "pw", "teacher")
        db.create_user("u2", "pw", "student")
        users = {u["username"]: u for u in db.list_users()}
        assert users["u1"]["role"] == "teacher"
        assert users["u2"]["role"] == "student"

    def test_list_users_names(self, db):
        db.create_user("u1", "pw", "teacher")
        db.create_user("u2", "pw", "student")
        names = {u["username"] for u in db.list_users()}
        assert {"u1", "u2"} == names

    def test_created_by_stored(self, db):
        t = _teacher(db)
        db.create_user("pupil", "pw", "student", created_by_id=t["id"])
        u = db.get_user_by_username("pupil")
        assert u["created_by"] == t["id"]


# ── Computers ─────────────────────────────────────────────────────────────────

class TestComputers:
    def test_upsert_returns_id(self, db):
        cid = _computer(db)
        assert isinstance(cid, int)

    def test_upsert_idempotent(self, db):
        assert _computer(db) == _computer(db)

    def test_list(self, db):
        db.upsert_computer("A", "1.1.1.1")
        db.upsert_computer("B", "1.1.1.2")
        names = {c["name"] for c in db.list_computers()}
        assert {"A", "B"} == names


# ── Detection events ──────────────────────────────────────────────────────────

class TestDetectionEvents:
    def test_insert_with_detections(self, db):
        cid = _computer(db)
        db.insert_event(cid, DETS)
        rows = db.query_events(computer_id=cid)
        assert len(rows) == 1 and rows[0]["had_detection"] == 1

    def test_insert_empty_detections(self, db):
        cid = _computer(db)
        db.insert_event(cid, [])
        rows = db.query_events(computer_id=cid)
        assert len(rows) == 1 and rows[0]["had_detection"] == 0

    def test_windows_username_stored(self, db):
        cid = _computer(db)
        db.insert_event(cid, [], os_username="Jonas")
        with sqlite3.connect(db.DB_PATH) as c:
            row = c.execute(
                "SELECT os_username FROM detection_event"
            ).fetchone()
        assert row[0] == "Jonas"

    def test_windows_username_shown_when_no_account(self, db):
        cid = _computer(db)
        db.insert_event(cid, DETS, os_username="Jonas")
        rows = db.query_events(computer_id=cid)
        assert rows[0]["student"] == "Jonas"

    def test_frame_blob_round_trip(self, db):
        cid   = _computer(db)
        frame = b"\xff\xd8\xff" + b"\xaa" * 50
        eid   = db.insert_event(cid, [], frame_bytes=frame)
        b64   = db.get_event_frame_b64(eid)
        assert b64 and b64.startswith("data:image/jpeg;base64,")

    def test_no_blob_returns_none(self, db):
        cid = _computer(db)
        eid = db.insert_event(cid, [])
        assert db.get_event_frame_b64(eid) is None

    def test_user_linked_when_provided(self, db):
        cid = _computer(db)
        s   = _student(db)
        db.insert_event(cid, DETS, user_id=s["id"])
        rows = db.query_events(user_id=s["id"])
        assert len(rows) == 1 and rows[0]["student"] == "jonas"

    def test_results_ordered_desc(self, db):
        cid = _computer(db)
        for _ in range(3):
            db.insert_event(cid, [])
        times = [r["detected_at"] for r in db.query_events(computer_id=cid)]
        assert times == sorted(times, reverse=True)


# ── Query filters ─────────────────────────────────────────────────────────────

class TestQueryFilters:
    def _setup(self, db):
        c1  = db.upsert_computer("PC-01", "10.0.0.1")
        c2  = db.upsert_computer("PC-02", "10.0.0.2")
        s   = _student(db)
        mid = _model_for_dets(db)
        db.insert_event(c1, DETS, user_id=s["id"], os_username="jonas", model_id=mid)
        db.insert_event(c1, [])
        db.insert_event(c2, DETS, model_id=mid)
        return c1, c2, s

    def test_by_computer(self, db):
        c1, _, _ = self._setup(db)
        assert all(r["computer"] == "PC-01"
                   for r in db.query_events(computer_id=c1))

    def test_by_user(self, db):
        _, _, s = self._setup(db)
        rows = db.query_events(user_id=s["id"])
        assert len(rows) == 1 and rows[0]["student"] == "jonas"

    def test_only_hits(self, db):
        c1, _, _ = self._setup(db)
        rows = db.query_events(computer_id=c1, only_hits=True)
        assert len(rows) == 1 and rows[0]["had_detection"] == 1

    def test_by_class_name(self, db):
        c1, _, _ = self._setup(db)
        assert len(db.query_events(computer_id=c1, class_name="DI")) == 1

    def test_limit(self, db):
        cid = _computer(db)
        for _ in range(10):
            db.insert_event(cid, [])
        assert len(db.query_events(computer_id=cid, limit=4)) == 4


# ── Auto-assign on account creation ──────────────────────────────────────────

class TestAutoAssign:
    def test_events_assigned_on_user_creation(self, db):
        cid = _computer(db)
        for _ in range(3):
            db.insert_event(cid, DETS, os_username="Jonas")
        uid = db.create_user("Jonas", "pw", "student")
        db.auto_assign_user_events("Jonas", uid)
        rows = db.query_events(user_id=uid)
        assert len(rows) == 3

    def test_case_insensitive_match(self, db):
        cid = _computer(db)
        db.insert_event(cid, [], os_username="JONAS")
        uid = db.create_user("jonas", "pw", "student")
        db.auto_assign_user_events("jonas", uid)
        rows = db.query_events(user_id=uid)
        assert len(rows) == 1

    def test_already_assigned_not_touched(self, db):
        cid   = _computer(db)
        other = _student(db, "other")
        db.insert_event(cid, [], user_id=other["id"], os_username="other")
        db.insert_event(cid, [], os_username="Jonas")
        uid = db.create_user("Jonas", "pw", "student")
        db.auto_assign_user_events("Jonas", uid)
        assert len(db.query_events(user_id=uid)) == 1

    def test_no_matching_events_is_fine(self, db):
        uid = db.create_user("newbie", "pw", "student")
        assert isinstance(uid, int)

    def test_count_anonymous_events(self, db):
        cid = _computer(db)
        db.insert_event(cid, [])
        db.insert_event(cid, [])
        assert db.count_anonymous_events(cid) == 2

    def test_manual_assign(self, db):
        cid = _computer(db)
        s   = _student(db)
        db.insert_event(cid, [])
        db.insert_event(cid, [])
        n = db.assign_anonymous_events(s["id"], cid)
        assert n == 2 and db.count_anonymous_events(cid) == 0