"""
tests/test_monitor.py
─────────────────────
Tests for app/services/monitor_service.py.
Heavy I/O and threading are mocked; the logic under test is unit-level.

Run:  pytest tests/test_monitor.py -v
"""
from __future__ import annotations
import queue
import threading
import time
from unittest.mock import MagicMock, patch

import cv2
import numpy as np
import pytest

from app.services.monitor_service import (
    MonitorController,
    _parse_os_username as _parse_win_username,
    drain_worker,
)
import app.state as state


def _jpeg_bytes(h: int = 10, w: int = 10, fill: int = 0) -> bytes:
    """Small valid JPEG for drain_worker tests (it now expects bytes, not ndarrays)."""
    img = np.full((h, w, 3), fill, dtype=np.uint8)
    ok, buf = cv2.imencode(".jpg", img)
    assert ok
    return buf.tobytes()


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def reset_state():
    """Restore global state after every test."""
    yield
    state.log_buffer.clear()
    state.latest_frames.clear()
    state.computer_ids.clear()
    state.computer_users.clear()
    state.computer_os_usernames.clear()
    # drain log_q and img_q
    for q in (state.log_q, state.img_q):
        while not q.empty():
            try: q.get_nowait()
            except queue.Empty: break


@pytest.fixture()
def cfg():
    return {
        "key_name": "class", "key_path": "dummy.pem",
        "veyon_cli": "veyon-cli.exe", "host": "localhost", "port": 11080,
        "auto_start": False, "start_wait": 5, "interval": 0.1,
        "img_fmt": "jpeg", "img_quality": 85, "img_width": 480,
        "model_path": "weights/model.onnx",
        "detect_conf": 0.4, "detect_iou": 0.2, "detect_imgsz": 480,
        "keep_top1": True,
    }


# ── _parse_win_username ───────────────────────────────────────────────────────

class TestParseWinUsername:
    def test_strips_computer_prefix(self):
        assert _parse_win_username("JP_laptop\\Jonas") == "Jonas"

    def test_no_prefix_unchanged(self):
        assert _parse_win_username("Jonas") == "Jonas"

    def test_strips_whitespace(self):
        assert _parse_win_username("PC\\  Jonas  ") == "Jonas"

    def test_multiple_backslashes_keeps_last(self):
        assert _parse_win_username("DOMAIN\\PC\\Jonas") == "Jonas"

    def test_empty_string(self):
        assert _parse_win_username("") == ""


# ── drain_worker ──────────────────────────────────────────────────────────────

class TestDrainWorker:
    """Run drain_worker briefly in a thread and check state changes."""

    def _run_briefly(self, seconds=0.15):
        t = threading.Thread(target=drain_worker, daemon=True)
        t.start()
        time.sleep(seconds)
        # daemon thread stops automatically on test teardown

    def test_drains_log_messages(self):
        state.log_q.put("[10:00:00] Hello")
        state.log_q.put("[10:00:01] World")
        self._run_briefly()
        assert "[10:00:00] Hello" in state.log_buffer
        assert "[10:00:01] World" in state.log_buffer

    def test_caps_log_buffer(self):
        for i in range(state.LOG_CAP + 20):
            state.log_q.put(f"msg {i}")
        self._run_briefly(0.2)
        assert len(state.log_buffer) <= state.LOG_CAP

    def test_drains_image_queue(self):
        # img_q now carries pre-encoded JPEG bytes, not BGR ndarrays
        jpg = _jpeg_bytes()
        state.img_q.put(("PC-01", jpg, jpg, []))
        self._run_briefly()
        assert "PC-01" in state.latest_frames

    def test_latest_frame_overwritten_by_newer(self):
        jpg1 = _jpeg_bytes(fill=0)
        jpg2 = _jpeg_bytes(fill=255)
        state.img_q.put(("PC-01", jpg1, jpg1, [{"class_name": "old"}]))
        state.img_q.put(("PC-01", jpg2, jpg2, [{"class_name": "new"}]))
        self._run_briefly()
        _, _, dets = state.latest_frames["PC-01"]
        assert dets[0]["class_name"] == "new"

    def test_multiple_computers_tracked(self):
        jpg = _jpeg_bytes()
        state.img_q.put(("PC-01", jpg, jpg, []))
        state.img_q.put(("PC-02", jpg, jpg, []))
        self._run_briefly()
        assert "PC-01" in state.latest_frames
        assert "PC-02" in state.latest_frames


