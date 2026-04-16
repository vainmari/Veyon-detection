"""
app/pages/_nav.py
─────────────────
Shared navigation bar — role-aware.

Roles
─────
  admin   — Users, Models, Audit Log, Settings
  teacher — Dashboard, History, Analytics, Alert Rules, Groups, Schedules,
            Users, Models, Audit Log
            + Start/Stop monitor + notification bell
  student — My History, My Analytics
"""
from __future__ import annotations

from nicegui import app as nicegui_app, ui

import app.state as state
from app.core.auth import clear_session
from app.pages._snapshot import make_snapshot_dialogs
from app.translate import t, get_lang, set_lang


def nav(current_user: dict) -> None:
    role = current_user["role"]

    dark = ui.dark_mode()
    saved = nicegui_app.storage.user.get("dark_mode", True)
    dark.enable() if saved else dark.disable()

    with ui.header().classes(
        "bg-gray-900 text-white px-4 py-2 flex items-center gap-4 shadow-md"
    ):
        ui.label("🎓 Veyon AI Monitor").classes("font-bold text-base mr-2")

        if role == "admin":
            for key, path in [
                ("nav_users",       "/users"),
                ("nav_models",      "/models"),
                ("nav_audit_log",   "/audit"),
                ("nav_settings",    "/settings"),
            ]:
                ui.link(t(key), path).classes(
                    "text-gray-300 hover:text-white text-sm no-underline")

        elif role == "teacher":
            for key, path in [
                ("nav_dashboard",   "/"),
                ("nav_history",     "/history"),
                ("nav_analytics",   "/analytics"),
                ("nav_alert_rules", "/alerts"),
                ("nav_groups",      "/groups"),
                ("nav_schedules",   "/schedules"),
                ("nav_users",       "/users"),
                ("nav_models",      "/models"),
                ("nav_audit_log",   "/audit"),
            ]:
                ui.link(t(key), path).classes(
                    "text-gray-300 hover:text-white text-sm no-underline")

        else:  # student
            for key, path in [
                ("nav_my_history",   "/history"),
                ("nav_my_analytics", "/analytics"),
            ]:
                ui.link(t(key), path).classes(
                    "text-gray-300 hover:text-white text-sm no-underline")

        if role == "teacher":
            ui.separator().props("vertical").classes("mx-1 opacity-30")
            status_dot = ui.label("").classes("text-xs font-mono")
            btn_start  = ui.button(t("btn_start"), color="green").props(
                "dense unelevated size=sm")
            btn_stop   = ui.button(t("btn_stop"),  color="red").props(
                "dense unelevated size=sm")

            def do_start() -> None:
                if state.monitor:
                    return
                from app.config import collect_cfg
                from app.core.yolo import reset_model
                from app.services.monitor_service import MonitorController
                try:
                    cfg = collect_cfg()
                except (ValueError, KeyError) as e:
                    ui.notify(t("dash_cfg_error").format(e=e), type="negative")
                    return
                reset_model()
                state.monitor = MonitorController(cfg)
                state.monitor.start()

            def do_stop() -> None:
                if state.monitor:
                    state.monitor.stop()
                    state.monitor = None
                state.schedule_triggered   = False   # manual stop overrides scheduler
                state.monitored_group_name = None

            btn_start.on_click(do_start)
            btn_stop.on_click(do_stop)

            def _sync_buttons() -> None:
                running = state.monitor is not None
                if running:
                    g = state.monitored_group_name
                    if g:
                        label = t("status_running_group").format(g=g)
                    else:
                        label = t("status_running_all")
                    status_dot.set_text(label)
                    status_dot.classes(replace="text-xs font-mono text-green-400")
                    btn_start.props("disable")
                    btn_stop.props(remove="disable")
                else:
                    status_dot.set_text(t("status_stopped"))
                    status_dot.classes(replace="text-xs font-mono text-red-400")
                    btn_start.props(remove="disable")
                    btn_stop.props("disable")

            _sync_buttons()
            ui.timer(1.0, _sync_buttons)

        with ui.row().classes("ml-auto items-center gap-2"):
            if role == "teacher":
                _notification_bell()
                ui.separator().props("vertical").classes("opacity-30")
            _user_menu(current_user, dark)


