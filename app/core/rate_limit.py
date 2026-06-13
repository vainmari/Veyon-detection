"""
app/core/rate_limit.py
──────────────────────
In-memory sliding-window rate limiter for login attempts.

Keyed by client IP. After MAX_ATTEMPTS failed logins within WINDOW_SEC the
key is locked out until the oldest failure ages out of the window. A
successful login clears the key immediately, so a legitimate user who
mistypes a few times is never blocked after they get it right.

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
