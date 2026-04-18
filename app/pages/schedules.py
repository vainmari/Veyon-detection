"""
app/pages/schedules.py
──────────────────────
Monitoring Schedules page  /schedules  (teacher only)

Define weekly time windows for each computer group.
Each schedule can optionally pin a specific ML model and/or a custom set of
detection classes to notify on (overriding the global notification_enabled flags).
"""
from nicegui import ui

from app.core.auth import require_auth
from app.db.database import (
    create_schedule,
    delete_schedule,
    find_overlapping_schedules,
    get_active_model,
    get_class_ids_for_model,
    get_schedule,
    list_classes,
    list_groups,
    list_models,
    list_schedules,
    log_action,
    sync_classes_from_model,
    update_schedule,
)
from app.pages._nav import nav
from app.translate import t

_ALL_DAYS = ["0", "1", "2", "3", "4", "5", "6"]


def _day_labels() -> dict[str, str]:
    return {
        "0": t("schedules_day_mon"),
        "1": t("schedules_day_tue"),
        "2": t("schedules_day_wed"),
        "3": t("schedules_day_thu"),
        "4": t("schedules_day_fri"),
        "5": t("schedules_day_sat"),
        "6": t("schedules_day_sun"),
    }


def _fmt_days(days_str: str) -> str:
    labels = _day_labels()
    return ", ".join(labels.get(d, d) for d in days_str.split(",") if d)


