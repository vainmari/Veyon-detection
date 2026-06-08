"""
app/pages/groups.py
────────────────────
Computer Groups page  /groups  (teacher only)

Teachers can create named groups (e.g. "Lab 1", "Exam Room"),
assign computers to them, and manage them here.
Computers can belong to multiple groups (many-to-many).
Groups are used by Schedules to target a set of computers.
"""
import asyncio

from nicegui import ui

from app.core.auth import require_auth
from app.db.database import (
    add_computer_to_group,
    create_group,
    delete_group,
    get_or_create_group,
    list_computers,
    list_computers_in_group,
    list_groups,
    log_action,
    remove_computer_from_group,
    update_group,
    upsert_computer,
)
from app.pages._nav import nav
from app.translate import t


@ui.page("/groups")
def page_groups() -> None:
    current = require_auth(required_role="teacher")
    if current is None:
        return
    nav(current)
    _uid = current["id"]

    with ui.column().classes("w-full max-w-4xl mx-auto p-4 gap-4"):
        ui.label(t("groups_title")).classes("text-xl font-bold")

        ui.markdown(t("groups_intro")).classes(
            "text-sm text-gray-500 dark:text-gray-400")

        # ── Create / Edit dialog ──────────────────────────────────────────────
        with ui.dialog() as dlg, ui.card().classes("p-5 gap-3 min-w-96"):
            dlg_title = ui.label("").classes("text-lg font-bold mb-1")
            dlg_id    = [None]

            with ui.row().classes("w-full items-center gap-3"):
                ui.label(t("groups_name")).classes("w-32 text-sm")
                dlg_name = ui.input().props("dense outlined").classes("flex-1")
            with ui.row().classes("w-full items-center gap-3"):
                ui.label(t("groups_description")).classes("w-32 text-sm")
                dlg_desc = ui.input().props("dense outlined").classes("flex-1")

            with ui.row().classes("gap-2 mt-1"):
                def _save() -> None:
                    name = dlg_name.value.strip()
                    if not name:
                        ui.notify(t("groups_name_required"), type="negative")
                        return
                    if dlg_id[0] is None:
                        gid = create_group(name, dlg_desc.value.strip())
                        log_action("group.create", user_id=_uid,
                                   entity="computer_group", entity_id=gid,
                                   detail=f"name={name}")
                        ui.notify(t("groups_created").format(name=name), type="positive")
                    else:
                        update_group(dlg_id[0], name, dlg_desc.value.strip())
                        log_action("group.update", user_id=_uid,
                                   entity="computer_group", entity_id=dlg_id[0],
                                   detail=f"name={name}")
                        ui.notify(t("groups_updated").format(name=name), type="positive")
                    dlg.close()
                    _groups_panel.refresh()

                ui.button(t("groups_save"), icon="save", on_click=_save).props("color=primary")
                ui.button(t("groups_cancel"), on_click=dlg.close).props("flat")

        def _open_create() -> None:
            dlg_id[0] = None
            dlg_title.set_text(t("groups_new"))
            dlg_name.set_value("")
            dlg_desc.set_value("")
            dlg.open()

        def _open_edit(g: dict) -> None:
            dlg_id[0] = g["id"]
            dlg_title.set_text(t("groups_edit").format(name=g["name"]))
            dlg_name.set_value(g["name"])
            dlg_desc.set_value(g.get("description") or "")
            dlg.open()

        # ── Assign computers dialog ───────────────────────────────────────────
        with ui.dialog() as assign_dlg, \
             ui.card().classes("p-5 gap-3").style("min-width:480px; max-width:95vw;"):
            assign_title  = ui.label("").classes("text-lg font-bold mb-1")
            ui.label(t("groups_assign_hint")).classes(
                "text-xs text-gray-500 dark:text-gray-400 -mt-1")
            assign_gid    = [None]
            assign_checks: dict[int, ui.checkbox] = {}

            assign_body = ui.column().classes("w-full gap-1")

            def _load_assign(group: dict) -> None:
                assign_gid[0] = group["id"]
                assign_title.set_text(t("groups_assign_title").format(name=group["name"]))
                assign_checks.clear()
                assign_body.clear()
                all_comps  = list_computers()
                in_grp_ids = {c["id"] for c in list_computers_in_group(group["id"])}
                with assign_body:
                    if not all_comps:
                        ui.label(t("groups_no_computers")).classes(
                            "text-sm text-gray-500")
                    for comp in all_comps:
                        cb = ui.checkbox(
                            comp["name"],
                            value=comp["id"] in in_grp_ids,
                        )
                        assign_checks[comp["id"]] = cb

            def _save_assign() -> None:
                gid = assign_gid[0]
                n_added = n_removed = 0
                for cid, cb in assign_checks.items():
                    if cb.value:
                        add_computer_to_group(cid, gid)
                        n_added += 1
                    else:
                        remove_computer_from_group(cid, gid)
                        n_removed += 1
                log_action("group.assign", user_id=_uid,
                           entity="computer_group", entity_id=gid,
                           detail=f"+{n_added}/-{n_removed} computers")
                assign_dlg.close()
                ui.notify(t("groups_assign_saved"), type="positive")
                _groups_panel.refresh()

            with ui.row().classes("gap-2 mt-2"):
                ui.button(t("groups_save"), icon="save",
                          on_click=_save_assign).props("color=primary")
                ui.button(t("groups_cancel"), on_click=assign_dlg.close).props("flat")

        # ── Veyon location import ─────────────────────────────────────────────
        async def _import_veyon_locations() -> None:
            """
            Import Veyon-defined locations as groups.

            list_locations() spawns `veyon-cli config get …` and waits for
            output — that's a blocking subprocess call. Running it on the UI
            thread freezes the page for the full timeout window (up to 10 s)
            on a misconfigured CLI. run_in_executor pushes the work to a
            worker thread; the UI keeps painting.
            """
            import subprocess
            from app.config import get_settings
            from app.core.veyon import list_locations
            cfg = get_settings()
            veyon_cli = cfg.get("veyon_cli", "")
            if not veyon_cli:
                ui.notify(t("groups_veyon_cli_missing"), type="negative")
                return
            loop = asyncio.get_event_loop()
            try:
                locations = await loop.run_in_executor(
                    None, list_locations, veyon_cli,
                )
            except FileNotFoundError:
                ui.notify(t("groups_veyon_not_found").format(path=veyon_cli), type="negative")
                return
            except subprocess.TimeoutExpired:
                ui.notify(t("groups_veyon_timeout"), type="negative")
                return
            except Exception as e:
                ui.notify(t("groups_import_failed").format(e=e), type="negative")
                return
            if not locations:
                ui.notify(t("groups_no_veyon_comps"), type="warning")
                return
            n_groups = n_comps = 0
            for loc in locations:
                gid = get_or_create_group(loc["name"], "Imported from Veyon location")
                for comp in loc["computers"]:
                    cid = upsert_computer(comp["name"], comp["host"])
                    add_computer_to_group(cid, gid)
                    n_comps += 1
                n_groups += 1
            log_action("group.import_veyon", user_id=_uid,
                       detail=f"{n_groups} groups, {n_comps} computers")
            ui.notify(
                t("groups_imported").format(n_groups=n_groups, n_comps=n_comps),
                type="positive",
            )
            _groups_panel.refresh()

        # ── Raw config debug dialog ───────────────────────────────────────────
        with ui.dialog() as raw_dlg, ui.card().classes("p-4").style(
            "min-width:600px; max-width:95vw;"
        ):
            ui.label(t("groups_raw_title")).classes("text-base font-bold mb-2")
            raw_area = ui.textarea().props(
                "outlined readonly rows=20"
            ).classes("w-full font-mono text-xs")
            ui.button(t("groups_raw_close"), on_click=raw_dlg.close).props("flat dense")

        async def _show_raw() -> None:
            """Show the raw veyon-cli JSON. Run subprocess off the UI thread."""
            import json as _json
            import subprocess
            from app.config import get_settings
            cfg = get_settings()
            veyon_cli = cfg.get("veyon_cli", "")
            if not veyon_cli:
                ui.notify(t("groups_veyon_cli_missing"), type="negative")
                return

            def _run() -> str:
                try:
                    r = subprocess.run(
                        [veyon_cli, "config", "get", "BuiltinDirectory/NetworkObjects"],
                        capture_output=True, text=True, timeout=10,
                    )
                    raw = r.stdout.strip()
                    if "=" in raw:
                        raw = raw.split("=", 1)[1].strip()
                    try:
                        raw = _json.dumps(_json.loads(raw), indent=2)
                    except Exception:
                        pass
                    return raw or r.stderr or "(empty)"
                except subprocess.TimeoutExpired:
                    return "(veyon-cli timed out after 10 s)"
                except Exception as e:
                    return f"(error: {e})"

            loop = asyncio.get_event_loop()
            raw_area.set_value(await loop.run_in_executor(None, _run))
            raw_dlg.open()

        # ── Groups panel ──────────────────────────────────────────────────────
        with ui.row().classes("items-center justify-between w-full"):
            ui.label("").classes("flex-1")
            ui.button(
                t("groups_show_raw"),
                icon="code",
                on_click=_show_raw,
            ).props("outline dense color=gray").classes("mr-2")
            ui.button(
                t("groups_import_veyon"),
                icon="download",
                on_click=_import_veyon_locations,
            ).props("outline dense color=teal").classes("mr-2")
            ui.button(t("groups_btn_new"), on_click=_open_create).props(
                "color=primary dense")

        @ui.refreshable
        def _groups_panel() -> None:
            groups = list_groups()
            if not groups:
                with ui.card().classes("w-full"):
                    ui.label(t("groups_no_groups")).classes("text-sm text-gray-500")
                return

            cols = [
                {"name": "name",   "label": t("groups_col_group"),
                 "field": "name",       "sortable": True, "align": "left"},
                {"name": "desc",   "label": t("groups_col_desc"),
                 "field": "description","sortable": False, "align": "left"},
                {"name": "count",  "label": t("groups_col_computers"),
                 "field": "computer_count","sortable": True, "align": "center"},
                {"name": "created","label": t("groups_col_created"),
                 "field": "created_at", "sortable": True, "align": "left"},
                {"name": "actions","label": "",
                 "field": "id",         "align": "right"},
            ]
            rows = [
                {**g, "description": g.get("description") or "—"}
                for g in groups
            ]
            tbl = ui.table(columns=cols, rows=rows, row_key="id").classes("w-full")
            tbl.props("dense flat bordered")

            tbl.add_slot("body-cell-actions", """
                <q-td :props="props">
                    <q-btn flat dense round icon="computer" color="teal"
                           title="Assign computers"
                           @click="$parent.$emit('assign', props.row)"/>
                    <q-btn flat dense round icon="edit" color="blue"
                           title="Edit"
                           @click="$parent.$emit('edit', props.row)"/>
                    <q-btn flat dense round icon="delete" color="red"
                           title="Delete"
                           @click="$parent.$emit('del', props.row)"/>
                </q-td>""")

            def on_assign(e) -> None:
                g = next((x for x in groups if x["id"] == int(e.args["id"])), None)
                if g:
                    _load_assign(g)
                    assign_dlg.open()

            def on_edit(e) -> None:
                g = next((x for x in groups if x["id"] == int(e.args["id"])), None)
                if g:
                    _open_edit(g)

            def on_delete(e) -> None:
                gid = e.args.get("id")
                if gid:
                    gid_int = int(gid)
                    g = next((x for x in groups if x["id"] == gid_int), None)
                    delete_group(gid_int)
                    log_action("group.delete", user_id=_uid,
                               entity="computer_group", entity_id=gid_int,
                               detail=f"name={g['name'] if g else '?'}")
                    ui.notify(t("groups_deleted"), type="warning")
                    _groups_panel.refresh()

            tbl.on("assign", on_assign)
            tbl.on("edit",   on_edit)
            tbl.on("del",    on_delete)

        _groups_panel()
