"""
app/pages/alerts.py
────────────────────
Alert Rules page  /alerts  (teacher only)
"""
from nicegui import ui

from app.core.auth import require_auth
from app.db.database import get_active_model, list_alert_rules, set_alert_rule
from app.pages._nav import nav
from app.translate import t


@ui.page("/alerts")
def page_alerts() -> None:
    current = require_auth(required_role="teacher")
    if current is None:
        return
    nav(current)

    with ui.column().classes("w-full max-w-2xl mx-auto p-4 gap-4"):
        ui.label(t("alerts_title")).classes("text-xl font-bold")

        with ui.card().classes(
            "w-full "
            "bg-yellow-50 border border-yellow-300 "
            "dark:bg-yellow-950 dark:border-yellow-700"
        ):
            ui.markdown(t("alerts_info")).classes(
                "text-sm text-yellow-900 dark:text-yellow-200")

        with ui.card().classes("w-full"):
            active = get_active_model()
            if active:
                active_names = {n.strip() for n in active.get("class_names", [])}
                ui.label(
                    t("alerts_classes_active").format(name=active["name"])
                ).classes("text-sm font-semibold text-gray-600 dark:text-gray-400 mb-3")
            else:
                active_names = None
                ui.label(t("alerts_classes_all")).classes(
                    "text-sm font-semibold text-gray-600 dark:text-gray-400 mb-3")

            rules = list_alert_rules()
            if active_names is not None:
                rules = [r for r in rules if r["name"] in active_names]

            if not rules:
                msg = (
                    t("alerts_none_active")
                    if active_names is not None
                    else t("alerts_none_any")
                )
                ui.label(msg).classes("text-gray-600 dark:text-gray-500 text-sm")
            else:
                for r in rules:
                    _class_row(r)

        ui.label(t("alerts_changes_note")).classes(
            "text-xs text-gray-500 dark:text-gray-500")


def _class_row(r: dict) -> None:
    with ui.card().classes("w-full").style("padding: 10px 16px;"):
        with ui.row().classes("items-center gap-4 w-full"):

            ui.element("div").style(
                f"width:14px; height:14px; border-radius:50%; "
                f"background:{r['color_hex']}; flex-shrink:0;"
            )

            ui.label(r["name"]).classes("flex-1 text-sm font-mono")

            status = ui.label(
                t("alerts_prohibited") if r["enabled"] else t("alerts_allowed")
            ).classes(
                "text-xs " +
                ("text-red-500 dark:text-red-400"
                 if r["enabled"] else
                 "text-green-600 dark:text-green-400")
            )

            toggle = ui.switch(value=bool(r["enabled"]))

            def on_change(e, cid=r["class_id"], name=r["name"], st=status) -> None:
                enabled = bool(e.value)
                set_alert_rule(cid, enabled)
                if enabled:
                    st.set_text(t("alerts_prohibited"))
                    st.classes(replace="text-xs text-red-500 dark:text-red-400")
                else:
                    st.set_text(t("alerts_allowed"))
                    st.classes(replace="text-xs text-green-600 dark:text-green-400")
                ui.notify(
                    t("alerts_enabled_for" if enabled else "alerts_disabled_for").format(name=name),
                    type="positive" if enabled else "info",
                )

            toggle.on_value_change(on_change)
