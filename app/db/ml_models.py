"""
app/db/ml_models.py
───────────────────
ML model registry and training session CRUD.
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
    pt_path:     Optional[str] = None,
    onnx_path:   Optional[str] = None,
    map50:       float = 0.0,
    map50_95:    float = 0.0,
    precision:   float = 0.0,
    recall:      float = 0.0,
    status:      str   = "ready",
    imgsz:       int   = 640,
) -> int:
    c = _conn()
    cur = c.execute(
        "INSERT INTO ml_model "
        "(name, pt_path, onnx_path, nc, classes_json, map50, map50_95, "
        " precision, recall, status, imgsz, created_at, finished_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (name, pt_path, onnx_path, nc,
         json.dumps(class_names),
         map50, map50_95, precision, recall,
         status, imgsz, _now(), _now()),
    )
    c.commit()
    return cur.lastrowid


def update_ml_model(model_id: int, **kwargs) -> None:
    allowed = {
        "name", "pt_path", "onnx_path", "nc", "classes_json",
        "imgsz", "map50", "map50_95", "precision",
        "recall", "status", "finished_at", "is_active",
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
    c = _conn()
    c.execute("UPDATE ml_model SET is_active = 0")
    c.execute("UPDATE ml_model SET is_active = 1 WHERE id = ?", (model_id,))
    c.commit()


def list_models() -> list[dict]:
    """Return all models joined with their most recent training session."""
    c = _conn()
    rows = c.execute("""
        SELECT m.*,
            (SELECT ts.base_model FROM training_session ts
             WHERE ts.model_id = m.id
             ORDER BY ts.started_at DESC LIMIT 1) AS base_model
        FROM ml_model m
        ORDER BY m.created_at DESC
    """).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["class_names"] = json.loads(d.get("classes_json", "[]"))
        result.append(d)
    return result


def list_model_sessions(model_id: int) -> list[dict]:
    c = _conn()
    rows = c.execute(
        "SELECT * FROM training_session WHERE model_id = ? "
        "ORDER BY started_at DESC",
        (model_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def delete_model(model_id: int) -> None:
    c = _conn()
    c.execute("DELETE FROM ml_model WHERE id = ?", (model_id,))
    c.commit()


# ── Training sessions ─────────────────────────────────────────────────────────

def create_training_session(
    model_id:     int,
    dataset_path: str,
    base_model:   str,
    epochs:       int,
    imgsz:        int,
    batch:        int,
    device:       str,
) -> int:
    c = _conn()
    cur = c.execute(
        "INSERT INTO training_session "
        "(model_id, dataset_path, base_model, epochs, imgsz, batch, "
        " device, started_at) VALUES (?,?,?,?,?,?,?,?)",
        (model_id, dataset_path, base_model, epochs,
         imgsz, batch, device, _now()),
    )
    c.commit()
    return cur.lastrowid


def update_training_session(session_id: int, **kwargs) -> None:
    allowed = {"status", "finished_at"}
    sets    = ", ".join(f"{k}=?" for k in kwargs if k in allowed)
    vals    = [v for k, v in kwargs.items() if k in allowed]
    if not sets:
        return
    c = _conn()
    c.execute(
        f"UPDATE training_session SET {sets} WHERE id=?",
        [*vals, session_id],
    )
    c.commit()


# ── Class sync ────────────────────────────────────────────────────────────────

def sync_classes_from_model(model_id: int) -> None:
    """Upsert detection_class rows from a trained model's class list."""
    from app.core.colors import class_hex
    model = get_model_by_id(model_id)
    if not model:
        return
    names = model.get("class_names", [])
    c = _conn()
    for i, name in enumerate(names):
        c.execute("""
            INSERT INTO detection_class (class_index, name, color_hex, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(class_index) DO UPDATE SET
                name      = excluded.name,
                color_hex = excluded.color_hex
        """, (i, name, class_hex(i), _now()))
    c.commit()
