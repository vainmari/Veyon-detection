"""
tests/test_tls.py
─────────────────
Tests for app/core/tls.py (self-signed cert generation) and the
TLS_ENABLED / get_ssl_kwargs() wiring in app/config.py.

Run:  pytest tests/test_tls.py -v
"""
from __future__ import annotations

import datetime
import sys
from unittest.mock import MagicMock, patch

import pytest
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import load_pem_private_key

from app.core.tls import ensure_self_signed_cert, generate_self_signed_cert


# ── Certificate generation ────────────────────────────────────────────────────

class TestGenerateSelfSignedCert:
    def test_creates_cert_and_key_files(self, tmp_path):
        cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
        generate_self_signed_cert(cert, key)
        assert cert.exists() and key.exists()

    def test_key_is_valid_unencrypted_rsa(self, tmp_path):
        cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
        generate_self_signed_cert(cert, key)
        parsed = load_pem_private_key(key.read_bytes(), password=None)
        assert isinstance(parsed, rsa.RSAPrivateKey)
        assert parsed.key_size == 2048

    def test_cert_is_self_signed_with_localhost_san(self, tmp_path):
        cert_p, key_p = tmp_path / "cert.pem", tmp_path / "key.pem"
        generate_self_signed_cert(cert_p, key_p)
        cert = x509.load_pem_x509_certificate(cert_p.read_bytes())
        assert cert.issuer == cert.subject  # self-signed
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        assert "localhost" in san.value.get_values_for_type(x509.DNSName)

    def test_cert_validity_window_covers_now(self, tmp_path):
        cert_p, key_p = tmp_path / "cert.pem", tmp_path / "key.pem"
        generate_self_signed_cert(cert_p, key_p)
        cert = x509.load_pem_x509_certificate(cert_p.read_bytes())
        now = datetime.datetime.now(datetime.timezone.utc)
        assert cert.not_valid_before_utc < now < cert.not_valid_after_utc

    def test_creates_missing_parent_directories(self, tmp_path):
        cert = tmp_path / "deep" / "nested" / "cert.pem"
        key  = tmp_path / "deep" / "nested" / "key.pem"
        generate_self_signed_cert(cert, key)
        assert cert.exists() and key.exists()


# ── ensure_self_signed_cert ───────────────────────────────────────────────────

class TestEnsureSelfSignedCert:
    def test_generates_when_files_missing(self, tmp_path):
        cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
        ensure_self_signed_cert(cert, key)
        assert cert.exists() and key.exists()

    def test_keeps_existing_valid_pair_untouched(self, tmp_path):
        """An institution-provided (or previously generated) cert must survive."""
        cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
        generate_self_signed_cert(cert, key)
        before = (cert.read_bytes(), key.read_bytes())
        ensure_self_signed_cert(cert, key)
        assert (cert.read_bytes(), key.read_bytes()) == before

    def test_regenerates_unparsable_cert(self, tmp_path):
        cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
        cert.write_text("not a certificate")
        key.write_text("not a key")
        ensure_self_signed_cert(cert, key)
        # Both replaced with a working pair
        x509.load_pem_x509_certificate(cert.read_bytes())
        load_pem_private_key(key.read_bytes(), password=None)

    def test_regenerates_when_key_missing(self, tmp_path):
        """Cert without its key is unusable — the pair must be regenerated."""
        cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
        generate_self_signed_cert(cert, key)
        key.unlink()
        ensure_self_signed_cert(cert, key)
        assert key.exists()


# ── config wiring — get_ssl_kwargs ────────────────────────────────────────────

def _make_config(monkeypatch, env: dict[str, str]):
    """
    Re-import app.config with mocked NiceGUI and exactly the given env vars.
    dotenv is mocked out so the developer's real .env (which may legitimately
    set TLS_ENABLED=true) can't leak into the test, and the TLS vars are
    cleared first so each test starts from a clean slate.
    """
    monkeypatch.setenv("STORAGE_SECRET", "test-secret-for-tls-tests")
    for k in ("TLS_ENABLED", "TLS_CERTFILE", "TLS_KEYFILE"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    mock_nicegui_app = MagicMock()
    mock_nicegui_app.storage.general = {}
    with patch.dict(sys.modules, {
        "nicegui": MagicMock(app=mock_nicegui_app),
        "dotenv":  MagicMock(),  # load_dotenv becomes a no-op
    }):
        sys.modules.pop("app.config", None)
        import app.config as cfg
    return cfg


@pytest.fixture(autouse=True)
def _isolate():
    yield
    sys.modules.pop("app.config", None)


class TestGetSslKwargs:
    def test_disabled_by_default(self, monkeypatch):
        cfg = _make_config(monkeypatch, {})
        assert cfg.get_ssl_kwargs() == {}

    def test_explicit_false_returns_empty(self, monkeypatch):
        cfg = _make_config(monkeypatch, {"TLS_ENABLED": "false"})
        assert cfg.get_ssl_kwargs() == {}

    def test_enabled_generates_cert_and_returns_paths(self, monkeypatch, tmp_path):
        cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
        cfg = _make_config(monkeypatch, {
            "TLS_ENABLED":  "true",
            "TLS_CERTFILE": str(cert),
            "TLS_KEYFILE":  str(key),
        })
        kwargs = cfg.get_ssl_kwargs()
        assert kwargs == {"ssl_certfile": str(cert), "ssl_keyfile": str(key)}
        assert cert.exists() and key.exists()

    def test_enabled_reuses_existing_cert(self, monkeypatch, tmp_path):
        cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
        generate_self_signed_cert(cert, key)
        before = cert.read_bytes()
        cfg = _make_config(monkeypatch, {
            "TLS_ENABLED":  "true",
            "TLS_CERTFILE": str(cert),
            "TLS_KEYFILE":  str(key),
        })
        cfg.get_ssl_kwargs()
        assert cert.read_bytes() == before
