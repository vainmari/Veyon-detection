"""
app/pages/models.py
────────────────────
Models page  /models  (teacher + admin)

Layout
──────
  TOP     — GPU status card
  SECTION — Model Library  (expanded: classes, imgsz, base model, history)
  SECTION — Training Wizard (4 steps)

Fixes vs original
─────────────────
  • do_save used raw sqlite3 to patch name/imgsz/nc/classes_json — now uses
    update_ml_model() via the proper DB layer (those fields added to allowed set).
  • Removed duplicate _import_section() function that was defined at module
    level but never called (real import UI lives inside _model_library).
"""
from __future__ import annotations

import asyncio
import json
import os
import queue
import sys
import threading
from datetime import datetime
from pathlib import Path

import yaml
from nicegui import events, ui

import app.state as state
from app.config import apply_active_model
from app.core.auth import require_auth
from app.db.database import (
    create_ml_model,
    delete_model,
    get_model_by_id,
    list_model_sessions,
    list_models,
    set_active_model,
    update_ml_model,
)
from app.pages._nav import nav
from app.services.training_service import (
    BASE_MODELS,
    DATASETS_DIR,
    TrainingWorker,
    analyze_dataset,
    extract_zip,
    get_torch_info,
    install_cuda_torch,
    prepare_splits,
)


@ui.page("/models")
def page_models() -> None:
    current = require_auth(required_role="teacher_or_admin")
    if current is None:
        return
    nav(current)

    with ui.column().classes("w-full p-4 gap-6"):
        _gpu_card()
        ui.separator()
        _model_library()
        ui.separator()
        ui.label("Train New Model").classes("text-xl font-bold")
        _training_wizard()


# ── GPU status card ───────────────────────────────────────────────────────────

def _gpu_card() -> None:
    info = get_torch_info()

    with ui.card().classes("w-full"):
        with ui.row().classes("items-center gap-3 flex-wrap"):
            ui.label("PyTorch / GPU").classes("text-base font-semibold mr-2")

            if not info.get("installed"):
                ui.badge("PyTorch not found", color="red")
                return

            if info["cuda_ok"]:
                ui.badge(f"GPU  {info['gpu_name']}", color="green")
                ui.badge(f"CUDA {info['cuda_ver']}",  color="teal")
                ui.badge(f"torch {info['torch_ver']}", color="gray")
                ui.label("Training will use GPU ✅").classes(
                    "text-sm text-green-300 ml-2")
                return

            ui.badge("CPU only", color="orange")
            ui.badge(f"torch {info['torch_ver']}", color="gray")
            if info.get("sys_cuda"):
                ui.badge(f"NVIDIA CUDA {info['sys_cuda']} detected", color="blue")

        if not info["cuda_ok"]:
            ui.markdown(
                "GPU not available — training will be **slow** on CPU.  \n"
                "The button below uninstalls the CPU build and installs the "
                "CUDA build (~2.4 GB). The server keeps running during the "
                "download and restarts automatically when done."
            ).classes("text-sm text-gray-600 dark:text-gray-400 mt-2")

            spinner     = ui.spinner("dots", size="sm").classes("mt-2")
            spinner.set_visibility(False)
            dl_label    = ui.label("").classes("text-xs text-blue-300 font-mono")
            install_log = ui.log(max_lines=200).classes(
                "w-full font-mono text-xs bg-gray-950 text-green-300 rounded mt-1"
            ).style("height:160px; display:none;")
            install_btn = ui.button(
                "Install CUDA PyTorch", icon="download",
            ).props("color=primary")

            def do_install() -> None:
                install_btn.props("disable")
                spinner.set_visibility(True)
                dl_label.set_text("Starting…")
                install_log.style("display:block;")
                q: queue.Queue[str] = queue.Queue()
                threading.Thread(
                    target=install_cuda_torch, args=(q,), daemon=True
                ).start()
                downloading = [False]

                def _drain() -> None:
                    while not q.empty():
                        try:
                            line = q.get_nowait()
                        except queue.Empty:
                            break
                        install_log.push(line)
                        low = line.lower()
                        if "downloading" in low and not downloading[0]:
                            downloading[0] = True
                            parts = line.split("/")
                            fname = parts[-1].split("?")[0] if parts else ""
                            dl_label.set_text(
                                f"⬇  Downloading {fname or 'torch CUDA'} "
                                "— this takes several minutes…")
                        elif "installing" in low or "successfully installed" in low:
                            downloading[0] = False
                            dl_label.set_text("Installing packages…")
                        elif "✅" in line:
                            spinner.set_visibility(False)
                            dl_label.set_text(
                                "✅ Install complete — restarting in 3 s…")
                            drain_t.cancel()
                            def _restart() -> None:
                                import time; time.sleep(3)
                                os.execv(sys.executable,
                                         [sys.executable] + sys.argv)
                            threading.Thread(
                                target=_restart, daemon=True).start()
                        elif "❌" in line:
                            spinner.set_visibility(False)
                            dl_label.set_text(line)
                            drain_t.cancel()

                drain_t = ui.timer(0.5, _drain)

            install_btn.on_click(do_install)


# ── Shared helpers ────────────────────────────────────────────────────────────

