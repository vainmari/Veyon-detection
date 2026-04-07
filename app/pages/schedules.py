"""
app/pages/schedules.py
──────────────────────
Monitoring Schedules page  /schedules  (teacher only)

Define weekly time windows for each computer group.
The schedule table drives automatic monitoring activation
(a background ticker can call get_active_schedules_now() to decide
whether to start/stop MonitorController automatically).
"""
from nicegui import ui

from app.core.auth import require_auth
from app.db.database import (
    create_schedule,
    delete_schedule,
    find_overlapping_schedules,
    list_groups,
    list_schedules,
    log_action,
    update_schedule,
)
from app.pages._nav import nav

_DAY_LABELS = {
    "0": "Mon", "1": "Tue", "2": "Wed",
    "3": "Thu", "4": "Fri", "5": "Sat", "6": "Sun",
}
_ALL_DAYS = list(_DAY_LABELS.keys())


def _fmt_days(days_str: str) -> str:
    return ", ".join(_DAY_LABELS.get(d, d) for d in days_str.split(",") if d)


@ui.page("/schedules")
def page_schedules() -> None:
    current = require_auth(required_role="teacher")
    if current is None:
        return
    nav(current)

    with ui.column().classes("w-full max-w-4xl mx-auto p-4 gap-4"):
        ui.label("Monitoring Schedules").classes("text-xl font-bold")

        ui.markdown(
            "Set up weekly monitoring windows per computer group.  \n"
            "Each schedule defines which **days** and **time range** a group should be monitored."
        ).classes("text-sm text-gray-500 dark:text-gray-400")

        groups = list_groups()
        if not groups:
            with ui.card().classes("w-full bg-yellow-50 dark:bg-yellow-950 "
                                   "border border-yellow-300 dark:border-yellow-700"):
                ui.label(
                    "No computer groups exist yet — create groups first on the Groups page."
                ).classes("text-sm text-yellow-800 dark:text-yellow-200")

        # ── Create / Edit dialog ──────────────────────────────────────────────
        with ui.dialog() as dlg, ui.card().classes("p-5 gap-3").style(
            "min-width:500px; max-width:95vw;"
        ):
            dlg_title = ui.label("").classes("text-lg font-bold mb-1")
            dlg_id    = [None]

            with ui.row().classes("w-full items-center gap-3"):
                ui.label("Name").classes("w-36 text-sm")
                dlg_name = ui.input().props("dense outlined").classes("flex-1")

            with ui.row().classes("w-full items-center gap-3"):
                ui.label("Group").classes("w-36 text-sm")
                group_opts = {str(g["id"]): g["name"] for g in groups}
                dlg_group = ui.select(
                    group_opts,
                    value=str(groups[0]["id"]) if groups else None,
                ).props("dense outlined").classes("flex-1")

            ui.label("Days of week").classes("text-sm text-gray-500 dark:text-gray-400 mt-1")
            day_checks: dict[str, ui.checkbox] = {}
            with ui.row().classes("gap-3 flex-wrap"):
                for k, label in _DAY_LABELS.items():
                    day_checks[k] = ui.checkbox(label, value=(int(k) < 5))

            with ui.row().classes("w-full items-center gap-3 mt-1"):
                ui.label("Start time").classes("w-36 text-sm")
                dlg_start = ui.input(value="08:00").props(
                    "dense outlined mask=##:## placeholder=HH:MM"
                ).classes("w-28")

            with ui.row().classes("w-full items-center gap-3"):
                ui.label("End time").classes("w-36 text-sm")
                dlg_end = ui.input(value="14:00").props(
                    "dense outlined mask=##:## placeholder=HH:MM"
                ).classes("w-28")

            dlg_active = ui.checkbox("Active", value=True)

            with ui.row().classes("gap-2 mt-2"):
                def _save() -> None:
                    name = dlg_name.value.strip()
                    if not name:
                        ui.notify("Name is required.", type="negative")
                        return
                    selected_days = ",".join(k for k in _ALL_DAYS if day_checks[k].value)
                    if not selected_days:
                        ui.notify("Select at least one day.", type="negative")
                        return
                    if not dlg_start.value or not dlg_end.value:
                        ui.notify("Set both start and end times.", type="negative")
                        return
                    if dlg_start.value >= dlg_end.value:
                        ui.notify("End time must be after start time.", type="negative")
                        return
                    gid = int(dlg_group.value) if dlg_group.value else None
                    conflicts = find_overlapping_schedules(
                        selected_days,
                        dlg_start.value, dlg_end.value,
                        exclude_id=dlg_id[0],
                    )
                    if conflicts:
                        names = ", ".join(f"'{c['name']}'" for c in conflicts)
                        ui.notify(
                            f"Time conflict with {names}. "
                            "Overlapping schedules are not allowed.",
                            type="negative",
                        )
                        return
                    day_str = ", ".join(
                        _DAY_LABELS[d] for d in selected_days.split(",") if d
                    )
                    detail = (f"name={name}, days={day_str}, "
                              f"{dlg_start.value}–{dlg_end.value}")
                    if dlg_id[0] is None:
                        sid = create_schedule(
                            group_id=gid,
                            name=name,
                            days_of_week=selected_days,
                            start_time=dlg_start.value,
                            end_time=dlg_end.value,
                            created_by=current["id"],
                        )
                        log_action("schedule.create", user_id=current["id"],
                                   entity="schedule", entity_id=sid,
                                   detail=detail)
                        ui.notify(f"Schedule '{name}' created.", type="positive")
                    else:
                        update_schedule(
                            dlg_id[0], name, selected_days,
                            dlg_start.value, dlg_end.value,
                            bool(dlg_active.value),
                        )
                        log_action("schedule.update", user_id=current["id"],
                                   entity="schedule", entity_id=dlg_id[0],
                                   detail=detail)
                        ui.notify(f"Schedule '{name}' updated.", type="positive")
                    dlg.close()
                    _schedules_panel.refresh()

                ui.button("Save", icon="save", on_click=_save).props("color=primary")
                ui.button("Cancel", on_click=dlg.close).props("flat")

        def _open_create() -> None:
            dlg_id[0] = None
            dlg_title.set_text("New Schedule")
            dlg_name.set_value("")
            for k in _ALL_DAYS:
                day_checks[k].set_value(int(k) < 5)
            dlg_start.set_value("08:00")
            dlg_end.set_value("14:00")
            dlg_active.set_value(True)
            dlg.open()

        def _open_edit(s: dict) -> None:
            dlg_id[0] = s["id"]
            dlg_title.set_text(f"Edit — {s['name']}")
            dlg_name.set_value(s["name"])
            dlg_group.set_value(str(s["group_id"]) if s.get("group_id") else None)
            active_days = s.get("days_of_week", "").split(",")
            for k in _ALL_DAYS:
                day_checks[k].set_value(k in active_days)
            dlg_start.set_value(s["start_time"])
            dlg_end.set_value(s["end_time"])
            dlg_active.set_value(bool(s["is_active"]))
            dlg.open()

        # ── Schedules panel ───────────────────────────────────────────────────
        with ui.row().classes("items-center justify-between w-full"):
            ui.label("").classes("flex-1")
            ui.button("+ New Schedule", on_click=_open_create).props(
                "color=primary dense")

        @ui.refreshable
        def _schedules_panel() -> None:
            schedules = list_schedules()
            if not schedules:
                with ui.card().classes("w-full"):
                    ui.label("No schedules yet.").classes("text-sm text-gray-500")
                return

            cols = [
                {"name": "name",    "label": "Name",      "field": "name",
                 "sortable": True,  "align": "left"},
                {"name": "group",   "label": "Group",     "field": "group_name",
                 "sortable": True,  "align": "left"},
                {"name": "days",    "label": "Days",      "field": "days_of_week",
                 "sortable": False, "align": "left"},
                {"name": "start",   "label": "Start",     "field": "start_time",
                 "sortable": True,  "align": "center"},
                {"name": "end",     "label": "End",       "field": "end_time",
                 "sortable": True,  "align": "center"},
                {"name": "active",  "label": "Active",    "field": "is_active",
                 "sortable": True,  "align": "center"},
                {"name": "actions", "label": "",          "field": "id",
                 "align": "right"},
            ]
            rows = [
                {**s,
                 "group_name":   s.get("group_name") or "—",
                 "days_of_week": _fmt_days(s.get("days_of_week", ""))}
                for s in schedules
            ]
            tbl = ui.table(columns=cols, rows=rows, row_key="id").classes("w-full")
            tbl.props("dense flat bordered")

            tbl.add_slot("body-cell-active", """
                <q-td :props="props">
                    <q-icon :name="props.row.is_active ? 'check_circle' : 'cancel'"
                            :color="props.row.is_active ? 'green' : 'red'" size="sm"/>
                </q-td>""")
            tbl.add_slot("body-cell-actions", """
                <q-td :props="props">
                    <q-btn flat dense round icon="edit" color="blue"
                           @click="$parent.$emit('edit', props.row)"/>
                    <q-btn flat dense round icon="delete" color="red"
                           @click="$parent.$emit('del', props.row)"/>
                </q-td>""")

            def on_edit(e) -> None:
                sid = e.args.get("id")
                s = next((x for x in schedules if x["id"] == int(sid)), None)
                if s:
                    _open_edit(s)

            def on_delete(e) -> None:
                sid = e.args.get("id")
                if sid:
                    sid_int = int(sid)
                    s = next((x for x in schedules if x["id"] == sid_int), None)
                    delete_schedule(sid_int)
                    log_action("schedule.delete", user_id=current["id"],
                               entity="schedule", entity_id=sid_int,
                               detail=f"name={s['name'] if s else '?'}")
                    ui.notify("Schedule deleted.", type="warning")
                    _schedules_panel.refresh()

            tbl.on("edit", on_edit)
            tbl.on("del",  on_delete)

        _schedules_panel()
