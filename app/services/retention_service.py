"""
app/services/retention_service.py
──────────────────────────────────
Background daemon that enforces the data-retention policy: once per hour it
reads the admin-configured `retention_days` setting and permanently deletes
detection events (screenshots + detections + notifications) older than that.

retention_days = 0 (the default) disables automatic deletion entirely.

The first pass runs shortly after startup so a lowered retention window
takes effect on the next restart, not up to an hour later. Deletion itself
is batched inside purge_old_events() so the monitoring hot path is never
blocked behind one giant DELETE.
"""
from __future__ import annotations

import logging
import threading

log = logging.getLogger(__name__)

TICK_SECONDS    = 3600   # re-check the policy once per hour
STARTUP_DELAY_S = 30     # let startup hooks / first page loads settle first


def _tick() -> None:
    """One enforcement pass — safe to call from any thread."""
    from app.config import get_settings
    from app.db.retention import purge_old_events

    try:
        days = int(str(get_settings().get("retention_days", "0")).strip() or 0)
    except (ValueError, TypeError):
        log.warning("retention_service: retention_days is not a number — skipping")
        return
    if days <= 0:
        return
    try:
        n = purge_old_events(days)
    except Exception as exc:
        log.error("retention_service: purge failed: %s", exc)
        return
    if n:
        log.info("retention_service: purged %d event(s) older than %d day(s)", n, days)


def _loop(stop_event: threading.Event) -> None:
    if stop_event.wait(STARTUP_DELAY_S):
        return
    while True:
        _tick()
        if stop_event.wait(TICK_SECONDS):
            return


def start_retention_worker() -> threading.Event:
    """
    Launch the daemon thread. Returns the stop Event (used by tests;
    the production app just lets the daemon die with the process).
    """
    stop_event = threading.Event()
    threading.Thread(
        target=_loop, args=(stop_event,), daemon=True, name="retention",
    ).start()
    return stop_event
