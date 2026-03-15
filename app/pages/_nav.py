"""
app/pages/_nav.py
─────────────────
Shared navigation bar — role-aware, with:
  • Start / Stop monitor buttons (teacher only)
  • 🔔 Notification bell with unread badge — visible on every page for both roles
"""
from __future__ import annotations

from nicegui import ui

import app.state as state
from app.core.auth import clear_session


def nav(current_user: dict) -> None:
    role = current_user["role"]

    with ui.header().classes(
        "bg-gray-900 text-white px-4 py-2 flex items-center gap-4 shadow-md"
    ):
        ui.label("🎓 Veyon AI Monitor").classes("font-bold text-base mr-2")

        # ── Navigation links ──────────────────────────────────────────────────
        if role == "teacher":
            for label, path in [
                ("Dashboard",   "/"),
                ("History",     "/history"),
                ("Analytics",   "/analytics"),
                ("Alert Rules", "/alerts"),
                ("Users",       "/users"),
                ("Settings",    "/settings"),
            ]:
                ui.link(label, path).classes(
                    "text-gray-300 hover:text-white text-sm no-underline")
        else:
            for label, path in [
                ("My History",   "/history"),
                ("My Analytics", "/analytics"),
            ]:
                ui.link(label, path).classes(
                    "text-gray-300 hover:text-white text-sm no-underline")

        # ── Monitor start / stop (teacher only) ───────────────────────────────
        if role == "teacher":
            ui.separator().props("vertical").classes("mx-1 opacity-30")
            status_dot = ui.label("").classes("text-xs font-mono")
            btn_start  = ui.button("▶ Start", color="green").props(
                "dense unelevated size=sm")
            btn_stop   = ui.button("■ Stop",  color="red").props(
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
                    ui.notify(f"Config error: {e}", type="negative"); return
                reset_model()
                state.monitor = MonitorController(cfg)
                state.monitor.start()

            def do_stop() -> None:
                if state.monitor:
                    state.monitor.stop()
                    state.monitor = None

            btn_start.on_click(do_start)
            btn_stop.on_click(do_stop)

            def _sync_buttons() -> None:
                running = state.monitor is not None
                if running:
                    status_dot.set_text("● Running")
                    status_dot.classes(replace="text-xs font-mono text-green-400")
                    btn_start.props("disable")
                    btn_stop.props(remove="disable")
                else:
                    status_dot.set_text("● Stopped")
                    status_dot.classes(replace="text-xs font-mono text-red-400")
                    btn_start.props(remove="disable")
                    btn_stop.props("disable")

            _sync_buttons()
            ui.timer(1.0, _sync_buttons)

        # ── Right side ────────────────────────────────────────────────────────
        with ui.row().classes("ml-auto items-center gap-2"):
            _notification_bell()
            ui.separator().props("vertical").classes("opacity-30")
            role_color = "text-orange-400" if role == "teacher" else "text-blue-400"
            ui.label(current_user["username"]).classes(
                f"text-sm font-mono {role_color}")
            ui.label(f"({role})").classes("text-xs text-gray-500")
            ui.button(
                "Sign out",
                on_click=lambda: (clear_session(), ui.navigate.to("/login")),
            ).props("flat dense size=sm color=red")


# ── Notification bell ─────────────────────────────────────────────────────────

def _notification_bell() -> None:
    """
    Bell icon with unread-count badge.
    Opens a notification drawer listing recent alerts.
    Each alert has a 📷 button that opens the annotated snapshot.
    """
    from app.db.database import (
        count_unread_notifications,
        get_event_frame_b64,
        list_notifications,
        mark_all_read,
        mark_read,
    )

    # ── Snapshot viewer dialog ────────────────────────────────────────────────
    with ui.dialog().props("maximized=false") as snap_dlg, \
         ui.card().classes("p-3 gap-2").style(
             "max-width:900px; width:95vw;"
         ):
        snap_meta  = ui.label("").classes("text-xs text-gray-400 font-mono")
        snap_image = ui.image("").classes("w-full rounded").style(
            "max-height:70vh; object-fit:contain; background:#111;"
        )
        ui.button("Close", on_click=snap_dlg.close).props("flat dense")

    # ── Notification list dialog ──────────────────────────────────────────────
    with ui.dialog().props("position=right") as notif_dlg, \
         ui.card().classes("p-0 h-full rounded-none").style(
             "width:400px; max-width:95vw;"
         ):
        with ui.row().classes("items-center justify-between px-4 py-3 w-full"):
            ui.label("Notifications").classes("text-base font-bold")
            ui.button(
                "Mark all read",
                on_click=lambda: (mark_all_read(), _reload(), _refresh_badge()),
            ).props("flat dense size=sm color=gray")
        ui.separator()
        scroll = ui.scroll_area().classes("w-full").style("flex:1; height:calc(100vh - 64px);")

    # ── Render helpers ────────────────────────────────────────────────────────

    def _reload() -> None:
        scroll.clear()
        with scroll:
            items = list_notifications(limit=60)
            if not items:
                with ui.column().classes("items-center w-full p-10 gap-3"):
                    ui.icon("notifications_none").classes("text-5xl text-gray-600")
                    ui.label("All clear — no alerts yet").classes(
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
            f"border-left: 4px solid {color}; border-radius:0 !important;"
        ):
            with ui.row().classes("items-start gap-2 px-3 py-2 w-full"):

                # Details column
                with ui.column().classes("gap-1 flex-1 min-w-0"):
                    with ui.row().classes("items-center gap-2 flex-wrap"):
                        ui.badge(n["class_name"]).props("rounded").style(
                            f"background:{color}; color:#fff; font-size:0.68rem;"
                        )
                        if is_new:
                            ui.badge("NEW").props("rounded color=orange")
                    ui.label(
                        f"🖥  {n['computer']}"
                    ).classes("text-xs text-gray-300")
                    ui.label(
                        f"👤 {n['student']}"
                    ).classes("text-xs text-gray-300")
                    ui.label(n["created_at"]).classes("text-xs text-gray-500")

                # View snapshot button
                if n.get("has_frame"):
                    meta_str = (
                        f"{n['class_name']}  •  {n['computer']}  "
                        f"•  {n['student']}  •  {n['created_at']}"
                    )

                    def _view(
                        nid  = n["id"],
                        eid  = n["event_id"],
                        meta = meta_str,
                    ) -> None:
                        mark_read(nid)
                        _refresh_badge()
                        _reload()
                        b64 = get_event_frame_b64(eid)
                        if b64:
                            snap_image.set_source(b64)
                            snap_meta.set_text(meta)
                            snap_dlg.open()
                        else:
                            ui.notify("Snapshot not available", type="warning")

                    ui.button(icon="image", on_click=_view).props(
                        "flat round dense color=blue"
                    ).tooltip("View snapshot")

    # ── Bell button ───────────────────────────────────────────────────────────

    def _open_notifs() -> None:
        _reload()
        notif_dlg.open()

    with ui.button(icon="notifications", on_click=_open_notifs).props(
        "flat round color=white"
    ):
        badge = ui.badge("").props("floating color=red transparent")

    def _refresh_badge() -> None:
        c = count_unread_notifications()
        badge.set_text(str(c) if c else "")
        badge.set_visibility(c > 0)

    _refresh_badge()

    # Track previous count so we only fire popup + sound on NEW notifications
    _prev_count = [count_unread_notifications()]

    def _refresh_badge() -> None:
        c = count_unread_notifications()
        badge.set_text(str(c) if c else "")
        badge.set_visibility(c > 0)

        if c > _prev_count[0]:
            # How many new ones arrived since last check
            new_items = list_notifications(limit=c - _prev_count[0])
            for n in new_items[:3]:          # cap popup spam at 3
                ui.notify(
                    f"🚨 {n['class_name']} detected\n"
                    f"🖥  {n['computer']}   👤 {n['student']}\n"
                    f"🕐 {n['created_at']}",
                    type="negative",
                    position="top-right",
                    timeout=8000,
                    close_button=True,
                )
            # Play alert sound via Web Audio API (no audio file needed)
            ui.run_javascript("""
                (function() {
                    try {
                        const ctx = new (window.AudioContext ||
                                         window.webkitAudioContext)();
                        function beep(freq, start, dur, vol) {
                            const o = ctx.createOscillator();
                            const g = ctx.createGain();
                            o.connect(g);
                            g.connect(ctx.destination);
                            o.type = 'sine';
                            o.frequency.value = freq;
                            g.gain.setValueAtTime(vol, ctx.currentTime + start);
                            g.gain.exponentialRampToValueAtTime(
                                0.001, ctx.currentTime + start + dur);
                            o.start(ctx.currentTime + start);
                            o.stop(ctx.currentTime + start + dur + 0.05);
                        }
                        beep(880, 0.0,  0.18, 0.4);
                        beep(660, 0.2,  0.18, 0.4);
                        beep(880, 0.4,  0.25, 0.5);
                    } catch(e) {}
                })();
            """)

        _prev_count[0] = c

    ui.timer(3.0, _refresh_badge)