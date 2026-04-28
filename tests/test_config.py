"""
tests/test_config.py
────────────────────
Tests for app/config.py — settings merging and collect_cfg() type casting.
NiceGUI storage is mocked so no running server is needed.

Run:  pytest tests/test_config.py -v
"""
from __future__ import annotations
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_config(stored: dict | None = None):
    """
    Re-import app.config with a fresh NiceGUI storage mock.
    `stored` is what would normally live in app.storage.general["settings"].
    """
    storage: dict = {} if stored is None else {"settings": stored}
    mock_nicegui_app = MagicMock()
    mock_nicegui_app.storage.general = storage
    with patch.dict(sys.modules, {"nicegui": MagicMock(app=mock_nicegui_app)}):
        sys.modules.pop("app.config", None)
        import app.config as cfg
        cfg.app = mock_nicegui_app   # pin the module-level binding
    return cfg


@pytest.fixture(autouse=True)
def _isolate():
    """Ensure app.config is re-imported fresh for every test."""
    yield
    sys.modules.pop("app.config", None)


# ── get_settings ──────────────────────────────────────────────────────────────

class TestGetSettings:
    def test_defaults_returned_when_nothing_stored(self):
        cfg = _make_config()
        s = cfg.get_settings()
        assert s["auth_method"] == "key"
        assert s["key_path"] == "class.pem"
        assert s["port"] == "11080"
        assert s["host"] == "localhost"

    def test_stored_value_overrides_default(self):
        cfg = _make_config({"port": "9999"})
        assert cfg.get_settings()["port"] == "9999"

    def test_partial_override_keeps_remaining_defaults(self):
        cfg = _make_config({"port": "9999"})
        s = cfg.get_settings()
        assert s["host"] == "localhost"   # untouched default
        assert s["auth_method"] == "key"  # untouched default


# ── collect_cfg — type casting ────────────────────────────────────────────────

class TestCollectCfgTypes:
    """collect_cfg() must cast setting strings to the types MonitorController expects."""

    def _collect(self, overrides: dict | None = None):
        return _make_config(overrides).collect_cfg()

    def test_numeric_fields_cast_to_correct_types(self):
        cfg = self._collect()
        assert isinstance(cfg["port"],         int)
        assert isinstance(cfg["start_wait"],   int)
        assert isinstance(cfg["interval"],     float)
        assert isinstance(cfg["img_quality"],  int)
        assert isinstance(cfg["detect_conf"],  float)
        assert isinstance(cfg["detect_iou"],   float)
        assert cfg["detect_conf"] == pytest.approx(0.40)

    def test_alert_threshold_clamped_to_minimum_one(self):
        """Zero or negative thresholds must be raised to 1 to prevent divide-by-zero."""
        assert self._collect({"alert_threshold": "0"})["alert_threshold"] == 1
        assert self._collect({"alert_threshold": "-5"})["alert_threshold"] == 1
        assert self._collect({"alert_threshold": "3"})["alert_threshold"] == 3


# ── collect_cfg — key file handling ──────────────────────────────────────────

class TestCollectCfgKeyData:
    def test_key_data_read_from_pem_file(self, tmp_path):
        key = tmp_path / "class.pem"
        key.write_text("MY_PRIVATE_KEY_CONTENT")
        cfg = _make_config({"auth_method": "key", "key_path": str(key)})
        assert cfg.collect_cfg()["key_data"] == "MY_PRIVATE_KEY_CONTENT"

    def test_key_data_stripped_of_whitespace(self, tmp_path):
        key = tmp_path / "class.pem"
        key.write_text("  KEY_DATA  \n")
        cfg = _make_config({"auth_method": "key", "key_path": str(key)})
        assert cfg.collect_cfg()["key_data"] == "KEY_DATA"

    def test_key_data_empty_for_logon_auth_even_if_file_exists(self, tmp_path):
        """Logon auth never needs the key file; key_data must stay empty."""
        key = tmp_path / "class.pem"
        key.write_text("SHOULD_NOT_BE_READ")
        cfg = _make_config({"auth_method": "logon", "key_path": str(key)})
        assert cfg.collect_cfg()["key_data"] == ""

    def test_key_data_empty_when_file_missing(self):
        cfg = _make_config({"auth_method": "key", "key_path": "/no/such/file.pem"})
        assert cfg.collect_cfg()["key_data"] == ""
