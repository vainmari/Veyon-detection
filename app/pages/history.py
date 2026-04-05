"""
app/pages/history.py
────────────────────
History page  /history

Teacher view  — full filters (computer, student, class, hits-only)
Student view  — auto-filtered to own records, computer selector hidden
Both views    — 📷 opens snapshot preview; ⛶ Full screen opens maximized dialog.
"""
from nicegui import ui

from app.core.auth import require_auth
from app.db.database import (
    get_event_frame_b64,
    get_event_frame_annotated_b64,
    list_classes,
    list_computers,
    list_users,
    query_events,
)
from app.pages._nav import nav
from app.pages._snapshot import make_snapshot_dialogs


@ui.page("/history")
def page_history() -> None:
    current = require_auth()
    if current is None:
        return
    nav(current)
    is_teacher = current["role"] == "teacher"

    _snap_dlg, _fs_dlg, show_snapshot = make_snapshot_dialogs()

    with ui.column().classes("w-full p-4 gap-4"):
        ui.label(
            "Detection History" if is_teacher else "My Detection History"
        ).classes("text-xl font-bold")

        # ── Filter bar ────────────────────────────────────────────────────────
        with ui.card().classes("w-full"):
            with ui.row().classes("gap-4 flex-wrap items-end"):

                if is_teacher:
                    computers = [{"label": "All computers", "value": ""}] + [
                        {"label": c["name"], "value": str(c["id"])}
                        for c in list_computers()
                    ]
                    f_computer = ui.select(
                        {c["value"]: c["label"] for c in computers},
                        value="", label="Computer",
                    ).props("dense outlined").classes("w-44")

                    students = [{"label": "All students", "value": ""}] + [
                        {"label": u["username"], "value": str(u["id"])}
                        for u in list_users() if u["role"] == "student"
                    ]
                    f_student = ui.select(
                        {s["value"]: s["label"] for s in students},
                        value="", label="Student",
                    ).props("dense outlined").classes("w-44")

                classes = [{"label": "All classes", "value": ""}] + [
                    {"label": c["name"], "value": c["name"]}
                    for c in list_classes()
                ]
                f_class = ui.select(
                    {c["value"]: c["label"] for c in classes},
                    value="", label="Class",
                ).props("dense outlined").classes("w-44")

                f_only_hits = ui.checkbox("Detections only", value=False)

                f_lim = ui.number(
                    "Max rows", value=200, min=10, max=5000,
                ).props("dense outlined").classes("w-24")

                ui.button("🔍 Search", on_click=lambda: _load()).props("color=primary")

        # ── Table ─────────────────────────────────────────────────────────────
        base_cols = [
            {"name": "detected_at",    "label": "Time",
             "field": "detected_at",   "sortable": True,  "align": "left"},
            {"name": "had_detection",  "label": "Hit",
             "field": "had_detection", "sortable": True,  "align": "center"},
            {"name": "detections",     "label": "Detections",
             "field": "detections",    "sortable": False, "align": "left"},
            {"name": "snapshot",       "label": "📷",
             "field": "has_frame",     "sortable": False, "align": "center"},
        ]
        if is_teacher:
            teacher_cols = [
                {"name": "computer", "label": "Computer",
                 "field": "computer", "sortable": True, "align": "left"},
                {"name": "student",  "label": "Student",
                 "field": "student",  "sortable": True, "align": "left"},
            ]
            cols = [base_cols[0]] + teacher_cols + base_cols[1:]
        else:
            cols = base_cols

        tbl = ui.table(columns=cols, rows=[], row_key="event_id").classes("w-full")
        tbl.props("dense flat bordered")

        tbl.add_slot("body-cell-snapshot", """
            <q-td :props="props">
                <q-btn v-if="props.row.has_frame"
                    flat dense round icon="image" color="blue"
                    @click="$parent.$emit('view_frame', props.row)"
                />
                <span v-else class="text-gray-600">—</span>
            </q-td>
        """)

        count = ui.label("").classes("text-xs text-gray-500 mt-1")

    # ── Event handlers ────────────────────────────────────────────────────────

    def handle_view_frame(e) -> None:
        row = e.args
        eid = row.get("event_id")
        if not eid:
            return
        raw_b64 = get_event_frame_b64(int(eid))
        if raw_b64:
            ann_b64 = get_event_frame_annotated_b64(int(eid))  # re-draws from DB
            meta = (
                f"{row.get('detected_at', '')}  •  "
                f"{row.get('computer', '—')}  •  "
                f"{row.get('student', '—')}"
            )
            show_snapshot(raw_b64, meta, ann_b64)
        else:
            ui.notify("No snapshot stored for this event", type="info")

    tbl.on("view_frame", handle_view_frame)

    # ── Load / refresh ────────────────────────────────────────────────────────

    def _load() -> None:
        comp_id  = None
        user_id_ = None

        if is_teacher:
            comp_id  = int(f_computer.value) if f_computer.value else None
            user_id_ = int(f_student.value)  if f_student.value  else None
        else:
            user_id_ = current["id"]

        rows = query_events(
            computer_id=comp_id,
            user_id=user_id_,
            class_name=f_class.value or "",
            only_hits=bool(f_only_hits.value),
            limit=int(f_lim.value or 200),
        )
        for r in rows:
            r["had_detection"] = "✅" if r["had_detection"] else "—"
            r["student"]       = r["student"] or "—"
            r["detections"]    = r["detections"] or "—"
        tbl.rows = rows
        tbl.update()
        count.set_text(f"{len(rows)} event(s)")

    _load()