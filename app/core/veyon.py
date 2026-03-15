"""
app/core/veyon.py
─────────────────
Low-level Veyon WebAPI helpers: server discovery, auth, framebuffer grab.
No business logic — pure I/O primitives.
"""
from __future__ import annotations
import json
import os
import socket
import subprocess
from typing import Optional

import numpy as np
import cv2
import requests

KEY_AUTH_UUID   = "0c69b301-81b4-42d6-8fae-128cdd113314"
WEBAPI_BASE_TPL = "http://{host}:{port}/api/v1"


# ── Server lifecycle ──────────────────────────────────────────────────────────

def is_port_open(host: str, port: int, timeout: float = 1.5) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        return s.connect_ex((host, port)) == 0


def launch_webapi_server(veyon_cli: str) -> Optional[subprocess.Popen]:
    kwargs: dict = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        return subprocess.Popen([veyon_cli, "webapi", "runserver"], **kwargs)
    except Exception:
        return None


def discover_computers(veyon_cli: str) -> list[dict]:
    """Return list of {"name": str, "host": str} from Veyon's built-in directory."""
    r = subprocess.run(
        [veyon_cli, "config", "get", "BuiltinDirectory/NetworkObjects"],
        capture_output=True, text=True, timeout=10,
    )
    raw = r.stdout.strip()
    if not raw:
        raise RuntimeError(r.stderr.strip() or "veyon-cli returned nothing")
    if "=" in raw:
        raw = raw.split("=", 1)[1].strip()
    return [
        {"name": o["Name"], "host": o["HostAddress"]}
        for o in json.loads(raw)
        if o.get("Type") == 3 and o.get("HostAddress")
    ]


# ── Per-session helpers (called inside I/O worker threads) ───────────────────

def authenticate(
    session:  requests.Session,
    base_url: str,
    host:     str,
    key_name: str,
    key_data: str,
) -> Optional[str]:
    """Return connection-uid on success, None on failure."""
    try:
        r = session.post(
            f"{base_url}/authentication/{host}",
            json={"method": KEY_AUTH_UUID,
                  "credentials": {"keyname": key_name, "keydata": key_data}},
            timeout=5,
        )
        if r.status_code == 200:
            return r.json().get("connection-uid")
    except requests.RequestException:
        pass
    return None


def grab_framebuffer(
    session:  requests.Session,
    base_url: str,
    conn_uid: str,
    fmt:      str,
    quality:  int,
    width:    int,
) -> Optional[bytes]:
    params: dict = {"format": fmt, "quality": quality}
    if width:
        params["width"] = width
    try:
        r = session.get(
            f"{base_url}/framebuffer",
            params=params,
            headers={"Connection-Uid": conn_uid},
            timeout=5,
        )
        if r.status_code == 200 and len(r.content) > 1000:
            return r.content
    except requests.RequestException:
        pass
    return None


def get_logged_user(
    session:  requests.Session,
    base_url: str,
    conn_uid: str,
) -> Optional[str]:
    """
    Return the Windows login name of the user currently active on the
    monitored computer, or None if unavailable.
    Veyon WebAPI endpoint: GET /user  → {"login": "...", "fullName": "..."}
    """
    try:
        r = session.get(
            f"{base_url}/user",
            headers={"Connection-Uid": conn_uid},
            timeout=3,
        )
        if r.status_code == 200:
            data = r.json()
            return data.get("login") or data.get("name") or None
    except requests.RequestException:
        pass
    return None


def decode_image(raw: bytes) -> Optional[np.ndarray]:
    if not raw:
        return None
    arr = np.frombuffer(raw, dtype=np.uint8)
    return cv2.imdecode(arr, cv2.IMREAD_COLOR)