def _parse_class_names(raw: str) -> list[str]:
    """Accept comma-separated text or a YAML names block, return a clean list."""
    raw = raw.strip()
    if not raw:
        return []
    try:
        parsed = yaml.safe_load(raw)
        if isinstance(parsed, dict) and "names" in parsed:
            return [str(n) for n in parsed["names"]]
        if isinstance(parsed, list):
            return [str(n) for n in parsed]
    except Exception:
        pass
    return [n.strip() for n in raw.split(",") if n.strip()]


# ── Model library ─────────────────────────────────────────────────────────────

@ui.refreshable
def _model_library() -> None:
    IMPORTED_DIR = Path("data/models/imported")
    IMPORTED_DIR.mkdir(parents=True, exist_ok=True)

    # ── Import dialog ─────────────────────────────────────────────────────────
    with ui.dialog() as import_dlg, \
         ui.card().classes("p-5 gap-3").style("min-width:560px; max-width:95vw;"):
        ui.label("Import Model").classes("text-lg font-bold mb-1")

        imp_msg  = ui.label("").classes("text-sm")
        imp_file = {"data": None, "name": ""}

        with ui.card().classes(
            "w-full bg-gray-50 border border-dashed border-gray-300 "
            "dark:bg-gray-800 dark:border-gray-500"
        ):
            ui.label("Option A — Upload .onnx / .pt").classes(
                "text-xs text-gray-500 dark:text-gray-400 mb-1")

            async def handle_model_upload(e: events.UploadEventArguments) -> None:
                raw  = await e.file.read()
                name = getattr(e, "name", "model.onnx")
                imp_file["data"] = raw
                imp_file["name"] = name
                imp_msg.set_text(f"✅  {name}  ({len(raw)//1024} KB)")
                imp_msg.classes(replace="text-sm text-green-400")

            ui.upload(
                label="Drop file here",
                on_upload=handle_model_upload,
                auto_upload=True,
            ).props("accept=.onnx,.pt flat").classes("w-full")

        ui.label("— or —").classes("text-center text-gray-500 text-xs my-1")

        with ui.card().classes("w-full bg-gray-50 dark:bg-gray-800"):
            ui.label("Option B — Server path").classes(
                "text-xs text-gray-500 dark:text-gray-400 mb-1")
            imp_path = ui.input(
                placeholder="e.g. weights/ONNX_FP32.onnx"
            ).props("dense outlined").classes("w-full")

        ui.separator().classes("my-2")

        with ui.row().classes("w-full items-center gap-3 py-0"):
            ui.label("Model name").classes("w-40 text-sm flex-shrink-0")
            imp_name = ui.input(placeholder="e.g. MyDetector_v1"
                                ).props("dense outlined").classes("flex-1")
        with ui.row().classes("w-full items-center gap-3 py-0"):
            ui.label("Image size").classes("w-40 text-sm flex-shrink-0")
            imp_imgsz = ui.select([320, 480, 640, 1280], value=640
                                  ).props("dense outlined").classes("w-28")
        with ui.row().classes("w-full items-center gap-3 py-0"):
            ui.label("Base / origin").classes("w-40 text-sm flex-shrink-0")
            imp_base = ui.input(placeholder="e.g. yolo11n.pt or external"
                                ).props("dense outlined").classes("flex-1")
        with ui.row().classes("w-full items-start gap-3 py-0"):
            ui.label("Classes").classes("w-40 text-sm mt-2 flex-shrink-0")
            with ui.column().classes("flex-1 gap-0"):
                imp_classes = ui.textarea(
                    placeholder=(
                        "Comma-separated:  DI, Narsykle\n"
                        "— or YAML names block —\n"
                        "names:\n  - DI\n  - Narsykle"
                    )
                ).props("dense outlined rows=4").classes("w-full font-mono text-xs")
                ui.label("Comma-separated or YAML names block"
                         ).classes("text-xs text-gray-500 dark:text-gray-500")

        ui.label("Metrics (optional)").classes("text-xs text-gray-400 mt-2")
        with ui.row().classes("gap-3 flex-wrap"):
            imp_map50   = ui.number(label="mAP50",    value=0.0,
                                    min=0, max=1, step=0.001, format="%.3f"
                                    ).props("dense outlined").classes("w-28")
            imp_map5095 = ui.number(label="mAP50-95", value=0.0,
                                    min=0, max=1, step=0.001, format="%.3f"
                                    ).props("dense outlined").classes("w-28")
            imp_prec    = ui.number(label="Precision", value=0.0,
                                    min=0, max=1, step=0.001, format="%.3f"
                                    ).props("dense outlined").classes("w-28")
            imp_recall  = ui.number(label="Recall",    value=0.0,
                                    min=0, max=1, step=0.001, format="%.3f"
                                    ).props("dense outlined").classes("w-28")

        with ui.row().classes("gap-2 mt-2"):
            async def do_import() -> None:
                if imp_file["data"]:
                    dst = IMPORTED_DIR / imp_file["name"]
                    dst.write_bytes(imp_file["data"])
                    onnx_path = str(dst)
                elif imp_path.value.strip():
                    p = Path(imp_path.value.strip())
                    if not p.exists():
                        imp_msg.set_text(f"❌  Not found: {p}")
                        imp_msg.classes(replace="text-sm text-red-400")
                        return
                    onnx_path = str(p)
                else:
                    imp_msg.set_text("❌  Provide a file or path.")
                    imp_msg.classes(replace="text-sm text-red-400")
                    return

                names = _parse_class_names(imp_classes.value)
                if not names:
                    imp_msg.set_text("❌  Enter at least one class name.")
                    imp_msg.classes(replace="text-sm text-red-400")
                    return
                if not imp_name.value.strip():
                    imp_msg.set_text("❌  Enter a model name.")
                    imp_msg.classes(replace="text-sm text-red-400")
                    return

                is_pt = onnx_path.endswith(".pt")
                try:
                    model_id = create_ml_model(
                        name        = imp_name.value.strip(),
                        nc          = len(names),
                        class_names = names,
                        pt_path     = onnx_path if is_pt else None,
                        onnx_path   = None if is_pt else onnx_path,
                        map50       = float(imp_map50.value   or 0),
                        map50_95    = float(imp_map5095.value or 0),
                        precision   = float(imp_prec.value    or 0),
                        recall      = float(imp_recall.value  or 0),
                        status      = "ready",
                        imgsz       = int(imp_imgsz.value),
                    )
                except Exception as ex:
                    imp_msg.set_text(f"❌  {ex}")
                    imp_msg.classes(replace="text-sm text-red-400")
                    return

                from app.db.database import create_training_session, update_training_session
                sess_id = create_training_session(
                    model_id     = model_id,
                    dataset_path = "— imported —",
                    base_model   = imp_base.value.strip() or "imported",
                    epochs=0, imgsz=int(imp_imgsz.value), batch=0, device="—",
                )
                update_training_session(
                    sess_id, status="done",
                    finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                )
                import_dlg.close()
                ui.notify(
                    f"✅  '{imp_name.value.strip()}' imported "
                    f"({len(names)} classes).",
                    type="positive")
                _model_library.refresh()

            ui.button("Import", icon="upload",
                      on_click=do_import).props("color=primary")
            ui.button("Cancel", on_click=import_dlg.close).props("flat")

    # ── Edit dialog ───────────────────────────────────────────────────────────
    with ui.dialog() as edit_dlg, \
         ui.card().classes("p-5 gap-3").style("min-width:520px; max-width:95vw;"):
        edit_title = ui.label("").classes("text-lg font-bold mb-1")
        edit_mid   = [None]

        with ui.row().classes("w-full items-center gap-3 py-0"):
            ui.label("Model name").classes("w-40 text-sm flex-shrink-0")
            edit_name = ui.input().props("dense outlined").classes("flex-1")
        with ui.row().classes("w-full items-center gap-3 py-0"):
            ui.label("Image size").classes("w-40 text-sm flex-shrink-0")
            edit_imgsz = ui.select([320, 480, 640, 1280], value=640
                                   ).props("dense outlined").classes("w-28")
        with ui.row().classes("w-full items-start gap-3 py-0"):
            ui.label("Classes").classes("w-40 text-sm mt-2 flex-shrink-0")
            with ui.column().classes("flex-1 gap-0"):
                edit_classes = ui.textarea().props(
                    "dense outlined rows=4"
                ).classes("w-full font-mono text-xs")
                ui.label("Comma-separated or YAML names block"
                         ).classes("text-xs text-gray-500 dark:text-gray-500")

        ui.label("Metrics").classes("text-xs text-gray-400 mt-2")
        with ui.row().classes("gap-3 flex-wrap"):
            edit_map50   = ui.number(label="mAP50",    min=0, max=1,
                                     step=0.001, format="%.3f"
                                     ).props("dense outlined").classes("w-28")
            edit_map5095 = ui.number(label="mAP50-95", min=0, max=1,
                                     step=0.001, format="%.3f"
                                     ).props("dense outlined").classes("w-28")
            edit_prec    = ui.number(label="Precision", min=0, max=1,
                                     step=0.001, format="%.3f"
                                     ).props("dense outlined").classes("w-28")
            edit_recall  = ui.number(label="Recall",    min=0, max=1,
                                     step=0.001, format="%.3f"
                                     ).props("dense outlined").classes("w-28")

        with ui.row().classes("gap-2 mt-2"):
            def do_save() -> None:
                mid = edit_mid[0]
                if mid is None:
                    return
                names = _parse_class_names(edit_classes.value)
                if not names:
                    ui.notify("Enter at least one class name.", type="negative")
                    return
                if not edit_name.value.strip():
                    ui.notify("Model name cannot be empty.", type="negative")
                    return
                # Use the proper DB layer — no raw sqlite3 here
                update_ml_model(
                    mid,
                    name         = edit_name.value.strip(),
                    imgsz        = int(edit_imgsz.value),
                    nc           = len(names),
                    classes_json = json.dumps(names),
                    map50        = float(edit_map50.value   or 0),
                    map50_95     = float(edit_map5095.value or 0),
                    precision    = float(edit_prec.value    or 0),
                    recall       = float(edit_recall.value  or 0),
                )
                edit_dlg.close()
                ui.notify("Model updated.", type="positive")
                _model_library.refresh()

            ui.button("Save", icon="save", on_click=do_save).props("color=primary")
            ui.button("Cancel", on_click=edit_dlg.close).props("flat")

    # ── Classes dialog ────────────────────────────────────────────────────────
    with ui.dialog() as cls_dlg, ui.card().classes("p-4 gap-3 min-w-80"):
        cls_dlg_title = ui.label("").classes("text-base font-bold mb-1")
        cls_dlg_body  = ui.column().classes("gap-1 w-full")
        ui.button("Close", on_click=cls_dlg.close).props("flat dense")

    # ── History dialog ────────────────────────────────────────────────────────
    with ui.dialog() as hist_dlg, \
         ui.card().classes("p-4 gap-3").style("min-width:640px; max-width:90vw;"):
        hist_dlg_title = ui.label("").classes("text-base font-bold mb-1")
        hist_table     = ui.table(
            columns=[
                {"name": "base",    "label": "Base model",
                 "field": "base_model", "sortable": True, "align": "left"},
                {"name": "epochs",  "label": "Epochs",
                 "field": "epochs",    "sortable": True, "align": "center"},
                {"name": "imgsz",   "label": "imgsz",
                 "field": "imgsz",     "sortable": True, "align": "center"},
                {"name": "batch",   "label": "Batch",
                 "field": "batch",     "sortable": True, "align": "center"},
                {"name": "device",  "label": "Device",
                 "field": "device",    "sortable": True, "align": "center"},
                {"name": "status",  "label": "Status",
                 "field": "status",    "sortable": True, "align": "center"},
                {"name": "started", "label": "Started",
                 "field": "started_at","sortable": True, "align": "left"},
                {"name": "done",    "label": "Finished",
                 "field": "finished_at","sortable": True, "align": "left"},
            ],
            rows=[], row_key="id",
        ).classes("w-full")
        hist_table.props("dense flat bordered")
        ui.button("Close", on_click=hist_dlg.close).props("flat dense")

    # ── Library card ──────────────────────────────────────────────────────────
    with ui.card().classes("w-full"):
        with ui.row().classes("items-center justify-between mb-2"):
            ui.label("Model Library").classes("text-lg font-bold")
            with ui.row().classes("gap-2"):
                ui.button("Import Model", icon="upload",
                          on_click=import_dlg.open).props("flat color=primary dense")
                ui.button(icon="refresh",
                          on_click=_model_library.refresh).props("flat round dense")

        models = list_models()
        if not models:
            ui.label("No models yet. Train or import one.").classes(
                "text-gray-500 text-sm")
            return

        cols = [
            {"name": "name",     "label": "Name",
             "field": "name",     "sortable": True, "align": "left"},
            {"name": "base",     "label": "Base",
             "field": "base_model","sortable": True, "align": "left"},
            {"name": "classes",  "label": "Classes",
             "field": "nc",       "sortable": True, "align": "center"},
            {"name": "imgsz",    "label": "imgsz",
             "field": "imgsz",    "sortable": True, "align": "center"},
            {"name": "map50",    "label": "mAP50",
             "field": "map50",    "sortable": True, "align": "center"},
            {"name": "map50_95", "label": "mAP50-95",
             "field": "map50_95", "sortable": True, "align": "center"},
            {"name": "status",   "label": "Status",
             "field": "status",   "sortable": True, "align": "center"},
            {"name": "active",   "label": "Active",
             "field": "is_active","align": "center"},
            {"name": "created",  "label": "Trained",
             "field": "created_at","sortable": True, "align": "left"},
            {"name": "actions",  "label": "",
             "field": "id",       "align": "right"},
        ]
        rows = [
            {**m,
             "map50":    f"{m['map50']:.3f}"    if m.get("map50")    else "—",
             "map50_95": f"{m['map50_95']:.3f}" if m.get("map50_95") else "—",
             "base_model": m.get("base_model") or "—",
             "imgsz":    m.get("imgsz") or 640}
            for m in models
        ]
        tbl = ui.table(columns=cols, rows=rows, row_key="id").classes("w-full")
        tbl.props("dense flat bordered")

        tbl.add_slot("body-cell-active", """
            <q-td :props="props">
                <q-icon v-if="props.row.is_active"
                        name="check_circle" color="green" size="sm"/>
            </q-td>""")
        tbl.add_slot("body-cell-status", """
            <q-td :props="props">
                <q-badge :color="props.row.status==='ready' ? 'green' :
                                 props.row.status==='training' ? 'orange' : 'red'">
                    {{ props.row.status }}
                </q-badge>
            </q-td>""")
        tbl.add_slot("body-cell-classes", """
            <q-td :props="props">
                <q-badge color="blue" class="q-mr-xs">{{ props.row.nc }}</q-badge>
                <q-btn flat dense round icon="list" size="xs" color="gray"
                       @click="$parent.$emit('show_classes', props.row)"/>
            </q-td>""")
        tbl.add_slot("body-cell-actions", """
            <q-td :props="props">
                <q-btn flat dense round icon="play_arrow" color="green"
                       title="Set as active"
                       @click="$parent.$emit('set_active', props.row)"/>
                <q-btn flat dense round icon="edit" color="blue"
                       title="Edit"
                       @click="$parent.$emit('edit_model', props.row)"/>
                <q-btn flat dense round icon="history" color="teal"
                       title="Training history"
                       @click="$parent.$emit('show_history', props.row)"/>
                <q-btn flat dense round icon="delete" color="red"
                       title="Delete"
                       @click="$parent.$emit('del_model', props.row)"/>
            </q-td>""")

        def on_set_active(e) -> None:
            mid = e.args.get("id")
            if not mid:
                return
            set_active_model(int(mid))
            apply_active_model(int(mid))
            m = get_model_by_id(int(mid))
            ui.notify(
                f"✅  '{m['name'] if m else mid}' is now active — "
                "restart monitoring to apply.", type="positive")
            _model_library.refresh()

        def on_edit_model(e) -> None:
            row = e.args
            mid = row.get("id")
            if not mid:
                return
            m = get_model_by_id(int(mid))
            if not m:
                return
            edit_mid[0] = int(mid)
            edit_title.set_text(f"Edit — {m['name']}")
            edit_name.set_value(m["name"])
            edit_imgsz.set_value(m.get("imgsz") or 640)
            edit_map50.set_value(m.get("map50") or 0.0)
            edit_map5095.set_value(m.get("map50_95") or 0.0)
            edit_prec.set_value(m.get("precision") or 0.0)
            edit_recall.set_value(m.get("recall") or 0.0)
            edit_classes.set_value(", ".join(m.get("class_names") or []))
            edit_dlg.open()

        def on_show_classes(e) -> None:
            row = e.args
            m = get_model_by_id(int(row["id"]))
            names = m.get("class_names", []) if m else []
            cls_dlg_title.set_text(f"{row.get('name', '')} — {len(names)} classes")
            cls_dlg_body.clear()
            with cls_dlg_body:
                from app.core.colors import class_hex
                for i, n in enumerate(names):
                    with ui.row().classes("items-center gap-2"):
                        ui.element("div").style(
                            f"width:12px; height:12px; border-radius:50%;"
                            f" background:{class_hex(i)}; flex-shrink:0;")
                        ui.label(f"{i}  {n}").classes("text-sm font-mono")
            cls_dlg.open()

        def on_show_history(e) -> None:
            row = e.args
            mid = row.get("id")
            hist_dlg_title.set_text(f"History — {row.get('name', '')}")
            hist_table.rows = list_model_sessions(int(mid)) if mid else []
            hist_table.update()
            hist_dlg.open()

        def on_delete(e) -> None:
            mid = e.args.get("id")
            if mid:
                delete_model(int(mid))
                ui.notify("Model deleted.", type="warning")
                _model_library.refresh()

        tbl.on("set_active",   on_set_active)
        tbl.on("edit_model",   on_edit_model)
        tbl.on("show_classes", on_show_classes)
        tbl.on("show_history", on_show_history)
        tbl.on("del_model",    on_delete)


