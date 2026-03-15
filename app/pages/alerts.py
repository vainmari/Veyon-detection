"""
app/pages/alerts.py
────────────────────
Alert Rules page  /alerts  (teacher only)

Teachers mark which detection classes are "prohibited".
When the monitor detects a prohibited class, a notification is fired
immediately and appears in the bell icon on every page.
"""
from nicegui import ui

from app.core.auth import require_auth
from app.db.database import list_alert_rules, set_alert_rule
from app.pages._nav import nav


@ui.page("/alerts")
def page_alerts() -> None:
    current = require_auth(required_role="teacher")
    if current is None:
        return
    nav(current)

    with ui.column().classes("w-full max-w-2xl mx-auto p-4 gap-4"):
        ui.label("Alert Rules").classes("text-xl font-bold")

        with ui.card().classes("w-full bg-yellow-950 border border-yellow-700"):
            ui.markdown(
                "**Mark classes as prohibited** to receive an instant notification "
                "whenever that class is detected on any monitored computer.  \n"
                "Notifications include the detection time, computer, student name, "
                "and a snapshot of the screen as evidence."
            ).classes("text-sm text-yellow-200")

        with ui.card().classes("w-full"):
            ui.label("Detection Classes").classes(
                "text-sm font-semibold text-gray-400 mb-3")

            rules = list_alert_rules()
            if not rules:
                ui.label("No detection classes found — start monitoring first.").classes(
                    "text-gray-500 text-sm")
            else:
                for r in rules:
                    _class_row(r)

        ui.label(
            "Changes take effect immediately — no restart needed."
        ).classes("text-xs text-gray-500")


def _class_row(r: dict) -> None:
    """One row per detection class with a toggle and color indicator."""
    with ui.card().classes("w-full").style("padding: 10px 16px;"):
        with ui.row().classes("items-center gap-4 w-full"):

            # Color swatch
            ui.element("div").style(
                f"width:14px; height:14px; border-radius:50%; "
                f"background:{r['color_hex']}; flex-shrink:0;"
            )

            # Class name
            ui.label(r["name"]).classes("flex-1 text-sm font-mono")

            # Status label — updates when toggle changes
            status = ui.label(
                "🚫 Prohibited" if r["enabled"] else "✅ Allowed"
            ).classes(
                "text-xs " +
                ("text-red-400" if r["enabled"] else "text-green-400")
            )

            # Toggle — auto-saves
            toggle = ui.switch(value=bool(r["enabled"]))

            def on_change(e, cid=r["class_id"], st=status, tg=toggle) -> None:
                enabled = bool(e.value)
                set_alert_rule(cid, enabled)
                if enabled:
                    st.set_text("🚫 Prohibited")
                    st.classes(replace="text-xs text-red-400")
                else:
                    st.set_text("✅ Allowed")
                    st.classes(replace="text-xs text-green-400")
                ui.notify(
                    f"{'Alert enabled' if enabled else 'Alert disabled'} "
                    f"for {tg._props.get('label', '')}",
                    type="positive" if enabled else "info",
                )

            toggle.on_value_change(on_change)