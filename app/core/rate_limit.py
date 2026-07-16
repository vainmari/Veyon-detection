"""
app/core/rate_limit.py
──────────────────────
In-memory sliding-window rate limiter for login attempts.

Login attempts are tracked under two keys at once: the client IP AND the
username being tried (lowercased). A login is blocked while EITHER key is
over the limit:

  • the per-IP key throttles a single machine hammering the login form;
  • the per-username key throttles a distributed guesser rotating IPs
    against one account — and, in a NAT'd lab where everyone shares an IP,
    it means one student's failures only lock out the username they were
    attacking, not every user behind that IP.

Trade-off: anyone who knows a username can lock that account for up to
WINDOW_SEC by spamming wrong passwords. That is bounded (the sliding window
expires) and strictly better than the same attack locking the whole IP.

After MAX_ATTEMPTS failed logins within WINDOW_SEC a key is locked out until
the oldest failure ages out of the window. A successful login clears both
keys immediately, so a legitimate user who mistypes a few times is never
blocked after they get it right.

State is process-local (a dict guarded by a lock) — sufficient for this
single-process app. Restarting the server clears all counters.

Limits are configurable via LOGIN_MAX_ATTEMPTS / LOGIN_WINDOW_SEC in .env
(parsed in app/config.py — importing it here also guarantees .env is loaded
before the values are read).
"""
from __future__ import annotations

import threading
import time

from app.config import LOGIN_MAX_ATTEMPTS, LOGIN_WINDOW_SEC

MAX_ATTEMPTS = LOGIN_MAX_ATTEMPTS        # failed attempts allowed inside the window
WINDOW_SEC   = float(LOGIN_WINDOW_SEC)   # sliding window length in seconds

# Usernames are attacker-controlled input, so the per-username keys could
# grow without bound. When the dict exceeds this many keys, expired entries
# are swept out; what survives is genuinely recent activity, whose size is
# already bounded by how fast an attacker can actually submit requests.
_MAX_KEYS = 4096

_lock = threading.Lock()
_failures: dict[str, list[float]] = {}   # key → monotonic timestamps of failures


def _prune(times: list[float], now: float) -> list[float]:
    cutoff = now - WINDOW_SEC
    return [t for t in times if t > cutoff]


def is_blocked(key: str) -> bool:
    """True if `key` has exhausted its attempts and must wait."""
    return retry_after(key) > 0


def retry_after(key: str) -> float:
    """
    Seconds until `key` may try again, or 0 if it is not blocked.
    Also prunes expired entries as a side effect.
    """
    now = time.monotonic()
    with _lock:
        times = _prune(_failures.get(key, []), now)
        if times:
            _failures[key] = times
        else:
            _failures.pop(key, None)
        if len(times) < MAX_ATTEMPTS:
            return 0.0
        # Blocked until the oldest failure leaves the window.
        return max(0.0, times[0] + WINDOW_SEC - now)


def record_failure(key: str) -> None:
    """Register one failed attempt for `key`."""
    now = time.monotonic()
    with _lock:
        if len(_failures) >= _MAX_KEYS:
            _sweep_expired(now)
        times = _prune(_failures.get(key, []), now)
        times.append(now)
        _failures[key] = times


def record_success(key: str) -> None:
    """Clear all failure history for `key` (called after a successful login)."""
    with _lock:
        _failures.pop(key, None)


def reset_all() -> None:
    """Clear every key — used by tests."""
    with _lock:
        _failures.clear()


def _sweep_expired(now: float) -> None:
    """Drop every fully-expired key. Caller must hold _lock."""
    for k in list(_failures):
        kept = _prune(_failures[k], now)
        if kept:
            _failures[k] = kept
        else:
            del _failures[k]


# ── Login-specific helpers (IP + username combined) ───────────────────────────

def _login_keys(client_ip: str, username: str) -> list[str]:
    """
    Both tracking keys for one login attempt. The username key is normalized
    (stripped, lowercased, length-capped) so case variants of one account
    share a counter and absurdly long inputs can't bloat the dict.
    """
    keys = [f"ip:{client_ip}"]
    uname = (username or "").strip().lower()[:64]
    if uname:
        keys.append(f"user:{uname}")
    return keys


def login_retry_after(client_ip: str, username: str) -> float:
    """Seconds until this (IP, username) pair may try again — 0 if allowed."""
    return max(retry_after(k) for k in _login_keys(client_ip, username))


def record_login_failure(client_ip: str, username: str) -> None:
    for k in _login_keys(client_ip, username):
        record_failure(k)


def record_login_success(client_ip: str, username: str) -> None:
    for k in _login_keys(client_ip, username):
        record_success(k)
