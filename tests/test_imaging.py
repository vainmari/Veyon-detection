"""
tests/test_imaging.py
─────────────────────
Tests for app/core/imaging.py — postprocess, img_to_b64.
No GPU or real YOLO model required; YOLO result objects are faked.

Run:  pytest tests/test_imaging.py -v
"""
from __future__ import annotations
import base64

import cv2
import numpy as np
import pytest

from app.core.imaging import img_to_b64, postprocess


# ── Fake YOLO result objects ──────────────────────────────────────────────────
# xyxy must be a numpy array so that .tolist() works — matching real Ultralytics

class _FakeBox:
    def __init__(self, cls_id: int, conf: float, xyxy: list[int]):
        self.cls  = [cls_id]
        self.conf = [conf]
        self.xyxy = [np.array(xyxy, dtype=np.float32)]   # ← numpy, not list


class _FakeResult:
    def __init__(self, boxes: list[_FakeBox]):
        self.boxes = boxes
        self.names = {0: "DI", 1: "Ekrano nuotraukos", 2: "Narsykle",
                      3: "Notepad", 4: "Paint", 5: "PowerPoint", 6: "Word"}


def _blank(h=100, w=200) -> np.ndarray:
    return np.zeros((h, w, 3), dtype=np.uint8)


def _large_jpeg() -> bytes:
    """Return a JPEG large enough to pass the > 1000 bytes framebuffer guard."""
    img = np.random.randint(0, 255, (100, 100, 3), dtype=np.uint8)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 95])
    assert ok and len(buf.tobytes()) > 1000
    return buf.tobytes()




# ── img_to_b64 ────────────────────────────────────────────────────────────────

class TestImgToB64:
    def test_returns_data_url(self):
        assert img_to_b64(_blank()).startswith("data:image/jpeg;base64,")

    def test_base64_is_valid(self):
        b64_part = img_to_b64(_blank()).split(",", 1)[1]
        decoded = base64.b64decode(b64_part)
        assert decoded[:2] == b"\xff\xd8"   # JPEG magic bytes

    def test_different_images_differ(self):
        white = np.full((50, 50, 3), 255, dtype=np.uint8)
        black = np.zeros((50, 50, 3), dtype=np.uint8)
        assert img_to_b64(white) != img_to_b64(black)

    def test_quality_affects_size(self):
        img = np.random.randint(0, 255, (100, 100, 3), dtype=np.uint8)
        assert len(img_to_b64(img, quality=95)) > len(img_to_b64(img, quality=10))


# ── postprocess ───────────────────────────────────────────────────────────────

class TestPostprocess:
    def test_empty_boxes_no_detections(self):
        annotated, dets = postprocess(_FakeResult([]), _blank(), keep_top1=False)
        assert dets == []
        assert annotated.shape == _blank().shape

    def test_single_detection_returned(self):
        res = _FakeResult([_FakeBox(0, 0.9, [10, 10, 50, 50])])
        _, dets = postprocess(res, _blank(), keep_top1=False)
        assert len(dets) == 1
        assert dets[0]["class_name"] == "DI"
        assert dets[0]["conf"] == pytest.approx(0.9, abs=0.001)
        assert dets[0]["box"] == [10, 10, 50, 50]

    def test_multiple_detections(self):
        boxes = [_FakeBox(0, 0.9, [0, 0, 10, 10]),
                 _FakeBox(2, 0.75, [20, 20, 60, 60])]
        _, dets = postprocess(_FakeResult(boxes), _blank(), keep_top1=False)
        assert len(dets) == 2

    def test_keep_top1_per_class(self):
        boxes = [
            _FakeBox(0, 0.95, [0,  0, 10, 10]),   # higher DI — should survive
            _FakeBox(0, 0.60, [20, 20, 30, 30]),   # lower  DI — should be dropped
            _FakeBox(2, 0.80, [5,   5, 15, 15]),   # Narsykle
        ]
        _, dets = postprocess(_FakeResult(boxes), _blank(), keep_top1=True)
        assert len(dets) == 2
        di = next(d for d in dets if d["class_id"] == 0)
        assert di["conf"] == pytest.approx(0.95, abs=0.001)

    def test_keep_top1_false_keeps_all(self):
        boxes = [_FakeBox(0, 0.9, [0, 0, 5, 5]),
                 _FakeBox(0, 0.6, [1, 1, 6, 6])]
        _, dets = postprocess(_FakeResult(boxes), _blank(), keep_top1=False)
        assert len(dets) == 2

    def test_annotated_image_differs_from_original(self):
        img = _blank()
        res = _FakeResult([_FakeBox(0, 0.9, [5, 5, 40, 40])])
        ann, _ = postprocess(res, img, keep_top1=False)
        assert not np.array_equal(ann, img)

    def test_original_image_not_mutated(self):
        img  = _blank()
        orig = img.copy()
        postprocess(_FakeResult([_FakeBox(0, 0.9, [5, 5, 40, 40])]),
                    img, keep_top1=False)
        assert np.array_equal(img, orig)

    def test_unknown_class_id_uses_fallback_name(self):
        res = _FakeResult([_FakeBox(99, 0.5, [0, 0, 10, 10])])
        _, dets = postprocess(res, _blank(), keep_top1=False)
        assert dets[0]["class_name"] == "99"

    def test_conf_rounded_to_3dp(self):
        res = _FakeResult([_FakeBox(0, 0.123456, [0, 0, 5, 5])])
        _, dets = postprocess(res, _blank(), keep_top1=False)
        assert dets[0]["conf"] == round(0.123456, 3)