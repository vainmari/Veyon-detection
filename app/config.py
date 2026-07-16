"""
app/config.py
─────────────
Default settings, environment-driven secrets, and helpers for reading /
writing user-facing settings via NiceGUI's persistent app.storage.general.

Secret handling
───────────────
  STORAGE_SECRET — signs NiceGUI session cookies. Loaded from the .env file
  (or process environment). If .env does not exist, one is auto-generated with
  a random secret so the app starts without manual setup.

Logon password
──────────────
  The Veyon "logon" auth-method password is stored in the OS credential vault
  (Windows Credential Manager / macOS Keychain / Secret Service on Linux) via
  the `keyring` package — never written to app.storage.general as plaintext.
  get_settings() / save_settings() transparently route logon_password through
  keyring so every caller sees a regular dict and doesn't need to know.
"""
from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path
from typing import Optional

import sys

import keyring
from dotenv import load_dotenv
from nicegui import app

log = logging.getLogger(__name__)

# When frozen by PyInstaller, __file__ is inside the temp _MEI* extraction dir.
# sys.executable always points to the actual .exe / script, so its parent is
# where the user placed the program — which is where .env should live.
if getattr(sys, "frozen", False):
    _ENV_PATH = Path(sys.executable).parent / ".env"
else:
    _ENV_PATH = Path(__file__).resolve().parent.parent / ".env"

# Auto-create .env with a random secret if it doesn't exist yet.
if not _ENV_PATH.exists():
    _generated_secret = secrets.token_urlsafe(48)
    _ENV_PATH.write_text(
        "# Auto-generated on first run. Edit as needed.\n"
        f"STORAGE_SECRET={_generated_secret}\n"
        "INITIAL_ADMIN_USERNAME=admin\n"
        "INITIAL_ADMIN_PASSWORD=admin\n",
        encoding="utf-8",
    )
    log.warning(
        ".env not found — created automatically at %s with a random "
        "STORAGE_SECRET and default admin/admin credentials. "
        "Change the admin password after first login.",
        _ENV_PATH,
    )

load_dotenv(_ENV_PATH)

# ── Session cookie signing key ────────────────────────────────────────────────
_PLACEHOLDER_SECRET = "replace-with-output-of-secrets.token_urlsafe(48)"
STORAGE_SECRET: str = os.environ.get("STORAGE_SECRET", "").strip()

if not STORAGE_SECRET or STORAGE_SECRET == _PLACEHOLDER_SECRET:
    raise RuntimeError(
        "STORAGE_SECRET is not set or is still the placeholder value. "
        "Edit .env at the repo root and set STORAGE_SECRET to a random string. "
        "Generate one with:\n"
        '    python -c "import secrets; print(secrets.token_urlsafe(48))"'
    )

# ── Initial admin (used by main.py on first run only) ─────────────────────────
INITIAL_ADMIN_USERNAME: str           = os.environ.get("INITIAL_ADMIN_USERNAME", "admin").strip()
INITIAL_ADMIN_PASSWORD: Optional[str] = os.environ.get("INITIAL_ADMIN_PASSWORD")
if INITIAL_ADMIN_PASSWORD is not None:
    INITIAL_ADMIN_PASSWORD = INITIAL_ADMIN_PASSWORD.strip() or None

# ── Login rate limiting ────────────────────────────────────────────────────────
# After LOGIN_MAX_ATTEMPTS failed logins from one client IP within
# LOGIN_WINDOW_SEC seconds, further attempts from that IP are rejected until
# the oldest failure ages out of the window. See app/core/rate_limit.py.
def _env_int(key: str, default: int, minimum: int = 1) -> int:
    raw = os.environ.get(key, "").strip()
    if not raw:
        return default
    try:
        return max(minimum, int(raw))
    except ValueError:
        log.warning("%s=%r is not a number — using default %d", key, raw, default)
        return default


LOGIN_MAX_ATTEMPTS: int = _env_int("LOGIN_MAX_ATTEMPTS", 5)
LOGIN_WINDOW_SEC:   int = _env_int("LOGIN_WINDOW_SEC",   300)

# ── Web server binding ─────────────────────────────────────────────────────────
# 0.0.0.0 (default) exposes the UI to the whole LAN so teachers can open it
# from other machines. Set BIND_HOST=127.0.0.1 in .env to restrict access to
# the server machine only (e.g. when fronting the app with a reverse proxy).
BIND_HOST: str = os.environ.get("BIND_HOST", "0.0.0.0").strip() or "0.0.0.0"
try:
    BIND_PORT: int = int(os.environ.get("BIND_PORT", "8080").strip() or "8080")
