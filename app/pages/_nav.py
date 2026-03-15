"""
app/pages/_nav.py
─────────────────
Shared navigation bar with inline Start / Stop monitor controls (teacher only).
A 1 s timer keeps button states in sync across all pages.
"""
from nicegui import ui

import app.state as state
from app.core.auth import clear_session


def nav(current_user: dict) -> None:
    role = current_user["role"]

    with ui.header().classes(
        "bg-gray-900 text-white px-4 py-2 flex items-center gap-4 shadow-md"
    ):
        ui.label("🎓 Veyon AI Monitor").classes("font-bold text-base mr-2")

        if role == "teacher":
            ui.link("Dashboard", "/").classes(
                "text-gray-300 hover:text-white text-sm no-underline")
            ui.link("History", "/history").classes(
                "text-gray-300 hover:text-white text-sm no-underline")
            ui.link("Users", "/users").classes(
                "text-gray-300 hover:text-white text-sm no-underline")
            ui.link("Settings", "/settings").classes(
                "text-gray-300 hover:text-white text-sm no-underline")
        else:
            ui.link("My History", "/history").classes(
                "text-gray-300 hover:text-white text-sm no-underline")

        # ── Monitor start / stop (teacher only) ───────────────────────────────
        if role == "teacher":
            ui.separator().props("vertical").classes("mx-1 opacity-30")

            status_dot = ui.label("").classes("text-xs font-mono")
            btn_start  = ui.button("▶ Start", color="green").props("dense unelevated size=sm")
            btn_stop   = ui.button("■ Stop",  color="red"  ).props("dense unelevated size=sm")

            def do_start() -> None:
                if state.monitor:
                    return
                # Lazy import avoids circular dependency
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

            _sync_buttons()                    # initial state on page load
            ui.timer(1.0, _sync_buttons)       # keep in sync while page is open

        # ── Right: username + sign-out ────────────────────────────────────────
        with ui.row().classes("ml-auto items-center gap-3"):
            role_color = "text-orange-400" if role == "teacher" else "text-blue-400"
            ui.label(current_user["username"]).classes(f"text-sm font-mono {role_color}")
            ui.label(f"({role})").classes("text-xs text-gray-500")
            ui.button(
                "Sign out",
                on_click=lambda: (clear_session(), ui.navigate.to("/login")),
            ).props("flat dense size=sm color=red")