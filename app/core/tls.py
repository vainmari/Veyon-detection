"""
app/core/tls.py
───────────────
Self-signed TLS certificate management for the built-in web server.

When TLS_ENABLED=true in .env, main.py serves the UI over HTTPS. If no
certificate exists at the configured paths, a self-signed one is generated
automatically (valid 10 years, SANs covering localhost, the machine's
hostname, and its current LAN IP).

Self-signed means browsers show a one-time "connection is not private"
warning — expected and unavoidable without a trusted CA. The connection is
still encrypted: session cookies and screen captures no longer cross the
LAN in plaintext. Institutions with an internal CA can drop their own
cert/key at the same paths (or point TLS_CERTFILE / TLS_KEYFILE at them)
and this module will use those instead of generating anything.

Pure utility — no imports from app.config (config imports us), so it stays
independently testable.
"""
from __future__ import annotations

import datetime
import ipaddress
import logging
import socket
from pathlib import Path

log = logging.getLogger(__name__)

CERT_VALIDITY_DAYS = 3650


def _lan_ip() -> str | None:
    """Best-effort local LAN IP (no packets are actually sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 80))  # TEST-NET-1 — never routed
            return s.getsockname()[0]
    except OSError:
        return None


def _cert_expired(cert_path: Path) -> bool:
    """True if the existing certificate is past its notAfter date."""
    from cryptography import x509
    try:
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        return cert.not_valid_after_utc <= datetime.datetime.now(datetime.timezone.utc)
    except Exception as exc:
        log.warning("could not parse %s (%s) — will regenerate", cert_path, exc)
        return True


def generate_self_signed_cert(cert_path: Path, key_path: Path) -> None:
    """
    Generate an RSA-2048 self-signed certificate + private key at the given
    paths, overwriting whatever is there. SANs: localhost, 127.0.0.1, the
    machine hostname, and the current LAN IP (if detectable).
    """
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    hostname = socket.gethostname()
    san: list[x509.GeneralName] = [
        x509.DNSName("localhost"),
        x509.DNSName(hostname),
        x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
    ]
    lan = _lan_ip()
    if lan:
        try:
            san.append(x509.IPAddress(ipaddress.ip_address(lan)))
        except ValueError:
            pass

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
    now  = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)  # self-signed: issuer == subject
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))  # clock-skew slack
        .not_valid_after(now + datetime.timedelta(days=CERT_VALIDITY_DAYS))
        .add_extension(x509.SubjectAlternativeName(san), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )

    cert_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    try:
        key_path.chmod(0o600)  # meaningful on POSIX; no-op semantics on Windows
    except OSError:
        pass
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    log.info(
        "generated self-signed TLS certificate at %s (SANs: %s)",
        cert_path, ", ".join(str(s.value) for s in san),
    )


def ensure_self_signed_cert(cert_path: Path, key_path: Path) -> None:
    """
    Make sure a usable cert/key pair exists at the given paths.

    • Both files present and cert not expired → leave them untouched
      (this is how an institution-provided cert is respected).
    • Anything missing, unparsable, or expired → (re)generate both.

    Raises on generation failure — main.py treats that as fatal rather than
    silently falling back to plaintext HTTP.
    """
    if cert_path.exists() and key_path.exists() and not _cert_expired(cert_path):
        return
    generate_self_signed_cert(cert_path, key_path)
