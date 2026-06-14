"""
tests/test_runs.py
──────────────────
Unit tests for app/db/runs.py — monitoring-run lifecycle and report queries.
Each test gets a fresh isolated DB via the autouse `db` fixture.

Run:  pytest tests/test_runs.py -v
"""
from __future__ import annotations
import pytest


# ── Fixture (same pattern as tests/test_database.py) ──────────────────────────

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


DETS = [
    {"class_id": 0, "class_name": "DI",       "conf": 0.92, "box": [0, 0, 10, 10]},
    {"class_id": 2, "class_name": "Narsykle", "conf": 0.85, "box": [5, 5, 50, 50]},
]

_DETS_CLASS_NAMES = ["DI", "Ekrano nuotraukos", "Narsykle"]


def _model_for_dets(db) -> int:
    model_id = db.create_ml_model(
        name="test-model",
        nc=len(_DETS_CLASS_NAMES),
        class_names=_DETS_CLASS_NAMES,
    )
    db.sync_classes_from_model(model_id)
    return model_id


def _run_with_events(db, n_hits=2, n_empty=1):
    """Create computer + model + run and write events tagged with the run."""
    cid = db.upsert_computer("PC-01", "10.0.0.1")
    mid = _model_for_dets(db)
    rid = db.create_run(trigger_type="manual", group_name="Lab 1")
    db.set_run_model(rid, mid)
    for _ in range(n_hits):
        db.insert_event(cid, DETS, os_username="jonas", model_id=mid, run_id=rid)
    for _ in range(n_empty):
        db.insert_event(cid, [], os_username="jonas", model_id=mid, run_id=rid)
    return rid, cid, mid


# ── Lifecycle ─────────────────────────────────────────────────────────────────

class TestRunLifecycle:

    def test_create_and_get(self, db):
        rid = db.create_run(trigger_type="manual", group_name="Lab 1")
        run = db.get_run(rid)
        assert run["status"] == "running"
        assert run["trigger_type"] == "manual"
        assert run["group_name"] == "Lab 1"
        assert run["started_at"]
        assert run["ended_at"] is None

    def test_finish_sets_ended_at(self, db):
        rid = db.create_run()
        db.finish_run(rid)
        run = db.get_run(rid)
        assert run["status"] == "finished"
        assert run["ended_at"] is not None

    def test_finish_is_idempotent(self, db):
        rid = db.create_run()
        db.finish_run(rid)
        first_end = db.get_run(rid)["ended_at"]
        db.finish_run(rid, status="interrupted")  # must not re-finish
        run = db.get_run(rid)
        assert run["status"] == "finished"
        assert run["ended_at"] == first_end

    def test_finish_stale_runs(self, db):
        rid_stale    = db.create_run()
        rid_finished = db.create_run()
        db.finish_run(rid_finished)
        n = db.finish_stale_runs()
        assert n == 1
        assert db.get_run(rid_stale)["status"] == "interrupted"
        assert db.get_run(rid_stale)["ended_at"] is not None
        assert db.get_run(rid_finished)["status"] == "finished"

    def test_stale_run_ended_at_uses_last_event(self, db):
        rid, cid, mid = _run_with_events(db)
        last_event = db._conn().execute(
            "SELECT MAX(detected_at) FROM detection_event WHERE run_id = ?",
            (rid,),
        ).fetchone()[0]
        db.finish_stale_runs()
        assert db.get_run(rid)["ended_at"] == last_event

    def test_set_run_model(self, db):
        mid = _model_for_dets(db)
        rid = db.create_run()
        db.set_run_model(rid, mid)
        assert db.get_run(rid)["model_id"] == mid
        assert db.get_run(rid)["model_name"] == "test-model"

    def test_schedule_trigger_metadata(self, db):
        gid = db.create_group("Lab 1")
        sid = db.create_schedule(
            group_id=gid, name="Morning exam",
            days_of_week="0,1", start_time="08:00", end_time="10:00",
        )
        rid = db.create_run(trigger_type="schedule", schedule_id=sid)
        run = db.get_run(rid)
        assert run["trigger_type"] == "schedule"
        assert run["schedule_name"] == "Morning exam"

    def test_run_short_label_manual_running(self, db):
        rid = db.create_run(trigger_type="manual")
        run = db.get_run(rid)
        date = run["started_at"][:10]
        start = run["started_at"][11:16]
        # Running session → end shown as "…".
        assert db.run_short_label(run, "Run") == f"Run | {date} {start}–…"

    def test_run_short_label_finished_shows_window(self, db):
        rid = db.create_run(trigger_type="manual")
        db.finish_run(rid)
        run = db.get_run(rid)
        label = db.run_short_label(run, "Run")
        assert label.startswith(f"Run | {run['started_at'][:10]} ")
        assert label.endswith(run["ended_at"][11:16])
        assert "#" not in label  # no bare sequence number anymore

    def test_run_short_label_schedule_uses_name(self, db):
        gid = db.create_group("Lab 1")
        sid = db.create_schedule(
            group_id=gid, name="Morning exam",
            days_of_week="0,1", start_time="08:00", end_time="10:00",
        )
        rid = db.create_run(trigger_type="schedule", schedule_id=sid)
        label = db.run_short_label(db.get_run(rid), "Run")
        assert label.startswith("Morning exam ")
        assert db.get_run(rid)["started_at"][:10] in label

    def test_run_short_label_schedule_deleted_falls_back(self, db):
        # schedule_id present but the schedule (name) is gone → manual name.
        rid = db.create_run(trigger_type="schedule", schedule_id=None)
        assert db.run_short_label(db.get_run(rid), "Run").startswith("Run ")


