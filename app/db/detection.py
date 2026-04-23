"""
app/db/detection.py
───────────────────
Detection class lookups, event insertion, frame retrieval,
and anonymous-event assignment.
"""
from __future__ import annotations

import base64
from typing import Optional

from app.db._core import _conn, _now


# ── Detection classes ─────────────────────────────────────────────────────────

def list_classes() -> list[dict]:
    c = _conn()
    return [dict(r) for r in c.execute(
        "SELECT * FROM detection_class ORDER BY name"
    ).fetchall()]


def get_class_by_model_index(model_id: int, class_index: int) -> Optional[dict]:
    c = _conn()
    row = c.execute("""
        SELECT dc.*
        FROM   model_class mc
        JOIN   detection_class dc ON dc.id = mc.class_id
        WHERE  mc.model_id = ? AND mc.class_index = ?
    """, (model_id, class_index)).fetchone()
    return dict(row) if row else None


# ── Detection events ──────────────────────────────────────────────────────────

def insert_event(
    computer_id:      int,
    dets:             list[dict],
    user_id:          Optional[int]   = None,
    os_username: Optional[str]   = None,
    frame_bytes:      Optional[bytes] = None,
    model_id:         Optional[int]   = None,
    _commit:          bool            = True,
) -> int:
    """
    Write one detection_event row + one detection row per bounding box.
    Always called — even when dets is empty (had_detection = 0).

    Pass _commit=False from a batching caller to coalesce many events into a
    single transaction (one fsync). The caller must call conn.commit()
    afterwards — all inserts up to that point share one WAL transaction.
    """
    c = _conn()
    cur = c.execute(
        "INSERT INTO detection_event "
        "(computer_id, user_id, model_id, os_username, detected_at, frame_blob, had_detection) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (computer_id, user_id, model_id, os_username, _now(),
         frame_bytes, 1 if dets else 0),
    )
    event_id = cur.lastrowid
    for d in dets:
        if model_id is None:
            continue
        cls_row = c.execute(
            "SELECT mc.class_id FROM model_class mc "
            "WHERE mc.model_id = ? AND mc.class_index = ?",
            (model_id, d["class_id"]),
        ).fetchone()
        if cls_row is None:
            continue
        c.execute(
            "INSERT INTO detection "
            "(event_id, class_id, confidence, box_x1, box_y1, box_x2, box_y2) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (event_id, cls_row[0], d["conf"], *d["box"]),
        )
    if _commit:
        c.commit()
    return event_id


def get_event_frame_b64(event_id: int) -> Optional[str]:
    """Return the stored raw (unannotated) frame as a base64 JPEG data-URL, or None."""
    c = _conn()
    row = c.execute(
        "SELECT frame_blob FROM detection_event WHERE id = ?", (event_id,)
    ).fetchone()
    if row and row["frame_blob"]:
        b64 = base64.b64encode(bytes(row["frame_blob"])).decode()
        return f"data:image/jpeg;base64,{b64}"
    return None


def get_event_frame_annotated_b64(event_id: int) -> Optional[str]:
    """
    Re-draw bounding boxes from the detection table onto the stored raw frame
    and return the result as a base64 JPEG data-URL.
    Returns None if no frame is stored or if the frame cannot be decoded.
    """
    import cv2 as _cv2
    import numpy as _np
    from app.core.colors import hex_to_bgr

    c = _conn()
    row = c.execute(
        "SELECT frame_blob FROM detection_event WHERE id = ?", (event_id,)
    ).fetchone()
    if not row or not row["frame_blob"]:
        return None

    arr = _np.frombuffer(bytes(row["frame_blob"]), dtype=_np.uint8)
    img = _cv2.imdecode(arr, _cv2.IMREAD_COLOR)
    if img is None:
        return None

    dets = c.execute("""
        SELECT d.box_x1, d.box_y1, d.box_x2, d.box_y2,
               dc.color_hex, dc.name, d.confidence
        FROM   detection d
        JOIN   detection_class dc ON dc.id = d.class_id
        WHERE  d.event_id = ?
    """, (event_id,)).fetchall()

    for x1, y1, x2, y2, color_hex, label, conf in dets:
        color = hex_to_bgr(color_hex)
        _cv2.rectangle(img, (x1, y1), (x2, y2), color, 2)
        text = f"{label} {conf:.0%}"
        (tw, th), _ = _cv2.getTextSize(text, _cv2.FONT_HERSHEY_SIMPLEX, 0.55, 1)
        _cv2.rectangle(img, (x1, y1 - th - 6), (x1 + tw + 4, y1), color, -1)
        _cv2.putText(img, text, (x1 + 2, y1 - 4),
                     _cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1,
                     _cv2.LINE_AA)

    ok, buf = _cv2.imencode(".jpg", img, [_cv2.IMWRITE_JPEG_QUALITY, 85])
    if not ok:
        return None
    return "data:image/jpeg;base64," + base64.b64encode(buf.tobytes()).decode()


# ── Retroactive assignment ────────────────────────────────────────────────────

def count_anonymous_events(computer_id: int) -> int:
    c = _conn()
    return c.execute(
        "SELECT COUNT(*) FROM detection_event "
        "WHERE computer_id = ? AND user_id IS NULL",
        (computer_id,),
    ).fetchone()[0]


def assign_anonymous_events(user_id: int, computer_id: int) -> int:
    """Manually assign anonymous events on a specific computer to a user."""
    c = _conn()
    cur = c.execute(
        "UPDATE detection_event SET user_id = ? "
        "WHERE computer_id = ? AND user_id IS NULL",
        (user_id, computer_id),
    )
    c.commit()
    return cur.rowcount
