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

from typing import Optional

import app.state as state
from app.config import get_settings
from app.db.database import get_prohibited_class_ids, insert_notification


def check_and_fire(
    event_id: int,
    dets:     list[dict],
    computer: str,
    model_id: Optional[int] = None,
) -> int:
    """
    For each detection whose class_id (YOLO index) is in the prohibited set,
    increment a consecutive-hit counter.  A notification is inserted only when
    the counter reaches ``alert_threshold`` (default 1 = every detection).
    Counters for classes absent from this frame are reset to zero.

    Parameters
    ----------
    event_id : DB id of the detection_event that was just inserted.
    dets     : List of detection dicts from imaging.postprocess().
    computer : Display name of the monitored computer.

    Returns
    -------
    Number of notifications fired this call.
    """
    prohibited = get_prohibited_class_ids(model_id)   # {class_index: {"id": db_id, ...}}
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
            )
            fired += 1

    return fired
