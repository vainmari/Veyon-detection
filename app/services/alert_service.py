"""
app/services/alert_service.py
──────────────────────────────
Checks each batch of detections against the prohibited-class rules and
inserts a notification row for every match, subject to the consecutive-
detection threshold set in Settings.

Called from the detect worker thread — must be thread-safe (it is, since
all DB writes use fresh sqlite3 connections and state dict updates are
effectively atomic on CPython's GIL for simple key/value ops).
"""
from __future__ import annotations

import threading
import time
from typing import Optional

import app.state as state
from app.config import get_settings
from app.db.database import get_prohibited_class_ids, insert_notification


# ── Prohibited-class cache ────────────────────────────────────────────────────
# The detect worker calls check_and_fire() for every frame on every computer
# (30 computers × 1 fps = 30 DB round-trips per second just to fetch the alert
# rule set). Alert rules change on human timescales, so a short TTL cache with
# explicit invalidation on rule edits is safe and drops this to near-zero.

_CACHE_TTL_SEC = 3.0
_cache_lock    = threading.Lock()
_cache: dict[tuple[Optional[int], Optional[int]], tuple[float, dict[int, dict]]] = {}


def _get_prohibited_cached(
    model_id:    Optional[int],
    schedule_id: Optional[int],
) -> dict[int, dict]:
    key = (model_id, schedule_id)
    now = time.monotonic()
    with _cache_lock:
        entry = _cache.get(key)
        if entry is not None and now - entry[0] < _CACHE_TTL_SEC:
            return entry[1]
    # Fetch outside the lock — the DB query is the slow part
    val = get_prohibited_class_ids(model_id, schedule_id)
    with _cache_lock:
        _cache[key] = (now, val)
    return val


def invalidate_prohibited_cache() -> None:
    """Drop all cached alert-rule lookups. Called from rule-mutation DB paths."""
    with _cache_lock:
        _cache.clear()


def check_and_fire(
    event_id:    int,
    dets:        list[dict],
    computer:    str,
    model_id:    Optional[int] = None,
    schedule_id: Optional[int] = None,
) -> int:
    """
    For each detection whose class_id (YOLO index) is in the prohibited set,
    increment a consecutive-hit counter.  A notification is inserted only when
    the counter reaches ``alert_threshold`` (default 1 = every detection).
    Counters for classes absent from this frame are reset to zero.

    Parameters
    ----------
    event_id    : DB id of the detection_event that was just inserted.
    dets        : List of detection dicts from imaging.postprocess().
    computer    : Display name of the monitored computer.
    schedule_id : When set, per-schedule class overrides are applied instead of
                  global notification_enabled flags.

    Returns
    -------
    Number of notifications fired this call.
    """
    prohibited = _get_prohibited_cached(model_id, schedule_id)   # {class_index: {"id": db_id, ...}}
    if not prohibited:
        # Reset all counters for this computer when there are no rules
        for key in list(state.consecutive_detections):
            if key[0] == computer:
                del state.consecutive_detections[key]
        return 0

    threshold = int(get_settings().get("alert_threshold", 1))
    threshold = max(1, threshold)

    # Collect which prohibited classes fired this frame
    detected_prohibited: dict[int, dict] = {}  # class_index → prohibited entry
    seen_in_frame: set[int] = set()

    for d in dets:
        cid = d["class_id"]
        if cid in prohibited and cid not in seen_in_frame:
            seen_in_frame.add(cid)
            detected_prohibited[cid] = prohibited[cid]

    # Reset counters for prohibited classes NOT in this frame
    for cid in list(prohibited):
        key = (computer, cid)
        if cid not in detected_prohibited:
            state.consecutive_detections[key] = 0

    # Increment counters and fire when threshold reached
    fired = 0
    for cid, entry in detected_prohibited.items():
        key = (computer, cid)
        count = state.consecutive_detections.get(key, 0) + 1
        state.consecutive_detections[key] = count

        if count >= threshold:
            # Reset so the next streak starts fresh (avoids firing every frame
            # once threshold is met — only fires once per threshold crossing)
            state.consecutive_detections[key] = 0
            insert_notification(
                event_id = event_id,
                class_id = entry["id"],
                _commit  = False,   # batched with the detect worker's event commit
            )
            fired += 1

    return fired
