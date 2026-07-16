"""
app/pages/settings.py
─────────────────────
Settings page  /settings  — all configuration in one form.
Access: admin only (Administratorius → Tvarkyti nustatymus).
"""
from nicegui import ui

from app.config import get_settings, save_settings
from app.core.auth import require_auth
from app.db.database import log_action
from app.pages._file_browser import browse_file
from app.pages._nav import nav
from app.translate import t


@ui.page("/settings")
def page_settings() -> None:
    current = require_auth(required_role="admin")
    if current is None:
        return
    nav(current)
    s = get_settings()
    widgets: dict[str, ui.element] = {}

    with ui.column().classes("max-w-2xl mx-auto p-4 gap-1"):
        ui.label(t("settings_title")).classes("text-xl font-bold mb-3")

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
        section(t("settings_auth"))

        with ui.row().classes("w-full items-center gap-4 py-1"):
            ui.label(t("settings_auth_method")).classes("w-60 text-sm")
            auth_sel = ui.select(
                {"key": t("settings_auth_key"),
                 "logon": t("settings_auth_logon")},
                value=s.get("auth_method", "key"),
            ).props("dense outlined").classes("flex-1")
        widgets["auth_method"] = auth_sel

        with ui.column().classes("w-full gap-0") as key_section:
            row("key_name", t("settings_key_name"))
            
            with ui.row().classes("w-full items-center gap-4 py-1"):
                ui.label(t("settings_key_path")).classes("w-60 text-sm")
                key_path_inp = ui.input(
                    value=str(s.get("key_path", ""))
                ).props("dense outlined").classes("flex-1")

                ui.button(
                    icon="folder_open",
                    on_click=lambda: browse_file(
                        key_path_inp,
                        extensions=[".pem"],
                    ),
                ).props("flat dense").tooltip(t("settings_browse_pem"))
            widgets["key_path"] = key_path_inp


        with ui.column().classes("w-full gap-0") as logon_section:
            row("logon_username", t("settings_logon_user"))
            row("logon_password", t("settings_logon_pass"), password=True)

        def _update_auth_visibility(method: str) -> None:
            key_section.set_visibility(method == "key")
            logon_section.set_visibility(method == "logon")

        _update_auth_visibility(s.get("auth_method", "key"))
        auth_sel.on_value_change(lambda e: _update_auth_visibility(e.value))

        # ── Veyon CLI ─────────────────────────────────────────────────────────
        section(t("settings_veyon_cli"))
        with ui.row().classes("w-full items-center gap-4 py-1"):
            ui.label(t("settings_veyon_path")).classes("w-60 text-sm")

            veyon_inp = ui.input(
                value=str(s.get("veyon_cli", ""))
            ).props("dense outlined").classes("flex-1")

            ui.button(
                icon="folder_open",
                on_click=lambda: browse_file(
                    veyon_inp,
                    extensions=[".exe"]
                ),
            ).props("flat dense")

        widgets["veyon_cli"] = veyon_inp

        # ── WebAPI Server ─────────────────────────────────────────────────────
        section(t("settings_webapi"))
        row("host",       t("settings_host"))
        row("port",       t("settings_port"))
        row("auto_start", t("settings_auto_start"),  kind="bool")
        row("start_wait", t("settings_start_wait"))

        # ── Capture ───────────────────────────────────────────────────────────
        section(t("settings_capture"))
        row("interval",    t("settings_poll_interval"))
        row("img_fmt",     t("settings_img_format"),  kind="select", choices=["jpeg", "png"])
        row("img_quality", t("settings_jpeg_quality"))
        row("img_width",   t("settings_capture_width"))

        # ── Detection Parameters ──────────────────────────────────────────────
        section(t("settings_detect_params"))
        with ui.card().classes(
            "w-full mb-2 "
            "bg-blue-50 border border-blue-300 "
            "dark:bg-blue-950 dark:border-blue-700"
        ):
            ui.markdown(t("settings_detect_info")).classes(
                "text-sm text-blue-900 dark:text-blue-200")
        row("detect_conf", t("settings_detect_conf"))
        row("detect_iou",  t("settings_detect_iou"))
        row("keep_top1",   t("settings_keep_top1"), kind="bool")

        # ── Detection Performance ─────────────────────────────────────────────
        section(t("settings_detect_perf"))
        row("batch_max_cuda",       t("settings_batch_max_cuda"))
        row("batch_max_cpu",        t("settings_batch_max_cpu"))
        row("detect_cycle_timing",  t("settings_detect_cycle_timing"), kind="bool")
        with ui.row().classes("w-full"):
            ui.label(t("settings_detect_cycle_timing_hint")).classes(
                "text-xs text-gray-500 dark:text-gray-500")

        # ── Alert Behaviour ───────────────────────────────────────────────────
        section(t("settings_alert_behaviour"))
        row("alert_threshold", t("settings_alert_threshold"))
        with ui.row().classes("w-full"):
            ui.label(t("settings_alert_hint")).classes(
                "text-xs text-gray-500 dark:text-gray-500")

        # ── Data Retention ────────────────────────────────────────────────────
        section(t("settings_retention"))
        row("retention_days", t("settings_retention_days"))
        with ui.row().classes("w-full"):
            ui.label(t("settings_retention_hint")).classes(
                "text-xs text-gray-500 dark:text-gray-500")

        ui.separator().classes("my-4")

        _settings_user_id = current["id"]

        def _save_all() -> None:
            s = get_settings()
            s.update({k: w.value for k, w in widgets.items()})
            save_settings(s)
            log_action("settings.save", user_id=_settings_user_id,
                       detail=f"auth_method={s.get('auth_method')}, "
                              f"veyon_cli={s.get('veyon_cli')}")
            ui.notify(t("settings_saved"), type="positive")

        ui.button(t("settings_save_btn"), on_click=_save_all).props(
            "color=primary size=md")
        ui.label(t("settings_save_note")).classes(
            "text-xs text-gray-500 dark:text-gray-500 mt-2")