# ── Training wizard ───────────────────────────────────────────────────────────

def _training_wizard() -> None:
    ws: dict = {
        "step":        "upload",
        "dataset_dir": None,
        "analysis":    None,
        "yaml_path":   None,
        "model_id":    None,
    }
    if state.training_worker and state.training_worker.is_running:
        ws["step"] = "training"

    @ui.refreshable
    def wizard_body() -> None:
        s = ws["step"]
        if   s == "upload":   _step_upload(ws, wizard_body)
        elif s == "analysis": _step_analysis(ws, wizard_body)
        elif s == "training": _step_training(ws, wizard_body)
        elif s == "done":     _step_done(ws, wizard_body)

    wizard_body()


# ── Step 1: Upload ────────────────────────────────────────────────────────────

def _step_upload(ws: dict, refresh) -> None:
    with ui.card().classes("w-full max-w-2xl"):
        ui.label("Step 1 — Upload Dataset").classes("text-base font-semibold mb-1")
        ui.markdown(
            "Upload a **YOLO-format** ZIP (`data.yaml` + `images/` + `labels/`) "
            "or a **COCO-format** ZIP (`annotations/*.json` + image folders).  \n"
            "Missing `val` or `test` splits are created automatically. "
            "COCO annotations are converted to YOLO format automatically."
        ).classes("text-sm text-gray-400 mb-3")

        msg_lbl    = ui.label("").classes("text-sm")
        upload_ref = {"data": None, "name": ""}

        with ui.card().classes(
            "w-full bg-gray-50 border border-dashed border-gray-300 "
            "dark:bg-gray-800 dark:border-gray-500"
        ):
            ui.label("Option A — Upload ZIP").classes("text-xs text-gray-500 dark:text-gray-400 mb-2")

            async def handle_upload(e: events.UploadEventArguments) -> None:
                raw  = await e.file.read()
                name = getattr(e, "name", "upload.zip")
                upload_ref["data"] = raw
                upload_ref["name"] = name
                msg_lbl.set_text(f"✅  {name}  ({len(raw)//1024} KB loaded)")
                msg_lbl.classes(replace="text-sm text-green-400")

            ui.upload(
                label="Drop dataset ZIP here",
                on_upload=handle_upload,
                auto_upload=True,
            ).props("accept=.zip flat").classes("w-full")

        ui.label("— or —").classes("text-center text-gray-500 text-sm my-1")

        with ui.card().classes("w-full bg-gray-50 dark:bg-gray-800"):
            ui.label("Option B — Local server path to extracted dataset"
                     ).classes("text-xs text-gray-500 dark:text-gray-400 mb-1")
            path_input = ui.input(
                placeholder="e.g. C:/datasets/my_dataset"
            ).props("dense outlined").classes("w-full")

        async def do_analyze() -> None:
            msg_lbl.set_text("Analysing dataset…")
            msg_lbl.classes(replace="text-sm text-yellow-400")
            if upload_ref["data"]:
                ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
                dest = DATASETS_DIR / ts
                def _work():
                    d = extract_zip(upload_ref["data"], dest)
                    return str(d), analyze_dataset(str(d))
                ds_dir, result = await asyncio.get_event_loop().run_in_executor(
                    None, _work)
            elif path_input.value.strip():
                ds_dir = path_input.value.strip()
                result = await asyncio.get_event_loop().run_in_executor(
                    None, analyze_dataset, ds_dir)
            else:
                msg_lbl.set_text("Provide a ZIP upload or a local path.")
                msg_lbl.classes(replace="text-sm text-red-400")
                return
            if not result["ok"]:
                msg_lbl.set_text(f"❌  {result['error']}")
                msg_lbl.classes(replace="text-sm text-red-400")
                return
            ws["dataset_dir"] = ds_dir
            ws["analysis"]    = result
            ws["step"]        = "analysis"
            refresh.refresh()

        ui.button("Analyse Dataset →", on_click=do_analyze).props(
            "color=primary").classes("mt-2")


