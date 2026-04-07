"""
app/pages/dashboard.py
──────────────────────
Dashboard page  /   (teacher only)
Live annotated preview + console log + Start/Stop.
Annotation overlay can be toggled on/off via the switch in the preview card.
"""
from nicegui import ui

import app.state as state
from app.config import collect_cfg
from app.core.auth import require_auth
from app.core.yolo import reset_model
from app.db.database import list_groups, list_computers_in_group, log_action
from app.pages._nav import nav
from app.services.monitor_service import MonitorController


@ui.page("/")
def page_dashboard() -> None:
    current = require_auth(required_role="teacher")
    if current is None:
        return
    nav(current)

    with ui.row().classes("w-full gap-4 p-4 items-stretch flex-nowrap"):

        # ── Left column ───────────────────────────────────────────────────────
        with ui.column().classes("flex-1 gap-3 min-w-0"):

            with ui.card().classes("w-full"):
                with ui.row().classes("items-center gap-3 flex-wrap"):
                    running = state.monitor is not None
                    # Build status text — include group name when running
                    def _status_text() -> str:
                        if state.monitor is None:
                            return "● Stopped"
                        g = state.monitored_group_name
                        return f"● Running ({g})" if g else "● Running (all)"
                    status_lbl = ui.label(_status_text()).classes(
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

                    # Group selector — 0 = "All computers", >0 = specific group id
                    # NiceGUI ui.select dict format: {stored_value: displayed_label}
                    groups = list_groups()
                    group_opts: dict[int, str] = {0: "All computers"}
                    group_opts.update({g["id"]: g["name"] for g in groups})
                    with ui.row().classes("items-center gap-1"):
                        ui.label("Group:").classes(
                            "text-sm text-gray-500 dark:text-gray-400")
                        group_sel = ui.select(
                            options=group_opts,
                            value=0,
                        ).props("dense outlined").classes("w-44")
                        group_sel.tooltip(
                            "Monitor only computers in the selected group, "
                            "or all Veyon-discovered computers"
                        )

                    with ui.row().classes("items-center gap-2 ml-auto"):
                        ui.label("Computer:").classes(
                            "text-sm text-gray-500 dark:text-gray-400")
                        pc_sel = ui.select(
                            options=list(state.latest_frames.keys()),
                            value=next(iter(state.latest_frames), None),
                        ).props("dense outlined").classes("w-48")

            with ui.card().classes("w-full"):
                with ui.row().classes("items-center justify-between mb-1"):
                    ui.label("Live Preview").classes(
                        "text-xs text-gray-500 dark:text-gray-400")
                    ann_switch = ui.switch("Annotations", value=True).props("dense")
                    ann_switch.tooltip(
                        "Toggle bounding-box overlay on the live preview")

                live_img = ui.image("").props("no-spinner").classes(
                    "w-full rounded"
                ).style("display:block;")

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
        with ui.card().classes("w-80 flex-shrink-0 flex flex-col"):
            with ui.row().classes("items-center justify-between mb-1 flex-shrink-0"):
                ui.label("Console  ↑ newest first").classes(
                    "text-xs text-gray-500 dark:text-gray-400")
                ui.button(
                    "Clear",
                    on_click=lambda: (
                        log_view.set_value(""),
                        log_offset.__setitem__(0, len(state.log_buffer)),
                    ),
                ).props("flat dense size=xs")
            log_view = ui.textarea(value="").props(
                "readonly outlined dense rows=53"
            ).classes(
                "w-full font-mono text-xs rounded "
                "bg-gray-100 text-green-700 "
                "dark:bg-gray-950 dark:text-green-300"
            ).style("flex: 1; min-height: 200px; resize: none;")

    log_offset = [0]
    _last_src:    list[str]       = [""]   # deduplicate set_source calls → no flicker
    _mon_names:   list[set[str]]  = [set()]  # computer names for the active session

    # ── Button handlers ───────────────────────────────────────────────────────

    def do_start() -> None:
        if state.monitor:
            return
        try:
            cfg = collect_cfg()
        except (ValueError, KeyError) as e:
            ui.notify(f"Config error: {e}", type="negative"); return

        # Resolve computers for the selected group (0 = all)
        gid   = group_sel.value   # 0 = all computers, >0 = group id
        gname = group_opts.get(gid, "") if gid else ""

        computers: list[dict] | None = None
        if gid:
            rows = list_computers_in_group(gid)
            if not rows:
                ui.notify(
                    f"Group '{gname}' has no computers assigned.", type="warning"
                )
                return
            computers = [{"name": r["name"], "host": r["host_address"]} for r in rows]

        reset_model()
        state.monitor = MonitorController(cfg, computers=computers)
        state.monitor.start()
        state.monitored_group_name = gname if gid else ""
        _mon_names[0] = {c["name"] for c in computers} if computers else set()
        log_action("monitor.start", user_id=current["id"],
                   detail=f"group={gname or 'all'}")
        btn_start.props("disable")
        btn_stop.props(remove="disable")
        group_sel.props("disable")
        label = f"● Running ({gname})" if gid else "● Running (all)"
        status_lbl.set_text(label)
        status_lbl.classes(
            replace="font-mono text-sm text-green-500 dark:text-green-400")

    def do_stop() -> None:
        if state.monitor:
            state.monitor.stop()
            state.monitor = None
        state.consecutive_detections.clear()
        state.schedule_triggered    = False   # manual stop overrides scheduler
        state.monitored_group_name  = None
        _mon_names[0] = set()
        log_action("monitor.stop", user_id=current["id"], detail="manual")
        btn_start.props(remove="disable")
        btn_stop.props("disable")
        group_sel.props(remove="disable")
        status_lbl.set_text("● Stopped")
        status_lbl.classes(
            replace="font-mono text-sm text-red-500 dark:text-red-400")

    btn_start.on_click(do_start)
    btn_stop.on_click(do_stop)

    # ── 100 ms UI refresh ─────────────────────────────────────────────────────

    def tick() -> None:
        # ── Console: prepend new lines so newest appears at the top ───────────
        new = state.log_buffer[log_offset[0]:]
        log_offset[0] = len(state.log_buffer)
        if new:
            new_block = "\n".join(reversed(new))
            old = log_view.value or ""
            combined = new_block + ("\n" + old if old else "")
            # Cap at 400 lines to prevent unbounded growth
            lines = combined.split("\n")
            if len(lines) > 400:
                combined = "\n".join(lines[:400])
            log_view.set_value(combined)

        # ── Keep status / buttons in sync with scheduler-driven start/stop ────
        if state.monitor is not None:
            g = state.monitored_group_name
            lbl = f"● Running ({g})" if g else "● Running (all)"
            if status_lbl.text != lbl:
                status_lbl.set_text(lbl)
                status_lbl.classes(
                    replace="font-mono text-sm text-green-500 dark:text-green-400")
                btn_start.props("disable")
                btn_stop.props(remove="disable")
                group_sel.props("disable")
        else:
            if status_lbl.text != "● Stopped":
                status_lbl.set_text("● Stopped")
                status_lbl.classes(
                    replace="font-mono text-sm text-red-500 dark:text-red-400")
                btn_start.props(remove="disable")
                btn_stop.props("disable")
                group_sel.props(remove="disable")
                _mon_names[0] = set()

        # ── Computer selector: only show computers in the monitored group ─────
        all_opts = list(state.latest_frames.keys())
        filter_names = _mon_names[0]
        opts = [n for n in all_opts if n in filter_names] if filter_names else all_opts
        if sorted(opts) != sorted(list(pc_sel.options or [])):
            pc_sel.options = opts
            pc_sel.update()
            if pc_sel.value not in opts:
                pc_sel.set_value(opts[0] if opts else None)

        sel = pc_sel.value
        if sel and sel in state.latest_frames:
            ann_b64, raw_b64, dets = state.latest_frames[sel]

            # Only push to the DOM when the frame actually changed — eliminates flicker
            src = ann_b64 if ann_switch.value else raw_b64
            if src != _last_src[0]:
                _last_src[0] = src
                live_img.set_source(src)

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