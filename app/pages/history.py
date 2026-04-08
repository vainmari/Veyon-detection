"""
app/pages/history.py
────────────────────
History page  /history

Teacher view  — full filters (computer, student, class, hits-only)
Student view  — auto-filtered to own records, computer selector hidden
Both views    — 📷 opens snapshot preview; ⛶ Full screen opens maximized dialog.
Paginated     — records loaded in configurable page sizes (50 / 100 / 200).
"""
import asyncio

from nicegui import ui

from app.core.auth import require_auth
from app.db.database import (
    count_query_events,
    get_event_frame_b64,
    get_event_frame_annotated_b64,
    list_classes,
    list_computers,
    list_users,
    query_events,
)
from app.pages._nav import nav
from app.pages._snapshot import make_snapshot_dialogs
from app.translate import t


@ui.page("/history")
async def page_history() -> None:
    current = require_auth()
    if current is None:
        return
    nav(current)
    is_teacher = current["role"] == "teacher"

    _snap_dlg, _fs_dlg, show_snapshot = make_snapshot_dialogs()

    # Mutable pagination state
    state = {"page": 0, "has_next": False, "total": None}

    with ui.column().classes("w-full p-4 gap-4"):
        ui.label(
            t("history_title") if is_teacher else t("history_my_title")
        ).classes("text-xl font-bold")

        # ── Filter bar ────────────────────────────────────────────────────────
        with ui.card().classes("w-full"):
            with ui.row().classes("gap-4 flex-wrap items-end"):

                if is_teacher:
                    computers = [{"label": t("history_all_computers"), "value": ""}] + [
                        {"label": c["name"], "value": str(c["id"])}
                        for c in list_computers()
                    ]
                    f_computer = ui.select(
                        {c["value"]: c["label"] for c in computers},
                        value="", label=t("history_computer"),
                    ).props("dense outlined").classes("w-44")

                    students = [{"label": t("history_all_students"), "value": ""}] + [
                        {"label": u["username"], "value": str(u["id"])}
                        for u in list_users() if u["role"] == "student"
                    ]
                    f_student = ui.select(
                        {s["value"]: s["label"] for s in students},
                        value="", label=t("history_student"),
                    ).props("dense outlined").classes("w-44")

                classes = [{"label": t("history_all_classes"), "value": ""}] + [
                    {"label": c["name"], "value": c["name"]}
                    for c in list_classes()
                ]
                f_class = ui.select(
                    {c["value"]: c["label"] for c in classes},
                    value="", label=t("history_class"),
                ).props("dense outlined").classes("w-44")

                f_only_hits = ui.checkbox(t("history_detections_only"), value=False)

                f_page_size = ui.select(
                    {50: "50", 100: "100", 200: "200"},
                    value=100, label=t("history_page_size"),
                ).props("dense outlined").classes("w-24")

                search_btn = ui.button(t("history_search"),
                                       on_click=lambda: _load_page(0)).props("color=primary")

        # ── Table ─────────────────────────────────────────────────────────────
        base_cols = [
            {"name": "detected_at",    "label": t("history_col_time"),
             "field": "detected_at",   "sortable": True,  "align": "left"},
            {"name": "had_detection",  "label": t("history_col_hit"),
             "field": "had_detection", "sortable": True,  "align": "center"},
            {"name": "detections",     "label": t("history_col_detections"),
             "field": "detections",    "sortable": False, "align": "left"},
            {"name": "snapshot",       "label": "📷",
             "field": "has_frame",     "sortable": False, "align": "center"},
        ]
        if is_teacher:
            teacher_cols = [
                {"name": "computer",   "label": t("history_col_computer"),
                 "field": "computer",   "sortable": True, "align": "left"},
                {"name": "student",    "label": t("history_student"),
                 "field": "student",    "sortable": True, "align": "left"},
                {"name": "model_name", "label": t("history_col_model"),
                 "field": "model_name", "sortable": True, "align": "left"},
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

        # ── Pagination bar ────────────────────────────────────────────────────
        with ui.row().classes("items-center gap-3 mt-1"):
            prev_btn = ui.button(icon="chevron_left",
                                 on_click=lambda: _go_page(state["page"] - 1)
                                 ).props("flat dense").classes("text-gray-600")
            count_lbl = ui.label("").classes("text-xs text-gray-500")
            next_btn  = ui.button(icon="chevron_right",
                                  on_click=lambda: _go_page(state["page"] + 1)
                                  ).props("flat dense").classes("text-gray-600")

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _filter_kwargs() -> dict:
        comp_id  = None
        user_id_ = None
        if is_teacher:
            comp_id  = int(f_computer.value) if f_computer.value else None
            user_id_ = int(f_student.value)  if f_student.value  else None
        else:
            user_id_ = current["id"]
        return dict(
            computer_id=comp_id,
            user_id=user_id_,
            class_name=f_class.value or "",
            only_hits=bool(f_only_hits.value),
        )

    def _format_count_lbl(n_shown: int) -> None:
        page      = state["page"]
        page_size = int(f_page_size.value)
        first     = page * page_size + 1 if n_shown else 0
        last      = page * page_size + n_shown
        total     = state["total"]
        if total is None:
            # count still loading — show range with + indicator
            suffix = "+" if state["has_next"] else ""
            count_lbl.set_text(
                t("history_page_info").format(
                    first=first, last=last, suffix=suffix, page=page + 1,
                )
            )
        else:
            pages = max(1, (total + page_size - 1) // page_size)
            count_lbl.set_text(
                t("history_page_info_total").format(
                    first=first, last=last, total=total,
                    page=page + 1, pages=pages,
                )
            )
        prev_btn.props("disable" if page == 0             else "")
        next_btn.props("disable" if not state["has_next"] else "")

    async def _load_page(page: int) -> None:
        state["page"] = page
        search_btn.props("loading")
        try:
            page_size = int(f_page_size.value)
            kwargs    = _filter_kwargs()
            loop      = asyncio.get_event_loop()

            # Fetch one extra row to detect whether a next page exists —
            # no separate COUNT query needed.
            raw = await loop.run_in_executor(
                None,
                lambda: query_events(
                    **kwargs,
                    limit=page_size + 1,
                    offset=page * page_size,
                ),
            )
            state["has_next"] = len(raw) > page_size
            rows = raw[:page_size]

            for r in rows:
                r["had_detection"] = "✅" if r["had_detection"] else "—"
                r["student"]       = r["student"]    or "—"
                r["detections"]    = r["detections"] or "—"
                r["model_name"]    = r.get("model_name") or "—"
            tbl.rows = rows
            tbl.update()
            _format_count_lbl(len(rows))
            # Fetch total in background so the page content is visible immediately
            if page == 0:
                state["total"] = None
                asyncio.create_task(_fetch_total_bg(len(rows)))
        finally:
            search_btn.props(remove="loading")

    async def _fetch_total_bg(n_shown: int) -> None:
        loop = asyncio.get_event_loop()
        kwargs = _filter_kwargs()
        total = await loop.run_in_executor(
            None, lambda: count_query_events(**kwargs))
        state["total"] = total
        _format_count_lbl(n_shown)

    async def _go_page(page: int) -> None:
        await _load_page(max(0, page))

    # ── Event handlers ────────────────────────────────────────────────────────

    def handle_view_frame(e) -> None:
        row = e.args
        eid = row.get("event_id")
        if not eid:
            return
        raw_b64 = get_event_frame_b64(int(eid))
        if raw_b64:
            ann_b64 = get_event_frame_annotated_b64(int(eid))
            meta = (
                f"{row.get('detected_at', '')}  •  "
                f"{row.get('computer', '—')}  •  "
                f"{row.get('student', '—')}"
            )
            show_snapshot(raw_b64, meta, ann_b64)
        else:
            ui.notify(t("history_no_snapshot"), type="info")

    tbl.on("view_frame", handle_view_frame)

    await _load_page(0)
