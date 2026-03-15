"""
app/pages/history.py
────────────────────
History page  /history  — searchable detection log table.
"""
from nicegui import ui

from app.db.database import query_detections
from app.pages._nav import nav


@ui.page("/history")
def page_history() -> None:
    nav()

    with ui.column().classes("w-full p-4 gap-4"):
        ui.label("Detection History").classes("text-xl font-bold")

        with ui.card().classes("w-full"):
            with ui.row().classes("gap-4 flex-wrap items-end"):
                f_pc  = ui.input("Computer") .props("dense outlined clearable").classes("w-40")
                f_cls = ui.input("Class name").props("dense outlined clearable").classes("w-40")
                f_lim = ui.number(
                    "Max rows", value=100, min=10, max=2000
                ).props("dense outlined").classes("w-28")
                ui.button("🔍 Search", on_click=lambda: _load()).props("color=primary")

        cols = [
            {"name": "detected_at", "label": "Time",       "field": "detected_at",
             "sortable": True, "align": "left"},
            {"name": "computer",    "label": "Computer",   "field": "computer",
             "sortable": True, "align": "left"},
            {"name": "class_name",  "label": "Class",      "field": "class_name",
             "sortable": True, "align": "left"},
            {"name": "confidence",  "label": "Confidence", "field": "confidence",
             "sortable": True, "align": "left"},
        ]
        tbl   = ui.table(columns=cols, rows=[], row_key="id").classes("w-full")
        tbl.props("dense flat bordered")
        count = ui.label("").classes("text-xs text-gray-500 mt-1")

    def _load() -> None:
        rows = query_detections(
            computer=f_pc.value or "",
            class_name=f_cls.value or "",
            limit=int(f_lim.value or 100),
        )
        for r in rows:
            r["confidence"] = f"{r['confidence']:.0%}"
        tbl.rows = rows
        tbl.update()
        count.set_text(f"{len(rows)} record(s)")

    _load()