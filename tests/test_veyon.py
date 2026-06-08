"""
tests/test_veyon.py
───────────────────
Tests for app/core/veyon.py — all network and subprocess calls are mocked.

Run:  pytest tests/test_veyon.py -v
"""
from __future__ import annotations
import json
from unittest.mock import MagicMock, patch

import cv2
import numpy as np
import pytest

from app.core import veyon


# ── Helpers ───────────────────────────────────────────────────────────────────

def _fake_jpeg() -> bytes:
    """Return a JPEG > 1000 bytes to pass the framebuffer size guard."""
    img = np.random.randint(0, 255, (100, 100, 3), dtype=np.uint8)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 95])
    assert ok
    data = buf.tobytes()
    assert len(data) > 1000, "test JPEG too small — increase image size"
    return data


def _mock_session(status=200, content=b"", json_data=None):
    sess = MagicMock()
    resp = MagicMock()
    resp.status_code = status
    resp.content     = content
    resp.json.return_value = json_data or {}
    sess.get.return_value  = resp
    sess.post.return_value = resp
    return sess, resp


# ── is_port_open ──────────────────────────────────────────────────────────────

class TestIsPortOpen:
    def test_open_port(self):
        with patch("app.core.veyon.socket.socket") as mock_sock_cls:
            mock_sock = MagicMock()
            mock_sock.connect_ex.return_value = 0
            mock_sock_cls.return_value.__enter__.return_value = mock_sock
            assert veyon.is_port_open("localhost", 11080) is True

    def test_closed_port(self):
        with patch("app.core.veyon.socket.socket") as mock_sock_cls:
            mock_sock = MagicMock()
            mock_sock.connect_ex.return_value = 111   # ECONNREFUSED
            mock_sock_cls.return_value.__enter__.return_value = mock_sock
            assert veyon.is_port_open("localhost", 9999) is False


# ── decode_image ──────────────────────────────────────────────────────────────

class TestDecodeImage:
    def test_valid_jpeg(self):
        img = veyon.decode_image(_fake_jpeg())
        assert img is not None
        assert img.ndim == 3

    def test_invalid_bytes_returns_none(self):
        assert veyon.decode_image(b"\x00\x01\x02\x03") is None

    def test_empty_bytes_returns_none(self):
        # decode_image guards against empty input before calling cv2
        assert veyon.decode_image(b"") is None


# ── authenticate ──────────────────────────────────────────────────────────────

_KEY_CFG = {"auth_method": "key", "key_name": "class", "key_data": "KEYDATA"}


class TestAuthenticate:
    def test_returns_uid_on_success(self):
        sess, _ = _mock_session(200, json_data={"connection-uid": "abc-123"})
        uid = veyon.authenticate(sess, "http://host/api/v1", "10.0.0.1", _KEY_CFG)
        assert uid == "abc-123"

    def test_returns_none_on_non_200(self):
        sess, _ = _mock_session(401)
        assert veyon.authenticate(sess, "http://x", "h", _KEY_CFG) is None

    def test_returns_none_on_request_exception(self):
        sess = MagicMock()
        import requests
        sess.post.side_effect = requests.RequestException("timeout")
        assert veyon.authenticate(sess, "http://x", "h", _KEY_CFG) is None

    def test_correct_endpoint_called(self):
        sess, _ = _mock_session(200, json_data={"connection-uid": "x"})
        veyon.authenticate(sess, "http://host/api/v1", "10.0.0.2", _KEY_CFG)
        sess.post.assert_called_once()
        url = sess.post.call_args[0][0]
        assert "10.0.0.2" in url


# ── grab_framebuffer ──────────────────────────────────────────────────────────

class TestGrabFramebuffer:
    def test_returns_bytes_on_success(self):
        content = _fake_jpeg()
        sess, _ = _mock_session(200, content=content)
        result = veyon.grab_framebuffer(sess, "http://h/api/v1", "uid",
                                        "jpeg", 85, 480)
        assert result == content

    def test_returns_none_when_content_too_small(self):
        sess, _ = _mock_session(200, content=b"\xff" * 10)
        assert veyon.grab_framebuffer(
            sess, "http://h/api/v1", "uid", "jpeg", 85, 480) is None

    def test_returns_none_on_non_200(self):
        sess, _ = _mock_session(404)
        assert veyon.grab_framebuffer(
            sess, "http://h/api/v1", "uid", "jpeg", 85, 480) is None

    def test_returns_none_on_exception(self):
        import requests
        sess = MagicMock()
        sess.get.side_effect = requests.RequestException
        assert veyon.grab_framebuffer(
            sess, "http://h/api/v1", "uid", "jpeg", 85, 480) is None

    def test_width_zero_not_sent_as_param(self):
        sess, _ = _mock_session(200, content=_fake_jpeg())
        veyon.grab_framebuffer(sess, "http://h/api/v1", "uid", "jpeg", 85, 0)
        params = sess.get.call_args[1]["params"]
        assert "width" not in params

    def test_width_nonzero_sent_as_param(self):
        sess, _ = _mock_session(200, content=_fake_jpeg())
        veyon.grab_framebuffer(sess, "http://h/api/v1", "uid", "jpeg", 85, 640)
        params = sess.get.call_args[1]["params"]
        assert params["width"] == 640


# ── get_logged_user ───────────────────────────────────────────────────────────

class TestGetLoggedUser:
    def test_returns_login(self):
        sess, _ = _mock_session(200, json_data={"login": "LV_laptop\\Jonas"})
        result = veyon.get_logged_user(sess, "http://h/api/v1", "uid")
        assert result == "LV_laptop\\Jonas"

    def test_falls_back_to_name_field(self):
        sess, _ = _mock_session(200, json_data={"name": "Jonas"})
        assert veyon.get_logged_user(sess, "http://h/api/v1", "uid") == "Jonas"

    def test_returns_none_on_non_200(self):
        sess, _ = _mock_session(404)
        assert veyon.get_logged_user(sess, "http://h/api/v1", "uid") is None

    def test_returns_none_on_exception(self):
        import requests
        sess = MagicMock()
        sess.get.side_effect = requests.RequestException
        assert veyon.get_logged_user(sess, "http://h/api/v1", "uid") is None

    def test_returns_none_when_both_fields_empty(self):
        sess, _ = _mock_session(200, json_data={})
        assert veyon.get_logged_user(sess, "http://h/api/v1", "uid") is None


# ── discover_computers ────────────────────────────────────────────────────────

class TestDiscoverComputers:
    _OBJECTS = json.dumps([
        {"Type": 3, "Name": "PC-01", "HostAddress": "10.0.0.1"},
        {"Type": 3, "Name": "PC-02", "HostAddress": "10.0.0.2"},
        {"Type": 1, "Name": "Room A"},          # group — no HostAddress
        {"Type": 3, "Name": "NoHost"},           # type 3 but missing HostAddress
    ])

    def _run(self, stdout):
        with patch("app.core.veyon.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout=stdout, stderr="")
            return veyon.discover_computers("veyon-cli.exe")

    def test_returns_computers_only(self):
        computers = self._run(self._OBJECTS)
        assert len(computers) == 2
        assert {c["name"] for c in computers} == {"PC-01", "PC-02"}

    def test_strips_assignment_prefix(self):
        with_prefix = "NetworkObjects=" + self._OBJECTS
        computers = self._run(with_prefix)
        assert len(computers) == 2

    def test_raises_on_empty_output(self):
        with patch("app.core.veyon.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="", stderr="no output")
            with pytest.raises(RuntimeError):
                veyon.discover_computers("veyon-cli.exe")