# ── Aggregates / report queries ───────────────────────────────────────────────

class TestRunReports:

    def test_list_runs_counts(self, db):
        rid, cid, mid = _run_with_events(db, n_hits=2, n_empty=1)
        # An event OUTSIDE the run must not leak into its counts.
        db.insert_event(cid, DETS, model_id=mid)
        runs = db.list_runs()
        assert len(runs) == 1
        r = runs[0]
        assert r["id"] == rid
        assert r["total_events"] == 3
        assert r["detection_events"] == 2
        assert r["computer_count"] == 1
        assert r["student_count"] == 1

    def test_class_summary(self, db):
        rid, *_ = _run_with_events(db, n_hits=2, n_empty=1)
        rows = db.get_run_class_summary(rid)
        by_name = {r["name"]: r for r in rows}
        assert by_name["DI"]["cnt"] == 2
        assert by_name["Narsykle"]["cnt"] == 2
        assert by_name["DI"]["avg_conf"] == pytest.approx(0.92)
        # 2 DI + 2 Narsykle boxes → each class is 50% of all detections.
        assert by_name["DI"]["pct"] == pytest.approx(50.0)
        assert by_name["Narsykle"]["pct"] == pytest.approx(50.0)

    def test_class_summary_share_sums_to_100(self, db):
        rid, *_ = _run_with_events(db, n_hits=3, n_empty=1)
        rows = db.get_run_class_summary(rid)
        assert sum(r["pct"] for r in rows) == pytest.approx(100.0)

    def test_student_summary(self, db):
        rid, *_ = _run_with_events(db, n_hits=2, n_empty=1)
        rows = db.get_run_student_summary(rid)
        assert len(rows) == 1
        s = rows[0]
        assert s["student"] == "jonas"
        assert s["frames"] == 3
        assert s["hits"] == 2
        assert "DI" in s["classes"] and "Narsykle" in s["classes"]

    def test_computer_summary(self, db):
        rid, *_ = _run_with_events(db, n_hits=2, n_empty=1)
        rows = db.get_run_computer_summary(rid)
        assert len(rows) == 1
        assert rows[0]["computer"] == "PC-01"
        assert rows[0]["frames"] == 3
        assert rows[0]["hits"] == 2

    def test_run_detections_for_csv(self, db):
        rid, *_ = _run_with_events(db, n_hits=2, n_empty=1)
        dets = db.get_run_detections(rid)
        assert len(dets) == 4  # 2 events × 2 boxes
        d = dets[0]
        assert {"detected_at", "computer", "student", "class_name",
                "confidence", "box_x1", "box_y1", "box_x2", "box_y2"} <= set(d)

    def test_empty_run_reports(self, db):
        rid = db.create_run()
        assert db.get_run_class_summary(rid) == []
        assert db.get_run_student_summary(rid) == []
        assert db.get_run_computer_summary(rid) == []
        assert db.get_run_detections(rid) == []
        r = db.get_run(rid)
        assert r["total_events"] == 0
        assert r["student_count"] == 0


