"""
app/core/yolo.py
────────────────
Process-wide YOLO model singleton with thread-safe load / reset.
"""
from __future__ import annotations
import threading
from typing import Optional
from ultralytics import YOLO


def onnx_static_batch_size(path: str) -> Optional[int]:
    """Return the fixed batch size of an ONNX model, or None if it's dynamic.

    Inspects the first input's batch dimension via onnxruntime session metadata.
    Returns None for dynamic-batch models (dim is a string symbol or None).
    Returns the integer value (typically 1) for static-batch models.
    Returns None on any error so the caller can apply a safe default.
    """
    try:
        import onnxruntime as ort
        sess = ort.InferenceSession(path, providers=["CPUExecutionProvider"])
        batch_dim = sess.get_inputs()[0].shape[0]
        return batch_dim if isinstance(batch_dim, int) else None
    except Exception:
        return None

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