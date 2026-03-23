"""
app/core/imaging.py
───────────────────
Post-processing: draw bounding boxes, encode to base64, optionally save to disk.
"""
from __future__ import annotations
import base64
import os
from datetime import datetime

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


def img_to_b64(img_bgr: np.ndarray, quality: int = 82) -> str:
    """Encode a BGR numpy array to a base64 JPEG data-URL for ui.image."""
    ok, buf = cv2.imencode(".jpg", img_bgr, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        return ""
    return "data:image/jpeg;base64," + base64.b64encode(buf.tobytes()).decode()


def save_image(
    output_dir: str,
    name:       str,
    img:        np.ndarray,
    suffix:     str,
    fmt:        str,
) -> None:
    folder = os.path.join(output_dir, name.replace(" ", "_"))
    os.makedirs(folder, exist_ok=True)
    ext  = "jpg" if fmt == "jpeg" else "png"
    ts   = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    path = os.path.join(folder, f"{ts}_{suffix}.{ext}")
    cv2.imwrite(path, img)