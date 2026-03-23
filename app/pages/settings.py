"""
app/pages/settings.py
─────────────────────
Settings page  /settings  — all configuration in one form.
Values are persisted via NiceGUI's app.storage.general (JSON file).
"""
from nicegui import ui

from app.config import get_settings, save_settings
from app.core.auth import require_auth
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
            ui.label(title).classes("text-sm font-semibold text-gray-400 mb-1")

        def row(
            key:     str,
            label:   str,
            kind:    str       = "text",
            choices: list|None = None,
        ) -> None:
            with ui.row().classes("w-full items-center gap-4 py-1"):
                ui.label(label).classes("w-60 text-sm")
                if kind == "bool":
                    w = ui.checkbox(value=bool(s.get(key, False)))
                elif kind == "select":
                    w = ui.select(
                        choices or [], value=s.get(key)
                    ).props("dense outlined").classes("flex-1")
                else:
                    w = ui.input(value=str(s.get(key, ""))).props(
                        "dense outlined").classes("flex-1")
            widgets[key] = w

        section("Authentication")
        row("key_name", "Key name")
        row("key_path", "Key file path (.pem)")

        section("Veyon CLI")
        row("veyon_cli", "veyon-cli executable path")

        section("WebAPI Server")
        row("host",       "Host")
        row("port",       "Port")
        row("auto_start", "Auto-start server",   kind="bool")
        row("start_wait", "Start-up wait (s)")

        section("Capture")
        row("interval",    "Poll interval (s)")
        row("img_fmt",     "Image format",        kind="select", choices=["jpeg", "png"])
        row("img_quality", "JPEG quality (1–100)")
        row("img_width",   "Capture width (px)")

        section("Output")
        row("output_dir",     "Screenshots directory")
        row("save_raw",       "Save raw frames",       kind="bool")
        row("save_annotated", "Save annotated frames", kind="bool")

        ui.separator().classes("my-3")
        with ui.card().classes("w-full bg-blue-950 border border-blue-700"):
            ui.markdown(
                "**YOLO detection parameters** (model path, image size, "
                "confidence, IoU) are managed automatically by the "
                "**Models** page.  \n"
                "Activate a model there and all parameters update instantly."
            ).classes("text-sm text-blue-200")

        ui.separator().classes("my-4")

        def _save_all() -> None:
            save_settings({k: w.value for k, w in widgets.items()})
            ui.notify("✅ Settings saved — restart monitoring to apply",
                      type="positive")

        ui.button("💾  Save Settings", on_click=_save_all).props("color=primary size=md")
        ui.label("Changes take effect the next time you press Start.").classes(
            "text-xs text-gray-500 mt-2")