"""
app/services/alert_service.py
──────────────────────────────
Checks each batch of detections against the prohibited-class rules and
inserts a notification row for every match.

Called from the detect worker thread — must be thread-safe (it is, since
all DB writes use fresh sqlite3 connections).
"""
from __future__ import annotations

from app.db.database import get_prohibited_class_ids, insert_notification


def check_and_fire(
    event_id: int,
    dets:     list[dict],
    computer: str,
    student:  str,
) -> int:
    """
    For each detection whose class_id (YOLO index) is in the prohibited set,
    insert one notification row.

    Parameters
    ----------
    event_id : DB id of the detection_event that was just inserted.
    dets     : List of detection dicts from imaging.postprocess().
    computer : Display name of the monitored computer.
    student  : Resolved student display name (Windows username or DB username).

    Returns
    -------
    Number of notifications fired (0 if nothing matched or no rules defined).
    """
    if not dets:
        return 0

    prohibited = get_prohibited_class_ids()   # {class_index: {"id": db_id, "color_hex": str}}
    if not prohibited:
        return 0

    fired = 0
    seen: set[int] = set()   # deduplicate: one notification per class per event

    for d in dets:
        cid = d["class_id"]
        if cid in prohibited and cid not in seen:
            seen.add(cid)
            insert_notification(
                event_id = event_id,
                class_id = prohibited[cid]["id"],
                computer = computer,
                student  = student,
            )
            fired += 1

    return fired