@ui.page("/schedules")
def page_schedules() -> None:
    current = require_auth(required_role="teacher")
    if current is None:
        return
    nav(current)

    with ui.column().classes("w-full max-w-4xl mx-auto p-4 gap-4"):
        ui.label(t("schedules_title")).classes("text-xl font-bold")

        ui.markdown(t("schedules_intro")).classes(
            "text-sm text-gray-500 dark:text-gray-400")

        groups  = list_groups()
        models  = list_models()

        # Ensure every model's classes exist in detection_class + model_class.
        # sync_classes_from_model is idempotent so this is safe to run on every
        # page load and fixes models that were imported before the sync was added.
        for m in models:
            if not get_class_ids_for_model(m["id"]):
                sync_classes_from_model(m["id"])

        # Reload classes after sync so newly-added ones are included.
        classes = list_classes()

        # model_id → ordered list of detection_class dicts for that model,
        # preserving the order from classes_json.
        _cls_by_id:   dict[int, dict] = {c["id"]: c for c in classes}
        _cls_by_name: dict[str, int]  = {c["name"]: c["id"] for c in classes}

        def _ordered_classes_for_model(m: dict) -> list[dict]:
            """Return detection_class rows for this model in classes_json order."""
            ids = set(get_class_ids_for_model(m["id"]))
            # Order by the model's own class name list so the UI matches the model
            result = []
            seen: set[int] = set()
            for name in (m.get("class_names") or []):
                cid = _cls_by_name.get(name)
                if cid and cid in ids and cid not in seen:
                    result.append(_cls_by_id[cid])
                    seen.add(cid)
            # Append any remaining IDs not covered by names (shouldn't happen, but safe)
            for cid in ids:
                if cid not in seen:
                    result.append(_cls_by_id[cid])
            return result

        model_classes: dict[int, list[dict]] = {
            m["id"]: _ordered_classes_for_model(m) for m in models
        }

        # Active model — used as fallback when schedule has no pinned model
        active_model = get_active_model()

        if not groups:
            with ui.card().classes("w-full bg-yellow-50 dark:bg-yellow-950 "
                                   "border border-yellow-300 dark:border-yellow-700"):
                ui.label(t("schedules_no_groups")).classes(
                    "text-sm text-yellow-800 dark:text-yellow-200")

        # ── Create / Edit dialog ──────────────────────────────────────────────
        with ui.dialog() as dlg, ui.card().classes("p-5 gap-3").style(
            "min-width:520px; max-width:95vw;"
        ):
            dlg_title = ui.label("").classes("text-lg font-bold mb-1")
            dlg_id    = [None]

            with ui.row().classes("w-full items-center gap-3"):
                ui.label(t("schedules_field_name")).classes("w-36 text-sm")
                dlg_name = ui.input().props("dense outlined").classes("flex-1")

            with ui.row().classes("w-full items-center gap-3"):
                ui.label(t("schedules_field_group")).classes("w-36 text-sm")
                group_opts = {str(g["id"]): g["name"] for g in groups}
                dlg_group = ui.select(
                    group_opts,
                    value=str(groups[0]["id"]) if groups else None,
                ).props("dense outlined").classes("flex-1")

            # class_check_rows rebuilt on every model change: class_id → checkbox
            class_check_rows: dict[int, ui.checkbox] = {}
            # Tracks which IDs should be checked after a rebuild
            _restore_ids: list[set] = [set()]

            # Default model: active model, or first in list if none active
            _default_mid: int | None = (
                active_model["id"] if active_model
                else (models[0]["id"] if models else None)
            )

            def _rebuild_class_list(model_val: str, restore_ids: set | None = None) -> None:
                ids_to_check = restore_ids if restore_ids is not None else _restore_ids[0]
                # Resolve model id: fall back to default if val is empty/missing
                mid = int(model_val) if model_val else _default_mid
                visible = model_classes.get(mid, []) if mid is not None else []

                class_check_rows.clear()
                class_section.clear()
                with class_section:
                    if not visible:
                        ui.label("—").classes("text-xs text-gray-400")
                    for cls in visible:
                        cb = ui.checkbox(cls["name"], value=(cls["id"] in ids_to_check))
                        class_check_rows[cls["id"]] = cb

            # ── Model selector ────────────────────────────────────────────────
            with ui.row().classes("w-full items-center gap-3"):
                ui.label(t("schedules_field_model")).classes("w-36 text-sm")
                active_id = active_model["id"] if active_model else None

                def _model_label(m: dict) -> str:
                    if m["id"] == active_id:
                        return f"{m['name']}  ({t('schedules_model_currently_active')})"
                    return m["name"]

                model_opts: dict = {str(m["id"]): _model_label(m) for m in models}
                dlg_model = ui.select(
                    model_opts,
                    value=str(_default_mid) if _default_mid else None,
                    on_change=lambda e: _rebuild_class_list(e.value),
                ).props("dense outlined").classes("flex-1")

            ui.label(t("schedules_days_label")).classes(
                "text-sm text-gray-500 dark:text-gray-400 mt-1")
            day_labels = _day_labels()
            day_checks: dict[str, ui.checkbox] = {}
            with ui.row().classes("gap-3 flex-wrap"):
                for k in _ALL_DAYS:
                    day_checks[k] = ui.checkbox(day_labels[k], value=(int(k) < 5))

            with ui.row().classes("w-full items-center gap-3 mt-1"):
                ui.label(t("schedules_start_time")).classes("w-36 text-sm")
                dlg_start = ui.input(value="08:00").props(
                    "dense outlined mask=##:## placeholder=HH:MM"
                ).classes("w-28")

            with ui.row().classes("w-full items-center gap-3"):
                ui.label(t("schedules_end_time")).classes("w-36 text-sm")
                dlg_end = ui.input(value="14:00").props(
                    "dense outlined mask=##:## placeholder=HH:MM"
                ).classes("w-28")

            dlg_active = ui.checkbox(t("schedules_active"), value=True)

            # ── Notification class selector ────────────────────────────────────
            ui.separator().classes("my-1")
            ui.label(t("schedules_notify_section")).classes("text-sm font-semibold mt-1")
            ui.label(t("schedules_notify_classes_hint")).classes(
                "text-xs text-gray-500 dark:text-gray-400")

            # Populated dynamically by _rebuild_class_list
            with ui.column().classes("gap-1 pl-2") as class_section:
                pass
            _rebuild_class_list(str(_default_mid) if _default_mid else "")   # initial fill

            with ui.row().classes("gap-2 mt-2"):
                def _save() -> None:
                    name = dlg_name.value.strip()
                    if not name:
                        ui.notify(t("schedules_err_name"), type="negative")
                        return
                    selected_days = ",".join(k for k in _ALL_DAYS if day_checks[k].value)
                    if not selected_days:
                        ui.notify(t("schedules_err_days"), type="negative")
                        return
                    if not dlg_start.value or not dlg_end.value:
                        ui.notify(t("schedules_err_times"), type="negative")
                        return
                    if dlg_start.value >= dlg_end.value:
                        ui.notify(t("schedules_err_order"), type="negative")
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
                            t("schedules_err_conflict").format(names=names),
                            type="negative",
                        )
                        return

                    model_val  = dlg_model.value
                    mid        = int(model_val) if model_val else None
                    notify_ids = [cid for cid, cb in class_check_rows.items() if cb.value]
                    # use_custom = True whenever the user has checked any specific class
                    use_custom = bool(notify_ids)

                    labels  = _day_labels()
                    day_str = ", ".join(labels[d] for d in selected_days.split(",") if d)
                    detail  = (f"name={name}, days={day_str}, "
                               f"{dlg_start.value}–{dlg_end.value}")

                    if dlg_id[0] is None:
                        sid = create_schedule(
                            group_id=gid,
                            name=name,
                            days_of_week=selected_days,
                            start_time=dlg_start.value,
                            end_time=dlg_end.value,
                            created_by=current["id"],
                            model_id=mid,
                            use_custom_notify_classes=use_custom,
                            notify_class_ids=notify_ids,
                        )
                        log_action("schedule.create", user_id=current["id"],
                                   entity="schedule", entity_id=sid,
                                   detail=detail)
                        ui.notify(t("schedules_created").format(name=name), type="positive")
                    else:
                        update_schedule(
                            dlg_id[0], name, selected_days,
                            dlg_start.value, dlg_end.value,
                            bool(dlg_active.value),
                            model_id=mid,
                            use_custom_notify_classes=use_custom,
                            notify_class_ids=notify_ids,
                        )
                        log_action("schedule.update", user_id=current["id"],
                                   entity="schedule", entity_id=dlg_id[0],
                                   detail=detail)
                        ui.notify(t("schedules_updated").format(name=name), type="positive")
                    dlg.close()
                    _schedules_panel.refresh()

                ui.button(t("schedules_save"), icon="save", on_click=_save).props("color=primary")
                ui.button(t("schedules_cancel"), on_click=dlg.close).props("flat")

        def _open_create() -> None:
            dlg_id[0] = None
            dlg_title.set_text(t("schedules_new"))
            dlg_name.set_value("")
            for k in _ALL_DAYS:
                day_checks[k].set_value(int(k) < 5)
            dlg_start.set_value("08:00")
            dlg_end.set_value("14:00")
            dlg_active.set_value(True)
            _restore_ids[0] = set()
            dlg_model.set_value(str(_default_mid) if _default_mid else None)
            _rebuild_class_list(str(_default_mid) if _default_mid else "", restore_ids=set())
            dlg.open()

        def _open_edit(s: dict) -> None:
            dlg_id[0] = s["id"]
            dlg_title.set_text(t("schedules_edit").format(name=s["name"]))
            dlg_name.set_value(s["name"])
            dlg_group.set_value(str(s["group_id"]) if s.get("group_id") else None)
            active_days = s.get("days_of_week", "").split(",")
            for k in _ALL_DAYS:
                day_checks[k].set_value(k in active_days)
            dlg_start.set_value(s["start_time"])
            dlg_end.set_value(s["end_time"])
            dlg_active.set_value(bool(s["is_active"]))

            model_val  = str(s["model_id"]) if s.get("model_id") else (str(_default_mid) if _default_mid else "")
            saved_ids  = set(s.get("notify_class_ids") or [])
            _restore_ids[0] = saved_ids
            dlg_model.set_value(model_val)
            _rebuild_class_list(model_val, restore_ids=saved_ids)
            dlg.open()

        # ── Schedules panel ───────────────────────────────────────────────────
        with ui.row().classes("items-center justify-between w-full"):
            ui.label("").classes("flex-1")
            ui.button(t("schedules_btn_new"), on_click=_open_create).props(
                "color=primary dense")

        @ui.refreshable
        def _schedules_panel() -> None:
            schedules = list_schedules()
            if not schedules:
                with ui.card().classes("w-full"):
                    ui.label(t("schedules_none")).classes("text-sm text-gray-500")
                return

            cols = [
                {"name": "name",    "label": t("schedules_col_name"),
                 "field": "name",       "sortable": True,  "align": "left"},
                {"name": "group",   "label": t("schedules_col_group"),
                 "field": "group_name", "sortable": True,  "align": "left"},
                {"name": "model",   "label": t("schedules_col_model"),
                 "field": "model_name", "sortable": True,  "align": "left"},
                {"name": "days",    "label": t("schedules_col_days"),
                 "field": "days_of_week","sortable": False, "align": "left"},
                {"name": "start",   "label": t("schedules_col_start"),
                 "field": "start_time", "sortable": True,  "align": "center"},
                {"name": "end",     "label": t("schedules_col_end"),
                 "field": "end_time",   "sortable": True,  "align": "center"},
                {"name": "notify",  "label": t("schedules_col_notify"),
                 "field": "notify_class_count", "sortable": False, "align": "center"},
                {"name": "active",  "label": t("schedules_col_active"),
                 "field": "is_active",  "sortable": True,  "align": "center"},
                {"name": "actions", "label": "",
                 "field": "id",         "align": "right"},
            ]
            rows = []
            for s in schedules:
                n_custom = s.get("notify_class_count", 0)
                if s.get("use_custom_notify_classes"):
                    notify_badge = t("schedules_notify_custom_badge").format(n=n_custom)
                else:
                    notify_badge = t("schedules_notify_global_badge")
                rows.append({
                    **s,
                    "group_name":    s.get("group_name") or "—",
                    "model_name":    s.get("model_name") or "—",
                    "days_of_week":  _fmt_days(s.get("days_of_week", "")),
                    "notify_badge":  notify_badge,
                })

            tbl = ui.table(columns=cols, rows=rows, row_key="id").classes("w-full")
            tbl.props("dense flat bordered")

            tbl.add_slot("body-cell-active", """
                <q-td :props="props">
                    <q-icon :name="props.row.is_active ? 'check_circle' : 'cancel'"
                            :color="props.row.is_active ? 'green' : 'red'" size="sm"/>
                </q-td>""")
            tbl.add_slot("body-cell-notify", """
                <q-td :props="props">
                    <q-badge :color="props.row.use_custom_notify_classes ? 'blue' : 'gray'"
                             class="text-xs">
                        {{ props.row.notify_badge }}
                    </q-badge>
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
                s   = next((x for x in schedules if x["id"] == int(sid)), None)
                if s:
                    full = get_schedule(s["id"]) or s
                    _open_edit(full)

            def on_delete(e) -> None:
                sid = e.args.get("id")
                if sid:
                    sid_int = int(sid)
                    s = next((x for x in schedules if x["id"] == sid_int), None)
                    delete_schedule(sid_int)
                    log_action("schedule.delete", user_id=current["id"],
                               entity="schedule", entity_id=sid_int,
                               detail=f"name={s['name'] if s else '?'}")
                    ui.notify(t("schedules_deleted"), type="warning")
                    _schedules_panel.refresh()

            tbl.on("edit", on_edit)
            tbl.on("del",  on_delete)

        _schedules_panel()