except ValueError:
    log.warning("BIND_PORT is not a number — falling back to 8080")
    BIND_PORT = 8080


# ── Keyring (Veyon logon password) ────────────────────────────────────────────
_KEYRING_SERVICE   = "veyon-ai-monitor"
_KEYRING_LOGON_KEY = "veyon_logon_password"


def _keyring_get_logon_password() -> str:
    try:
        return keyring.get_password(_KEYRING_SERVICE, _KEYRING_LOGON_KEY) or ""
    except Exception as exc:
        log.warning("keyring read failed (%s); falling back to empty password", exc)
        return ""


def _keyring_set_logon_password(value: str) -> None:
    try:
        if value:
            keyring.set_password(_KEYRING_SERVICE, _KEYRING_LOGON_KEY, value)
        else:
            try:
                keyring.delete_password(_KEYRING_SERVICE, _KEYRING_LOGON_KEY)
            except keyring.errors.PasswordDeleteError:
                pass  # already absent — fine
    except Exception as exc:
        log.error("keyring write failed: %s", exc)


# ── User-facing defaults (everything below is shown in /settings) ─────────────
# Each key can be overridden via a VEYON_* env var in .env.
# UI changes saved through /settings take precedence over these defaults.

def _env_bool(key: str, default: bool) -> bool:
    val = os.environ.get(key)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes")


# ── TLS / HTTPS ────────────────────────────────────────────────────────────────
# TLS_ENABLED=true serves the UI over HTTPS. If the cert/key files don't exist
# a self-signed pair is generated automatically (see app/core/tls.py) — the
# browser shows a one-time warning, but session cookies and screen captures
# stop crossing the LAN in plaintext. Point TLS_CERTFILE / TLS_KEYFILE at an
# institution-issued cert to avoid the warning.
TLS_ENABLED: bool = _env_bool("TLS_ENABLED", False)
_TLS_DIR = _ENV_PATH.parent / "data" / "tls"
TLS_CERTFILE: str = os.environ.get("TLS_CERTFILE", "").strip() or str(_TLS_DIR / "cert.pem")
TLS_KEYFILE:  str = os.environ.get("TLS_KEYFILE",  "").strip() or str(_TLS_DIR / "key.pem")


def get_ssl_kwargs() -> dict:
    """
    Return the ssl_* kwargs for ui.run(), or {} when TLS is disabled.
    Generation failure raises instead of silently starting in plaintext —
    an operator who set TLS_ENABLED=true must never get unencrypted HTTP
    without noticing.
    """
    if not TLS_ENABLED:
        return {}
    from app.core.tls import ensure_self_signed_cert
    ensure_self_signed_cert(Path(TLS_CERTFILE), Path(TLS_KEYFILE))
    return {"ssl_certfile": TLS_CERTFILE, "ssl_keyfile": TLS_KEYFILE}


DEFAULTS: dict = {
    # ── Authentication ─────────────────────────────────────────────────────────
    "auth_method":         os.environ.get("VEYON_AUTH_METHOD",         "key"),
    "key_name":            os.environ.get("VEYON_KEY_NAME",            "class"),
    "key_path":            os.environ.get("VEYON_KEY_PATH",            "class.pem"),
    "logon_username":      os.environ.get("VEYON_LOGON_USERNAME",      ""),
    # ── Veyon CLI ──────────────────────────────────────────────────────────────
    "veyon_cli":           os.environ.get("VEYON_CLI",                 r"C:\Program Files\Veyon\veyon-cli.exe"),
    # ── WebAPI Server ──────────────────────────────────────────────────────────
    "host":                os.environ.get("VEYON_HOST",                "localhost"),
    "port":                os.environ.get("VEYON_PORT",                "11080"),
    "auto_start":          _env_bool("VEYON_AUTO_START",               True),
    "start_wait":          os.environ.get("VEYON_START_WAIT",          "10"),
    # ── Capture ────────────────────────────────────────────────────────────────
    "interval":            os.environ.get("VEYON_INTERVAL",            "1"),
    "img_fmt":             os.environ.get("VEYON_IMG_FMT",             "jpeg"),
    "img_quality":         os.environ.get("VEYON_IMG_QUALITY",         "85"),
    "img_width":           os.environ.get("VEYON_IMG_WIDTH",           "1920"),
    # ── Detection ──────────────────────────────────────────────────────────────
    # model_path and detect_imgsz are managed by the Models page, not /settings.
    "model_path":          "weights/ONNX_FP32.onnx",
    "detect_conf":         os.environ.get("VEYON_DETECT_CONF",         "0.40"),
    "detect_iou":          os.environ.get("VEYON_DETECT_IOU",          "0.20"),
    "detect_imgsz":        "480",
    "keep_top1":           _env_bool("VEYON_KEEP_TOP1",                True),
    # ── Detection performance ───────────────────────────────────────────────────
    "batch_max_cuda":      os.environ.get("VEYON_BATCH_MAX_CUDA",      "32"),
    "batch_max_cpu":       os.environ.get("VEYON_BATCH_MAX_CPU",       "16"),
    "detect_cycle_timing": _env_bool("VEYON_DETECT_CYCLE_TIMING",      False),
    # ── Alert behaviour ────────────────────────────────────────────────────────
    "alert_threshold":     os.environ.get("VEYON_ALERT_THRESHOLD",     "1"),
    # ── Data retention ─────────────────────────────────────────────────────────
    # Days to keep detection events (screenshots included); 0 = keep forever.
    # Enforced hourly by app/services/retention_service.py.
    "retention_days":      os.environ.get("VEYON_RETENTION_DAYS",      "0"),
}


