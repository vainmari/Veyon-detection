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
from app.db.database import get_active_model, list_groups, list_computers_in_group, log_action
from app.pages._nav import nav
from app.services.monitor_service import MonitorController
from app.translate import t


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
                            return t("status_stopped")
                        g = state.monitored_group_name
                        return (
                            t("status_running_group").format(g=g)
                            if g else t("status_running_all")
                        )
                    status_lbl = ui.label(_status_text()).classes(
                        "font-mono text-sm " +
                        ("text-green-500 dark:text-green-400"
                         if running else
                         "text-red-500 dark:text-red-400")
                    )
                    btn_start = ui.button(t("dash_btn_start"), color="green")
                    btn_stop  = ui.button(t("dash_btn_stop"),  color="red")
                    if running:
                        btn_start.props("disable")
                    else:
                        btn_stop.props("disable")

                    # Group selector — 0 = "All computers", >0 = specific group id
                    groups = list_groups()
                    group_opts: dict[int, str] = {0: t("dash_all_computers")}
                    group_opts.update({g["id"]: g["name"] for g in groups})
                    with ui.row().classes("items-center gap-1"):
                        ui.label(t("dash_group_label")).classes(
                            "text-sm text-gray-500 dark:text-gray-400")
                        group_sel = ui.select(
                            options=group_opts,
                            value=0,
                        ).props("dense outlined").classes("w-44")
                        group_sel.tooltip(t("dash_group_tooltip"))

                    with ui.row().classes("items-center gap-2 ml-auto"):
                        ui.label(t("dash_computer_label")).classes(
                            "text-sm text-gray-500 dark:text-gray-400")
                        pc_sel = ui.select(
                            options=list(state.latest_frames.keys()),
                            value=next(iter(state.latest_frames), None),
                        ).props("dense outlined").classes("w-48")

            with ui.card().classes("w-full"):
                with ui.row().classes("items-center justify-between mb-1"):
                    ui.label(t("dash_live_preview")).classes(
                        "text-xs text-gray-500 dark:text-gray-400")
                    ann_switch = ui.switch(t("dash_annotations"), value=True).props("dense")
                    ann_switch.tooltip(t("dash_ann_tooltip"))

                # OFFLINE badge — shown when the selected computer has stopped
                # delivering frames. Hidden by default; tick() toggles visibility.
                offline_badge = ui.label(t("dash_offline_badge")).classes(
                    "text-xs font-bold text-white bg-red-600 "
                    "px-2 py-1 rounded mb-1 self-start"
                )
                offline_badge.set_visibility(False)

                live_img = ui.image("").props("no-spinner").classes(
                    "w-full rounded"
                ).style("display:block;")

            with ui.card().classes("w-full"):
                with ui.row().classes("items-center gap-4"):
                    with ui.column().classes("gap-0"):
                        ui.label(t("dash_active_student")).classes(
                            "text-xs text-gray-500 dark:text-gray-400")
                        student_lbl = ui.label("—").classes(
                            "font-mono text-sm "
                            "text-yellow-600 dark:text-yellow-300")
                    ui.separator().props("vertical")
                    with ui.column().classes("gap-0"):
                        ui.label(t("dash_last_detections")).classes(
                            "text-xs text-gray-500 dark:text-gray-400")
                        det_info = ui.label(t("dash_no_detections")).classes(
                            "font-mono text-sm "
                            "text-blue-600 dark:text-blue-300")

        # ── Right column: console ─────────────────────────────────────────────
        with ui.card().classes("w-80 flex-shrink-0 flex flex-col"):
            with ui.row().classes("items-center justify-between mb-1 flex-shrink-0"):
                ui.label(t("dash_console_title")).classes(
                    "text-xs text-gray-500 dark:text-gray-400")
                ui.button(
                    t("dash_clear"),
                    on_click=lambda: (
                        log_view.set_value(""),
                        log_seq.__setitem__(0, state.log_total),
                    ),
                ).props("flat dense size=xs")
            log_view = ui.textarea(value="").props(
                "readonly outlined dense rows=53"
            ).classes(
                "w-full font-mono text-xs rounded "
                "bg-gray-100 text-green-700 "
                "dark:bg-gray-950 dark:text-green-300"
            ).style("flex: 1; min-height: 200px; resize: none;")

    log_seq = [0]          # how many total log messages we have already displayed
    _last_src:    list[str]       = [""]   # deduplicate set_source calls → no flicker
    _mon_names:   list[set[str]]  = [set()]  # computer names for the active session

    # ── Button handlers ───────────────────────────────────────────────────────

    def do_start() -> None:
        # Check-then-assign on state.monitor must be atomic across the UI
        # thread and the scheduler thread; otherwise both could create their
        # own MonitorController and leak one of them.
        with state.monitor_lock:
            if state.monitor:
                return
            if not get_active_model():
                ui.notify(t("dash_no_active_model"), type="warning")
                return
            try:
                cfg = collect_cfg()
            except (ValueError, KeyError) as e:
                ui.notify(t("dash_cfg_error").format(e=e), type="negative"); return

            # Resolve computers for the selected group (0 = all)
            gid   = group_sel.value   # 0 = all computers, >0 = group id
            gname = group_opts.get(gid, "") if gid else ""

            computers: list[dict] | None = None
            if gid:
                rows = list_computers_in_group(gid)
                if not rows:
                    ui.notify(
                        t("dash_no_computers").format(gname=gname), type="warning"
                    )
                    return
                computers = [{"name": r["name"], "host": r["host_address"]} for r in rows]

            reset_model()
            state.monitor = MonitorController(
                cfg, computers=computers,
                started_by=current["id"],
                group_name=gname if gid else "",
            )
            state.monitor.start()
            state.monitored_group_name = gname if gid else ""
            _mon_names[0] = {c["name"] for c in computers} if computers else set()
            log_action("monitor.start", user_id=current["id"],
                       detail=f"group={gname or 'all'}")
            btn_start.props("disable")
            btn_stop.props(remove="disable")
            group_sel.props("disable")
            label = (
                t("status_running_group").format(g=gname)
                if gid else t("status_running_all")
            )
            status_lbl.set_text(label)
            status_lbl.classes(
                replace="font-mono text-sm text-green-500 dark:text-green-400")

    def do_stop() -> None:
        with state.monitor_lock:
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
            status_lbl.set_text(t("status_stopped"))
        status_lbl.classes(
            replace="font-mono text-sm text-red-500 dark:text-red-400")

    btn_start.on_click(do_start)
    btn_stop.on_click(do_stop)

    # ── 100 ms UI refresh ─────────────────────────────────────────────────────

    def tick() -> None:
        # ── Console: prepend new lines so newest appears at the top ───────────
        # Use a monotonic total counter so we never lose entries when the buffer
        # trims old messages from the front (pop(0) shifts all list indices).
        total = state.log_total
        new_count = total - log_seq[0]
        if new_count > 0:
            log_seq[0] = total
            # The buffer holds at most LOG_CAP entries (most recent).
            # Grab the last new_count entries — clamped to what's available.
            buf = state.log_buffer
            new = buf[max(0, len(buf) - new_count):]
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
            lbl = (
                t("status_running_group").format(g=g)
                if g else t("status_running_all")
            )
            if status_lbl.text != lbl:
                status_lbl.set_text(lbl)
                status_lbl.classes(
                    replace="font-mono text-sm text-green-500 dark:text-green-400")
                btn_start.props("disable")
                btn_stop.props(remove="disable")
                group_sel.props("disable")
        else:
            stopped = t("status_stopped")
            if status_lbl.text != stopped:
                status_lbl.set_text(stopped)
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

        # Build a {value: label} mapping so offline machines visibly carry the
        # "(offline)" suffix without changing the option value (which is the
        # raw computer name). NiceGUI's ui.select supports both list and dict
        # forms for `options`.
        offline_suffix = t("dash_offline_suffix")
        opts_map = {
            n: (f"{n}  {offline_suffix}" if not state.is_computer_online(n) else n)
            for n in opts
        }
        if opts_map != (pc_sel.options or {}):
            pc_sel.options = opts_map
            pc_sel.update()
            if pc_sel.value not in opts_map:
                pc_sel.set_value(next(iter(opts_map), None))

        sel = pc_sel.value
        if sel and sel in state.latest_frames:
            ann_b64, raw_b64, dets = state.latest_frames[sel]

            # Only push to the DOM when the frame actually changed — eliminates flicker
            src = ann_b64 if ann_switch.value else raw_b64
            if src != _last_src[0]:
                _last_src[0] = src
                live_img.set_source(src)

            # Toggle the OFFLINE badge for the currently selected computer.
            offline_badge.set_visibility(not state.is_computer_online(sel))

            uid = state.computer_users.get(sel)
            if uid:
                from app.db.database import get_user_by_id
                u = get_user_by_id(uid)
                student_lbl.set_text(u["username"] if u else "—")
            else:
                student_lbl.set_text(t("dash_not_matched"))

            det_info.set_text(
                "  |  ".join(
                    f"{d['class_name']} {d['conf']:.0%}" for d in dets)
                if dets else t("dash_no_detections")
            )
        else:
            # No selection / no frames yet — hide the badge.
            offline_badge.set_visibility(False)

    ui.timer(0.1, tick)