# ── User menu ─────────────────────────────────────────────────────────────────

def _user_menu(current_user: dict, dark) -> None:
    role = current_user["role"]

    with ui.dialog() as pwd_dlg, ui.card().classes("p-5 gap-3 w-80"):
        ui.label(t("pwd_title")).classes("text-base font-bold")
        current_pw = ui.input(
            t("pwd_current"), password=True, password_toggle_button=True,
        ).props("dense outlined").classes("w-full")
        new_pw = ui.input(
            t("pwd_new"), password=True, password_toggle_button=True,
        ).props("dense outlined").classes("w-full")
        confirm_pw = ui.input(
            t("pwd_confirm"), password=True, password_toggle_button=True,
        ).props("dense outlined").classes("w-full")
        err_lbl = ui.label("").classes("text-red-400 text-sm")

        def do_change() -> None:
            from app.db.database import update_password, verify_password
            if not verify_password(current_user["username"], current_pw.value):
                err_lbl.set_text(t("pwd_err_wrong"))
                return
            if len(new_pw.value) < 6:
                err_lbl.set_text(t("pwd_err_short"))
                return
            if new_pw.value != confirm_pw.value:
                err_lbl.set_text(t("pwd_err_mismatch"))
                return
            update_password(current_user["id"], new_pw.value)
            pwd_dlg.close()
            current_pw.set_value(""); new_pw.set_value("")
            confirm_pw.set_value(""); err_lbl.set_text("")
            ui.notify(t("pwd_success"), type="positive")

        with ui.row().classes("gap-2"):
            ui.button(t("pwd_save"),   on_click=do_change).props("color=primary dense")
            ui.button(t("pwd_cancel"), on_click=pwd_dlg.close).props("flat dense")

    label_text = f"{current_user['username']}  ({role})"
    with ui.dropdown_button(
        label_text, icon="account_circle",
    ).props("flat no-caps dense color=white"):

        with ui.menu_item():
            with ui.row().classes("items-center gap-3 w-full"):
                ui.icon("dark_mode").classes("text-base")
                ui.label(t("nav_dark_mode")).classes("text-sm flex-1")
                ui.switch(
                    value=nicegui_app.storage.user.get("dark_mode", True),
                    on_change=lambda e: (
                        nicegui_app.storage.user.__setitem__("dark_mode", e.value),
                        dark.enable() if e.value else dark.disable(),
                    ),
                ).props("dense")

        ui.separator()

        # ── Language switcher ──────────────────────────────────────────────────
        with ui.menu_item():
            with ui.row().classes("items-center gap-3 w-full"):
                ui.icon("translate").classes("text-base")
                ui.label("EN / LT").classes("text-sm flex-1")
                lang_now = get_lang()
                ui.toggle(
                    {"en": "EN", "lt": "LT"},
                    value=lang_now,
                    on_change=lambda e: (set_lang(e.value), ui.navigate.reload()),
                ).props("dense")

        ui.separator()

        with ui.menu_item(on_click=pwd_dlg.open):
            with ui.row().classes("items-center gap-3"):
                ui.icon("lock").classes("text-base")
                ui.label(t("nav_change_password")).classes("text-sm")

        ui.separator()

        with ui.menu_item(
            on_click=lambda: (clear_session(), ui.navigate.to("/login"))
        ):
            with ui.row().classes("items-center gap-3 text-red-400"):
                ui.icon("logout").classes("text-base")
                ui.label(t("nav_sign_out")).classes("text-sm")


# ── Notification bell ─────────────────────────────────────────────────────────

