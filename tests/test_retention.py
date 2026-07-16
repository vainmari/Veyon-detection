"""
tests/test_retention.py
───────────────────────
Tests for app/db/retention.py (purge_old_events) and the retention_service
tick logic. Uses the same isolated-DB fixture pattern as test_database.py.

Run:  pytest tests/test_retention.py -v
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest


# ── Fixture (mirrors test_database.py) ────────────────────────────────────────

@pytest.fixture(autouse=True)
def db(tmp_path, monkeypatch):
    import app.db.database as m
    import app.db._core as _core
    monkeypatch.setattr(_core, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(m,     "DB_PATH", tmp_path / "test.db")
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

def _event_aged(db, computer_id: int, age_days: float, **kwargs) -> int:
    """Insert an event, then backdate its detected_at by `age_days`."""
    from app.db._core import _conn
    ev_id = db.insert_event(computer_id, [], **kwargs)
    stamp = (datetime.now() - timedelta(days=age_days)).strftime("%Y-%m-%d %H:%M:%S")
    c = _conn()
    c.execute("UPDATE detection_event SET detected_at = ? WHERE id = ?", (stamp, ev_id))
    c.commit()
    return ev_id


def _count(db, table: str) -> int:
    from app.db._core import _conn
    return _conn().execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


# ── purge_old_events ──────────────────────────────────────────────────────────

class TestPurgeOldEvents:
    def test_disabled_when_days_zero_or_negative(self, db):
        cid = db.upsert_computer("PC-01", "10.0.0.1")
        _event_aged(db, cid, age_days=400)
        assert db.purge_old_events(0) == 0
        assert db.purge_old_events(-5) == 0
        assert _count(db, "detection_event") == 1

    def test_old_deleted_recent_kept(self, db):
        cid = db.upsert_computer("PC-01", "10.0.0.1")
        old_id    = _event_aged(db, cid, age_days=31)
        recent_id = _event_aged(db, cid, age_days=1)
        assert db.purge_old_events(30) == 1
        from app.db._core import _conn
        remaining = {r[0] for r in _conn().execute("SELECT id FROM detection_event")}
        assert remaining == {recent_id}
        assert old_id not in remaining

    def test_boundary_event_inside_window_survives(self, db):
        """An event slightly newer than the cutoff must not be deleted."""
        cid = db.upsert_computer("PC-01", "10.0.0.1")
        _event_aged(db, cid, age_days=29.5)
        assert db.purge_old_events(30) == 0

    def test_detections_and_notifications_cascade(self, db):
        from app.db._core import _conn
        cid = db.upsert_computer("PC-01", "10.0.0.1")
        model_id = db.create_ml_model(name="m", nc=1, class_names=["DI"])
        db.sync_classes_from_model(model_id)
        ev_id = db.insert_event(
            cid,
            [{"class_id": 0, "class_name": "DI", "conf": 0.9, "box": [0, 0, 5, 5]}],
            model_id=model_id,
        )
        c = _conn()
        cls_id = c.execute("SELECT id FROM detection_class WHERE name='DI'").fetchone()[0]
        db.insert_notification(ev_id, cls_id)
        stamp = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d %H:%M:%S")
        c.execute("UPDATE detection_event SET detected_at = ? WHERE id = ?", (stamp, ev_id))
        c.commit()
        assert _count(db, "detection") == 1
        assert _count(db, "notification") == 1

        assert db.purge_old_events(30) == 1
        assert _count(db, "detection_event") == 0
        assert _count(db, "detection") == 0
        assert _count(db, "notification") == 0

    def test_monitoring_runs_survive_purge(self, db):
        cid = db.upsert_computer("PC-01", "10.0.0.1")
        run_id = db.create_run(trigger_type="manual", group_name="")
        _event_aged(db, cid, age_days=60, run_id=run_id)
        db.finish_run(run_id)
        assert db.purge_old_events(30) == 1
        assert _count(db, "monitoring_run") == 1  # session metadata kept

    def test_batching_deletes_everything(self, db):
        cid = db.upsert_computer("PC-01", "10.0.0.1")
        for _ in range(7):
            _event_aged(db, cid, age_days=45)
        assert db.purge_old_events(30, batch_size=3) == 7
        assert _count(db, "detection_event") == 0

    def test_audit_entry_written_only_when_something_deleted(self, db):
        cid = db.upsert_computer("PC-01", "10.0.0.1")
        db.purge_old_events(30)  # nothing to delete
        assert not any(
            a["action"] == "system.retention_purge" for a in db.list_audit_log())
        _event_aged(db, cid, age_days=60)
        db.purge_old_events(30)
        entries = [a for a in db.list_audit_log()
                   if a["action"] == "system.retention_purge"]
        assert len(entries) == 1
        assert "1 event(s)" in entries[0]["detail"]


# ── retention_service._tick ───────────────────────────────────────────────────

class TestRetentionTick:
    """_tick reads retention_days from settings and calls the purge."""

    def _tick_with_setting(self, monkeypatch, value):
        import app.services.retention_service as svc
        import app.config as cfg
        calls: list[int] = []
        monkeypatch.setattr(cfg, "get_settings", lambda: {"retention_days": value})
        import app.db.retention as ret
        monkeypatch.setattr(ret, "purge_old_events",
                            lambda days: calls.append(days) or 0)
        svc._tick()
        return calls

    def test_zero_disables_purge(self, db, monkeypatch):
        assert self._tick_with_setting(monkeypatch, "0") == []

    def test_days_passed_through(self, db, monkeypatch):
        assert self._tick_with_setting(monkeypatch, "14") == [14]

    def test_garbage_value_skipped(self, db, monkeypatch):
        assert self._tick_with_setting(monkeypatch, "forever") == []
