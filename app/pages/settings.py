"""
app/pages/settings.py
─────────────────────
Settings page  /settings  — all configuration in one form.
"""
from nicegui import ui

from app.config import get_settings, save_settings
from app.core.auth import require_auth
from app.db.database import log_action
from app.pages._nav import nav


@ui.page("/settings")
def page_settings() -> None:
    current = require_auth(required_role="teacher")
    if current is None:
        return
    nav(current)
    s = get_settings()
    widgets: dict[str, ui.element] = {}

    with ui.column().classes("max-w-2xl mx-auto p-4 gap-1"):
        ui.label("Settings").classes("text-xl font-bold mb-3")

        def section(title: str) -> None:
            ui.separator().classes("my-3")
            ui.label(title).classes(
                "text-sm font-semibold text-gray-600 dark:text-gray-400 mb-1")

        def row(
            key:     str,
            label:   str,
            kind:    str       = "text",
            choices: list|None = None,
            password: bool     = False,
        ) -> None:
            with ui.row().classes("w-full items-center gap-4 py-1"):
                ui.label(label).classes("w-60 text-sm")
                if kind == "bool":
                    w = ui.checkbox(value=bool(s.get(key, False)))
                elif kind == "select":
                    w = ui.select(
                        choices or [], value=s.get(key)
                    ).props("dense outlined").classes("flex-1")
                elif password:
                    w = ui.input(
                        value=str(s.get(key, "")),
                        password=True,
                        password_toggle_button=True,
                    ).props("dense outlined").classes("flex-1")
                else:
                    w = ui.input(value=str(s.get(key, ""))).props(
                        "dense outlined").classes("flex-1")
            widgets[key] = w

        # ── Authentication ────────────────────────────────────────────────────
        section("Authentication")

        with ui.row().classes("w-full items-center gap-4 py-1"):
            ui.label("Authentication method").classes("w-60 text-sm")
            auth_sel = ui.select(
                {"key": "Cryptographic key", "logon": "Logon (username/password)"},
                value=s.get("auth_method", "key"),
            ).props("dense outlined").classes("flex-1")
        widgets["auth_method"] = auth_sel

        # Key-based section
        with ui.column().classes("w-full gap-0") as key_section:
            row("key_name", "Key name")
            row("key_path", "Key file path (.pem)")

        # Logon section
        with ui.column().classes("w-full gap-0") as logon_section:
            row("logon_username", "Logon username")
            row("logon_password", "Logon password", password=True)

        def _update_auth_visibility(method: str) -> None:
            key_section.set_visibility(method == "key")
            logon_section.set_visibility(method == "logon")

        _update_auth_visibility(s.get("auth_method", "key"))
        auth_sel.on_value_change(lambda e: _update_auth_visibility(e.value))

        # ── Veyon CLI ─────────────────────────────────────────────────────────
        section("Veyon CLI")
        row("veyon_cli", "veyon-cli executable path")

        # ── WebAPI Server ─────────────────────────────────────────────────────
        section("WebAPI Server")
        row("host",       "Host")
        row("port",       "Port")
        row("auto_start", "Auto-start server",  kind="bool")
        row("start_wait", "Start-up wait (s)")

        # ── Capture ───────────────────────────────────────────────────────────
        section("Capture")
        row("interval",    "Poll interval (s)")
        row("img_fmt",     "Image format",       kind="select", choices=["jpeg", "png"])
        row("img_quality", "JPEG quality (1–100)")
        row("img_width",   "Capture width (px)")

        # ── Detection Parameters ──────────────────────────────────────────────
        section("Detection Parameters")
        with ui.card().classes(
            "w-full mb-2 "
            "bg-blue-50 border border-blue-300 "
            "dark:bg-blue-950 dark:border-blue-700"
        ):
            ui.markdown(
                "Model path and inference image size are set automatically "
                "when you **activate a model** on the Models page.  \n"
                "The parameters below take effect on the next monitoring start."
            ).classes("text-sm text-blue-900 dark:text-blue-200")
        row("detect_conf", "Confidence threshold (0–1)")
        row("detect_iou",  "IoU threshold for NMS (0–1)")
        row("keep_top1",   "Keep top-1 detection per class only", kind="bool")

        # ── Alert Behaviour ───────────────────────────────────────────────────
        section("Alert Behaviour")
        row("alert_threshold", "Consecutive detections before alert")
        with ui.row().classes("w-full"):
            ui.label(
                "Set to 1 to alert on every detection.  "
                "Set to 2 or 3 to require repeated detections before "
                "notifying (reduces false positives)."
            ).classes("text-xs text-gray-500 dark:text-gray-500")

        ui.separator().classes("my-4")

        _settings_user_id = current["id"]   # capture before closure shadows name

        def _save_all() -> None:
            s = get_settings()
            s.update({k: w.value for k, w in widgets.items()})
            save_settings(s)
            log_action("settings.save", user_id=_settings_user_id,
                       detail=f"auth_method={s.get('auth_method')}, "
                              f"veyon_cli={s.get('veyon_cli')}")
            ui.notify("✅ Settings saved — restart monitoring to apply",
                      type="positive")

        ui.button("💾  Save Settings", on_click=_save_all).props(
            "color=primary size=md")
        ui.label("Changes take effect the next time you press Start.").classes(
            "text-xs text-gray-500 dark:text-gray-500 mt-2")
