"""
app/core/yolo.py
────────────────
Process-wide YOLO model singleton with thread-safe load / reset.
"""
from __future__ import annotations
import threading
from typing import Optional
from ultralytics import YOLO

_model:      Optional[YOLO] = None
_model_lock: threading.Lock = threading.Lock()


def get_model(path: str) -> YOLO:
    """Load model once; return cached instance on subsequent calls."""
    global _model
    with _model_lock:
        if _model is None:
            _model = YOLO(path, task="detect")
    return _model


def reset_model() -> None:
    """Force a fresh load on the next get_model() call (e.g. after path change)."""
    global _model
    with _model_lock:
        _model = None