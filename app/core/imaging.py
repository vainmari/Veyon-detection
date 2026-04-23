"""
app/core/imaging.py
───────────────────
Post-processing: draw bounding boxes and encode to base64.
"""
from __future__ import annotations
import base64

import cv2
import numpy as np

from app.core.colors import BGR_PALETTE

# BOX_COLORS: unlimited classes supported — cycles through the 32-color palette.
# Imported from colors.py so it stays in sync with detection_class.color_hex in the DB.
BOX_COLORS = BGR_PALETTE


def postprocess(
    res,
    img_bgr:   np.ndarray,
    keep_top1: bool,
) -> tuple[np.ndarray, list[dict]]:
    """
    Draw bounding boxes on a copy of img_bgr and return
    (annotated_image, detection_list).

    When there are no detections we skip the ~600 KB image copy and return
    img_bgr directly — most classroom frames have zero detections.
    """
    names = res.names
    raw = [
        {"cls_id": int(b.cls[0]), "conf": float(b.conf[0]),
         "xyxy": list(map(int, b.xyxy[0].tolist()))}
        for b in res.boxes
    ]
    if keep_top1:
        top: dict[int, dict] = {}
        for b in raw:
            if b["cls_id"] not in top or b["conf"] > top[b["cls_id"]]["conf"]:
                top[b["cls_id"]] = b
        raw = list(top.values())

    if not raw:
        return img_bgr, []

    annotated = img_bgr.copy()
    dets: list[dict] = []

    for b in raw:
        cid             = b["cls_id"]
        conf_v          = b["conf"]
        x1, y1, x2, y2 = b["xyxy"]
        label           = names.get(cid, str(cid))
        color           = BOX_COLORS[cid % len(BOX_COLORS)]

        cv2.rectangle(annotated, (x1, y1), (x2, y2), color, 2)

        text        = f"{label} {conf_v:.0%}"
        (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 0.55, 1)
        cv2.rectangle(annotated, (x1, y1 - th - 6), (x1 + tw + 4, y1), color, -1)
        cv2.putText(annotated, text, (x1 + 2, y1 - 4),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1, cv2.LINE_AA)

        dets.append({
            "class_id":   cid,
            "class_name": label,
            "conf":       round(conf_v, 3),
            "box":        [x1, y1, x2, y2],
        })

    return annotated, dets


def encode_jpeg(img_bgr: np.ndarray, quality: int = 85) -> bytes:
    """Encode a BGR numpy array to raw JPEG bytes. Returns b'' on failure."""
    ok, buf = cv2.imencode(".jpg", img_bgr, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return buf.tobytes() if ok else b""


def bytes_to_b64_dataurl(jpeg_bytes: bytes) -> str:
    """Wrap already-encoded JPEG bytes as a base64 data-URL for ui.image."""
    if not jpeg_bytes:
        return ""
    return "data:image/jpeg;base64," + base64.b64encode(jpeg_bytes).decode()


def img_to_b64(img_bgr: np.ndarray, quality: int = 82) -> str:
    """Encode a BGR numpy array to a base64 JPEG data-URL for ui.image.

    Kept for callers that don't have pre-encoded bytes. The hot path in the
    monitor service calls encode_jpeg() + bytes_to_b64_dataurl() so the JPEG
    is produced once and reused for DB and preview.
    """
    return bytes_to_b64_dataurl(encode_jpeg(img_bgr, quality))