def apply_active_model(model_id: int) -> None:
    """
    Called from UI context after set_active_model().
    Syncs detection_class table and updates all model-dependent settings
    (model_path, detect_imgsz) so the next monitoring start picks them up
    automatically — no manual Settings edits needed.

    Skips the model_path update (and warns) if the file referenced in the
    DB row no longer exists on disk; otherwise monitoring would start with
    a stale path and fail at YOLO load time with a confusing error.
    """
    from app.db.database import get_model_by_id, sync_classes_from_model
    m = get_model_by_id(model_id)
    if not m:
        return
    sync_classes_from_model(model_id)
    s = get_settings()
    path = m.get("onnx_path") or m.get("pt_path")
    if path and Path(path).exists():
        s["model_path"] = path
    elif path:
        log.warning(
            "apply_active_model: model file %r referenced in DB row %s does "
            "not exist on disk — keeping previous model_path", path, model_id,
        )
    if m.get("imgsz"):
        s["detect_imgsz"] = str(m["imgsz"])
    save_settings(s)


def get_settings() -> dict:
    """
    Return stored settings merged on top of defaults.
    `logon_password` is transparently pulled from the OS keyring so callers
    receive a plain dict and don't have to know where the secret lives.
    """
    merged = {**DEFAULTS, **app.storage.general.get("settings", {})}
    merged["logon_password"] = _keyring_get_logon_password()
    return merged


def save_settings(vals: dict) -> None:
    """
    Persist settings, splitting the logon_password out into the OS keyring
    so it is never written to app.storage.general (a plaintext JSON on disk).
    """
    vals = dict(vals)  # don't mutate caller's dict
    logon_pw = vals.pop("logon_password", None)
    if logon_pw is not None:
        _keyring_set_logon_password(logon_pw)
    app.storage.general["settings"] = vals


def collect_cfg() -> dict:
    """
    Cast settings strings to the types MonitorController expects.
    For key-based auth, reads the key file and injects key_data into the cfg
    so the I/O worker never touches the filesystem during monitoring.
    """
    s = get_settings()
    cfg = {
        **s,
        "port":             int(s["port"]),
        "start_wait":       int(s["start_wait"]),
        "interval":         float(s["interval"]),
        "img_quality":      int(s["img_quality"]),
        "img_width":        int(s["img_width"]),
        "detect_conf":      float(s["detect_conf"]),
        "detect_iou":       float(s["detect_iou"]),
        "detect_imgsz":     int(s["detect_imgsz"]),
        "alert_threshold":     max(1, int(s.get("alert_threshold", 1))),
        "batch_max_cuda":      max(1, int(s.get("batch_max_cuda", 32))),
        "batch_max_cpu":       max(1, int(s.get("batch_max_cpu", 16))),
        "detect_cycle_timing": bool(s.get("detect_cycle_timing", False)),
        "key_data":            "",
    }
    if s.get("auth_method", "key") == "key":
        try:
            cfg["key_data"] = Path(s["key_path"]).read_text(encoding="utf-8").strip()
        except OSError:
            cfg["key_data"] = ""
    return cfg
