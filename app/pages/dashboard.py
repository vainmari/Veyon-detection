"""
app/pages/dashboard.py
──────────────────────
Dashboard page  /   (teacher only)
Live annotated preview + console log + Start/Stop.
"""
from nicegui import ui

import app.state as state
from app.config import collect_cfg
from app.core.auth import require_auth
from app.core.yolo import reset_model
from app.pages._nav import nav
from app.services.monitor_service import MonitorController


@ui.page("/")
def page_dashboard() -> None:
    current = require_auth(required_role="teacher")
    if current is None:
        return
    nav(current)

    with ui.row().classes("w-full gap-4 p-4 items-start flex-nowrap"):

        # ── Left column ───────────────────────────────────────────────────────
        with ui.column().classes("flex-1 gap-3 min-w-0"):

            with ui.card().classes("w-full"):
                with ui.row().classes("items-center gap-3 flex-wrap"):
                    running = state.monitor is not None
                    status_lbl = ui.label(
                        "● Running" if running else "● Stopped"
                    ).classes(
                        "font-mono text-sm " +
                        ("text-green-500 dark:text-green-400"
                         if running else
                         "text-red-500 dark:text-red-400")
                    )
                    btn_start = ui.button("▶  Start", color="green")
                    btn_stop  = ui.button("■  Stop",  color="red")
                    if running:
                        btn_start.props("disable")
                    else:
                        btn_stop.props("disable")

                    with ui.row().classes("items-center gap-2 ml-auto"):
                        ui.label("Computer:").classes(
                            "text-sm text-gray-500 dark:text-gray-400")
                        pc_sel = ui.select(
                            options=list(state.latest_frames.keys()),
                            value=next(iter(state.latest_frames), None),
                        ).props("dense outlined").classes("w-48")

            with ui.card().classes("w-full"):
                ui.label("Live Preview").classes(
                    "text-xs text-gray-500 dark:text-gray-400 mb-1")
                live_img = ui.image("").classes("w-full rounded").style(
                    "background:#111; display:block;"
                )

            with ui.card().classes("w-full"):
                with ui.row().classes("items-center gap-4"):
                    with ui.column().classes("gap-0"):
                        ui.label("Active Student").classes(
                            "text-xs text-gray-500 dark:text-gray-400")
                        student_lbl = ui.label("—").classes(
                            "font-mono text-sm "
                            "text-yellow-600 dark:text-yellow-300")
                    ui.separator().props("vertical")
                    with ui.column().classes("gap-0"):
                        ui.label("Last Detections").classes(
                            "text-xs text-gray-500 dark:text-gray-400")
                        det_info = ui.label("— no detections —").classes(
                            "font-mono text-sm "
                            "text-blue-600 dark:text-blue-300")

        # ── Right column: console ─────────────────────────────────────────────
        with ui.card().classes("w-80 flex-shrink-0"):
            with ui.row().classes("items-center justify-between mb-1"):
                ui.label("Console").classes(
                    "text-xs text-gray-500 dark:text-gray-400")
                ui.button(
                    "Clear",
                    on_click=lambda: (
                        log_view.clear(),
                        log_offset.__setitem__(0, len(state.log_buffer)),
                    ),
                ).props("flat dense size=xs")
            log_view = ui.log(max_lines=400).classes(
                "w-full font-mono text-xs rounded "
                "bg-gray-100 text-green-700 "
                "dark:bg-gray-950 dark:text-green-300"
            ).style("height: 560px;")

    log_offset = [0]

    # ── Button handlers ───────────────────────────────────────────────────────

    def do_start() -> None:
        if state.monitor:
            return
        try:
            cfg = collect_cfg()
        except (ValueError, KeyError) as e:
            ui.notify(f"Config error: {e}", type="negative"); return
        reset_model()
        state.monitor = MonitorController(cfg)
        state.monitor.start()
        btn_start.props("disable")
        btn_stop.props(remove="disable")
        status_lbl.set_text("● Running")
        status_lbl.classes(
            replace="font-mono text-sm text-green-500 dark:text-green-400")

    def do_stop() -> None:
        if state.monitor:
            state.monitor.stop()
            state.monitor = None
        btn_start.props(remove="disable")
        btn_stop.props("disable")
        status_lbl.set_text("● Stopped")
        status_lbl.classes(
            replace="font-mono text-sm text-red-500 dark:text-red-400")

    btn_start.on_click(do_start)
    btn_stop.on_click(do_stop)

    # ── 100 ms UI refresh ─────────────────────────────────────────────────────

    def tick() -> None:
        new = state.log_buffer[log_offset[0]:]
        for msg in new:
            log_view.push(msg)
        log_offset[0] = len(state.log_buffer)

        opts = list(state.latest_frames.keys())
        if sorted(opts) != sorted(list(pc_sel.options or [])):
            pc_sel.options = opts
            pc_sel.update()
            if pc_sel.value is None and opts:
                pc_sel.set_value(opts[0])

        sel = pc_sel.value
        if sel and sel in state.latest_frames:
            b64, dets = state.latest_frames[sel]
            live_img.set_source(b64)

            uid = state.computer_users.get(sel)
            if uid:
                from app.db.database import get_user_by_id
                u = get_user_by_id(uid)
                student_lbl.set_text(u["username"] if u else "—")
            else:
                student_lbl.set_text("— not matched —")

            det_info.set_text(
                "  |  ".join(
                    f"{d['class_name']} {d['conf']:.0%}" for d in dets)
                if dets else "— no detections —"
            )

    ui.timer(0.1, tick)