# ── MonitorController ─────────────────────────────────────────────────────────

class TestMonitorController:
    def test_stop_sets_event(self, cfg):
        mc = MonitorController(cfg)
        mc._stop.clear()
        mc.stop()
        assert mc._stop.is_set()

    def test_stop_safe_when_not_started(self, cfg):
        mc = MonitorController(cfg)
        mc.stop()   # should not raise

    def test_stop_terminates_proc(self, cfg):
        mc = MonitorController(cfg)
        mock_proc = MagicMock()
        mc._proc = mock_proc
        mc.stop()
        mock_proc.terminate.assert_called_once()
        assert mc._proc is None

    def test_start_launches_thread(self, cfg):
        mc = MonitorController(cfg)
        with patch.object(mc, "_run") as mock_run:
            mock_run.return_value = None
            mc.start()
            time.sleep(0.05)
            mock_run.assert_called_once()

    def test_log_pushes_to_log_q(self, cfg):
        mc = MonitorController(cfg)
        mc._log("test message")
        msg = state.log_q.get_nowait()
        assert "test message" in msg

    def test_run_fails_gracefully_on_missing_key_file(self, cfg):
        cfg["key_path"] = "/nonexistent/path/key.pem"
        mc = MonitorController(cfg)
        mc._run()   # should not raise; logs the error instead
        logged = []
        while not state.log_q.empty():
            logged.append(state.log_q.get_nowait())
        assert any("Cannot read key file" in m or "❌" in m for m in logged)

    def test_run_fails_gracefully_on_bad_model(self, cfg, tmp_path):
        key = tmp_path / "key.pem"
        key.write_text("FAKE_KEY")
        cfg["key_path"] = str(key)
        cfg["auto_start"] = False
        mc = MonitorController(cfg)
        with patch("app.services.monitor_service.veyon.is_port_open", return_value=True), \
             patch("app.services.monitor_service.yolo.get_model",
                   side_effect=Exception("model not found")):
            mc._run()
        logged = []
        while not state.log_q.empty():
            logged.append(state.log_q.get_nowait())
        assert any("Model load failed" in m or "❌" in m for m in logged)


# ── Integration: detect worker writes to DB ───────────────────────────────────

class TestDetectWorkerDBIntegration:
    """
    Simulate the detect worker's DB write path without real YOLO inference.
    Verifies that insert_event is called with the correct computer_id,
    user_id, and windows_username.
    """

    def _setup_db(self, monkeypatch, tmp_path, name="test.db"):
        import app.db.database as db_module
        import app.db._core as _core
        monkeypatch.setattr(_core,      "DB_PATH", tmp_path / name)
        monkeypatch.setattr(db_module,  "DB_PATH", tmp_path / name)
        if hasattr(_core._tls, "conn"):
            try:
                _core._tls.conn.close()
            except Exception:
                pass
            del _core._tls.conn
            del _core._tls.db_path
        db_module.init_db()
        db_module.seed_classes()
        return db_module

    def test_event_inserted_with_windows_username(self, cfg, tmp_path, monkeypatch):
        db_module = self._setup_db(monkeypatch, tmp_path)

        cid = db_module.upsert_computer("PC-01", "10.0.0.1")
        state.computer_ids["PC-01"]          = cid
        state.computer_users["PC-01"]        = None
        state.computer_os_usernames["PC-01"] = "Jonas"

        db_module.insert_event(
            cid, [],
            user_id=None,
            os_username=state.computer_os_usernames["PC-01"],
        )
        rows = db_module.query_events(computer_id=cid)
        assert len(rows) == 1
        assert rows[0]["student"] == "Jonas"

    def test_auto_assign_after_user_creation(self, cfg, tmp_path, monkeypatch):
        db_module = self._setup_db(monkeypatch, tmp_path, "test2.db")

        cid = db_module.upsert_computer("PC-01", "10.0.0.1")
        # Log 2 events before account exists
        db_module.insert_event(cid, [], os_username="Jonas")
        db_module.insert_event(cid, [], os_username="Jonas")
        # Create account and explicitly auto-assign historical events
        uid = db_module.create_user("Jonas", "pw", "student")
        db_module.auto_assign_user_events("Jonas", uid)
        rows = db_module.query_events(user_id=uid)
        assert len(rows) == 2