def _notification_bell() -> None:
    from app.db.database import (
        count_unread_notifications,
        get_event_frame_b64,
        get_event_frame_annotated_b64,
        list_notifications,
        mark_all_read,
        mark_read,
    )

    _snap_dlg, _fs_dlg, show_snapshot = make_snapshot_dialogs()

    # ── Notification list dialog ──────────────────────────────────────────────
    with ui.dialog().props("position=right") as notif_dlg, \
         ui.card().classes("p-0 h-full rounded-none").style(
             "width:400px; max-width:95vw;"):
        with ui.row().classes("items-center justify-between px-4 py-3 w-full"):
            ui.label(t("notif_title")).classes("text-base font-bold")
            ui.button(
                t("notif_mark_all_read"),
                on_click=lambda: (mark_all_read(), _reload(), _refresh_badge()),
            ).props("flat dense size=sm color=gray")
        ui.separator()
        scroll = ui.scroll_area().classes("w-full").style(
            "flex:1; height:calc(100vh - 64px);")

    def _reload() -> None:
        scroll.clear()
        with scroll:
            items = list_notifications(limit=60)
            if not items:
                with ui.column().classes("items-center w-full p-10 gap-3"):
                    ui.icon("notifications_none").classes("text-5xl text-gray-600")
                    ui.label(t("notif_empty")).classes(
                        "text-gray-500 text-sm")
                return
            for n in items:
                _render_row(n)

    def _render_row(n: dict) -> None:
        color  = n.get("class_color", "#888")
        is_new = not bool(n["is_read"])
        bg     = "bg-gray-800" if is_new else ""

        with ui.card().classes(
            f"w-full rounded-none border-0 shadow-none {bg}"
        ).style(
            f"border-left:4px solid {color}; border-radius:0 !important;"
        ):
            with ui.row().classes("items-start gap-2 px-3 py-2 w-full"):
                with ui.column().classes("gap-1 flex-1 min-w-0"):
                    with ui.row().classes("items-center gap-2 flex-wrap"):
                        ui.badge(n["class_name"]).props("rounded").style(
                            f"background:{color}; color:#fff; font-size:0.68rem;")
                        if is_new:
                            ui.badge("NEW").props("rounded color=orange")
                    ui.label(f"🖥  {n['computer']}").classes("text-xs text-gray-300")
                    ui.label(f"👤 {n['student']}").classes("text-xs text-gray-300")
                    ui.label(n["created_at"]).classes("text-xs text-gray-500")

                if n.get("has_frame"):
                    meta_str = (
                        f"{n['class_name']}  •  {n['computer']}"
                        f"  •  {n['student']}  •  {n['created_at']}"
                    )

                    def _view(nid=n["id"], eid=n["event_id"], meta=meta_str) -> None:
                        mark_read(nid)
                        _refresh_badge()
                        _reload()
                        raw_b64 = get_event_frame_b64(eid)
                        if raw_b64:
                            ann_b64 = get_event_frame_annotated_b64(eid)
                            show_snapshot(raw_b64, meta, ann_b64)
                        else:
                            ui.notify(t("notif_no_snapshot"), type="warning")

                    ui.button(icon="image", on_click=_view).props(
                        "flat round dense color=blue"
                    ).tooltip(t("notif_view_snapshot"))

    def _open_notifs() -> None:
        _reload()
        notif_dlg.open()

    with ui.button(icon="notifications", on_click=_open_notifs).props(
        "flat round color=white"
    ):
        badge = ui.badge("").props("floating color=red transparent")

    _prev = [0]

    def _refresh_badge() -> None:
        c = count_unread_notifications()
        badge.set_text(str(c) if c else "")
        badge.set_visibility(c > 0)

        if c > _prev[0]:
            new_items = list_notifications(limit=c - _prev[0])
            for n in new_items[:3]:
                ui.notify(
                    f"🚨 {n['class_name']} detected\n"
                    f"🖥  {n['computer']}   👤 {n['student']}\n"
                    f"🕐 {n['created_at']}",
                    type="negative",
                    position="top-right",
                    timeout=8000,
                    close_button=True,
                )
            ui.run_javascript("""
                (function(){
                  try {
                    const c=new(window.AudioContext||window.webkitAudioContext)();
                    function b(f,s,d,v){
                      const o=c.createOscillator(),g=c.createGain();
                      o.connect(g);g.connect(c.destination);
                      o.type='sine';o.frequency.value=f;
                      g.gain.setValueAtTime(v,c.currentTime+s);
                      g.gain.exponentialRampToValueAtTime(
                        0.001,c.currentTime+s+d);
                      o.start(c.currentTime+s);
                      o.stop(c.currentTime+s+d+0.05);
                    }
                    b(880,0.0,0.18,0.4);b(660,0.2,0.18,0.4);
                    b(880,0.4,0.25,0.5);
                  } catch(e){}
                })();
            """)

        _prev[0] = c

    _refresh_badge()
    ui.timer(3.0, _refresh_badge)
