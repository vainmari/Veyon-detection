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

def read_model_class_names(path: str) -> Optional[list[str]]:
    """
    Return the class names embedded in a model file, ordered by class index
    (so result[i] is the name the model emits for output index i) — or None
    if the file has no usable name metadata.

    This is the authoritative index→name mapping the model actually uses at
    inference time. The DB's model_class mapping MUST be built from this, not
    from hand-typed names, or History will mislabel detections (the live
    preview uses these embedded names while History used the typed list).

    ONNX: read from the metadata 'names' prop (cheap — no full model load).
    .pt : read via ultralytics, which loads the checkpoint's names dict.
    """
    p = str(path).lower()
    try:
        if p.endswith(".onnx"):
            import ast
            import onnxruntime as ort
            sess = ort.InferenceSession(path, providers=["CPUExecutionProvider"])
            raw = sess.get_modelmeta().custom_metadata_map.get("names")
            names = ast.literal_eval(raw) if raw else None
        else:
            names = YOLO(path, task="detect").names
    except Exception:
        return None
    if not isinstance(names, dict) or not names:
        return None
    try:
        return [str(names[i]) for i in range(len(names))]
    except (KeyError, TypeError):
        # Non-contiguous or non-integer keys — not a usable index mapping.
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