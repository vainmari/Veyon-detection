"""
app/core/veyon.py
─────────────────
Low-level Veyon WebAPI helpers: server discovery, auth, framebuffer grab.
No business logic — pure I/O primitives.

Supported authentication methods
──────────────────────────────────
  key    — Cryptographic key-pair (default).  Requires a .pem key file generated
            by Veyon Configurator and distributed to all monitored machines.
  logon  — OS username + password of an account that exists on the monitored
            machine (or a domain account).  Simpler to set up; no key files needed.
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
LOGON_AUTH_UUID = "63611f7c-be4a-490a-b7e4-2cb0618b2b37"
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


def list_locations(veyon_cli: str) -> list[dict]:
    """
    Parse Veyon's BuiltinDirectory/NetworkObjects and return locations with their
    computers.  Veyon object types: 1 = location/room, 3 = computer.
    Returns: [{"name": str, "computers": [{"name": str, "host": str}]}]
    Only locations that contain at least one computer are returned.

    Three strategies are tried in order so this works across Veyon versions:
      1. UID-based:       each computer carries a ParentUid pointing to a location Uid
      2. Sequential:      location object immediately precedes its computers in the list
      3. Flat fallback:   no location objects at all — all computers go into one group
    """
    r = subprocess.run(
        [veyon_cli, "config", "get", "BuiltinDirectory/NetworkObjects"],
        capture_output=True, text=True, timeout=10,
    )
    raw = r.stdout.strip()
    if not raw:
        return []
    if "=" in raw:
        raw = raw.split("=", 1)[1].strip()
    try:
        objects = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(objects, list):
        objects = []

    def _uid(o: dict) -> str:
        """Normalize a UID: strip surrounding braces, lowercase."""
        raw = o.get("Uid") or o.get("uid") or o.get("UID") or ""
        return raw.strip("{}").lower()

    def _parent_uid(o: dict) -> str:
        raw = o.get("ParentUid") or o.get("parentUid") or o.get("ParentUID") or ""
        return raw.strip("{}").lower()

    def _comp(o: dict) -> Optional[dict]:
        host = o.get("HostAddress") or o.get("hostAddress") or ""
        if not host:
            return None
        return {
            "name": o.get("Name") or o.get("name") or host,
            "host": host,
        }

    # ── Strategy 1: Uid / ParentUid (normalized) ─────────────────────────────
    # Veyon uses Type 2 for locations (rooms/groups) and Type 3 for computers.
    # Type 1 was used in older Veyon versions — check both to be safe.
    locs: dict[str, dict] = {}
    for o in objects:
        if o.get("Type") in (1, 2):
            uid = _uid(o)
            if uid:
                locs[uid] = {
                    "name": o.get("Name") or o.get("name") or "Unknown",
                    "computers": [],
                }
    for o in objects:
        if o.get("Type") == 3:
            comp = _comp(o)
            if not comp:
                continue
            parent = _parent_uid(o)
            if parent in locs:
                locs[parent]["computers"].append(comp)
    result = [v for v in locs.values() if v["computers"]]
    if result:
        return result

    # ── Strategy 2: sequential — each computer goes to the nearest PRECEDING
    #    location in the array.  Works even when all locations are listed before
    #    all computers, by tracking position explicitly. ────────────────────────
    loc_list: list[tuple[int, dict]] = []
    for i, o in enumerate(objects):
        if o.get("Type") in (1, 2):
            loc = {
                "name": o.get("Name") or o.get("name") or "Unknown",
                "computers": [],
            }
            loc_list.append((i, loc))

    if loc_list:
        for i, o in enumerate(objects):
            if o.get("Type") != 3:
                continue
            comp = _comp(o)
            if not comp:
                continue
            # Assign to the location whose index is closest to (but ≤) this
            # computer's index.  If computer appears BEFORE all locations (edge
            # case), assign to the first location.
            target = loc_list[0][1]
            for pos, loc in loc_list:
                if pos <= i:
                    target = loc
                else:
                    break
            target["computers"].append(comp)
        result = [loc for _, loc in loc_list if loc["computers"]]
        if result:
            return result

    # ── Strategy 3: flat — no location objects, all computers in one group ────
    computers = [_comp(o) for o in objects if o.get("Type") == 3]
    computers = [c for c in computers if c]
    if computers:
        return [{"name": "Imported", "computers": computers}]

    return []


# ── Per-session helpers (called inside I/O worker threads) ───────────────────

def authenticate_key(
    session:  requests.Session,
    base_url: str,
    host:     str,
    key_name: str,
    key_data: str,
) -> Optional[str]:
    """
    Key-based authentication.
    key_name — name configured in Veyon (e.g. "class")
    key_data — raw content of the .pem private-key file
    Returns connection-uid on success, None on failure.
    """
    try:
        r = session.post(
            f"{base_url}/authentication/{host}",
            json={
                "method":      KEY_AUTH_UUID,
                "credentials": {"keyname": key_name, "keydata": key_data},
            },
            timeout=5,
        )
        if r.status_code == 200:
            return r.json().get("connection-uid")
    except requests.RequestException:
        pass
    return None


def authenticate_logon(
    session:  requests.Session,
    base_url: str,
    host:     str,
    username: str,
    password: str,
) -> Optional[str]:
    """
    Logon (username/password) authentication.
    username — OS account username on the monitored machine (or domain account)
    password — corresponding password
    Returns connection-uid on success, None on failure.
    """
    try:
        r = session.post(
            f"{base_url}/authentication/{host}",
            json={
                "method":      LOGON_AUTH_UUID,
                "credentials": {"username": username, "password": password},
            },
            timeout=5,
        )
        if r.status_code == 200:
            return r.json().get("connection-uid")
    except requests.RequestException:
        pass
    return None


def authenticate(
    session:  requests.Session,
    base_url: str,
    host:     str,
    cfg:      dict,
) -> Optional[str]:
    """
    Dispatch to the correct auth method based on cfg["auth_method"].
    "key"   → authenticate_key()   (requires key_name, key_data)
    "logon" → authenticate_logon() (requires logon_username, logon_password)
    """
    method = cfg.get("auth_method", "key")
    if method == "logon":
        return authenticate_logon(
            session, base_url, host,
            cfg.get("logon_username", ""),
            cfg.get("logon_password", ""),
        )
    # default: key-based
    return authenticate_key(
        session, base_url, host,
        cfg.get("key_name", ""),
        cfg.get("key_data", ""),
    )


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
    Return the OS login name of the user currently active on the
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
