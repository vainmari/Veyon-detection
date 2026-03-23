"""
app/config.py
─────────────
Default settings and helpers for reading / writing settings
via NiceGUI's persistent app.storage.general.
"""
from __future__ import annotations
from nicegui import app

STORAGE_SECRET = "change-me-to-any-random-string"   # override via .env / env var

DEFAULTS: dict = {
    "key_name":       "class",
    "key_path":       "class.pem",
    "veyon_cli":      r"C:\Program Files\Veyon\veyon-cli.exe",
    "host":           "localhost",
    "port":           "11080",
    "auto_start":     True,
    "start_wait":     "10",
    "interval":       "1",
    "img_fmt":        "jpeg",
    "img_quality":    "85",
    "img_width":      "480",
    "model_path":     "weights/ONNX_FP32.onnx",
    "detect_conf":    "0.40",
    "detect_iou":     "0.20",
    "detect_imgsz":   "480",
    "keep_top1":      True,
    "output_dir":     "./data/screenshots",
    "save_raw":       False,
    "save_annotated": True,
}


def apply_active_model(model_id: int) -> None:
    """
    Called from UI context after set_active_model().
    Syncs detection_class table and updates all model-dependent settings
    (model_path, detect_imgsz) so the next monitoring start picks them up
    automatically — no manual Settings edits needed.
    """
    from app.db.database import get_model_by_id, sync_classes_from_model
    m = get_model_by_id(model_id)
    if not m:
        return
    sync_classes_from_model(model_id)
    s = get_settings()
    if m.get("onnx_path"):
        s["model_path"] = m["onnx_path"]
    if m.get("imgsz"):
        s["detect_imgsz"] = str(m["imgsz"])
    save_settings(s)


def get_settings() -> dict:
    """Return stored settings merged on top of defaults."""
    return {**DEFAULTS, **app.storage.general.get("settings", {})}


def save_settings(vals: dict) -> None:
    app.storage.general["settings"] = vals


def collect_cfg() -> dict:
    """Cast settings strings to the types MonitorController expects."""
    s = get_settings()
    return {
        **s,
        "port":         int(s["port"]),
        "start_wait":   int(s["start_wait"]),
        "interval":     float(s["interval"]),
        "img_quality":  int(s["img_quality"]),
        "img_width":    int(s["img_width"]),
        "detect_conf":  float(s["detect_conf"]),
        "detect_iou":   float(s["detect_iou"]),
        "detect_imgsz": int(s["detect_imgsz"]),
    }