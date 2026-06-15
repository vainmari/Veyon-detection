"""
tests/test_model_classes.py
───────────────────────────
Tests for reading a model's embedded class names — the authoritative
index→name order used to build the model_class mapping at import time —
and for sync_classes_from_file(), which refreshes a model's class list/
mapping from its file WITHOUT touching previously recorded detections.

This guards against the History-mislabeling bug where model_class was built
from hand-typed names instead of the model's real class order (e.g. index 2
shown as "Narsykle" live but stored as "Notepad").
"""
from __future__ import annotations
import os

import pytest


# Embedded order of the reference model (matches weights/yolo26n.onnx).
EMBEDDED = ["DI", "Ekrano nuotraukos", "Narsykle", "Notepad",
            "Paint", "PowerPoint", "Word"]
_REF_ONNX = "weights/yolo26n.onnx"


class TestReadModelClassNames:

    def test_reads_embedded_onnx_names(self):
        from app.core.yolo import read_model_class_names
        if not os.path.exists(_REF_ONNX):
            pytest.skip("reference ONNX weights not present")
        assert read_model_class_names(_REF_ONNX) == EMBEDDED

    def test_missing_file_returns_none(self):
        from app.core.yolo import read_model_class_names
        assert read_model_class_names("nope/does-not-exist.onnx") is None

    def test_non_model_file_returns_none(self, tmp_path):
        from app.core.yolo import read_model_class_names
        junk = tmp_path / "not-a-model.onnx"
        junk.write_bytes(b"not really an onnx file")
        assert read_model_class_names(str(junk)) is None


# ── sync_classes_from_file (updates model metadata only) ──────────────────────

@pytest.fixture
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


# A wrong typed order: indices 2 and 3 swapped (Narsykle ↔ Notepad).
WRONG_TYPED = ["DI", "Ekrano nuotraukos", "Notepad", "Narsykle",
               "Paint", "PowerPoint", "Word"]


class TestSyncClassesFromFile:

    def test_updates_mapping_to_embedded_order(self, db):
        if not os.path.exists(_REF_ONNX):
            pytest.skip("reference ONNX weights not present")
        mid = db.create_ml_model(
            name="yolo26n", nc=len(WRONG_TYPED),
            class_names=WRONG_TYPED, onnx_path=_REF_ONNX)
        db.sync_classes_from_model(mid)
        # Before: index 2 wrongly resolves to "Notepad".
        assert db.get_class_by_model_index(mid, 2)["name"] == "Notepad"

        res = db.sync_classes_from_file(mid)
        assert res["ok"] and res["names"] == EMBEDDED
        # After: corrected to the embedded order.
        assert db.get_class_by_model_index(mid, 2)["name"] == "Narsykle"
        assert db.get_model_by_id(mid)["class_names"] == EMBEDDED

    def test_does_not_touch_existing_detections(self, db):
        if not os.path.exists(_REF_ONNX):
            pytest.skip("reference ONNX weights not present")
        mid = db.create_ml_model(
            name="yolo26n", nc=len(WRONG_TYPED),
            class_names=WRONG_TYPED, onnx_path=_REF_ONNX)
        db.sync_classes_from_model(mid)
        cid = db.upsert_computer("PC-01", "10.0.0.1")
        dets = [{"class_id": 2, "class_name": "x", "conf": 0.9,
                 "box": [0, 0, 5, 5]}]
        eid = db.insert_event(cid, dets, model_id=mid)
        before = db._conn().execute(
            "SELECT class_id FROM detection WHERE event_id = ?", (eid,)
        ).fetchone()[0]

        db.sync_classes_from_file(mid)

        after = db._conn().execute(
            "SELECT class_id FROM detection WHERE event_id = ?", (eid,)
        ).fetchone()[0]
        assert before == after  # historical detection rows are left as-is

    def test_missing_file_is_noop(self, db):
        mid = db.create_ml_model(
            name="m", nc=2, class_names=["a", "b"],
            onnx_path="nope/missing.onnx")
        db.sync_classes_from_model(mid)
        res = db.sync_classes_from_file(mid)
        assert res["ok"] is False and res["reason"] == "file_missing"
        assert db.get_model_by_id(mid)["class_names"] == ["a", "b"]