# ── Step 2: Analysis + config ─────────────────────────────────────────────────

def _step_analysis(ws: dict, refresh) -> None:
    a = ws["analysis"]

    with ui.column().classes("w-full gap-4"):
        ui.label("Step 2 — Review Dataset & Configure Training").classes(
            "text-base font-semibold")

        with ui.row().classes("gap-4 flex-wrap"):
            fmt = a.get("source_format", "yolo").upper()
            fmt_color = "orange" if fmt == "COCO" else "blue"
            for label, val in [
                ("Total images",  str(a["total_images"])),
                ("Classes",       str(a["nc"])),
                ("Splits found",  ", ".join(a["splits"].keys()) or "—"),
            ]:
                with ui.card().classes("bg-gray-800 px-4 py-3 min-w-36"):
                    ui.label(label).classes("text-xs text-gray-400")
                    ui.label(val).classes("text-xl font-bold text-blue-300")
            with ui.card().classes("bg-gray-800 px-4 py-3 min-w-36"):
                ui.label("Source format").classes("text-xs text-gray-400")
                ui.badge(fmt, color=fmt_color).classes("text-sm mt-1")
            if fmt == "COCO":
                with ui.card().classes(
                    "bg-orange-50 border border-orange-300 px-4 py-3 "
                    "dark:bg-orange-950 dark:border-orange-700"
                ):
                    ui.label("COCO → YOLO").classes(
                        "text-xs text-orange-700 dark:text-orange-300 font-semibold")
                    ui.label("Converted automatically").classes(
                        "text-xs text-orange-600 dark:text-orange-200")

        with ui.card().classes("w-full"):
            ui.label("Splits").classes("text-sm font-semibold text-gray-600 dark:text-gray-400 mb-2")
            with ui.row().classes("gap-4 flex-wrap"):
                for split, info in a["splits"].items():
                    with ui.card().classes("bg-gray-800 px-3 py-2 min-w-28"):
                        ui.label(split).classes("text-xs text-gray-400 uppercase")
                        ui.label(f"{info['images']} images").classes(
                            "text-sm font-mono text-green-300")

        cc = a["class_counts"]
        with ui.card().classes("w-full"):
            ui.label("Class Distribution").classes(
                "text-sm font-semibold text-gray-600 dark:text-gray-400 mb-2")
            ui.echart({
                "tooltip": {"trigger": "axis",
                            "axisPointer": {"type": "shadow"}},
                "grid":    {"left": "3%", "right": "4%",
                            "bottom": "3%", "containLabel": True},
                "xAxis":   {"type": "value",
                            "axisLabel": {"color": "#aaa"}},
                "yAxis":   {"type": "category",
                            "data": [c["name"] for c in cc],
                            "axisLabel": {"color": "#ccc"}},
                "series":  [{"type": "bar",
                             "data": [c["count"] for c in cc],
                             "itemStyle": {"color": "#4b8cf5"},
                             "barMaxWidth": 28,
                             "label": {"show": True, "position": "right",
                                       "color": "#aaa", "fontSize": 11}}],
            }).classes("w-full").style(
                f"height:{max(180, len(cc) * 32)}px")

        if a["warnings"]:
            with ui.card().classes(
                "w-full bg-yellow-50 border border-yellow-300 "
                "dark:bg-yellow-950 dark:border-yellow-700"
            ):
                ui.label("Warnings").classes(
                    "text-sm font-semibold text-yellow-700 dark:text-yellow-300 mb-1")
                for w in a["warnings"]:
                    ui.label(w).classes("text-sm text-yellow-800 dark:text-yellow-200")

        with ui.card().classes("w-full max-w-xl"):
            ui.label("Training Configuration").classes(
                "text-sm font-semibold text-gray-600 dark:text-gray-400 mb-3")

            with ui.row().classes("w-full items-center gap-4 py-1"):
                ui.label("Run name").classes("w-44 text-sm")
                f_name = ui.input(
                    value=f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
                ).props("dense outlined").classes("flex-1")
            with ui.row().classes("w-full items-center gap-4 py-1"):
                ui.label("Base model").classes("w-44 text-sm")
                f_base = ui.select(BASE_MODELS, value=BASE_MODELS[0]).props(
                    "dense outlined").classes("w-44")
            with ui.row().classes("w-full items-center gap-4 py-1"):
                ui.label("Epochs").classes("w-44 text-sm")
                f_epochs = ui.number(value=100, min=1, max=1000).props(
                    "dense outlined").classes("w-32")
            with ui.row().classes("w-full items-center gap-4 py-1"):
                ui.label("Image size").classes("w-44 text-sm")
                f_imgsz = ui.select([320, 480, 640, 1280], value=640).props(
                    "dense outlined").classes("w-32")
            with ui.row().classes("w-full items-center gap-4 py-1"):
                ui.label("Batch size  (-1 = auto)").classes("w-44 text-sm")
                f_batch = ui.number(value=16, min=-1, max=256).props(
                    "dense outlined").classes("w-32")

        with ui.row().classes("gap-3"):
            ui.button("← Back", on_click=lambda: (
                ws.__setitem__("step", "upload"), refresh.refresh()
            )).props("flat")

            async def do_start() -> None:
                yaml_p = await asyncio.get_event_loop().run_in_executor(
                    None, prepare_splits, a)
                ws["yaml_path"] = yaml_p
                config = {
                    "yaml_path":  yaml_p,
                    "nc":         a["nc"],
                    "names":      a["names"],
                    "base_model": f_base.value,
                    "epochs":     int(f_epochs.value),
                    "imgsz":      int(f_imgsz.value),
                    "batch":      int(f_batch.value),
                    "run_name":   (f_name.value.strip() or
                                   f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}"),
                }
                worker = TrainingWorker()
                worker.start(config)
                state.training_worker = worker
                ws["step"] = "training"
                refresh.refresh()

            ui.button("Start Training ▶", on_click=do_start).props("color=green")