def _run_with_alerts(db):
    """Run with two alerted events (DI class) and one clean event."""
    rid, cid, mid = _run_with_events(db, n_hits=2, n_empty=1)
    c = db._conn()
    di_class_id = c.execute(
        "SELECT id FROM detection_class WHERE name = 'DI'").fetchone()[0]
    event_ids = [r[0] for r in c.execute(
        "SELECT id FROM detection_event WHERE run_id = ? AND had_detection = 1",
        (rid,),
    ).fetchall()]
    for eid in event_ids:
        db.insert_notification(eid, di_class_id)
    return rid, event_ids


class TestRunAlerts:

    def test_alerts_resolved_with_metadata(self, db):
        rid, event_ids = _run_with_alerts(db)
        alerts = db.get_run_alerts(rid)
        assert len(alerts) == 2
        a = alerts[0]
        assert a["class_name"] == "DI"
        assert a["computer"] == "PC-01"
        assert a["student"] == "jonas"
        assert a["event_id"] in event_ids
        assert a["created_at"]

    def test_alerts_has_frame_flag(self, db):
        rid, event_ids = _run_with_alerts(db)
        # Events were inserted without frame bytes → no screenshot available.
        assert all(not a["has_frame"] for a in db.get_run_alerts(rid))
        c = db._conn()
        c.execute("UPDATE detection_event SET frame_blob = ? WHERE id = ?",
                  (b"\xff\xd8jpegdata", event_ids[0]))
        c.commit()
        flags = {a["event_id"]: a["has_frame"] for a in db.get_run_alerts(rid)}
        assert flags[event_ids[0]] == 1

    def test_alerts_scoped_to_run(self, db):
        rid, _ = _run_with_alerts(db)
        cid = db.upsert_computer("PC-02", "10.0.0.2")
        c = db._conn()
        di = c.execute("SELECT id FROM detection_class WHERE name='DI'").fetchone()[0]
        eid = db.insert_event(cid, [])          # event outside the run
        db.insert_notification(eid, di)
        assert len(db.get_run_alerts(rid)) == 2

    def test_alert_count_in_run_listing(self, db):
        rid, _ = _run_with_alerts(db)
        assert db.get_run(rid)["alert_count"] == 2


# ── PDF export ────────────────────────────────────────────────────────────────

_PDF_LABELS = {
    "title": "{label} — detection report", "run_word": "Run",
    "generated": "Generated {ts}",
    "period": "Period", "trigger": "Trigger", "group": "Group",
    "model": "Model", "status": "Status", "started_by": "Started by",
    "all_computers": "All computers", "trigger_manual": "Manual",
    "trigger_schedule": "Schedule: {name}", "status_running": "Running",
    "status_finished": "Finished", "status_interrupted": "Interrupted",
    "summary": "Summary", "sum_events": "Frames analyzed",
    "sum_hits": "Frames with detections", "sum_alerts": "Alerts fired",
    "sum_students": "Students", "computers": "Computers",
    "alerts_section": "Alerts — prohibited classes", "time": "Time",
    "class": "Class", "computer": "Computer", "student": "Studentas ąčęėįšųū",
    "by_class": "Detections by class", "count": "Count",
    "avg_conf": "Avg conf.", "share": "% of detections",
    "shots_section": "Alert screenshots",
    "shot_none": "No screenshots stored.",
    "by_student": "Detections by student", "frames": "Frames",
    "detections": "Detections", "classes": "Detected classes",
    "by_computer": "Detections by computer",
}


