"""
app/pages/alerts.py
────────────────────
Alert Rules page  /alerts  (teacher only)
"""
from nicegui import ui

import app.state as state
from app.core.auth import require_auth
from app.db.database import (
    get_active_model, get_model_by_id, get_prohibited_class_ids,
    list_alert_rules, set_alert_rule,
)
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
            # When monitoring is running, use the model it's actually using.
            # A schedule may pin a different model than the DB-active one.
            db_active   = get_active_model()
            running_mid = state.running_model_id if state.monitor else None
            schedule_id = getattr(state.monitor, "schedule_id", None) if state.monitor else None

            if running_mid is not None and running_mid != (db_active or {}).get("id"):
                active = get_model_by_id(running_mid)
                is_schedule_override = True
            else:
                active = db_active
                is_schedule_override = False

            # Compute effective prohibited class IDs for the running session.
            # This respects per-schedule custom class overrides.
            effective_prohibited: set[int] | None = None
            schedule_has_custom = False
            if state.monitor and running_mid is not None:
                prohibited_map = get_prohibited_class_ids(running_mid, schedule_id)
                effective_prohibited = {v["id"] for v in prohibited_map.values()}
                # Check if this is a schedule-specific override (not just global)
                if schedule_id is not None:
                    from app.db.database import get_schedule
                    s = get_schedule(schedule_id)
                    schedule_has_custom = bool(s and s.get("use_custom_notify_classes"))

            if active:
                active_names = {n.strip() for n in active.get("class_names", [])}
                label_text = t("alerts_classes_active").format(name=active["name"])
                if is_schedule_override:
                    label_text += f"  ({t('alerts_classes_running')})"
                ui.label(label_text).classes(
                    "text-sm font-semibold text-gray-600 dark:text-gray-400 mb-3")
            else:
                active_names = None
                ui.label(t("alerts_classes_all")).classes(
                    "text-sm font-semibold text-gray-600 dark:text-gray-400 mb-3")

            # Banner when a schedule overrides the global prohibited-class list
            if schedule_has_custom:
                with ui.row().classes("items-center gap-2 mb-2 px-1 py-1 rounded "
                                      "bg-blue-50 dark:bg-blue-950 "
                                      "border border-blue-200 dark:border-blue-700"):
                    ui.icon("info", size="xs").classes("text-blue-500")
                    ui.label(t("alerts_schedule_override_note")).classes(
                        "text-xs text-blue-700 dark:text-blue-300")

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
                    # When monitoring is live, derive effective enabled from the
                    # actual prohibited set (respects schedule overrides).
                    eff = (r["class_id"] in effective_prohibited
                           if effective_prohibited is not None
                           else bool(r["enabled"]))
                    _class_row(r, effective_enabled=eff,
                               readonly=schedule_has_custom)

        ui.label(t("alerts_changes_note")).classes(
            "text-xs text-gray-500 dark:text-gray-500")


def _class_row(
    r: dict,
    effective_enabled: bool | None = None,
    readonly: bool = False,
) -> None:
    """
    Render a single class row.

    effective_enabled — when provided, overrides r["enabled"] for the status
                        label and toggle display (reflects the live session state).
    readonly          — when True the toggle is disabled (schedule override active;
                        editing global rules won't affect the current session).
    """
    display_enabled = effective_enabled if effective_enabled is not None else bool(r["enabled"])

    with ui.card().classes("w-full").style("padding: 10px 16px;"):
        with ui.row().classes("items-center gap-4 w-full"):

            ui.element("div").style(
                f"width:14px; height:14px; border-radius:50%; "
                f"background:{r['color_hex']}; flex-shrink:0;"
            )

            ui.label(r["name"]).classes("flex-1 text-sm font-mono")

            status = ui.label(
                t("alerts_prohibited") if display_enabled else t("alerts_allowed")
            ).classes(
                "text-xs " +
                ("text-red-500 dark:text-red-400"
                 if display_enabled else
                 "text-green-600 dark:text-green-400")
            )

            toggle = ui.switch(value=display_enabled)
            if readonly:
                toggle.props("disable")
                toggle.tooltip(t("alerts_toggle_readonly_hint"))

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

            if not readonly:
                toggle.on_value_change(on_change)