# ── Step 3: Live training ─────────────────────────────────────────────────────

def _step_training(ws: dict, refresh) -> None:
    worker: TrainingWorker = state.training_worker

    with ui.column().classes("w-full gap-4"):
        ui.label("Step 3 — Training in Progress").classes(
            "text-base font-semibold")

        with ui.card().classes("w-full"):
            status_lbl = ui.label("").classes(
                "text-sm font-mono text-yellow-300 mb-1")
            epoch_lbl  = ui.label("").classes("text-xs text-gray-500 dark:text-gray-400 mb-1")
            ui.label("Epoch progress").classes("text-xs text-gray-500 dark:text-gray-500 mb-0")
            prog_epoch = ui.linear_progress(value=0).props("color=green")
            ui.label("Batch progress (current epoch)").classes(
                "text-xs text-gray-500 mt-2 mb-0")
            prog_batch = ui.linear_progress(value=0).props("color=blue")
            batch_lbl  = ui.label("").classes("text-xs text-gray-500 dark:text-gray-500 mt-0")

        with ui.card().classes("w-full"):
            ui.label("Validation mAP (after each epoch)").classes(
                "text-sm font-semibold text-gray-600 dark:text-gray-400 mb-1")
            map_chart = ui.echart({
                "tooltip": {"trigger": "axis"},
                "legend":  {"data": ["mAP50", "mAP50-95"],
                            "textStyle": {"color": "#ccc"}},
                "grid":    {"left": "3%", "right": "4%",
                            "bottom": "3%", "containLabel": True},
                "xAxis":   {"type": "category", "data": [],
                            "name": "Epoch",
                            "axisLabel": {"color": "#aaa"}},
                "yAxis":   {"type": "value", "min": 0, "max": 1,
                            "axisLabel": {"color": "#aaa"}},
                "series":  [
                    {"name": "mAP50",    "type": "line", "smooth": True,
                     "data": [], "itemStyle": {"color": "#4ec9b0"},
                     "showSymbol": False},
                    {"name": "mAP50-95", "type": "line", "smooth": True,
                     "data": [], "itemStyle": {"color": "#4b8cf5"},
                     "showSymbol": False},
                ],
            }).classes("w-full").style("height:240px")

        with ui.card().classes("w-full"):
            ui.label("Training Loss (per epoch)").classes(
                "text-sm font-semibold text-gray-600 dark:text-gray-400 mb-1")
            loss_chart = ui.echart({
                "tooltip": {"trigger": "axis"},
                "legend":  {"data": ["box", "cls", "dfl"],
                            "textStyle": {"color": "#ccc"}},
                "grid":    {"left": "3%", "right": "4%",
                            "bottom": "3%", "containLabel": True},
                "xAxis":   {"type": "category", "data": [],
                            "name": "Epoch",
                            "axisLabel": {"color": "#aaa"}},
                "yAxis":   {"type": "value",
                            "axisLabel": {"color": "#aaa"}},
                "series":  [
                    {"name": "box", "type": "line", "smooth": True,
                     "data": [], "itemStyle": {"color": "#f56262"},
                     "showSymbol": False},
                    {"name": "cls", "type": "line", "smooth": True,
                     "data": [], "itemStyle": {"color": "#dcdcaa"},
                     "showSymbol": False},
                    {"name": "dfl", "type": "line", "smooth": True,
                     "data": [], "itemStyle": {"color": "#c084fc"},
                     "showSymbol": False},
                ],
            }).classes("w-full").style("height:240px")

        ui.button("Cancel", on_click=lambda: _cancel(ws, refresh)).props(
            "flat color=red")

    epochs_x = []
    map50_y  = []; m5095_y = []
    box_y    = []; cls_y   = []; dfl_y = []

    def _apply_history(msgs: list[dict]) -> None:
        for msg in msgs:
            if msg.get("type") != "epoch":
                continue
            ep = str(msg["epoch"])
            if ep not in epochs_x:
                epochs_x.append(ep)
            map50_y.append(round(msg["map50"],    4))
            m5095_y.append(round(msg["map50_95"], 4))
            box_y.append(  round(msg["box_loss"], 5))
            cls_y.append(  round(msg["cls_loss"], 5))
            dfl_y.append(  round(msg["dfl_loss"], 5))

    def _redraw() -> None:
        map_chart.options["xAxis"]["data"]     = epochs_x
        map_chart.options["series"][0]["data"] = map50_y
        map_chart.options["series"][1]["data"] = m5095_y
        map_chart.update()
        loss_chart.options["xAxis"]["data"]     = epochs_x
        loss_chart.options["series"][0]["data"] = box_y
        loss_chart.options["series"][1]["data"] = cls_y
        loss_chart.options["series"][2]["data"] = dfl_y
        loss_chart.update()

    if worker and worker.epoch_history:
        _apply_history(worker.epoch_history)
        _redraw()
        last = worker.epoch_history[-1]
        status_lbl.set_text(worker.current_status)
        epoch_lbl.set_text(f"Epoch {last['epoch']} / {last['total']}")
        prog_epoch.set_value(
            last["epoch"] / last["total"] if last["total"] else 0)
    elif worker:
        status_lbl.set_text(worker.current_status)

    def tick() -> None:
        if worker is None:
            return
        bp = worker.batch_progress
        if bp["total_batches"] > 0:
            prog_batch.set_value(bp["batch"] / bp["total_batches"])
            batch_lbl.set_text(
                f"Batch {bp['batch']} / {bp['total_batches']}  "
                f"(epoch {bp['epoch']} / {bp['total_epochs']})")
        changed = False
        while not worker.progress_q.empty():
            try:
                msg = worker.progress_q.get_nowait()
            except Exception:
                break
            t = msg.get("type")
            if t == "status":
                status_lbl.set_text(msg["message"])
            elif t == "epoch":
                ep_str = str(msg["epoch"])
                if ep_str not in epochs_x:
                    epochs_x.append(ep_str)
                map50_y.append(round(msg["map50"],    4))
                m5095_y.append(round(msg["map50_95"], 4))
                box_y.append(  round(msg["box_loss"], 5))
                cls_y.append(  round(msg["cls_loss"], 5))
                dfl_y.append(  round(msg["dfl_loss"], 5))
                status_lbl.set_text(worker.current_status)
                prog_epoch.set_value(
                    msg["epoch"] / msg["total"] if msg["total"] else 0)
                epoch_lbl.set_text(
                    f"Epoch {msg['epoch']} / {msg['total']}  |  "
                    f"mAP50 {msg['map50']:.3f}  |  "
                    f"Precision {msg['precision']:.3f}  |  "
                    f"Recall {msg['recall']:.3f}")
                changed = True
            elif t == "done":
                ws["model_id"] = msg["model_id"]
                ws["step"]     = "done"
                timer.cancel()
                _model_library.refresh()
                refresh.refresh()
                return
            elif t == "error":
                status_lbl.set_text(f"❌  {msg['message']}")
                status_lbl.classes(replace="text-sm font-mono text-red-400")
                timer.cancel()
                return
            elif t == "cancelled":
                status_lbl.set_text("Training cancelled.")
                timer.cancel()
                return
        if changed:
            _redraw()

    timer = ui.timer(0.5, tick)