class TestPdfExport:

    def test_pdf_generated_for_run_with_data(self, db):
        from app.services.report_export import build_run_pdf
        rid, _ = _run_with_alerts(db)
        db.finish_run(rid)
        pdf = build_run_pdf(rid, _PDF_LABELS)
        assert pdf is not None
        assert pdf.startswith(b"%PDF")
        assert len(pdf) > 1000

    def test_pdf_for_empty_run(self, db):
        from app.services.report_export import build_run_pdf
        rid = db.create_run()
        pdf = build_run_pdf(rid, _PDF_LABELS)
        assert pdf is not None and pdf.startswith(b"%PDF")

    def test_pdf_none_for_missing_run(self, db):
        from app.services.report_export import build_run_pdf
        assert build_run_pdf(99999, _PDF_LABELS) is None

    def _pdf_page_count(self, pdf: bytes) -> int:
        # fpdf2 emits one `/Type /Page` object per page (and a single
        # `/Type /Pages` tree node, which this pattern excludes).
        import re
        return len(re.findall(rb"/Type\s*/Page(?![s])", pdf))

    def test_pdf_includes_screenshot_pages(self, db):
        """Alerts with a stored frame add screenshot page(s) after page 1."""
        import io as _io
        from PIL import Image
        from app.services.report_export import build_run_pdf

        # A valid 64×48 JPEG so fpdf2/Pillow can embed it.
        jpg = _io.BytesIO()
        Image.new("RGB", (64, 48), (200, 30, 30)).save(jpg, format="JPEG")
        frame = jpg.getvalue()

        cid = db.upsert_computer("PC-01", "10.0.0.1")
        mid = _model_for_dets(db)
        rid = db.create_run(trigger_type="manual", group_name="Lab 1")
        db.set_run_model(rid, mid)
        di = db._conn().execute(
            "SELECT id FROM detection_class WHERE name='DI'").fetchone()[0]
        eid = db.insert_event(cid, DETS, os_username="jonas",
                              model_id=mid, run_id=rid, frame_bytes=frame)
        db.insert_notification(eid, di)
        db.finish_run(rid)

        pdf = build_run_pdf(rid, _PDF_LABELS)
        assert self._pdf_page_count(pdf) >= 2  # page 1 tables + screenshot page

    def test_pdf_no_embedded_jpeg_without_frames(self, db):
        """Alerts without stored frames embed no JPEG (no /DCTDecode stream)."""
        from app.services.report_export import build_run_pdf
        rid, _ = _run_with_alerts(db)   # alerts exist but no frame bytes
        db.finish_run(rid)
        pdf = build_run_pdf(rid, _PDF_LABELS)
        assert b"/DCTDecode" not in pdf


# ── Migration ─────────────────────────────────────────────────────────────────

class TestMigration:

    def test_run_id_column_added_to_legacy_db(self, db):
        """A pre-v1.2 DB (no run_id column) gains it via _migrate()."""
        c = db._conn()
        # Simulate a legacy DB: drop the index (SQLite refuses to drop an
        # indexed column) and the column, then re-run init_db().
        c.execute("DROP INDEX idx_event_run")
        c.execute("ALTER TABLE detection_event DROP COLUMN run_id")
        c.commit()
        db.init_db()
        cols = {r[1] for r in c.execute("PRAGMA table_info(detection_event)")}
        assert "run_id" in cols
        idx = {r[1] for r in c.execute("PRAGMA index_list(detection_event)")}
        assert "idx_event_run" in idx
