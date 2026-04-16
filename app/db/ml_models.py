"""
app/db/ml_models.py
───────────────────
ML model registry.  Training configuration (dataset_path, base_model, epochs,
batch, device) is stored directly on the ml_model row — no separate
training_session table.

sync_classes_from_model() lives here because it reads model data and updates
detection_class in one operation that belongs to the model lifecycle.
"""
from __future__ import annotations

import json
from typing import Optional

from app.db._core import _conn, _now


# ── ML Models ─────────────────────────────────────────────────────────────────

def create_ml_model(
    name:        str,
    nc:          int,
    class_names: list[str],
    pt_path:     Optional[str]   = None,
    onnx_path:   Optional[str]   = None,
    map50:       float           = 0.0,
    map50_95:    float           = 0.0,
    precision:   float           = 0.0,
    recall:      float           = 0.0,
    status:      str             = "ready",
    imgsz:       int             = 640,
    dataset_path: Optional[str] = None,
    base_model:   Optional[str] = None,
    epochs:       Optional[int] = None,
    batch:        Optional[int] = None,
    device:       Optional[str] = None,
) -> int:
    c = _conn()
    now = _now()
    cur = c.execute(
        "INSERT INTO ml_model "
        "(name, pt_path, onnx_path, nc, classes_json, map50, map50_95, "
        " precision, recall, status, imgsz, "
        " dataset_path, base_model, epochs, batch, device, "
        " created_at, finished_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (name, pt_path, onnx_path, nc,
         json.dumps(class_names),
         map50, map50_95, precision, recall,
         status, imgsz,
         dataset_path, base_model, epochs, batch, device,
         now, now),
    )
    c.commit()
    return cur.lastrowid


def update_ml_model(model_id: int, **kwargs) -> None:
    allowed = {
        "name", "pt_path", "onnx_path", "nc", "classes_json",
        "imgsz", "map50", "map50_95", "precision", "recall",
        "status", "finished_at", "is_active",
        "dataset_path", "base_model", "epochs", "batch", "device",
    }
    sets = ", ".join(f"{k}=?" for k in kwargs if k in allowed)
    vals = [v for k, v in kwargs.items() if k in allowed]
    if not sets:
        return
    c = _conn()
    c.execute(f"UPDATE ml_model SET {sets} WHERE id=?", [*vals, model_id])
    c.commit()


def get_active_model() -> Optional[dict]:
    c = _conn()
    row = c.execute(
        "SELECT * FROM ml_model WHERE is_active = 1 LIMIT 1"
    ).fetchone()
    if not row:
        return None
    d = dict(row)
    d["class_names"] = json.loads(d.get("classes_json", "[]"))
    return d


def get_model_by_id(model_id: int) -> Optional[dict]:
    c = _conn()
    row = c.execute(
        "SELECT * FROM ml_model WHERE id = ?", (model_id,)
    ).fetchone()
    if not row:
        return None
    d = dict(row)
    d["class_names"] = json.loads(d.get("classes_json", "[]"))
    return d


def set_active_model(model_id: int) -> None:
    from app.db.audit import _insert_audit
    c = _conn()
    c.execute("UPDATE ml_model SET is_active = 0")
    c.execute("UPDATE ml_model SET is_active = 1 WHERE id = ?", (model_id,))
    _insert_audit(c, "model.activate", entity="ml_model", entity_id=model_id)
    c.commit()


def list_models() -> list[dict]:
    """Return all models ordered by creation date, newest first."""
    c = _conn()
    rows = c.execute(
        "SELECT * FROM ml_model ORDER BY created_at DESC"
    ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["class_names"] = json.loads(d.get("classes_json", "[]"))
        result.append(d)
    return result


def delete_model(model_id: int) -> None:
    c = _conn()
    c.execute("DELETE FROM ml_model WHERE id = ?", (model_id,))
    c.commit()


# ── Class sync ────────────────────────────────────────────────────────────────

def sync_classes_from_model(model_id: int) -> None:
    """
    Upsert detection_class rows from a trained model's class list, then
    record the model-specific index mapping in model_class.

    detection_class is keyed by name — two models that share a class name
    reuse the same detection_class row (and its color / notification settings).
    model_class maps each model's raw output index to the correct class row so
    models with identical classes at different indices never corrupt each other.
    """
    from app.core.colors import class_hex
    model = get_model_by_id(model_id)
    if not model:
        return
    names = model.get("class_names", [])
    c = _conn()
    for i, name in enumerate(names):
        # Upsert detection_class by name (not by index).
        c.execute("""
            INSERT INTO detection_class (name, color_hex, created_at)
            VALUES (?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                color_hex = excluded.color_hex
        """, (name, class_hex(i), _now()))

        dc_id = c.execute(
            "SELECT id FROM detection_class WHERE name = ?", (name,)
        ).fetchone()[0]

        # Record this model's index → class mapping.
        c.execute("""
            INSERT INTO model_class (model_id, class_index, class_id)
            VALUES (?, ?, ?)
            ON CONFLICT(model_id, class_index) DO UPDATE SET
                class_id = excluded.class_id
        """, (model_id, i, dc_id))
    c.commit()