def _cancel(ws: dict, refresh) -> None:
    if state.training_worker:
        state.training_worker.cancel()
        state.training_worker = None
    ws["step"] = "upload"
    refresh.refresh()


# ── Step 4: Done ──────────────────────────────────────────────────────────────

def _step_done(ws: dict, refresh) -> None:
    mid   = ws.get("model_id")
    model = get_model_by_id(mid) if mid else None

    with ui.card().classes("w-full max-w-xl"):
        ui.icon("check_circle").classes("text-green-400 text-5xl mb-2")
        ui.label("Training Complete!").classes("text-xl font-bold text-green-300")

        if model:
            with ui.column().classes("gap-1 mt-2"):
                for label, val in [
                    ("Model name",  model["name"]),
                    ("Base model",  model.get("base_model") or "—"),
                    ("Classes",     str(model["nc"])),
                    ("imgsz",       str(model.get("imgsz") or 640)),
                    ("mAP50",       f"{model['map50']:.3f}"),
                    ("mAP50-95",    f"{model['map50_95']:.3f}"),
                    ("Precision",   f"{model['precision']:.3f}"),
                    ("Recall",      f"{model['recall']:.3f}"),
                    ("ONNX path",   model.get("onnx_path") or "—"),
                ]:
                    with ui.row().classes("gap-3"):
                        ui.label(f"{label}:").classes("text-gray-400 text-sm w-28")
                        ui.label(val).classes("text-sm font-mono")

        with ui.row().classes("gap-3 mt-4"):
            if model:
                def activate() -> None:
                    set_active_model(model["id"])
                    apply_active_model(model["id"])
                    ui.notify(
                        "Model set as active — restart monitoring to apply.",
                        type="positive")
                    _model_library.refresh()
                ui.button("Set as Active Model",
                          on_click=activate).props("color=green")

            def new_run() -> None:
                state.training_worker = None
                ws.update({"step": "upload", "analysis": None, "model_id": None})
                refresh.refresh()
            ui.button("Train Another Model", on_click=new_run).props("flat")